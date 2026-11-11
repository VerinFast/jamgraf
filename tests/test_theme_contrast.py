"""Guards against controls that exist but cannot be seen.

Two bugs shipped past presence assertions and were only caught by looking at a
dark-theme screenshot:

* the lead section's speaker was a bare ``<button>`` with no ``color``, so it
  inherited the UA default black — fine on the light theme, invisible on dark;
* the lead ``edit``/speaker pair carried ``opacity:0`` that only a heading hover
  raised, and the lead strip has no heading, so nothing could ever reveal them.

Asserting an element is in the DOM proves nothing about either. These tests read
the stylesheet instead, because the themes are *variable overrides* on top of
``default.css`` — so a control that hard-codes a colour, or omits one, is
unreadable on some theme by construction.
"""
from __future__ import annotations

import pathlib
import re

import jamgraf

_PKG = pathlib.Path(jamgraf.__file__).parent
THEMES = _PKG / "static" / "themes"
TEMPLATES = _PKG / "templates"


def _blocks(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for each rule, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _declared_at_rest(css: str, selector_part: str, prop: str) -> list[str]:
    """Every value ``prop`` takes in *unconditional* rules for the selector.

    State rules are deliberately excluded. Accepting a colour from ``:hover`` or
    ``.speaking`` is how the first version of this test passed while the button
    was still black at rest — the state the user actually looks at.
    """
    out = []
    for sel, decls in _blocks(css):
        if selector_part not in sel:
            continue
        if re.search(r":(?:hover|focus|focus-within|active)\b", sel) \
                or ".speaking" in sel:
            continue
        for m in re.finditer(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", decls):
            out.append(m.group(1).strip())
    return out


def test_the_themes_only_swap_variables():
    """The premise of every check below."""
    for name in ("dark.css", "sepia.css"):
        body = (THEMES / name).read_text(encoding="utf-8")
        assert '@import url("default.css")' in body, f"{name} must layer on default"


def test_article_controls_take_their_colour_from_a_variable():
    """A control with no ``color`` inherits black — invisible on the dark theme.

    This is the exact "black on black" the speaker button shipped with.
    """
    css = (THEMES / "default.css").read_text(encoding="utf-8")
    for control in (".tts-btn", ".section-edit", ".header-anchor",
                    # The article's link to its own history (issue #73): the
                    # app's undo, so invisible-on-dark is not a cosmetic bug.
                    ".lastedit", ".lastedit-note", ".lastedit-none"):
        colours = _declared_at_rest(css, control, "color")
        assert colours, (
            f"{control} sets no colour at rest, so it inherits UA black — "
            "readable on the light theme, invisible on the dark one")
        assert any(c.startswith("var(--") for c in colours), (
            f"{control} hard-codes {colours}; the themes only swap variables, "
            "so it cannot follow them")


def test_the_lead_controls_are_revealed_without_a_heading():
    """The lead strip has no heading, so heading-hover rules can never fire.

    Guards the specificity trap too: raising the *container* does nothing while
    each child carries its own ``opacity:0``, which is why the first fix to this
    bug changed nothing on screen.
    """
    css = (THEMES / "default.css").read_text(encoding="utf-8")
    revealing = [
        (sel, decls) for sel, decls in _blocks(css)
        if ".lead-tools" in sel
        and re.search(r"(?:^|;)\s*opacity\s*:\s*(?!0\s*(?:;|$))", decls)
        and (".section-edit" in sel or ".tts-btn" in sel)
    ]
    assert revealing, (
        "nothing raises the opacity of the lead controls themselves; a rule on "
        ".lead-tools alone is overridden by each child's own opacity:0")
    # At rest, not only on hover: children of a strip nobody thought to hover
    # are the same as no children at all.
    assert any(":hover" not in sel and ":focus-within" not in sel
               for sel, _ in revealing), \
        "the lead controls are hover-only, so they are invisible at rest"


# --- the context menu's dismissal gesture -----------------------------------
#
# The menu must survive the very gesture that opens it. A ctrl-click IS a left
# click, so mouseup/click follow the contextmenu event — in a LATER task. My
# first fix deferred the dismiss listener by a tick and a synchronous test
# passed, while the real gesture still closed the menu instantly. The listener
# therefore cannot be the gate; only a *fresh* mousedown may arm dismissal.

MENU_JS = None


def _menu_js() -> str:
    global MENU_JS
    if MENU_JS is None:
        MENU_JS = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    return MENU_JS


def test_dismissal_is_gated_on_a_fresh_mousedown():
    js = _menu_js()
    assert re.search(r"function\s+onDocClick\s*\([^)]*\)\s*\{\s*"
                     r"if\s*\(\s*!armed\b", js), (
        "the document click handler must bail unless a new mousedown armed it; "
        "a bare timeout is not enough — the opening gesture's own click lands "
        "in a later task and would close the menu immediately")
    assert re.search(r"addEventListener\('mousedown',\s*arm", js), \
        "nothing ever arms dismissal, so the menu could never be dismissed"


def test_opening_the_menu_disarms_it():
    """Reopening must reset the flag, or the second menu closes on its own click."""
    js = _menu_js()
    # The handler nests callbacks, so slice to the IIFE that wraps it rather
    # than to the first `});`.
    opener = js[js.index("addEventListener('contextmenu'"):]
    opener = opener[:opener.index("})();")]
    disarm = re.search(r"armed\s*=\s*false", opener)
    wire = re.search(r"addEventListener\('mousedown',\s*arm", opener)
    assert disarm and wire, \
        "the contextmenu handler must clear `armed` and wire the arming listener"
    assert disarm.start() < wire.start(), \
        "`armed` is cleared after the listeners are wired, so a reopened menu " \
        "inherits the previous gesture's armed state and closes on its own click"


def test_closing_tears_every_listener_down():
    """A leaked capture-phase listener eats clicks for the rest of the session."""
    js = _menu_js()
    close = js[js.index("function close()"):]
    close = close[:close.index("function onKey")]
    for ev in ("mousedown", "click", "keydown", "scroll"):
        assert f"removeEventListener('{ev}'" in close, f"{ev} listener leaks"
