"""Renaming a wiki: the label and the address are two different operations.

A wiki has a display **name** and a **slug**. The name is what a person reads.
The slug is the wiki's address: it is in every URL, it is the filename of its
database, it keys each CRDT room, and it is what an agent's `switch_wiki`
pointer holds. So changing the label is free and changing the address moves a
file and invalidates everything pointing at the old one — which is why they are
separate calls rather than one "rename" that guesses.

The interesting tests here are not that the name changes. They are the four
things that hold a slug and would otherwise keep holding the old one.
"""
from __future__ import annotations

import threading

import anyio
import pytest
from pycrdt import Doc, Text

from jamgraf import collab, config, db, store, wikis


class _StandInRoom:
    """A room with a Y.Doc and none of a `YRoom`'s machinery."""

    def __init__(self) -> None:
        self.ydoc = Doc()
        self.ydoc.get("content", type=Text)


class _StandInServer:
    """Enough of `WebsocketServer` for the bookkeeping under test.

    Deliberately not the real one. A `YRoom` holds a pycrdt Subscription, which
    is bound to the thread that made it — the reason `collab.py` persists by
    snapshot-diff rather than a change observer — so driving real rooms by hand
    from a test and then starting the app in the same process aborts the
    interpreter when one is touched or collected on the portal thread. What
    these tests are about is which rooms `collab` saves, forgets and refuses to
    write, and that logic never looks inside the room.
    """

    def __init__(self) -> None:
        self.rooms: dict = {}

    async def get_room(self, name: str):
        return self.rooms.setdefault(name, _StandInRoom())

    async def delete_room(self, *, name=None, room=None) -> None:
        self.rooms.pop(name, None)

    async def __aenter__(self):          # the app's lifespan starts the server
        return self

    async def __aexit__(self, *exc) -> None:
        return None


_BOOKS = ("_last_text", "_last_saved", "_stable_since", "_claude_seen")


@pytest.fixture(autouse=True)
def stand_in_rooms(monkeypatch):
    """Point `collab` at the stand-in server, from clean bookkeeping.

    Autouse, and not only for the tests that drive rooms: changing a wiki's
    address releases that wiki's rooms, so the *route* tests reach into
    `collab.server` too. Its rooms and the `_seeded` index are module-level, so
    a test inheriting them from another file would have this route touch a real
    `YRoom` built on that file's thread — which aborts the interpreter rather
    than failing. Clean state in, clean state out.
    """
    monkeypatch.setattr(collab, "server", _StandInServer())
    monkeypatch.setattr(collab, "_seeded", set())
    for book in _BOOKS:
        monkeypatch.setattr(collab, book, {})
    yield


def _seed(slug: str, title: str, text: str) -> None:
    tok = db.current_wiki.set(slug)
    try:
        store.create_page(title, text)
    finally:
        db.current_wiki.reset(tok)


# --- The label ----------------------------------------------------------------

def test_the_display_name_changes_without_touching_the_address(wiki):
    """The case that prompted this: imported as `startupos`, reads StartupOS."""
    slug = wikis.create_wiki("startupos")
    _seed(slug, "Roadmap", "content that must not move")

    wikis.rename(slug, "StartupOS")

    assert wikis.name_of(slug) == "StartupOS"
    assert wikis.exists(slug)                       # address untouched
    assert wikis.db_path(slug).exists()             # file untouched
    tok = db.current_wiki.set(slug)
    try:
        assert store.get_page("roadmap")["markdown"] == "content that must not move"
    finally:
        db.current_wiki.reset(tok)


def test_a_wiki_needs_a_name(wiki):
    with pytest.raises(ValueError, match="needs a name"):
        wikis.rename("main", "   ")


# --- The address --------------------------------------------------------------

def test_changing_the_address_moves_the_file_and_keeps_the_content(wiki):
    slug = wikis.create_wiki("Old Name")
    _seed(slug, "Kept", "every word survives the move")
    old_path = wikis.db_path(slug)

    target = wikis.change_slug(slug, "New Address")

    assert target == "new-address"
    assert not wikis.exists("old-name") and wikis.exists("new-address")
    assert not old_path.exists()
    assert wikis.db_path("new-address").exists()
    # The display name is a separate fact and is left alone.
    assert wikis.name_of("new-address") == "Old Name"
    tok = db.current_wiki.set("new-address")
    try:
        assert store.get_page("kept")["markdown"] == "every word survives the move"
    finally:
        db.current_wiki.reset(tok)


def test_the_kahala_link_rides_along(wiki):
    """The link records a *remote* name, which a local rename does not touch."""
    slug = wikis.create_wiki("Linked")
    wikis.set_link(slug, "https://kahala.example", "startupos")

    wikis.change_slug(slug, "renamed")

    assert wikis.get_link("renamed") == {"base_url": "https://kahala.example",
                                         "remote": "startupos"}


def test_the_default_wiki_pointer_follows_its_wiki(wiki):
    assert wikis.default_slug() == "main"
    wikis.change_slug("main", "primary")
    assert wikis.default_slug() == "primary"


@pytest.mark.parametrize("bad, why", [
    ("   ", "letter or digit"),
    ("!!!", "letter or digit"),
])
def test_an_address_that_is_not_an_address_is_refused(wiki, bad, why):
    with pytest.raises(ValueError, match=why):
        wikis.change_slug("main", bad)


def test_an_address_another_wiki_answers_to_is_refused(wiki):
    with pytest.raises(ValueError, match="already another wiki"):
        wikis.change_slug("main", "beaconlight")
    assert wikis.exists("main") and wikis.exists("beaconlight")


def test_the_help_wiki_keeps_its_address(wiki):
    """`ensure_help_wiki` re-creates `help`, so a renamed one comes back twice."""
    wikis.ensure_help_wiki()
    with pytest.raises(ValueError, match="built in"):
        wikis.change_slug(config.HELP_WIKI, "handbook")
    # ...but its label is ordinary.
    assert wikis.rename(config.HELP_WIKI, "Handbook") == "Handbook"


def test_renaming_to_its_own_address_is_a_no_op(wiki):
    _seed("main", "Stay", "unchanged")
    assert wikis.change_slug("main", "main") == "main"
    tok = db.current_wiki.set("main")
    try:
        assert store.get_page("stay")
    finally:
        db.current_wiki.reset(tok)


def test_an_unrelated_file_in_the_way_is_never_overwritten(wiki):
    """A `.db` with no wiki registered to it is still somebody's data."""
    stray = wikis.db_path("occupied")
    stray.write_bytes(b"not ours to clobber")

    with pytest.raises(ValueError, match="already in the wikis folder"):
        wikis.change_slug("main", "occupied")

    assert stray.read_bytes() == b"not ours to clobber"


# --- What was holding the old slug --------------------------------------------

def test_a_handle_from_before_the_move_cannot_write_into_the_renamed_wiki(wiki):
    """The hazard is an address being *reused*, which is a normal thing to do.

    Rename `startupos` out of the way and clone `startupos` again — now a thread
    that cached a handle under that name before the move is holding an open file
    that belongs to the renamed wiki, and its key matches the new wiki's. Every
    write meant for the new wiki would land in the old one's file, invisibly.
    Handles are per (thread, wiki) and no thread can reach into another's cache,
    so the move has to invalidate them where they are used.

    One worker thread throughout (`max_workers=1`), because the point is a cache
    that the renaming thread cannot touch.
    """
    from concurrent.futures import ThreadPoolExecutor

    def write(slug: str, title: str, text: str) -> str:
        tok = db.current_wiki.set(slug)
        try:
            db.get_conn()                        # cache a handle under this name
            store.create_page(title, text)
            return db.active_wiki()
        finally:
            db.current_wiki.reset(tok)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(write, "beaconlight", "Original", "the first wiki").result()

        wikis.change_slug("beaconlight", "beaconlight-archived")
        reused = wikis.create_wiki("Beaconlight")        # the name is free again
        assert reused == "beaconlight"

        pool.submit(write, "beaconlight", "Second", "the new wiki").result()

    tok = db.current_wiki.set("beaconlight")
    try:
        slugs = {p["slug"] for p in store.list_pages()}
    finally:
        db.current_wiki.reset(tok)
    assert slugs == {"second"}, "a stale handle wrote into the renamed wiki"

    tok = db.current_wiki.set("beaconlight-archived")
    try:
        slugs = {p["slug"] for p in store.list_pages()}
    finally:
        db.current_wiki.reset(tok)
    assert slugs == {"original"}, "the renamed wiki was written to under its old name"


def test_an_agent_pointed_at_the_old_address_is_refused_not_redirected(wiki,
                                                                      monkeypatch):
    """Rule 4: inheriting another wiki silently is the bug, refusing is correct.

    `db.active_wiki()` answers with the registry default for a slug it does not
    know, so an agent still holding the old address would have written its
    pages into whatever wiki happens to be default.
    """
    from jamgraf import mcp_server

    slug = wikis.create_wiki("Agent Wiki")
    monkeypatch.setattr(mcp_server, "_ACTIVE", slug)
    assert mcp_server._require_wiki() == slug

    wikis.change_slug(slug, "agent-wiki-renamed")

    with pytest.raises(RuntimeError, match="no longer exists"):
        mcp_server._require_wiki()


def test_the_mcp_tool_renames_the_label(wiki, monkeypatch):
    from jamgraf import mcp_server

    slug = wikis.create_wiki("Tool Wiki")
    monkeypatch.setattr(mcp_server, "_ACTIVE", slug)

    out = mcp_server.rename_wiki(name="Tool Wiki!")

    assert out == {"wiki": slug, "name": "Tool Wiki!"}
    assert wikis.name_of(slug) == "Tool Wiki!"


def test_the_mcp_tool_cannot_change_an_address(wiki, monkeypatch):
    """Deliberate: the editors that must be saved first live in another process.

    The MCP server runs separately from the web app, so the rooms holding what
    somebody is typing right now are not visible from here and cannot be
    released. An address change made from this side would strand that text with
    neither process in a position to notice, so this surface only carries the
    label.
    """
    from jamgraf import mcp_server

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    import inspect

    params = inspect.signature(mcp_server.rename_wiki).parameters
    assert set(params) == {"name"}, (
        "the MCP surface grew an address argument; the rooms that have to be "
        "released first are in the app's process, not this one")
    assert "Manage wikis" in (mcp_server.rename_wiki.__doc__ or ""), \
        "an agent told it cannot do this needs to know where it can be done"


def test_the_mcp_tool_asks_for_something_to_change(wiki, monkeypatch):
    from jamgraf import mcp_server

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    assert "error" in mcp_server.rename_wiki(name="  ")


# --- The live editor ----------------------------------------------------------

def test_a_room_left_behind_never_writes_into_the_default_wiki(
        wiki, stand_in_rooms):
    """A CRDT room outlives the request and saves under the wiki its key names.

    For an address the registry no longer knows, that resolves through
    `db.active_wiki()`'s fallback — straight into the default wiki. A page
    appearing in a wiki nobody was editing is about the worst shape this bug
    could take, so the save paths check first.
    """
    import anyio

    slug = wikis.create_wiki("Roomy")
    _seed(slug, "Live", "saved text")

    async def edit_then_rename():
        await collab.ensure_room(slug, "live")
        key = collab.room_key(slug, "live")
        assert key in collab._seeded
        # Unsaved text in the room, then the wiki's address changes underneath.
        collab._last_saved[key] = "saved text"
        collab._last_text[key] = "text typed but never written"
        wikis.change_slug(slug, "roomy-renamed")
        await collab.flush_all()
        return key

    key = anyio.run(edit_then_rename)

    assert key not in collab._seeded              # dropped, not written
    tok = db.current_wiki.set("main")             # the default wiki
    try:
        assert store.list_pages() == []           # nothing leaked into it
    finally:
        db.current_wiki.reset(tok)


def test_releasing_a_wiki_saves_what_was_typed_first(wiki, stand_in_rooms):
    """The graceful path: flush, then forget. Nothing anyone typed is lost."""
    import anyio

    slug = wikis.create_wiki("Typing")
    _seed(slug, "Draft", "original")

    async def type_then_release():
        room = await collab.ensure_room(slug, "draft")
        text = collab._ytext(room)
        with room.ydoc.transaction():
            del text[0:len(text)]
            text += "typed in the editor, never saved"
        return await collab.release_wiki(slug)

    released = anyio.run(type_then_release)

    assert released == 1
    assert collab.room_key(slug, "draft") not in collab._seeded
    tok = db.current_wiki.set(slug)
    try:
        assert store.get_page("draft")["markdown"] == \
            "typed in the editor, never saved"
    finally:
        db.current_wiki.reset(tok)


# --- Through the routes a person actually clicks ------------------------------

def test_the_rename_routes_work_end_to_end(wiki):
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    slug = wikis.create_wiki("startupos")
    _seed(slug, "Plan", "route test content")

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        c.cookies.set("jamgraf_wiki", slug)
        r = c.post(f"/wikis/{slug}/rename", data={"name": "StartupOS"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert wikis.name_of(slug) == "StartupOS"

        r = c.post(f"/wikis/{slug}/address", data={"new_slug": "startup-os"},
                   follow_redirects=False)
        assert r.status_code == 303, r.status_code

    assert wikis.exists("startup-os") and not wikis.exists(slug)
    # The cookie pointed at the old address, which no longer resolves — a bare
    # POST from that tab would otherwise have landed in the default wiki.
    assert "jamgraf_wiki=startup-os" in r.headers.get("set-cookie", "")
    tok = db.current_wiki.set("startup-os")
    try:
        assert store.get_page("plan")["markdown"] == "route test content"
    finally:
        db.current_wiki.reset(tok)


def test_a_refused_address_comes_back_as_a_message_not_a_500(wiki):
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        r = c.post("/wikis/main/address", data={"new_slug": "beaconlight"},
                   follow_redirects=False)

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert wikis.exists("main")


# --- Refusals must not cost anything ------------------------------------------

def test_a_refused_address_leaves_the_live_editors_alone(wiki, stand_in_rooms):
    """Releasing rooms closes what people have open, so ask before acting.

    The refusals are all knowable up front (`plan_slug_change`), and running
    them after the teardown would throw everyone out of their editors to
    accomplish nothing — the rename was never going to happen.
    """
    import anyio

    slug = wikis.create_wiki("Busy")
    _seed(slug, "Open", "being edited right now")

    async def try_a_doomed_rename():
        await collab.ensure_room(slug, "open")
        key = collab.room_key(slug, "open")
        from fastapi.testclient import TestClient

        from jamgraf.api import app
        with TestClient(app, client=("127.0.0.1", 1)) as c:
            r = await anyio.to_thread.run_sync(
                lambda: c.post(f"/wikis/{slug}/address",
                               data={"new_slug": "beaconlight"},   # taken
                               follow_redirects=False))
        return key, r

    key, r = anyio.run(try_a_doomed_rename)

    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert wikis.exists(slug)                    # nothing renamed...
    assert key in collab._seeded                 # ...and nobody thrown out


def test_a_release_that_cannot_save_refuses_rather_than_dropping_the_text(
        wiki, stand_in_rooms, monkeypatch):
    """A failed save must not be followed by forgetting the room.

    The caller is about to make this wiki's address unreachable. A room dropped
    holding text that never landed is that text gone, while the release reports
    success — so the failure propagates, the rename does not happen, and what
    was typed is still in the editor.
    """
    import anyio

    slug = wikis.create_wiki("Fragile")
    _seed(slug, "Draft", "on disk")

    def explode(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(collab, "_persist", explode)

    async def release_with_a_broken_disk():
        room = await collab.ensure_room(slug, "draft")
        text = collab._ytext(room)
        with room.ydoc.transaction():
            del text[0:len(text)]
            text += "typed but not yet saved"
        return await collab.release_wiki(slug)

    with pytest.raises(ValueError, match="nothing was renamed"):
        anyio.run(release_with_a_broken_disk)

    # The room is still there, still holding the text.
    assert collab.room_key(slug, "draft") in collab._seeded
    assert wikis.exists(slug)


def test_a_write_in_flight_when_the_address_changes_is_not_lost(wiki):
    """A write through a handle from before the move arrives, or says it didn't.

    The whole reason the file is renamed rather than copied. A handle another
    thread cached before the move stays usable until that thread next asks for
    one, so the question is where its next write goes. Copy-then-delete sent it
    into the old file and then deleted that file — the write was gone, and
    nothing anywhere said so. That is the one outcome ruled out here.

    Which of the two acceptable outcomes you get is a property of the OS, not of
    this code, and `db.OPEN_FILES_CAN_MOVE` is where that is written down:

    * **POSIX** renames the directory entry, so the handle still refers to the
      same inode — this wiki under its new address — and the write lands where
      it was always going. Nothing has to be locked and no save pays for it.
    * **Windows** will not rename a file while a handle is open at all, so the
      rename has to close them first (`db.close_wiki_handles`). The write then
      fails, loudly, on a closed connection. That is a real loss of this
      platform's and it is recorded in `docs/data-safety.md` question 6 — but it
      is a *reported* one, and the thread that was mid-save finds out.

    Either way the renamed wiki is intact and nothing was written anywhere else.

    Simulated at the handle, because that is where the race lives: a real one
    needs a save committing in the same millisecond as the rename.
    """
    from concurrent.futures import ThreadPoolExecutor

    _seed("beaconlight", "Existing", "already here")

    with ThreadPoolExecutor(max_workers=1) as pool:
        # The worker caches a handle for the wiki, the way any request does.
        def cache_a_handle():
            db.current_wiki.set("beaconlight")
            return db.get_conn()

        stale = pool.submit(cache_a_handle).result()

        wikis.change_slug("beaconlight", "beaconlight-renamed")

        def write_through(conn):
            conn.execute(
                "INSERT INTO pages(slug, title, markdown, html) "
                "VALUES ('in-flight', 'In Flight', 'written as it moved', '')")
            conn.commit()

        outcome = pool.submit(write_through, stale)
        refused = outcome.exception()

    if db.OPEN_FILES_CAN_MOVE:
        assert refused is None, f"the write was refused on POSIX: {refused!r}"
    else:
        assert refused is not None, ("the handle was still open across a rename "
                                     "this OS does not allow")

    tok = db.current_wiki.set("beaconlight-renamed")
    try:
        assert store.get_page("existing")            # nothing else moved
        arrived = store.get_page("in-flight")
    finally:
        db.current_wiki.reset(tok)
    if db.OPEN_FILES_CAN_MOVE:
        assert arrived["markdown"] == "written as it moved"
    else:
        assert arrived is None       # refused above, so it was never written

    # The one outcome that is never acceptable: it went into some other wiki.
    for other in ("main", "crosslake", "startupos"):
        tok = db.current_wiki.set(other)
        try:
            assert store.get_page("in-flight") is None, f"it landed in {other}"
        finally:
            db.current_wiki.reset(tok)


# --- An OS that will not move an open file ------------------------------------
#
# Windows refuses `os.rename` and `os.unlink` while any handle is open on the
# file (`WinError 32`), so moving a wiki's database means closing handles other
# threads own — which the per-thread cache could not do, and which had no
# coverage because POSIX never needed it. The `as_if_windows` fixture applies
# that rule here: it takes the Windows branch through `db.file_move` *and* makes
# this machine's `os.rename`/`os.unlink` refuse a file this process still holds
# open, so a leaked handle fails the test instead of going unnoticed.


def _handle_on(slug: str):
    """Cache a handle for `slug` on a worker thread, and keep that thread."""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1)

    def cache():
        db.current_wiki.set(slug)
        return db.get_conn()

    return pool, pool.submit(cache).result()


def test_a_rename_closes_the_handles_another_thread_owns(wiki, as_if_windows):
    """The failure this is for: six tests, `WinError 32`, nothing renameable.

    A request thread caches a handle and keeps it — that is the whole point of
    the cache — so by the time anyone renames a wiki there is almost always a
    handle open in a thread the renaming thread cannot reach. On POSIX that is
    fine. Here it is the difference between the feature working and not existing.
    """
    slug = wikis.create_wiki("Beaconlight Two")
    _seed(slug, "Kept", "every word survives the move")
    pool, _held = _handle_on(slug)
    try:
        assert wikis.change_slug(slug, "beaconlight-moved") == "beaconlight-moved"
        assert not wikis.db_path(slug).exists()
        assert wikis.db_path("beaconlight-moved").exists()

        # And the worker is working again: it reopens under the new address
        # rather than carrying a closed handle around.
        def read_back():
            db.current_wiki.set("beaconlight-moved")
            return store.get_page("kept")["markdown"]

        assert pool.submit(read_back).result() == "every word survives the move"
    finally:
        pool.shutdown(wait=True)


def test_a_delete_closes_them_too(wiki, as_if_windows):
    """Unlink is refused for the same reason rename is, and says so."""
    slug = wikis.create_wiki("Disposable")
    _seed(slug, "Gone", "not for long")
    pool, _held = _handle_on(slug)
    try:
        assert wikis.delete_wiki(slug) is True
        for suffix in ("", "-wal", "-shm"):
            assert not (config.WIKIS_DIR / (slug + ".db" + suffix)).exists()
    finally:
        pool.shutdown(wait=True)


def test_a_save_in_flight_refuses_the_rename_rather_than_being_closed_under(
        wiki, as_if_windows):
    """Rule 6's atomic unit outranks the rename, and the refusal says why.

    One page save is one transaction: the projection, the version snapshot, the
    tags and the canonical Y.Doc land together or not at all. Closing that
    connection from another thread to free the file would roll the save back
    with nobody told, and the saving thread's next statement would fail a long
    way from the cause. So the rename is refused, nothing is closed and nothing
    is moved — the user tries again a moment later.
    """
    from concurrent.futures import ThreadPoolExecutor

    _seed("beaconlight", "Existing", "already here")
    holding = threading.Event()
    release = threading.Event()

    def save_slowly():
        db.current_wiki.set("beaconlight")
        with db.transaction() as conn:
            conn.execute("INSERT INTO pages(slug, title, markdown, html) "
                         "VALUES ('slow', 'Slow', 'mid-save', '')")
            holding.set()
            release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(save_slowly)
        assert holding.wait(10)
        try:
            # A ValueError, like every other refusal `change_slug` can make, so
            # the route and the MCP tools put it on screen rather than 500.
            with pytest.raises(ValueError) as caught:
                wikis.change_slug("beaconlight", "beaconlight-renamed")
            said = str(caught.value)
            assert "being written to right now" in said
            assert "Nothing was moved" in said
            assert "a save is in flight" in said     # and why, not just that
            assert isinstance(caught.value.__cause__, db.WikiBusy)
        finally:
            release.set()
        saving.result()

    # Nothing moved, and the save that was in flight is all there.
    assert wikis.exists("beaconlight") and not wikis.exists("beaconlight-renamed")
    assert wikis.db_path("beaconlight").exists()
    tok = db.current_wiki.set("beaconlight")
    try:
        assert store.get_page("slow")["markdown"] == "mid-save"
        assert store.get_page("existing")
    finally:
        db.current_wiki.reset(tok)


def test_a_reader_arriving_mid_move_waits_instead_of_creating_an_empty_wiki(wiki):
    """The window between the close and the registry write, which is the trap.

    For an instant the wiki's file is not where the registry says it is. SQLite
    creates an empty database for a path that does not exist, so a reader
    arriving in that instant would have one manufactured at the old address — a
    rename that silently empties the wiki, which is the worst outcome available
    here. `db.file_move` holds readers off and they resolve the address again
    afterwards.

    Not under `as_if_windows`: the window is the same on both, and this is about
    what a *reader* does, not about what the OS allows.
    """
    slug = wikis.create_wiki("Shifting")
    _seed(slug, "Here", "still here")
    arrived: dict = {}
    started = threading.Event()

    def read_while_it_moves():
        started.set()
        db.current_wiki.set(slug)
        try:
            arrived["pages"] = [p["slug"] for p in store.list_pages()]
        except Exception as exc:                 # noqa: BLE001 - reported
            arrived["error"] = f"{type(exc).__name__}: {exc}"

    reader = threading.Thread(target=read_while_it_moves)
    with db.file_move(slug):
        reader.start()
        assert started.wait(10)
        # It is inside get_conn now, waiting. Move the files under it.
        for suffix in ("-shm", "-wal", ""):
            a = config.WIKIS_DIR / (slug + ".db" + suffix)
            if a.exists():
                a.rename(config.WIKIS_DIR / ("shifted.db" + suffix))
        with wikis._lock:
            reg = wikis._load()
            for w in reg["wikis"]:
                if w["slug"] == slug:
                    w["slug"] = "shifted"
            wikis._save(reg)
        # Still nothing created at the address it is waiting on.
        assert not (config.WIKIS_DIR / (slug + ".db")).exists()
    reader.join(10)

    assert "error" not in arrived, arrived["error"]
    # It re-resolved: the old slug is gone, so it fell back to the default wiki
    # rather than conjuring an empty one of its own at the old address.
    assert not (config.WIKIS_DIR / (slug + ".db")).exists()
    assert arrived["pages"] == []
    tok = db.current_wiki.set("shifted")
    try:
        assert store.get_page("here")["markdown"] == "still here"
    finally:
        db.current_wiki.reset(tok)


def test_a_busy_wiki_comes_back_as_a_message_not_a_500(wiki, as_if_windows):
    """Both file-moving routes have a new refusal, so both have to report it.

    `/address` already turned a `ValueError` into the banner on *Manage wikis*;
    `/delete` had nothing to report before this and so caught nothing, which
    would have made "someone is saving" a traceback in a packaged app with no
    console. Driven through the routes rather than the functions, because
    "nobody reads a 500" is a claim about the routes.

    Under `as_if_windows` because that is where *both* refuse. `/delete` refuses
    on every platform — it is about to unlink the inode, so no handle on it is
    allowed to survive — while `/address` only has to close other threads'
    handles where the OS will not move an open file, and so only refuses there.
    """
    from concurrent.futures import ThreadPoolExecutor

    from urllib.parse import unquote_plus

    from fastapi.testclient import TestClient

    from jamgraf.api import app

    _seed("beaconlight", "Existing", "already here")
    holding = threading.Event()
    release = threading.Event()

    def save_slowly():
        db.current_wiki.set("beaconlight")
        with db.transaction() as conn:
            conn.execute("INSERT INTO pages(slug, title, markdown, html) "
                         "VALUES ('slow', 'Slow', 'mid-save', '')")
            holding.set()
            release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(save_slowly)
        assert holding.wait(10)
        try:
            with TestClient(app, client=("127.0.0.1", 1)) as c:
                for path, data in (("address", {"new_slug": "beaconlight-moved"}),
                                   ("delete", {})):
                    r = c.post(f"/wikis/beaconlight/{path}", data=data,
                               follow_redirects=False)
                    assert r.status_code == 303, (path, r.status_code)
                    where = r.headers["location"]
                    assert "error=" in where, (path, where)
                    said = unquote_plus(where)
                    assert "is being written to right now" in said, (path, said)
                    assert "a save is in flight" in said, (path, said)
        finally:
            release.set()
        saving.result()

    # Both were refused, so the wiki is exactly where it was, with its save.
    assert wikis.exists("beaconlight") and not wikis.exists("beaconlight-moved")
    assert wikis.db_path("beaconlight").exists()
    tok = db.current_wiki.set("beaconlight")
    try:
        assert store.get_page("slow")["markdown"] == "mid-save"
    finally:
        db.current_wiki.reset(tok)


def test_a_rename_refused_part_way_puts_every_file_back(wiki, monkeypatch):
    """The `-shm` moved and the database did not: a wiki with a piece missing.

    Any one of the three renames can be refused — by a scheduled backup holding
    a handle for the seconds it takes to copy the wiki (`db.backup_db`), or by
    anything else the OS is unwilling to move. The registry still names the old
    address, so a file left behind at the new one is not litter: the next open
    would find a database with no log, or somebody else's.

    Refused on the **second** rename on purpose, because a failure on the first
    has nothing to undo and proves nothing. Getting a second file to move at all
    takes a handle held elsewhere — closing the last connection to a WAL
    database makes SQLite delete the `-wal` and `-shm`, so a wiki whose handles
    all closed has only its `.db` left to rename.
    """
    from pathlib import Path as _Path

    slug = wikis.create_wiki("Interrupted")
    _seed(slug, "Kept", "must survive a failed rename")
    pool, _held = _handle_on(slug)            # keeps the -wal/-shm in existence
    try:
        before = sorted(p.name for p in config.WIKIS_DIR.iterdir())
        assert f"{slug}.db-wal" in before, "nothing to interrupt part-way"

        real = _Path.rename
        calls = {"n": 0}

        def flaky(self, target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise PermissionError(32, "The process cannot access the file "
                                          "because it is being used by another "
                                          "process")
            return real(self, target)

        # A nested context, not `monkeypatch.undo()`: `monkeypatch` is one
        # function-scoped object shared with the `wiki` fixture, so undoing
        # everything on it would put `config.DATA_DIR` back to the developer's
        # real data directory half-way through the test.
        with monkeypatch.context() as m:
            m.setattr(_Path, "rename", flaky)
            with pytest.raises(ValueError) as caught:
                wikis.change_slug(slug, "interrupted-moved")

        said = str(caught.value)
        assert "could not be moved" in said
        assert f"/{slug}" in said            # it says where the wiki still is
        assert isinstance(caught.value.__cause__, PermissionError)

        # Everything is back under the name the registry still holds, and
        # nothing is sitting at the address the wiki never got to.
        assert wikis.exists(slug) and not wikis.exists("interrupted-moved")
        assert sorted(p.name for p in config.WIKIS_DIR.iterdir()) == before
        assert not list(config.WIKIS_DIR.glob("interrupted-moved.db*"))

        # And it still opens, with its content.
        tok = db.current_wiki.set(slug)
        try:
            assert store.get_page("kept")["markdown"] == (
                "must survive a failed rename")
        finally:
            db.current_wiki.reset(tok)

        # Renaming again, with nothing in the way, just works.
        assert wikis.change_slug(slug, "interrupted-moved") == "interrupted-moved"
    finally:
        pool.shutdown(wait=True)
