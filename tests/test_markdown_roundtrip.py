"""Rule 12 for markdown: a folder of `.md` this app wrote, it reads back.

Markdown export was rule 12's one open violation — `<slug>.md` per page, and
nothing that could turn a directory of them back into a wiki. Closing it took
two decisions that these tests pin, because both are the kind that quietly
regress:

* **the exported file carries a header.** The body alone does not name the
  page. The slug is in the filename, but the title is a column and Jamgraf
  resolves `[[links]]` by title, so a wiki restored from filenames alone comes
  back with its links pointing at pages that answer to nothing. `title:` fixes
  that and `parent:` carries the hierarchy markdown used to drop.
* **`title` and `parent` are structure, not properties.** They are read back
  into the page's columns and taken *out* of the stored text; `tags:` and every
  other key stay exactly where the page had them.

The other half of rule 12 is reachability — the bundle importer existed, was
tested, and could not be reached by a person for a whole release — so the
route a person actually clicks is exercised here too, not just the function.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from jamgraf import db, rag, store, structure, wikis


def _export(tmp_path, slug="main", name="docs"):
    dest = tmp_path / name
    wikis.export_markdown(slug, str(dest))
    return dest


def _populate():
    db.current_wiki.set("main")
    store.create_page("Hōkūleʻa's Voyage",
                      "---\ntags: canoe, history\ncrew: 17\n---\n"
                      "She sailed to Tahiti without instruments.")
    store.create_page("Navigation", "Wayfinding by stars and swell.")
    store.create_page("Star Compass", "Thirty-two houses.")
    store.set_parent("star-compass", "navigation")


# --- The round-trip -----------------------------------------------------------

def test_a_folder_of_markdown_comes_back_as_the_same_wiki(wiki, tmp_path):
    """Pages, titles, tags, properties and hierarchy all survive the trip."""
    _populate()
    docs = _export(tmp_path)

    landed = wikis.import_markdown(str(docs), name="Restored")
    assert landed["slug"] == "restored"
    db.current_wiki.set("restored")

    pages = {p["slug"]: p for p in store.list_pages(include_children=True)}
    assert set(pages) == {"hōkūleʻas-voyage", "navigation", "star-compass"}

    # The title is the reason the header exists: derived from the filename it
    # would come back as "H K Le A S Voyage", and every [[link]] to it broken.
    voyage = store.get_page("hōkūleʻas-voyage")
    assert voyage["title"] == "Hōkūleʻa's Voyage"
    assert store.tags_of("hōkūleʻas-voyage") == ["canoe", "history"]
    assert store.get_property("hōkūleʻas-voyage", "crew") == "17"
    assert "without instruments" in voyage["markdown"]

    # Hierarchy: flat on disk, restored from `parent:`.
    assert pages["star-compass"]["parent_slug"] == "navigation"
    assert pages["navigation"]["parent_slug"] is None


def test_export_import_export_is_a_fixed_point(wiki, tmp_path):
    """Re-exporting what was imported produces the same files, byte for byte.

    The property that says the two halves agree about the format: if the
    importer dropped a tag, kept `title:` as a page property, or reordered the
    frontmatter, the second export would differ from the first.
    """
    _populate()
    first = _export(tmp_path, name="first")

    wikis.import_markdown(str(first), name="Again")
    second = _export(tmp_path, slug="again", name="second")

    assert sorted(p.name for p in second.iterdir()) == \
        sorted(p.name for p in first.iterdir())
    for f in sorted(first.iterdir()):
        assert (second / f.name).read_text() == f.read_text(), f.name


def test_structural_keys_do_not_become_page_properties(wiki, tmp_path):
    """`title:`/`parent:` are columns; left in the text they'd be infobox rows."""
    _populate()
    docs = _export(tmp_path)
    assert "title: Navigation" in (docs / "navigation.md").read_text()
    assert "parent: navigation" in (docs / "star-compass.md").read_text()

    wikis.import_markdown(str(docs), name="Clean")
    db.current_wiki.set("clean")
    meta, tags, _body = structure.parse_frontmatter(
        store.get_page("hōkūleʻas-voyage")["markdown"])
    assert "title" not in {k.lower() for k in meta}
    assert "parent" not in {k.lower() for k in meta}
    assert meta == {"crew": "17"} and tags == ["canoe", "history"]
    # A page with nothing but structure in its header keeps no block at all.
    assert store.get_page("star-compass")["markdown"] == "Thirty-two houses."


def test_every_page_lands_through_the_repository(wiki, tmp_path):
    """Rules 2/5/6: rendered, versioned, tag-indexed, embedded — like any write.

    An importer that wrote rows directly would produce pages that display and
    search nothing, which is the failure `wikis.import_from` already learned
    once (a wiki file whose chunk index didn't travel).
    """
    _populate()
    docs = _export(tmp_path)
    wikis.import_markdown(str(docs), name="Indexed")
    db.current_wiki.set("indexed")

    page = store.get_page("navigation")
    assert "<p>" in page["html"]                       # rendered
    assert store.page_versions("navigation")           # versioned
    assert store.pages_with_tag("canoe")               # tag index
    assert any(h["slug"] == "navigation"
               for h in rag.search_pages("wayfinding stars"))   # embedded


# --- What a folder means on the way in ---------------------------------------

def test_a_folder_becomes_a_new_wiki_and_leaves_the_others_alone(wiki, tmp_path):
    _populate()
    docs = _export(tmp_path)
    before = {w["slug"] for w in wikis.list_wikis()}

    landed = wikis.import_markdown(str(docs), name="Copy")

    assert landed["slug"] == "copy" and landed["slug"] not in before
    db.current_wiki.set("main")
    assert store.get_page("navigation")["markdown"] == \
        "Wayfinding by stars and swell."


def test_merging_updates_in_place_and_keeps_the_old_text_in_history(wiki, tmp_path):
    """`into=` is the repo round-trip: export, edit the files, bring them back.

    Merging by slug means an edit lands on the page it came from. It is
    versioned rather than overwritten, because an import that silently replaced
    a page the person had since edited would be a data-loss path wearing a
    restore's clothes.
    """
    _populate()
    docs = _export(tmp_path)
    (docs / "navigation.md").write_text(
        "---\ntitle: Navigation\n---\nWayfinding, edited in the repo.\n")
    (docs / "new-page.md").write_text("# Brought Back\nWritten in an editor.\n")

    landed = wikis.import_markdown(str(docs), into="main")

    assert landed["slug"] == "main"
    assert set(landed["updated"]) == {"hōkūleʻas-voyage", "navigation",
                                      "star-compass"}
    assert landed["created"] == ["new-page"]
    db.current_wiki.set("main")
    assert "edited in the repo" in store.get_page("navigation")["markdown"]
    assert any("stars and swell" in v["markdown"]
               for v in [store.get_version(v["id"])
                         for v in store.page_versions("navigation")])
    assert store.get_page("new-page")["title"] == "Brought Back"


def test_a_merge_never_deletes_and_never_flattens(wiki, tmp_path):
    """Absent is not deleted, and "doesn't say" is not "top level".

    Markdown cannot express either, so a folder that is missing a page leaves it
    alone, and a file with no `parent:` leaves an existing page where it is
    rather than restructuring a wiki the folder never claimed to describe.
    """
    _populate()
    docs = tmp_path / "partial"
    docs.mkdir()
    (docs / "star-compass.md").write_text("# Star Compass\nRewritten.\n")

    wikis.import_markdown(str(docs), into="main")

    db.current_wiki.set("main")
    assert store.get_page("hōkūleʻas-voyage")          # untouched, not deleted
    pages = {p["slug"]: p for p in store.list_pages(include_children=True)}
    assert pages["star-compass"]["parent_slug"] == "navigation"


# --- Slugs, titles and collisions --------------------------------------------

def test_the_filename_is_the_slug_and_the_title_falls_back(wiki, tmp_path):
    """A hand-written folder Jamgraf never exported still imports."""
    docs = tmp_path / "hand-written"
    docs.mkdir()
    (docs / "Getting Started.md").write_text("# Getting started\nRun it.\n")
    (docs / "release-notes.md").write_text("Just prose, no heading.\n")
    (docs / "shell.md").write_text("```sh\n# not a title\n```\n")

    wikis.import_markdown(str(docs), name="Handwritten")
    db.current_wiki.set("handwritten")

    assert store.get_page("getting-started")["title"] == "Getting started"
    assert store.get_page("release-notes")["title"] == "Release notes"
    # A `#` comment inside a fence is not the page's title.
    assert store.get_page("shell")["title"] == "Shell"
    # Text Jamgraf did not write comes back unreformatted.
    assert store.get_page("shell")["markdown"] == "```sh\n# not a title\n```\n"


def test_two_files_claiming_one_slug_are_refused_before_any_write(wiki, tmp_path):
    """"Refused" is a promise that nothing changed, so it has to be true."""
    docs = tmp_path / "clashing"
    (docs / "guides").mkdir(parents=True)
    (docs / "setup.md").write_text("# Setup\nOne.\n")
    (docs / "guides" / "Setup.md").write_text("# Setup\nTwo.\n")
    before = {w["slug"] for w in wikis.list_wikis()}

    with pytest.raises(ValueError) as exc:
        wikis.import_markdown(str(docs), name="Clashing")

    assert "setup" in str(exc.value)
    # No half-made wiki left in the registry for a refusal.
    assert {w["slug"] for w in wikis.list_wikis()} == before


def test_an_empty_folder_says_so(wiki, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    (empty / "README.txt").write_text("not markdown")
    with pytest.raises(ValueError, match="No .md files"):
        wikis.import_markdown(str(empty), name="Nothing")


def test_a_path_that_isnt_there_is_a_refusal_not_a_crash(wiki, tmp_path):
    """Every caller of this reports to a person or an agent; a typo is a refusal."""
    with pytest.raises(ValueError, match="nothing at"):
        wikis.import_markdown(str(tmp_path / "no-such-folder"))


def test_a_parent_that_isnt_there_is_reported_not_raised(wiki, tmp_path):
    """One stale line in a hand-edited folder must not fail the whole import."""
    docs = tmp_path / "orphan"
    docs.mkdir()
    (docs / "child.md").write_text("---\ntitle: Child\nparent: ghost\n---\nText.\n")

    landed = wikis.import_markdown(str(docs), name="Orphans")

    assert landed["unplaced"] == ["child"]
    db.current_wiki.set(landed["slug"])
    assert store.list_pages()[0]["slug"] == "child"      # top level, still here


# --- Reachability: the door a person can actually get to ----------------------

def test_open_takes_a_markdown_zip(wiki, tmp_path):
    """`markdown_zip` is what the Export .md button downloads; Open must read it.

    Rule 12 is about the *pair being reachable*. Sniffed by shape, never by
    name — the file here is called `.wiki` deliberately, the way
    `test_wiki_bundle.py` does it.
    """
    _populate()
    archive = tmp_path / "pretending.wiki"
    archive.write_bytes(wikis.markdown_zip("main"))

    slug = wikis.import_from(str(archive), name="From Zip")

    db.current_wiki.set(slug)
    pages = {p["slug"]: p for p in store.list_pages(include_children=True)}
    assert set(pages) == {"hōkūleʻas-voyage", "navigation", "star-compass"}
    assert pages["star-compass"]["parent_slug"] == "navigation"


def test_the_upload_route_opens_a_markdown_zip_too(wiki, tmp_path):
    """The browser fallback for Open is the other door to the same reader."""
    from fastapi.testclient import TestClient

    from jamgraf.api import app

    _populate()
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        blob = c.get("/wikis/main/export-md")
        assert blob.status_code == 200
        r = c.post("/wikis/import",
                   files={"file": ("main-markdown.zip", io.BytesIO(blob.content),
                                   "application/zip")},
                   follow_redirects=False)

    assert r.status_code == 303, r.status_code
    landed = r.cookies.get("jamgraf_wiki")
    assert landed and wikis.exists(landed), r.headers.get("set-cookie")
    db.current_wiki.set(landed)
    assert store.get_page("navigation")["title"] == "Navigation"


def test_a_markdown_zip_is_never_called_not_a_wiki_file(wiki, tmp_path):
    """The refusal that blamed the file is the bug this rule was written for.

    A zip of markdown is one of the three shapes Open takes, so it must never
    fall through to the wiki-file branch and be reported as a broken wiki file
    — while genuine junk still earns that branch's answer.
    """
    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr("notes.txt", "nothing to see")
    with pytest.raises(ValueError, match="no wiki.db"):
        wikis.import_from(str(junk))

    md = tmp_path / "docs.zip"
    with zipfile.ZipFile(md, "w") as z:
        z.writestr("__MACOSX/._alpha.md", b"\x00resource fork")
        z.writestr("alpha.md", "# Alpha\ntext\n")
    slug = wikis.import_from(str(md), name="Sniffed")   # not "has no wiki.db"
    db.current_wiki.set(slug)
    assert store.get_page("alpha")["title"] == "Alpha"


def test_a_wiki_file_still_wins_over_the_markdown_sniff(wiki, tmp_path):
    """Order matters: a `.wiki` save that happens to carry notes is a wiki file."""
    saved = tmp_path / "backup.wiki"
    _populate()
    wikis.export_to("main", str(saved))
    with zipfile.ZipFile(saved, "a") as z:
        z.writestr("notes.md", "# Stray\na loose file in the archive\n")

    slug = wikis.import_from(str(saved), name="Still A Wiki")

    db.current_wiki.set(slug)
    assert store.get_page("navigation")            # the database, not the .md
    assert store.get_page("notes") is None


# --- The MCP surface ----------------------------------------------------------

def test_mcp_export_and_import_are_a_pair(wiki, tmp_path, monkeypatch):
    """The agent-facing half of the same round-trip (rule 5: one code path)."""
    from jamgraf import mcp_server

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    _populate()
    dest = tmp_path / "repo-docs"

    out = mcp_server.export_markdown(str(dest))
    assert out["written"] == 3

    (dest / "navigation.md").write_text(
        "---\ntitle: Navigation\n---\nEdited by hand in the repo.\n")
    back = mcp_server.import_markdown(str(dest))

    assert back["wiki"] == "main" and back["unplaced"] == []
    db.current_wiki.set("main")
    assert "Edited by hand" in store.get_page("navigation")["markdown"]

    fresh = mcp_server.import_markdown(str(dest), new_wiki="Sidecar")
    assert fresh["wiki"] == "sidecar"
    assert sorted(fresh["created"]) == ["hōkūleʻas-voyage", "navigation",
                                        "star-compass"]


def test_the_export_never_relies_on_the_platform_default_encoding(wiki, tmp_path):
    """Rule 12 is a *pair*, and `open(..., "w")` broke it on one platform only.

    `_markdown_docs` reads strict UTF-8. The writer used the platform default,
    which is UTF-8 on macOS and cp1252 on Windows — so an em dash or an emoji in
    a page was written as something the import half then refuses, and the Mac
    suite could not see it. Asserted by watching the call rather than by reading
    the source: removing `encoding="utf-8"` from `wikis.export_markdown` makes
    this go red on any platform.
    """
    import builtins

    from jamgraf import store, wikis

    store.create_page("Dash", "An em dash — and an emoji 🌋, both non-ASCII.",
                      author="t")

    opened: list[dict] = []
    real_open = builtins.open

    def watching(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            opened.append({"file": str(file), "mode": mode,
                           "encoding": kwargs.get("encoding")})
        return real_open(file, mode, *args, **kwargs)

    builtins.open = watching
    try:
        wikis.export_markdown("main", str(tmp_path / "out"))
    finally:
        builtins.open = real_open

    writes = [o for o in opened if "w" in o["mode"]]
    assert writes, "nothing was written"
    for call in writes:
        assert call["encoding"] == "utf-8", (
            f"{call['file']} was opened without an explicit encoding, so its "
            f"bytes depend on the platform the export happened to run on")

    # And the pair still closes: what was written reads back as what went in.
    exported = sorted((tmp_path / "out").glob("*.md"))
    assert exported
    text = "\n".join(p.read_text(encoding="utf-8") for p in exported)
    assert "—" in text and "🌋" in text
