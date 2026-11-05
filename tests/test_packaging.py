"""The two places this app declares its dependencies must agree.

`requirements.txt` is what the build and the dev venv install; the `dependencies`
list in `pyproject.toml` is what `pip install jamgraf` resolves. Packaging for
PyPI created that duplication, and a Dependabot PR moves one file at a time — so
without this the wheel on PyPI could pin something different from the app we
test and ship, and nothing would say so.
"""
from __future__ import annotations

import ast
import pathlib
import re
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    out = []
    for line in (_ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            out.append(line)
    return sorted(out)


def _pyproject() -> list[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return sorted(data["project"]["dependencies"])


def test_pyproject_dependencies_match_requirements():
    req, proj = _requirements(), _pyproject()
    assert proj == req, (
        "requirements.txt and pyproject.toml disagree — the wheel would pin "
        f"something the app does not.\n  only in requirements: {set(req) - set(proj)}"
        f"\n  only in pyproject:     {set(proj) - set(req)}")


def test_the_packaged_version_matches_the_app_version():
    """A wheel that says 1.0.2 while the app says 1.0.3 is a support nightmare."""
    import jamgraf

    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert data["project"]["version"] == jamgraf.__version__


# --- The desktop shell's file-dialog filters ----------------------------------
#
# pywebview does not treat a filter string as free text: `parse_file_type`
# matches `^([\w ]+)\((\*...)\)$` and raises `ValueError` on anything else, and
# `Window.create_file_dialog` validates every filter *before* opening a dialog.
# So one comma in a description is an Open button that does nothing — no dialog,
# no error, because the exception escapes the JS-API method and merely rejects
# the promise the page is awaiting. That shipped once, from a label that read
# "Jamgraf wiki, bundle or markdown zip".
#
# The pattern is restated here rather than imported: pywebview is a packaging
# dependency, absent from the test venv, and this has to fail in CI too. If
# pywebview ever loosens its own rule, this test is stricter than it needs to
# be, which is the safe direction — it only rejects filters that would not have
# worked before.
_PYWEBVIEW_FILTER = re.compile(r'^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$')


def _dialog_filters() -> list[str]:
    """Every file-dialog filter the desktop shell declares.

    Parsed, not imported: the shell imports `webview` at call time, and the
    suite must not start a server or a window.
    """
    tree = ast.parse((_ROOT / "jamgraf" / "desktop" / "api.py").read_text())
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id.endswith("FILE_TYPES")
                   for t in node.targets):
            continue
        for element in getattr(node.value, "elts", []):
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                found.append(element.value)
    return found


def test_every_file_dialog_filter_is_one_pywebview_accepts():
    """A filter pywebview rejects is a button that silently does nothing."""
    filters = _dialog_filters()
    assert filters, (
        "no *FILE_TYPES tuple found in jamgraf/desktop/api.py — if the dialog "
        "filters "
        "moved or were inlined again, point this test at them rather than "
        "deleting it; an unchecked filter is a dead Open button")
    for f in filters:
        assert _PYWEBVIEW_FILTER.match(f), (
            f"pywebview would raise ValueError on {f!r} before opening any "
            "dialog, so the button would do nothing at all. Descriptions take "
            "word characters and spaces only — no commas.")
