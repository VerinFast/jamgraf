import zipfile

from jamgraf import store, wikis, db


def test_comments(wiki):
    store.create_page("Doc", "body")
    store.comment_add("doc", "expand this", author="ai")
    c = store.comments_list("doc")
    assert len(c) == 1 and c[0]["body"] == "expand this" and c[0]["resolved"] == 0
    store.comment_resolve(c[0]["id"])
    assert store.comments_list("doc")[0]["resolved"] == 1


def test_suggestion_apply(wiki):
    store.create_page("Spec", "original text")
    s = store.suggestion_add("spec", "rewritten text", note="cleanup", author="ai")
    pending = store.suggestions_list("spec")
    assert len(pending) == 1 and pending[0]["note"] == "cleanup"
    # not applied yet
    assert store.get_page("spec")["markdown"] == "original text"
    store.suggestion_apply(s["id"])
    assert store.get_page("spec")["markdown"] == "rewritten text"
    assert store.suggestions_list("spec") == []          # no longer pending


def test_suggestion_reject(wiki):
    store.create_page("Spec2", "keep me")
    s = store.suggestion_add("spec2", "nope", author="ai")
    store.suggestion_reject(s["id"])
    assert store.get_page("spec2")["markdown"] == "keep me"
    assert store.suggestions_list("spec2") == []


def test_export_markdown(wiki, tmp_path):
    db.current_wiki.set("main")
    store.create_page("Alpha", "# Alpha\ncontent")
    store.create_page("Beta", "# Beta\nmore")
    n = wikis.export_markdown("main", str(tmp_path / "docs"))
    assert n == 2
    # The file leads with the frontmatter header that makes it restorable —
    # the title is a column, not something the body carries (rule 12; the
    # round-trip itself is `tests/test_markdown_roundtrip.py`).
    assert (tmp_path / "docs" / "alpha.md").read_text(encoding="utf-8") == \
        "---\ntitle: Alpha\n---\n# Alpha\ncontent"

    zbytes = wikis.markdown_zip("main")
    import io
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        assert set(z.namelist()) == {"alpha.md", "beta.md"}
