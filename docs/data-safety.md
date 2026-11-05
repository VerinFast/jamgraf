# Data safety

*The 1.0 audit — issue [#68](https://github.com/VerinFast/jamgraf/issues/68).*

A family wiki losing content is the worst outcome this app has. Backup and
restore code existed and several safety properties had been established along
the way, but nobody had checked the whole story end to end. This is that check.

**Everything below was made to happen, not reasoned about.** Databases were
opened and their pragmas read; processes were killed mid-write; wiki files were
truncated and scribbled on; an import was failed part-way through; a 215-page
wiki was restored from a real backup. Where the honest answer is "we accept this
risk", it says so. The tests that pin each real-but-previously-unproven property
live in `tests/test_data_safety.py`.

Method note: the developer's own wikis were only ever opened **read-only**, and
every destructive experiment ran against a copy in a scratch `JAMGRAF_DATA`
directory.

**Summary**

| # | Question | Verdict |
|---|---|---|
| 1 | Crash / power loss mid-write | WAL and `synchronous=FULL` confirmed on every file; no committed write was lost. A page save was **several** transactions, so a crash between them left the canonical Y.Doc behind its markdown projection — **fixed since this audit ([#72](https://github.com/VerinFast/jamgraf/issues/72))**: one save is now one transaction on both backends. Live typing is still durable only ~1.5–2.5s after you stop. |
| 2 | A corrupted wiki file | **Fixed since this audit ([#71](https://github.com/VerinFast/jamgraf/issues/71)).** The app now starts whichever wiki is damaged, including the default; the neighbours are unaffected; *Manage wikis* lists what it can and marks what it can't read; and the damaged file is named, explained and left byte-for-byte alone. |
| 3 | Restore | Works, proven on a real 55MB backup: 215 pages, 75 images, 711 versions. Backups are on by default. Documented only inside the app, and they live on the same disk as the wikis. |
| 4 | Import paths | A part-written import is incomplete but never destructive, and re-running the same bundle finishes it exactly. **1.0 does not need staging-and-swap.** |
| 5 | Version history | Works, and now **findable** ([#73](https://github.com/VerinFast/jamgraf/issues/73), fixed since this audit). Every article carries a line saying when it last changed and how many earlier versions are kept; it opens the history with the list showing. Two interactions, from a sentence that names the thing in plain words. |
| 6 | Moving a wiki's files where the OS will not move an open file | Added after the audit, from Windows CI ([#108](https://github.com/VerinFast/jamgraf/pull/108)). Every `conn.close()` in `db.py` was raising `AttributeError` into a `suppress(Exception)` — the shipped apsw shim had no `close()` at all — so handles leaked on every backend. POSIX renames by directory entry and never noticed; Windows could not rename or delete a wiki at all. Fixed; one property genuinely does not survive the platform, named below. |

---

## 1. Crash / power loss mid-write

### Is WAL actually on for every wiki?

Yes — checked per file, on a cold read-only handle, which is what a recovering
process sees. The developer's real install:

```
beaconlight.db     journal=wal sync=2 pages=224 ydoc=114 versions=711  (55 MB)
crosslake.db       journal=wal sync=2 pages=0
doorman.db         journal=wal sync=2 pages=4
ever-afterlife.db  journal=wal sync=2 pages=6
help.db            journal=wal sync=2 pages=11
main.db            journal=wal sync=2 pages=0
startupos.db       journal=wal sync=2 pages=78 ydoc=0  versions=156
```

and every file a fresh install creates, with `PRAGMA integrity_check` = `ok` on
each. `sync=2` is `FULL`, which is stronger than WAL's usual `NORMAL`: SQLite
fsyncs the WAL before each commit returns, so an unexpected power cut cannot
lose an already-acknowledged write. This is now pinned by
`test_every_wiki_file_is_wal_with_full_sync`.

### Does a hard kill lose committed writes?

No. The real app was started on a scratch data directory and hammered with
`/api/autosave` from four threads; `SIGKILL` arrived mid-burst. **826 writes had
been acknowledged; all 826 were present after the kill**, `integrity_check` was
`ok`, and the leftover `-wal` recovered on next open with no repair step.

### Can a half-written Y.Doc outlive its markdown projection, or vice versa?

The Y.Doc side cannot be half-written: `ydoc._store_state` is a single
`INSERT ... ON CONFLICT`, which SQLite makes atomic on either backend. All 116
canonical Y.Doc blobs in the real install decode cleanly — none is torn.

But **the two could get out of step, and the projection was the one that
won.** `store._set_body` performed the projection write, the version snapshot,
the metadata index, the canonical Y.Doc write and the RAG reindex as *separate*
transactions — under apsw (the default backend, and the one the packaged app
ships) `commit()` is a documented no-op and no `BEGIN` was ever issued, so every
statement autocommitted on its own:

```
apsw 3.53.4.0
in_transaction before insert: False
in_transaction after  insert: False
```

Killing the process at two deliberate points, saving `v2` over `v1`:

| crash point | `pages.markdown` | `page_versions` | canonical Y.Doc |
|---|---|---|---|
| before the version snapshot | `v2` | `v1` only | `v1` |
| before the Y.Doc write | `v2` | `v1`, `v2` | `v1` |

The file is not corrupt in either case — `integrity_check` says `ok`. On next
open the user sees `v2`, because every read path returns `pages.markdown`. The
canonical Y.Doc silently stays at `v1`; a plain read does not repair it, and
`export_snapshot` ships the stale doc. The next ordinary save does repair it.

**This is not hypothetical.** Comparing every canonical Y.Doc against its
markdown across the developer's real wikis found 115 agreeing and one diverged —
the Help wiki's About page:

```
--- canonical ydoc
+++ pages.markdown
-**Jamgraf** version **0.18.0**.
+**Jamgraf** version **0.21.0**.
```

Chasing that produced the more likely trigger, which is not a crash at all.
`rag.reindex_page` sat *between* the projection commit and the canonical Y.Doc
write, so anything that raised in reindexing — an embedder not ready yet, a
missing `sqlite-vec`, a model download failing — committed the projection and
then skipped the canonical write. Reproduced exactly:

```
start markdown : 'version 1 of the ledger'
start canonical: 'version 1 of the ledger'
update raised  : embedder not ready
markdown now   : 'version 2 of the ledger'
canonical now  : 'version 1 of the ledger'      <-- silently a revision behind
```

**Fixed in this PR**, because it is small and obviously correct: the canonical
Y.Doc is now written *before* the derived search index, since `page_ydoc` is the
source of truth and the RAG index is a cache that can be rebuilt from the
markdown at any time. Guarded by
`test_canonical_ydoc_is_written_before_the_derived_index`.

**Also fixed since this audit ([#72](https://github.com/VerinFast/jamgraf/issues/72)):
one page save is now one transaction.** `db.transaction()` issues an explicit
`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` through the shim, and the stdlib
connection is opened `isolation_level=None` so both backends reach a transaction
by the same code path. The same measurement now reads:

```
apsw 3.53.4.0
in_transaction outside transaction(): False
in_transaction inside  transaction(): True
in_transaction mid-save            : True
in_transaction after   transaction(): False
```

The projection, the version snapshot, the tag index and the canonical Y.Doc
commit together or not at all. `BEGIN IMMEDIATE` rather than deferred, because a
save reads (`get_page`, the slug uniqueness loop) before it writes and a deferred
transaction's lock upgrade can fail outright under a second writer — and this app
has a human, an MCP agent and the collab flusher writing the same file. The seam
is re-entrant through savepoints, because store functions call each other and the
import paths wrap a whole page write.

Killing a *separate process* at the same seam as before now lands nothing: the
markdown is still `v1`, there is one version row, the canonical doc is `v1`, and
`integrity_check` is `ok`. `test_a_real_kill_between_the_writes_lands_nothing`
does exactly that on every run, and
`test_a_save_that_fails_part_way_leaves_the_page_exactly_as_it_was` breaks each
seam in turn.

**What is deliberately still outside that transaction:** `rag.reindex_page`.
Chunking and embedding can load a model and take seconds, and holding the write
lock across that would queue every other writer behind an embedding run. The RAG
index is a cache rebuildable from the markdown, so it runs *after* the commit
(`db.after_commit`), and a crash in the gap leaves that page's search index stale
until that page's next save — or until `rag.reindex_all()` rebuilds the lot, which
is what Settings runs when the embedding model changes. The page itself is intact
either way; only its place in the search index is behind. That is the trade,
stated plainly.

**Two smaller seams remain, and they are not repaired automatically:**

* A **divergence that already exists** — like the Help wiki's About page above —
  still heals only on that page's next ordinary save. Nothing rewrites canonical
  state during a read: a read path that writes is its own hazard, and a wrong
  "repair" would overwrite the truth with the projection.
* A **title change** rewrites `[[links]]` in other pages, one ordinary save each
  (`_rewrite_backlinks`). A crash part-way leaves some rewritten and some not.
  No content is lost and every link still resolves, because slugs never move.

### How much live typing does a hard kill lose?

In collab mode the browser editor posts nothing; the server-side flusher owns
persistence, saving a room once its text has been stable for `_FLUSH_IDLE`
(1.5s) on a one-second loop. Measured, by editing a room and then `os._exit`:

* killed **1.0s** after the edit → the edit is gone
* killed **3.0s** after the edit → the edit is on disk

**We accept this risk.** The durability window for typing is roughly 1.5–2.5
seconds after you stop typing, and someone typing continuously with no pause
longer than 1.5s has nothing written for as long as that lasts. A clean quit
does not lose it — `collab.flush_all()` runs before the flusher is cancelled,
which is the whole point of issue #19 — so this is specifically the power-cut /
`SIGKILL` case.

---

## 2. A corrupted wiki file

Two shapes, both on a scratch install with `main` and `beaconlight` seeded.

### A corrupt non-default wiki

The app **starts**, and the other wikis are completely unaffected — pages,
search and links all work. Physical separation earns its keep. Pinned by
`test_a_corrupt_wiki_does_not_take_its_neighbours_with_it`.

Two things still went wrong at the time of the audit:

```
GET /                                    -> 200
GET /wikis                               -> 500   <-- Manage wikis
GET /wiki/home        wiki=main          -> 200
GET /search?q=text    wiki=main          -> 200
GET /wiki/home        wiki=beaconlight   -> 500   (bare "Internal Server Error")
```

`/wikis` — *Manage wikis*, the one page a user would go to in order to get away
from the broken wiki, or to open a backup — built stats for **every** wiki, so
one bad file took the whole page down. And the broken wiki's own pages returned a
bare 500 rather than saying which wiki is unreadable or what to do about it.
Both are fixed in [#71](https://github.com/VerinFast/jamgraf/issues/71) — see
*What #71 changed*, below.

### A corrupt default wiki

The app **did not start**:

```
File ".../jamgraf/api.py", line 33, in lifespan
    db.init_db()
apsw.CorruptError: database disk image is malformed
ERROR:    Application startup failed. Exiting.
```

Every other wiki — including a healthy 215-page family wiki — became
unreachable, though its file was fine. That is exactly the outcome the registry
was supposed to prevent, so it was filed as
[#71](https://github.com/VerinFast/jamgraf/issues/71) rather than fixed inside
an audit.

### What #71 changed

Same scratch install, same damage to `main.db` (the default), after the fix:

```
GET /                                    -> 503   "Main can’t be read" + how to recover
GET /wikis                               -> 200   healthy wikis listed, Main flagged
GET /wiki/home        wiki=beaconlight   -> 200
GET /search?q=…       wiki=beaconlight   -> 200
GET /api/pages        wiki=beaconlight   -> 200
main.db unchanged: True (591189 bytes, sha256 identical)
backup after corruption: {'ok': True, 'wikis': [...4 healthy...], 'skipped': ['main']}
```

Four things carry it, and each is deliberately narrow:

1. **`db.unreadable_reason(exc)`** decides whether SQLite is refusing a *file*
   or our own code is wrong. apsw raises one class per result code
   (`CorruptError`, `NotADBError`, `IOError`, `CantOpenError`…); the stdlib
   collapses them onto `sqlite3.DatabaseError` and its subclasses, so a subclass
   is only believed when the message says what is wrong. `apsw.SQLError: no such
   table: pages` and a `KeyError` in `store.py` are **not** corruption and stay
   loud — pinned by `test_a_bug_is_never_mistaken_for_a_corrupt_file`.
2. **`db.get_conn` raises `WikiUnreadable`** naming the wiki, and both
   connection shims classify per statement, so damage found *after* the header
   opens surfaces the same way. `init_db` reports it and comes up anyway.
3. **The routes say it.** `wikis.health()`/`wikis.stats()` report rather than
   raise, so `/wikis` renders with the healthy wikis intact; a request that
   really needs the damaged wiki gets a 503 page naming it, the file path, the
   reason, buttons to the other wikis, and the restore steps — the packaged app
   is Finder-launched, so a traceback on stderr is invisible and everything
   knowable has to be said in the window. `/api/*` gets the same as JSON, and
   `switch_wiki` refuses over MCP with the reason (rule 5).
4. **Nothing touches the file.** No delete, truncate, rename or in-place
   "repair": it is the user's data in a damaged state and may be recoverable.
   `test_a_corrupt_wiki_file_is_left_exactly_as_it_was` hashes it before and
   after a full browse.

The scheduled backup used to abort the entire run — and delete the directory —
when any wiki failed to copy, so one damaged file meant no snapshot for *any*
wiki on the day one was most needed. It now skips that wiki and reports it in
`skipped`.

Known limit, accepted: **Settings is a per-wiki page**, so while a damaged wiki
is the active one, `/settings` answers with the same 503 explanation rather than
the settings form. The recovery route does not depend on it — *Manage wikis*
carries the backups folder and the restore steps inline — and switching to any
healthy wiki brings Settings back.

### The registry itself

`wikis.json` is a plain JSON file, and `_load` treats anything unparseable as
"no wikis". A torn write there made a user-created wiki vanish from the app
entirely while its database sat untouched on disk:

```
registry now: []
file still on disk: True
reachable through the app: False
```

The seeded wikis came back (because `ensure_initialized` re-seeds the same
names), which is what made this easy to miss — only wikis the *user* created
disappear.

**Fixed in this PR**, because it is small and obviously correct: `wikis._save`
and `appconfig.set` now write beside the file and `os.replace` it, so a reader
sees either the old registry or the new one and never a torn one. The same
applies to `app_config.json`, which holds whether backups run at all. Pinned by
`test_a_torn_registry_write_cannot_lose_a_wiki` and
`test_a_torn_app_config_write_cannot_turn_backups_off`.

---

## 3. Restore

### What actually exists

`jamgraf/backups.py` snapshots **every** registered wiki on a schedule, using
SQLite's online backup API (`db.backup_db`) rather than a file copy, so a
snapshot taken mid-write is consistent. Images live in the `images` table as
BLOBs, so a database snapshot is a complete copy.

* **On by default.** `backup_enabled` is `True` unless explicitly set otherwise.
* **Every 24 hours**, keeping the last **7**, both configurable in Settings.
* **Landing at `<data>/backups/<YYYY-MM-DD_HHMM>/<slug>.db`.**
* Written in full or not at all — a failed run removes its directory rather than
  leaving a partial snapshot that looks like a good one.

Confirmed on the developer's real install: seven snapshot directories, newest
from the previous day, 132MB of them. The gaps between dates are real and
expected — the backup runs from the app's hourly maintenance loop, so **days
when the app was never opened have no snapshot**.

### Does restore actually work?

Yes, proven on the real thing. The newest real snapshot of the 215-page wiki was
copied out (never opened in place) and taken through the documented route —
*Manage wikis → Open* on the `.db`:

```
newest snapshot: 2026-08-20_1631 (55.0 MB)
is_wiki_db: True
opened as: restored-beaconlight
pages restored: 215
first page: The Erasure | '\n![In game pixelart portrait of the demon](/image/75/...'
search works: 20 hits
images: 75 versions: 711
.wiki bundle: 94.7 MB
round-tripped pages: 215
```

Nothing was lost: pages, images and the full version history all came across,
search worked immediately, and the restored wiki exported to a `.wiki` file and
back in again intact. It opens as a **separate** wiki, so you can compare before
replacing anything — the broken original is left alone. Pinned by three tests
(`test_backups_are_on_by_default_and_land_where_the_docs_say`,
`test_a_backup_snapshot_carries_images_and_history_too`,
`test_restoring_a_backup_leaves_the_broken_wiki_alone`).

### Is it documented anywhere they would find it?

Partly, and this is the weak point.

Settings → Backups says it plainly: *"To restore: quit Jamgraf, then use Manage
wikis → Open on a `.db` file from a backup folder"*, along with the folder path
and the honest caveat that these are local copies. That is the right place — for
someone whose app still runs.

Two gaps, **both accepted for 1.0 and written down here rather than left
implied**:

1. **The instructions live inside the thing that might be broken.** ~~If the app
   will not start (see question 2), the user cannot read them.~~ Largely closed
   by [#71](https://github.com/VerinFast/jamgraf/issues/71): the app now starts
   with a damaged wiki, and the restore steps — the backups folder path, the
   newest snapshot, *Open wiki file…* — are printed **on the failure itself**,
   both on *Manage wikis* and on the page that reports the damage, so they are
   read exactly when they are needed rather than found beforehand. `README.md`
   now covers backups and the damaged-file behaviour too. What remains: the Help
   wiki still says nothing about either, and instructions inside an app still
   cannot help someone whose whole install is gone — which is what gap 2 is for.
2. **Backups sit on the same disk, in the same folder tree, as the wikis.** They
   protect against corruption, a bad import and human error. They do **not**
   protect against losing the machine, or against someone deleting
   `~/Library/Application Support/Jamgraf`. Settings says this; it is worth
   saying twice. Keep a `.wiki` export somewhere else.

A footnote from the round-trip above: a `.wiki` bundle is *larger* than the
database it came from (94.7 MB vs 55 MB), because it carries the database and a
copy of each image as a separate file in the zip.

---

## 4. Import paths

`store._read_bundle` decodes every page envelope, every section and every image
blob — re-hashing each against the digest it was stored under — **before the
first local write**. So a malformed, version-incompatible or tampered bundle
leaves the wiki completely untouched. That is the realistic failure, since the
bundle arrives from a peer, and it is already all-or-nothing.

What is not covered is a failure of the **local writes** — a full disk, lock
contention. `docs/vendoring.md` records this as a known limit. So: what does that
actually leave behind?

Measured. A 6-page bundle imported into a wiki that already held a differing
`chapter-1`, with the third local write raising `OSError(28, "No space left on
device")`:

```
import raised: [Errno 28] No space left on device
after the failure, pages present: ['chapter-1', 'chapter-2', 'chapter-3']
re-run result: {'pages': 6, 'elements': 1, 'templates': 3, 'images': 0}
after the re-run, pages present: ['chapter-1' ... 'chapter-6']
every page has its body back: True
chapter-1 version count after two imports: 2
```

So the state after a failed import is **incomplete, but never destructive**:

* nothing local is deleted — the import merges by slug and only ever creates or
  updates;
* every page that did land is an ordinary, rendered, versioned, indexed page —
  not a half-written one;
* the pre-import text of any page it overwrote is still in `page_versions`, one
  click from being restored (question 5);
* **re-running the same bundle completes it exactly**, with no duplicates,
  because merge-by-slug is idempotent.

### Does 1.0 need staging-and-swap?

**No.** Recorded here as a decision, not an omission.

Staging the whole wiki in a scratch database and swapping the file would buy
atomicity for a failure whose current outcome is already "incomplete and
retryable", never "corrupt or lost". Against that it would cost: a second full
copy of the wiki on disk at import time (a real wiki is ~57MB, and the import is
explicitly streamed one page at a time precisely to avoid holding it all), a
file swap under a running app with live CRDT rooms and cached connections
pointing at the old inode, and the loss of the merge semantics that make the
retry work — a swap replaces, while today's import merges into whatever the user
already has.

Two cheaper things carry the risk instead, and both already hold: the dry run
covers the failure that actually happens, and the retry is safe. The remaining
gap is honesty about it — an import that fails should say what landed and that
re-running is safe. That is a message, not an architecture.

Pinned by `test_a_part_written_import_is_incomplete_but_never_destructive`, so
the "never destructive, always retryable" property cannot quietly stop being
true.

---

## 5. Version history as a safety net

Every content write versions the page, retention keeps the last 50 per page by
default, and the restore genuinely works. Driven end to end through the real
app:

```
--- article page ---
tabs                : ['Article', 'Metadata', 'Details']
links to /details   : True
--- details page ---
history block       : <details class="history" id="history">
open by default     : False
summary             : History (2)
--- version page ---
shows a diff        : True
restore button      : True
restore POST        : 303
text now            : "yesterday's careful list"
versions kept       : 3
```

Restoring is itself a versioned write, so the text you just undid is still
there — the safety net has a safety net.

### Can they get there without knowing the word "version"?

**Getting yesterday's text back is four interactions:** *Details* tab → expand
*History (N)* → *Restore* → confirm. Or *Details* → *History* → click the
timestamp → *Restore this version*, which is the better route because it shows a
diff against the current page first.

The count is fine. The **findability is not**, and this is the honest weakness:

* Nothing on the article page names page history. You have to open a tab called
  *Details* on a hunch.
* The History block is a `<details>`, closed by default, so the count is not
  even visible until you expand it.
* The word "History" *does* appear in the rendered article — as
  `aria-label="History"` on the browser back/forward buttons in the top bar.
  That is actively misleading for someone hunting for undo.
* The Help wiki never mentions versions or history. Only `README.md` does, and
  someone trying to recover a page is not reading the README.

Filed as [#73](https://github.com/VerinFast/jamgraf/issues/73) — it is mostly
labelling, but it is the app's undo, and undo you cannot find is not undo.
`test_yesterdays_text_is_reachable_from_the_article_page` pins the chain of
links as it stands, so it can be improved without silently breaking.

### What #73 changed

The article now says it has a past, in one quiet line under the tabs:

```
--- a page that has been edited ---
Edited 3 minutes ago — 4 earlier versions you can go back to
  -> /wiki/<slug>/details?show=history#history   (the History block, open)

--- a page nobody has edited yet ---
Written 2 days ago — no earlier versions yet      (plain text, no link)
```

Three things carry it, and each is deliberately small:

1. **It says something, not just *something exists*.** A generic "History" link
   is a filing cabinet; "Edited 3 minutes ago" is news, and news about a page you
   just pasted over is what makes you look. The count that follows is of texts
   you can go *back* to — one fewer than the rows in the block, because the
   newest snapshot is the page as it stands.
2. **It only appears when it is true.** A page with a single revision has nothing
   earlier, so it gets a sentence saying exactly that and no link — the same
   convention the MCP `hint` follows, for the same reason: an affordance that
   fires unconditionally is one people learn to skip.
3. **The link opens the block.** A fragment never reaches the server, so the
   article's link carries `?show=history` and `details_view` renders the
   `<details>` already open. Landing on a collapsed block after clicking
   something that promised earlier versions is a dead end for exactly the person
   who needs it.

Getting yesterday's text back is now **two interactions** from the article —
the line, then *Restore* (plus the confirm) — or three if you take the better
route and click the timestamp to read the diff first. The word "history" is on
the article page, in the summary of the block it opens, and in the Help wiki's
*Editing & Formatting* page, which previously never mentioned versions at all.
The affordance's colours come from theme variables, so it is legible on dark
(6.1:1) as well as light; `test_article_controls_take_their_colour_from_a_variable`
covers it alongside the other article controls, and the rest is pinned by
`tests/test_page_history_affordance.py`.

---

## 6. Moving a wiki's files where the OS will not move an open file

*Added after the audit, from the first Windows CI run
([#108](https://github.com/VerinFast/jamgraf/pull/108)).*

Eight tests failed on `windows-latest` with
`PermissionError: [WinError 32] The process cannot access the file because it is
being used by another process` — six in `tests/test_wiki_rename.py`, two in
`tests/test_data_safety.py`. Not a test problem: renaming or deleting a wiki did
not work on Windows.

### Why POSIX hid a real bug for this long

POSIX renames and unlinks a *directory entry*. An open handle keeps working on
the same inode, so a move needs no cooperation from whoever holds one. Windows
refuses outright: SQLite opens a database and its `-wal`/`-shm` without
`FILE_SHARE_DELETE`, so while any handle is open anywhere in the process,
`os.rename` and `os.unlink` on that file fail. There is no way to rename through
SQLite's own handle either.

`change_slug` did call `db.release_wiki_handles` before moving anything. It
closed nothing. `_ApswConn` — the shim over apsw, which is the **shipped**
backend — had no `close()` method at all, so `conn.close()` raised
`AttributeError` straight into a `contextlib.suppress(Exception)`, in
`release_wiki_handles` *and* in `get_conn`'s stale-handle path. Every handle
leaked until the object happened to be garbage collected.

Measured, before the fix, with `lsof` at the moment of the rename: four open
descriptors on `main.db`, `main.db-wal` and `main.db-shm`. POSIX renamed them
anyway, which is exactly why nothing ever reported it.

### What closing them properly needs

Handles are cached per `(thread, wiki)` and a thread cannot reach into another
thread's cache, so "close every handle on this wiki" did not exist as an
operation. Three things were added to `db.py`, which stays the only module that
knows SQLite (rule 2) — `wikis.py` just wraps its file operations in
`db.file_move(slug)`:

* **`close_wiki_handles(wiki)`** closes every live handle in every thread, via a
  weak-referenced registry kept alongside the per-thread cache. Weak, so a
  retired anyio worker thread's connections are not pinned for the life of the
  process — that would be a second leak in place of the first.
* **A refusal, not a force.** A connection inside a managed `transaction()` is
  part-way through a page save, whose four writes land together or not at all
  (rule 6). Closing it from another thread would roll that back with nobody
  told, and the saving thread would fail somewhere far from the cause. So a wiki
  with a save in flight is refused — `db.WikiBusy`, nothing closed and nothing
  moved — and the user tries again a moment later. A handle that will not close
  is reported the same way, naming the thread that owns it. `wikis.py` restates
  it as the `ValueError` its other refusals already use, so both routes put it in
  the *Manage wikis* banner rather than raising a 500 at a packaged app with no
  console; `/wikis/{slug}/delete` caught nothing before this, because before
  this it had nothing to report.
* **A barrier for readers.** Once handles really close, the instant between the
  close and the registry write becomes dangerous: the file is not where the
  registry says it is, and SQLite *creates* an empty database for a path that
  does not exist. A reader arriving then would have an empty wiki manufactured
  at the old address — a rename that silently empties the wiki, the worst
  outcome available here. `db.file_move` holds those readers for the few
  milliseconds the move takes, and they resolve the address again afterwards.
  What they get if the address they asked for has *gone* is the second half, and
  rule 4 decides it: `active_wiki()` falls back to the registry default for a
  slug it cannot find, so carrying on would put that request into whichever wiki
  happens to be default — someone else's content, silently. It is refused
  (`db.WikiMoved`). A caller that named no wiki is not refused; the default is
  what it asked for. A connection opened *just* as a move starts is dropped and
  reopened rather than escaping the snapshot `close_wiki_handles` took, since
  opening a database is far too slow to hold a lock across.

Two orderings turned out to matter, both of them only because the step can now
fail. **A delete removes the files before it deregisters the wiki**: the other
way round leaves a file nobody is tracking, so `create_wiki` hands the same slug
to the next wiki of that name, `get_conn` opens the file still sitting there, and
a wiki the user deleted comes back wearing the new one's label. Files first
leaves at worst a *registered* wiki whose file has gone, which section 2 above
already handles out loud. **A transaction's depth comes down only once its
COMMIT has been issued**, never before: `close_wiki_handles` reads that depth to
decide a save is in flight, and the old ordering left a window where a save that
had written everything and committed nothing looked idle — a move landing there
would have closed the connection between the two and rolled the save back with
nobody told, which is the exact thing the refusal exists to prevent.

A rename can also be refused **part-way**, which is new ground: three files
move one at a time, and a scheduled backup holds a handle on a wiki for the
seconds it takes to copy it (`db.backup_db`), so on Windows a rename landing in
that window is refused after the first file has already gone. The registry still
names the old address, so a file left at the new one is not litter — it is a wiki
with a piece missing, and the next open finds a database with no log or with
somebody else's. The renames now sit inside the same rollback as the registry
write, and a file that cannot be put back is **named** rather than left for the
user to discover. `test_a_rename_refused_part_way_puts_every_file_back` holds it.

(Closing the last connection to a WAL database makes SQLite delete the `-wal`
and `-shm`, so once the handles really close there is usually only the `.db`
left to rename. The partial case is reachable where a handle is deliberately
kept — which is the POSIX branch below.)

On POSIX only this thread's handle is closed, as before. A stale handle in
another thread follows the renamed file, so the write it is carrying arrives in
the right wiki — strictly better than losing it, so it is left alone. A
*deletion* closes them everywhere on both platforms: once the inode has no name
a write through an old handle goes nowhere and nothing reports it.

### The property that does not survive Windows

Accepted risk 7 below — a write in flight when the address changes is not lost —
holds on POSIX and **cannot** hold on Windows. Keeping the handle open is what
makes the write arrive, and keeping it open is what makes the rename impossible.
So on Windows the handle is closed and that write **fails, loudly**, on a closed
connection; the thread that was mid-save finds out. What is still guaranteed
everywhere is the thing that actually mattered: the write never disappears
silently, and it never lands in a different wiki.
`test_a_write_in_flight_when_the_address_changes_is_not_lost` states both
outcomes and asserts the one that is never acceptable on either.

### How this is tested without Windows

A leak POSIX does not care about is one macOS cannot catch, so the suite makes
macOS care. The `as_if_windows` fixture (`tests/conftest.py`) takes the Windows
branch through `db.file_move` **and** patches `os.rename`/`os.unlink` to refuse
any path this process still holds an open descriptor on, matched by
`(st_dev, st_ino)` through `/dev/fd`. It is not an emulator; it enforces the one
property the fix has to have. Restoring the original bug — `_ApswConn.close()`
back to a no-op — turns four tests red on macOS with the same `PermissionError`
Windows reported. `tests/test_wiki_rename.py` also pins the refusal, the
cross-thread close on delete, and the reader barrier, and each was confirmed by
breaking it on purpose.

---

## Accepted risks, in one place

Everything below is a deliberate 1.0 position, not an oversight:

1. **Up to ~1.5–2.5 seconds of live typing** is lost to a power cut or
   `SIGKILL`. A clean quit loses nothing.
2. ~~**A page save is not one transaction.**~~ Fixed in
   [#72](https://github.com/VerinFast/jamgraf/issues/72). What is accepted now is
   narrower: the **RAG index** is rebuilt after the commit, on purpose, so a crash
   in that gap leaves one page's search index stale until its next save (or a
   rebuild from Settings). A divergence that predates the fix heals on the next
   save of that page, not on read.
3. ~~**A corrupt default wiki stops the app from starting.**~~ Fixed in
   [#71](https://github.com/VerinFast/jamgraf/issues/71). What is accepted now is
   narrower: a damaged wiki's **own** pages cannot be shown (there is nothing to
   show), and while it is the active wiki, Settings answers with the explanation
   page instead of the settings form. The app starts, every other wiki works, the
   damaged file is left untouched, and the way back is on screen.
4. **Backups are local only** — same disk, same folder tree as the wikis, and
   only taken on days the app was actually open. They are insurance against
   corruption and mistakes, not against losing the machine.
5. **A failed import leaves a partial wiki.** Nothing is destroyed and a retry
   completes it; there is no staging-and-swap and 1.0 does not add one.
6. ~~**Version history is findable only via the Details tab.**~~ Fixed in
   [#73](https://github.com/VerinFast/jamgraf/issues/73): every article carries a
   line naming when it last changed and how many earlier versions are kept, and
   that line opens the history. Nothing is accepted here any more.
7. ~~**A save committing in the same instant a wiki's address changes can be
   lost.**~~ Not accepted — fixed by changing how the move works. Renaming a
   wiki (`wikis.change_slug`) **renames** its files rather than copying them and
   deleting the originals, and the `-wal`/`-shm` move with the database. The
   difference is what happens to a handle another thread cached before the move:
   under copy-then-delete its next write went into the old file, which was then
   deleted, and the write was gone with nothing anywhere saying so. Under a
   rename that handle is still pointing at the same inode — which is now this
   wiki under its new address — so the write lands where it was always going.
   Nothing is locked and no save pays anything for it.
   `tests/test_wiki_rename.py::test_a_write_in_flight_when_the_address_changes_is_not_lost`
   holds the line, and fails against the copy.
   **On Windows this does not hold, and cannot** (question 6): the OS will not
   rename a file while a handle is open, so the rename closes them and that
   write fails on a closed connection instead of arriving. Accepted there, and
   narrower than it sounds — the write is still never lost *silently* and still
   never lands in another wiki.
