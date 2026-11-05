"""What this install can actually do — and the button that fixes what it can't.

Until now a user without the ``claude`` CLI found out by pressing **Chat**,
waiting, and reading an error with a shell command in it to copy. That is
backwards twice over: the answer was knowable *before* the click, and "here is a
command" is not a remedy, it is homework.

This module answers both halves. It probes every optional capability and returns
a state, a plain-language reason, and — where one exists — a **remedy
descriptor**: a stable id the routes can execute, a label for a button, and what
the user is being asked to agree to. Never a string of shell to paste.

Four rules hold it together.

*Report the chain, not the destination.* A remedy has prerequisites of its own.
Offering "install the Claude Code CLI" on a machine with no ``npm`` is the same
dead end in a new place, so every remedy resolves to the step that is
**actionable now**: no npm means the offer becomes Node.js, and no way to
install Node either means we say so plainly instead of pretending.

*Never invent an install.* Where no vendor publishes one, the capability says it
needs manual setup and points at the setting that changes it. An honest "we
can't fix this for you" beats a fabricated command that may not work.

*A script piped into a shell is not a package install.* ``curl … | bash`` runs
whatever the host sends back, with nothing verifying it — a long way from
``updater.py``, which refuses to execute a release it has not Ed25519-verified
against a build-time-pinned key (CLAUDE.md rule 8). We still offer the button,
because a command to copy is no safer and much less usable, but the URL is a
constant in this file (never a setting, never anything a caller supplies), the
host is named to the user before anything happens, and it runs only from a
deliberate click plus a confirmation — never from a probe or a page render.

*Doorman is never remedied.* It is the user's own app: detected, described,
never installed and never started (``docs/doorman.md``). Its row here carries no
button, deliberately.

What may actually be *run* lives next door in :mod:`jamgraf.remedies` — this
module decides which step is actionable, that one owns what is executable at all
and is the only place an argv is written.

The shape follows ``doorman.status()`` and ``updater.status()``, the existing
precedent: a module below the routes reports, Settings renders, POST routes act.
"""
from __future__ import annotations

import os
import sys
import time

from . import db, remedies, shellenv, store

OK = "ok"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"

STATE_LABELS = {OK: "Ready", DEGRADED: "Limited", UNAVAILABLE: "Not available"}

# How long a probed answer is reused. The view is rendered on every page (the
# feature buttons need it), and the Ollama probe is a network call.
_TTL = 5.0
_PROBE_TTL = 30.0

_cache: dict[str, dict] = {}
_reach: dict[str, dict] = {}


def refresh(rescan_path: bool = False) -> None:
    """Drop every cached probe, so the next question re-asks the machine.

    Called after a settings change and after a remedy runs — a capabilities view
    that still says "not installed" one second after installing it is worse than
    no view at all.

    ``rescan_path`` additionally forgets the login shell's PATH, which an
    installer may have just added a directory to. It is off by default because
    recovering it re-spawns a login shell, and a settings save has no news about
    where binaries live.
    """
    _cache.clear()
    _reach.clear()
    if rescan_path:
        try:
            shellenv.augmented_path.cache_clear()
        except Exception:                                # pragma: no cover
            pass


# --- looking things up -------------------------------------------------------

def _which(name: str) -> str | None:
    """Is `name` runnable on this machine? One seam, so tests fake one thing.

    Uses the login shell's PATH, not the process's: a Finder-launched .app
    inherits a minimal PATH and would otherwise report every CLI missing.
    """
    return shellenv.which(name)


def _reachable(url: str, timeout: float = 0.6) -> bool:
    """Is an HTTP server answering at `url`? Cached both ways for `_PROBE_TTL`.

    Negative results are cached too, for the same reason ``doorman`` caches
    them: otherwise a machine without Ollama makes a doomed request on every
    single page render.
    """
    now = time.monotonic()
    got = _reach.get(url)
    if got and (now - got["at"]) < _PROBE_TTL:
        return got["up"]
    up = False
    try:
        import httpx

        with httpx.Client(timeout=timeout) as c:
            up = c.get(url).status_code < 500
    except Exception:
        up = False
    _reach[url] = {"at": now, "up": up}
    return up


def _accessibility_trusted() -> bool | None:
    """Has macOS granted this process Accessibility rights? None = can't tell.

    Dictation in the desktop shell works by clicking macOS's own *Start
    Dictation* menu item, which is an Accessibility-gated action. The probe is
    read-only (``AXIsProcessTrusted``, never the prompting variant) and its
    binding is not guaranteed to be present, so "can't tell" is a real answer
    and is reported as such rather than guessed either way.
    """
    if sys.platform != "darwin":
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return None


# --- remedies ----------------------------------------------------------------
#
# What may actually be executed lives in `remedies`, deliberately apart from the
# probing here: this module decides which step is *actionable*, that one owns
# what is *runnable* at all. `_which` is the single seam either side looks a tool
# up through, which is why `apply` hands it over rather than letting `remedies`
# go find its own.

# Which installer produces which CLI. Keyed by binary so the chain below can ask
# "what would make this appear?" without knowing the package name.
_CLI_INSTALLERS = {"claude": "install-claude-cli", "gemini": "install-gemini-cli"}


def describe(remedy_id: str) -> dict | None:
    """The remedy `remedy_id` as this machine would carry it out, or None.

    None means "not something we can do here" — an unknown id, or a platform the
    vendor publishes nothing for. Callers treat both the same way: no button.
    """
    return remedies.describe(remedy_id)


def apply(remedy_id: str) -> dict:
    """Carry out one remedy, then re-probe. Blocking — call from a worker thread.

    Never raises: a remedy that dies has to come back as a *reported* failure,
    because the whole point of the button is that the user isn't left reading
    tea leaves.
    """
    try:
        # Late-bound on purpose: the lookup has to see the machine as it is at
        # each step, including *after* an installer has just changed it.
        return remedies.apply(remedy_id, lambda name: _which(name))
    finally:
        # The machine may have just changed underneath us — including gaining a
        # PATH entry that only the login shell knows about. A view still saying
        # "not installed" a second after installing it is worse than no view.
        refresh(rescan_path=True)


def _manual(label: str, why: str, url: str = "", link: str = "",
            settings: str = "") -> dict:
    return remedies.manual(label, why, url, link, settings)


def _script_step(remedy_id: str, prerequisite: bool = False) -> dict | None:
    """A script remedy, but only when this machine can actually run it.

    Script installs declare what they need to fetch with (`curl`, `powershell`).
    A button offered without that present is a button that fails, which is the
    exact behaviour this module replaced.
    """
    plan = describe(remedy_id)
    if not plan or (plan["needs"] and not _which(plan["needs"])):
        return None
    plan["step"] = "prerequisite" if prerequisite else "direct"
    return plan


def _step(remedy_id: str, prerequisite: bool = False) -> dict | None:
    """A remedy descriptor, tagged with whether it is the fix or the step before it."""
    got = describe(remedy_id)
    if got:
        got["step"] = "prerequisite" if prerequisite else "direct"
    return got


def _cli_remedy(binary: str) -> dict:
    """The actionable step toward having `binary` — the crux of this module.

    The npm CLIs are installed with npm, so a machine without npm cannot be
    offered them; the honest offer there is Node.js. A machine with neither npm
    nor Homebrew can't be offered that either, and gets instructions rather than
    a button that would fail.
    """
    rid = _CLI_INSTALLERS.get(binary)
    if rid and _which("npm"):
        return _step(rid) or _manual(
            f"Install the {binary} CLI",
            f"Jamgraf has no automatic way to install `{binary}` on this system.")
    if sys.platform == "win32":
        # winget installs Node directly, so there is no nvm-shaped middle step
        # here and nothing is piped into a shell on this platform.
        if _which("winget"):
            return _step("install-node-winget", prerequisite=True) or _manual(
                "Install Node.js", "Node.js brings npm, which installs the CLI.")
        return _manual(
            "Install Node.js",
            f"The {binary} CLI is installed with npm, and this machine has "
            f"neither npm nor winget to install npm with. Installing Node.js "
            f"brings npm, after which Jamgraf can install the CLI for you with "
            f"one click.",
            url="https://nodejs.org/en/download", link="nodejs.org")
    if _which("brew"):
        return _step("install-node", prerequisite=True) or _manual(
            "Install Node.js", "Node.js brings npm, which installs the CLI.")
    # No npm and no Homebrew. nvm installs Node without a package manager, so
    # there is still a button here — it just has one more step behind it.
    step = _script_step("install-node-nvm", prerequisite=True)
    if step:
        return step
    return _manual(
        "Install Node.js",
        f"The {binary} CLI is installed with npm, and this machine has neither "
        f"npm nor Homebrew to install npm with. Installing Node.js brings npm, "
        f"after which Jamgraf can install the CLI for you with one click.",
        url="https://nodejs.org/en/download", link="nodejs.org")


def _brew_remedy(remedy_id: str, what: str, winget_id: str = "") -> dict:
    """A package-manager install, or instructions for getting the manager.

    One function for two platforms because the shape is identical: if the
    machine has the package manager there is a button, and if it hasn't there
    are instructions naming what to get first. What Jamgraf will not do either
    way is install the package manager itself — Homebrew is another
    ``curl | bash``, winget is part of Windows and comes from the Store, and
    neither is ours to push onto someone's machine.
    """
    if sys.platform == "win32":
        if winget_id and _which("winget"):
            return _step(winget_id) or _manual(f"Install {what}", "")
        return _manual(
            f"Install {what}",
            f"{what} is installed with winget, which this machine hasn't got. "
            f"winget arrives with App Installer; once it is there, Jamgraf can "
            f"install {what} for you with one click.",
            url="https://learn.microsoft.com/en-us/windows/package-manager/winget/",
            link="learn.microsoft.com")
    if _which("brew"):
        return _step(remedy_id) or _manual(f"Install {what}", "")
    return _manual(
        "Install Homebrew first",
        f"{what} is installed with Homebrew, which this machine hasn't got. "
        f"Once Homebrew is there, Jamgraf can install {what} for you with one "
        f"click.", url="https://brew.sh", link="brew.sh")


# --- the capabilities --------------------------------------------------------

def _cap(cid: str, label: str, powers: str, state: str, reason: str,
         remedy: dict | None = None, where: dict | None = None,
         optional: bool = False, backend: str = "") -> dict:
    return {"id": cid, "label": label, "powers": powers, "state": state,
            "state_label": STATE_LABELS[state], "reason": reason,
            "remedy": remedy, "where": where, "optional": optional,
            "backend": backend}


def _where(label: str, anchor: str) -> dict:
    return {"label": label, "href": f"/settings#{anchor}"}


def _chat_binary() -> str:
    return "gemini" if store.get_setting("chat_provider", "claude") == "gemini" \
        else "claude"


def _chat(door) -> dict:
    binary = _chat_binary()
    if door["ask"]:
        return _cap("chat", "Chat with an article",
                    "The 💬 button on every page.", OK,
                    f"Answered by {door['ask']['label']}.",
                    where=_where("Chat", "chat"), backend=door["ask"]["label"])
    if _which(binary):
        return _cap("chat", "Chat with an article",
                    "The 💬 button on every page.", OK,
                    f"The {binary} CLI is installed and will answer.",
                    where=_where("Chat", "chat"), backend=f"{binary} CLI")
    return _cap("chat", "Chat with an article",
                "The 💬 button on every page.", UNAVAILABLE,
                f"Chat runs the {binary} CLI on this machine, and it isn't "
                f"installed.",
                remedy=_cli_remedy(binary), where=_where("Chat", "chat"))


def _drafting() -> dict:
    """Deliberately not Doorman-aware: ``authoring.draft`` has no Doorman path.

    Saying "ready" here because Doorman is running would be a lie the user only
    discovers by pressing the button — the exact failure this module exists to
    remove.
    """
    binary = _chat_binary()
    powers = "“Draft with AI” in the template and custom-element editors."
    if _which(binary):
        return _cap("drafting", "AI drafting of templates and elements", powers,
                    OK, f"The {binary} CLI is installed and will write the draft.",
                    where=_where("Chat", "chat"), backend=f"{binary} CLI")
    return _cap("drafting", "AI drafting of templates and elements", powers,
                UNAVAILABLE,
                f"Drafting always runs the {binary} CLI here — it reads your "
                f"existing elements over MCP, which the Doorman path can't do — "
                f"and the CLI isn't installed.",
                remedy=_cli_remedy(binary), where=_where("Chat", "chat"))


def _generation(door) -> dict:
    powers = "The editor's ✦ Generate button, and the /api/ai/stream endpoint."
    where = _where("AI generation", "ai-generation")
    if door["ask"]:
        return _cap("generation", "AI generation", powers, OK,
                    f"Answered by {door['ask']['label']}.", where=where,
                    backend=door["ask"]["label"])
    provider = store.get_setting("gen_provider", "anthropic")
    if provider == "ollama":
        url = (store.get_setting("ollama_url", "http://localhost:11434")
               or "http://localhost:11434").rstrip("/")
        model = store.get_setting("gen_model_local", "phi3")
        if _reachable(url):
            return _cap("generation", "AI generation", powers, OK,
                        f"Ollama is answering at {url}.", where=where,
                        backend=f"Ollama · {model}")
        return _cap("generation", "AI generation", powers, UNAVAILABLE,
                    f"Generation is set to Ollama, and nothing is answering at "
                    f"{url}.",
                    remedy=_manual(
                        "Start or install Ollama",
                        "Ollama is a separate app that serves models on this "
                        "machine. Jamgraf neither installs nor starts it — it is "
                        "yours to run. You can also switch generation back to "
                        "Anthropic, which needs no local server.",
                        url="https://ollama.com", link="ollama.com",
                        settings="/settings#ai-generation"),
                    where=where)
    model = store.get_setting("gen_model", "") or "the configured model"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return _cap("generation", "AI generation", powers, OK,
                    f"An Anthropic API key is set, and {model} will answer.",
                    where=where, backend=f"Anthropic · {model}")
    return _cap(
        "generation", "AI generation", powers, DEGRADED,
        "No Anthropic API key is set in Jamgraf's environment. Generation will "
        "still work if the Anthropic SDK finds a signed-in session, and will "
        "come back with an authentication error if it doesn't — which is why "
        "this says 'limited' rather than switching the button off.",
        remedy=_manual(
            "Set an Anthropic key",
            "The key is read from the environment Jamgraf starts in "
            "(ANTHROPIC_API_KEY), not from a field here, so this is not "
            "something Jamgraf can fill in for you. Switching the provider to "
            "Ollama avoids needing one at all.",
            settings="/settings#ai-generation"),
        where=where)


def _images(door) -> dict:
    powers = "The ✨ button in the editor toolbar, and the generate_image MCP tool."
    where = _where("Images", "images")
    if door["image"]:
        return _cap("images", "Image generation", powers, OK,
                    f"Rendered by {door['image']['label']}.", where=where,
                    backend=door["image"]["label"])
    cli = (store.get_setting("image_cli", "agy") or "agy").strip()
    if _which(cli):
        return _cap("images", "Image generation", powers, OK,
                    f"The {cli} CLI is installed and will render the image.",
                    where=where, backend=f"{cli} CLI")
    # A remedy belongs to the *tool that is configured*, not to the capability.
    # `agy` is only the default; a user who has pointed this at something else
    # gets instructions, because we have no idea how their tool is installed.
    if cli == "agy":
        remedy = _agy_remedy()
    else:
        remedy = _manual(
            f"Install {cli} yourself",
            f"Jamgraf knows of no install path for `{cli}` — it is whatever you "
            f"named in the image CLI setting — so it won't guess at a command "
            f"that might not work. Install it yourself, or name a different "
            f"tool in the setting below.",
            settings="/settings#images")
    return _cap("images", "Image generation", powers, UNAVAILABLE,
                f"Image generation runs the {cli} CLI on this machine, and it "
                f"isn't installed.", remedy=remedy, where=where)


def _agy_remedy() -> dict:
    """The Antigravity installer — offered only when `curl` can carry it out."""
    plan = _script_step("install-agy-cli")
    if plan:
        return plan
    return _manual(
        "Install the Antigravity CLI",
        "Google publishes an installer script for it, but this machine hasn't "
        "got the tool needed to fetch it. Install `agy` yourself, or name a "
        "different image CLI in the setting below.",
        settings="/settings#images")


def _speech(door) -> dict:
    powers = "Listen, the per-section 🔊 speakers, and “Say this”."
    if door["voices"]:
        return _cap("speech", "Read aloud", powers, OK,
                    "Doorman's voices are in use here, which sound considerably "
                    "better than the browser's. Nothing to set up.",
                    backend="Doorman")
    return _cap("speech", "Read aloud", powers, OK,
                "Your browser's own voice reads pages aloud. Doorman, if you "
                "happen to run it, sounds better — but nothing is missing.",
                backend="browser")


def _dictation() -> dict:
    from . import config

    powers = "The 🎤 buttons beside the chat box and the image description."
    if not config.is_desktop():
        return _cap("dictation", "Dictation", powers, OK,
                    "In a browser, dictation uses the browser's own speech "
                    "recognition. Nothing to install.", backend="browser")
    if sys.platform != "darwin":
        # The window here is WebView2, which is Chromium, so the page uses the
        # web speech route rather than a native one -- there is no equivalent of
        # macOS Dictation to click, and inventing one is not on offer (rule 9).
        # What an embedded WebView2 provides is not ours to promise: Chrome's
        # speech service is not part of the runtime, so this is reported as
        # "may not", with the route that definitely works named. The mic says so
        # itself if the browser refuses it.
        return _cap("dictation", "Dictation", powers, DEGRADED,
                    "Jamgraf's own window has no native dictation on this "
                    "platform, so the 🎤 uses the web speech API --- which an "
                    "embedded WebView2 may not provide. Opening Jamgraf in a "
                    "browser at the same address always has it.",
                    backend="browser")
    trusted = _accessibility_trusted()
    remedy = _step("open-accessibility")
    if trusted is True:
        return _cap("dictation", "Dictation", powers, OK,
                    "macOS has granted Jamgraf Accessibility rights, so the 🎤 "
                    "button can start macOS Dictation for you.", backend="macOS")
    if trusted is False:
        return _cap("dictation", "Dictation", powers, UNAVAILABLE,
                    "The desktop app starts macOS Dictation by clicking its own "
                    "menu item, which macOS only allows with Accessibility "
                    "permission — and Jamgraf hasn't been given it.",
                    remedy=remedy)
    return _cap("dictation", "Dictation", powers, DEGRADED,
                "Jamgraf can't tell whether macOS has granted it Accessibility "
                "permission. Dictation will say so plainly if it is refused.",
                remedy=remedy)


def _doorman_cap(door) -> dict:
    """Informational, and it stays that way.

    No remedy, no button, no offer — Doorman is the user's own app and Jamgraf
    must never install or start it (CLAUDE.md; ``docs/doorman.md``). The row
    exists so the view is honest about what is and isn't in play, not so it can
    be turned on from here.
    """
    powers = "Better speech, and it can answer generation, chat and images."
    where = _where("Doorman", "doorman")
    if not door["running"]:
        return _cap("doorman", "Doorman", powers, UNAVAILABLE,
                    "Not running — which is the ordinary case, not a problem. "
                    "Everything above works without it. Jamgraf never starts or "
                    "installs it; it is your app to open.",
                    where=where, optional=True)
    if not door["enabled"]:
        return _cap("doorman", "Doorman", powers, DEGRADED,
                    "Running, but you've asked Jamgraf not to use it. That is a "
                    "reasonable thing to want and nothing is broken.",
                    where=where, optional=True)
    offers = [name for name, on in (("speech", bool(door["voices"])),
                                    ("generation and chat", bool(door["ask"])),
                                    ("images", bool(door["image"]))) if on]
    return _cap("doorman", "Doorman", powers, OK,
                ("Running, and offering " + ", ".join(offers) + "."
                 if offers else
                 "Running, but this Doorman offers none of the capabilities "
                 "Jamgraf can use. Normal for an older version, and it needs "
                 "nothing from you."),
                where=where, optional=True)


def _updates() -> dict:
    from . import appconfig, updater

    powers = "Jamgraf updating itself, with the release signature verified first."
    where = _where("Updates", "updates")
    ok, why = updater.can_update()
    if ok:
        waiting = appconfig.get("update_last_available")
        return _cap("updates", "Automatic updates", powers, OK,
                    (f"{waiting} is ready to install." if waiting else
                     "This install can update itself."), where=where)
    # Rule 9: name the step that is actionable now. A pip install cannot swap
    # its own bundle and never will, so "replace it with a signed release build"
    # is not advice there -- `pip install -U` is. Still a manual remedy and not
    # a button: upgrading someone's Python environment is theirs to run.
    if updater.install_kind() == "package":
        detail = ("Nothing to press: Jamgraf is installed here as a Python "
                  "package, so it updates the way you installed it --- "
                  "`pip install -U jamgraf` (or uv/pipx), then start it again. "
                  "Only the signed .app can update itself from inside.")
    else:
        detail = ("Nothing to press: this isn't a missing tool, it is how this "
                  "copy of Jamgraf was built or where it is installed. "
                  "Replace it with a signed release build to get automatic "
                  "updates.")
    return _cap("updates", "Automatic updates", powers, UNAVAILABLE,
                f"Updating is off for this install: {why}.",
                remedy=_manual("Update from here", detail,
                               settings="/settings#updates"),
                where=where, optional=True)


def _public_link() -> dict:
    powers = "“Share with someone remote” — a temporary public https address."
    where = _where("Share with someone remote", "public-link")
    if _which("cloudflared"):
        return _cap("public-link", "Temporary public link", powers, OK,
                    "cloudflared is installed, so a public link can be opened "
                    "once a sharing password is set.", where=where)
    return _cap("public-link", "Temporary public link", powers, UNAVAILABLE,
                "It runs Cloudflare's `cloudflared` client, which isn't "
                "installed on this machine.",
                remedy=_brew_remedy("install-cloudflared", "cloudflared",
                                    winget_id="install-cloudflared-winget"),
                where=where, optional=True)


def _kahala_sync() -> dict:
    """Syncing with a Kahala server (issue #58).

    Three states, and the difference matters: **no secure store** is a property
    of the build and there is nothing to press; **not signed in** is one click
    away and the button says so; **signed in** is ready. Not-linked is
    deliberately not a failure -- most wikis are local on purpose, so a wiki with
    no link is working as intended, not degraded.
    """
    from . import kahala, kahalaauth, secretstore, wikis

    powers = "Pushing a wiki up to Kahala and pulling one back down."
    where = {"label": "Kahala", "href": "/kahala"}

    if not secretstore.available():
        return _cap("kahala", "Kahala sync", powers, UNAVAILABLE,
                    secretstore.unavailable_reason(),
                    remedy=_manual(
                        "Open the Kahala pane",
                        "Nothing to install: this build has no secure store to "
                        "keep a sign-in token in, and Jamgraf will not fall back "
                        "to writing one into a plain file.",
                        link="/kahala"),
                    where=where, optional=True)

    can, why = kahalaauth.can_sign_in()
    if not can:
        return _cap("kahala", "Kahala sync", powers, UNAVAILABLE, why,
                    where=where, optional=True)

    if not kahalaauth.signed_in():
        return _cap("kahala", "Kahala sync", powers, DEGRADED,
                    "Not signed in to Kahala, so push and pull will refuse.",
                    remedy=_manual(
                        "Sign in to Kahala",
                        "Opens Kahala's own sign-in page in your browser and "
                        "comes back. Jamgraf never sees your password.",
                        link="/kahala"),
                    where=where, optional=True)

    linked = sum(1 for w in wikis.list_wikis() if wikis.get_link(w["slug"]))
    return _cap("kahala", "Kahala sync", powers, OK,
                (f"Signed in; {linked} wiki{'' if linked == 1 else 's'} linked."
                 if linked else
                 "Signed in. No wiki is linked to a Kahala one yet."),
                where=where, optional=True)


def _semantic_search() -> dict:
    from . import db as _db

    powers = "The vector half of search; keyword search never depends on it."
    if _db.VEC_AVAILABLE:
        return _cap("semantic-search", "Semantic search", powers, OK,
                    "sqlite-vec loaded, so search fuses meaning-based hits with "
                    "keyword ones.")
    return _cap("semantic-search", "Semantic search", powers, DEGRADED,
                "sqlite-vec didn't load in this process, so search is "
                "keyword-only. It ships inside Jamgraf, so there is nothing for "
                "you to install — searching still works, with less recall.",
                optional=True)


def _safe(cid: str, label: str, build, *args) -> dict:
    """One capability, or a row saying we couldn't work it out.

    ``states()`` is on the render path for *every* page now, so a probe that
    throws must not take the wiki down with it. It also must not quietly report
    the capability as broken: an unknown answer is `degraded`, which leaves the
    feature's own button alone rather than greying out something that works.
    """
    try:
        return build(*args)
    except Exception as exc:                             # pragma: no cover
        return _cap(cid, label, "", DEGRADED,
                    f"Jamgraf couldn't work out whether this is available "
                    f"({str(exc)[:120]}). The feature is left switched on; try "
                    f"it and it will say if something is missing.")


def report() -> list[dict]:
    """Every capability, its state, and the step that would fix it. Never raises.

    Cached for a few seconds and keyed by wiki, because several of these read
    per-wiki settings and this is rendered on every page.
    """
    wiki = db.active_wiki()
    now = time.monotonic()
    got = _cache.get(wiki)
    if got and (now - got["at"]) < _TTL:
        return got["value"]

    from . import doorman

    try:
        door = doorman.status()
    except Exception:                                    # pragma: no cover
        door = {"running": False, "enabled": False, "ask": None, "image": None,
                "voices": []}

    caps = [
        _safe("chat", "Chat with an article", _chat, door),
        _safe("drafting", "AI drafting of templates and elements", _drafting),
        _safe("generation", "AI generation", _generation, door),
        _safe("images", "Image generation", _images, door),
        _safe("speech", "Read aloud", _speech, door),
        _safe("dictation", "Dictation", _dictation),
        _safe("doorman", "Doorman", _doorman_cap, door),
        _safe("updates", "Automatic updates", _updates),
        _safe("public-link", "Temporary public link", _public_link),
        _safe("semantic-search", "Semantic search", _semantic_search),
        _safe("kahala", "Kahala sync", _kahala_sync),
    ]
    _cache[wiki] = {"at": now, "value": caps}
    return caps


def states() -> dict[str, str]:
    """``{capability id: state}`` — what the templates gate a button on."""
    return {c["id"]: c["state"] for c in report()}


def get(cap_id: str) -> dict | None:
    for c in report():
        if c["id"] == cap_id:
            return c
    return None
