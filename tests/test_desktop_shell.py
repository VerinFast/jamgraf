"""The desktop shell, and what it must not claim.

One launcher serves two platforms (:mod:`jamgraf.desktop`), so the risks are not
about windows — they are about a shell telling the page something untrue. Three
of them are pinned here.

What cannot be tested anywhere but Windows is the registry walk in
``windows.webview2_version`` and pywebview's own WebView2 backend. Those are the
``windows-latest`` job's, and the tests below stop at the seam rather than
pretending to cover them. The macOS window itself is exercised by running it:
``jamgraf_app.py`` opens, loads the app and exposes the six-method API this file
asserts the shape of.
"""
from __future__ import annotations

import sys

import pytest

from jamgraf import config
from jamgraf.desktop import api as desktop_api
from jamgraf.desktop import macos, windows
import jamgraf.desktop as shell

NATIVE_ONLY = ("start_dictation", "read_clipboard")


def test_the_page_can_tell_which_native_routes_exist():
    """``base.html`` picks the native dictation route by asking whether
    ``api.start_dictation`` is there, so a platform without one must leave the
    method **off** rather than stub it. A stub returning an error would have the
    page take the native route and fail, instead of using the browser's own
    speech recognition — which on Chromium is the working route.

    Adding either method to the shared ``DesktopApi`` makes this go red.
    """
    for method in NATIVE_ONLY:
        assert hasattr(macos.Api, method), f"macOS lost its {method}"
        assert not hasattr(windows.Api, method), (
            f"the Windows window advertises {method} to the page, which will "
            f"take the native route and get nothing back")
        assert not hasattr(desktop_api.DesktopApi, method), (
            f"{method} moved into the cross-platform API, so every platform "
            f"now claims a route only macOS has")


def test_both_platforms_keep_the_dialogs_the_page_offers():
    """``wikis.html`` shows its native Save/Open only when these exist."""
    for method in ("save_wiki", "open_wiki", "open_markdown_folder", "open_url"):
        assert hasattr(macos.Api, method)
        assert hasattr(windows.Api, method)


def test_windows_pins_its_backend_so_mshtml_is_never_a_surprise():
    """pywebview falls back to MSHTML — Internet Explorer's engine — when
    WebView2 is missing, and Jamgraf's editor would not render in it. A person
    who clicks and finds a broken app has been told nothing (rule 9).
    """
    assert windows.GUI == "edgechromium"
    assert macos.GUI is None, "WKWebView is the only Mac backend; don't pin one"


def test_a_missing_webview2_runtime_is_named_with_where_to_get_it(monkeypatch):
    monkeypatch.setattr(windows, "webview2_version", lambda: None)
    reason = windows.runtime_problem()

    assert reason and "WebView2" in reason
    assert windows.WEBVIEW2_URL in reason, "the fix has no address"
    assert "browser" in reason, "says what is missing but not what happens now"


def test_a_present_runtime_is_not_a_problem(monkeypatch):
    monkeypatch.setattr(windows, "webview2_version", lambda: "121.0.2277.98")
    assert windows.runtime_problem() is None


def test_a_platform_with_no_window_says_so_rather_than_failing(monkeypatch):
    monkeypatch.setattr(desktop_api, "native_module", lambda: None)
    monkeypatch.setattr(shell, "native_module", lambda: None)

    reason = shell._no_window_reason()
    assert reason and "browser" in reason


def test_a_window_we_could_not_open_does_not_claim_to_be_the_desktop_shell(
        monkeypatch):
    """``config.is_desktop()`` drives what the help and capabilities pages say
    the app can do — native dictation, native Save/Open. A browser fallback that
    set JAMGRAF_DESKTOP would have both describe affordances the page has not
    got, which is worse than the missing window it is standing in for.

    Moving the ``os.environ`` line in ``main()`` above the reason check makes
    this go red.
    """
    monkeypatch.delenv("JAMGRAF_DESKTOP", raising=False)
    monkeypatch.delenv("WAIKIKI_DESKTOP", raising=False)
    monkeypatch.setattr(shell, "_no_window_reason", lambda: "no window here")
    monkeypatch.setattr(shell, "_pick_port", lambda host, start: start)
    if sys.platform == "darwin":
        from jamgraf import updater
        monkeypatch.setattr(updater, "finish_bundle_rename", lambda: None)

    seen = {}

    def fake_browser(host, bind_host, port, reason):
        seen["is_desktop"] = config.is_desktop()
        seen["reason"] = reason

    monkeypatch.setattr(shell, "_in_browser", fake_browser)

    shell.main()

    assert seen["reason"] == "no window here"
    assert seen["is_desktop"] is False, (
        "the browser fallback told the page it was running in the desktop shell")
    assert config.is_desktop() is False


def test_the_mcp_server_still_wins_over_any_window(monkeypatch):
    """A window where an MCP server was asked for is a server Claude waits on
    forever, plus a stray window. Guarded for the renamed env var in
    ``test_rename_compat.py``; this is the same promise for the new one.
    """
    monkeypatch.setenv("JAMGRAF_MCP", "1")
    monkeypatch.setattr(sys, "argv", ["jamgraf-app"])
    called = []
    from jamgraf import mcp_server
    monkeypatch.setattr(mcp_server, "main", lambda: called.append("mcp"))

    def no(*a, **k):
        raise AssertionError("a window was opened in MCP mode")

    monkeypatch.setattr(shell, "_no_window_reason", no)

    shell.main()
    assert called == ["mcp"]


# --- Connect Claude, on whichever platform is reading it ----------------------

def test_the_connect_page_names_this_platforms_claude_config(monkeypatch):
    """A wrong path here is worse than no path: someone follows it, finds
    nothing, and writes a second config Claude Desktop will never read.
    """
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    win = config.claude_desktop_config_path()
    assert win.parts[-2:] == ("Claude", "claude_desktop_config.json")
    assert "AppData" in str(win) and "Library" not in str(win)

    monkeypatch.setattr(config.sys, "platform", "darwin")
    mac = config.claude_desktop_config_path()
    assert "Library/Application Support" in mac.as_posix()

    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert ".config" in config.claude_desktop_config_path().as_posix()


def test_the_connect_page_asks_the_helper_rather_than_hardcoding_a_mac_path(
        monkeypatch):
    """Asserted with the platform faked to Windows on purpose: on a Mac the
    hardcoded path and the computed one are the same string, so a test that just
    looked for the computed path would pass with the bug still in place.
    """
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")

    page = TestClient(app, client=("127.0.0.1", 1)).get("/connect")

    assert page.status_code == 200
    assert "AppData" in page.text
    assert "Library/Application Support" not in page.text, \
        "the Connect page tells a Windows reader to edit a macOS path"
