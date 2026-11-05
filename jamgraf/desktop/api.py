"""What the page may call natively, on any platform.

``window.pywebview.api`` is the one place the document can reach out of the
browser, and the page decides what to offer by **asking whether a method is
there** --- ``base.html``'s dictation picks the native route only when
``api.start_dictation`` exists, and ``wikis.html`` shows a native Save only when
``api.save_wiki`` does. So absence is not an oversight in this design, it is the
signal: a platform that has no native route for something leaves the method off
rather than stubbing it, and the page falls back to what a browser can do.

That is why the macOS-only methods are not here. They live in
:mod:`jamgraf.desktop.macos`, on a subclass of this, and a Windows window gets
exactly this class --- so ⌘-style dictation and the pasteboard reader are
missing there, which is the truth and renders as the browser route.
"""
from __future__ import annotations

import sys

OPEN_FILE_TYPES = ("Jamgraf wiki or export (*.wiki;*.db;*.zip)",
                   "All files (*.*)")
"""The Open dialog's file filters, and they are **not** free text. pywebview
parses each one with ``^([\\w ]+)\\((\\*...)\\)$`` (``webview.util.parse_file_type``)
and raises ``ValueError`` on anything else --- a **comma** in the description is
enough --- and ``Window.create_file_dialog`` validates every filter *before* it
opens a dialog. So a stray punctuation mark here is not a cosmetic bug: it is an
Open button that does nothing at all, which is exactly what shipped when this
read "Jamgraf wiki, bundle or markdown zip". Word characters and spaces only;
``tests/test_packaging.py`` fails the build on a filter pywebview would reject."""


class DesktopApi:
    """Exposed to the page as ``window.pywebview.api``.

    Every method here works on any platform pywebview supports: the file
    dialogs are pywebview's own, and opening a URL is :mod:`webbrowser`.

    Each dialog is opened *inside* the try, which is not stylistic: the dialog
    call itself can raise (pywebview validates the file filters first, see
    :data:`OPEN_FILE_TYPES`), and an exception escaping a JS-API method rejects
    the promise the page is awaiting --- so the ``alert()`` on the other side
    never runs and the button reads as dead. A failure a person can see beats a
    correct-looking no-op.
    """

    def _window(self):
        import webview

        return webview.windows[0]

    def open_url(self, url):
        """Open a URL in the system default browser/viewer (e.g. a PDF)."""
        import webbrowser

        webbrowser.open(url)
        return {"ok": True}

    def save_wiki(self, slug):
        import webview

        from .. import wikis

        try:
            result = self._window().create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{wikis.name_of(slug)}.wiki")
            if not result:
                return {"ok": False, "cancelled": True}
            path = result if isinstance(result, str) else result[0]
            wikis.export_to(slug, path)
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_wiki(self):
        import webview

        from .. import wikis

        try:
            result = self._window().create_file_dialog(
                webview.OPEN_DIALOG, file_types=OPEN_FILE_TYPES)
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            slug = wikis.import_from(path)
            return {"ok": True, "slug": slug, "name": wikis.name_of(slug)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_markdown_folder(self):
        """Open a *folder* of ``<slug>.md`` files as a new wiki.

        Rule 12 is about the pair being reachable, and a file dialog cannot pick
        a directory --- which is the shape ``export_markdown`` writes (the MCP
        tool, or a repo's docs/). The zip of the same files needs no button of
        its own: it goes through Open like any other archive.
        """
        import webview

        from .. import wikis

        try:
            result = self._window().create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return {"ok": False, "cancelled": True}
            path = result[0] if isinstance(result, (list, tuple)) else result
            r = wikis.import_markdown(path)
            return {"ok": True, "slug": r["slug"],
                    "name": wikis.name_of(r["slug"]), "pages": r["pages"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def native_module():
    """The platform's native extras, or None where we have none.

    Imported by name rather than probed: a module that fails to import is a bug
    we want to see, not a reason to quietly hand someone a lesser window.
    """
    if sys.platform == "darwin":
        from . import macos

        return macos
    if sys.platform == "win32":
        from . import windows

        return windows
    return None
