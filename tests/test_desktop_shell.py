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

import pathlib
import sys

import pytest

from jamgraf import config
from jamgraf.desktop import api as desktop_api
from jamgraf.desktop import macos, windows
import jamgraf.desktop as shell

_ROOT = pathlib.Path(__file__).resolve().parent.parent

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

def test_the_connect_page_names_this_platforms_claude_config(monkeypatch,
                                                              pretend_platform):
    """A wrong path here is worse than no path: someone follows it, finds
    nothing, and writes a second config Claude Desktop will never read.
    """
    pretend_platform("win32", config)
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    win = config.claude_desktop_config_path()
    assert win.parts[-2:] == ("Claude", "claude_desktop_config.json")
    assert "AppData" in str(win) and "Library" not in str(win)

    pretend_platform("darwin", config)
    mac = config.claude_desktop_config_path()
    assert "Library/Application Support" in mac.as_posix()

    pretend_platform("linux", config)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert ".config" in config.claude_desktop_config_path().as_posix()


def test_the_connect_page_asks_the_helper_rather_than_hardcoding_a_mac_path(
        monkeypatch, pretend_platform):
    """Asserted with the platform faked to Windows on purpose: on a Mac the
    hardcoded path and the computed one are the same string, so a test that just
    looked for the computed path would pass with the bug still in place.
    """
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    pretend_platform("win32", config)
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")

    page = TestClient(app, client=("127.0.0.1", 1)).get("/connect")

    assert page.status_code == 200
    assert "AppData" in page.text
    assert "Library/Application Support" not in page.text, \
        "the Connect page tells a Windows reader to edit a macOS path"


def test_without_the_desktop_extra_the_window_command_still_works(monkeypatch):
    """`pip install jamgraf` puts `jamgraf-app` on the PATH whether or not the
    extra was asked for — console scripts are not conditional on an extra. So
    running it without pywebview used to be an ImportError traceback, which
    names neither the extra nor the fact that a browser would have worked.
    """
    monkeypatch.setattr(shell, "_pywebview_installed", lambda: False)

    reason = shell._no_window_reason()

    assert reason and "jamgraf[desktop]" in reason
    assert "browser" in reason


def test_with_the_extra_installed_the_window_is_not_refused(monkeypatch):
    monkeypatch.setattr(shell, "_pywebview_installed", lambda: True)
    monkeypatch.setattr(shell, "native_module", lambda: macos)
    monkeypatch.setattr(macos, "runtime_problem", lambda: None)

    assert shell._no_window_reason() is None


# --- Dictation: the page needs to know which shell it is in -------------------

def test_a_shell_with_no_native_dictation_does_not_claim_one(monkeypatch):
    """`is_desktop` and "has a native dictation route" are different questions,
    and conflating them is what made the Windows mic a dead button: the page
    suppressed the web speech API because it was in a desktop, then found no
    native method to call. The answer comes from the API class that would own
    the method, so there is one source of truth.
    """
    monkeypatch.delenv("JAMGRAF_NATIVE_DICTATION", raising=False)
    monkeypatch.delenv("WAIKIKI_NATIVE_DICTATION", raising=False)

    assert hasattr(macos.Api, "start_dictation")
    assert not hasattr(windows.Api, "start_dictation")
    assert config.native_dictation() is False

    monkeypatch.setenv("JAMGRAF_NATIVE_DICTATION", "1")
    assert config.native_dictation() is True


def test_the_page_is_told_both_flags_separately(monkeypatch):
    """A Windows window renders `JAMGRAF_DESKTOP = true` with
    `JAMGRAF_NATIVE_DICTATION = false`, which is the combination that lets the
    mic use the browser's own speech recognition. Emitting one flag for both
    states is the bug.
    """
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    client = TestClient(app, client=("127.0.0.1", 1))

    monkeypatch.setenv("JAMGRAF_DESKTOP", "1")
    monkeypatch.delenv("JAMGRAF_NATIVE_DICTATION", raising=False)
    monkeypatch.delenv("WAIKIKI_NATIVE_DICTATION", raising=False)
    page = client.get("/wikis").text
    assert "window.JAMGRAF_DESKTOP = true;" in page
    assert "window.JAMGRAF_NATIVE_DICTATION = false;" in page

    monkeypatch.setenv("JAMGRAF_NATIVE_DICTATION", "1")
    page = client.get("/wikis").text
    assert "window.JAMGRAF_NATIVE_DICTATION = true;" in page


def test_the_mic_takes_the_web_route_when_there_is_no_native_one():
    """The guard itself, read out of the template.

    Not a presence check: it asserts the *old* condition is gone. `!Rec ||
    isDesktop` returned on every desktop, before the web route could run;
    restoring it makes this red.
    """
    page = (_ROOT / "jamgraf" / "templates" / "base.html").read_text(encoding="utf-8")

    assert "if (nativeDictation || !Rec) return;" in page
    assert "if (!Rec || isDesktop) return;" not in page, \
        "the mic suppresses the web speech API on every desktop again"
    assert "if (nativeDictation && api) {" in page


# --- Where a pip install keeps its wikis --------------------------------------

def test_a_pip_install_does_not_write_inside_site_packages(monkeypatch, tmp_path,
                                                           pretend_platform):
    """`DATA_DIR.mkdir()` runs at import, so the old default — `ROOT/"data"`,
    which for a pip install is inside site-packages — was not merely an odd
    location: a machine-wide install gives a normal user no write permission
    there, so importing jamgraf raised PermissionError before the app could say
    anything. And where it *is* writable, `pip install -U` may remove it.
    """
    pretend_platform("win32", config)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    site = tmp_path / "lib" / "python3.13" / "site-packages"
    (site / "jamgraf").mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", site)

    assert config._installed_as_package() is True
    chosen = config._checkout_or_user_data()
    assert "site-packages" not in str(chosen)
    assert chosen == config.user_data_dir()


def test_a_checkout_keeps_its_own_data_directory(monkeypatch, tmp_path):
    """A dev copy stays self-contained; that is why this branch existed."""
    checkout = tmp_path / "jamgraf-checkout"
    (checkout / "jamgraf").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='jamgraf'\n")
    monkeypatch.setattr(config, "ROOT", checkout)

    assert config._installed_as_package() is False
    assert config._checkout_or_user_data() == checkout / "data"


def test_data_already_inside_the_package_is_still_used(monkeypatch, tmp_path,
                                                       capsys):
    """It should not be there, but if it is it holds somebody's wikis, and
    quietly reading a different directory would look exactly like losing them.
    So it is used, and the problem is reported instead."""
    site = tmp_path / "site-packages"
    (site / "jamgraf").mkdir(parents=True)
    legacy = site / "data"
    legacy.mkdir()
    (legacy / "wikis.json").write_text("{}")
    monkeypatch.setattr(config, "ROOT", site)

    assert config._checkout_or_user_data() == legacy
    assert "may remove it" in capsys.readouterr().err


# --- Quitting, without dropping what was just typed ---------------------------

def test_quitting_waits_for_the_server_to_shut_down():
    """Setting uvicorn's exit flag is not stopping it.

    The server runs on a daemon thread, so returning from the launcher lets the
    interpreter exit and kill it — possibly before the lifespan shutdown has
    awaited `collab.flush_all()`, which is the thing that writes an edit still
    inside the debounce window (issue #19). Dropping the join makes this red.
    """
    order = []

    class Server:
        should_exit = False

        def __setattr__(self, name, value):
            order.append(("flag", name, value))
            object.__setattr__(self, name, value)

    class Thread:
        def join(self, timeout=None):
            order.append(("join", timeout))

        def is_alive(self):
            return False

    shell._stop(Server(), Thread())

    assert order[0] == ("flag", "should_exit", True)
    assert order[1][0] == "join", "the launcher did not wait for the server"
    assert order[1][1] == shell.SHUTDOWN_GRACE


def test_a_server_that_will_not_stop_is_reported(capsys):
    """Rather than waited on forever, or passed over in silence — which would
    read as "your edits were saved"."""
    class Server:
        should_exit = False

    class Thread:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    shell._stop(Server(), Thread())

    assert "may not have been written" in capsys.readouterr().err
