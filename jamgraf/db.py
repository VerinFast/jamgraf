"""SQLite layer: schema, FTS5 (BM25), and the sqlite-vec vector table.

Everything lives in one .db file so the whole wiki — pages, images, full-text
index, and vectors — is portable as a single artifact.

Backend selection: many stock CPython builds ship a `sqlite3` compiled WITHOUT
loadable-extension support, which sqlite-vec needs. So we prefer **apsw** (its
wheels bundle a modern SQLite with extensions enabled) and fall back to the
stdlib module. A tiny shim gives apsw the small slice of the sqlite3 DB-API that
the rest of the app uses (dict rows, `.fetchone/.fetchall`, `.lastrowid`,
`.commit`, `.execute*`), so `store.py`/`rag.py` never know the difference.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Callable, Optional

from . import config

_local = threading.local()
VEC_AVAILABLE = False  # set True once sqlite-vec loads in this process

# The wiki whose database the current request/task operates on. Set per-request
# by the web app's middleware (from the X-Jamgraf-Wiki header or cookie) and set
# explicitly by the MCP server / background flusher. Isolation depends on this.
current_wiki: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "current_wiki", default=None
)
# Wikis this process has set up. NOT the gate for schema creation -- that is
# per-connection (see `get_conn`), because this set is process-wide while the
# connection cache is per (thread, wiki). Its one job now is telling "a wiki
# being opened for the first time" apart from "a wiki whose file has gone".
_schema_ready: set[str] = set()

# Bumped whenever a wiki's file moves (a slug change). Cached handles are
# per (thread, wiki) and no thread can reach into another's cache, so the
# only way to retire them all is to make them *look* stale where they are
# used: `get_conn` compares this against the epoch each handle was opened in.
# Without it, a thread holding a handle from before the move keeps writing
# through it -- the fd follows the renamed file, so those writes land in the
# renamed wiki under the old name, which is the same class of bug as serving
# a file that has been deleted.
#
# **Per wiki, not process-wide.** It used to be one counter for everything, so
# moving one wiki retired every handle in the process -- including one a save on
# an unrelated wiki was holding. A store function that calls `get_conn()` again
# inside its own `transaction()` (`_snapshot` does) would then be handed a
# *different* connection, with its BEGIN still open on the one it left behind:
# half the save on one connection and half on another, or, once closing
# actually worked, no connection at all. A move is a fact about one wiki, so it
# invalidates one wiki.
_cache_epoch: dict[str, int] = {}

# --- Moving a wiki's files ----------------------------------------------------
#
# **Can this OS move or remove a file that is still open?** POSIX renames and
# unlinks a directory *entry*, so an open handle keeps working on the same
# inode and the move needs no cooperation from whoever holds it. Windows
# refuses: a handle opened without `FILE_SHARE_DELETE` — which is how SQLite
# opens a database and its `-wal`/`-shm` — makes `os.rename` and `os.unlink`
# fail with `WinError 32`, "the process cannot access the file because it is
# being used by another process", for every handle in every thread of this
# process. There is no way to rename through SQLite's own handle either.
#
# So on Windows a wiki's files cannot be touched until every handle on them is
# closed, and that is a genuinely different bargain rather than the same code
# with a flag — it is why this is a named property of the OS and not an
# `os.name` test spelled out at each call site. `file_move()` below does as
# little as the platform allows, and `docs/data-safety.md` question 6 records
# what each one costs.
OPEN_FILES_CAN_MOVE = os.name != "nt"

# Every live connection, keyed by wiki, across every thread. The per-thread
# cache above is still the fast path and still the only thing a read touches;
# this is the registry that makes a *cross-thread* close possible at all, which
# is what Windows needs before a file can move. Guarded by `_handles_lock`, and
# nothing holding that lock may call `get_conn` (it wants the same lock).
#
# Weak references, deliberately. The owning thread's cache is what keeps a
# connection alive; anyio worker threads are created and retired constantly, and
# a strong reference here would keep every connection they ever opened — and its
# file handles — alive for the life of the process. That is the leak this was
# meant to fix, not a second one.
_handles: "dict[str, weakref.WeakSet]" = {}
_handles_lock = threading.Lock()

# The wiki whose files are being moved right now, and the thread doing it.
# `get_conn` waits on this: between the close and the registry write there is an
# instant where a wiki's file is not where the registry says it is, and a reader
# arriving then would have SQLite *create* an empty database at the old path —
# a rename that silently empties the wiki, which is the worst outcome available
# here. The moving thread is exempt or it would wait for itself.
_moving: dict[str, int] = {}
_moving_cv = threading.Condition()
_MOVE_WAIT_SECONDS = 10.0
# How many times a connection may lose the race with a move before we say so.
_REOPEN_TRIES = 8


class WikiMoved(RuntimeError):
    """The wiki you asked for changed address while you were waiting for it.

    Refusing is the only safe answer, and rule 4 is why: `active_wiki()` falls
    back to the registry default for a slug it cannot find, so carrying on would
    put this caller's write into whichever wiki happens to be default — someone
    else's content, silently. A caller that waited at the move barrier named a
    specific wiki, so it gets told rather than redirected.
    """

    def __init__(self, wiki: str):
        super().__init__(
            f"the wiki '{wiki}' was renamed or deleted while this request was "
            f"waiting for it. Nothing was read or written. Open it at its new "
            f"address, or pick it again from the list of wikis.")
        self.wiki = wiki


class WikiBusy(RuntimeError):
    """A wiki's files cannot be moved right now, and nothing has been moved.

    Raised instead of forcing a handle shut under a thread that is mid-write.
    A refusal that says why is recoverable; a half-written save is not.
    """

    def __init__(self, wiki: str, reason: str):
        super().__init__(f"{wiki}: {reason}")
        self.wiki = wiki
        self.reason = reason


def active_wiki() -> str:
    """Resolve the active wiki slug, falling back to the registry default."""
    from . import wikis

    w = current_wiki.get()
    if w and wikis.exists(w):
        return w
    d = wikis.default_slug()
    if d is None:
        wikis.ensure_initialized()
        d = wikis.default_slug()
    return d

# --- Backend detection --------------------------------------------------------
try:
    import apsw  # type: ignore
    _HAS_APSW = True
except Exception:  # pragma: no cover
    apsw = None  # type: ignore
    _HAS_APSW = False

import sqlite3  # stdlib fallback (always importable)


# --- "this file cannot be read" ----------------------------------------------
#
# A wiki file can be damaged — a dying disk, a bad sync, a truncated copy — and
# when it is, the app has to stay up and say so. That only works if a damaged
# *file* is told apart from a bug in *our code*: a `KeyError` in store.py is not
# corruption and must stay loud. So the test below is deliberately narrow, and
# everything else is re-raised untouched.

class WikiUnreadable(RuntimeError):
    """SQLite cannot read this wiki's database file.

    Carries the wiki slug and a plain-language reason, because this is shown to
    someone whose family wiki has just stopped opening — not to a log.
    """

    def __init__(self, wiki: str, reason: str, path: Optional[str] = None):
        super().__init__(f"{wiki}: {reason}")
        self.wiki = wiki
        self.reason = reason
        self.path = path


# apsw raises one class per SQLite result code. These are the ones that mean the
# file itself is unusable; apsw.SQLError ("no such table: pages") is ours to fix.
_UNREADABLE_APSW = {"CorruptError", "NotADBError", "IOError", "CantOpenError",
                    "PermissionsError", "ReadOnlyError"}

# The stdlib collapses those onto sqlite3.DatabaseError and its subclasses, so
# the subclasses are only believed when SQLite says what is actually wrong.
_UNREADABLE_PHRASES = ("malformed", "not a database", "file is encrypted",
                       "unable to open database file", "disk i/o error",
                       "database disk image")


def unreadable_reason(exc: BaseException) -> Optional[str]:
    """A plain reason if `exc` is SQLite refusing a file, else None.

    None means "not corruption" — the caller must re-raise, so a programming
    error never arrives dressed up as damaged data.
    """
    if isinstance(exc, WikiUnreadable):
        return exc.reason
    name = type(exc).__name__
    low = str(exc).lower()
    if _HAS_APSW and isinstance(exc, apsw.Error):
        if name not in _UNREADABLE_APSW:
            return None
    elif isinstance(exc, sqlite3.DatabaseError):
        # A bare DatabaseError is what the stdlib raises for a damaged file.
        if (type(exc) is not sqlite3.DatabaseError
                and not any(p in low for p in _UNREADABLE_PHRASES)):
            return None
    else:
        return None
    if name == "NotADBError" or "not a database" in low or "encrypted" in low:
        return ("the file does not look like a SQLite database — its header is "
                "damaged, or it was never a wiki")
    if name == "CorruptError" or "malformed" in low or "database disk image" in low:
        return "SQLite reports the database file as damaged (disk image is malformed)"
    if name in ("CantOpenError", "PermissionsError", "ReadOnlyError"):
        return "the database file could not be opened (permissions, or it moved)"
    if "disk i/o error" in low:
        return "the disk reported an I/O error reading the file"
    return f"SQLite could not read the file ({exc})"


def _as_unreadable(exc: BaseException, wiki: str) -> WikiUnreadable:
    """Translate `exc` into a WikiUnreadable, or re-raise it unchanged."""
    reason = unreadable_reason(exc)
    if reason is None:
        raise exc
    if isinstance(exc, WikiUnreadable):
        return exc
    from . import wikis

    path = str(wikis.db_path(wiki)) if wiki else None
    return WikiUnreadable(wiki or "this wiki", reason, path)


class _Result:
    """Eagerly-materialized dict rows over an apsw cursor, DB-API-ish."""

    def __init__(self, apsw_conn, sql: str, params):
        cur = apsw_conn.cursor()
        cur.execute(sql, tuple(params) if params else ())
        try:
            cols = [d[0] for d in cur.getdescription()]
            self._rows = [dict(zip(cols, row)) for row in cur]
        except Exception:
            self._rows = []  # non-SELECT: no result columns
        self.lastrowid = apsw_conn.last_insert_rowid()

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _ApswConn:
    """Minimal sqlite3-compatible wrapper around an apsw.Connection.

    It also names the wiki on every statement it runs, so damage found *after*
    the file opens — a truncated tail whose header is still intact — surfaces as
    a `WikiUnreadable` for that wiki rather than a bare `apsw.CorruptError`.
    """

    def __init__(self, path: str, wiki: str = ""):
        self.wiki = wiki
        self._c = apsw.Connection(path)
        # busy_timeout FIRST: setting journal_mode needs a brief exclusive lock,
        # so with the timeout still at 0 another connection mid-write turned a
        # normal open into `BusyError: database is locked` — which is contention,
        # not damage, and must not be mistaken for it.
        self._c.cursor().execute("PRAGMA busy_timeout=5000")
        self._c.cursor().execute("PRAGMA journal_mode=WAL")
        self._c.cursor().execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params=()):
        try:
            return _Result(self._c, sql, params)
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc

    def executemany(self, sql: str, seq):
        try:
            self._c.cursor().executemany(sql, [tuple(p) for p in seq])
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc
        return self

    def executescript(self, script: str):
        try:
            self._c.cursor().execute(script)  # apsw runs multi-statement scripts
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc
        return self

    def commit(self):
        """A no-op: transaction control belongs to `transaction()` (see below).

        apsw autocommits every statement outside an explicit transaction, and
        inside one this must *not* end it early — the whole point of the seam is
        that only the outermost `transaction()` commits.
        """

    def close(self):
        """Release the OS handles on the database and its -wal/-shm.

        Not optional, and not only tidiness. This shim is the shipped backend
        and it had no `close` at all, so every `conn.close()` in this module
        raised `AttributeError` into a `suppress(Exception)` and the handle
        stayed open until the object happened to be collected. On POSIX that
        was invisible — a rename or an unlink goes by directory entry, so the
        leak never stopped anything — which is exactly why it survived: on
        Windows the same code could not rename a wiki at all (`WinError 32`,
        the file is in use). See `OPEN_FILES_CAN_MOVE`.
        """
        self._c.close()

    # sqlite3-style extension API, so sqlite_vec.load() works unchanged.
    def enable_load_extension(self, flag: bool):
        self._c.enableloadextension(flag)

    def load_extension(self, path: str):
        self._c.loadextension(path)


class _Sqlite3Conn(sqlite3.Connection):
    """The stdlib fallback, saying the same thing as the apsw shim above.

    The packaged app ships apsw, but a stock CPython without loadable-extension
    support falls back here — and "a corrupt wiki does not stop the app" cannot
    be a property of one backend only.
    """

    wiki = ""

    def execute(self, sql, params=()):
        try:
            return super().execute(sql, params)
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc

    def executemany(self, sql, seq):
        try:
            return super().executemany(sql, seq)
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc

    def executescript(self, script):
        try:
            return super().executescript(script)
        except Exception as exc:
            raise _as_unreadable(exc, self.wiki) from exc

    def commit(self):
        """Match apsw: a no-op inside a managed transaction, harmless outside.

        The connection is opened with ``isolation_level=None``, so the stdlib
        starts no implicit transaction of its own and every statement autocommits
        — exactly what apsw does. That leaves `transaction()` as the *only* thing
        that opens or closes a transaction on either backend, so a stray
        `conn.commit()` deep inside a save cannot commit half of it.
        """
        if _tx(self).depth:
            return
        super().commit()


def _load_sqlite_vec(conn) -> bool:
    global VEC_AVAILABLE
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        VEC_AVAILABLE = True
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        VEC_AVAILABLE = False
        # stderr, never stdout: when this module runs inside the stdio MCP server,
        # stdout IS the JSON-RPC channel and any stray write corrupts the protocol
        # (the client then silently hangs on every later call until restarted).
        print(f"[jamgraf] sqlite-vec unavailable, vector search disabled: {exc}",
              file=sys.stderr)
        return False


def get_conn():
    """Connection for the active wiki, cached per (thread, wiki)."""
    from . import wikis

    wiki = _settled_wiki()
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(wiki)
    if conn is not None and getattr(conn, "epoch", None) != _epoch_of(wiki):
        _shut(conn, wiki)
        conns.pop(wiki, None)
        conn = None
    vanished = False
    if conn is None:
        path = str(wikis.db_path(wiki))
        # SQLite creates an empty database for a path that does not exist, so
        # opening is the last moment we can tell "new wiki, first open" from
        # "the file this process was already using is gone". Worth saying out
        # loud: the pages are not coming back on their own, and the wiki is
        # about to start answering as if it were simply empty.
        vanished = wiki in _schema_ready and not Path(path).exists()
        try:
            if _HAS_APSW:
                conn = _ApswConn(path, wiki)
            else:
                # isolation_level=None: no implicit transaction, so every
                # statement autocommits and an explicit BEGIN is ours to issue —
                # the same contract apsw has. Without it the stdlib opens a
                # transaction of its own before the first DML and `BEGIN
                # IMMEDIATE` fails with "cannot start a transaction within a
                # transaction", so the two backends could not share one seam.
                # `sqlite3.connect`'s default timeout=5.0 already sets a 5s busy
                # timeout before any pragma runs, which is the same ordering the
                # apsw shim spells out above.
                conn = sqlite3.connect(path, check_same_thread=False,
                                       factory=_Sqlite3Conn, isolation_level=None)
                conn.wiki = wiki
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
        except Exception as exc:
            # A damaged file is a WikiUnreadable naming this wiki; anything else
            # is a bug and keeps its own traceback.
            raise _as_unreadable(exc, wiki) from exc
        _load_sqlite_vec(conn)
        conn.epoch = _epoch_of(wiki)
        conn.owner_thread = threading.current_thread().name
        with _moving_cv:
            # Opening a database is file I/O and schema work, far too slow to
            # hold a lock across — so a move can begin while we are in it, and
            # this handle would be registered *after* `close_wiki_handles` took
            # its snapshot. On Windows that is the rename failing again, for a
            # handle nobody could have closed. Registering and checking under
            # the same lock the mover holds closes that: if a move has started,
            # drop this connection and go round again.
            racing = _moving.get(wiki) not in (None, threading.get_ident())
            if not racing:
                conns[wiki] = conn
                with _handles_lock:
                    _handles.setdefault(wiki, weakref.WeakSet()).add(conn)
        if racing:
            # A loop and not a recursive call: the next pass waits out the move
            # at the barrier before it opens anything, so one more move means
            # one more pass rather than one more stack frame.
            _shut(conn, wiki)
            return _reopen(wiki)
    # Ensure the schema once per CONNECTION, not once per process. The cache is
    # per (thread, wiki), so a process-wide memo meant any connection opened
    # after the first one skipped setup entirely -- fine while the file stays
    # put, and silently fatal when it does not. A wiki whose database was
    # replaced or removed underneath a running app came back as an empty file
    # that nothing ever created tables in, so every request reaching a thread
    # without a cached handle died on `no such table: pages` for the life of the
    # process, while threads still holding the old handle served its pages as if
    # nothing had happened. The DDL is `CREATE TABLE IF NOT EXISTS` plus
    # idempotent migrations, so paying it per connection is cheap and correct.
    if not getattr(conn, "schema_ready", False):
        try:
            _ensure_schema(conn)
        except WikiUnreadable:
            # Don't keep a handle on a file we can't use: if the user restores a
            # good copy over it, the next call should open the new one. Closed,
            # not merely forgotten — on Windows a forgotten handle is still a
            # handle, and the restore it is waiting for is a file write.
            _shut(conn, wiki)
            conns.pop(wiki, None)
            raise
        conn.schema_ready = True
        _schema_ready.add(wiki)
        if vanished:
            print(f"[jamgraf] the database for wiki '{wiki}' was gone when it "
                  f"was opened again ({wikis.db_path(wiki)}); it has been "
                  f"re-created empty. Whatever it held is not in it -- restore "
                  f"a backup or re-open a .wiki file.", file=sys.stderr)
    return conn


def _reopen(wiki: str):
    """Ask again for a connection whose open lost a race with a move.

    Bounded, and the bound is the point: each pass blocks at the move barrier
    first, so the only way round again is another move starting in the gap. A
    handful of those is contention; a hundred is something wrong, and saying so
    beats spinning (rule 4 — surface it, never guess).
    """
    for _ in range(_REOPEN_TRIES):
        conn = get_conn()
        if conn is not None:
            return conn
    raise WikiBusy(wiki, f"its files moved {_REOPEN_TRIES} times while this "
                         f"request was trying to open it")


def _epoch_of(wiki: str) -> int:
    return _cache_epoch.get(wiki, 0)


def invalidate_connections(wiki: str) -> None:
    """Retire `wiki`'s cached handles in every thread, at their next use.

    For a wiki whose file has moved. A handle still open in another thread is
    *stale*, not broken: on POSIX it follows the renamed file, so a write
    through it lands in this wiki under its new address. What must not happen is
    a thread going on using it for the *old* name, because that name can be
    registered again — so each thread drops its own the next time it asks, by
    comparing the epoch its handle was opened in against this one.

    One wiki, and the argument is required for that reason: a move is a fact
    about one wiki, and a save in flight on another must not be disturbed by it.
    """
    with _handles_lock:
        _cache_epoch[wiki] = _cache_epoch.get(wiki, 0) + 1


def _shut(conn, wiki: str, checkpoint: bool = False) -> Optional[str]:
    """Close one connection and forget it. Returns a reason if it would not.

    A failure here is reported rather than suppressed. It used to be suppressed,
    and the one thing it was hiding was `_ApswConn` having no `close()` at all
    (see that shim): every handle leaked, which POSIX did not mind and Windows
    could not work around.
    """
    if checkpoint:
        # A property of the file, not of this connection: after TRUNCATE every
        # committed frame is in the `.db` itself, so the `-wal`/`-shm` beside it
        # can be moved or dropped without losing a transaction.
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reason = None
    if not getattr(conn, "wk_closed", False):
        try:
            conn.close()
            conn.wk_closed = True
        except Exception as exc:              # noqa: BLE001 - reported, not raised
            reason = (f"a handle opened on thread "
                      f"{getattr(conn, 'owner_thread', '?')} would not close "
                      f"({type(exc).__name__}: {exc})")
    with _handles_lock:
        live = _handles.get(wiki)
        if live is not None:
            live.discard(conn)
            if not live:
                _handles.pop(wiki, None)
    return reason


def release_wiki_handles(wiki: str) -> None:
    """Checkpoint `wiki`'s WAL and close this thread's handle on it."""
    conns = getattr(_local, "conns", None) or {}
    conn = conns.pop(wiki, None)
    if conn is not None:
        _shut(conn, wiki, checkpoint=True)


def close_wiki_handles(wiki: str) -> int:
    """Close every live handle on `wiki`, in every thread. Returns how many.

    This is what Windows needs before a wiki's files can be renamed or removed
    (`OPEN_FILES_CAN_MOVE`), and it is the one operation the per-thread cache
    cannot do on its own.

    **A save in flight is a refusal, not a casualty.** A connection inside a
    managed `transaction()` is part-way through a page save — the projection,
    the version snapshot, the tags and the canonical Y.Doc land together or not
    at all (rule 6). Closing it under the thread writing it would roll that back
    with nobody told, and the thread's next statement would fail somewhere far
    from the cause. So any wiki with a transaction open is refused, with nothing
    closed and nothing moved; the caller reports it and the user tries again a
    moment later. The same goes for a handle that will not close: it is named
    and the move is abandoned, because a move that half happened is worse than
    one that did not.

    It does **not** wait for a transaction to finish. A save takes
    milliseconds, but `after_commit` work can embed, and the honest answer to
    "a write is happening" is to say so now rather than hold the UI for it.
    """
    with _handles_lock:
        live = list(_handles.get(wiki, ()))
    busy = [c for c in live if getattr(getattr(c, "_wk_tx", None), "depth", 0) > 0]
    if busy:
        # The reason says what is true, not what the caller was going to do:
        # this closes handles and it has closed none, so what did not happen is
        # the caller's sentence to write (`wikis.change_slug`, `delete_wiki`).
        plural = "a save is" if len(busy) == 1 else f"{len(busy)} saves are"
        raise WikiBusy(wiki, f"{plural} in flight on it "
                             f"(thread {busy[0].owner_thread})")
    try:
        refused = [r for r in (_shut(c, wiki, checkpoint=True) for c in live)
                   if r]
    finally:
        # Every handle that got closed must not be found in its owner's cache
        # and used, and that holds whether or not a later one refused: a
        # half-done close with a matching epoch would hand some thread a closed
        # connection. The epoch is what each thread checks, so it moves here,
        # on the way out, either way.
        invalidate_connections(wiki)
    if refused:
        raise WikiBusy(wiki, "; ".join(refused))
    return len(live)


def handles_elsewhere_note() -> str:
    """Why a move can still be refused with every handle of ours closed.

    `_handles` is this process's. The MCP server is a **separate** process with
    a connection cache of its own, and on Windows one open handle anywhere is
    enough to refuse a rename or an unlink — so closing all of ours is necessary
    and not always sufficient. There is no IPC to ask it to let go, so the
    honest thing is to name the likely holder in the refusal rather than let the
    user wonder which of their own programs is at fault.
    """
    if OPEN_FILES_CAN_MOVE:
        return ""
    return (" An agent connected over MCP, or another copy of Jamgraf, keeps "
            "its own connection to this wiki and has to let go of it first.")


def _await_move(wiki: str) -> bool:
    """Block while `wiki`'s files are moving. True if we actually waited.

    The thread doing the move is exempt — it has to be able to read the registry
    and checkpoint — so this cannot deadlock on itself. A move is a handful of
    `os.rename` calls; a wait that reaches the timeout means something is wrong
    rather than slow, and rule 4 says surface it instead of guessing.
    """
    def clear() -> bool:
        return _moving.get(wiki) in (None, threading.get_ident())

    with _moving_cv:
        if clear():
            return False
        if not _moving_cv.wait_for(clear, _MOVE_WAIT_SECONDS):
            raise WikiBusy(wiki, f"its files have been moving for more than "
                                 f"{_MOVE_WAIT_SECONDS:.0f}s")
        return True


def _settled_wiki() -> str:
    """The active wiki, once its files are not in the middle of moving.

    Two things can be true after a wait, and neither may be glossed over. The
    wiki may now live at a different address — so the name is resolved *again*,
    because SQLite creates an empty database for a path that is not there and
    opening the old name would quietly manufacture an empty wiki. Or the slug
    the caller explicitly asked for may be gone, renamed or deleted — and then
    `active_wiki()`'s fallback to the registry default would put this caller's
    write into someone else's wiki, which rule 4 says is the bug and refusing is
    the fix (`WikiMoved`). A caller that named no wiki is not refused: the
    default is what it asked for in the first place.
    """
    asked = current_wiki.get()
    wiki = active_wiki()
    if not _await_move(wiki):
        return wiki
    if asked and not _wikis().exists(asked):
        raise WikiMoved(asked)
    return active_wiki()


def _wikis():
    from . import wikis

    return wikis


def _rekey_handles(old: str, new: str) -> None:
    """Follow a renamed wiki's still-open handles to its new slug.

    On POSIX a handle another thread cached before the move is left open on
    purpose: it points at the same inode, so the write it is carrying arrives in
    this wiki under its new address. But it was *registered* under the old slug,
    and the registry is what `close_wiki_handles` reads — so deleting that wiki
    later would have looked under the new slug, found nothing, and left the
    handle open on a file about to be unlinked. On POSIX that write then goes
    into an inode with no name and is gone with nothing said; on Windows the
    unlink is refused by a handle nobody could find. Either way the fix is to
    file them where they now live.
    """
    with _handles_lock:
        live = _handles.pop(old, None)
        if live:
            _handles.setdefault(new, weakref.WeakSet()).update(live)


@contextlib.contextmanager
def file_move(wiki: str, *, vanishing: bool = False, becomes: Optional[str] = None):
    """Hold `wiki`'s database still so its files can be renamed or removed.

    Everything `db` knows about moving a wiki's files is in here, so `wikis.py`
    renames and deletes without knowing that SQLite — or Windows — has an
    opinion. Three things happen, and only the second depends on the platform:

    * **Readers are held off** for the duration. The window matters: between the
      close and the registry write, this wiki's file is not where the registry
      says it is, and `get_conn` would have SQLite create an empty database at
      the old path. Those callers wait a few milliseconds instead and then
      re-resolve the address.
    * **Handles are closed.** This thread's always, so the `-wal` is
      checkpointed and the three files move as one consistent set. Every other
      thread's when the files are `vanishing`, or where the OS requires it: on
      POSIX a handle left open across a *rename* follows the renamed file, so
      the write it is carrying arrives in the right wiki — strictly better than
      losing it, so we leave it alone. Across a *removal* there is nothing good
      left to say about it: the inode has no name, so the write goes nowhere and
      nothing reports it, which is why `vanishing` closes them everywhere. On
      Windows neither is on offer: rename and unlink both fail outright while a
      handle is open.
    * **Cached handles are retired** on the way out, whichever branch ran, so no
      thread goes on using the old name through a handle from before the move.

    `becomes` is the slug this wiki will answer to afterwards. Handles left open
    across the move are re-filed under it on success, so a later delete can
    still find them (`_rekey_handles`).

    Raises `WikiBusy` without touching anything if a save is in flight.
    """
    with _moving_cv:
        if wiki in _moving:
            raise WikiBusy(wiki, "its files are already being moved")
        _moving[wiki] = threading.get_ident()
    try:
        if vanishing or not OPEN_FILES_CAN_MOVE:
            close_wiki_handles(wiki)
        else:
            release_wiki_handles(wiki)
        yield
    except BaseException:
        raise                      # the files are back; the keys never changed
    else:
        if becomes:
            _rekey_handles(wiki, becomes)
    finally:
        invalidate_connections(wiki)
        if becomes:
            invalidate_connections(becomes)
        with _moving_cv:
            _moving.pop(wiki, None)
            _moving_cv.notify_all()


# --- Transactions -------------------------------------------------------------
#
# One page save writes several rows — the `pages` projection, a `page_versions`
# snapshot, the page's tags, and the canonical Y.Doc in `page_ydoc`. Rule 6 says
# the Y.Doc is the source of truth and the markdown is a projection of it, which
# only holds if the two land together. They did not: apsw (the shipped backend)
# autocommits every statement and its `commit()` is a documented no-op, so a
# crash between statements left the projection durable and the canonical doc a
# revision behind — with the projection winning every read (issue #72,
# `docs/data-safety.md` question 1).
#
# `transaction()` is the seam that makes one save one transaction. Four things
# are load-bearing:
#
# * **One code path, both backends.** apsw autocommits unless a `BEGIN` is
#   issued; the stdlib connection is opened `isolation_level=None` so it behaves
#   the same way. Transaction control then lives here and nowhere else, on either
#   backend, and `conn.commit()` is a no-op inside a managed transaction so a
#   stray call deep in a save cannot commit half of it.
# * **`BEGIN IMMEDIATE`, never deferred.** A deferred transaction takes a read
#   lock first and asks for the write lock only at the first write, and that
#   upgrade can fail outright with SQLITE_BUSY — a busy *timeout* does not help,
#   because the reader cannot be made to wait without risking deadlock. This app
#   has a human, an MCP agent and the collab flusher writing the same file at
#   once, so a save that begins by reading (`get_page`, the slug uniqueness loop)
#   and then writes is exactly that case. IMMEDIATE takes the write lock up
#   front, where the 5s busy timeout applies and writers simply queue.
# * **Re-entrant, through savepoints.** Store functions call each other
#   (`update_page` → `_set_body` → `_sync_ydoc`; the import paths wrap a whole
#   page write), so a nested `transaction()` must not commit the outer one. The
#   outermost owns BEGIN/COMMIT/ROLLBACK; a nested one is a SAVEPOINT, which
#   rolls back its own work and leaves the enclosing transaction intact.
# * **Derived work runs after the commit, never inside it.** `rag.reindex_page`
#   chunks, embeds (possibly loading a model) and writes vectors; holding the
#   write lock across that would queue every other writer behind an embedding
#   run. The RAG index is a cache rebuildable from the markdown, so it does not
#   belong in the atomic unit — `after_commit()` defers it to just past the
#   outermost COMMIT, whatever depth the caller happens to be at.


class _Tx:
    """Transaction state for one connection (connections are per thread+wiki)."""

    __slots__ = ("depth", "pending")

    def __init__(self) -> None:
        self.depth = 0
        self.pending: list = []


def _tx(conn) -> _Tx:
    state = getattr(conn, "_wk_tx", None)
    if state is None:
        state = _Tx()
        conn._wk_tx = state
    return state


def in_transaction() -> bool:
    """Whether the active wiki's connection is inside a managed transaction."""
    return _tx(get_conn()).depth > 0


def after_commit(fn: Callable, *args, **kwargs) -> None:
    """Run `fn` once the outermost transaction commits — immediately if there is
    none, and not at all if the transaction rolls back.

    This is where *derived* state goes: a search index that can be rebuilt from
    the content is not part of the write's atomic unit, and must not hold the
    write lock while it embeds. Deferring it here rather than calling it after
    the `with` block keeps that true even when the caller is nested inside a
    larger transaction.
    """
    conn = get_conn()
    state = _tx(conn)
    if state.depth == 0:
        fn(*args, **kwargs)
        return
    state.pending.append((fn, args, kwargs))


@contextlib.contextmanager
def transaction():
    """All-or-nothing write scope for the active wiki. Re-entrant.

    Yields the connection, so `with db.transaction() as conn:` replaces a
    `conn = db.get_conn()` line rather than adding one.
    """
    conn = get_conn()
    state = _tx(conn)
    outer = state.depth == 0
    name = "" if outer else f"wk_sp_{state.depth}"
    mark = len(state.pending)
    conn.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {name}")
    state.depth += 1
    # The depth comes back down only once the COMMIT or ROLLBACK has actually
    # been issued, never before. It used to drop first, which left a window
    # where this connection looked idle while its transaction was still open —
    # and `close_wiki_handles` reads exactly this to decide whether a save is in
    # flight. A move landing in that window would have closed the connection
    # between the last statement and its COMMIT, rolling the save back with
    # nobody told: the one thing that refusal exists to prevent.
    try:
        yield conn
    except BaseException:
        del state.pending[mark:]        # dropped with the work they belonged to
        try:
            if outer:
                conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
        except Exception:
            pass   # a failing rollback must not mask what actually went wrong
        finally:
            state.depth -= 1
        raise
    if not outer:
        try:
            conn.execute(f"RELEASE {name}")
        finally:
            state.depth -= 1
        return
    try:
        conn.execute("COMMIT")
    except BaseException:
        # A commit that fails (a full disk, a lock lost) has landed nothing, so
        # the deferred work has nothing to be derived from: drop it rather than
        # let it fire on somebody else's commit.
        state.pending.clear()
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        state.depth -= 1
        raise
    state.depth -= 1
    pending, state.pending = state.pending, []
    for fn, args, kwargs in pending:
        fn(*args, **kwargs)


# --- Schema -------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    markdown   TEXT NOT NULL DEFAULT '',
    html       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT,                      -- NULL = active; timestamp = in trash
    starred    INTEGER NOT NULL DEFAULT 0, -- 1 = starred (favorite)
    parent_id  INTEGER REFERENCES pages(id) ON DELETE SET NULL  -- child page if set
);

-- Canonical CRDT state: the authoritative encoded Y.Doc for a page.
-- The Y.Doc (not `pages.markdown`) is the source of truth; `pages.markdown`/
-- `html` are a projection maintained alongside it for FTS, rendering and RAG.
-- `ydoc_state` is a full `pycrdt` update (Yjs v1 binary); `spec_version` is the
-- wiki-interchange spec version that produced it. See `jamgraf/ydoc.py`.
CREATE TABLE IF NOT EXISTS page_ydoc (
    page_id      INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    ydoc_state   BLOB NOT NULL,
    spec_version INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Lightweight history: one row per save. Not CRDT, but gives undo/audit.
CREATE TABLE IF NOT EXISTS page_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    markdown   TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT 'human',   -- 'human' | 'ai' | custom
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    author     TEXT NOT NULL DEFAULT 'human',
    body       TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Proposed edits held for review (propose-vs-apply).
CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    author     TEXT NOT NULL DEFAULT 'ai',
    note       TEXT NOT NULL DEFAULT '',
    markdown   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | applied | rejected
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS page_tags (
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (page_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_page_tags_tag ON page_tags(tag);

-- `meta_schema` is the optional declaration of what the frontmatter of pages
-- made from this template should look like (see `jamgraf/metaschema.py`): a few
-- lines of `name[*]: type`, authored as text so what a human wrote round-trips
-- verbatim. Empty (the default) means the template constrains nothing, which is
-- how every template behaved before it existed.
CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    markdown    TEXT NOT NULL DEFAULT '',
    meta_schema TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activity (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL DEFAULT (datetime('now')),
    actor  TEXT NOT NULL,   -- human | ai
    action TEXT NOT NULL    -- read | write
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts);

CREATE TABLE IF NOT EXISTS custom_elements (
    slug   TEXT PRIMARY KEY,       -- invoked from ```<slug> fenced blocks
    name   TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '[]',   -- JSON [{"name","required","label"}]
    html   TEXT NOT NULL DEFAULT '',     -- shadow-DOM template
    css    TEXT NOT NULL DEFAULT '',     -- scoped CSS
    js     TEXT NOT NULL DEFAULT ''      -- encapsulated JS (root, props, host)
);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    mimetype   TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Page-level full-text index for the search box (BM25 via FTS5).
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, markdown,
    content='pages', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, markdown) VALUES (new.id, new.title, new.markdown);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown) VALUES ('delete', old.id, old.title, old.markdown);
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown) VALUES ('delete', old.id, old.title, old.markdown);
    INSERT INTO pages_fts(rowid, title, markdown) VALUES (new.id, new.title, new.markdown);
END;

-- Chunk-level tables power hybrid RAG (BM25 + vector).
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def _ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    # Migrations: add columns to pre-existing wiki DBs (idempotent).
    for stmt in (
        "ALTER TABLE pages ADD COLUMN deleted_at TEXT",
        "ALTER TABLE pages ADD COLUMN starred INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pages ADD COLUMN parent_id INTEGER REFERENCES pages(id)",
        "ALTER TABLE pages ADD COLUMN sort_order INTEGER",
        # Existing wikis get the column empty: no template declares a schema, so
        # no page's metadata changes meaning. The migration is a no-op by design.
        "ALTER TABLE templates ADD COLUMN meta_schema TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(stmt)
        except WikiUnreadable:
            raise          # a damaged file is not "that column already exists"
        except Exception:
            pass  # already present
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )
    for name, markdown in config.DEFAULT_TEMPLATES:
        conn.execute(
            "INSERT OR IGNORE INTO templates(name, markdown) VALUES (?, ?)",
            (name, markdown),
        )
    for slug, name, fields, html, css, js in config.DEFAULT_ELEMENTS:
        conn.execute(
            "INSERT OR IGNORE INTO custom_elements(slug, name, fields, html, css, js) "
            "VALUES (?, ?, ?, ?, ?, ?)", (slug, name, fields, html, css, js),
        )
    conn.commit()


def init_db() -> None:
    """Ensure the wiki registry exists and the active/default wiki is schema-ready.

    A wiki whose file cannot be read is **reported, not raised**. This runs
    inside the FastAPI lifespan, so raising here meant the app did not start at
    all: one damaged `main.db` took down a UI through which every other wiki —
    each a separate, perfectly healthy file — would have been reachable, and the
    restore instructions live inside that UI (issue #71). Isolation has to hold
    for failure, not only for content.

    Nothing is repaired, moved or deleted: the damaged file is the user's data,
    and it may still be recoverable.
    """
    from . import wikis

    wikis.ensure_initialized()
    try:
        get_conn()  # lazily runs _ensure_schema for the active wiki
    except WikiUnreadable as exc:
        # stderr, not stdout — see _load_sqlite_vec. The app says this properly
        # in the browser; a packaged, Finder-launched app has no console.
        print(f"[jamgraf] wiki '{exc.wiki}' is unreadable: {exc.reason}. "
              f"Starting anyway — other wikis are unaffected.", file=sys.stderr)


def backup_db(src_path: str, dest_path: str) -> None:
    """Write a consistent single-file copy of a SQLite DB (safe while it's in use).
    Used for Save/Export and Open/Import of wikis."""
    if _HAS_APSW:
        src = apsw.Connection(src_path)
        dest = apsw.Connection(dest_path)
        try:
            with dest.backup("main", src, "main") as b:
                b.step()  # copy all pages
        finally:
            dest.close()
            src.close()
    else:  # pragma: no cover
        s = sqlite3.connect(src_path)
        d = sqlite3.connect(dest_path)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()


def is_wiki_db(path: str) -> bool:
    """True if `path` looks like a Jamgraf wiki file (has our core tables)."""
    try:
        conn = apsw.Connection(path) if _HAS_APSW else sqlite3.connect(path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return {"pages", "settings"} <= tables
    except Exception:
        return False


def ensure_vec_table(dim: int) -> None:
    """Create (or recreate on dimension change) the sqlite-vec vector table.

    vec0 tables fix their dimension at creation, so if the active embedder
    changes to one with a different dim we drop and rebuild — callers then
    re-embed. `chunk_id` links a vector row back to the chunks table.
    """
    if not VEC_AVAILABLE:
        return
    conn = get_conn()
    current = get_setting("vec_dim")
    if current is not None and int(current) != dim:
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.execute("DROP TABLE IF EXISTS vec_chunks_sub")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
    )
    # Child pages' vectors live in a separate index so the main index (used by
    # RAG) stays small — faster insertion and search. `parent_id` partitions it.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_sub USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, parent_id INTEGER, embedding FLOAT[{dim}])"
    )
    set_setting("vec_dim", str(dim))
    conn.commit()


# --- Settings helpers ---------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def all_settings() -> dict[str, str]:
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
