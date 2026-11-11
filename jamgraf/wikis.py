"""Wiki registry — the list of isolated wikis and which is the default.

Each wiki is a separate SQLite file (``data/wikis/<slug>.db``). Physical
separation is what guarantees the isolation the app promises: a page in one
wiki can never link to or surface a page in another, because every lookup,
search, and ``[[wiki link]]`` resolves against a single wiki's database.

The registry itself is a small JSON file (``data/wikis.json``) read fresh on
each call so the web app and the (separate-process) MCP server always agree.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path

from . import config, db, render, structure

_lock = threading.Lock()


def _registry_path() -> Path:
    return config.DATA_DIR / "wikis.json"


def _wikis_dir() -> Path:
    config.WIKIS_DIR.mkdir(parents=True, exist_ok=True)
    return config.WIKIS_DIR


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (name or "").strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "wiki"


def _load() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"wikis": [], "default": None}


def _save(reg: dict) -> None:
    """Replace the registry atomically.

    A plain ``write_text`` truncates first, so a crash mid-write leaves a torn
    JSON file — and ``_load`` treats unparseable as "no wikis", which makes every
    wiki the user created themselves disappear from the app while its ``.db``
    sits untouched on disk. Write beside it and rename: on the same filesystem
    that swap is atomic, so a reader sees the old registry or the new one.
    """
    path = _registry_path()
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(reg, indent=2))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)   # never leave a half-written twin behind


def list_wikis() -> list[dict]:
    return _load()["wikis"]


def exists(slug: str) -> bool:
    return any(w["slug"] == slug for w in list_wikis())


def name_of(slug: str) -> str:
    for w in list_wikis():
        if w["slug"] == slug:
            return w["name"]
    return slug


def default_slug() -> str | None:
    reg = _load()
    if reg.get("default") and exists(reg["default"]):
        return reg["default"]
    wl = reg["wikis"]
    return wl[0]["slug"] if wl else None


def db_path(slug: str) -> Path:
    return _wikis_dir() / f"{slug}.db"


def create_wiki(name: str) -> str:
    """Register a new wiki; returns its unique slug. The DB file is created
    lazily on first access (db.get_conn ensures the schema)."""
    base = slugify(name)
    with _lock:
        reg = _load()
        existing = {w["slug"] for w in reg["wikis"]}
        slug, n = base, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        reg["wikis"].append({"slug": slug, "name": name.strip() or slug})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)
    return slug


# --- Renaming: two operations, deliberately ---------------------------------
#
# A wiki has a display **name** and a **slug**, and they are not the same kind
# of fact. The name is a label a person reads; the slug is the wiki's address —
# it is in every URL, it is the filename of its database, it keys the CRDT
# rooms and each agent's active-wiki pointer. Changing the label is free.
# Changing the address moves a file and invalidates things pointing at the old
# one, so they are separate calls and the UI offers them separately: a wiki
# imported as `startupos` that should read *StartupOS* needs the first, not the
# second.


def rename(slug: str, name: str) -> str:
    """Change a wiki's display name. Its address and file are untouched."""
    title = (name or "").strip()
    if not title:
        raise ValueError("A wiki needs a name")
    with _lock:
        reg = _load()
        for w in reg["wikis"]:
            if w["slug"] == slug:
                w["name"] = title
                _save(reg)
                return title
    raise ValueError(f"no wiki '{slug}'")


def plan_slug_change(slug: str, new_slug: str) -> str:
    """Every refusal `change_slug` can make, without changing anything.

    Split out because the caller has to take this wiki's live editors down
    before the file can move (`collab.release_wiki`), and doing that for a
    rename that was never going to happen would close everyone's editor to
    accomplish nothing. Ask first, then act.

    Returns the slug the wiki would end up with — which is the one it already
    has when there is nothing to do.
    """
    if not exists(slug):
        raise ValueError(f"no wiki '{slug}'")
    if slug == config.HELP_WIKI:
        raise ValueError(
            "The Help wiki's address is built in: the app re-creates 'help' "
            "whenever it is missing, so a renamed one would come back as a "
            "second copy beside it. Its display name can be changed.")
    # `slugify` falls back to "wiki" for input with nothing usable in it, which
    # is right when naming a new wiki and wrong here: it would silently move a
    # wiki to /wiki because someone typed punctuation.
    if not re.sub(r"[^\w]", "", new_slug or ""):
        raise ValueError("A wiki address needs at least one letter or digit")
    target = slugify(new_slug)
    if target == slug:
        return slug
    if exists(target):
        raise ValueError(f"'{target}' is already another wiki's address")
    if db_path(target).exists():
        raise ValueError(
            f"{db_path(target).name} is already in the wikis folder without a "
            "wiki registered to it; move it aside first rather than have this "
            "overwrite it")
    return target


def change_slug(slug: str, new_slug: str) -> str:
    """Change a wiki's slug: its URLs, and the name of its database file.

    Returns the slug it now has. The display name and the Kahala link ride
    along unchanged — the link records a *remote* name, which this does not
    touch, so a renamed wiki still pushes and pulls where it always did.

    Four things have to happen together, and the order is the whole job.
    **The handles that have to close are closed first** — always this thread's,
    so the `-wal` is checkpointed rather than left beside a file that no longer
    matches it, and every other thread's where the OS will not move an open
    file. **The file moves before the registry does**, because a registry that
    names a file which is not there yet would have the next reader create an
    empty one in its place (`db.get_conn`) — a rename that silently empties the
    wiki is the worst outcome available here, so a failure to record the move
    puts the file back instead. **Every cached handle is retired** and any left
    open is re-filed under the new slug: they are per (thread, wiki) and a stale
    one follows the renamed file, so writes for the old name would land in the
    renamed wiki. All three are `db.file_move`'s, not this function's. And **the
    caller releases the CRDT rooms first** — see `collab.release_wiki`, which
    cannot be done from here because it is async.

    Refused before anything moves, and before the caller takes the editors
    down: see `plan_slug_change`.

    **Renamed, not copied, and that is what makes it safe.** A handle another
    thread cached before the move stays usable until that thread next asks for
    one, so the question is where its next write lands. Copy-then-delete sent it
    into the old file, which was then deleted — the write was simply gone, with
    nothing anywhere saying so. A rename leaves it writing to the *same inode*,
    which is now this wiki under its new address, so the write arrives where it
    was always meant to go. Nothing has to be locked and no save pays for this.

    The `-wal` and `-shm` move **with** the database, and before it. Renaming
    the database alone and deleting its WAL is what "the disk reported an I/O
    error" looks like: a hot WAL is not ours to delete, and the three files are
    one thing. Sidecars first, database last, so nothing can observe a database
    without its log.
    """
    target = plan_slug_change(slug, new_slug)
    if target == slug:
        return slug
    src, dest = db_path(slug), db_path(target)

    # `db.file_move` owns everything SQLite and the OS have to say about moving
    # these three files: it checkpoints and closes the handles that have to go,
    # holds readers off for the duration so none of them can have an empty
    # database created at the old path, and retires every cached handle on the
    # way out. It refuses with `db.WikiBusy` — nothing closed, nothing moved —
    # if a save is in flight. How many handles it has to close is a property of
    # the platform, not of this function: see `db.OPEN_FILES_CAN_MOVE`.
    moved: list[tuple[Path, Path]] = []
    try:
        with db.file_move(slug, becomes=target):
            try:
                # Sidecars before the database. Nothing can open the new name
                # until the registry names it, below, but the ordering costs
                # nothing and means there is never an instant where a database
                # is visible without its log.
                #
                # The renames are inside the same `try` as the registry write
                # for a reason the OS supplies: any one of them can fail, and on
                # Windows that is a thing that actually happens — a scheduled
                # backup holds a handle on this wiki for the seconds it takes to
                # copy, and a rename landing in that window is refused. Rolling
                # back only the registry would have left a wiki whose `-shm` had
                # moved and whose database had not.
                for suffix in ("-shm", "-wal", ""):
                    a, b = Path(str(src) + suffix), Path(str(dest) + suffix)
                    if a.exists():
                        a.rename(b)
                        moved.append((a, b))
                with _lock:
                    reg = _load()
                    for w in reg["wikis"]:
                        if w["slug"] == slug:
                            w["slug"] = target
                    if reg.get("default") == slug:
                        reg["default"] = target
                    _save(reg)
            except Exception:
                _put_back(moved)           # the registry still names the old one
                raise
    except db.WikiBusy as exc:
        # Every other refusal this function can make is a ValueError the routes
        # and the MCP tools already turn into a sentence on screen; a wiki that
        # is being written to is the same kind of answer — "not now, and here is
        # why" — so it arrives the same way rather than as a 500 nobody reads.
        # Nothing has moved: `file_move` refuses before it closes anything.
        raise ValueError(
            f"'{name_of(slug)}' is being written to right now — {exc.reason}. "
            f"Nothing was moved; try again in a moment.") from exc
    except OSError as exc:
        # The OS refused one of the renames. Everything is back where it was
        # (`_put_back` says so, or raises), so this is a refusal like any other.
        raise ValueError(
            f"'{name_of(slug)}' could not be moved: {exc.strerror or exc}. "
            f"It is still at /{slug} with everything in it; try again in a "
            f"moment.{db.handles_elsewhere_note()}") from exc
    return target


def _put_back(moved: list[tuple[Path, Path]]) -> None:
    """Undo the renames in `moved`, newest first. Raises if any will not go back.

    The registry still names the old address, so a file left at the new one is
    not litter — it is a wiki with a piece missing. If a file cannot be put
    back, say which: that is a state only a person can sort out, and silence
    would leave them with a wiki that opens empty and no idea why.
    """
    stuck = []
    for a, b in reversed(moved):
        try:
            b.rename(a)
        except OSError:
            stuck.append(b.name)
    if stuck:
        raise ValueError(
            f"a rename failed part-way and {', '.join(stuck)} could not be put "
            f"back. Nothing is lost, but this wiki's files are split across two "
            f"names: close Jamgraf and rename them to match before opening it "
            f"again.")


def delete_wiki(slug: str) -> bool:
    """Forget a wiki and remove its three files. Returns False if unregistered.

    Under `db.file_move` for the same reasons a rename is, plus one of its own:
    a handle left open on a file that has been *removed* is not stale but
    harmless the way a renamed one is — on POSIX it keeps writing into an inode
    with no name, so the write is simply gone with nothing anywhere saying so.
    Closing first is what makes the next write an honest error instead.
    Unlinking is also the operation Windows refuses outright while a handle is
    open, which is the same reason stated by the other OS.
    """
    label = name_of(slug)
    try:
        with db.file_move(slug, vanishing=True):
            if not any(w["slug"] == slug for w in _load()["wikis"]):
                return False
            # **Files first, registry second**, and the order is the whole
            # point. Unlinking is the step that can be refused — on Windows
            # while anything holds a handle, anywhere on a read-only volume —
            # and the two orders fail very differently. Deregistering first
            # leaves files nobody is tracking: `create_wiki` would hand the
            # same slug to a new wiki, `get_conn` would open the file still
            # sitting there, and a wiki the user deleted would come back
            # wearing a new name's label. Removing the files first leaves at
            # worst a *registered* wiki whose file has gone, which rule 4
            # already handles out loud — it says so on screen and in the log,
            # and the file is never guessed about.
            for suffix in ("", "-wal", "-shm"):
                f = Path(str(db_path(slug)) + suffix)
                if f.exists():
                    f.unlink()
            with _lock:
                reg = _load()
                reg["wikis"] = [w for w in reg["wikis"] if w["slug"] != slug]
                if reg.get("default") == slug:
                    reg["default"] = (reg["wikis"][0]["slug"]
                                      if reg["wikis"] else None)
                _save(reg)
    except db.WikiBusy as exc:
        raise ValueError(
            f"'{label}' is being written to right now — {exc.reason}. "
            f"Nothing was deleted; try again in a moment.") from exc
    except OSError as exc:
        # Still registered, so it is still a wiki — with however many of its
        # files are left. Say that rather than report a clean delete.
        raise ValueError(
            f"'{label}' could not be deleted: {exc.strerror or exc}. It is "
            f"still listed at /{slug}; close anything using it and try "
            f"again.{db.handles_elsewhere_note()}") from exc
    return True


# --- the Kahala link ---------------------------------------------------------
#
# Which remote wiki a local one is linked to is a fact about *this install*, not
# about the content, so it lives here in the registry and not in the wiki's own
# settings table. That is not tidiness: `export_to` copies the whole .db file and
# `store.export_wiki_bundle` reads from it, so a link recorded in the wiki would
# travel to whoever you shared the wiki with and point their copy at your Kahala.
#
# Only the address and the remote slug live here -- never a credential. The
# refresh token is in the Keychain (see `secretstore`), and nothing in this file
# is secret.


def get_link(slug: str) -> dict | None:
    """The Kahala link for ``slug``, or None when it isn't linked."""
    for w in _load()["wikis"]:
        if w["slug"] == slug:
            link = w.get("kahala")
            return dict(link) if isinstance(link, dict) and link.get("base_url") else None
    return None


def set_link(slug: str, base_url: str, remote: str) -> bool:
    """Link ``slug`` to the wiki ``remote`` on the Kahala at ``base_url``."""
    with _lock:
        reg = _load()
        for w in reg["wikis"]:
            if w["slug"] == slug:
                w["kahala"] = {"base_url": base_url.rstrip("/"), "remote": remote}
                _save(reg)
                return True
    return False


def clear_link(slug: str) -> bool:
    """Forget ``slug``'s link. Content is untouched on both sides."""
    with _lock:
        reg = _load()
        for w in reg["wikis"]:
            if w["slug"] == slug and w.pop("kahala", None) is not None:
                _save(reg)
                return True
    return False


def disk_bytes(slug: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path(slug)) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def health(slug: str) -> dict:
    """Can this wiki's file actually be opened? ``{slug, ok, reason, path}``.

    Probed live rather than remembered: an open wiki costs a dict lookup, a
    damaged one fails fast on its header, and a wiki the user has just restored
    from a backup starts working again without a restart. A wiki that has never
    been opened is created here, exactly as any other read would.
    """
    token = db.current_wiki.set(slug)
    try:
        db.get_conn()
        return {"slug": slug, "ok": True, "reason": None,
                "path": str(db_path(slug))}
    except db.WikiUnreadable as exc:
        return {"slug": slug, "ok": False, "reason": exc.reason,
                "path": exc.path or str(db_path(slug))}
    finally:
        db.current_wiki.reset(token)


def stats(slug: str) -> dict:
    """Summary for a wiki: articles, internal links (resolved vs broken),
    trashed count, and size on disk.

    A wiki whose file cannot be read reports ``unreadable`` with a reason
    instead of raising. *Manage wikis* builds this for every registered wiki, so
    raising here took down the one page a user would go to in order to open a
    backup or move to a working wiki — the page whose whole job is recovery.
    """
    token = db.current_wiki.set(slug)
    try:
        conn = db.get_conn()
        articles = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE deleted_at IS NULL").fetchone()["c"]
        trashed = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE deleted_at IS NOT NULL").fetchone()["c"]
        from . import store  # lazy: avoid import cycle at module load
        idx = store.link_index()  # resolves [[Title]] and [[slug]] alike
        active = conn.execute(
            "SELECT markdown FROM pages WHERE deleted_at IS NULL").fetchall()
        resolved = broken = 0
        for r in active:
            for key in render.extract_wikilinks(r["markdown"]):
                if key in idx:
                    resolved += 1
                else:
                    broken += 1
    except Exception as exc:
        if db.unreadable_reason(exc) is None:
            raise                       # a bug in our own code stays loud
        return {
            "slug": slug,
            "unreadable": True,
            "reason": db.unreadable_reason(exc),
            "path": str(db_path(slug)),
            "articles": 0, "trashed": 0,
            "links": 0, "links_resolved": 0, "links_broken": 0,
            "bytes": disk_bytes(slug),   # the file is still there; say how big
        }
    finally:
        db.current_wiki.reset(token)
    return {
        "slug": slug,
        "unreadable": False,
        "reason": None,
        "articles": articles,
        "trashed": trashed,
        "links": resolved + broken,
        "links_resolved": resolved,
        "links_broken": broken,
        "bytes": disk_bytes(slug),
    }


def export_to(slug: str, dest_path: str) -> str:
    """Save a wiki to a .wiki file — a zip bundle of the SQLite DB, its media as
    files, and a manifest."""
    import json
    import tempfile
    import zipfile

    if not exists(slug):
        raise ValueError(f"no wiki '{slug}'")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    db.backup_db(str(db_path(slug)), tmp.name)  # consistent snapshot

    token = db.current_wiki.set(slug)
    try:
        # The database went out through `backup_db`, which opens the file by
        # path; the images come through `get_conn`, which resolves a *slug*. If
        # this wiki's address changed between the two, `db.active_wiki()` falls
        # back to the registry default for a slug it no longer knows — and the
        # export would be one wiki's pages zipped up with another wiki's
        # pictures, with nothing saying so. Check, rather than hand that to
        # someone as a backup of their wiki.
        if not exists(slug):
            raise ValueError(
                f"the wiki '{slug}' was renamed or deleted while it was being "
                f"saved, so nothing was written. Save it again at its new "
                f"address.")
        media = db.get_conn().execute(
            "SELECT id, filename, data FROM images").fetchall()
    finally:
        db.current_wiki.reset(token)

    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp.name, "wiki.db")
        for row in media:
            z.writestr(f"media/{row['id']}-{row['filename']}", row["data"])
        z.writestr("manifest.json", json.dumps(
            {"format": "waikiki-zip-1", "name": name_of(slug), "slug": slug,
             "media": len(media)}, indent=2))
    Path(tmp.name).unlink(missing_ok=True)
    return dest_path


def _extract_db(src_path: str) -> str:
    """Return a path to the wiki.db — from a zip bundle or a raw .db file."""
    with open(src_path, "rb") as f:
        head = f.read(2)
    if head == b"PK":  # zip bundle
        import tempfile

        with zipfile.ZipFile(src_path) as z:
            name = "wiki.db" if "wiki.db" in z.namelist() else next(
                (n for n in z.namelist() if n.endswith(".db")), None)
            if not name:
                raise ValueError("Zip bundle has no wiki.db")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
            tmp.write(z.read(name))
            tmp.close()
            return tmp.name
    return src_path  # assume raw SQLite


def _bundle_manifest(src_path: str) -> dict | None:
    """The interchange manifest if ``src_path`` is a **wiki bundle**, else ``None``.

    Two unrelated archives arrive through Open and they are not interchangeable.
    A Jamgraf *wiki file* wraps a whole SQLite database (``wiki.db``); a
    wiki-*interchange bundle* — what Kahala's export and `export_wiki_bundle`
    both produce — carries ``manifest.json`` plus one ``pages/<slug>.snapshot``
    per page and no database at all. Only the format tag tells them apart, so
    this reads that and nothing else: the version gate, the content-only guard
    and every per-page envelope stay the vendored library's judgement (rule 7).

    ``None`` means "not a bundle" — a raw ``.db``, the zip that wraps one, or a
    file that is not a zip at all. A file that *is* tagged as a bundle but is
    unreadable for some other reason still returns its manifest, so the import
    fails with that real reason instead of the misleading "not a Jamgraf wiki
    file" that a non-bundle earns.
    """
    from .vendor import wiki_interchange as wi

    try:
        with open(src_path, "rb") as f:
            if f.read(2) != b"PK":
                return None
        with zipfile.ZipFile(src_path) as z:
            manifest = json.loads(z.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(manifest, dict) or manifest.get("format") != wi.FORMAT_BUNDLE:
        return None
    return manifest


def _import_bundle(src_path: str, display: str) -> str:
    """Apply a wiki-interchange bundle into a freshly registered wiki.

    A bundle is not a wiki *file*, so there is nothing to copy into place: the
    pages are interchange snapshots, not rows. We register an empty wiki and let
    ``store.import_wiki_bundle`` merge the bundle into it through the ordinary
    repository path — the same call Kahala's pull makes (rule 7), so pages are
    rendered, versioned and re-embedded locally exactly as they are on a sync.
    That is also why this path does **not** end in ``_reindex_import``: the
    embeddings were regenerated page by page as they landed, so there is no
    imported index to distrust.

    Failure splits the same way it does on a pull, and for the same reason —
    "refused" is a promise that nothing changed, so it is only said when that is
    true. Every gate runs before the first write, so a refused bundle leaves the
    empty wiki we just made with nothing in it and we remove it again rather than
    strand a shell in the registry. A failure *after* writes began keeps the
    wiki: it is a partly-merged import, and one we created this call — never a
    file of the user's to delete — so it is named and left for them.
    """
    from . import store

    slug = create_wiki(display)
    started = False

    def writing() -> None:
        nonlocal started
        started = True

    token = db.current_wiki.set(slug)
    try:
        with open(src_path, "rb") as fh:
            store.import_wiki_bundle(fh, author="import", on_writes_begin=writing)
    except Exception as exc:
        if started:
            raise ValueError(
                f"'{display}' was only partly imported: {exc}. Nothing was "
                "deleted, every page that did arrive is an ordinary versioned "
                "page, and importing the same bundle again finishes it.") from exc
        delete_wiki(slug)          # nothing was written; leave no empty shell
        raise ValueError(f"Bundle refused rather than imported: {exc}") from exc
    finally:
        db.current_wiki.reset(token)
    return slug


def import_from(src_path: str, name: str | None = None) -> str:
    """Open an external wiki, whichever of the two shapes it arrives in.

    A **wiki file** (raw ``.db``, or the zip wrapping one) is validated and
    copied into the managed wikis dir under a fresh slug, registered, and its
    retrieval index rebuilt if the file arrived without a usable one. A
    **wiki-interchange bundle** goes to ``_import_bundle`` instead, which lands
    it into a new wiki through the repository. A **folder of markdown**, or the
    zip of one that ``markdown_zip`` downloads, goes to ``import_markdown``.
    Rule 12: every export this app can write, it can read back, and this is the
    one door all three arrive at.

    Which shape it is, is **sniffed, never assumed**: a bundle by its format
    tag, a markdown archive by carrying `.md` and neither a database nor an
    interchange manifest, a wiki file last. An extension is not evidence — and
    the ordering is what keeps a refusal honest, because the wiki-file branch
    says "not a Jamgraf wiki file" about anything it cannot parse, which is the
    wrong thing to tell someone holding a perfectly good export of another
    shape.

    The rebuild is the load-bearing part. A wiki file carries `pages` and
    `pages_fts`, but every search in the app runs over `chunks`/`vec_chunks`
    (`rag.py`), so a file exported without that cache — or embedded at another
    machine's dimension — imports as a wiki that renders and links perfectly and
    answers *nothing*, silently. `rag.reindex_if_stale` is a no-op when the index
    came across intact, so a large healthy wiki is not re-embedded for nothing.
    """
    if Path(src_path).is_dir():
        return import_markdown(src_path, name=name)["slug"]

    manifest = _bundle_manifest(src_path)
    if manifest is not None:
        # The bundle names the wiki it came from; the filename is incidental
        # (Kahala's export lands as `<slug>-wiki-bundle.zip`), so the label wins
        # and the caller's name is only the fallback.
        label = str(manifest.get("label") or "").strip()
        return _import_bundle(
            src_path, label or (name or Path(src_path).stem or "Imported").strip())

    if _is_markdown_archive(src_path):
        return import_markdown(src_path, name=name)["slug"]

    display = (name or Path(src_path).stem or "Imported").strip()
    src_path = _extract_db(src_path)  # zip bundle -> temp wiki.db, or raw .db
    if not db.is_wiki_db(src_path):
        raise ValueError("Not a Jamgraf wiki file (missing pages/settings tables)")
    base = slugify(display)
    with _lock:
        reg = _load()
        existing = {w["slug"] for w in reg["wikis"]}
        slug, n = base, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        db.backup_db(src_path, str(db_path(slug)))  # consistent copy into the app
        reg["wikis"].append({"slug": slug, "name": display})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)
    _reindex_import(slug)
    return slug


def _reindex_import(slug: str) -> int:
    """Rebuild the freshly imported wiki's index, in *its* db context.

    Failure here is reported, never fatal: the wiki is imported and readable
    either way, and an index is a cache the user can rebuild from Settings.
    """
    from . import rag

    token = db.current_wiki.set(slug)
    try:
        return rag.reindex_if_stale()
    except Exception as exc:  # a cache rebuild must not fail an import
        import sys

        print(f"[jamgraf] could not index imported wiki '{slug}': {exc}",
              file=sys.stderr)
        return 0
    finally:
        db.current_wiki.reset(token)


# --- Markdown: a folder of <slug>.md, and the reader that takes it back ------
#
# Markdown is the lossy, portable shape: text only, one file per page, meant to
# land in a repo's `docs/` where a person edits it with everything else. Rule 12
# still applies — if the app can write wiki state to a file it must read that
# file back — so what the export writes has to be enough to reconstruct the
# pages it came from, and the two halves live next to each other here.
#
# That is why the exported file carries a frontmatter *header*. The body alone
# does not name the page: the slug is in the filename, but the title is a column
# and Jamgraf resolves `[[links]]` by title, so a wiki restored from filenames
# alone comes back with its links pointing at pages that no longer answer to
# those names. `title:` costs one line and fixes that; `parent:` costs one line
# on child pages and is the difference between markdown export being flat and it
# round-tripping the hierarchy, so both are written and both are read back as
# structure rather than as page properties (`structure.STRUCTURAL_KEYS`).
#
# What markdown does *not* carry is everything that is not page text: images,
# history, comments, suggestions, templates, elements, the trash, manual order
# and starred flags. A `.wiki` file or a bundle is the faithful copy; this is
# the one for a repo. `docs/export-import-parity.md` names the limits.

_MD_SUFFIXES = (".md", ".markdown")
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$")


def _export_text(page: dict, parent_slug: str | None) -> str:
    """One page as the file it exports to: its markdown, plus the header.

    A property of the page's own named `title` or `parent` gives way to the
    structural key of the same name — there is one `title:` line and the page's
    actual title has to be it. That is the one thing a markdown round-trip
    drops silently, and it is named in `docs/export-import-parity.md`.
    """
    meta, tags, body = structure.parse_frontmatter(page["markdown"])
    head = {"title": page["title"] or page["slug"]}
    if parent_slug:
        head["parent"] = parent_slug
    for key, value in meta.items():
        if key.lower() not in structure.STRUCTURAL_KEYS:
            head[key] = value
    return structure.build_frontmatter(head, tags) + body.lstrip("\n")


def _markdown_files(slug: str) -> list[tuple[str, str]]:
    """Every page of a wiki as ``(<slug>.md, text)``, sub-pages included."""
    from . import store

    out: list[tuple[str, str]] = []
    token = db.current_wiki.set(slug)
    try:
        for p in store.list_pages(include_children=True):
            page = store.get_page(p["slug"])
            out.append((f"{p['slug']}.md", _export_text(page, p.get("parent_slug"))))
    finally:
        db.current_wiki.reset(token)
    return out


def export_markdown(slug: str, dest_dir: str) -> int:
    """Write every page of a wiki to `dest_dir` as `<slug>.md` (round-trip to a
    repo's docs/). Returns the number of files written.

    Read back by `import_markdown`, which is the same door in reverse: the
    filename is the slug, and the frontmatter header carries the title and (for
    a sub-page) its parent."""
    import os

    os.makedirs(dest_dir, exist_ok=True)
    files = _markdown_files(slug)
    for name, text in files:
        with open(os.path.join(dest_dir, name), "w") as f:
            f.write(text)
    return len(files)


def markdown_zip(slug: str) -> bytes:
    """All pages of a wiki as a zip of `<slug>.md` files (for download).

    The same files `export_markdown` writes, so the download is restorable
    through Open like any other export (rule 12)."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in _markdown_files(slug):
            z.writestr(name, text)
    return buf.getvalue()


def _first_heading(markdown: str) -> str:
    """The page's leading `# H1`, if it has one before any fenced block.

    Only the fallback for a file with no `title:` — a hand-written one, since
    ours always writes the header. Stopping at the first fence is what keeps a
    `# comment` in a shell example from being read as the page's title."""
    _meta, _tags, body = structure.parse_frontmatter(markdown)
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            break
        m = _H1.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _title_from_name(stem: str) -> str:
    """Last resort: the filename, made readable. `my-notes` -> `My notes`."""
    text = re.sub(r"[-_]+", " ", stem).strip()
    return (text[:1].upper() + text[1:]) if text else ""


def _decode_markdown(source: str, text: str) -> dict:
    """One file -> one document for `store.import_markdown`.

    The filename is the slug, because it was the slug on the way out. `title:`
    and `parent:` are read as structure and removed from the text; `tags:` and
    every other property stay exactly where they are, so the ordinary write path
    indexes them as if a person had typed them.
    """
    stem = Path(source).stem
    struct, markdown = structure.take_structural(text)
    return {
        "source": source,
        "slug": render.slugify(stem),
        "title": (struct.get("title") or _first_heading(markdown)
                  or _title_from_name(stem)),
        "markdown": markdown,
        "parent": struct.get("parent", ""),
    }


def _is_markdown_member(name: str) -> bool:
    """A `.md` a person meant — not a resource fork, not something's dotfile.

    Skipping hidden paths is not tidiness: macOS zips carry `__MACOSX/._page.md`
    beside every file, whose name slugifies to the same page, and a folder that
    happens to be a repo carries `.git`. Either one would turn a good import
    into a collision refusal."""
    parts = Path(name).parts
    if not parts or name.endswith("/") or parts[0] == "__MACOSX":
        return False
    if any(part.startswith(".") for part in parts):
        return False
    return parts[-1].lower().endswith(_MD_SUFFIXES)


def _is_markdown_archive(src_path: str) -> bool:
    """True when this zip is a folder of markdown — the third shape Open takes.

    Sniffed like the other two and for the same reason (rule 12): a
    `<slug>-markdown.zip` this app produced must not be reported as "not a
    Jamgraf wiki file", which is the refusal the wiki-file branch gives anything
    it cannot parse. A database member or an interchange manifest means it is
    one of those instead, and both are decided before this is asked.
    """
    try:
        with open(src_path, "rb") as f:
            if f.read(2) != b"PK":
                return False
        with zipfile.ZipFile(src_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    if any(n.endswith(".db") or Path(n).name == "manifest.json" for n in names):
        return False
    return any(_is_markdown_member(n) for n in names)


def _markdown_docs(src: str) -> list[dict]:
    """Decode every `.md` under `src` (a folder or a zip) before anything is written.

    Nested files are taken by their **basename**, because that is what carries
    the slug: a subfolder is not hierarchy (`parent:` is), so `guides/setup.md`
    is the page `setup`. Two files claiming one slug is refused downstream, with
    both paths named, rather than one of them quietly winning.
    """
    path = Path(src)
    if not path.exists():
        # A ValueError, not the OSError `is_zipfile` would raise below: every
        # caller here reports a refusal to a person or an agent, and a typo'd
        # path is one of those, not a crash.
        raise ValueError(f"nothing at {src}")
    if path.is_dir():
        found = [(str(f.relative_to(path)), f)
                 for f in sorted(path.rglob("*")) if f.is_file()]
        entries = [(name, f) for name, f in found if _is_markdown_member(name)]
        docs = []
        for name, f in entries:
            try:
                docs.append(_decode_markdown(name, f.read_text(encoding="utf-8")))
            except UnicodeDecodeError as exc:
                raise ValueError(f"'{name}' is not UTF-8 text: {exc}") from exc
        return docs
    if not zipfile.is_zipfile(src):
        raise ValueError(f"{path.name} is neither a folder nor a zip of .md files")
    docs = []
    with zipfile.ZipFile(src) as z:
        for name in sorted(n for n in z.namelist() if _is_markdown_member(n)):
            try:
                docs.append(_decode_markdown(name, z.read(name).decode("utf-8")))
            except UnicodeDecodeError as exc:
                raise ValueError(f"'{name}' is not UTF-8 text: {exc}") from exc
    return docs


def import_markdown(src: str, name: str | None = None,
                    into: str | None = None) -> dict:
    """Read a folder (or zip) of `<slug>.md` back in — the reader for
    `export_markdown`, and rule 12's other half for it.

    **`into=None` makes a new wiki**, which is what Open does: a folder of
    markdown carries no label saying which wiki it came from (a bundle does),
    and opening a file has never modified a wiki that was already here.
    **`into='<slug>'` merges** into that wiki instead — the round-trip the MCP
    tool exists for, where an agent exported to a repo's `docs/`, the text was
    edited there, and it comes back. Merging updates a page of the same slug in
    place and versions it; nothing is ever deleted (`store.import_markdown`).

    Failure splits the way a bundle's does, because "refused" is a promise that
    nothing changed: every file is decoded and every slug settled before the
    first write, so a refusal removes the empty wiki it had just registered
    rather than stranding a shell in the registry, and only a failure *after*
    writes began is reported as partly imported.

    Returns the target slug and what landed. No reindex pass follows it, for the
    same reason `_import_bundle` has none: every page went through `store`, so
    each was embedded as it arrived.
    """
    from . import store

    docs = _markdown_docs(src)
    if not docs:
        raise ValueError(
            f"No .md files in {Path(src).name} — a markdown import is a folder "
            "(or a zip) of <slug>.md pages, one per page")

    if into is not None:
        if not exists(into):
            raise ValueError(f"no wiki '{into}'")
        slug, fresh = into, False
    else:
        display = (name or Path(src).stem or "Imported").strip()
        slug, fresh = create_wiki(display), True

    started = False

    def writing() -> None:
        nonlocal started
        started = True

    token = db.current_wiki.set(slug)
    try:
        landed = store.import_markdown(docs, author="import",
                                       on_writes_begin=writing)
    except Exception as exc:
        if started:
            raise ValueError(
                f"'{name_of(slug)}' was only partly imported: {exc}. Nothing "
                "was deleted, every page that did arrive is an ordinary "
                "versioned page, and importing the same folder again finishes "
                "it.") from exc
        if fresh:
            delete_wiki(slug)      # nothing was written; leave no empty shell
        raise ValueError(f"Markdown refused rather than imported: {exc}") from exc
    finally:
        db.current_wiki.reset(token)
    return {"slug": slug, **landed}


def ensure_help_wiki() -> None:
    """Register the built-in Help wiki if missing. Idempotent; safe from both the
    web app and the (separate-process) MCP server."""
    slug = config.HELP_WIKI
    with _lock:
        reg = _load()
        if any(w["slug"] == slug for w in reg["wikis"]):
            return
        reg["wikis"].append({"slug": slug, "name": "Help"})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)


def ensure_initialized() -> None:
    """First-run setup: migrate the legacy single DB into a 'main' wiki and
    seed the named wikis. Idempotent."""
    if _load()["wikis"]:
        return
    with _lock:
        reg = _load()
        if reg["wikis"]:
            return
        wdir = _wikis_dir()
        wikis: list[dict] = []

        # Migrate the pre-multi-wiki database into "main", if present. No
        # `db.file_move` here, and not an oversight: the connection cache is
        # keyed by wiki slug and `get_conn` only ever opens `db_path(slug)`, so
        # nothing in this process can have the legacy single-database file open
        # — there is no slug that names it. This also runs before any wiki is
        # registered, so there is no reader to hold off either.
        legacy = config.DB_PATH
        if legacy.exists():
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(legacy) + suffix)
                if src.exists():
                    shutil.move(str(src), str(wdir / ("main.db" + suffix)))
            wikis.append({"slug": "main", "name": "Main"})
        else:
            wikis.append({"slug": "main", "name": "Main"})

        for name in config.SEED_WIKIS:
            slug = slugify(name)
            if not any(w["slug"] == slug for w in wikis):
                wikis.append({"slug": slug, "name": name})

        _save({"wikis": wikis, "default": "main"})
