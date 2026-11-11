# Syncing with Kahala

Kahala is the multi-tenant, server-side half of the same wiki (good-place
`services/kahala`). Jamgraf can **link** a local wiki to one there, **push** it
up, **pull** it back down, or **clone** a remote wiki into a new local one.

Both ends speak the shared, version-gated `wiki-interchange` format — Kahala
through `packages/wiki-interchange`, Jamgraf through its
[vendored copy](vendoring.md) — so nothing here invents a protocol. This is only
the wire and the credential.

* `jamgraf/kahala.py` — the link record and the transfers.
* `jamgraf/kahalaauth.py` — OpenID Connect, authorization code + PKCE.
* `jamgraf/secretstore.py` — the OS credential store, and the refusal to use
  anything else. `jamgraf/wincreds.py` is its Windows backend.

## What travels

Content, and only content (CLAUDE.md rule 7): pages as canonical Y.Docs, the
hierarchy **by slug**, manual order and starred flags, custom elements,
templates with their metadata schemas, and one copy of each distinct image blob.

Never `tenant_id` or `wiki_id`. Kahala re-attaches those from the caller's own
server-resolved scope, and the format's `ServerFieldLeakError` guard refuses a
payload carrying them — so a local push cannot steer where it lands or escalate
into another tenant's wiki. Embeddings are never shipped either: they are
derived data, regenerated locally on import.

## Push and pull both merge. Neither deletes.

This is the single most important thing to understand about the feature, and the
UI says it next to the buttons rather than only here.

A snapshot round-trip merges by slug on both ends. A push adds and updates pages
on Kahala and removes nothing there; a pull does the same locally. So:

* a page you deleted locally is **still on Kahala** after a push, and
* the next pull **brings it back**.

Calling this "upload" or "sync" would set up exactly the surprise issue #58
warned about, so it isn't called that anywhere in the interface. Deleting a page
on both sides is a deliberate act on both sides.

## Incremental by default

A push or a pull sends **only what changed**. Jamgraf publishes a state vector —
a per-page Yjs summary, plus digests of the elements, templates and images it
holds — and the far end replies with just the missing bytes. Re-syncing a
215-page wiki that differs by a paragraph is a few kilobytes rather than ~57MB.

Definitions travel with the pages (interchange spec v3), so a page never arrives
referring to a custom element the peer has never seen. That was not always true:
before v3 the incremental path carried pages *only*, so a peer's pages stayed
current while its definitions silently went stale — and nothing reported it.

### When a sync goes full

Two situations make Jamgraf transfer the whole wiki instead. Both are automatic,
and both **say so in the result** — a fallback nobody can see is one nobody can
question.

* **The far end predates spec v3.** Detected by the *absence* of the definition
  sections in its envelope, not by a version number: both v3 envelope kinds
  always emit `elements`/`templates`/`images`, empty or not, so a missing key
  identifies an older peer. That distinction is the whole trick — a v3 peer with
  nothing new to send and an old peer that *cannot* send definitions produce
  byte-identical content, and only one of them is safe to sync incrementally.
* **An image did not survive the trip.** A per-page changelog is a Yjs update
  naming the *sender's* image ids. `store.apply_wiki_changelog` remaps them from
  the envelope's `image_ids` once the merge lands — a normal local edit, never a
  rewrite inside the CRDT payload, which would corrupt it. If any page still
  points at an image this wiki does not have, the pull repairs itself with a full
  bundle rather than leaving a page that renders a broken image and says nothing.

A genuine error — 403, 404, an unreachable host — is **reported, not retried**
as a full transfer. Falling back on a real failure just fails again, slower.

*Transfer the whole wiki instead* in the Kahala pane forces the full path by
hand, and the MCP tools take `full=True` for the same reason.

## Signing in

Kahala authenticates against Keycloak natively (good-place decision D5), so
Jamgraf is an OIDC **public client**: `good-place-waikiki`, standard flow with
PKCE S256 and **no client secret**. A desktop binary cannot keep a secret —
anyone holding the `.app` holds it — so PKCE, not a secret, is what binds an
authorization code to the app that asked for it.

The redirect comes back to **`http://127.0.0.1:<port>/kahala/callback`**, a
route on Jamgraf's own server. Two reasons:

* There is no second listener to start, no port race, and no window in which an
  unrelated socket is open.
* RFC 8252 §7.2 warns that any other app on the machine can claim a custom URL
  scheme and intercept the code. Jamgraf's `jamgraf://` scheme is an
  [allow-list of page destinations](deep-links.md) precisely because it is an
  unauthenticated external input; an auth callback has no business going through
  it, and an early draft of the Kahala-side chart registered a
  `goodplace-jamgraf://` scheme that no build of this app has ever claimed.

The redirect URI always uses `127.0.0.1`, never `config.HOST`, which becomes
`0.0.0.0` when LAN sharing is on — not an address a browser can return to.

### Signing in from the packaged app

In the desktop shell the sign-in opens in the **system browser**, not in
Jamgraf's own window. RFC 8252 §8.12 says a native app must not run OAuth in an
embedded user-agent, and WKWebView is one — the app could read what is typed
into it, which is the trust problem the rule exists for. The practical cost has
the same shape as the principle: inside our window there is no Keycloak session
to reuse, no password manager, and no second factor that depends on either.

So `/kahala/signin?shell=desktop` returns the authorize URL as JSON and the pane
hands it to `pywebview.api.open_url`. The code still comes back to
`http://127.0.0.1:<port>/kahala/callback`, which *this* process serves — which
browser made the request does not matter.

Because that flow finishes in another application, two things follow. The
callback renders a short "you can close this tab" page rather than redirecting
into the app UI, which would otherwise leave the person looking at Jamgraf in a
stray browser tab. And the pane **polls** `/kahala/state`, because a window whose
sign-in completed elsewhere gets no event and would otherwise sit showing "not
signed in" until someone thought to reload. A flow whose `state` we did not mint
is treated as external: a callback we never started is not something to steer the
app's own window on.

## Where the refresh token lives, and why not anywhere else

In the operating system's own credential store, under service `Jamgraf`, keyed
by the realm's issuer: the **macOS Keychain**, or the **Windows Credential
Manager** (`jamgraf/wincreds.py`). A platform with a real store gets a backend
rather than an exception to the rule below; a platform without one refuses to
sign in and says so. There is no file backend, not even a hidden one.

The two obvious alternatives are both wrong:

* **The wiki's `settings` table.** The wiki file *is* the export — "Save wiki"
  copies that database and `store.export_wiki_bundle` reads from it — so a token
  there is handed to whoever you share a wiki with.
* **`app_config.json`.** It never leaves on its own, but it is plaintext in the
  data directory and lands in Time Machine and any folder backup.

The link record (address + remote wiki name) is not secret and lives in
`data/wikis.json`, in the registry entry for the wiki. That is still *outside*
the wiki file, so a shared wiki does not carry a pointer at your Kahala.

### What the Keychain actually buys

Stated plainly, because overclaiming here would be its own bug: it protects the
token **at rest and off the machine**. It is not in a file you can accidentally
export, mail, or commit, and a stolen copy of the data directory contains nothing
to sign in with.

It does **not** protect against code already running as you — a same-user
process can shell out to `security`, or call `CredRead`, exactly as we do. That
is the same trust
boundary the rest of the app sits on (`auth.py` grants loopback callers owner
rights), so it is not a new exposure.

The secret never appears in `argv`: `security -i` takes its commands on stdin,
so the token is not visible in `ps` while it is written.

### No silent fallback

If there is no secure store — a future Linux or Windows build, or a Mac without
`security` — signing in is **refused and reported**, and the capabilities pane
says so. There is no file backend to fall back to, not even a hidden one:
writing a credential to a plain file "just for now" is the outcome this design
exists to prevent.

## The slug, not the display name

Kahala's *Wikis* page shows both — `StartupOS /startupos` — and the interchange
wire addresses a wiki by its **slug**. Link to the display name and the push
404s, which reads like a permissions problem and arrives long after the mistake
was made.

Both sides derive slugs with the same `slugify`, so `kahala.link` refuses a
non-slug name *where it is typed* and names the slug it will be. The push 404
carries the same hint, for links made before that check existed. Neither
auto-corrects: if the wiki's slug collided when it was created, Kahala's is
`startupos-2` and guessing would send the push somewhere real and wrong.

## One sign-in per install

The sign-in is app-global and keyed by **issuer** (`kahala:{issuer}` in the
Keychain), while a *link* is per wiki and carries its own base URL. So every
linked wiki uses the same identity, and a Kahala on a different sign-in server
needs `kahala_issuer` changed in `app_config.json` and a fresh sign-in.

That asymmetry is a real gap — if you choose which Kahala to push to, you should
choose which one you are signed in to — and the pane now at least *names* the
server rather than saying "Signed in." with nothing to check it against. The
Keychain already keys by issuer, so several identities can coexist; what is
missing is carrying the issuer on the link and threading it through
`kahalaauth`.

## A push cannot create the wiki on Kahala

The wiki has to exist there first. This is not a gap we chose: creating one is
`POST /wikis/create`, a **session-authenticated browser form**, and Kahala only
accepts a bearer token on `/api/*` (`oidc.py`, the middleware's
`path.startswith("/api/")` gate). The credential Jamgraf holds cannot reach that
route, so there is nothing to automate against.

So the pane says so plainly, and *Open Kahala…* sends you to that server's own
*Wikis* page — in the browser you are already signed in to, via the same
`data-open-url` hook the app uses for PDFs. Create it there, then link to the
name Kahala gives it. Note that Kahala derives the slug from the name you type,
so the two can differ; the link wants the slug.

Automating this properly needs an `/api/*` creation route on Kahala, which is
server-side work tracked separately.

## Refusals

Every one of these is a normal state that reports itself and changes nothing
locally. They are deliberately *different messages*, because they need different
actions from the reader.

| What happened | What Jamgraf says |
|---|---|
| Not linked / not signed in | Which of the two it is, before any request is made |
| **3xx redirect** | Reported, **never followed** — see below |
| 401 | The sign-in was not accepted, and may have expired |
| 403 | Signed in, but not an owner/admin of that wiki on Kahala |
| 404 | No such wiki *that this account can see* — Kahala answers identically for a wiki that doesn't exist and one in another tenant, so the message names both possibilities |
| 409 | Refused as incompatible rather than merged; the two ends are on different interchange versions |
| Unreachable host | Named, with "nothing was changed here" |
| A bundle that fails its gates | "Refused rather than merged" — the dry run reads the whole payload first, so nothing local changed |
| A bundle whose **local writes** fail | "Only partly merged … running the same transfer again finishes it" — see below |

A part-written pull is the one refusal that is not a promise that nothing
happened. The import is deliberately not staged-and-swapped (`docs/data-safety.md`
question 4), so a full disk or a lost lock part-way through leaves pages merged —
never deleted, each one an ordinary versioned page, and re-running the same
transfer completes it. `store.import_wiki_bundle` reports when it begins writing
so the two cases can say different things; calling both of them "refused" would
be the comforting sentence rather than the true one.

### A wiki's name on Kahala is one path segment

The name is typed by the owner and goes straight into the interchange URL, so a
name containing `/`, `\`, `?`, `#` or `%` — or `..` — is **refused when it is
entered**, and escaped again on the way out. Unescaped, `..` doesn't address a
wiki at all: `httpx` resolves it and the request arrives at a different route on
that host, with the bearer token attached. A typo must be reported, not sent.

### Redirects are never followed

`httpx` follows redirects by default, and a followed redirect **re-sends the
`Authorization` header** to whatever host the response names. Since every
request here carries a bearer token, that is a credential handed to a third
party. Every client in `kahala.py` and `kahalaauth.py` sets
`follow_redirects=False` and treats a 3xx as a refusal naming the location.

`tests/test_kahala_sync.py` pins this by asserting the token never reaches the
redirect target — flip that argument to `True` and the test goes red.

## Who may do what

* **The person at the machine** may do everything, from the Kahala pane.
* **A LAN guest may do none of it.** `/kahala` is in `auth._GUEST_DENY_PREFIX`.
  A guest who could reach `/kahala/link` or `/kahala/clone` could aim this
  machine at a Kahala *they* name and push the owner's wiki to it — exfiltration,
  not misconfiguration.
* **An agent (MCP)** gets `kahala_status`, `kahala_push` and `kahala_pull` (both
  taking `full`), and
  deliberately *not* linking, cloning or signing in. Those three establish a
  destination or a credential, which is the owner's call — and signing in needs
  their browser regardless. An agent may move content along a route the owner
  set up; it may not create one.

* **A web page you happen to be visiting** may not either. Jamgraf has no CSRF
  tokens — loopback callers are owner, so any site can POST to `127.0.0.1` and be
  obeyed. Across the rest of the app the worst that does is local damage, which
  is a known accepted risk. Here it is not the same risk: a forged
  `/kahala/link` followed by a forged `/kahala/push` would send the whole wiki to
  a server the attacker names. So every mutating `/kahala` POST is checked for
  `Sec-Fetch-Site` (falling back to `Origin` vs `Host`) and refused when it did
  not come from Jamgraf's own pages. The check lives in the ASGI middleware, not
  in each route, so a route added later cannot forget it — and
  `tests/test_kahala_sync.py` enumerates the routes off the app rather than by
  hand for the same reason.

  The general gap is worth closing on its own terms; this PR guards the surface
  that turns it from local damage into exfiltration, and does not silently
  change how every other route behaves.

Push and pull go through the same `kahala` module for both surfaces, per rule 5,
so a human and an agent get the same transfer, the same gates and the same words.

## Configuration

Both default to the good-place realm and are overridable in `app_config.json`
for another deployment:

| Key | Default |
|---|---|
| `kahala_issuer` | `https://app.kwirker.com/kc/realms/good-place` |
| `kahala_client_id` | `good-place-waikiki` |

The Kahala address and remote wiki name are per-wiki and set in the UI.

## The far end

Kahala's routes are in `services/kahala/kahala/routes/interchange.py`:

* `GET  /api/interchange/wikis/{wiki}/snapshot` — clone/pull (streamed)
* `POST /api/interchange/wikis/{wiki}/snapshot` — push (multipart, OWNER/ADMIN)

It enforces scope translation, the admin gate on bulk push, and the version gate
server-side, because a peer cannot enforce them. The desktop client it admits —
the public `good-place-waikiki` Keycloak client, whose audience mapper stamps
`good-place-kahala` so Kahala's existing bearer acceptor takes it — is
good-place/platform issue #63.
