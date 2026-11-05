"""The data-safety properties from the 1.0 audit (issue #68).

Each of these was established by experiment in `docs/data-safety.md` and is
pinned here, because a property nobody tests is a property that will quietly
stop being true. They are deliberately about *durability and recovery*, not
features: the worst outcome this app has is a family wiki losing content.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest
from fastapi.testclient import TestClient

from jamgraf import appconfig, backups, config, db, store, wikis, ydoc
from jamgraf.api import app


# --- Q1: crash / power loss mid-write ----------------------------------------

def test_every_wiki_file_is_wal_with_full_sync(wiki):
    """WAL is the crash-safety story, so prove it per file rather than assume it.

    Checked on a cold read-only handle, not the live connection: WAL is a
    persistent property of the file, and that is what a recovering process sees.
    """
    for w in wikis.list_wikis():
        tok = db.current_wiki.set(w["slug"])
        try:
            db.get_conn()                       # creates the file + schema
            store.create_page("Seed", "words")
        finally:
            db.current_wiki.reset(tok)

    seen = 0
    for path in sorted(config.WIKIS_DIR.glob("*.db")):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", path
            # 2 = FULL. WAL would tolerate NORMAL, but FULL is what the app has
            # always run with and what the crash answer in the doc assumes.
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2, path
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        seen += 1
    assert seen >= 2


@pytest.mark.parametrize("write", ["create", "update", "upsert"])
def test_canonical_ydoc_is_written_before_the_derived_index(wiki, write):
    """A failing reindex must not leave the canonical Y.Doc a revision behind.

    `page_ydoc` is the source of truth; the RAG index is a cache that can be
    rebuilt from the markdown. Writing the cache first meant an embedder that
    wasn't ready (or a missing sqlite-vec, or a model download failing) committed
    the projection and skipped the canonical write. That really happened: see
    question 1 in `docs/data-safety.md`.
    """
    def boom(*a, **k):
        raise RuntimeError("embedder not ready")

    if write != "create":
        store.create_page("Ledger", "version one")

    original = store.rag.reindex_page
    store.rag.reindex_page = boom
    try:
        with pytest.raises(RuntimeError):
            if write == "create":
                store.create_page("Ledger", "version two")
            elif write == "update":
                store.update_page("ledger", "Ledger", "version two")
            else:
                store.upsert_page("Ledger", "version two", slug="ledger")
    finally:
        store.rag.reindex_page = original

    # Every content path, not just the one that happened to be audited: each
    # writes the canonical doc first, so a failing cache leaves truth intact.
    page = store.get_page("ledger")
    assert page["markdown"] == "version two"

    # Assert the blob was PERSISTED, not that a read can reconstruct one.
    # canonical_doc() lazily seeds a doc from the projection when no state is
    # stored, so asserting only on its content passes even when the canonical
    # write never happened — which is precisely the bug this guards.
    assert ydoc._load_state(page["id"]) is not None, (
        "no canonical Y.Doc was persisted; the derived index was written first")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == "version two"


@pytest.mark.parametrize("write", ["create", "update", "upsert", "restore_version"])
def test_canonical_ydoc_matches_the_projection_after_every_write(wiki, write):
    """Rule 6's invariant, checked on each ordinary content path."""
    store.create_page("Recipe", "the original")
    if write == "update":
        store.update_page("recipe", "Recipe", "edited")
    elif write == "upsert":
        store.upsert_page("Recipe", "the original\nplus more", slug="recipe")
    elif write == "restore_version":
        store.update_page("recipe", "Recipe", "edited")
        first = store.page_versions("recipe")[-1]
        store.restore_version("recipe", first["id"])

    page = store.get_page("recipe")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == page["markdown"]


def test_the_canonical_ydoc_blob_is_written_in_one_statement(wiki):
    """No torn Y.Doc: the blob lands whole or not at all, so it always decodes.

    `_store_state` is a single INSERT ... ON CONFLICT, which SQLite makes atomic
    regardless of backend. Guarded because a future refactor that splits it into
    read-modify-write would reintroduce a half-written canonical state.
    """
    import inspect

    source = inspect.getsource(ydoc._store_state)
    assert source.count("conn.execute(") == 1

    store.create_page("Long", "x" * 200_000)
    page = store.get_page("long")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == page["markdown"]



# A page save writes four things — the `pages` projection, the `page_versions`
# snapshot, the tag index and the canonical Y.Doc — and issue #72 was that they
# were four separate autocommitted statements. These are the seams between them,
# named the way the crash table in `docs/data-safety.md` names them.
_SAVE_SEAMS = {
    "after the projection": "_snapshot",
    "after the version row": "_index_meta",
    "before the canonical write": "_sync_ydoc",
}


def _page_row(slug: str = "ledger") -> dict:
    """Everything a save touches, read back for comparison."""
    page = store.get_page(slug)
    return {
        "title": page["title"],
        "markdown": page["markdown"],
        "html": page["html"],
        "updated_at": page["updated_at"],
        "versions": len(store.page_versions(slug)),
        "tags": store.tags_of(slug),
        "canonical": ydoc.content_of(ydoc.canonical_doc(page)),
        "state": ydoc._load_state(page["id"]),
    }


@pytest.mark.parametrize("seam", list(_SAVE_SEAMS))
def test_a_save_that_fails_part_way_leaves_the_page_exactly_as_it_was(
        wiki, monkeypatch, seam):
    """One page save is one transaction (issue #72): all of it, or none of it.

    Failure is injected at each seam between the writes, which is what a crash
    between them looks like from the database's point of view — the statements
    before it either committed on their own or they didn't. Before this was one
    transaction, breaking the *last* seam still left `pages.markdown` holding the
    new text with the canonical Y.Doc a revision behind, and the projection is
    what every read path returns. Rule 6 says the Y.Doc is the truth; that is
    only true if the two land together.
    """
    store.create_page("Ledger", "version one\n\nfirst draft")
    store.set_tags("ledger", ["ledgers"])
    before = _page_row()

    def boom(*a, **k):
        raise RuntimeError(f"crash {seam}")

    monkeypatch.setattr(store, _SAVE_SEAMS[seam], boom)
    with pytest.raises(RuntimeError):
        store.update_page("ledger", "Ledger Renamed", "version two")

    assert _page_row() == before, f"the save tore at the seam {seam!r}"
    # The FTS projection is rebuilt by trigger from `pages`, so a rolled-back
    # save must not leave the new title findable either.
    assert not [p for p in store.list_pages() if p["title"] == "Ledger Renamed"]


def test_a_failed_create_leaves_no_page_and_no_orphan_rows(wiki, monkeypatch):
    """The same rule for a brand-new page: no half-created page, no debris.

    A page row that committed on its own while its canonical Y.Doc never did is
    a page whose source of truth is missing — the interchange round-trip would
    ship an empty document for it.
    """
    def boom(*a, **k):
        raise RuntimeError("crash before the canonical write")

    monkeypatch.setattr(store, "_sync_ydoc", boom)
    with pytest.raises(RuntimeError):
        store.create_page("Nana's Pie", "the only copy")

    conn = db.get_conn()
    assert store.get_page("nanas-pie") is None
    assert conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM page_versions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM page_ydoc").fetchone()["n"] == 0


def test_a_real_kill_between_the_writes_lands_nothing(wiki):
    """Not a simulated failure: a separate process is killed mid-save.

    `os._exit` inside the call that would write the canonical Y.Doc — the second
    row of the crash table in `docs/data-safety.md`, which used to leave
    `pages.markdown` at `v2`, a `v2` version row, and the canonical doc at `v1`.
    The database is then read back on a cold handle, which is what a recovering
    process sees: an uncommitted transaction leaves no commit frame in the WAL,
    so none of it is there.
    """
    store.create_page("Ledger", "version one")
    page = store.get_page("ledger")

    script = textwrap.dedent("""
        import os
        from jamgraf import db, store
        db.current_wiki.set("main")
        store._sync_ydoc = lambda *a, **k: os._exit(9)   # die mid-save
        store.update_page("ledger", "Ledger", "version two")
        os._exit(0)                                      # never reached
    """)
    env = dict(os.environ, JAMGRAF_DATA=str(config.DATA_DIR))
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(config.ROOT),
                          env=env, capture_output=True, timeout=120)
    assert proc.returncode == 9, (proc.returncode, proc.stderr.decode()[-2000:])

    cold = sqlite3.connect(f"file:{config.WIKIS_DIR / 'main.db'}?mode=ro", uri=True)
    cold.row_factory = sqlite3.Row
    try:
        assert cold.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = cold.execute("SELECT markdown FROM pages WHERE slug='ledger'").fetchone()
        assert row["markdown"] == "version one"
        versions = cold.execute(
            "SELECT COUNT(*) FROM page_versions WHERE page_id=?", (page["id"],)
        ).fetchone()[0]
        assert versions == 1
        blob = cold.execute("SELECT ydoc_state FROM page_ydoc WHERE page_id=?",
                            (page["id"],)).fetchone()["ydoc_state"]
    finally:
        cold.close()

    from pycrdt import Doc

    doc = Doc()
    doc.apply_update(bytes(blob))
    assert ydoc.content_of(doc) == "version one"


def test_the_derived_index_is_rebuilt_outside_the_write_transaction(wiki, monkeypatch):
    """The RAG index is a cache, and it must never hold the write lock.

    `rag.reindex_page` chunks, embeds — which can load a model — and writes
    vectors. Inside the save's transaction that would queue the human, the MCP
    agent and the collab flusher behind an embedding run, and invite lock
    timeouts. It is rebuildable from the markdown, so it is not part of the
    atomic unit: it runs *after* the outermost commit, at whatever depth the
    caller happens to be.
    """
    inside = []
    real = store.rag.reindex_page

    def spy(page_id, markdown):
        inside.append(db.in_transaction())
        return real(page_id, markdown)

    monkeypatch.setattr(store.rag, "reindex_page", spy)
    store.create_page("Ledger", "version one")
    store.update_page("ledger", "Ledger", "version two")
    assert inside == [False, False]

    inside.clear()
    with db.transaction():
        store.update_page("ledger", "Ledger", "version three")
        assert inside == []          # deferred past the enclosing commit...
    assert inside == [False]         # ...and still not inside a transaction


def test_a_nested_failure_rolls_back_only_its_own_work(wiki):
    """`transaction()` is re-entrant, so a save can be wrapped in a bigger one.

    The import paths do exactly that (a page's projection and the *sender's*
    canonical doc commit together). A nested scope is a SAVEPOINT: it rolls back
    its own work and leaves the enclosing transaction intact, instead of
    committing it early or aborting it.
    """
    store.create_page("Ledger", "version one")

    with db.transaction():
        store.update_page("ledger", "Ledger", "version two")
        with pytest.raises(RuntimeError):
            with db.transaction():
                store.create_page("Scratch", "never mind")
                raise RuntimeError("boom")

    assert store.get_page("ledger")["markdown"] == "version two"
    assert store.get_page("scratch") is None
    conn = db.get_conn()
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE page_id NOT IN (SELECT id FROM pages)"
    ).fetchone()["n"]
    assert orphans == 0          # the rolled-back page never got indexed either


def test_a_save_is_one_transaction_on_the_stdlib_backend_too(wiki, monkeypatch):
    """apsw ships with the app; a stock CPython falls back to `sqlite3`.

    The two reach a transaction differently — apsw autocommits unless a `BEGIN`
    is issued, the stdlib has its own implicit-transaction behaviour driven by
    `isolation_level` — so "a page save is atomic" cannot be a property of one
    backend only.
    """
    monkeypatch.setattr(db, "_HAS_APSW", False)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_schema_ready", set())
    # sqlite-vec won't load on this backend; record the current value so the flag
    # it flips doesn't leak into the next test.
    monkeypatch.setattr(db, "VEC_AVAILABLE", db.VEC_AVAILABLE)

    store.create_page("Ledger", "version one")
    assert type(db.get_conn()) is db._Sqlite3Conn
    before = _page_row()

    def boom(*a, **k):
        raise RuntimeError("crash before the canonical write")

    monkeypatch.setattr(store, "_sync_ydoc", boom)
    with pytest.raises(RuntimeError):
        store.update_page("ledger", "Ledger", "version two")
    assert _page_row() == before


def test_two_writers_at_once_neither_deadlock_nor_lose_a_save(wiki):
    """A human, an agent and the collab flusher all write the same file.

    Taking the write lock up front (`BEGIN IMMEDIATE`) rather than upgrading a
    read lock mid-transaction is what keeps that safe: the second writer queues
    on the busy timeout instead of failing the upgrade. Two threads, each with
    its own connection, exactly as the collab flusher gets (it persists through
    `anyio.to_thread.run_sync`).
    """
    store.create_page("Ledger", "version one")
    errors: list = []

    def hammer(tag: str) -> None:
        try:
            for i in range(6):
                store.update_page("ledger", "Ledger", f"{tag} {i}")
        except Exception as exc:                      # reported, not swallowed
            errors.append(f"{tag}: {exc!r}")

    threads = [threading.Thread(target=hammer, args=(t,))
               for t in ("human", "agent")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert not [t for t in threads if t.is_alive()]
    page = store.get_page("ledger")
    assert len(store.page_versions("ledger")) == 13     # 1 create + 12 saves
    # Whoever wrote last, the page is one of the writes — never a mix of two —
    # and its canonical doc agrees with it.
    assert page["markdown"] in {f"{t} {i}" for t in ("human", "agent")
                                for i in range(6)}
    assert ydoc.content_of(ydoc.canonical_doc(page)) == page["markdown"]


# --- Q2: a corrupted wiki file ------------------------------------------------

def _corrupt(slug: str) -> None:
    """Scribble over a wiki file the way a bad sync or a dying disk would.

    Every cached connection is dropped first: a live SQLite handle keeps its own
    view of the file (and would checkpoint the WAL back over the damage), so
    corrupting underneath one tests nothing. This is the cold-file case — the
    state the app finds on next launch.
    """
    db._local = threading.local()
    db._schema_ready.clear()
    gc.collect()                                  # closes the apsw handles

    path = config.WIKIS_DIR / (slug + ".db")
    for suffix in ("-wal", "-shm"):
        extra = config.WIKIS_DIR / (slug + ".db" + suffix)
        if extra.exists():
            extra.unlink()
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.write(b"NotSQLite format 3\x00")   # header gone
        handle.truncate(size // 3)                # ...and the tail with it


def test_a_corrupt_wiki_does_not_take_its_neighbours_with_it(wiki):
    """Physical separation is the isolation guarantee — including in failure."""
    for slug, text in (("main", "main text"), ("beaconlight", "beacon text")):
        tok = db.current_wiki.set(slug)
        try:
            store.create_page("Home", text)
        finally:
            db.current_wiki.reset(tok)

    _corrupt("beaconlight")

    tok = db.current_wiki.set("beaconlight")
    try:
        with pytest.raises(Exception):
            store.get_page("home")
    finally:
        db.current_wiki.reset(tok)

    tok = db.current_wiki.set("main")
    try:
        assert store.get_page("home")["markdown"] == "main text"
        assert store.list_pages()
    finally:
        db.current_wiki.reset(tok)


def _seed(slug: str, text: str) -> None:
    tok = db.current_wiki.set(slug)
    try:
        store.create_page("Home", text)
    finally:
        db.current_wiki.reset(tok)


def test_the_app_starts_with_a_corrupt_default_wiki(wiki):
    """One unreadable file must never stop the app — issue #71.

    `db.init_db()` opened the default wiki inside the FastAPI lifespan, so a
    damaged `main.db` raised there and startup failed: a healthy 215-page wiki
    sitting next to it became unreachable through a UI that would not come up.
    """
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")
    assert wikis.default_slug() == "main"        # the broken one is the default

    with TestClient(app, client=("127.0.0.1", 12345)) as client:  # used to raise
        resp = client.get("/")
        # 503, not 500: this wiki really is unavailable, and unlike a bare
        # "Internal Server Error" the body says which one and what to do.
        assert resp.status_code == 503, resp.status_code
        body = resp.text
        # It says which wiki, and does not pretend the wiki is fine.
        assert "Main" in body
        assert "can’t be read" in body or "can't be read" in body
        # ...and points at the way out: the other wikis, and a backup.
        assert "/wikis" in body
        assert "backup" in body.lower()


def test_other_wikis_stay_usable_when_the_default_is_corrupt(wiki):
    """Isolation is a failure property too, not only a content one."""
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        page = client.get("/wiki/home?wiki=beaconlight")
        assert page.status_code == 200
        assert "beacon text" in page.text
        found = client.get("/search?q=beacon&wiki=beaconlight")
        assert found.status_code == 200
        assert "Home" in found.text
        made = client.post("/api/pages", json={"title": "Still Writable",
                                               "markdown": "yes"},
                           headers={"X-Jamgraf-Wiki": "beaconlight"})
        assert made.status_code == 200, made.text


def test_manage_wikis_lists_what_it_can_when_a_wiki_is_corrupt(wiki):
    """/wikis is the recovery page. It must not be the page that dies."""
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        resp = client.get("/wikis")             # used to 500 (wikis.stats raised)
        assert resp.status_code == 200, resp.text
        assert "Beaconlight" in resp.text
        assert "1 article" in resp.text         # the healthy wiki still counted
        assert "can’t be read" in resp.text or "can't be read" in resp.text

        # ...and from a healthy wiki too, once the cookie has moved on.
        resp = client.get("/wikis?wiki=beaconlight")
        assert resp.status_code == 200


def test_a_corrupt_wiki_file_is_left_exactly_as_it_was(wiki):
    """A corrupt database is the user's data in a damaged state.

    It may still be recoverable — by a later Jamgraf, by `sqlite3 .recover`, by
    a specialist. Nothing here may delete it, truncate it, overwrite it or
    "repair" it in place.
    """
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")
    path = config.WIKIS_DIR / "main.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.get("/")
        client.get("/wikis")
        client.get("/wiki/home")
        client.get("/search?q=text")

    assert path.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_bug_is_never_mistaken_for_a_corrupt_file(wiki):
    """The narrowness is the point: only SQLite refusing a *file* counts.

    Catch-and-continue that also swallows a `KeyError` in our own code turns
    every programming error into a friendly "restore from a backup" page.
    """
    assert db.unreadable_reason(KeyError("slug")) is None
    assert db.unreadable_reason(ValueError("nope")) is None
    assert db.unreadable_reason(
        sqlite3.OperationalError("no such table: pages")) is None
    assert db.unreadable_reason(
        sqlite3.DatabaseError("database disk image is malformed"))
    assert db.unreadable_reason(sqlite3.DatabaseError("file is not a database"))
    if db._HAS_APSW:
        import apsw
        assert db.unreadable_reason(apsw.CorruptError("malformed"))
        assert db.unreadable_reason(apsw.NotADBError("not a database"))
        assert db.unreadable_reason(apsw.SQLError("no such table: pages")) is None


def test_wiki_health_reports_the_broken_one_and_only_that_one(wiki):
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")

    assert wikis.health("main")["ok"] is False
    assert wikis.health("main")["reason"]
    assert wikis.health("beaconlight")["ok"] is True

    broken = wikis.stats("main")                 # used to raise
    assert broken["unreadable"] is True
    assert broken["articles"] == 0
    assert wikis.stats("beaconlight")["articles"] == 1


def test_damage_past_the_header_is_caught_too(wiki):
    """The other shape: a file that opens fine and falls apart while being read.

    `_corrupt` scribbles the header, so SQLite refuses at open. A half-copied or
    truncated file keeps a valid header and only fails on the pages that are
    gone — which is why both connection shims classify per statement, not just
    at open.
    """
    tok = db.current_wiki.set("main")
    try:
        for i in range(200):
            store.create_page(f"Page {i}", "body " * 200)
    finally:
        db.current_wiki.reset(tok)

    db._local = threading.local()
    db._schema_ready.clear()
    gc.collect()
    path = config.WIKIS_DIR / "main.db"
    for suffix in ("-wal", "-shm"):
        extra = config.WIKIS_DIR / ("main.db" + suffix)
        if extra.exists():
            extra.unlink()
    with open(path, "r+b") as handle:
        handle.truncate(path.stat().st_size // 2)   # header intact, tail gone

    assert wikis.health("main")["ok"] is False
    assert wikis.stats("main")["unreadable"] is True
    tok = db.current_wiki.set("main")
    try:
        with pytest.raises(db.WikiUnreadable):
            store.list_pages()
    finally:
        db.current_wiki.reset(tok)

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/wikis").status_code == 200


def test_an_agent_is_told_a_wiki_is_unreadable_rather_than_switched_into_it(
        wiki, monkeypatch):
    """Rule 5 — one code path for Human and LLM — holds in failure too.

    Switching into a wiki that cannot be opened would make every later tool call
    fail obscurely, one at a time. Say it once, at the point of the decision, and
    name the other wikis that do work.
    """
    from jamgraf import mcp_server

    monkeypatch.setattr(mcp_server, "_ACTIVE", None)
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")

    refused = mcp_server.switch_wiki("main")
    assert "can’t be read" in refused["error"] or "can't be read" in refused["error"]
    assert refused.get("active") is None
    assert "beaconlight" in refused["wikis"]
    assert mcp_server.current_wiki()["active"] is None      # it did not move

    assert mcp_server.switch_wiki("beaconlight")["active"] == "beaconlight"


def test_a_corrupt_wiki_is_recognised_on_the_stdlib_backend_too(wiki, monkeypatch):
    """apsw ships with the app; a stock CPython falls back to `sqlite3`.

    The two raise different exception types for the same damaged file
    (`apsw.NotADBError` vs `sqlite3.DatabaseError`), so "one bad file does not
    take the app down" cannot be a property of one backend only.
    """
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")
    monkeypatch.setattr(db, "_HAS_APSW", False)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_schema_ready", set())
    # sqlite-vec won't load on this backend; monkeypatch records the current
    # value so the flag it flips doesn't leak into the next test.
    monkeypatch.setattr(db, "VEC_AVAILABLE", db.VEC_AVAILABLE)

    assert wikis.health("main")["ok"] is False
    assert wikis.stats("main")["unreadable"] is True

    tok = db.current_wiki.set("main")
    try:
        with pytest.raises(db.WikiUnreadable):
            store.list_pages()
    finally:
        db.current_wiki.reset(tok)

    tok = db.current_wiki.set("beaconlight")     # the neighbour still opens
    try:
        assert store.get_page("home")["markdown"] == "beacon text"
    finally:
        db.current_wiki.reset(tok)


def test_a_corrupt_wiki_does_not_stop_the_others_being_backed_up(wiki):
    """The backup is the restore path. One bad file must not cancel it.

    `run_backup` wrote every wiki into one directory and removed the whole
    directory on any failure — so a corrupt wiki meant no snapshot at all, for
    any wiki, on the day you most needed one.
    """
    _seed("main", "main text")
    _seed("beaconlight", "beacon text")
    _corrupt("main")

    res = backups.run_backup()
    assert res["ok"] is True, res
    assert "beaconlight" in res["wikis"]
    assert "main" in res.get("skipped", [])
    snapshot = backups._root() / res["name"]
    assert (snapshot / "beaconlight.db").exists()
    assert not (snapshot / "main.db").exists()   # never a broken file that looks good


def test_a_torn_registry_write_cannot_lose_a_wiki(wiki):
    """`wikis.json` is replaced by rename, so a failed save keeps the old file.

    Before this, a truncated registry parsed as "no wikis" and every wiki the
    user had created themselves vanished from the app.
    """
    slug = wikis.create_wiki("Family Recipes")
    tok = db.current_wiki.set(slug)
    try:
        store.create_page("Nana's Pie", "the only copy")
    finally:
        db.current_wiki.reset(tok)
    before = (config.DATA_DIR / "wikis.json").read_text()

    real_replace = wikis.os.replace

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    wikis.os.replace = fail
    try:
        with pytest.raises(OSError):
            wikis.create_wiki("Doomed")
    finally:
        wikis.os.replace = real_replace

    raw = (config.DATA_DIR / "wikis.json").read_text()
    assert raw == before
    assert json.loads(raw)                       # still parses
    assert wikis.exists(slug)
    assert not any(w["name"] == "Doomed" for w in wikis.list_wikis())
    # ...and no debris left beside it that a later reader could pick up
    assert not list(config.DATA_DIR.glob("wikis.json.tmp*"))


def test_a_torn_app_config_write_cannot_turn_backups_off(wiki):
    """`app_config.json` holds whether backups run at all — same rename rule."""
    appconfig.set("backup_keep", 4)
    before = (config.DATA_DIR / "app_config.json").read_text()

    real_replace = appconfig.os.replace

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    appconfig.os.replace = fail
    try:
        with pytest.raises(OSError):
            appconfig.set("backup_enabled", False)
    finally:
        appconfig.os.replace = real_replace

    assert (config.DATA_DIR / "app_config.json").read_text() == before
    assert backups.enabled() is True
    assert backups.keep() == 4
    assert not list(config.DATA_DIR.glob("app_config.json.tmp*"))


# --- Q3: restore --------------------------------------------------------------

def test_backups_are_on_by_default_and_land_where_the_docs_say(wiki):
    """Settings tells people to restore from `<data>/backups` — hold that true."""
    assert backups.enabled() is True             # nothing configured yet
    assert backups.interval_hours() == backups.DEFAULT_INTERVAL_HOURS
    assert backups.keep() == backups.DEFAULT_KEEP

    slug = wikis.create_wiki("Family Recipes")
    tok = db.current_wiki.set(slug)
    try:
        store.create_page("Nana's Pie", "the only copy of this recipe")
    finally:
        db.current_wiki.reset(tok)

    res = backups.run_backup()
    assert res["ok"] and slug in res["wikis"]     # every wiki, not just the default
    snapshot = config.DATA_DIR / "backups" / res["name"] / f"{slug}.db"
    assert snapshot.exists()


def test_a_backup_snapshot_carries_images_and_history_too(wiki):
    """"A snapshot is a complete copy" is a claim the Settings pane makes."""
    store.create_page("Nana's Pie", "the only copy")
    store.update_page("nanas-pie", "Nana's Pie", "the only copy, revised")
    image_id = store.save_image("pie.png", "image/png", b"\x89PNG-not-really")

    res = backups.run_backup()
    restored = wikis.import_from(
        str(config.DATA_DIR / "backups" / res["name"] / "main.db"), name="Restored")

    tok = db.current_wiki.set(restored)
    try:
        page = store.get_page("nanas-pie")
        assert page["markdown"] == "the only copy, revised"
        assert len(store.page_versions("nanas-pie")) == 2   # yesterday's text too
        assert store.get_image(image_id)["data"] == b"\x89PNG-not-really"
    finally:
        db.current_wiki.reset(tok)


def test_restoring_a_backup_leaves_the_broken_wiki_alone(wiki):
    """Open-as-a-new-wiki, never overwrite: you can compare before you commit."""
    store.create_page("Home", "good text")
    res = backups.run_backup()
    store.update_page("home", "Home", "text I regret")

    restored = wikis.import_from(
        str(config.DATA_DIR / "backups" / res["name"] / "main.db"), name="Restored")
    assert restored != "main"

    tok = db.current_wiki.set(restored)
    try:
        assert store.get_page("home")["markdown"] == "good text"
    finally:
        db.current_wiki.reset(tok)
    assert store.get_page("home")["markdown"] == "text I regret"


# --- Q4: import paths ---------------------------------------------------------

def test_a_part_written_import_is_incomplete_but_never_destructive(wiki):
    """The accepted risk, pinned: a failed local write leaves a *retryable* wiki.

    `_read_bundle` already makes a bad payload all-or-nothing. A failure of the
    local writes is not staged, so the import stops part-way — but nothing is
    deleted, every page that landed is a normal versioned page, and re-running
    the same bundle finishes the job. See question 4 in `docs/data-safety.md`.
    """
    for i in range(1, 7):
        store.create_page(f"Chapter {i}", f"chapter {i} body")
    bundle = store.export_wiki_bundle(label="Source")

    target = wikis.create_wiki("Target")
    tok = db.current_wiki.set(target)
    try:
        store.create_page("Chapter 1", "a local draft I already had", slug="chapter-1")

        real_create = store.create_page
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
            return real_create(*a, **k)

        store.create_page = flaky
        try:
            with pytest.raises(OSError):
                store.import_wiki_bundle(bundle)
        finally:
            store.create_page = real_create

        partial = {p["slug"] for p in store.list_pages(include_children=True)}
        assert partial                                   # something landed
        assert len(partial) < 6                          # but not everything
        # the page that existed before the import kept its history
        assert any(v["markdown"] == "a local draft I already had"
                   for v in [store.get_version(v["id"])
                             for v in store.page_versions("chapter-1")])

        res = store.import_wiki_bundle(bundle)           # retry finishes it
        assert res["pages"] == 6
        final = {p["slug"] for p in store.list_pages(include_children=True)}
        assert final == {f"chapter-{i}" for i in range(1, 7)}
        for slug in final:
            assert store.get_page(slug)["markdown"].strip()
    finally:
        db.current_wiki.reset(tok)


# --- Q5: version history as a safety net --------------------------------------

def test_yesterdays_text_is_reachable_from_the_article_page(wiki):
    """The only undo a non-technical user has: article -> Details -> History.

    Pinned as a chain of links a person can actually follow, because a template
    tidy-up that dropped the Details tab would remove the app's undo without
    breaking a single other test.
    """
    store.create_page("Packing List", "yesterday's careful list")
    store.update_page("packing-list", "Packing List", "oops, I deleted everything")

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.cookies.set("jamgraf_wiki", "main")

        article = client.get("/wiki/packing-list")
        assert article.status_code == 200
        assert '/wiki/packing-list/details' in article.text     # click 1

        details = client.get("/wiki/packing-list/details")
        assert details.status_code == 200
        assert 'id="history"' in details.text                   # click 2 opens it
        assert "History (2)" in details.text

        old = store.page_versions("packing-list")[-1]
        assert f'/wiki/packing-list/history/{old["id"]}' in details.text

        view = client.get(f"/wiki/packing-list/history/{old['id']}")
        assert view.status_code == 200
        assert "yesterday's careful list" in view.text
        assert "Changes since this version" in view.text        # a diff, not a guess

        client.post(f"/wiki/packing-list/history/{old['id']}/restore",
                    follow_redirects=False)

    assert store.get_page("packing-list")["markdown"] == "yesterday's careful list"
    # restoring is itself a versioned write, so the regretted text is still there
    assert len(store.page_versions("packing-list")) == 3


# --- A wiki whose file goes missing under a running app ----------------------
#
# Not corruption (Q2) and not a crash (Q1): the file is simply *gone* while the
# process is up. That happened in the field — a wiki's pages rendered fine for
# minutes and then every editor click returned 500 `no such table: pages`, and
# the file on disk was 4096 bytes with no tables in it.
#
# The cause was a seam, not the disappearance: schema setup was memoised in a
# process-wide `_schema_ready`, while connections are cached per (thread, wiki).
# The first thread had already marked the wiki ready, so when another thread
# opened the now-missing path — SQLite happily creates an empty database for one
# — setup was skipped and that connection had no tables, for the rest of the
# process's life. Threads still holding the old handle kept serving the old
# content, which is why it looked like "viewing works, editing is broken".

def test_a_wiki_whose_file_vanishes_is_usable_again_not_permanently_broken(
        wiki, as_if_windows):
    """Every connection sets up its own schema, whatever the process remembers.

    The file is closed before it is made to vanish, and `as_if_windows` is what
    insists on it. A file can only *vanish* out from under a running app where
    the OS allows an open file to be unlinked; on Windows it cannot, so a wiki's
    file goes missing between runs or because the app let go of it, and staging
    it any other way would be testing a thing that cannot happen. Closing first
    is also what the app itself has to do now (`db.close_wiki_handles`), so this
    stages the same way the product does.
    """
    _seed("main", "text that will not survive its file")
    assert db._schema_ready                      # the memo that used to gate this

    db.close_wiki_handles("main")
    for suffix in ("", "-wal", "-shm"):
        p = config.WIKIS_DIR / ("main.db" + suffix)
        p.unlink(missing_ok=True)

    result: dict = {}

    def another_worker():                        # a request on a different thread
        tok = db.current_wiki.set("main")
        try:
            store.create_page("After", "written into the re-created file")
            result["pages"] = [p["slug"] for p in store.list_pages()]
        except Exception as exc:                 # noqa: BLE001 - reported, not raised
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.current_wiki.reset(tok)

    t = threading.Thread(target=another_worker)
    t.start()
    t.join()

    assert "error" not in result, result["error"]
    assert result["pages"] == ["after"]


def test_the_wiki_that_lost_its_file_says_so(capsys, wiki, as_if_windows):
    """Re-creating it empty is recovery, but it is not a silent one.

    The pages are not coming back on their own, and the wiki is about to answer
    as though it had simply always been empty — which reads as "nothing here"
    rather than "something went missing". Opening the file is the only moment
    the two can be told apart, so that is where it is said.

    Closed before it vanishes, for the reason the test above gives. That also
    replaces a `conns.pop` that forgot the handle without closing it — which is
    precisely the bug this fix is about, left in a test.
    """
    _seed("main", "text that will not survive its file")
    db.close_wiki_handles("main")                # closed, not merely forgotten
    (config.WIKIS_DIR / "main.db").unlink()

    tok = db.current_wiki.set("main")
    try:
        db.get_conn()
    finally:
        db.current_wiki.reset(tok)

    warned = capsys.readouterr().err
    assert "main" in warned and "re-created empty" in warned


def test_a_brand_new_wiki_is_not_reported_as_having_lost_anything(wiki):
    """The report has to distinguish a first open from a disappearance."""
    slug = wikis.create_wiki("Fresh")
    tok = db.current_wiki.set(slug)
    try:
        db.get_conn()                            # first ever open: creates the file
    finally:
        db.current_wiki.reset(tok)
    # Nothing was lost, so nothing is claimed to be.
    assert slug in db._schema_ready
