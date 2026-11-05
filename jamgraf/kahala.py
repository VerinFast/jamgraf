"""Linking a local wiki to a Kahala one: clone it down, push it up, pull it back.

Kahala is the multi-tenant server half of the same wiki (good-place
``services/kahala``). Both ends speak the shared, version-gated
``wiki-interchange`` format -- Jamgraf through its vendored copy -- so this
module is only the **wire**: it moves a whole-wiki bundle over Kahala's
``/api/interchange/*`` routes and hands it to :mod:`store`, which is where every
write already goes.

What travels, and what deliberately does not
--------------------------------------------
The bundle is **content only** (CLAUDE.md rule 7). Pages as canonical Y.Docs,
hierarchy by slug, order, elements, templates, images. Never ``tenant_id`` or
``wiki_id``: Kahala re-attaches those from the caller's own server-side scope,
and the format's own guard refuses a payload carrying them. Embeddings are never
shipped either -- they are derived, and they are regenerated locally on import.

Three facts worth being blunt about
-----------------------------------
1. **Push and pull both merge; neither deletes.** Kahala's importer merges by
   slug and removes nothing, and so does ours. A page you deleted locally will
   still be on Kahala after a push, and will come back on the next pull. That is
   the honest behaviour of a snapshot round-trip and the UI says so in those
   words -- calling it "upload" or "sync" would set up exactly the surprise
   issue #58 warned about.
2. **The link is local.** ``wikis.get_link`` keeps the address and the remote
   slug in the registry, never in the wiki file, so it cannot travel to whoever
   you share a wiki with. No credential is stored there at all.
3. **We never follow a redirect.** Every request carries a bearer token, and
   ``httpx`` re-sends the ``Authorization`` header when it follows a 3xx -- which
   hands the token to whatever host the response names. All of these clients set
   ``follow_redirects=False`` and treat a 3xx as a refusal to be reported.

Reachability is never assumed. An unreachable Kahala, an expired sign-in, and a
wiki the account cannot see are all normal states that report themselves and
change nothing locally.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from . import db, kahalaauth, secretstore, store, wikis
from .vendor import wiki_interchange as wi

# A real wiki is ~215 pages / ~57MB, so reads and writes get room while the
# connect timeout stays short -- an unreachable host should fail quickly.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

_UA = "Jamgraf"


# --- the link ----------------------------------------------------------------

# A wiki's name on Kahala is **one path segment** of the interchange URL. These
# characters would make it more than that: `/` and `\` add segments, `?` and `#`
# end the path and start a query or fragment, and `%` would be read as the start
# of an escape. `..` is the same problem spelled differently.
#
# The name is typed by the owner, so this is not an injection from outside -- but
# a mistyped name must be *reported*, not quietly used to address some other
# route on that host with the bearer token attached. Refused here, and escaped
# again in `_wiki_url` on the way out, because the two guards fail differently:
# this one tells the person, that one holds even if some later caller skips this.
_BAD_IN_REMOTE = ("/", "\\", "?", "#", "%")


def _bad_remote(remote: str) -> str:
    """Why ``remote`` can't name a wiki on Kahala, or "" when it can."""
    if remote in (".", ".."):
        return f"“{remote}” isn't the name of a wiki on Kahala."
    for ch in _BAD_IN_REMOTE:
        if ch in remote:
            return (f"A wiki's name on Kahala can't contain “{ch}”. Use the name "
                    "as it appears there, with nothing around it.")
    if any(ch < " " or ch == "\x7f" for ch in remote):
        return "That wiki name contains a character that can't be sent."
    # Kahala addresses a wiki by its SLUG, not its display name, and its Wikis
    # page shows both ("StartupOS /startupos") -- so the name is the one that
    # catches the eye and the slug is the one the wire needs. Linking to the
    # display name produces a 404 that reads like a permissions problem, hours
    # later, at push time. Caught here instead, with the answer in hand: both
    # sides derive slugs with the same `slugify`, so we can say what it will be.
    guess = wikis.slugify(remote)
    if guess != remote:
        return (f"Kahala addresses a wiki by its slug, not its display name — "
                f"its Wikis page shows both, like “{remote} /{guess}”. "
                f"Use “{guess}”. (If that wiki's slug ends in a number, Kahala "
                "hit a name collision when it was created; copy the slug from "
                "that page instead.)")
    return ""


def link(slug: str, base_url: str, remote: str) -> dict:
    """Record that local wiki ``slug`` corresponds to ``remote`` on ``base_url``."""
    base_url = (base_url or "").strip().rstrip("/")
    remote = (remote or "").strip()
    if not wikis.exists(slug):
        return _err("There is no local wiki by that name.")
    if not base_url or not remote:
        return _err("A Kahala address and the name of the wiki there are both "
                    "needed.")
    if not kahalaauth.secure_url(base_url):
        return _err(f"{base_url} isn't an https address. A sign-in token is "
                    "sent with every request, so this has to be https.")
    wrong = _bad_remote(remote)
    if wrong:
        return _err(wrong)
    wikis.set_link(slug, base_url, remote)
    return {"ok": True, "error": "", "base_url": base_url, "remote": remote}


def unlink(slug: str) -> dict:
    """Forget the link. Nothing is deleted on either side."""
    wikis.clear_link(slug)
    return {"ok": True, "error": ""}


def suggested_link(slug: str) -> dict:
    """Sensible defaults for the link form: {base_url, remote}.

    ``remote`` defaults to this wiki's **own slug**, because that is nearly
    always what it is called on the other side, and because the field wants a
    slug rather than a display name -- Kahala derives its slug from the name you
    type into its form, so "StartupOS" and "startupos" are not interchangeable
    there. Offering the local slug also matches what a create-on-Kahala call
    would ask for.

    ``base_url`` comes from any wiki already linked on this machine: people have
    one Kahala, not one per wiki, and re-typing it is the step where a typo turns
    into a 404 that reads like a permissions problem.
    """
    for other in wikis.list_wikis():
        lk = wikis.get_link(other["slug"])
        if lk and lk.get("base_url"):
            return {"base_url": lk["base_url"], "remote": slug}
    return {"base_url": "", "remote": slug}


def status(slug: str) -> dict:
    """Everything the UI and the MCP tool need, without touching the network."""
    can, why = kahalaauth.can_sign_in()
    lk = wikis.get_link(slug) or {}
    return {
        "wiki": slug,
        "linked": bool(lk),
        "base_url": lk.get("base_url", ""),
        "remote": lk.get("remote", ""),
        "signed_in": kahalaauth.signed_in() if can else False,
        "can_sign_in": can,
        "reason": why,
        "secure_store": secretstore.available(),
        # Named, not assumed: this pane used to tell every reader the token was
        # in "your Keychain", which on Windows names something they do not have
        # and hides the store that actually holds it.
        "store_name": secretstore.store_name(),
        # WHICH Kahala the sign-in is for. "Signed in." on its own is a claim
        # the reader cannot check, and there is more than one Kahala — so name
        # the realm, and let the pane say that one sign-in covers the install.
        "issuer": kahalaauth.issuer(),
        # Where to create a wiki on that Kahala. Jamgraf cannot do it: creating
        # one is `POST /wikis/create`, a session-authenticated browser form, and
        # our bearer token is only accepted on `/api/*`. So we send the person
        # to the page they are already signed in to rather than pretending.
        "manage_url": f"{lk['base_url']}/wikis" if lk.get("base_url") else "",
    }


# --- the operations ----------------------------------------------------------


def push(slug: str, full: bool = False) -> dict:
    """Send the local wiki up, merging into the linked remote wiki.

    Incremental by default: ask Kahala what it holds, send only the bytes it
    lacks. Falls back to the whole bundle when the far end is too old to carry
    definitions incrementally (see :func:`_speaks_v3`) or when ``full`` is set.

    Nothing on Kahala is deleted: its importer merges by slug. A push needs
    OWNER/ADMIN on that wiki there, which is Kahala's gate, not ours -- a 403
    comes back as exactly that.
    """
    ready = _ready(slug)
    if not ready["ok"]:
        return ready
    lk, token = ready["link"], ready["token"]

    full_reason = ""
    if not full:
        out = _push_incremental(slug, lk, token)
        if "fallback" not in out:
            return out
        full_reason = out["fallback"]

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / f"{slug}.zip"
        try:
            with _bind(slug), bundle.open("wb") as fh:
                store.export_wiki_bundle(dest=fh)
        except Exception as exc:
            return _err(f"The wiki could not be packed up: {exc}")

        url = _wiki_url(lk, "/snapshot")
        try:
            with bundle.open("rb") as fh, _client() as client:
                resp = client.post(
                    url, headers=_auth(token),
                    files={"file": (f"{slug}.zip", fh, "application/zip")})
        except httpx.HTTPError as exc:
            return _err(_unreachable(lk["base_url"], exc))

    bad = _refusal(resp, lk, "push")
    if bad:
        return bad
    summary = _json(resp)
    return {"ok": True, "error": "", "action": "push", "mode": "full",
            "detail": _counts(summary),
            "note": ("Kahala merged this in. Nothing there was deleted."
                     + (f" {full_reason}" if full_reason else ""))}


def pull(slug: str, full: bool = False) -> dict:
    """Bring the linked remote wiki down, merging into the local one.

    Incremental by default, falling back to the whole bundle when the far end
    predates spec v3 or when a page ends up pointing at an image that did not
    survive the trip. ``full`` forces the bundle.
    """
    ready = _ready(slug)
    if not ready["ok"]:
        return ready
    lk, token = ready["link"], ready["token"]

    reason = ""
    if not full:
        out = _pull_incremental(slug, lk, token)
        if "fallback" not in out:
            return out
        reason = out["fallback"]

    out = _download_into(slug, lk, token, action="pull")
    if out["ok"] and reason:
        out["note"] = f"{out['note']} {reason}"
    return out


def clone(base_url: str, remote: str, name: str = "") -> dict:
    """Create a **new** local wiki from a remote one, and link it.

    Refuses to write into an existing wiki: ``wikis.create_wiki`` always mints a
    fresh slug, so a clone can never silently land on top of something you have.
    """
    base_url = (base_url or "").strip().rstrip("/")
    remote = (remote or "").strip()
    if not base_url or not remote:
        return _err("A Kahala address and the name of the wiki there are both "
                    "needed.")
    if not kahalaauth.secure_url(base_url):
        return _err(f"{base_url} isn't an https address. A sign-in token is "
                    "sent with every request, so this has to be https.")
    wrong = _bad_remote(remote)
    if wrong:
        return _err(wrong)
    token = kahalaauth.access_token()
    if not token:
        return _err(_signed_out())

    lk = {"base_url": base_url, "remote": remote}
    local = wikis.create_wiki(name.strip() or remote)
    out = _download_into(local, lk, token, action="clone")
    if not out["ok"]:
        # A clone that fetched nothing leaves an empty wiki behind, which reads
        # as "it worked and the wiki is empty". Take it back out instead.
        wikis.delete_wiki(local)
        return out
    wikis.set_link(local, base_url, remote)
    out["wiki"] = local
    return out


# --- the incremental path (issue #58) -----------------------------------------
#
# The bundle ships every page in full: re-syncing a 215-page wiki that differs
# by a paragraph pushes ~57MB. The changelog ships only the missing bytes,
# usually a few kilobytes.
#
# Each of these returns either a finished result, or ``_fallback(reason)``
# meaning "use the bundle instead, and tell them why". A real failure comes back
# as an ordinary ``ok: False`` result and does NOT fall back, because retrying a
# genuine error the expensive way just fails again slower.

_OLD_PEER = ("That Kahala is on an older interchange version, so the whole wiki "
             "was transferred instead of just the changes.")

_BROKEN_IMAGES = ("Some pages came back pointing at images that didn't survive "
                  "the incremental transfer, so the whole wiki was fetched to "
                  "repair them.")


def _speaks_v3(body: bytes) -> bool:
    """Whether the peer's envelope carries the spec-v3 sections at all.

    Not a version check -- a version number would only tell us what the peer
    *claims*. Both v3 envelope kinds always emit ``elements``/``templates``/
    ``images``, empty or not, so the key's **absence** identifies a peer that
    predates them. That distinction matters: a v3 peer with nothing new to send
    and an old peer that cannot send definitions at all produce identical
    content, and only one of them is safe to sync incrementally.
    """
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError):
        return False
    return isinstance(envelope, dict) and "elements" in envelope


def _push_incremental(slug: str, lk: dict, token: str) -> dict:
    base = _wiki_url(lk)
    try:
        with _client() as client:
            their = client.get(f"{base}/wiki-state-vector", headers=_auth(token))
    except httpx.HTTPError as exc:
        return _err(_unreachable(lk["base_url"], exc))
    if their.status_code == 404:
        # Only an ABSENT ROUTE means an older Kahala. A 404 that says "No such
        # wiki" means the wiki isn't there, and falling back would push ~57MB
        # before failing for the reason we already had in our hands.
        if _reason(their).strip().lower() in ("not found", "not_found"):
            return _fallback(_OLD_PEER)
        bad = _refusal(their, lk, "push")
        return bad if bad else _fallback(_OLD_PEER)
    bad = _refusal(their, lk, "push")
    if bad:
        return bad
    if not _speaks_v3(their.content):
        return _fallback(_OLD_PEER)

    try:
        peer = wi.WikiStateVector.deserialize(their.content)
        with _bind(slug):
            log = store.wiki_changelog_for(peer)
        payload = log.serialize()
    except wi.InterchangeError as exc:
        return _err(f"Kahala's state vector was refused rather than merged: {exc}")
    except Exception as exc:
        return _err(f"The changes could not be gathered up: {exc}")

    try:
        with _client() as client:
            resp = client.post(
                f"{base}/wiki-updates",
                headers={**_auth(token), "Content-Type": "application/json"},
                content=payload)
    except httpx.HTTPError as exc:
        return _err(_unreachable(lk["base_url"], exc))
    if resp.status_code == 409:
        return _fallback(_OLD_PEER)      # version gate refused it: use the bundle
    bad = _refusal(resp, lk, "push")
    if bad:
        return bad
    return {"ok": True, "error": "", "action": "push", "mode": "incremental",
            "detail": _counts(_json(resp)),
            "note": "Only the changes were sent. Nothing on Kahala was deleted."}


def _pull_incremental(slug: str, lk: dict, token: str) -> dict:
    base = _wiki_url(lk)
    try:
        with _bind(slug):
            ours = store.wiki_state_vector().serialize()
    except Exception as exc:
        return _err(f"This wiki's state could not be summarised: {exc}")

    try:
        with _client() as client:
            resp = client.post(
                f"{base}/wiki-changelog",
                headers={**_auth(token), "Content-Type": "application/json"},
                content=ours)
    except httpx.HTTPError as exc:
        return _err(_unreachable(lk["base_url"], exc))
    if resp.status_code in (404, 409):
        return _fallback(_OLD_PEER)      # older route, or the version gate
    bad = _refusal(resp, lk, "pull")
    if bad:
        return bad
    if not _speaks_v3(resp.content):
        return _fallback(_OLD_PEER)

    try:
        log = wi.WikiChangelog.deserialize(resp.content)
        with _bind(slug):
            summary = store.apply_wiki_changelog(log)
    except wi.InterchangeError as exc:
        return _err(f"Kahala's changes were refused rather than merged: {exc}")
    except Exception as exc:
        return _err(f"Kahala's changes could not be merged: {exc}")

    # An update is a Yjs payload naming the sender's image ids. `store` remaps
    # them after the merge, but if anything did not line up the page renders a
    # broken image and says nothing -- so check, and repair with a full bundle
    # rather than leave it.
    with _bind(slug):
        broken = store.unresolved_image_refs(
            [e.slug for e in log.pages if e.changelog is not None])
    if broken:
        return _fallback(_BROKEN_IMAGES)

    return {"ok": True, "error": "", "action": "pull", "mode": "incremental",
            "wiki": slug, "detail": _counts(summary),
            "note": "Only the changes were fetched. Nothing local was deleted."}


# --- internals ---------------------------------------------------------------


def _download_into(slug: str, lk: dict, token: str, action: str) -> dict:
    url = _wiki_url(lk, "/snapshot")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "remote.zip"
        try:
            with _client() as client:
                with client.stream("GET", url, headers=_auth(token)) as resp:
                    bad = _refusal(resp, lk, action, streaming=True)
                    if bad:
                        return bad
                    with bundle.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            return _err(_unreachable(lk["base_url"], exc))

        # The format's own gates (version, content-only, a tampered image) raise
        # before the first write, and that is the failure a peer's bundle
        # realistically produces -- but a local write can fail too, and then part
        # of the wiki has changed. Saying "refused" in that case would be a
        # comforting sentence that isn't true, so `store` says when it started
        # writing and the two cases report themselves differently.
        started = False

        def writing():
            nonlocal started
            started = True

        try:
            with _bind(slug), bundle.open("rb") as fh:
                summary = store.import_wiki_bundle(fh, author="kahala",
                                                   on_writes_begin=writing)
        except Exception as exc:
            if started:
                return _err(
                    f"Kahala's copy was only partly merged: {exc}. Nothing was "
                    "deleted, and every page that did arrive is an ordinary "
                    "versioned page -- running the same transfer again finishes "
                    "it.")
            return _err(f"Kahala's copy was refused rather than merged: {exc}")

    return {"ok": True, "error": "", "action": action, "wiki": slug,
            "mode": "full", "detail": _counts(summary),
            "note": "Merged into this wiki. Nothing local was deleted."}


@contextlib.contextmanager
def _bind(slug: str):
    """Run the repository calls against ``slug``, whatever the caller was on."""
    token = db.current_wiki.set(slug)
    try:
        yield
    finally:
        db.current_wiki.reset(token)


def _wiki_url(lk: dict, path: str = "") -> str:
    """Kahala's address for the linked wiki, with its name escaped as one segment.

    ``_bad_remote`` already refuses the characters that would matter, so this is
    the second of the two guards: a name that somehow reached the registry
    anyway (an older link recorded before that check existed) addresses the wiki
    it names or fails, rather than reaching a different route on that host with
    the bearer token attached.
    """
    base = lk["base_url"]
    return f"{base}/api/interchange/wikis/{quote(lk['remote'], safe='')}{path}"


def _client() -> httpx.Client:
    # follow_redirects=False is the load-bearing argument here, not a default
    # being restated: these requests carry a bearer token.
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=False,
                        headers={"User-Agent": _UA})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ready(slug: str) -> dict:
    """The link and a usable token, or a reported reason there isn't one."""
    if not wikis.exists(slug):
        return _err("There is no local wiki by that name.")
    lk = wikis.get_link(slug)
    if not lk:
        return _err("This wiki isn't linked to a Kahala wiki yet.")
    token = kahalaauth.access_token()
    if not token:
        return _err(_signed_out())
    return {"ok": True, "error": "", "link": lk, "token": token}


def _signed_out() -> str:
    can, why = kahalaauth.can_sign_in()
    if not can:
        return why
    return "You aren't signed in to Kahala, or the sign-in has expired."


def _refusal(resp, lk: dict, action: str, streaming: bool = False) -> dict | None:
    """Turn a non-200 into a sentence that says what to do. None when it's fine."""
    code = resp.status_code
    if code == 200:
        return None
    if streaming:
        resp.read()          # a streamed error body still has to be drained

    if 300 <= code < 400:
        where = resp.headers.get("location", "somewhere else")
        return _err(f"{lk['base_url']} redirected to {where}. Jamgraf does not "
                    "follow redirects here, because that would hand your "
                    "sign-in token to whatever host the redirect names. Check "
                    "the address.")
    if code == 401:
        return _err("Kahala didn't accept the sign-in. It may have expired — "
                    "sign in again and retry.")
    if code == 403:
        return _err(f"You're signed in, but this account isn't an owner or "
                    f"admin of “{lk['remote']}” on Kahala, and only they may "
                    "push a whole wiki.")
    if code == 404:
        # Two different 404s wear the same clothes, and telling them apart is
        # the difference between "fix your link" and "that server is too old".
        # FastAPI answers an unmatched route with {"detail": "Not Found"};
        # Kahala's own handler says "No such wiki". Guessing between them sent
        # two separate investigations down the wrong road, so: report what the
        # server said, first, and interpret second.
        said = _reason(resp)
        if said.strip().lower() in ("not found", "not_found"):
            return _err(
                f"{lk['base_url']} has no {action} route at "
                f"{_wiki_url(lk, '')} — that is the web framework's own "
                "\"Not Found\", not Kahala saying the wiki is missing. This "
                "Kahala is too old for the interchange API, or it isn't a "
                "Kahala. Nothing is wrong with the link or the wiki name.")
        hint = ""
        slugged = wikis.slugify(lk["remote"])
        if slugged != lk["remote"]:
            # Almost certainly this: the link holds a display name and the wire
            # wants a slug. Say it first, and say what to do about it.
            hint = (f" This link uses “{lk['remote']}”, which looks like a "
                    f"display name — Kahala addresses wikis by slug, so it is "
                    f"probably “{slugged}”. Forget this link and make it again "
                    "with the slug from Kahala's Wikis page.")
        return _err(f"Kahala said “{said}” for “{lk['remote']}” on "
                    f"{lk['base_url']}.{hint} A push cannot create a wiki — it "
                    "has to exist there first, and “Open Kahala” above is where "
                    "to make it. Kahala also answers this way for a wiki that "
                    "belongs to another tenant, so check which account you "
                    "signed in as.")
    if code == 409:
        return _err("Kahala refused the transfer as incompatible rather than "
                    f"merging it: {_reason(resp)}. Nothing changed on either "
                    "side. This means the two ends are on different "
                    "wiki-interchange versions.")
    return _err(f"Kahala answered {code} to the {action}: {_reason(resp)}")


def _reason(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:200] or "no reason given"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)[:200]
    return str(body)[:200]


def _json(resp) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _counts(summary: dict) -> str:
    """A short "what landed" line from either side's import summary."""
    if not isinstance(summary, dict):
        return ""
    bits = []
    for key in ("pages", "images", "elements", "templates"):
        n = summary.get(key)
        if isinstance(n, int) and n:
            bits.append(f"{n} {key if n != 1 else key[:-1]}")
    skipped = summary.get("skipped")
    if isinstance(skipped, list) and skipped:
        bits.append(f"{len(skipped)} skipped")
    return ", ".join(bits)


def _unreachable(base_url: str, exc: Exception) -> str:
    return (f"Couldn't reach {base_url}: {exc.__class__.__name__}. Nothing was "
            "changed here.")


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _fallback(reason: str) -> dict:
    """Not a failure: "the incremental path can't do this, use the bundle"."""
    return {"ok": False, "error": "", "fallback": reason}
