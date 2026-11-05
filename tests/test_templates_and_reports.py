"""Two MCP surface gaps: reading a template (#88) and filing a bug (#89).

Both are about an agent not being able to say something it knows. #88: it can
overwrite a template but never read one, so "change this one line" means
reconstructing the body from a page and losing whatever that page didn't show.
#89: it hits a bug holding the exact failing call, and has nowhere to put it.

The bug-report tests are mostly about what does NOT happen. Nothing in
``bugreports`` may reach the network: the queue exists precisely so a tool call
cannot publish to a public tracker, and so a person sees what an agent wrote
before the world does.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from jamgraf import bugreports, config, store


# --- #88: reading and editing a template -------------------------------------


def test_a_template_can_be_read_back(wiki):
    store.template_save("Person", "# {{title}}\n\nBorn:\n", meta_schema="born: date")
    got = store.template_by_name("Person")
    assert got["markdown"] == "# {{title}}\n\nBorn:\n"
    assert got["meta_schema"] == "born: date"


def test_editing_a_template_leaves_the_rest_of_it_alone(wiki):
    """The actual bug: the only way to change one line was to replace all of them."""
    body = "# {{title}}\n\nBorn:\nLives:\nWorks:\n"
    store.template_save("Person", body, meta_schema="born: date")

    out = store.template_edit("Person", "Lives:\n", "Lives:\nTV shows:\n")
    assert out["ok"], out.get("error")

    after = store.template_by_name("Person")
    assert "TV shows:" in after["markdown"]
    for kept in ("# {{title}}", "Born:", "Works:"):
        assert kept in after["markdown"], f"{kept!r} was lost by a targeted edit"
    assert after["meta_schema"] == "born: date", \
        "the metadata schema a human authored was dropped by a body edit"


def test_an_ambiguous_edit_is_refused_rather_than_guessed(wiki):
    store.template_save("Person", "Name:\nName:\n")
    out = store.template_edit("Person", "Name:\n", "Full name:\n")
    assert not out["ok"] and "ambiguous" in out["error"]
    assert store.template_by_name("Person")["markdown"] == "Name:\nName:\n", \
        "an ambiguous edit changed the template anyway"


def test_an_edit_against_stale_text_is_refused(wiki):
    store.template_save("Person", "Born:\n")
    out = store.template_edit("Person", "Died:\n", "x")
    assert not out["ok"] and "does not appear" in out["error"]


def test_editing_a_template_that_does_not_exist_says_so(wiki):
    out = store.template_edit("Nope", "a", "b")
    assert not out["ok"] and "no template" in out["error"]


def test_the_mcp_surface_can_read_before_it_overwrites(wiki):
    """#88 in one line: create_template exists, so a reader must too."""
    from jamgraf import mcp_server

    for tool in ("get_template", "edit_template", "create_template",
                 "list_templates"):
        assert hasattr(mcp_server, tool), f"{tool} is missing from the MCP surface"


# --- #89: the bug-report queue ------------------------------------------------


@pytest.fixture
def queue(wiki, monkeypatch):
    """An empty queue in this test's own data dir."""
    assert not bugreports.listing()
    return bugreports


def test_a_report_is_queued_and_never_sent(queue, monkeypatch):
    """The whole design: a tool call must not publish to a public tracker.

    Patched at the HTTP boundary rather than trusting the implementation, so a
    future change that adds a request here fails loudly instead of quietly
    filing issues on someone's behalf.
    """
    import httpx

    def no_network(*a, **k):
        raise AssertionError("bugreports must never reach the network")

    monkeypatch.setattr(httpx, "Client", no_network)
    monkeypatch.setattr(httpx, "post", no_network)

    out = bugreports.add("It broke", "steps here", wiki="family", tool="edit_page")
    assert out["ok"]
    assert bugreports.count() == 1


def test_the_queue_is_not_in_the_wiki_file(queue):
    """A per-wiki home would ship agent reports inside an exported wiki."""
    bugreports.add("It broke", "steps")
    assert (config.DATA_DIR / "bug_reports.json").exists()
    assert not any("It broke" in str(v) for v in store.all_settings().values())


def test_the_report_carries_the_context_the_server_knew(queue):
    bugreports.add("It broke", "steps", wiki="family", tool="edit_page")
    body = bugreports.listing()[0]["full_body"]
    assert "family" in body and "edit_page" in body
    assert "Jamgraf" in body


def test_the_github_link_prefills_the_form_and_submits_nothing(queue):
    bugreports.add("Title here", "Body here")
    item = bugreports.listing()[0]
    parts = urlparse(item["url"])
    assert parts.netloc == "github.com"
    assert parts.path.endswith("/issues/new"), \
        "the link must open GitHub's form, not post anything"
    query = parse_qs(parts.query)
    assert query["title"] == ["Title here"]
    assert "Body here" in query["body"][0]


def test_a_body_too_long_for_a_url_is_reported_not_truncated(queue):
    """A truncated report that reads as complete is worse than an obvious
    two-step."""
    bugreports.add("Long one", "x" * 12_000)
    item = bugreports.listing()[0]
    assert item["body_fits"] is False
    assert "body=" not in item["url"], "a body was crammed into an over-long URL"
    assert "x" * 12_000 in item["full_body"], \
        "the full text must still be available to copy"


def test_a_runaway_agent_cannot_fill_the_disk(queue):
    for n in range(bugreports.MAX_REPORTS + 10):
        bugreports.add(f"Report {n}", "body")
    assert bugreports.count() == bugreports.MAX_REPORTS
    # The newest survive: those are the ones still reproducible.
    assert bugreports.listing()[0]["title"] == \
        f"Report {bugreports.MAX_REPORTS + 9}"


def test_an_oversized_body_is_capped(queue):
    bugreports.add("Big", "y" * (bugreports.MAX_BODY * 2))
    assert len(json.loads((config.DATA_DIR / "bug_reports.json").read_text(encoding="utf-8"))
               [0]["body"]) == bugreports.MAX_BODY


def test_a_report_needs_a_title(queue):
    assert not bugreports.add("   ", "body")["ok"]
    assert bugreports.count() == 0


def test_discarding_removes_exactly_one(queue):
    bugreports.add("First", "a")
    bugreports.add("Second", "b")
    target = bugreports.listing()[0]["id"]
    assert bugreports.discard(target)
    assert [r["title"] for r in bugreports.listing()] == ["First"]
    assert not bugreports.discard(target), "discarding twice reported success"


def test_a_torn_file_reads_as_empty_rather_than_raising(queue):
    (config.DATA_DIR / "bug_reports.json").write_text("{ not json")
    assert bugreports.listing() == []
    assert bugreports.add("Still works", "b")["ok"]


# --- the pane -----------------------------------------------------------------


def test_the_reports_pane_renders_and_shows_the_warning(wiki):
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    bugreports.add("Something failed", "it did", wiki="family", tool="edit_page")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/reports")
        assert out.status_code == 200
        assert "Something failed" in out.text
        assert "public tracker" in out.text, \
            "nothing warns that these reports may quote page content"


def test_the_nav_entry_appears_only_when_something_is_queued(wiki):
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert 'href="/reports"' not in client.get("/").text
        bugreports.add("Something failed", "it did")
        assert 'href="/reports"' in client.get("/").text


def test_a_guest_cannot_read_other_peoples_bug_reports():
    from jamgraf import auth
    assert not auth.guest_may("/reports")
    assert not auth.guest_may("/reports/abc/discard")
