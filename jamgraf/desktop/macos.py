"""What only a Mac can do: Apple Event deep links, Dictation, the pasteboard.

Three of these exist because WKWebView withholds something a browser gives the
page. They are not "nice to have on a Mac" — each one replaces a web API that is
*present and non-functional* in the packaged app, which is the worst kind of
missing, and the measurements that establish that are recorded with each.

A WebView2 window on Windows is Chromium and withholds none of them, which is
why :mod:`jamgraf.desktop.windows` is almost empty. Nothing here should be
generalised into the cross-platform shell on the assumption it is needed there.
"""
from __future__ import annotations

import sys

from .api import DesktopApi

NAME = "macOS"
GUI = None
"""Let pywebview pick: WKWebView is the only backend we ship for."""

_DEEPLINK_HANDLER = None    # see install_deeplinks


def runtime_problem() -> str | None:
    """Why the native window cannot open, or None when it can.

    Nothing to check on a Mac: WKWebView is part of the OS, so the window is
    always available. Windows has a runtime that may be missing.
    """
    return None


def install_deeplinks(base: str) -> None:
    """Route ``jamgraf://`` URLs from macOS into the window.

    macOS delivers a URL-scheme open as the ``GURL`` Apple Event, so we register
    a handler with NSAppleEventManager. Every failure here is swallowed: deep
    links are a convenience, and a missing pyobjc symbol or an odd macOS build
    must never stop the app from starting.

    Only ``deeplink.resolve``'s output is ever navigated to -- see that module
    for why the URL is not passed through.
    """
    try:
        import objc                                      # noqa: F401
        from Foundation import NSAppleEventManager, NSObject

        import webview

        from .. import deeplink

        # Four-char codes: 'GURL'/'GURL' is the internet-event class and the
        # get-url event id; '----' is the direct-object keyword holding the URL.
        k_internet_class = k_get_url = 0x4755524C
        k_direct_object = 0x2D2D2D2D

        class _DeepLinkHandler(NSObject):
            def handleEvent_withReplyEvent_(self, event, reply):
                try:
                    desc = event.paramDescriptorForKeyword_(k_direct_object)
                    url = desc.stringValue() if desc else None
                    path = deeplink.resolve(url or "")
                    if not path:
                        print(f"[jamgraf] deep link refused: {url!r}",
                              file=sys.stderr)
                        return
                    if webview.windows:
                        webview.windows[0].load_url(base + path)
                except Exception as exc:
                    print(f"[jamgraf] deep link failed: {exc}", file=sys.stderr)

        # Kept on the module so the ObjC runtime doesn't collect our handler --
        # NSAppleEventManager holds only a weak reference to the target.
        global _DEEPLINK_HANDLER
        _DEEPLINK_HANDLER = _DeepLinkHandler.alloc().init()
        NSAppleEventManager.sharedAppleEventManager() \
            .setEventHandler_andSelector_forEventClass_andEventID_(
                _DEEPLINK_HANDLER, b"handleEvent:withReplyEvent:",
                k_internet_class, k_get_url)
    except Exception as exc:
        print(f"[jamgraf] deep links unavailable: {exc}", file=sys.stderr)


class Api(DesktopApi):
    """The cross-platform API plus what WKWebView makes us do ourselves."""

    def start_dictation(self):
        """Trigger macOS Start Dictation into the focused field.

        WKWebView exposes no SpeechRecognition and no getUserMedia (measured:
        both false in the packaged app), so the web speech route is a dead
        end here. Native dictation sidesteps it entirely — it types into
        whatever text field has focus, which is the box the user just
        clicked the mic beside.

        Clicking a menu item needs Accessibility permission, so say so
        plainly when it is refused rather than failing silently.
        """
        import subprocess

        script = (
            'tell application "System Events"\n'
            '  set p to first process whose frontmost is true\n'
            '  set em to menu "Edit" of menu bar 1 of p\n'
            '  repeat with mi in menu items of em\n'
            '    try\n'
            '      if name of mi starts with "Start Dictation" then\n'
            '        click mi\n'
            '        return "ok"\n'
            '      end if\n'
            '    end try\n'
            '  end repeat\n'
            'end tell\n'
            'return "no-item"'
        )
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=6)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out == "ok":
                return {"ok": True}
            err = (r.stderr or "").strip() or (
                "Dictation isn't enabled — turn it on in System Settings › "
                "Keyboard › Dictation" if out == "no-item"
                else "couldn't start dictation")
            low = err.lower()
            if ("not allowed" in low or "assistive" in low
                    or "-1719" in err or "-25211" in err):
                err += (" — grant Jamgraf Accessibility access in System "
                        "Settings › Privacy & Security › Accessibility")
            return {"ok": False, "error": err}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def read_clipboard(self):
        """Text currently on the system pasteboard, or "".

        WKWebView does not expose ``navigator.clipboard.readText()`` to the
        page, so the browser has no way to reach the clipboard and ⌘V did
        nothing at all. Reading NSPasteboard natively is the only reliable
        path. Read-only, and only ever called from our own paste handler.
        """
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString

            pb = NSPasteboard.generalPasteboard()
            return pb.stringForType_(NSPasteboardTypeString) or ""
        except Exception as exc:
            print(f"[jamgraf] clipboard read failed: {exc}", file=sys.stderr)
            return ""
