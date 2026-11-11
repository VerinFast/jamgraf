import json
import threading
import time

import anyio
import pytest

from jamgraf import help_content, mcp_server, store


def test_mcp_custom_order_tools(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    for t in ("A", "B", "C"):
        store.create_page(t, "x")
    store.set_page_order(["a", "b", "c"])
    assert mcp_server.list_order()["order"] == ["a", "b", "c"]
    assert mcp_server.get_order("b")["position"] == 1
    assert mcp_server.get_order("missing")["position"] is None
    res = mcp_server.set_order("c", 0)
    assert res["order"] == ["c", "a", "b"] and res["position"] == 0
    assert "error" in mcp_server.set_order("nope", 0)


def _branchy_wiki():
    for title in ("Pantheon", "Villages", "Loner"):
        store.create_page(title, "top")
    store.create_page("Igni", "a god")
    store.create_page("Hamlet", "a village")
    store.set_parent("igni", "pantheon")
    store.set_parent("hamlet", "villages")


def test_mcp_list_pages_children(wiki, monkeypatch):
    """The tool can reach sub-pages, and says how many it withheld (issue #45)."""
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    _branchy_wiki()

    default = mcp_server.list_pages()
    assert {p["slug"] for p in default["pages"]} == {"pantheon", "villages", "loner"}
    # The default still hides children — but never silently.
    assert default["children_hidden"] == 2
    assert "children=true" in default["hint"]

    every = mcp_server.list_pages(children=True)
    assert {"igni", "hamlet"} <= {p["slug"] for p in every["pages"]}
    assert every["children_hidden"] == 0
    assert "hint" not in every
    # null means "everything", same as true.
    assert ({p["slug"] for p in mcp_server.list_pages(children=None)["pages"]}
            == {p["slug"] for p in every["pages"]})

    branch = mcp_server.list_pages(children=["pantheon"])
    slugs = {p["slug"] for p in branch["pages"]}
    assert "igni" in slugs and "hamlet" not in slugs
    assert branch["children_hidden"] == 1            # the villages' child
    assert "unknown_parents" not in branch

    # A parent that doesn't exist is reported, not silently empty.
    bogus = mcp_server.list_pages(children=["pantheon", "no-such-parent"])
    assert bogus["unknown_parents"] == ["no-such-parent"]

    # Rows are navigable by slug: an agent never sees page ids.
    igni = next(p for p in every["pages"] if p["slug"] == "igni")
    assert igni["parent_slug"] == "pantheon"
    assert mcp_server.get_page(igni["parent_slug"])["title"] == "Pantheon"


def test_mcp_list_pages_schema_accepts_every_shape(wiki, monkeypatch):
    """The bool | list[str] | None union must survive MCP schema generation and
    real client-shaped calls — an unusable schema is a broken tool."""
    _branchy_wiki()
    # Act as one identified agent — the seam _SESSION_OVERRIDE exists for, since a
    # tool dispatched through the server looks up its wiki per session.
    monkeypatch.setitem(mcp_server._ACTIVE_BY_SESSION, "test-session", "main")

    async def go():
        tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
        schema = tools["list_pages"].parameters["properties"]["children"]
        assert schema["default"] is False
        types = {b["type"] for b in schema["anyOf"]}
        assert types == {"boolean", "array", "null"}
        out = {}
        for label, args in (("omitted", {}), ("true", {"children": True}),
                            ("false", {"children": False}), ("null", {"children": None}),
                            ("branch", {"children": ["pantheon"]})):
            res = await mcp_server.mcp.call_tool("list_pages", args)
            out[label] = {p["slug"] for p in res.structured_content["pages"]}
        return out

    token = mcp_server._SESSION_OVERRIDE.set("test-session")
    try:
        out = anyio.run(go)
    finally:
        mcp_server._SESSION_OVERRIDE.reset(token)     # never leak into the next test
    assert out["omitted"] == out["false"] == {"pantheon", "villages", "loner"}
    assert out["true"] == out["null"] == out["false"] | {"igni", "hamlet"}
    assert out["branch"] == out["false"] | {"igni"}


def test_in_process_dispatch_survives_a_context_with_no_session(wiki, monkeypatch):
    """A tool dispatched in-process must do its job, not die looking up a session.

    Inside `mcp.call_tool` there IS a context but there is NO session, and
    FastMCP's `Context.session_id` is a property that raises in that state.
    `_session_key()` read it as `getattr(ctx, "session_id", None)` outside its
    try/except — and `getattr`'s default only swallows AttributeError — so the
    RuntimeError escaped and every in-process call failed with a ToolError about
    sessions instead of answering (issue #52). Over stdio a session always
    exists, which is why it stayed invisible.

    Deliberately NO `_SESSION_OVERRIDE` here: setting it makes `_session_key()`
    return before it ever reaches that read, so a test that sets it cannot catch
    this bug — which is exactly why the tests that use the override didn't.
    """
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    monkeypatch.setattr(mcp_server, "_ACTIVE_BY_SESSION", {})
    store.create_page("Sessionless", "written by a caller with no session")
    assert mcp_server._SESSION_OVERRIDE.get() is None      # the seam stays unused

    async def go():
        return (await mcp_server.mcp.call_tool("list_wikis", {}),
                await mcp_server.mcp.call_tool("list_pages", {}))

    listed, pages = anyio.run(go)
    assert listed.structured_content["active"] == "main"
    assert {p["slug"] for p in pages.structured_content["pages"]} == {"sessionless"}
    # A caller with no session falls back to the process-local active wiki, and
    # must NOT mint a session entry: distinct sessions sharing one pointer is
    # the shape that wrote pages into the wrong wiki (issue #11, rule 4).
    assert mcp_server._ACTIVE_BY_SESSION == {}


def test_link_following_is_asked_for_in_the_prompt_surface():
    """Agents follow links when something asks them to, and the asking lives in
    prompt text a refactor can silently drop (issue #47). Assert on what the
    client is actually served: the server instructions, which land before the
    first tool call, and get_page's description, which lands at it."""
    async def descriptions():
        return {t.name: t.description or "" for t in await mcp_server.mcp.list_tools()}

    served = anyio.run(descriptions)
    instructions = mcp_server.mcp.instructions

    for text in (instructions, served["get_page"]):
        low = text.lower()
        assert "links" in low
        # Concrete about the moment, like the staleness guidance next to it —
        # not a general plea to be thorough.
        assert "before you write" in low and "edit" in low
        # ...and concrete about the move: fetch the linked page.
        assert "read the linked page" in low or "get_page on that" in low

    # The staleness nudge this one is modelled on must still be there too: the
    # two are the same lesson (the page in your transcript isn't the whole truth).
    assert "check_pages" in instructions


def test_search_points_at_search_subpages_for_a_branch():
    """Search is deliberately two-step so an agent isn't handed a 215-page result
    set — but the second step only exists for an agent who is told about it
    (issue #59). `search` returning results is not itself evidence anything is
    missing, so the telling lives in the descriptions the client is served, where
    a refactor can silently drop it. Assert it is still there, in the hint voice
    the page tools share: name what is NOT in front of the agent, then the single
    move that fetches it."""
    async def descriptions():
        return {t.name: t.description or "" for t in await mcp_server.mcp.list_tools()}

    served = anyio.run(descriptions)
    search, subpages, listing = (served["search"], served["search_subpages"],
                                 served["list_pages"])

    low = search.lower()
    # What these results are, and what they therefore leave out...
    assert "top-level" in low and "sub-page" in low
    # ...and the one move that goes deeper, named with its argument.
    assert "search_subpages(parent_slug, query)" in search

    # The other end points back, so an agent arriving from either side reads one
    # story rather than two unrelated notices.
    assert "search" in subpages.lower() and "children_hidden" in subpages
    # list_pages is where an agent learns a branch has depth; it names the tool
    # that searches one.
    assert "children_hidden" in listing and "search_subpages" in listing


class _Resp:
    """Minimal stand-in for the httpx response /api/collab/{slug}/live returns."""

    def __init__(self, markdown):
        self.status_code = 200
        self._markdown = markdown

    def json(self):
        return {"markdown": self._markdown}


@pytest.fixture
def linked_wiki(wiki, monkeypatch, fake_live_http):
    """A small wiki with links, frontmatter and a red link, acting as one agent.

    Offline by default so no dev server on the default port can answer for it; a
    test that wants unsaved live text re-installs the fake with its own handler.
    """
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    fake_live_http()
    store.create_page(
        "Igni",
        "---\nstatus: draft\ntags: fire, gods\n---\n\n# Igni\n\n"
        "The fire god, brother of [[Meru]] and enemy of [[Corliss]]. "
        "Again [[Meru|the mountain]], and again [[Meru]].",
    )
    store.create_page("Meru", "# Meru\n\nA peak that watches [[Igni]].")
    store.create_page("Thane", "# Thane\n\nNo links here.")


def test_mcp_read_pages_batches_hits_and_misses(linked_wiki):
    """One bad slug must not discard the good pages (issue #48)."""
    res = mcp_server.read_pages(["igni", "corliss", "meru"])

    assert list(res["pages"]) == ["igni", "meru"]        # asked-for order kept
    assert res["missing"] == ["corliss"]
    assert res["wiki"] == "main"
    assert "error" not in res and "dropped" not in res
    assert res["pages"]["igni"]["title"] == "Igni"
    assert res["pages"]["meru"]["markdown"].startswith("# Meru")

    # Empty input is an error rather than a silent empty batch...
    assert "error" in mcp_server.read_pages([])
    # ...but a batch of only-missing slugs is a legitimate answer.
    only_missing = mcp_server.read_pages(["corliss", "nowhere"])
    assert only_missing["pages"] == {} and only_missing["missing"] == ["corliss", "nowhere"]


def test_mcp_read_pages_caps_and_says_what_it_dropped(wiki, monkeypatch, fake_live_http):
    """Over the cap, the extra slugs come back named — never silently truncated."""
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    fake_live_http()
    slugs = [f"p{i}" for i in range(mcp_server.READ_PAGES_MAX + 3)]
    for s in slugs:
        store.create_page(s.upper(), f"page {s}")

    res = mcp_server.read_pages(slugs)
    assert len(res["pages"]) == mcp_server.READ_PAGES_MAX
    assert list(res["pages"]) == slugs[:mcp_server.READ_PAGES_MAX]
    assert res["dropped"] == slugs[mcp_server.READ_PAGES_MAX:]
    assert "dropped" in res["hint"] and "read_pages" in res["hint"]
    # The cap is documented where the caller will actually read it: the tool
    # description the MCP client is served.
    async def served():
        return {t.name: t.description or "" for t in await mcp_server.mcp.list_tools()}

    desc = anyio.run(served)["read_pages"]
    assert str(mcp_server.READ_PAGES_MAX) in desc and "missing" in desc

    # Duplicates collapse before the cap applies, so they can't crowd it out.
    dupes = mcp_server.read_pages(["p0"] * 5 + ["p1"])
    assert list(dupes["pages"]) == ["p0", "p1"] and "dropped" not in dupes


def test_mcp_read_pages_is_identical_to_get_page(linked_wiki, fake_live_http):
    """Same page, either tool, byte-identical — including the live overlay and
    the `links`/`hint` computed from it. Two shapes would mean an agent's answer
    depended on which tool it happened to reach for (issue #48)."""
    # Snapshot the saved text here: the fake runs on worker threads, which get no
    # DB connection and no active wiki of their own.
    saved = {s: store.get_page(s)["markdown"] for s in ("igni", "meru", "thane")}

    def fake_get(url, **kw):
        return _Resp(saved[url.rsplit("/", 2)[-2]] + "\n\nUnsaved: [[Thane]].")

    fake_live_http(fake_get)

    batch = mcp_server.read_pages(["igni", "meru", "thane"])
    for slug in ("igni", "meru", "thane"):
        assert json.dumps(batch["pages"][slug], sort_keys=True) == \
            json.dumps(mcp_server.get_page(slug), sort_keys=True)

    igni = batch["pages"]["igni"]
    assert igni["live"] is True and "Unsaved" in igni["markdown"]
    # The live text is what `links` and `version` describe, in both tools.
    assert {r["target"] for r in igni["links"]} == {"meru", "corliss", "thane"}
    assert igni["version"] == store.content_version(igni["markdown"])
    assert igni["properties"] == {"status": "draft"} and igni["tags"] == ["fire", "gods"]
    assert "hint" in igni and "hint" not in batch


def test_mcp_read_pages_logs_one_read_not_one_per_page(linked_wiki):
    """A batch read is one entry in the activity feed, not N (issue #48)."""
    def ai_reads():
        return sum(d["ai_read"] for d in store.activity_last_7_days())

    before = ai_reads()
    mcp_server.read_pages(["igni", "meru", "thane"])
    assert ai_reads() == before + 1


def test_mcp_read_pages_fetches_live_text_concurrently(linked_wiki, fake_live_http):
    """The live-edit fetch is one HTTP call per page. Serially, a batch of N is
    slower than the N get_page calls it replaces, which defeats the tool. Assert
    the calls actually overlap rather than timing them (issue #48)."""
    for i in range(5):
        store.create_page(f"Extra {i}", "filler")

    slugs = ["igni", "meru", "thane"] + [f"extra-{i}" for i in range(5)]
    saved = {s: store.get_page(s)["markdown"] for s in slugs}   # see note above
    lock = threading.Lock()
    state = {"now": 0, "peak": 0, "calls": 0}

    def fake_get(url, **kw):
        with lock:
            state["now"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return _Resp(saved[url.rsplit("/", 2)[-2]])

    fake_live_http(fake_get)

    started = time.monotonic()
    res = mcp_server.read_pages(slugs)
    elapsed = time.monotonic() - started

    assert len(res["pages"]) == 8 and state["calls"] == 8   # still one fetch per page
    # `peak` is the property: more than one fetch was in flight at once, which
    # serial execution cannot produce however slow the machine is.
    assert state["peak"] > 1, "live fetches ran one at a time"
    # The clock is a second opinion, not the test. 8 x 50ms serial is 400ms and
    # concurrent is ~50ms, so the bound only has to sit between them — it was
    # 0.25s, and a Windows CI runner took 0.26s and failed a test about
    # concurrency for being slow. Half of serial still bites, without that.
    assert elapsed < 0.2 * 8 * 0.5, \
        f"batch of 8 took {elapsed:.2f}s — fetches look serial"

    # A single-page batch must not pay for a thread pool it doesn't need.
    assert mcp_server.read_pages(["igni"])["pages"]["igni"]["slug"] == "igni"


def test_mcp_docs_tools(wiki):
    help_content.seed()
    slugs = [d["slug"] for d in mcp_server.list_docs()["docs"]]
    assert "templates" in slugs and "getting-started" in slugs
    doc = mcp_server.read_doc("templates")
    assert doc["title"] == "Templates" and "create_from_template" in doc["markdown"]
    assert "error" in mcp_server.read_doc("no-such-doc")
