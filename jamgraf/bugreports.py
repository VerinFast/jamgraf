"""Bug reports an agent filed, queued for a person to review before they go out.

An agent hits a limitation at the moment it has everything needed to describe it
-- the exact tool call, the arguments, the error -- and none of that survives to
a bug report unless it can be captured right there (issue #89). So the MCP server
has a ``report_bug`` tool, and this module is where those reports wait.

Why they wait, rather than being filed
--------------------------------------
Publishing to a public tracker is not the kind of side effect a tool call should
have on its own, and there are two separate reasons, only one of which is about
credentials:

* **The credential.** Filing directly means a GitHub token living in the app.
  We have somewhere safe to put one now (:mod:`secretstore`), so this is
  possible -- but a tool call that can publish to a public tracker is a
  different class of power from one that syncs the user's own wiki, and the
  cheapest way to get that wrong is to build it at all.
* **What ends up in the report.** An agent is at maximum context exactly when it
  is most likely to paste *wiki content* into one -- a failing ``edit_page``
  carries the page's text with it. On a public tracker that is a privacy leak,
  not merely noise, and no amount of prompting reliably prevents it. A person
  reading the report before it is submitted is the actual mitigation.

So nothing here reaches the network. :func:`github_url` builds a link that
pre-fills GitHub's own *new issue* form; the person clicks it, reads what is
about to be published in GitHub's editor, and submits it themselves. No token,
no publishing from a tool call, and the review step is somewhere they cannot
miss it.

Storage is ``DATA_DIR/bug_reports.json`` -- app-global, deliberately not the
wiki's own database, which is per-wiki and is what "Save wiki" exports.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from urllib.parse import quote

from . import config

_lock = threading.Lock()

REPO = config.env("ISSUES_REPO", "VerinFast/jamgraf")

MAX_REPORTS = 50
"""Kept per install. An agent in a retry loop must not fill the disk; past this
the oldest is dropped, because the newest report is the one still reproducible."""

MAX_BODY = 20_000
"""Characters kept per report. Long enough for a stack trace and the arguments,
short enough that one runaway report cannot bloat the file."""

# GitHub's new-issue form takes the body as a query parameter, and a URL that
# grows past roughly this gets rejected or truncated by something along the way
# (browsers, GitHub, or a proxy -- they disagree about exactly where). Past it we
# link to an empty form and the person copies the body from the page instead: a
# truncated bug report that *looks* complete is worse than an obvious two-step.
_URL_BUDGET = 6000


def _path():
    return config.DATA_DIR / "bug_reports.json"


def _load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save(reports: list[dict]) -> None:
    """Replace the file atomically, as the wiki registry does.

    A torn write here reads back as "no reports", which would silently lose
    every queued one; write beside it and rename.
    """
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(reports, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def add(title: str, body: str, wiki: str = "", tool: str = "") -> dict:
    """Queue a report. Returns it, including the id needed to discard it."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "a report needs a title"}

    report = {
        "id": uuid.uuid4().hex[:12],
        "title": title[:200],
        "body": (body or "").strip()[:MAX_BODY],
        "wiki": wiki,
        "tool": tool,
        "created": time.time(),
        "version": _version(),
    }
    with _lock:
        reports = _load()
        reports.append(report)
        _save(reports[-MAX_REPORTS:])
    return {"ok": True, "error": "", "report": report}


def listing() -> list[dict]:
    """Queued reports, newest first, each with its ready-to-open GitHub link."""
    out = []
    for report in reversed(_load()):
        item = dict(report)
        item["url"], item["body_fits"] = github_url(report)
        item["full_body"] = compose_body(report)
        out.append(item)
    return out


def count() -> int:
    return len(_load())


def discard(report_id: str) -> bool:
    with _lock:
        reports = _load()
        keep = [r for r in reports if r.get("id") != report_id]
        if len(keep) == len(reports):
            return False
        _save(keep)
    return True


def compose_body(report: dict) -> str:
    """The report plus the context the server already knew.

    Version, active wiki and the failing tool are things the agent would have to
    be told to include and would sometimes get wrong, so they are added here
    rather than trusted to the caller.
    """
    lines = [report.get("body") or ""]
    facts = [("Jamgraf", report.get("version") or "unknown")]
    if report.get("wiki"):
        facts.append(("Wiki", report["wiki"]))
    if report.get("tool"):
        facts.append(("Tool", report["tool"]))
    lines.append("")
    lines.append("---")
    lines.append("_Reported by an agent through Jamgraf's MCP server._")
    lines.append("")
    lines.extend(f"- **{k}:** {v}" for k, v in facts)
    return "\n".join(lines).strip()


def github_url(report: dict) -> tuple[str, bool]:
    """(link to GitHub's new-issue form, whether the body fitted in it).

    When the body doesn't fit, the link opens an empty form and the caller shows
    the text to copy. Truncating it silently would produce a bug report that
    reads as complete and isn't.
    """
    base = f"https://github.com/{REPO}/issues/new"
    title = quote(report.get("title") or "", safe="")
    body = quote(compose_body(report), safe="")
    full = f"{base}?title={title}&body={body}"
    if len(full) <= _URL_BUDGET:
        return full, True
    return f"{base}?title={title}", False


def _version() -> str:
    from . import __version__
    return __version__
