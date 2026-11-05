# Jamgraf — Claude notes

*Formerly **Waikiki** (renamed in v1.1.0). Strings saying `waikiki` that remain in
this repo are load-bearing, not leftovers — see rule 13.*

Small local wiki for Human ↔ LLM collaboration: FastAPI + one SQLite file per
wiki, hybrid RAG (FTS5 BM25 + sqlite-vec, fused with RRF), a `pycrdt` room per
`(wiki, page)` for live co-editing, and an MCP server so Claude edits the same
document you do. This file is the agent-facing companion to `README.md`; keep the
two in parity (same substance, different voice) whenever you change either.

## Layout

- `jamgraf/api.py` — FastAPI app: HTML views, JSON REST, image serving, AI SSE,
  the `/collab` CRDT websocket. **Routes only.**
- `jamgraf/store.py` — the **repository chokepoint**: page/image/version/comment/
  suggestion/tag/activity CRUD **and** the route-facing settings accessors.
- `jamgraf/elements.py`, `wikis.py`, `rag.py` — sibling repositories (custom
  elements, the wiki registry, hybrid retrieval).
- `jamgraf/db.py` — infrastructure chokepoint: SQLite connection cache, schema,
  FTS5, sqlite-vec load, apsw/stdlib backend shim, low-level settings SQL, the
  **transaction seam** (`transaction()` / `after_commit()`, rule 6), and
  the **damaged-file judgement** (`unreadable_reason` / `WikiUnreadable`) that
  keeps one bad wiki from taking the app down (rule 4).
- `jamgraf/mcp_server.py` — FastMCP tool surface; shares `store`/`rag` with the API.
- `jamgraf/collab.py` — CRDT rooms + the debounced snapshot flusher.
- `jamgraf/metaschema.py` — the metadata declaration a template may carry
  (`name[*]: type`) compiled to `pydantic` checks. Pure, sits below `store`; it
  **reports**, it never blocks a write or rewrites a value (issue #28).
- `jamgraf/ydoc.py` — **canonical Y.Doc persistence** + the wiki-interchange
  produce/consume round-trip. Sits below `store`, like `rag`.
- `jamgraf/vendor/wiki_interchange/` — vendored, version-pinned interchange
  format (see `docs/vendoring.md`).
- `jamgraf/doorman.py` — **optional** integration with the sibling Doorman app:
  detection, nicer speech, and the **routing decision** for generation, chat and
  images (`/api/ask`, `/api/image`). Must never be required, started or
  installed. Capability-probe, never version-sniff: an older Doorman 404s those
  routes and a 404 means exactly what the documented quiet `{available: false}`
  200 means. The one place "optional" doesn't apply is Jamgraf displayed *inside*
  Doorman's window (`embedded()`), where the setting is locked on with a reason
  rather than hidden — see `docs/doorman.md`.
- `jamgraf/capabilities.py` — what this install can actually do, and the
  **remedy descriptors** that fix what it can't. Probes every optional tool,
  reports a state + a plain-language reason, and resolves each remedy to the
  step that is *actionable now* (see `docs/capabilities.md`). Sits below the
  routes with `doorman`/`updater`, whose `status()` shape it follows.
- `jamgraf/remedies.py` — the **only** place a remedy's argv is written: three
  registries (package installs, vendor scripts, "open this pane") plus the
  execution that re-checks the prerequisite, reports failure honestly, and
  treats exit 0 as a claim rather than proof. Sits below `capabilities`.
- `jamgraf/backups.py` — scheduled local snapshots of **every** wiki, on by
  default, taken with SQLite's online backup API (never a file copy of a live WAL
  database) and written whole-or-not-at-all. A wiki whose file cannot be read is
  skipped and reported (`skipped`), never fatal to the run. Same disk as the
  wikis, so it is insurance against corruption and mistakes, not against losing
  the machine — see `docs/data-safety.md`.
- `jamgraf/calendarfeed.py` — subscribed **iCalendar feeds**: fetch, RRULE/EXDATE/
  RECURRENCE-ID expansion, and the **host allow-list** that is the sole guard on
  the one route that fetches a caller-chosen URL. Sits below the routes and holds
  no SQL — see `docs/calendar-feeds.md` and rule 10.
- `jamgraf/bugreports.py` — bug reports an agent filed through MCP, **queued for
  a person**, never sent. Holds no network code at all: it builds a link that
  pre-fills GitHub's own new-issue form so the human submits it themselves. Two
  reasons, and the second is the load-bearing one — filing directly would need a
  GitHub token in the app, and an agent reporting a failure routinely quotes the
  wiki content it was working on, which on a public tracker is a privacy leak
  rather than noise. `/reports` is owner-only for the same reason.
- `jamgraf/kahala.py` — linking a local wiki to a **Kahala** one and moving whole
  wikis over its `/api/interchange/*` wire (clone, push, pull). Holds the link
  record accessors and the wording of every refusal; no SQL, no format code —
  the bundle is the vendored interchange library's. Sits below the routes.
- `jamgraf/kahalaauth.py` — the **OIDC public client** (`good-place-waikiki`,
  auth code + PKCE S256, no secret) that signs in to Kahala. The callback lands
  on our own loopback port, never a custom scheme.
- `jamgraf/secretstore.py` — the OS credential store, and the **refusal to fall
  back** to a file when there isn't one. The only place a credential is written;
  picks a backend by platform and keeps everything that must be true of a stored
  credential above both.
- `jamgraf/wincreds.py` — the Windows half of that: the Credential Manager
  through `advapi32`. The ctypes marshalling is behind one function (`_load`)
  because it cannot run anywhere but Windows; every *decision* — target name,
  encoding, length, whether it roams — sits above that seam where a test can see
  it. See `docs/windows.md`.
- `jamgraf/desktop/` — the desktop shell: **one launcher, one native module per
  platform**. The shared half (`__init__`) picks the port, runs uvicorn in a
  thread, builds the menu bar and opens the window; `macos.py` holds what
  WKWebView withholds from the page (Apple Event deep links, Dictation,
  NSPasteboard) and `windows.py` is nearly empty *because* WebView2 is Chromium
  and withholds none of it. A platform with no native route leaves the method
  **off** rather than stubbing it — `base.html` chooses the native route by
  asking whether the method exists, so absence is the signal. `jamgraf_app.py`
  (PyInstaller) and the `jamgraf-app` script both land on `desktop.main`.
- `jamgraf/deeplink.py` — `jamgraf://` deep links: the allow-list that turns an
  external URL into an in-app destination (see `docs/deep-links.md`).
- `jamgraf/updater.py` — self-update for the packaged `.app`: signature-verified
  download, staging, and the detached bundle swap (see `docs/updates.md`).
- `docs/rfc/0001-multi-tenant-waikiki.md` — the port-to-multi-tenant RFC.
- `docs/repository-layer.md` — the layering spec (RFC 0001, Phase 0).
- `docs/vendoring.md` — vendored packages, pins, and re-sync steps.
- `docs/updates.md` — the auto-update trust model and release procedure.
- `docs/windows.md` — the Windows install: what `pip install jamgraf[desktop]`
  gives, why the Windows native module is nearly empty, the WebView2 refusal and
  the browser fallback, the Credential Manager, what Windows does not get — and
  which claims are measured, which are CI-only, and which are not verified yet.
- `docs/deep-links.md` — the `jamgraf://` scheme and why it is an allow-list.
- `docs/calendar-feeds.md` — subscribed calendars: why the fetch is
  server-side, why the host allow-list is the only thing keeping that route from
  being an open proxy, and what recurrence expansion has to get right.
- `docs/kahala-sync.md` — the Kahala round-trip: why push and pull both merge,
  where the refresh token lives and what the Keychain does and does not buy,
  and why a redirect is never followed.
- `docs/doorman.md` — the optional Doorman integration and why it stays optional.
- `docs/capabilities.md` — the capabilities view, remedy descriptors, and the
  one install that pipes a script into a shell.
- `docs/data-safety.md` — the 1.0 durability audit (issue #68): what survives a
  crash, a corrupt wiki file, a failed import; how restore actually works; and
  the risks we accept, named. Every answer there was established by making it
  happen, and `tests/test_data_safety.py` pins the properties.
- `docs/rename-to-jamgraf.md` — rule 13: every old name still out in the world,
  what reads it, what deliberately kept its old value, how the first launch moves
  an existing install, and the order the rename has to ship in.
- `docs/export-import-parity.md` — rule 12 as a standing decision: why every
  export must have a reachable import, the release where the bundle importer
  existed and was unreachable anyway, why PDF is exempt, and what a folder of
  markdown means on the way in (the gap that used to be recorded there, now
  closed).

## Architectural rules (load-bearing)

1. **Routes own routing + pydantic validation only.** A handler parses/validates
   the request, calls a repository function, and shapes the response. **No raw
   SQL and no cursor use (`get_conn()`/`.execute()`/`.fetch*()`) in `api.py`** —
   `tests/test_repository_chokepoint.py` fails the build if either reappears.
2. **All SQL lives behind the repository.** `store.py` (and `elements`/`wikis`/
   `rag`) own the domain SQL; `db.py` is the only module that knows SQLite. Route
   settings access goes through `store.get_setting/set_setting/all_settings`, not
   `db.*`. Infrastructure-internal callers (embeddings, imagegen, ai, mcp_server)
   may call `db` directly — they already sit below the repository.
3. **The repository seam is where per-wiki / tenant scoping attaches later.**
   Keep every read/write behind it so a future `WikiScope` + Postgres RLS lands
   without touching a route. Isolation is the reason, not tidiness.
4. **Wikis are fully isolated.** Pages, search, and `[[links]]` never cross
   wikis. The active wiki is a per-request contextvar (`db.current_wiki`); the
   MCP server's active wiki is independent of the browser's **and of every other
   agent's** — it is keyed per MCP session (`mcp_server._ACTIVE_BY_SESSION`) and
   deliberately **not persisted**. Never store it in a module global or on disk:
   a shared pointer meant a respawned agent inherited another agent's wiki and
   wrote pages into it (issue #11). A fresh session has no active wiki and must
   call `switch_wiki`; refusing is correct, silently inheriting is not.
   `tests/test_cross_wiki_isolation.py` guards this end to end.
   **Isolation covers failure, not just content (issue #71).** One unreadable
   `.db` must never stop the app or its neighbours — a corrupt *default* wiki
   used to raise inside `db.init_db()` in the lifespan, so the app did not start
   and a healthy 215-page wiki became unreachable. Four things are load-bearing:
   the judgement is **narrow** (`db.unreadable_reason` — apsw's per-code classes
   and a bare `sqlite3.DatabaseError` count; `apsw.SQLError: no such table` and a
   `KeyError` of ours do not, and must stay loud); `db.get_conn` raises
   `db.WikiUnreadable` naming the wiki while `init_db` reports and continues;
   the state is **surfaced, never guessed** (`wikis.health`/`wikis.stats` report
   instead of raising, so `/wikis` — the recovery page — renders, and everything
   else gets a 503 page naming the wiki, the file and the restore route, because
   a Finder-launched app has no console); and the file is **never touched** — no
   delete, truncate, rename or in-place repair, because it is the user's data and
   may be recoverable. `backups.run_backup` skips it and snapshots the rest.
   `tests/test_data_safety.py` guards all of it, on both backends.
   **Schema setup belongs to the connection, not to the process.** Handles are
   cached per (thread, wiki) but setup used to be memoised process-wide, so any
   connection after the first skipped it. That is invisible while a file stays
   put and fatal when it does not: a wiki whose database was replaced or removed
   underneath a running app came back as an empty file nothing ever created
   tables in, and every request that reached a thread without a cached handle
   died on `no such table: pages` for the life of the process — while threads
   still holding the old handle served its pages as if nothing had happened,
   which is why it presented as "viewing works, editing 500s". The DDL is
   idempotent, so paying it per connection is cheap; `_schema_ready` survives
   only to tell a first open apart from a disappearance, which is **reported**
   rather than passed off as an empty wiki.
   **A slug is an address other things hold, which is why renaming it is its own
   operation.** `wikis.rename` changes the display name and touches nothing
   else; `wikis.change_slug` moves the database file, and four things point at
   the old name when it does. The file is **copied with the backup API, not
   renamed** — a plain rename strands the `-wal`, and deleting a hot WAL out
   from under another thread's handle earns "the disk reported an I/O error" on
   the next open — and the original is removed only once the registry write has
   succeeded, so a failure costs a copy rather than a wiki. Cached handles are
   retired through `db.invalidate_connections` (an epoch, because no thread can
   reach into another's cache) or they keep writing through an fd that followed
   the file. **CRDT rooms are released first** (`collab.release_wiki`): a room
   key names its wiki and both save paths write under it, so one left pointing
   at a slug the registry no longer knows resolves through `active_wiki()`'s
   fallback and lands that page in the **default** wiki — flushed first so
   nothing typed is lost, and `collab._orphaned` is the net under every other
   caller. And an agent still pointed at the old slug is **refused**, not
   redirected, for the reason above: the fallback would have written its pages
   into whatever wiki happens to be default. Every refusal is collected
   **before** the rooms come down (`plan_slug_change`): releasing them closes
   whatever people have open, so running the checks afterwards throws everyone
   out of their editors for a rename that was never going to happen. A room
   whose flush **fails** aborts the rename rather than being forgotten with
   unsaved text in it. The **MCP surface carries the name only** — those rooms
   live in the web app's process, not the MCP server's, so an address changed
   from there would strand whatever somebody is typing with neither process in a
   position to notice. `help` cannot be re-addressed at all, since
   `ensure_help_wiki` would bring it back as a second copy. The files are
   **renamed, never copied-then-deleted**: a handle another thread cached before
   the move is still pointing at the same inode, so its next write lands in this
   wiki under its new address instead of in a file that is about to be deleted —
   which is what closes that race without locking anything, and why the `-wal`
   and `-shm` move with the database (deleting a hot WAL is what "the disk
   reported an I/O error" looks like). `tests/test_wiki_rename.py` guards each
   of those.
5. **One code path for Human and LLM.** REST, HTML views, and MCP tools all go
   through `store`/`rag` so both callers get identical render + version + index.
   The same applies to *which model answers*: the Doorman-or-local decision lives
   in `doorman.py` (below `ai`/`chat`/`imagegen`, which are below the routes), so
   the editor, the REST API and the MCP tools can't disagree about it. Whatever
   answers is named back to the caller (`backend`/`label`) — routing to a
   different model without saying so is its own bug.
6. **The Y.Doc is the canonical persisted state; markdown is a projection.** Each
   page's full encoded `pycrdt` Y.Doc lives in `page_ydoc` and is the source of
   truth; `pages.markdown`/`html` are maintained *alongside* it for FTS, render
   and RAG. Every content write goes through `store` and advances the canonical
   Y.Doc (`store._sync_ydoc` → `ydoc.sync`) — never write markdown as truth and
   discard the doc. The live `collab.py` room is still just an editing buffer; its
   flush lands through `store` like any other write, so the canonical doc stays
   authoritative. See `jamgraf/ydoc.py`.
   **Order matters within a write: canonical state before derived state.**
   `_sync_ydoc` runs *before* `rag.reindex_page`, because the Y.Doc is truth and
   the RAG index is a cache rebuildable from the markdown. With the old order,
   anything raising in reindex committed the projection and skipped the canonical
   write, silently leaving the doc a revision behind — which is how the Help
   wiki's About page ended up carrying two different versions at once.
   **One page save is also one transaction (issue #72).** The projection, the
   version snapshot, the tag index and the canonical Y.Doc go through
   `db.transaction()` — an explicit `BEGIN IMMEDIATE`, re-entrant via savepoints,
   working the same on apsw and the stdlib (which is opened `isolation_level=None`
   so transaction control lives in one place). What must stay *outside* it is
   `rag.reindex_page`: it embeds, and holding the write lock across that would
   queue every other writer, so it is deferred to `db.after_commit` — still
   outside even when a caller has nested the save in a bigger transaction. The
   index is a cache; truth is not. `docs/data-safety.md` question 1 has the
   measurements, and `tests/test_data_safety.py` breaks each seam in turn.
7. **The Kahala ⟷ Jamgraf round-trip is content-only and version-gated.** Export
   (`store.export_snapshot` / `export_changelog` / `export_wiki_bundle`) and
   import (`store.import_snapshot` / `import_changelog` / `import_wiki_bundle`)
   go through the repository, using the **vendored** `wiki_interchange` encoders.
   Payloads carry content only — never invent or require
   `tenant_id`/`wiki_id`/permissions (Kahala re-attaches those server-side); local
   embeddings are regenerated on import, never shipped. An incompatible spec/Yjs
   version is **rejected**, never merged. Re-sync the vendored lib per
   `docs/vendoring.md` and keep its pin in lockstep.
   **"Embeddings are regenerated, never shipped" binds the file-import path too**
   — the one path that *does* carry an index, because it carries the whole SQLite
   file. Every search in the app runs over `chunks`/`chunks_fts`/`vec_chunks` and
   never over `pages_fts`, so a `.wiki` whose chunk table is empty — or whose
   vectors are another machine's width — opens as a wiki whose pages render and
   link perfectly and answer *nothing*, with no error anywhere. So
   `wikis.import_from` ends in `rag.reindex_if_stale`: it names its reason on
   stderr, rebuilds, and is a **no-op on a wiki that arrived indexed** (never
   re-embed 215 pages for nothing). Three smaller judgements there — a vec
   dimension mismatch counts, because `db.ensure_vec_table` drops the mismatched
   table from the *search* path and would otherwise discard the imported vectors
   on the first query; an embedder this machine cannot load does **not** count,
   since re-chunking would trade vectors for nothing and BM25 is the whole index
   there anyway; and a failed rebuild is reported but never fails the import,
   because the index is a cache and the wiki is readable without it.
   The **whole-wiki bundle** (issue #57) adds three rules of its own:
   *hierarchy travels by slug, never by integer id* — ids are local, so an id
   that crossed would point at whatever page happened to hold that number in the
   other wiki, which is why `import_wiki_bundle` creates pages under the slug the
   bundle names (`create_page(..., slug=...)`) rather than deriving one from the
   title; *the payload is decoded in full before the first write*
   (`store._read_bundle`), so a bad bundle leaves the wiki untouched instead of
   half-imported; and *export/import stream*, one page at a time, because a real
   wiki is 215 pages / ~57MB. `tests/test_wiki_bundle.py` is the round-trip
   proof and guards all three.
8. **The updater fails closed, and its trust root is pinned at build time.**
   `updater.py` downloads a bundle and then *executes* it, so it is the highest-
   privilege path in the app. Every release zip must Ed25519-verify against
   one of the keys in `PUBLIC_KEYS_HEX` **before** it is expanded; no pinned key
   means updating is
   disabled, never "trust the download". Never source that key from the network,
   `app_config.json`, or any other writable place — a replaceable pinned key is
   not a trust root. The bundle's ad-hoc `codesign --sign -` proves nothing about
   origin; it is not a substitute. See `docs/updates.md`.
   The refusal is unchanged off a bundle — no verified `.app`, no update — but
   the **next step** is not: `updater.install_kind()` names the install shape so
   a pip install is pointed at its package manager and a checkout at git, and a
   shape we don't recognise stays *unknown* rather than being told to run `pip`.
   Wrong advice is worse than none, which is rule 9's point applied here.
   **The private half is the opposite case, and the two must not be confused.**
   Fetching the *signing* key from the network is exactly what `release.sh` does
   — from Google Secret Manager (`waikikey` in `startupos-328814`, via
   `gcloud`) — because the only
   machine that ever holds it is the one cutting a release, and keeping it off
   disk is the point. Three things are load-bearing there: it is **held in a
   variable and handed to the one signing process**, never written to a file
   (the only temp file is gcloud's stderr, and it is removed); a local key file
   is used **only when named** (`JAMGRAF_UPDATE_KEY`), with no silent fallback
   between the two sources; and it is fetched **and checked** before the build
   (parsed, Ed25519, matched against the compiled keys), so a missing, malformed
   or untrusted key costs seconds rather than a wiped `dist/`. Two leaks are
   closed by construction: **xtrace is off** around every step that touches the
   key (`bash -x` prints assignments with values), and **inherited variables that
   could carry it are unset** first (bash keeps the export flag on reassignment,
   so the key would ride into the build). And the script **refuses
   `JAMGRAF_UPDATE_PUBKEY`** — that override is for tests and private builds of
   the app; here it would make both verification steps measure against the
   environment and approve a release no installed copy accepts.
   `tests/test_release_script.py` runs the real script in a sandboxed checkout
   with the test key compiled in and a stub build, so the release path is
   exercised up to the build without one; each of those guards fails when its
   fix is removed.

9. **A missing tool is reported, never discovered on click — and never as a
   command to paste.** `capabilities.py` probes each optional tool and hands the
   routes a **remedy descriptor** (a registry id + a label), so Settings shows a
   button and the feature's own affordance renders disabled and points at it.
   Four things are load-bearing: a remedy resolves to the **prerequisite that is
   actionable now** (no `npm` means the button is Node.js, not the CLI); an
   install is **confirmed server-side** before it changes the machine, and its
   failure is *reported*, exit code and all; where no vendor publishes an
   install, we say so and point at the setting rather than **inventing a
   command**; and **Doorman is never a remedy** — informational only, per rule
   above and `docs/doorman.md`. The one `curl | bash` path (`agy`) keeps its URL
   as a constant in `remedies.py`, names the host before it runs, and only ever
   runs from a click plus a confirmation — never from a probe. **The package
   manager is per platform**, because "actionable now" includes what this machine
   installs things with: Homebrew on macOS, winget on Windows, using winget's own
   published identifiers (`OpenJS.NodeJS`, `Cloudflare.cloudflared`) — a guess at
   what a package *might* be called is exactly the invention this rule forbids,
   so `-e` makes the id exact and the `--accept` flags keep it from hanging on a
   prompt nobody can see. Jamgraf never installs the package manager itself, and
   the `curl | bash` path does not exist on Windows at all: winget installs Node
   outright, so there is nothing for a shell pipeline to rescue. See
   `docs/capabilities.md` and `docs/windows.md`;
   `tests/test_capabilities.py` guards all of it.

10. **The calendar element carries its own ICS address; the allow-list is what
    makes that safe.** `calendarfeed.py` fetches an external calendar on the
    browser's behalf (no provider sends CORS headers, so the element cannot),
    which makes `GET /api/calendar-feed?url=` the one route that performs an
    outbound request to a **caller-chosen** address. The URL lives in the page's
    `calendar` fence deliberately — a page can subscribe to anything without
    touching settings or code — so `calendarfeed.validate_url` is the only thing
    standing between this and an open proxy: loopback callers are **owner**
    (`auth.py`) and any web page can hit our loopback port, so without it a site
    could aim us at `192.168.1.1` or a metadata endpoint (CORS blocks the reply;
    the request is the damage). It runs before **every** fetch, accepts only
    https at a host in `ALLOWED_HOSTS`, and matches exact-or-subdomain so
    `calendar.google.com.evil.com` is refused. Widening that tuple is a security
    decision. Three more things are load-bearing: the address is **visible in
    page content** (history, exports, backups) and a Google secret address reads
    the whole calendar — say so rather than implying it is private; recurrence is
    **expanded, not approximated** (RRULE + EXDATE + RECURRENCE-ID, bounded by a
    window, placed in the *local* day so an 8pm event doesn't slide to tomorrow);
    and an unreachable host is a **502 that says so**, because an empty month
    grid claims "nothing is scheduled". `tests/test_calendar_feed.py` guards it,
    and its refusal cases must never be relaxed to make something pass.

11. **The Kahala round-trip merges, holds no credential in a wiki, and never
    follows a redirect.** `kahala.py` moves a whole-wiki bundle over Kahala's
    `/api/interchange/*` routes; the format and its version gate are the
    vendored library's (rule 7), so what this adds is a wire and a credential —
    and each has one way it goes wrong. Four things are load-bearing. **Push and
    pull both merge and neither deletes**: a snapshot round-trip merges by slug
    on both ends, so a page deleted locally survives a push and returns on the
    next pull — the UI says that beside the buttons, because calling it "upload"
    or "sync" builds in exactly the surprise issue #58 warned about. **The
    refresh token lives in the OS credential store and nowhere else**
    (`secretstore`): the Keychain on macOS, the Credential Manager on Windows
    (`wincreds`) — not in the wiki's `settings` table, because the wiki file *is*
    what "Save wiki" exports, and not in `app_config.json`, because that is
    plaintext in the data dir; with no secure store, signing in is refused and
    reported rather than quietly downgraded to a file. A platform with a real
    store gets a real backend rather than an exception to this rule, and what
    may be stored does **not** vary by platform: a value refused on one machine
    and accepted on another is a sign-in that fails for a reason nobody can see.
    Persistence is local-machine, never roaming — a credential that follows the
    user's profile to their other machines is this rule failing with a Win32
    constant on it. The link record (address + remote slug) is not
    secret but still lives in `wikis.json`, outside the wiki, so it cannot travel
    to whoever you share one with. **Redirects are never followed** — `httpx`
    re-sends the `Authorization` header across a 3xx, which hands a bearer token
    to whatever host the response names, so every client sets
    `follow_redirects=False` and reports the location instead. And **`/kahala` is
    owner-only** (`auth._GUEST_DENY_PREFIX`): a LAN guest who could link or clone
    could aim this machine at a Kahala they name and push the owner's wiki to it,
    which is exfiltration, not misconfiguration — the MCP surface draws the same
    line, exposing push/pull but never link, clone or sign-in. That same
    escalation is why every mutating `/kahala` POST is checked for same-origin
    (`auth.same_origin`, in the middleware so a later route can't forget): the
    app carries no CSRF tokens anywhere, which is an accepted local-damage risk
    elsewhere and an exfiltration risk *here*.
    A transfer is **incremental by default** and falls back to the whole bundle
    in exactly two cases, each of which names itself in the result: a peer that
    predates interchange spec v3 (detected by the *absence* of the definition
    sections, never a version number — a v3 peer with nothing to send and an old
    peer that cannot send produce identical bytes), and a page left pointing at
    an image that did not survive the trip. A genuine error is reported, never
    retried the expensive way. `store.apply_wiki_changelog` lands definitions and
    blobs *before* pages, verifies every blob against the hash it claims, and
    remaps the sender's image ids **after** the merge — rewriting inside an
    incoming Yjs update would corrupt it.
    Three smaller rules have the same shape — a value crossing into an
    interpreter, where the honest answer is to refuse rather than escape
    creatively. **A wiki's name on Kahala is one path segment**: it is typed by
    the owner and goes into the URL a bearer token is sent to, so `/ \ ? # %`
    and `..` are refused where they are entered (`kahala._bad_remote`) and
    escaped again on the way out (`kahala._wiki_url`) — unescaped, `httpx`
    resolves the `..` and the request lands on another route of that host.
    **Nothing with a control character goes to `security`** (`secretstore`):
    `security -i` reads one command per line, and the refresh token is minted
    off-machine, so a newline would end the write command and have its remainder
    read as the next one — there is no quoting that fixes that, hence
    `_sendable`. **A rotated refresh token must land or the sign-in is over**:
    Keycloak invalidates the one just spent, so a failed Keychain write signs
    out rather than keeping a dead token behind a UI that reads as signed in.
    And a transfer that fails *after* its first local write says "partly
    merged", not "refused" — `store.import_wiki_bundle` reports when writing
    begins, because "refused" is a promise that nothing changed (the partial
    state itself is the accepted limit: `docs/data-safety.md` question 4).
    `tests/test_kahala_sync.py` and `tests/test_secretstore.py` guard all of it;
    the redirect, credential-location, CSRF, older-peer, path-segment,
    control-character, rotation and partly-merged cases are each written to fail
    if the guard is removed, and must not be relaxed to make something pass.

12. **Every export has an import, and no exception to that is allowed to be
    silent.** If this app can write wiki state to a file, it must be able to read
    that file back. An export you cannot restore is not a backup, it is a file
    that looks like one — and the person only finds out on the day they need it.
    This is a promise about the *pair*: shipping an export surface without its
    reader is incomplete work, not a follow-up ticket, and
    `tests/test_export_import_parity.py` enumerates the export functions and
    fails the build on one that has no importer. The pairs today are
    `store.export_snapshot`/`import_snapshot`,
    `store.export_changelog`/`import_changelog`,
    `store.export_wiki_bundle`/`import_wiki_bundle`,
    `wikis.export_to`/`wikis.import_from`, and
    `wikis.export_markdown`/`wikis.import_markdown` (with the MCP pair above
    it). **The known-gap list is empty, and may shrink but never grow.**
    **One door, three archive shapes — sniffed, never assumed.** `wikis.import_from`
    is the single entry point for a file a person picked, and it reads the format
    tag rather than trusting the name: a *wiki file* wraps a whole SQLite
    database (`wiki.db`), an *interchange bundle* carries `manifest.json` plus
    one `pages/<slug>.snapshot` per page and no database at all, and *markdown*
    is `<slug>.md` per page with neither. All are zips (markdown also arrives as
    a plain directory), so a file-picker filter is not a format check and an
    extension is not evidence, and the order matters — database and manifest are
    ruled out before the markdown sniff, so a `.wiki` carrying a stray `.md` is
    still a wiki file. Sniffing is also what keeps the refusal honest: a bundle
    the version gate rejects must say *that*, because the alternative — the
    wiki-file branch reporting "not a Jamgraf wiki file" about a perfectly valid
    bundle — is how this was broken for a whole release. A bundle lands through
    `store.import_wiki_bundle` into a newly registered wiki, the same repository
    path Kahala's pull uses (rule 7), so its pages are versioned and re-embedded
    locally as they arrive; a *refused* bundle removes that empty wiki again
    rather than strand a shell in the registry, while one that failed *after* the
    first write keeps it and says "partly imported", per rule 11's distinction.
    **A render target is not an export of state.** The PDF path (`pdfgen`,
    `mcp_server.export_pdf`) is exempt and always will be — it is a lossy
    presentation of a page for a person to read, not a serialization anything
    could restore from. Markdown is **not** exempt, and is no longer a gap:
    `wikis.import_markdown` reads a folder (or zip) of `<slug>.md` back through
    `store`, so every page is rendered, versioned and re-embedded like any other
    write. Four decisions are load-bearing there, and
    `tests/test_markdown_roundtrip.py` pins each. *The export carries a
    frontmatter header* — the title is a column and `[[links]]` resolve by
    title, so a wiki restored from filenames alone comes back with every link
    broken; `parent:` rides along, which is how the flat format stopped losing
    hierarchy. *`title`/`parent` are structure, not properties*: read into the
    columns and taken back out of the text, while `tags:` and every other key
    are stored verbatim so the ordinary write path indexes them — export →
    import → export is a fixed point. *The filename is the slug* on the way in
    because it was on the way out; an existing slug **merges** (updated in
    place, versioned, never deleted), two files claiming one slug are refused
    before the first write, and a file with no `parent:` never moves an existing
    page to top level, because markdown cannot tell "top level" from "doesn't
    say". *A folder a person opens becomes a new wiki*; merging into an existing
    one is `into=`, which is what the MCP tool does for the docs/ round-trip.
    What markdown does **not** carry — images, history, comments, templates,
    elements, trash, order, stars — is named in the doc rather than implied
    away: it is the shape for a repo, `.wiki` is the faithful copy.

13. **The old name is still out in the world: read it, never write it.** Jamgraf
    was Waikiki, and the old names are written into places this repo cannot
    edit — Claude Desktop configs (`WAIKIKI_MCP`, `WAIKIKI_DATA`, and a command
    ending `Waikiki.app/Contents/MacOS/Waikiki`), doorman, browsers' cookie jars,
    `waikiki://` links in people's notes, MCP servers started before an upgrade.
    A pure rename passes every test while abandoning all of them, which is the
    trap. So every old name is **read** as a fallback (`config.env`,
    `config.cookie`, both headers, both schemes, both Keychain services, both
    Bonjour names) and **never written**, and each stays until whatever still says
    it has been changed — usually a file on someone else's machine, so removing
    one is its own decision, never a tidy-up. The table is
    `docs/rename-to-jamgraf.md`. Four things are load-bearing beyond courtesy.
    **The old data folder is a symlink to the new one**, because `config.py`
    creates its data folder at import: without it a stale `WAIKIKI_DATA` is handed
    a fresh, *empty* install where the wikis were, silently
    (`config.migrate_legacy_data_dir` — all or nothing, `rmdir` never `rmtree`,
    hands off when both hold data). **`release.sh` refuses `WAIKIKI_UPDATE_PUBKEY`
    exactly like the new name**, because the app reads it as a fallback; refusing
    one spelling reopens the trapdoor #103 closed. **Sign-out deletes under both
    Keychain services**, or the next read moves the old token back and resurrects
    the session. **Never create a repo called `VerinFast/waikiki`**: every
    pre-rename install finds updates by following GitHub's redirect from that
    name, and a new repo there ends it permanently. Some identifiers kept their
    old value on purpose (`PUBLIC_KEYS_HEX`, `waikikey`, `good-place-waikiki`,
    `waikiki-zip-1`, `waikiki.db`) — they are not names. `tests/test_rename_compat.py`
    and `tests/test_rename_migration.py` guard all of it; each guard fails when
    removed, and the build's second smoke test boots the bundle through the old
    executable name with the old env vars on every build.

## Before committing

- `python -m pytest` — the suite is the correctness anchor. Don't weaken, skip,
  or `xfail` a test to make a refactor pass; fix the refactor.
- Keep files under ~500 lines where practical and keep `README.md` ⇄ `CLAUDE.md`
  ⇄ `docs/` in parity.

## Scope discipline (RFC 0001)

Phase 0 (this structure) changes **no** schema, storage, API surface, or auth.
Postgres/RLS (P1), tenancy/Keycloak/Casbin (P2), tsvector+pgvector search (P3),
externalised collab (P4), agents replacing the local CLIs — chat/imagegen/clirun/
bonjour/tunnel (P5), and the desktop sync client (P6) are **later phases**. Don't
pull them forward.

Note: canonical Y.Doc persistence + the `wiki_interchange` round-trip (rules 6–7,
`jamgraf/ydoc.py`) belong to the newer **atd-v3 / Kahala** track (issue #3391),
not to RFC 0001's phases. They stack on the Phase 0 chokepoint but are their own
work: the interchange *format* and *canonical state*, not the P4/P6 transport.
The local desktop surface is deliberately left intact (the monorepo fold is a
separate ticket).
