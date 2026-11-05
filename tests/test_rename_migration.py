"""The first launch after the rename moves an existing install, and loses nothing.

Three things a pre-rename install has under the old name: its data directory,
its Kahala sign-in in the Keychain, and — when the old updater installed the
rename in place — the app bundle's own folder. Plus the Help wiki's prose, which
seeding would otherwise leave saying "Waikiki" forever.

The rule every test here holds the line on: anything still naming the OLD path
must keep finding the data. The worst outcome available is not a failed move — it
is a successful one that leaves a stale config silently creating a fresh, empty
install where the wikis used to be.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jamgraf import appconfig, config, db, help_content, secretstore, store, updater

REPO = Path(__file__).resolve().parent.parent


def _tree_digest(root: Path) -> str:
    """A fingerprint of every file under root — to prove nothing was touched."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def _old_install(parent: Path) -> Path:
    """What a pre-rename install leaves in Application Support."""
    old = parent / "Waikiki"
    (old / "wikis").mkdir(parents=True)
    (old / "wikis.json").write_text('{"wikis": [{"slug": "main", "name": "Main"}]}')
    (old / "app_config.json").write_text('{"backup_enabled": true}')
    conn = sqlite3.connect(old / "wikis" / "main.db")
    conn.execute("CREATE TABLE pages (slug TEXT, markdown TEXT)")
    conn.execute("INSERT INTO pages VALUES ('home', 'everything we wrote')")
    conn.commit()
    conn.close()
    return old


# --- The data directory --------------------------------------------------------

def test_an_old_install_is_moved_and_the_old_path_still_reaches_it(tmp_path):
    old = _old_install(tmp_path)
    before = _tree_digest(old)

    use, outcome = config.migrate_legacy_data_dir(tmp_path)

    new = tmp_path / "Jamgraf"
    assert (use, outcome) == (new, "moved")
    assert new.is_dir() and not new.is_symlink()
    assert _tree_digest(new) == before                   # every byte arrived
    assert old.is_symlink() and old.resolve() == new.resolve()
    # The point of the symlink: a stale WAIKIKI_DATA still finds the wikis.
    rows = sqlite3.connect(old / "wikis" / "main.db").execute(
        "SELECT markdown FROM pages").fetchall()
    assert rows == [("everything we wrote",)]


def test_a_stale_old_path_never_becomes_a_fresh_empty_install(tmp_path):
    """The hazard this whole migration is built around.

    config.py creates its data directory at import. After the move, a Claude
    Desktop config passing WAIKIKI_DATA=<old path> must land on the real wikis —
    not have that mkdir quietly make an empty folder in their place.
    """
    old = _old_install(tmp_path)
    config.migrate_legacy_data_dir(tmp_path)

    old.mkdir(parents=True, exist_ok=True)               # exactly what config.py does

    assert old.is_symlink()
    assert (old / "wikis.json").read_text().startswith('{"wikis"')


def test_a_handle_opened_before_the_move_keeps_writing_to_the_moved_data(tmp_path):
    """An MCP server or the old app may hold the database open across the move."""
    old = _old_install(tmp_path)
    held = sqlite3.connect(old / "wikis" / "main.db")

    config.migrate_legacy_data_dir(tmp_path)
    held.execute("INSERT INTO pages VALUES ('later', 'written mid-move')")
    held.commit()
    held.close()

    rows = sqlite3.connect(tmp_path / "Jamgraf" / "wikis" / "main.db").execute(
        "SELECT slug FROM pages ORDER BY slug").fetchall()
    assert rows == [("home",), ("later",)]


def test_an_empty_new_folder_is_cleared_out_of_the_way(tmp_path):
    old = _old_install(tmp_path)
    (tmp_path / "Jamgraf").mkdir()                        # e.g. made by a stray mkdir
    before = _tree_digest(old)

    use, outcome = config.migrate_legacy_data_dir(tmp_path)

    assert outcome == "moved"
    assert _tree_digest(tmp_path / "Jamgraf") == before


def test_two_sets_of_wikis_are_left_exactly_as_they_were(tmp_path):
    """Merging them, or picking one silently, is not this code's decision."""
    old = _old_install(tmp_path)
    new = tmp_path / "Jamgraf"
    (new / "wikis").mkdir(parents=True)
    (new / "wikis.json").write_text('{"wikis": []}')
    before_old, before_new = _tree_digest(old), _tree_digest(new)

    use, outcome = config.migrate_legacy_data_dir(tmp_path)

    assert (use, outcome) == (new, "both")
    assert not old.is_symlink()
    assert (_tree_digest(old), _tree_digest(new)) == (before_old, before_new)


def test_a_new_folder_that_is_a_symlink_is_somebody_elses_choice(tmp_path):
    old = _old_install(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "Jamgraf").symlink_to(elsewhere)
    before = _tree_digest(old)

    _, outcome = config.migrate_legacy_data_dir(tmp_path)

    assert outcome == "both"
    assert (tmp_path / "Jamgraf").is_symlink()           # not replaced by the data
    assert _tree_digest(old) == before


def test_a_move_that_cannot_link_back_is_undone(tmp_path, monkeypatch):
    """All or nothing: a moved folder with no symlink is the empty-install hazard."""
    old = _old_install(tmp_path)
    before = _tree_digest(old)

    def refuse(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(os, "symlink", refuse)

    use, outcome = config.migrate_legacy_data_dir(tmp_path)

    assert use == old and outcome.startswith("failed")
    assert old.is_dir() and not old.is_symlink()
    assert not (tmp_path / "Jamgraf").exists()
    assert _tree_digest(old) == before


def test_nothing_to_do_leaves_nothing_behind(tmp_path):
    assert config.migrate_legacy_data_dir(tmp_path) == (tmp_path / "Jamgraf", None)
    _old_install(tmp_path)
    config.migrate_legacy_data_dir(tmp_path)
    # A second launch finds the old path already a symlink.
    assert config.migrate_legacy_data_dir(tmp_path) == (tmp_path / "Jamgraf", None)


def test_two_processes_starting_at_once_move_it_exactly_once(tmp_path):
    """The desktop app and an MCP server spawned by Claude can start together."""
    _old_install(tmp_path)
    outcomes: list = []
    barrier = threading.Barrier(4)

    def start():
        barrier.wait()
        outcomes.append(config.migrate_legacy_data_dir(tmp_path)[1])

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("moved") == 1
    assert outcomes.count(None) == 3
    assert (tmp_path / "Waikiki").is_symlink()


def test_the_packaged_app_runs_the_move_before_creating_its_data_folder(tmp_path):
    """The wiring, not just the function: frozen config import, fresh process."""
    _old_install(tmp_path / "Library" / "Application Support")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("JAMGRAF_", "WAIKIKI_"))}
    env["HOME"] = str(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.frozen = True\n"
         "import jamgraf.config as c\n"
         "print(c.DATA_DIR); print(c.DATA_MIGRATION['outcome'])"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    data_dir, outcome = out.stdout.splitlines()   # "Application Support" has a space
    support = tmp_path / "Library" / "Application Support"
    assert Path(data_dir) == support / "Jamgraf" and outcome == "moved"
    assert (support / "Waikiki").is_symlink()


# --- The Kahala sign-in in the Keychain ----------------------------------------

ACCOUNT = "kahala:https://kc.example/realms/good-place"


class FakeKeychain:
    """A `security` that keeps (service, account) -> secret, and can be broken."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.refuse_writes = False

    def run(self, argv, **kwargs):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        done = Done()
        if argv[1] == "-i":
            words = shlex.split(kwargs["input"])
            opts = dict(zip(words[2::2], words[3::2]))
            if self.refuse_writes:
                done.returncode = 1
            else:
                self.items[(opts["-s"], opts["-a"])] = opts["-w"]
        else:
            opts = dict(zip(argv[2::2], argv[3::2]))
            key = (opts["-s"], opts["-a"])
            if argv[1] == "find-generic-password":
                if key in self.items:
                    done.stdout = self.items[key] + "\n"
                else:
                    done.returncode = 44
            elif argv[1] == "delete-generic-password":
                if self.items.pop(key, None) is None:
                    done.returncode = 44
        return done


@pytest.fixture
def keychain(monkeypatch):
    k = FakeKeychain()
    monkeypatch.setattr(secretstore, "_windows", lambda: False)
    monkeypatch.setattr(secretstore, "_security", lambda: "/usr/bin/security")
    monkeypatch.setattr(subprocess, "run", k.run)
    return k


def test_a_pre_rename_sign_in_is_moved_and_still_signs_in(keychain):
    keychain.items[("Waikiki", ACCOUNT)] = "refresh-token"

    assert secretstore.get_secret(ACCOUNT) == "refresh-token"

    assert keychain.items == {("Jamgraf", ACCOUNT): "refresh-token"}


def test_a_move_that_cannot_be_read_back_leaves_the_old_sign_in(keychain, capsys):
    """The worst a failed move may cost is doing it again — never the sign-in."""
    keychain.items[("Waikiki", ACCOUNT)] = "refresh-token"
    keychain.refuse_writes = True

    assert secretstore.get_secret(ACCOUNT) == "refresh-token"   # still signed in

    assert keychain.items == {("Waikiki", ACCOUNT): "refresh-token"}
    assert "could not move the Kahala sign-in" in capsys.readouterr().err


def test_a_current_sign_in_wins_and_the_old_one_is_not_consulted(keychain):
    keychain.items[("Jamgraf", ACCOUNT)] = "new-token"
    keychain.items[("Waikiki", ACCOUNT)] = "stale-token"

    assert secretstore.get_secret(ACCOUNT) == "new-token"


def test_signing_out_cannot_be_undone_by_the_old_entry(keychain):
    """An old item left at sign-out would be moved back into place on the next read."""
    keychain.items[("Jamgraf", ACCOUNT)] = "new-token"
    keychain.items[("Waikiki", ACCOUNT)] = "stale-token"

    assert secretstore.delete_secret(ACCOUNT) is True

    assert secretstore.get_secret(ACCOUNT) is None
    assert keychain.items == {}


# --- The app bundle, after the old updater installed the rename in place ------

def _bundle(parent: Path, name: str) -> Path:
    b = parent / name / "Contents" / "MacOS"
    b.mkdir(parents=True)
    (b / "Jamgraf").write_text("binary")
    return parent / name


def test_an_in_place_update_is_given_its_new_name(tmp_path):
    old = _bundle(tmp_path, "Waikiki.app")

    assert updater.finish_bundle_rename(old) == "renamed"

    new = tmp_path / "Jamgraf.app"
    assert new.is_dir() and not new.is_symlink()
    assert old.is_symlink() and old.resolve() == new.resolve()
    # A config naming the old path still launches: it resolves through the link.
    assert (old / "Contents" / "MacOS" / "Jamgraf").read_text() == "binary"


def test_a_jamgraf_bundle_already_beside_it_is_left_alone(tmp_path):
    old = _bundle(tmp_path, "Waikiki.app")
    _bundle(tmp_path, "Jamgraf.app")

    assert updater.finish_bundle_rename(old) == "both"
    assert old.is_dir() and not old.is_symlink()


def test_a_rename_that_cannot_link_back_is_undone(tmp_path, monkeypatch):
    old = _bundle(tmp_path, "Waikiki.app")

    def refuse(*a, **k):
        raise OSError("read-only volume")
    monkeypatch.setattr(os, "symlink", refuse)

    assert updater.finish_bundle_rename(old).startswith("failed")
    assert old.is_dir() and not old.is_symlink()
    assert not (tmp_path / "Jamgraf.app").exists()


def test_nothing_to_rename(tmp_path, monkeypatch):
    assert updater.finish_bundle_rename(_bundle(tmp_path, "Jamgraf.app")) is None
    monkeypatch.setattr(updater, "bundle_path", lambda: None)   # not a packaged run
    assert updater.finish_bundle_rename() is None


# --- The Help wiki's prose -------------------------------------------------------

@pytest.fixture
def help_wiki(wiki):
    help_content.seed()                                   # current text, flag set
    tok = db.current_wiki.set(config.HELP_WIKI)
    yield
    db.current_wiki.reset(tok)


def _rerun_migration():
    appconfig.set(help_content._RENAMED_FLAG, False)
    return help_content._rename_help_prose()


def test_an_untouched_pre_rename_page_gets_the_new_text_wholesale(help_wiki):
    """Sharing names the product five times — the page the rename hits hardest."""
    current = help_content._sharing()
    old = help_content._as_seeded_before_the_rename(current)
    assert old != current and "Waikiki" in old            # the test means something
    store.update_page("sharing", "Sharing", old, author="system")

    assert _rerun_migration() >= 1

    assert store.get_page("sharing")["markdown"] == current
    assert len(store.page_versions("sharing")) >= 2              # undoable


def test_an_edited_page_keeps_its_edits_and_only_loses_the_old_name(help_wiki):
    store.update_page("sharing", "Sharing",
                      "My own notes. Waikiki at github.com/VerinFast/waikiki, "
                      "open waikiki://main. Leave WAIKIKI_DATA alone.", author="human")

    _rerun_migration()

    text = store.get_page("sharing")["markdown"]
    assert text == ("My own notes. Jamgraf at github.com/VerinFast/jamgraf, "
                    "open jamgraf://main. Leave WAIKIKI_DATA alone.")


def test_the_system_prompts_are_never_touched(help_wiki):
    store.update_page("chat-system-prompt", "Chat System Prompt",
                      "You are Waikiki's helper.", author="human")

    _rerun_migration()

    assert store.get_page("chat-system-prompt")["markdown"] == "You are Waikiki's helper."


def test_it_runs_once(help_wiki, monkeypatch):
    calls = []
    monkeypatch.setattr(help_content, "_rename_help_prose", lambda: calls.append(1) or 0)
    help_content.seed()
    help_content.seed()
    assert calls == []                                    # flag set by the first seed


# --- What a person sees ----------------------------------------------------------

@pytest.mark.parametrize("outcome, shown", [
    (None, False), ("moved", False), ("both", True), ("failed: read-only", True)])
def test_manage_wikis_says_so_only_when_a_person_has_to_act(wiki, monkeypatch,
                                                           outcome, shown):
    from fastapi.testclient import TestClient

    from jamgraf.api import app
    monkeypatch.setattr(config, "DATA_MIGRATION",
                        outcome and {"outcome": outcome, "old": "/old/Waikiki",
                                     "new": "/new/Jamgraf"})
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        body = c.get("/wikis").text
    assert ("/old/Waikiki" in body) is shown
