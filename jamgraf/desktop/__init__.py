"""Jamgraf's desktop shell: one server, one window, two native backends.

The server half of Jamgraf runs anywhere Python does; this package is the window
around it. There is one shell, not one per platform: the port scan, the
background uvicorn, the wait for the port, the menu bar and the window are
shared, and only what an operating system genuinely does differently lives in
:mod:`jamgraf.desktop.macos` and :mod:`jamgraf.desktop.windows`. That split is
the point --- two copies of a launcher would drift, and the half that drifts is
always the one nobody is looking at (rule 5's reason, applied to the shell).

Three ways in, all landing here:

* ``Jamgraf.app`` --- PyInstaller bundles ``jamgraf_app.py``, which calls
  :func:`main`.
* ``jamgraf-app`` --- the console script from ``pip install jamgraf[desktop]``,
  which is how Windows gets a window at all.
* ``jamgraf`` --- the server with no window (``jamgraf.__main__``), unchanged.

When a platform cannot open a window --- Windows without the WebView2 runtime,
or anything that is neither macOS nor Windows --- this does **not** fail and does
not quietly produce a worse window. It says what is missing and serves the app
to the system browser instead, which is a complete Jamgraf minus the native
Save/Open dialogs. A person who installed a desktop app and got a terminal with
no explanation has been told nothing; that is the failure rule 9 is about.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .. import config
from ..api import app as fastapi_app
from .api import OPEN_FILE_TYPES, DesktopApi, native_module

__all__ = ["main", "OPEN_FILE_TYPES", "DesktopApi", "native_module"]


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def _pick_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        if not _port_open(host, port):
            return port
    return start


def _serve(bind_host: str, port: int, log_level: str) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(fastapi_app, host=bind_host, port=port,
                       log_level=log_level))


def _wait_for_port(host: str, port: int, seconds: float = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline and not _port_open(host, port):
        time.sleep(0.15)
    return _port_open(host, port)


def _menu(base: str):
    """The native menu bar.

    On macOS this is not decoration: WKWebView ships no Edit menu, so Copy,
    Paste and Select All have no real menu items and the keyboard shortcuts do
    nothing without them. On Windows, WebView2 already handles those keys
    itself, and the same menu then serves as the discoverable route to the two
    kinds of searching --- in the page, and across the wiki --- plus Help.
    """
    import webview
    from webview.menu import Menu, MenuAction

    def _js(code):
        def handler():
            try:
                webview.windows[0].evaluate_js(code)
            except Exception:
                pass
        return handler

    def _go(path):
        def handler():
            try:
                webview.windows[0].load_url(base + path)
            except Exception:
                pass
        return handler

    return [
        Menu("Edit", [
            MenuAction("Copy", _js("window.wkCopy && window.wkCopy()")),
            MenuAction("Paste", _js("window.wkPaste && window.wkPaste()")),
            MenuAction("Select All", _js("window.wkSelectAll && window.wkSelectAll()")),
            MenuAction("Find in Page…", _js("window.wkFind && window.wkFind.open()")),
            MenuAction("Search Wiki…", _js("window.wkFocusSearch && window.wkFocusSearch()")),
        ]),
        Menu("View", [
            MenuAction("Zoom In", _js("wkTextScale && wkTextScale.bump(1)")),
            MenuAction("Zoom Out", _js("wkTextScale && wkTextScale.bump(-1)")),
            MenuAction("Actual Size", _js("wkTextScale && wkTextScale.reset()")),
        ]),
        Menu("Help", [
            MenuAction("Help Contents", _go("/help")),
            MenuAction("About Jamgraf", _go("/help/about")),
            MenuAction("Connect Claude", _go("/connect")),
        ]),
    ]


def _pywebview_installed() -> bool:
    """Whether the GUI dependency is here, without importing it.

    ``find_spec`` rather than a try/import: importing pywebview starts looking
    for a GUI toolkit, and this runs on the path where there may not be one.
    """
    from importlib.util import find_spec

    try:
        return find_spec("webview") is not None
    except Exception:
        return False


def _no_window_reason() -> str | None:
    """Why this machine gets the browser instead of a window, or None.

    Three different answers, deliberately: the GUI dependency is not installed,
    the platform has no window at all, or its window needs a runtime. Each names
    the thing to do about it, because each is fixable by a different person doing
    a different thing.

    The first one exists because `pip install jamgraf` without the extra still
    puts `jamgraf-app` on the PATH -- console scripts are not conditional on an
    extra -- so running it was an ImportError traceback. A traceback is not a
    report (rule 9): it does not name the extra, and it buries the fact that the
    app would have worked in a browser.
    """
    if not _pywebview_installed():
        return ("Jamgraf's own window needs the `desktop` extra, which isn't "
                "installed: `pip install \"jamgraf[desktop]\"`. Opening in your "
                "browser instead, which does everything the window does except "
                "the native Save and Open dialogs.")
    native = native_module()
    if native is None:
        return ("Jamgraf's own window is available on macOS and Windows. On "
                f"{sys.platform} it opens in your browser instead, which does "
                "everything the window does except the native Save and Open "
                "dialogs.")
    return native.runtime_problem()


def _in_browser(host: str, bind_host: str, port: int, reason: str) -> None:
    """Serve the app and open it in the system browser.

    Not the same as headless: headless is for smoke-testing a build with no
    display, and prints nothing a person was meant to read. This is a window
    that could not open, so the reason goes out first and the app still arrives.
    """
    base = f"http://{host}:{port}"
    print(reason, file=sys.stderr)
    print(f"Jamgraf on {base}")

    server = _serve(bind_host, port, "warning")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if _wait_for_port(host, port):
        webbrowser.open(base + "/")
    else:
        print("Jamgraf server failed to start", file=sys.stderr)
        sys.exit(1)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True


def main() -> None:
    # MCP mode: run the stdio MCP server instead of the window. Lets Claude
    # Desktop launch the packaged .app directly as an MCP server (no source
    # checkout needed) — see the in-app Help page for the copy-paste config.
    # config.env, not os.environ: Claude Desktop configs written before the rename
    # pass WAIKIKI_MCP=1, and missing it would open a window where an MCP server
    # was asked for.
    if config.env("MCP") or "--mcp" in sys.argv:
        from .. import mcp_server
        mcp_server.main()
        return

    # Bind beyond loopback only when the owner has turned on LAN sharing (and set
    # a password) — see jamgraf/auth.py. Read at startup, so toggling it needs a
    # restart; that keeps the port closed by default.
    #
    # `host` stays the LOOPBACK address the window and health checks use: the auth
    # middleware grants owner rights to loopback callers, so the desktop window
    # must never load via 0.0.0.0 or its own LAN IP or we'd lock the owner out of
    # their own Settings. `bind_host` is what uvicorn listens on.
    from .. import auth
    host = config.HOST
    port = _pick_port(host, config.PORT)
    bind_host = "0.0.0.0" if (host == "127.0.0.1" and auth.share_lan_enabled()) else host

    # Headless mode (used to smoke-test the packaged binary without a display):
    # run the server in the foreground, no window.
    if config.env("HEADLESS"):          # doorman still sets WAIKIKI_HEADLESS
        print(f"Jamgraf (headless) on http://{host}:{port}")
        _serve(bind_host, port, "info").run()
        return

    # Finish a rename the old updater could only half-do (see the function).
    # macOS only: what it renames is an .app bundle, which no other platform has.
    # Desktop launches only: in MCP mode Claude may be starting this binary by its
    # old path at the very moment it would be moving. Never fatal.
    if sys.platform == "darwin":
        try:
            from .. import updater
            outcome = updater.finish_bundle_rename()
            if outcome:
                print(f"[jamgraf] app bundle from before the rename: {outcome}",
                      file=sys.stderr)
        except Exception as exc:                  # noqa: BLE001 - startup wins
            print(f"[jamgraf] bundle rename skipped: {exc}", file=sys.stderr)

    # No window here, so serve the browser instead and say why. Deliberately
    # before JAMGRAF_DESKTOP is set: this process is not the desktop shell, and
    # claiming it is would have the help and capabilities pages describe
    # affordances the page does not have.
    reason = _no_window_reason()
    if reason:
        _in_browser(host, bind_host, port, reason)
        return

    native = native_module()

    # Tell the page it is running in the desktop shell. Sniffing for
    # window.pywebview races its async injection, and a mic that fails to render
    # because the check ran too early is invisible rather than broken.
    os.environ["JAMGRAF_DESKTOP"] = "1"

    import webview

    server = _serve(bind_host, port, "warning")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to accept connections before showing the window.
    if not _wait_for_port(host, port):
        print("Jamgraf server failed to start", file=sys.stderr)
        sys.exit(1)

    base = f"http://{host}:{port}"
    webview.create_window(
        "Jamgraf", f"{base}/", width=1200, height=820,
        min_size=(800, 600), js_api=native.Api(), text_select=True,
    )
    # Deep links (jamgraf://). webview.start runs the callback once the GUI is
    # up, so a handler always has a window to navigate. Nothing is queued: a
    # link that arrives before the handler is registered (a cold launch *via* a
    # link) is the OS's to redeliver, and if it doesn't, the app simply opens on
    # the front page. See docs/deep-links.md.
    #
    # `gui` is whatever the platform pins — on Windows that stops pywebview
    # quietly rendering the app in MSHTML.
    start_kwargs = {"menu": _menu(base)}
    if native.GUI:
        start_kwargs["gui"] = native.GUI
    webview.start(native.install_deeplinks, (base,), **start_kwargs)  # blocks

    server.should_exit = True  # ask uvicorn to stop; daemon thread exits with us
