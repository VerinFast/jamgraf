"""Guards for RFC 0001, Phase 0: routes own routing + pydantic only; all data
access goes through the ``store`` repository chokepoint, never raw SQL/cursors.

The structural test is the load-bearing one: it fails the build if a future
route handler reaches past the repository into a cursor or embeds SQL. That seam
is where per-wiki / tenant scoping (WikiScope + RLS) attaches in a later phase,
so keeping it clean now is a correctness (isolation) property, not just tidiness.
"""
from __future__ import annotations

import re
from pathlib import Path

from jamgraf import db, store

API_SRC = (Path(__file__).resolve().parent.parent / "jamgraf" / "api.py").read_text(encoding="utf-8")

# SQL verbs and the cursor accessor that must never appear in the route module.
_SQL = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b")
_CURSOR = re.compile(r"\bget_conn\s*\(|\.execute(?:many|script)?\s*\(|"
                     r"\.fetch(?:one|all)\s*\(")


def test_routes_contain_no_raw_sql():
    """No SQL string literals in the HTTP route module."""
    offenders = [ln for ln in API_SRC.splitlines() if _SQL.search(ln)]
    assert not offenders, f"raw SQL found in api.py routes: {offenders}"


def test_routes_open_no_cursor():
    """Routes call the repository; they never open a DB cursor themselves."""
    offenders = [ln for ln in API_SRC.splitlines() if _CURSOR.search(ln)]
    assert not offenders, f"direct cursor use found in api.py routes: {offenders}"


def test_routes_do_not_reach_into_db_for_settings():
    """Settings reads/writes in routes go through the repository, not ``db``."""
    assert "db.get_setting(" not in API_SRC
    assert "db.set_setting(" not in API_SRC
    assert "db.all_settings(" not in API_SRC


def test_parent_of_repository_lookup(wiki):
    """store.parent_of owns the parent-page lookup routes used to run inline."""
    assert store.parent_of(None) is None
    parent = store.create_page("Parent Topic", "root")
    child = store.create_page("Child Topic", "leaf")
    store.set_parent(child["slug"], parent["slug"])

    child = store.get_page(child["slug"])
    got = store.parent_of(child)
    assert got is not None and got["slug"] == parent["slug"]
    assert got["title"] == "Parent Topic"

    # A top-level page has no parent.
    assert store.parent_of(store.get_page(parent["slug"])) is None


def test_settings_repository_delegates_to_db(wiki):
    """The repository settings seam round-trips and agrees with the db layer."""
    assert store.get_setting("nope") is None
    assert store.get_setting("nope", "fallback") == "fallback"
    store.set_setting("chokepoint_probe", "on")
    assert store.get_setting("chokepoint_probe") == "on"
    assert db.get_setting("chokepoint_probe") == "on"  # same underlying store
    assert store.all_settings()["chokepoint_probe"] == "on"
