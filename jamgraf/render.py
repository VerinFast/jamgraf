"""Markdown -> HTML rendering with GFM tables, fenced code, and [[wiki links]]."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape as _esc
from html import unescape as _unesc

from markdown_it import MarkdownIt
from markdown_it.common.utils import unescapeAll
from mdit_py_plugins.anchors import anchors_plugin


def _timeline_html(code: str) -> str:
    """A ```timeline fenced block: one 'When: What' per line -> a timeline."""
    items = []
    for line in code.splitlines():
        line = line.strip()
        if not line:
            continue
        when, sep, what = line.partition(":")
        if not sep:
            when, what = "", when
        items.append(f'<div class="tl-item"><div class="tl-when">{_esc(when.strip())}'
                     f'</div><div class="tl-what">{_esc(what.strip())}</div></div>')
    return '<div class="timeline">' + "".join(items) + "</div>"


def infobox(meta: dict) -> str:
    """Render frontmatter properties as an infobox (structured-data table)."""
    if not meta:
        return ""
    rows = "".join(f"<tr><th>{_esc(str(k))}</th><td>{_esc(str(v))}</td></tr>"
                   for k, v in meta.items())
    return f'<table class="infobox">{rows}</table>'

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, guess_lexer

    _HAS_PYGMENTS = True
except Exception:  # pragma: no cover
    _HAS_PYGMENTS = False


def _highlight(code: str, lang: str, _attrs) -> str:
    if lang == "timeline":            # component: ```timeline fenced block
        return _timeline_html(code)
    if _HAS_PYGMENTS:
        try:
            lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
            return highlight(code, lexer, HtmlFormatter(nowrap=False))
        except Exception:
            pass
    # markdown-it escapes for us when we return "" — but we've taken over, so escape.
    escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre><code>{escaped}</code></pre>"


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


def _valid_link(url: str) -> bool:
    u = (url or "").strip().lower()
    return not (u.startswith("javascript:") or u.startswith("vbscript:"))


def _wikilink_rule(state, silent: bool) -> bool:
    """Hide a ``[[Wiki Link]]`` from linkify as one opaque inline token.

    linkify rewrites any domain-shaped word inside a ``text`` token, and
    ``.md``/``.py``/``.sh`` are real TLDs — so ``[[Soul.md]]`` came out as
    ``[[<a href="http://Soul.md">…]]``, and :func:`linkify_wikilinks` then skipped
    that anchor, leaving the wikilink broken. Code spans and fences are consumed
    before this rule runs, so they still show ``[[Page]]`` literally.

    Backslash escapes and entities are decoded here, as the ``escape`` and
    ``entity`` rules would have done had they seen the span: ``[[A\\_B]]`` still
    reads ``A_B`` and ``[[Salt &amp; Pepper]]`` still resolves to ``salt-pepper``."""
    m = _WIKILINK.match(state.src, state.pos)
    if not m:
        return False
    if not silent:
        state.push("wikilink_text", "", 0).content = unescapeAll(m.group(0))
    state.pos = m.end()
    return True


def _wikilinks_back_to_text(state) -> None:
    """Once linkify has run, turn the hidden spans back into plain ``text``, so
    heading anchors, rendering and the post-render wikilink pass see exactly what
    they saw before."""
    for block in state.tokens:
        for tok in block.children or ():
            if tok.type == "wikilink_text":
                tok.type = "text"


def _make_md(allow_html: bool) -> "MarkdownIt":
    md = MarkdownIt("gfm-like",
                    {"highlight": _highlight, "linkify": True, "html": allow_html})
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.core.ruler.after("linkify", "wikilink_text", _wikilinks_back_to_text)
    md.use(anchors_plugin, min_level=1, max_level=3, slug_func=_slugify,
           permalink=True, permalinkSymbol="#")
    md.validateLink = _valid_link  # allow file:/mailto:/data: (block js:) on a local wiki
    return md


_md = _make_md(False)          # default: raw HTML escaped
_md_html = _make_md(True)      # opt-in per wiki: raw HTML passes through

# Post-render tweaks (simpler + more robust than renderer-rule overrides):
_VIDEO = ("mp4", "webm", "mov", "ogv")
_AUDIO = ("mp3", "wav", "ogg", "m4a")
_IMG_MEDIA = re.compile(
    r'<img\b[^>]*\bsrc="([^"]+\.(?:mp4|webm|mov|ogv|mp3|wav|ogg|m4a))"[^>]*>', re.I)
_EXT_LINK = re.compile(r'<a href="(https?://[^"]+)"')
# the ```timeline component returns raw HTML that markdown-it wraps in <pre><code>.
_TL_WRAP = re.compile(
    r'<pre><code class="language-timeline">(.*?)</code></pre>', re.S)


def _post_process(html: str) -> str:
    html = _TL_WRAP.sub(lambda m: m.group(1), html)   # unwrap timeline component

    def media(m: re.Match) -> str:
        src = m.group(1)
        kind = "video" if src.rsplit(".", 1)[-1].lower() in _VIDEO else "audio"
        return f'<{kind} src="{src}" controls style="max-width:100%"></{kind}>'

    html = _IMG_MEDIA.sub(media, html)
    html = _EXT_LINK.sub(
        r'<a class="ext" target="_blank" rel="noopener noreferrer" href="\1"', html)
    return html

# [[Wiki Link]], [[slug|Label]], [[Page#Section]], [[#Section]] -> internal links.
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_href(target: str, resolver=None) -> str:
    """Resolve a wikilink target (optionally 'Page#Section' or '#Section').

    `resolver(slugified_page)` -> canonical slug maps a page *title or slug* to the
    real page slug (link-by-title), so links survive renames. Falls back to the
    slugified target (a red link) when unresolved."""
    page_part, _, section = target.partition("#")
    if page_part.strip():
        key = _slugify(page_part)
        slug = (resolver(key) if resolver else None) or key
        href = "/wiki/" + slug
        if section.strip():
            href += "#" + _slugify(section)
        return href
    return "#" + _slugify(section)  # same-page section link


def _expand_wikilinks(markdown: str, resolver=None) -> str:
    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), m.group(2)
        label = (label or target).strip()
        return f"[{label}]({_wikilink_href(target, resolver)})"

    return _WIKILINK.sub(repl, markdown)


# Wikilinks are resolved AFTER markdown rendering, directly to anchors.
#
# They used to be rewritten to markdown links *before* rendering, which broke in
# every context the markdown renderer doesn't process:
#   * inside raw HTML blocks the link stayed literal "[Label](/wiki/x)" — the
#     shape most infobox templates are written in;
#   * inside `code` spans and fenced blocks the text was rewritten too, so the
#     Help pages documenting `[[Page Title]]` displayed a mangled link;
#   * element props never saw it at all (see elements.py).
# Rewriting the rendered HTML instead fixes all of those at once: markdown-it
# passes "[[X]]" through untouched as text, so it survives to this pass wherever
# it appears.
_SKIP_BLOCKS = re.compile(r"(?is)(<pre\b.*?</pre>|<code\b.*?</code>|<a\b.*?</a>)")
_TAG = re.compile(r"(<[^>]*>)")


def wikilink_anchor(target: str, label: str | None, resolver=None) -> str:
    """An anchor from RAW text — the label is escaped here.

    For callers holding plain text: element props, infobox cells, the wikilink
    map handed to components. If your label came out of rendered HTML it is
    already escaped; use `wikilink_anchor_html` instead or it gets escaped twice.
    """
    label = (label or target).strip()
    return f'<a href="{_esc(_wikilink_href(target, resolver))}">{_esc(label)}</a>'


def wikilink_anchor_html(target: str, label_html: str | None, resolver=None) -> str:
    """An anchor from text that markdown-it has ALREADY escaped.

    Two things go wrong if you treat rendered HTML as raw text (issue #78):

    * the label gets escaped twice — `"` became `&quot;` at render, escaping the
      `&` again yields `&amp;quot;`, which the browser draws as the literal
      characters `&quot;`. That is what a reader saw on the page.
    * the target arrives entity-encoded, so `[[Salt & Pepper]]` slugified to
      `salt-amp-pepper` and pointed at a page that does not exist.

    So: unescape the target before resolving it, and pass the label through
    untouched — it is already correct, and it lands in a text position where its
    existing escaping is exactly what is wanted.
    """
    target = _unesc(target)
    label_html = (label_html or _esc(target)).strip()
    return f'<a href="{_esc(_wikilink_href(target, resolver))}">{label_html}</a>'


def render_value(text: str, resolver=None) -> str:
    """Plain text -> safe HTML with [[wiki links]] resolved.

    For short values that aren't markdown documents — custom-element fields,
    infobox cells — so an element author gets working links without hand-writing
    a link parser in their component's JS."""
    return linkify_wikilinks(_esc(str(text if text is not None else "")), resolver)


def wikilink_map(values, resolver=None) -> dict:
    """{"[[Meru]]": "<a href=...>Meru</a>"} for every wikilink in `values`.

    Lets the browser substitute links into text a component wrote itself, while
    the *resolution* stays here: link-by-title and red-link detection need the
    page index, which a component in a shadow root has no access to.
    """
    out: dict[str, str] = {}
    for value in values:
        for m in _WIKILINK.finditer(str(value if value is not None else "")):
            out.setdefault(m.group(0),
                           wikilink_anchor(m.group(1), m.group(2), resolver))
    return out


def linkify_wikilinks(html: str, resolver=None) -> str:
    """Turn [[Page]] into anchors in already-rendered HTML.

    Only text nodes are touched: code/pre/existing anchors are skipped whole, and
    within what remains, tags are left alone so attribute values can't be
    corrupted."""
    if not html or "[[" not in html:
        return html

    def sub_text(text: str) -> str:
        return _WIKILINK.sub(
            lambda m: wikilink_anchor_html(m.group(1), m.group(2), resolver), text)

    out = []
    for i, chunk in enumerate(_SKIP_BLOCKS.split(html)):
        if i % 2:                     # a skipped block (code / pre / anchor)
            out.append(chunk)
            continue
        # Substitute in text only, never inside a tag's attributes.
        out.append("".join(
            part if j % 2 else sub_text(part)
            for j, part in enumerate(_TAG.split(chunk))))
    return "".join(out)


def extract_wikilink_refs(markdown: str) -> list[dict]:
    """Every cross-page [[wiki link]], in document order, keeping what
    `extract_wikilinks` throws away — the visible label and the #section:

        target   the slugified page part; resolve it through
                 `store.link_index()` to get the canonical slug
        label    the text a reader actually sees — "earth" in [[Edaphos|earth]],
                 "Guide#Setup" in [[Guide#Setup]] — same rule as
                 `wikilink_anchor`
        section  the anchor slug the link points at, or None

    Same-page [[#Section]] links have no page part and are skipped, exactly as in
    `extract_wikilinks`."""
    out = []
    for m in _WIKILINK.finditer(markdown or ""):
        raw, label = m.group(1), m.group(2)
        page_part, _, section = raw.partition("#")
        if not page_part.strip():
            continue
        out.append({"target": _slugify(page_part.strip()),
                    "label": (label or raw).strip(),
                    "section": _slugify(section) if section.strip() else None})
    return out


def extract_wikilinks(markdown: str) -> list[str]:
    """Return the target *page* slugs of cross-page [[wiki links]] (ignores the
    section part and same-page #links) — for the resolved/broken link stats.

    Labels and sections are dropped here; `extract_wikilink_refs` keeps them."""
    return [ref["target"] for ref in extract_wikilink_refs(markdown)]


_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPH = re.compile(r"[*_`~]")


def _heading_display(raw: str) -> str:
    """The heading's rendered text: [[Page|Label]]->Label, [t](u)->t, no emphasis
    — so its slug matches the id the anchor plugin assigns."""
    t = _WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), raw)
    t = _MD_LINK.sub(r"\1", t)
    t = _EMPH.sub("", t)
    return t.strip()


def _headings(markdown: str) -> list[tuple[int, int, str, str]]:
    """(line_index, level, display, slug) for each h1-3, with the SAME slug the
    anchor plugin uses (deduped: notes, notes-1, notes-2, …)."""
    out, in_fence, seen = [], False, {}
    for i, line in enumerate((markdown or "").split("\n")):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        display = _heading_display(m.group(2).strip())
        base = _slugify(display)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append((i, len(m.group(1)), display, base if n == 0 else f"{base}-{n}"))
    return out


def extract_toc(markdown: str) -> list[dict]:
    """Level-1..3 headings for the table of contents (slug matches the anchor id)."""
    return [{"level": lvl, "text": disp, "slug": slug}
            for (_i, lvl, disp, slug) in _headings(markdown)]


LEAD_ANCHOR = "__lead__"
"""Anchor naming the text before the first heading.

That text has no heading, so it could not be addressed at all — which is why it
had no per-section edit or Listen control. It is a real section to a reader, so
it gets a reserved name rather than staying unreachable.
"""


def lead_span(markdown: str) -> tuple[int, int] | None:
    """Char range of the lead: after any frontmatter, up to the first heading.

    Returns None when there is no lead text. The frontmatter block is excluded
    deliberately — editing the lead must not swallow or duplicate it.
    """
    text = (markdown or "")
    lines = text.split("\n")
    # Where the body starts: parse_frontmatter hands back the body, so the
    # offset is whatever it removed. Reusing it keeps the fence handling in one
    # place. (Stored markdown is LF-only — store normalises on save — so the
    # lengths line up.)
    #
    # Not `if body else 0`: a page that is frontmatter and nothing else has an
    # empty body, and that guard returned the frontmatter itself as the lead.
    from . import structure

    _meta, _tags, body = structure.parse_frontmatter(text)
    start = len(text) - len(body)
    heads = _headings(text)
    end = sum(len(lines[k]) + 1 for k in range(heads[0][0])) if heads else len(text)
    end = min(end, len(text))
    if end <= start or not text[start:end].strip():
        return None                       # page opens on a heading: no lead
    return (start, end)


def section_span_for_slug(markdown: str, slug: str) -> tuple[int, int] | None:
    """Char range of the section whose heading anchor is `slug`."""
    if slug == LEAD_ANCHOR:
        return lead_span(markdown)
    lines = (markdown or "").split("\n")
    heads = _headings(markdown)
    target = next((h for h in heads if h[3] == slug), None)
    if not target:
        return None
    start_line, level = target[0], target[1]
    end_line = len(lines)
    for h in heads:
        if h[0] > start_line and h[1] <= level:
            end_line = h[0]
            break
    start = sum(len(lines[k]) + 1 for k in range(start_line))
    end = min(sum(len(lines[k]) + 1 for k in range(end_line)), len(markdown))
    return (start, end)


def render_markdown(markdown: str, resolver=None, allow_html: bool = False) -> str:
    """Render markdown to HTML. `allow_html` (per-wiki opt-in) passes raw HTML
    through; otherwise raw HTML is escaped."""
    md = _md_html if allow_html else _md
    return linkify_wikilinks(_post_process(md.render(markdown or "")), resolver)


def pygments_css() -> str:
    return HtmlFormatter().get_style_defs(".highlight") if _HAS_PYGMENTS else ""


slugify = _slugify


# --- timestamps, for the templates -------------------------------------------
#
# SQLite writes `datetime('now')`, which is UTC and naive. Templates showed the
# raw string, which is precise and useless: "2026-08-21 09:14:02" does not tell
# a person that the page changed while they were looking away. Rounded, relative
# wording does, and it is the whole point of the history affordance on an
# article (issue #73) — the sentence has to make you notice.

_AGO_STEPS = (
    (60, "just now", 0),
    (3600, "minute", 60),
    (86400, "hour", 3600),
    (2592000, "day", 86400),
)


def time_ago(ts, now: datetime | None = None) -> str:
    """A UTC SQLite timestamp as "3 minutes ago"; the raw value if unparseable.

    Falls back to a plain date past a month, because "47 days ago" is arithmetic
    the reader has to undo. A timestamp in the future (a clock that moved) reads
    as "just now" rather than a negative.
    """
    if not ts:
        return ""
    text = str(ts).strip()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    seconds = (now - when).total_seconds()
    if seconds < 0:
        seconds = 0
    for limit, unit, size in _AGO_STEPS:
        if seconds < limit:
            if not size:
                return unit
            n = max(1, int(seconds // size))
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return f"on {when:%d %b %Y}".replace(" 0", " ")
