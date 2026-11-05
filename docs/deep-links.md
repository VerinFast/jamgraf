# Deep links (`jamgraf://`)

A stable link that opens the desktop app at a specific place. Implementation:
[`jamgraf/deeplink.py`](../jamgraf/deeplink.py) (parsing + allow-list) and
`_install_deeplink_handler` in [`jamgraf_app.py`](../jamgraf_app.py) (the macOS
event handler).

## Forms

The wiki sits in the **authority** position, mirroring how a wiki is the
outermost unit of isolation everywhere else in the app:

| Link | Opens |
|---|---|
| `jamgraf://<wiki>/<page>` | that page |
| `jamgraf://<wiki>/<page>#<section>` | that page, scrolled to a heading |
| `jamgraf://<wiki>` | the wiki's front page |
| `jamgraf://<wiki>?q=<terms>` | search, inside that wiki |
| `jamgraf://` | the front page |

### There are no verbs, on purpose

With the wiki in the authority position, a reserved word like `open`, `search`, or
`home` would **shadow any wiki actually named that** — `jamgraf://search` has to
mean "the wiki called search", not "do a search". So search is a query on a wiki,
and the bare `jamgraf://` is the front page (an empty authority is safe: no slug
can be empty).

Search being wiki-scoped is also the only correct form. Wikis are fully isolated
and a search never spans them, so there is no unscoped search to express. The
scope always comes from the validated authority, never from a `wiki=` in the
query — a caller-supplied one is ignored.

The authority form is required: a bare `jamgraf:` with no `//` is malformed
rather than shorthand.

`<section>` is a heading's anchor slug — the same value `render.py` puts on
headings, and what MCP `get_page` returns in `outline`.

## Why not just an http:// URL

`jamgraf_app._pick_port` scans for a free port at startup, so an install serving
`127.0.0.1:8787` today can be on `8788` tomorrow. An http link embeds that port
and goes stale; `jamgraf://` doesn't.

This is the reason the feature exists: an agent holding a page wants to hand a
human something openable. MCP `get_page` therefore returns a `link` field with
the scheme URL, and **Page options → Copy link** copies the same thing.

## Security — read before widening this

A registered URL scheme is an **unauthenticated external input**. Any web page,
mail message, or other app can fire `jamgraf://…` and macOS will deliver it to
us. There is no origin, no user gesture we can verify, and no authentication.

That matters more here than in most apps, because the desktop window loads over
loopback and [`auth.py`](../jamgraf/auth.py) grants loopback callers **owner**
rights. A design that forwarded the incoming URL's path to `load_url` would let
any website drive owner-level routes inside someone's wiki.

So `deeplink.resolve()`:

- accepts only the forms in the table above and refuses everything else
- takes the wiki from the **authority only**, and validates it before use
- **constructs** the resulting path itself; it never echoes a caller-supplied path
- validates every slug against `^[a-z0-9][a-z0-9-]{0,127}$` — no dots, slashes, or
  percent escapes, so nothing can traverse a path or smuggle a second component
- validates section anchors the same way, and refuses the whole link on a bad one
- returns an **app-relative** path only. The loopback base is prepended by the
  caller, never taken from input, so a link can't redirect the window off-host
- caps query length and refuses an empty search

Widening any of this widens what the entire internet can ask this app to do. The
refusal cases in [`tests/test_deeplink.py`](../tests/test_deeplink.py) are the
load-bearing half of that file.

Note that `jamgraf://<wiki>/settings` is *allowed* — it resolves to
`/wiki/settings`, a wiki page that happens to be named "settings". That is not
the app's owner-only `/settings` route, and the tests pin the distinction.

## How the URL reaches the app

macOS delivers a scheme open as the `GURL` Apple Event, so:

1. `jamgraf.spec` registers `CFBundleURLSchemes: ["jamgraf"]` in the bundle's
   `Info.plist`. **Without this macOS never routes the URL to us** — and it only
   takes effect in a packaged build, not a source run.
2. `_install_deeplink_handler` registers a handler with `NSAppleEventManager`
   for that event, keeping a module-level reference (the manager holds only a
   weak one to the target).
3. The handler resolves the URL through `deeplink.resolve` and, only on success,
   calls `load_url(base + path)`. A refused link is logged to stderr.

Every failure in that path is swallowed: deep links are a convenience, and a
missing pyobjc symbol must never stop the app from launching.

## Limits

- **Packaged app only.** A `python jamgraf_app.py` source run has no `Info.plist`,
  so macOS has no scheme registration to route. The parsing layer is fully
  testable without a build; the delivery layer isn't.
- **Cold launch via a link is not guaranteed.** The Apple Event handler is
  registered once the GUI is up, so a link that arrives *before* that is
  LaunchServices' to redeliver. Nothing is queued on our side; if it isn't
  redelivered the app just opens on the front page rather than the target. Links
  fired at an already-running app are the reliable path.
- **One registration wins.** If several Jamgraf builds exist on the machine,
  LaunchServices picks one — usually the most recently registered. Testing a dev
  build while a released app is installed can open the wrong one.
- **No cross-machine meaning.** A link names a wiki and a page, not a host. Sent
  to someone else it opens *their* wiki of that name, or fails. These are local
  links, not shareable URLs — that's a job for the LAN/tunnel sharing in
  Settings.
- **macOS only, and reported as such.** The scheme needs an owner the OS can
  route to: an `Info.plist` in a packaged `.app`. On Windows that would be a key
  under `HKCU\Software\Classes` naming an executable, and a `pip install`'s
  launcher sits inside whichever virtualenv it landed in — so the path we would
  register goes stale the moment that moves, and claiming a URL scheme in
  someone's registry is more than a `pip install` was asked to do.
  `jamgraf/desktop/windows.py` therefore installs nothing and says so, rather
  than registering a handler that would one day open the wrong thing or nothing
  at all. See `docs/windows.md`.
