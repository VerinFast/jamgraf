"""Jamgraf was Waikiki: every old name that is still out in the world keeps working.

A pure rename passes every other test in this suite while abandoning every
existing install, because nothing else looks at what the *rest of the world*
still says. These tests do. Each old name below is in a place this repo cannot
edit — a Claude Desktop config, doorman, a browser's cookie jar, a link pasted
into somebody's notes, an MCP server that has been running since before the
upgrade — so each one has to be READ under the old spelling, and never written.

See docs/rename-to-jamgraf.md for the full list and why none of it is removed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jamgraf import auth, bonjour, config, deeplink, store, updater, wikis

REPO = Path(__file__).resolve().parent.parent


# --- Environment variables ----------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("JAMGRAF_", "WAIKIKI_")):
            monkeypatch.delenv(k)
    return monkeypatch


def test_the_old_env_name_is_read_when_the_new_one_is_absent(clean_env):
    clean_env.setenv("WAIKIKI_WEB_URL", "http://old.example:1")
    assert config.env("WEB_URL") == "http://old.example:1"


def test_the_new_env_name_wins_over_the_old(clean_env):
    clean_env.setenv("WAIKIKI_WEB_URL", "http://old.example:1")
    clean_env.setenv("JAMGRAF_WEB_URL", "http://new.example:2")
    assert config.env("WEB_URL") == "http://new.example:2"


def test_an_empty_new_value_is_not_overruled_by_an_old_one(clean_env):
    """`JAMGRAF_UPDATE_PUBKEY=""` means "trust no keys" — a deliberate value."""
    clean_env.setenv("JAMGRAF_UPDATE_PUBKEY", "")
    clean_env.setenv("WAIKIKI_UPDATE_PUBKEY", "aa" * 32)
    assert config.env("UPDATE_PUBKEY") == ""
    assert updater._pinned_keys() == []


def test_neither_spelling_falls_back_to_the_default(clean_env):
    assert config.env("WEB_URL", "fallback") == "fallback"
    assert config.env_is_set("WEB_URL") is False
    clean_env.setenv("WAIKIKI_WEB_URL", "")
    assert config.env_is_set("WEB_URL") is True


def test_an_old_claude_desktop_config_still_gets_an_mcp_server(clean_env):
    """WAIKIKI_MCP=1 is what every pre-rename Claude Desktop config passes.

    Missed, the binary opens a desktop window where an MCP server was asked for:
    Claude sees a server that never answers, and a stray window appears.
    """
    import jamgraf_app
    from jamgraf import mcp_server

    called = []
    clean_env.setattr(mcp_server, "main", lambda: called.append("mcp"))
    clean_env.setenv("WAIKIKI_MCP", "1")
    clean_env.setattr(sys, "argv", ["jamgraf"])

    jamgraf_app.main()

    assert called == ["mcp"]


def test_the_legacy_single_database_keeps_its_historical_filename():
    """Pre-multi-wiki installs left `waikiki.db`; the migration must find it."""
    assert config.DB_PATH.name == "waikiki.db" or config.env_is_set("DB")


# --- The wiki header, and the cookies -----------------------------------------

def _client():
    from jamgraf.api import app
    return TestClient(app, client=("127.0.0.1", 1))


def test_an_mcp_server_from_before_the_upgrade_still_lands_in_its_wiki(wiki):
    """It keeps sending X-Waikiki-Wiki until it restarts.

    Ignored, its edits would fall through to the cookie or the default wiki — an
    agent writing into a wiki nobody pointed it at.
    """
    db_wiki = wikis.create_wiki("Agent Target")
    with _client() as c:
        r = c.post("/api/pages", json={"title": "Via Old Header", "markdown": "x"},
                   headers={"X-Waikiki-Wiki": db_wiki})
    assert r.status_code in (200, 201), r.text
    from jamgraf import db
    tok = db.current_wiki.set(db_wiki)
    try:
        assert store.get_page("via-old-header")
    finally:
        db.current_wiki.reset(tok)
    tok = db.current_wiki.set("main")
    try:
        assert store.get_page("via-old-header") is None
    finally:
        db.current_wiki.reset(tok)


def test_the_new_header_wins_when_both_are_sent(wiki):
    a, b = wikis.create_wiki("Header New"), wikis.create_wiki("Header Old")
    with _client() as c:
        c.post("/api/pages", json={"title": "Both Headers", "markdown": "x"},
               headers={"X-Jamgraf-Wiki": a, "X-Waikiki-Wiki": b})
    from jamgraf import db
    tok = db.current_wiki.set(a)
    try:
        assert store.get_page("both-headers")
    finally:
        db.current_wiki.reset(tok)


def test_a_browsers_pre_rename_wiki_cookie_still_selects_the_wiki(wiki):
    target = wikis.create_wiki("Cookie Target")
    with _client() as c:
        c.cookies.set("waikiki_wiki", target)
        r = c.post("/api/pages", json={"title": "Via Old Cookie", "markdown": "x"})
    assert r.status_code in (200, 201), r.text
    from jamgraf import db
    tok = db.current_wiki.set(target)
    try:
        assert store.get_page("via-old-cookie")
    finally:
        db.current_wiki.reset(tok)


def test_config_cookie_prefers_the_new_name():
    assert config.cookie({"waikiki_wiki": "old", "jamgraf_wiki": "new"}, "wiki") == "new"
    assert config.cookie({"waikiki_wiki": "old"}, "wiki") == "old"
    assert config.cookie({}, "wiki", "dflt") == "dflt"


def test_a_guests_pre_rename_share_cookie_still_signs_them_in(wiki, monkeypatch):
    """Accepted because the TOKEN is what is checked, not the name it arrives under."""
    monkeypatch.setattr(auth, "enabled", lambda: True)
    monkeypatch.setattr(auth, "check_token", lambda t: t == "good-token")
    from jamgraf.api import app
    with TestClient(app, client=("192.168.1.50", 1)) as c:      # a LAN guest
        c.cookies.set("waikiki_share", "good-token")
        signed_in = c.get("/", follow_redirects=False)
        c.cookies.clear()
        c.cookies.set("waikiki_share", "forged")
        refused = c.get("/", follow_redirects=False)
    assert signed_in.status_code == 200, signed_in.status_code
    assert refused.status_code in (302, 303, 307)
    assert "/login" in refused.headers["location"]


def test_logout_clears_the_pre_rename_share_cookie_too(wiki):
    """Otherwise a guest who logs out keeps a valid old cookie and stays in."""
    with _client() as c:
        r = c.post("/logout", follow_redirects=False)
    cleared = [h for h in r.headers.get_list("set-cookie")]
    assert any(h.startswith("jamgraf_share=") for h in cleared), cleared
    assert any(h.startswith("waikiki_share=") for h in cleared), cleared


def test_the_browser_reads_its_pre_rename_preferences():
    """Text size and rail width persist in the browser, under the old names."""
    base = (REPO / "jamgraf" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "(?:jamgraf|waikiki)_text_scale" in base
    assert "localStorage.getItem('waikiki_rail_w')" in base
    # ...and nothing writes the old names any more.
    assert "'waikiki_text_scale=" not in base
    assert "setItem('waikiki_" not in base


# --- Deep links ----------------------------------------------------------------

def test_a_waikiki_link_opens_exactly_where_a_jamgraf_link_does():
    for path in ("beaconlight", "beaconlight/meru", "beaconlight/meru#abilities",
                 "beaconlight?q=storm"):
        assert deeplink.resolve(f"waikiki://{path}") == \
            deeplink.resolve(f"jamgraf://{path}") is not None, path


@pytest.mark.parametrize("hostile", [
    "waikiki://../etc/passwd", "waikiki:beaconlight/meru", "waikiki://Bad Slug/x",
    "waikiki://beaconlight/../../x",
])
def test_the_old_scheme_gets_the_same_refusals(hostile):
    """A second spelling of the same door, not a second door."""
    assert deeplink.resolve(hostile) is None
    assert deeplink.resolve(hostile.replace("waikiki", "jamgraf", 1)) is None


def test_links_handed_out_now_use_the_new_scheme():
    assert deeplink.for_page("beaconlight", "meru").startswith("jamgraf://")


def test_the_bundle_registers_both_schemes():
    spec = (REPO / "jamgraf.spec").read_text(encoding="utf-8")
    m = re.search(r'"CFBundleURLSchemes":\s*\[([^\]]*)\]', spec)
    assert m and set(re.findall(r'"(\w+)"', m.group(1))) == {"jamgraf", "waikiki"}


# --- MCP, Bonjour, the updater ---------------------------------------------------

def test_the_version_tool_still_answers_under_the_old_key():
    from jamgraf import mcp_server
    out = mcp_server.version()
    assert out["jamgraf_version"] == out["waikiki_version"] == config.VERSION


def test_a_crashed_pre_rename_advertiser_is_reaped(monkeypatch):
    reaped = []
    monkeypatch.setattr(bonjour, "available", lambda: True)
    monkeypatch.setattr(bonjour, "_reap_orphans", reaped.append)

    class _Proc:
        def poll(self):
            return None
    monkeypatch.setattr(bonjour.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(bonjour, "_proc", None)

    bonjour.start(8787)

    assert set(reaped) == {"Jamgraf", "Waikiki"}


def test_a_symlinked_old_bundle_path_is_never_the_swap_target(tmp_path, monkeypatch):
    """After the first-launch rename, Waikiki.app is a symlink to Jamgraf.app.

    A process launched through it still reports the old path as sys.executable.
    The swap must land on the real bundle — moving the symlink instead would leave
    the update in a new Waikiki.app beside a stale Jamgraf.app.
    """
    real = tmp_path / "Jamgraf.app" / "Contents" / "MacOS"
    real.mkdir(parents=True)
    (real / "Jamgraf").write_text("")
    (tmp_path / "Waikiki.app").symlink_to(tmp_path / "Jamgraf.app")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(tmp_path / "Waikiki.app" / "Contents" / "MacOS" / "Jamgraf"))

    assert updater.bundle_path() == (tmp_path / "Jamgraf.app").resolve()
