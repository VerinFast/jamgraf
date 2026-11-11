"""Rule 12: every export this app can write, it can read back.

An export you cannot restore is not a backup — it is a file that looks like one,
and the person finds out on the day they need it. That is not hypothetical here:
Kahala's whole-wiki bundle was *produced* by `store.export_wiki_bundle` and
*consumed* by `store.import_wiki_bundle` for a whole release, and still could not
be opened, because the only door a person could reach (`wikis.import_from`) knew
one archive shape and reported a valid bundle as "not a Jamgraf wiki file".
Both halves existed and passed their own tests. Nothing checked that the pair
was reachable.

So this file does the checking, and it checks the *surface* rather than any one
round-trip: it discovers every `export*` function in the app and fails on one
that has neither a named importer, a recorded exemption, nor a place on the short
list of known gaps. The round-trips themselves are proved next door
(`test_wiki_bundle.py`, `test_ydoc_interchange.py`, `test_wikis.py`); what is
guarded here is that no *new* export can arrive without its reader.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent.parent / "jamgraf"

# (module, export) -> (module, importer). Both halves are named so a rename
# cannot quietly break the pair: `test_declared_importers_exist` resolves them.
PAIRS = {
    ("store", "export_snapshot"): ("store", "import_snapshot"),
    ("store", "export_changelog"): ("store", "import_changelog"),
    ("store", "export_wiki_bundle"): ("store", "import_wiki_bundle"),
    ("wikis", "export_to"): ("wikis", "import_from"),
    ("wikis", "export_markdown"): ("wikis", "import_markdown"),
    ("mcp_server", "export_markdown"): ("mcp_server", "import_markdown"),
    ("ydoc", "export_snapshot"): ("ydoc", "decode_snapshot"),
    ("ydoc", "export_changelog"): ("ydoc", "apply_changelog"),
    ("ydoc", "export_bundle"): ("ydoc", "open_bundle"),
}

# Exempt, with the reason. A render target is a lossy presentation of a page for
# a person to read, not a serialization of wiki state — there is nothing to
# restore from, and that will not change.
EXEMPT = {
    ("mcp_server", "export_pdf"): "a render target, not wiki state",
}

# No open violations. Markdown was the last one: `wikis.export_markdown` wrote
# `<slug>.md` per page and nothing read a directory of them back, so a wiki
# exported that way could not be restored from what was written. It is closed —
# the export now carries the page's title (and a sub-page's parent) in the
# frontmatter header, and `wikis.import_markdown` reads a folder or a zip of
# them back through `store`. `tests/test_markdown_roundtrip.py` proves the
# round-trip and that Open reaches it.
#
# This set may SHRINK, never grow. A new export arrives with its reader or it
# does not arrive; `test_the_known_gaps_are_exactly_these` fails on any edit in
# either direction, so closing a gap and adding one are both deliberate acts
# that show up in review.
KNOWN_GAPS: dict[tuple[str, str], str] = {}


def _exported_names() -> set[tuple[str, str]]:
    """Every ``export*`` function defined in the app (never the vendored lib).

    Parsed rather than imported: discovery must not depend on a module's import
    side effects, and a function that is never reached still ships.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(_PACKAGE.rglob("*.py")):
        if "vendor" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("export"):
                found.add((path.stem, node.name))
    return found


def test_every_export_has_an_import():
    """An export surface shipped without a reader is incomplete work (rule 12)."""
    unaccounted = _exported_names() - set(PAIRS) - set(EXEMPT) - set(KNOWN_GAPS)
    assert not unaccounted, (
        "these export functions have no importer, no exemption and no recorded "
        "gap — rule 12 says an export you cannot restore is not a backup, so "
        "either add the reader or say here in writing why there isn't one: "
        f"{sorted(unaccounted)}")


def test_declared_importers_exist():
    """Both halves of every pair must still be there, under the names claimed."""
    for (mod, exp), (imod, imp) in sorted(PAIRS.items()):
        produce = importlib.import_module(f"jamgraf.{mod}")
        consume = importlib.import_module(f"jamgraf.{imod}")
        assert callable(getattr(produce, exp, None)), \
            f"jamgraf.{mod}.{exp} is gone — update PAIRS rather than dropping it"
        assert callable(getattr(consume, imp, None)), (
            f"jamgraf.{imod}.{imp} is gone, and it is rule 12's reader for "
            f"{mod}.{exp} — that export is now unrestorable")


def test_the_known_gaps_are_exactly_these():
    """The debt list is pinned, and it is currently empty.

    Failing here is not a reason to edit the expectation — it is the review
    prompt. Markdown was the last entry and its reader landed
    (`wikis.import_markdown`), so an export that cannot be restored now has
    nowhere to sit: it needs a reader, not a line here.
    """
    assert KNOWN_GAPS == {}, (
        "rule 12's known-gap list grew; see this test's docstring")


def test_exemptions_are_render_targets_only():
    """Only a lossy render target may be exempt — never a serialization of state."""
    for (mod, name), reason in sorted(EXEMPT.items()):
        assert "render target" in reason, (
            f"{mod}.{name} claims an exemption for a reason rule 12 does not "
            f"allow ({reason!r}); wiki state must round-trip")
