"""The Windows window: WebView2, and the three things it does not need.

Short on purpose. A WebView2 window is Chromium, so the page keeps the web APIs
WKWebView withholds — ``navigator.clipboard``, ``getUserMedia``, a real Edit
accelerator set — and the right move is to let the page use them rather than to
mirror :mod:`jamgraf.desktop.macos` method for method. The native API this
platform hands the page is therefore the plain cross-platform one, and the
dictation and pasteboard methods are **absent**, which is how ``base.html``
chooses the browser route (see :mod:`jamgraf.desktop.api`).

Two things here are load-bearing.

**The backend is named, never inferred.** pywebview falls back to MSHTML --- the
Trident engine from Internet Explorer --- when WebView2 is missing, and Jamgraf's
editor would not render in it. A silent fallback like that is the failure mode
rule 9 is about: the person clicks and finds a broken app, having been told
nothing. So the runtime is checked first (:func:`runtime_problem`), the backend
is pinned to ``edgechromium``, and a machine without it is told what is missing
and handed the browser --- which works perfectly --- rather than a window that
looks like a bug.

**Deep links are reported unavailable, not quietly claimed.** ``jamgraf://``
needs a key under ``HKCU\\Software\\Classes`` naming the executable. A pip
install's launcher is a shim inside whichever virtualenv it was installed into,
so the path we would register goes stale the moment that venv moves, and writing
a URL-scheme claim into someone's registry from a ``pip install`` is more than
they asked for. Saying so is the honest answer; see ``docs/deep-links.md``.
"""
from __future__ import annotations

from .api import DesktopApi

NAME = "Windows"
GUI = "edgechromium"
"""Pinned so pywebview cannot silently render the app in MSHTML."""

Api = DesktopApi
"""No native extras: Chromium gives the page everything macOS withholds."""

WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

# Microsoft's documented detection: the Evergreen runtime writes its version to
# `pv` under this client id, per-machine (usually under WOW6432Node) or
# per-user. A key with no `pv`, or `pv` of "0.0.0.0", means "not installed" --
# the uninstaller leaves the key behind -- so the version string is what counts,
# not the key's existence.
_CLIENT = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
           r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
_CLIENT_NATIVE = (r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                  r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")


def webview2_version() -> str | None:
    """The installed WebView2 runtime version, or None if there isn't one."""
    try:
        import winreg
    except ImportError:          # not Windows; nothing to find
        return None
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, _CLIENT),
                       (winreg.HKEY_LOCAL_MACHINE, _CLIENT_NATIVE),
                       (winreg.HKEY_CURRENT_USER, _CLIENT),
                       (winreg.HKEY_CURRENT_USER, _CLIENT_NATIVE)):
        try:
            with winreg.OpenKey(root, path) as key:
                version, _type = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        version = (version or "").strip()
        if version and version != "0.0.0.0":
            return version
    return None


def runtime_problem() -> str | None:
    """Why the native window cannot open, or None when it can.

    The words land in a terminal and in the help page, so they name the thing to
    install and where it comes from, and they say what happens instead --- an
    install step with no consequence attached reads as a dead end.
    """
    if webview2_version():
        return None
    return ("Jamgraf's own window needs Microsoft's WebView2 runtime, which "
            f"this machine hasn't got. Install it from {WEBVIEW2_URL} and start "
            "Jamgraf again for the app window. Until then Jamgraf opens in your "
            "browser, which does everything the window does except the native "
            "Save and Open dialogs.")


def install_deeplinks(base: str) -> None:
    """Nothing to install: see the module docstring."""
    return None
