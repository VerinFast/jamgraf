# 🌺 Jamgraf

A small, local, SQLite-backed wiki built for **Human ↔ LLM collaboration** — where
Claude (via Claude Desktop over MCP) and you edit the **same document at the same
time**, seeing each other's changes live.

> **Jamgraf used to be called Waikiki.** If you're upgrading, there is nothing to
> do: the first launch moves your wikis, your Kahala sign-in and the app itself to
> the new name, and everything that still says the old one keeps working — your
> Claude Desktop config, doorman, `waikiki://` links, all of it. If you had both an
> old Waikiki folder and a new Jamgraf one full of wikis, nothing is merged or
> moved; **Manage wikis** tells you where each is. The details, and what to never
> do (create a repo named `VerinFast/waikiki`), are in
> **[docs/rename-to-jamgraf.md](docs/rename-to-jamgraf.md)**.

## Requirements → how

| Requirement | How |
|---|---|
| Python + SQLite-backed | FastAPI app; one SQLite file per wiki (`data/wikis/<slug>.db`) holds its pages, images, FTS index, and vectors |
| Multiple isolated wikis | Separate DB per wiki; pages, search, and `[[links]]` never cross |
| Themes | Swappable CSS in `jamgraf/static/themes/` (default / dark / sepia) |
| Images | Upload / paste / drag → stored in SQLite, served at `/image/{id}` |
| Extensible | Layered `routes → store (repository) → db (SQLite)`; open-source deps |
| MCP + REST | `jamgraf.mcp_server` (FastMCP) and `/api/*` share one code path |
| Markdown → HTML | `markdown-it-py` (GFM), `[[wiki links]]`, Pygments code |
| Tables | GFM tables via the `gfm-like` preset |
| Human editing | EasyMDE editor with live preview |
| Versioning | Every save snapshots to `page_versions` (human / ai / collab authored) |
| BM25 / RAG | SQLite **FTS5 `bm25()`** + **sqlite-vec** vectors, fused with RRF |
| **Real-time co-editing (CRDT)** | **Yjs / `pycrdt` room per (wiki, page); browser + Claude edit live with presence** |
| AI streaming | Claude writes into the live doc via MCP; plus an optional pull-model "Generate" button |

## Multiple isolated wikis

Jamgraf hosts several **fully isolated** wikis (e.g. Beaconlight, Crosslake,
StartupOS) — each is its own SQLite file under `data/wikis/`. Pages, search, and
`[[wiki links]]` **never cross between wikis**: a link resolves only within the
active wiki's own database, so contamination is structurally impossible.

- **Humans** switch with the wiki dropdown in the header (a per-browser cookie).
- **Claude** has a *separate* active wiki, changed only with the MCP
  `switch_wiki` tool. Every content tool refuses to run until a wiki is chosen
  and **echoes the wiki it acted on**, so the AI can never silently cross wikis.
- **Each agent** gets its own active wiki, scoped to its MCP session. Two agents
  can work in two different wikis at once without moving each other, and the
  choice is never written to disk — so a restarted server leaves an agent with no
  active wiki rather than inheriting whichever one another agent last picked.

These are all independent by design — the human browsing Crosslake doesn't move
Claude, one agent doesn't move another, and vice-versa. To co-edit, ask Claude to
`switch_wiki` to the one you're in.

### Renaming a wiki

A wiki has a **name** and an **address**, and *Rename…* on the **Wikis** page
offers them separately, because they are not the same kind of change.

The **name** is the label you read in the switcher and the header. Changing it
touches nothing else, which is what you want for a wiki that arrived from
somewhere else under a name you wouldn't have picked — one cloned from Kahala as
`startupos` that should read *StartupOS*.

The **address** is the wiki's identity: it is in every URL, it is the filename of
its database, and it is what Claude's `switch_wiki` takes. Changing it renames
the file, so links using the old address stop working and any tab with that wiki
open needs a reload. Jamgraf handles the rest — your browser follows the new
address, unsaved editor text is written out first, and an agent still holding the
old one is told to pick the wiki up again rather than being quietly moved into a
different wiki. The **Help** wiki keeps its address (the app re-creates `help`
whenever it's missing, so a renamed one would come back as a second copy); its
name is yours to change like any other.

Claude can change the **name** with the `rename_wiki` tool. The address is the app's to change, not Claude's: the live editors that have to be saved and closed first are held by the app itself, in a different process from the one Claude talks to.

### Save / Open wikis to files

Because each wiki is a single self-contained SQLite file, you can **Save** one to
a location you choose and **Open** an external wiki file back in — from the
**Wikis** page (⚙ in the header):

- **Save to file…** writes a consistent `.wiki` snapshot (safe even while the wiki
  is in use). In the desktop app this is a native Save dialog; in a browser it
  downloads the file.
- **Open wiki file…** validates the file and brings it in as a new isolated wiki.
  Native Open dialog in the app; a file upload in a browser.
- **Export .md** downloads every page as a folder of markdown, and **Open
  markdown folder…** (desktop app) reads one back in.

`.wiki` files are just SQLite, so they're easy to back up, move between machines,
or share.

**Open takes any shape Jamgraf can write.** A `.wiki` save wraps a whole
database; a **wiki bundle** — what *Push/Pull with Kahala* moves, and what
Kahala's own export downloads — carries each page as an interchange snapshot and
no database at all; **markdown** is one `.md` per page and nothing else. They
are all zips (markdown can also be a plain folder), so Open reads what the file
*is* rather than what it is called. A bundle opens as a new wiki carrying its
pages, hierarchy, elements, templates and images, and is named after the wiki it
came from, not the file: a bundle labelled *StartupOS* opens as StartupOS,
however the download happened to be named. A bundle that can't be read is
refused in the bundle's own words and leaves nothing behind.

**A folder of markdown comes back as a wiki.** Each file's name is its page's
address, and the frontmatter header the export writes carries the page's title
and — for a sub-page — its parent, so the pages come back under the same names
with the same shape and their `[[links]]` still resolving. Tags and any other
frontmatter you wrote stay exactly as they are. Opening a folder makes a new
wiki; Claude can also read one straight back into a wiki you already have
(`import_markdown`), which is the loop worth knowing: export a wiki into a
repo's `docs/`, edit the text there with everything else, bring it back. A page
that comes back changed is *updated*, not replaced — the previous text is in its
history — nothing is ever deleted for being absent from the folder, and two
files that would claim the same page are refused before anything is written.

Markdown is text only, though: no images, history, comments, templates or
trash. It is the shape for a repo; a `.wiki` file or a bundle is the faithful
copy.

Anything Jamgraf can export, Jamgraf can open again. That's a rule, not a
coincidence — an export you can't restore is a file that only looks like a
backup. The one exception is PDF, which is a page rendered for a person to read
rather than a copy of the wiki. The decision, the release this was learned from,
and what a folder of markdown means on the way in are in
**[docs/export-import-parity.md](docs/export-import-parity.md)**.

Opening a file also **rebuilds the search index** when the file arrived without a
usable one. Search runs over a chunk index, not over the page text directly, so a
file whose index didn't travel — or one embedded by a different model on the other
machine — would otherwise open as a wiki that renders and links perfectly and finds
nothing, with no error to explain it. A file that arrives correctly indexed is left
alone, so opening a large healthy wiki is still instant.

### History, trash & retention

- **Version history:** every save snapshots the page. Each article says when it
  last changed — *Edited 3 minutes ago — 4 earlier versions you can go back to*,
  under the tabs — and that line opens the page's **History** with the versions
  listed. (A page nobody has edited yet says so instead of pointing at an empty
  list.) The same place is reachable the long way round: **Details** →
  **History**. From there, view any version (with a diff vs current) and
  **Restore** it. Restoring is itself a save, so the text you just undid is still
  there. Retention keeps the last *N* versions per page (default 50; set per wiki
  in Settings, 0 = all).
- **Trash (soft delete):** deleting a page moves it to the **Trash** (header link),
  hidden from lists and search but restorable. **Restore** brings it back;
  **Delete forever** removes it permanently. Trashed pages are auto-purged after
  *N* days (default 30; per wiki in Settings, 0 = never). Claude's `delete_page`
  is also soft; it has `list_trash` / `restore_page` tools.
- **Wiki stats:** the **Wikis** page shows per-wiki article count, internal-link
  count (with **broken** — red-link — count), size on disk, and trash count.

### Backups & restore

Jamgraf backs itself up **automatically, on by default**: every 24 hours it takes
a consistent snapshot of *every* wiki (SQLite's online backup API, so it's safe
mid-write) and keeps the last 7. They land in
`<data>/backups/<YYYY-MM-DD_HHMM>/<slug>.db`; the schedule, the count, and a
**Back up now** button are in Settings → Backups.

**To restore:** quit Jamgraf, then **Manage wikis → Open** on a `.db` from a
backup folder. It opens as a *separate* wiki, so you can compare before replacing
anything. Images and version history come with it — a snapshot is a complete
copy of the wiki.

These are local copies on the same disk as your wikis: insurance against
corruption, a bad import, or a mistake — **not** against losing the machine. Keep
a `.wiki` export somewhere else too. Backups only run while the app is open, so
days you never launched it have no snapshot.

**If a wiki file is damaged**, the app still starts and every other wiki keeps
working — each wiki is a separate file, and one bad file is not allowed to take
the app down with it. The broken wiki says so where you meet it: *Manage wikis*
lists it as *can't be read*, with the reason, the backups folder, and the steps
to open a snapshot; visiting one of its pages explains the same thing instead of
showing an error. Nothing is deleted, rewritten or "repaired" — the damaged file
is your data, and it may still be recoverable. The scheduled backup skips it and
still snapshots everything else.

What survives a crash, a corrupt file, a failed import or a bad edit — and what
doesn't — is audited end to end, with the experiments that established each
answer, in **[docs/data-safety.md](docs/data-safety.md)**.

## How the collaboration works

```
   your browser  <--y-websocket-->  [ CRDT room (pycrdt) ]  <--HTTP inject--  MCP server
   (EasyMDE + Yjs, presence)               |                                  (Claude Desktop)
                                      (debounced)
                                           v
                          render HTML + snapshot to SQLite + RAG reindex
```

- Open a page's editor and you join a **CRDT room**; a colored presence chip shows
  who else is there.
- When Claude (through the MCP `append_to_page` / `replace_page` tools) writes,
  it lands in the **same room** and streams into your open editor live — and a
  **"Claude" presence chip** appears while it writes.
- Concurrent edits **merge** (CRDT), so you don't overwrite each other.
- Edits are debounced-persisted to SQLite, re-rendered to HTML, and re-embedded
  for search.

The old in-editor **✦ Generate** button is a *separate* convenience: it pulls a
one-off draft from the Anthropic API (needs `ANTHROPIC_API_KEY`). The real
collaboration path above uses no API key — the text comes from Claude over MCP.

## Architecture — layers

Data access is layered so routes stay thin and all SQL lives behind one seam:

```
HTTP routes / MCP tools   api.py, mcp_server.py   parse + validate, call the repository
        ▼
Repository (data access)  store.py, elements.py, wikis.py, rag.py   owns the SQL
        ▼
Infrastructure            db.py   SQLite connection, schema, FTS5 + sqlite-vec
        ▼
SQLite file per wiki      data/wikis/<slug>.db
```

Route handlers **never open a cursor and never contain SQL** — they parse the
request, call a repository function (`store.get_page`, `store.parent_of`,
`store.get_setting`, …), and shape the response. That invariant is guarded by
`tests/test_repository_chokepoint.py`. This is Phase 0 of
[RFC 0001](docs/rfc/0001-multi-tenant-waikiki.md); see
[docs/repository-layer.md](docs/repository-layer.md) for the full rationale and
the seam where multi-tenant scoping attaches later.

### Typed page metadata (optional)

Frontmatter properties are plain strings, and stay plain strings. What a
**template** can declare is what those strings are supposed to *mean* — a few
lines of `name[*]: type` in the template editor (`str`, `int`, `float`, `bool`,
`date`, `list[str]`, or a choice like `player | npc`), compiled to `pydantic`
checks in [`jamgraf/metaschema.py`](jamgraf/metaschema.py).

Pages made from such a template carry a `template:` property and are checked
against it on their Metadata tab and through MCP's `get_metadata`/`set_metadata`.
Checking **warns; it never blocks**. A wiki is a place for half-finished notes:
the write always lands, the value on disk is never rewritten (`20 / 100` stays
`20 / 100`), and the mismatch is reported instead. A template that declares
nothing behaves exactly as it always did, and pages written before a schema
existed are untouched by it.

### The Y.Doc is canonical

A page's content is a **CRDT, not a string**. The full encoded `pycrdt` Y.Doc for
every page is persisted in `page_ydoc` and is the **source of truth**; the
`markdown`/`html` columns are a *projection* kept alongside it for full-text
search, rendering, and RAG. Every content write goes through the repository and
advances the canonical Y.Doc, so the CRDT accumulates real history across saves
and restarts (the live co-editing room in `collab.py` is just an editing buffer
whose flush lands through the same seam). See [`jamgraf/ydoc.py`](jamgraf/ydoc.py).

One save is **one transaction**: the page row, its version snapshot, its tags and
the canonical Y.Doc all land together or not at all, so a crash or a power cut
cannot leave the truth a revision behind the text you can see. The search index is
the one thing deliberately left outside — it embeds, which can be slow, and it is
a cache that can be rebuilt from the markdown at any time, so it is refreshed just
after the save commits rather than while it holds the write lock. The reasoning
and the measurements are in [docs/data-safety.md](docs/data-safety.md).

Renaming or deleting a wiki moves that wiki's three files, and that is the one
place the operating system has an opinion: Windows will not rename or delete a
file while anything still has it open, where macOS and Linux will. So the move
closes the handles it has to close first, **refuses** rather than closing one out
from under a save that is part-way through, and holds readers for the
milliseconds it takes — long enough that nobody can have an empty database
created at the address the wiki has just left. The one durability property that
genuinely differs between the two platforms is named in
[docs/data-safety.md](docs/data-safety.md#6-moving-a-wikis-files-where-the-os-will-not-move-an-open-file).

Because the persisted state is a genuine CRDT, Jamgraf can **export** a page as a
snapshot (full Y.Doc + a content-addressed image sidecar) or a changelog (updates
since a peer's state vector), and **import** the same from a peer — the local half
of the round-trip with the hosted platform. The format is the vendored,
version-pinned [`wiki_interchange`](docs/vendoring.md) library; it carries content
only (no tenancy or permissions — those are the server's), regenerates embeddings
locally on import, and **rejects** an incompatible spec/protocol version rather
than merging bad bytes.

A whole wiki travels as a **bundle** (`store.export_wiki_bundle` /
`import_wiki_bundle`): every page's Y.Doc, the page hierarchy **by slug**, the
manual order and starred flags, the custom elements, the templates with the
metadata schemas they declare, and one copy of each distinct image blob however
many pages embed it. It streams to and from a file — a real wiki is hundreds of
pages and tens of megabytes — and a malformed or version-incompatible bundle is
refused in full *before* the first local write, so a failure part-way through
leaves the wiki untouched rather than half-imported. Pages merge by slug:
existing ones are updated (and versioned) in place, and nothing local is deleted.
Hierarchy travels by slug because integer page ids are local — the same page is a
different number in every wiki.

### Sync with Kahala

Kahala is the shared, server-side half of the same wiki. A local wiki can be
**linked** to one there, then **pushed** up or **pulled** down; a remote wiki can
be **cloned** into a new local one. It's all in the *Kahala* pane.

Only content travels — pages, hierarchy, elements, templates and images. Tenancy
and permissions are the server's and are re-attached there; embeddings are
regenerated on arrival rather than shipped.

**Push and pull both merge, and neither deletes.** A push adds and updates pages
on Kahala and removes nothing there; a pull does the same here. So a page you
deleted locally is still on Kahala afterwards, and the next pull brings it back.
Removing a page from both is a deliberate act on both.

The wiki has to already exist on Kahala — a push won't create it, because
creating one there is a browser form tied to your Kahala session rather than
something the app's token can reach. *Open Kahala…* takes you to the page where
you make one.

Only what changed travels — a few kilobytes rather than the whole wiki — and
custom elements, templates and images travel with the pages, so a page never
arrives referring to a definition that didn't. If the server is running an older
version, or an image doesn't survive the trip, Jamgraf transfers the whole wiki
instead and tells you that's what it did. *Transfer the whole wiki* forces it.
If a transfer stops part-way — a full disk, say — it says it only *partly*
merged rather than claiming it was refused: nothing is ever deleted, every page
that arrived is an ordinary page with its history, and running the same transfer
again finishes the job.

Signing in uses Kahala's own sign-in page — OpenID Connect with PKCE, no client
secret, and the callback comes back to Jamgraf's own loopback port. In the
desktop app it opens in your **web browser** rather than inside Jamgraf's window,
so you get your usual session, your password manager and whatever second factor
your organisation uses; the app notices by itself when you're done. The refresh
token goes in your **Keychain**, never into a wiki file (that file *is* what
"Save wiki" hands over) and never into a plain config file. Where there's no
secure store, signing in is switched off and says so rather than falling back to
writing the token somewhere less safe. The link itself — the address and the
remote wiki's name — is recorded outside the wiki too, so it can't travel to
someone you share the wiki with.

Someone who joins over LAN sharing can't reach any of it. An agent connected over
MCP can push and pull, but can't link, clone or sign in — choosing where a wiki
gets sent is yours.

Full detail, including every refusal and why redirects are never followed, in
**[docs/kahala-sync.md](docs/kahala-sync.md)**.

## Setup

```bash
cd ~/localdev/jamgraf
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

First run downloads the default embedding model (`fastembed` BGE-small, ONNX,
~130 MB) and warms it at startup. No PyTorch required.

**Embedding models are managed in the UI** — Settings → *Embedding model* → paste
a HuggingFace slug and click **Add model**. It downloads, becomes active, and all
pages are re-embedded (vector-dimension changes handled automatically). The
`fastembed` provider (ONNX, default) needs no PyTorch; the `local` provider
(any sentence-transformers slug) needs the optional `requirements-local.txt`.

## Run the web app

```bash
python run.py
# → http://127.0.0.1:8787
```

## Desktop app (macOS)

Jamgraf ships as a native `.app` (a WKWebView window over the local server) — see
[Releases](https://github.com/VerinFast/jamgraf/releases). Or build it yourself:

```bash
bash scripts/build_macos.sh
# → dist/Jamgraf.app  (+ dist/Jamgraf-macos.zip)
```

The app is currently **unsigned**, so on first launch macOS Gatekeeper will warn:
right-click the app → **Open** → **Open** to allow it (once). Signing/notarization
is a later step.

The packaged app stores its data in **`~/Library/Application Support/Jamgraf`**
(not inside the bundle), so it survives moving/replacing the app. Override with
`JAMGRAF_DATA`.

### Updates

**Settings → Updates** checks GitHub for a newer release and can install it: the
app backs up every wiki, downloads the release, verifies it, then quits and
relaunches itself to finish. Your content lives outside the bundle, so an update
replaces code only.

Because the app carries no Apple identity, it can't rely on macOS to tell a real
release from a tampered one — so every release zip is **Ed25519-signed** and the
app verifies it against a public key pinned at build time, *before* unpacking.
Anything unsigned, wrongly signed, or modified is refused, and a build with no
pinned key disables updating rather than trusting the download.

Checks are check-only and hourly at most; installing is always an explicit click,
since the swap restarts the app. Cutting a signed release is
`./scripts/release.sh v0.14.0` — see **[docs/updates.md](docs/updates.md)** for
the trust model, key handling, and failure modes.

### Capabilities and one-click fixes

Jamgraf shells out to tools it doesn't ship — the `claude`/`gemini` CLIs, an
image CLI, `cloudflared`, Ollama. **Settings → Capabilities** says which of them
this machine actually has, what each one powers, and what is missing, *before*
you press anything. Chat, ✦ Generate and the editor's ✨ image button render
greyed out and linked to that view when they can't work, instead of failing a
minute after the click.

Where something can be fixed, there's a **button**, not a command to copy — and
it offers the step that is actually possible: without `npm`, "install the Claude
Code CLI" becomes "install Node.js first", and with no Homebrew either it becomes
instructions with a link rather than a button that would fail. Installing asks
first, says what it is about to do, and reports honestly when it doesn't work
(`npm i -g` failing on permissions is common, and reads as exactly that).

Where nobody publishes an install path, Jamgraf says so and points at the setting
you can change — it won't guess at a command. Doorman is listed but never
offered as a fix: it is your app, and Jamgraf never installs or starts it. The
one installer that pipes a download into a shell (Google's Antigravity CLI, the
default image tool) names the host it fetches from before it runs. See
**[docs/capabilities.md](docs/capabilities.md)**.

### Doorman (optional)

If you also run [Doorman](https://github.com/VerinFast/doorman), Jamgraf will
notice and use it for things it does better:

- **Speech** — its voices for **Listen** and *"Say this word"*.
- **Generation, chat and images** — the agents and image models you already
  configured there, instead of a second set of API keys and CLIs here. Whatever
  answers is named in the editor's status line, under the chat reply, and in
  **Settings → Doorman**, so you always know which model wrote something.

Each of those is capability-probed, not version-sniffed: an older Doorman
without them simply keeps Jamgraf on its own Anthropic/Ollama/CLI path, silently.

It is optional in every direction: Jamgraf never starts or installs Doorman,
everything works identically without it, and you can decline the integration even
while Doorman is running (**Settings → Doorman**). The one exception is Jamgraf
displayed *inside* Doorman's own window, where "optional" has no meaning — there
the setting is shown locked, with the reason. See
**[docs/doorman.md](docs/doorman.md)**.

### Deep links

`jamgraf://` URLs open the app at a specific place, and survive the app picking a
different port (which an `http://127.0.0.1:8787` link doesn't):

```
jamgraf://beaconlight/meru            # a page
jamgraf://beaconlight/meru#abilities  # a section
jamgraf://beaconlight                 # a wiki's front page
jamgraf://beaconlight?q=clockwork     # search, inside that wiki
jamgraf://                            # the front page
```

**Page options → Copy link** copies one, and MCP `get_page` returns the same
thing as `link` — so an agent can hand you something you can actually open.

The wiki is the authority, so there are no reserved verbs to shadow a wiki named
`search` or `home`. A URL scheme is an input anything on the machine can fire, and
the app window has owner rights, so parsing is a strict **allow-list**: every slug
validated, paths constructed rather than echoed, search always scoped to the
validated wiki. See
**[docs/deep-links.md](docs/deep-links.md)**. Deep links work in the packaged
`.app` only — a source run has no `Info.plist` for macOS to route through.

### Calendar feeds

A `calendar` element can show events typed into the page, subscribe to a real
calendar, or both:

````
```calendar
ics: https://calendar.google.com/calendar/ical/.../basic.ics
tz: America/New_York
events: [{"title":"Troop 63 Campout","start":"2026-09-11","end":"2026-09-13","link":"troop-63-campout"}]
```
````

The `ics` address keeps itself current; the `events` list holds the entries that
link to pages in this wiki, which a subscription can't do. To add or change a
calendar, edit the `ics:` line — in Google Calendar the address is *Settings →
your calendar → Integrate calendar → Secret address in iCal format*. Nothing else
to configure.

Jamgraf fetches the calendar server-side because no provider allows a web page to
read one (there are no CORS headers on any of them), via
`/api/calendar-feed?url=…`. Since the page chooses that URL, the **allow-list is
the only thing keeping this from being an open proxy**: any web page in your
browser can call our loopback port, so the route accepts nothing but https at a
known calendar host — otherwise a website could aim it at your router or a
metadata endpoint. Note the trade: the address sits in page content, and a Google
secret address reads the whole calendar, so it is visible to anyone who can read
the page.

Recurring events are expanded properly — repeat rules, deleted occurrences, and
single occurrences that were moved — and placed on the day they fall on where you
are, so an 8pm event doesn't show up on tomorrow. If the calendar host can't be
reached the element says so rather than rendering an empty month, which would
claim nothing is scheduled. See
**[docs/calendar-feeds.md](docs/calendar-feeds.md)**.

### Reports from agents

An agent that hits a bug or a limitation in Jamgraf can write it down with
`report_bug` while it still has the failing call in hand. Reports are **queued,
not sent**: they appear under *Reports from agents* (the menu entry shows up only
when something is waiting), and *Open on GitHub* opens GitHub's own new-issue
form pre-filled, so nothing is published until you submit it there.

That indirection is deliberate. Filing directly would mean Jamgraf holding a
GitHub token — and, more to the point, a report written at the moment something
failed often quotes the page it was working on. Reading it first is what keeps
your wiki's content off a public tracker.

## Connect Claude Desktop (MCP)

**Easiest:** open Jamgraf and click **Connect Claude** in the header (or visit
`/help`). That page shows a config **pre-filled with this install's real paths**
and a copy button — paste it into Claude Desktop's config and you're done.

The config file lives at
`~/Library/Application Support/Claude/claude_desktop_config.json` (create it if
it doesn't exist). Keep Jamgraf running, paste the config, then **fully quit and
reopen Claude Desktop** (⌘Q). Two forms:

**A) Packaged app** — Claude Desktop launches the `.app` itself in MCP mode
(no source checkout needed). Replace the path with where your app lives:

```json
{
  "mcpServers": {
    "jamgraf": {
      "command": "/Applications/Jamgraf.app/Contents/MacOS/Jamgraf",
      "env": {
        "JAMGRAF_MCP": "1",
        "JAMGRAF_DATA": "/Users/YOU/Library/Application Support/Jamgraf",
        "JAMGRAF_WEB_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

**B) Running from source** — launch the venv Python:

```json
{
  "mcpServers": {
    "jamgraf": {
      "command": "/Users/YOU/localdev/jamgraf/.venv/bin/python",
      "args": ["-m", "jamgraf.mcp_server"],
      "env": {
        "PYTHONPATH": "/Users/YOU/localdev/jamgraf",
        "JAMGRAF_DATA": "/Users/YOU/localdev/jamgraf/data",
        "JAMGRAF_WEB_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

> `JAMGRAF_DATA` **must match** the data directory the running app uses, so the
> MCP server and the window share the same wikis. The in-app Help page fills this
> in for you.

Then, in Claude: *"list the jamgraf wikis and switch to Beaconlight."* Claude
must `switch_wiki` before it can read or write — this is what keeps wikis from
mixing. Open a page's editor and ask Claude to add a section; watch it type in
beside you.

MCP tools: `list_wikis`, `current_wiki`, `switch_wiki`, `create_wiki` (wiki
selection — required first), then `list_pages` (top-level by default, like the
sidebar; `children=true` for the whole wiki, `children=["a-parent"]` for one
branch — and it always reports how many sub-pages it withheld, so an agent never
reads silence as absence), `get_page` (returns a heading outline plus its
resolved outbound `links` — target, title, the label the reader sees, and
whether the page exists, plus a one-line `hint` asking the agent to read the
linked pages it is about to rely on), `read_pages` (the same payload for up to 10
slugs in one call, so following those links costs one round-trip instead of ten —
slugs that don't exist come back in `missing` rather than failing the batch, and
anything past the cap in `dropped`), `create_page`, and a family of **merge-safe
live edit** tools:
`edit_page` (find/replace — preferred), `replace_section`, `insert_after` /
`insert_before`, `prepend_to_page`, `remove_from_page`, `append_to_page`, and
`replace_page` (full rewrite, last resort). Plus `changes_since` (change feed),
`backlinks`, `broken_links`, `delete_page` (to trash), `list_trash`,
`restore_page`, and `search` (hybrid RAG) — all scoped to the active wiki.

Templates get the same read-before-you-write treatment: `get_template` returns a
template's markdown and metadata schema, and `edit_template` replaces one exact
snippet of it. Without those, changing a single line of a template meant
rebuilding the body from a page it produced and overwriting everything that page
didn't happen to show.

## REST API

| Method | Path | |
|---|---|---|
| GET | `/api/pages` | list |
| POST | `/api/pages` | `{title, markdown}` |
| GET/PUT/DELETE | `/api/pages/{slug}` | get / update / delete |
| GET | `/api/search?q=&k=` | hybrid BM25+vector RAG |
| POST | `/api/images` | multipart upload |
| POST | `/api/collab/{slug}/append` · `/replace` | inject a live edit (used by MCP) |
| GET | `/api/collab/{slug}/live` | current live (unsaved) markdown |
| POST | `/api/ai/stream` | SSE token stream (pull-model Generate button) |
| GET | `/api/calendar-feed?url=&tz=` | events from a subscribed calendar (allow-listed hosts only) |

Interactive docs at `/docs`. Websocket sync at `ws://host/collab/{wiki}/{slug}`.
REST/collab requests select the wiki via the `X-Jamgraf-Wiki` header (default:
the registry's default wiki).

## Notes

- **sqlite-vec** loads via **apsw** (bundles a SQLite with loadable-extension
  support, which stock CPython often lacks). If unavailable, search degrades to
  **BM25-only** and everything else still works.
- Switching the embedder in Settings changes the vector dimension, so the vector
  index is rebuilt and all pages re-embedded automatically.
- Collaborative editing loads Yjs (`yjs`, `y-websocket`, `y-codemirror`) from
  esm.sh and EasyMDE from unpkg — an internet connection is needed for the
  editor libs (they can be vendored locally later). The read view, REST, and MCP
  work fully offline.

## License

[Elastic License 2.0](LICENSE) — **source-available, not open source.**

You can read it, run it, modify it, and redistribute it. The one substantive
restriction is that you may not offer Jamgraf to third parties as a hosted or
managed service. Use it yourself, inside your company, on your own machines,
freely.

The vendored `wiki_interchange` package (`jamgraf/vendor/`) is first-party and
covered by the same license — see [`docs/vendoring.md`](docs/vendoring.md).
