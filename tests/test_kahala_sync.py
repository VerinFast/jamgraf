"""Kahala sync (issue #58): the credential, the wire, and the refusals.

This is an **auth + outbound network** path, so most of what is worth testing
here is what it refuses to do. Two properties are load-bearing enough that the
tests are written to fail if someone "simplifies" them away:

* a bearer token is never handed to a host we did not mean to talk to, which in
  practice means a 3xx is *reported*, never followed (``httpx`` re-sends the
  ``Authorization`` header across a redirect);
* the refresh token never lands in a wiki file or in ``app_config.json``, only
  in the Keychain -- because the wiki file *is* what "Save wiki" exports.

The HTTP fake replaces only the **transport**, never ``kahala._client`` itself.
That is deliberate: patching the client would also replace the arguments the
client is constructed with, and ``follow_redirects=False`` is one of those
arguments. Faking a layer lower means the redirect test exercises the real
setting -- flip it to True in ``kahala._client`` and this file goes red.
"""
from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from jamgraf import (config, db, kahala, kahalaauth, secretstore, store,
                     wikis)
from jamgraf.vendor import wiki_interchange as wi


# --- fakes -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Never touch the developer's actual Keychain, in either direction."""
    vault: dict[str, str] = {}
    monkeypatch.setattr(secretstore, "available", lambda: True)
    monkeypatch.setattr(secretstore, "set_secret",
                        lambda a, s: vault.__setitem__(a, s) or True)
    monkeypatch.setattr(secretstore, "get_secret", lambda a: vault.get(a))
    monkeypatch.setattr(secretstore, "delete_secret",
                        lambda a: vault.pop(a, None) is not None)
    kahalaauth.forget()
    yield vault
    kahalaauth.forget()


@pytest.fixture(autouse=True)
def _no_network_by_default(monkeypatch):
    """Structural, like conftest's update-check guard: no accidental egress.

    A test that wants HTTP installs a handler with ``http``. Anything else that
    reaches the network fails loudly instead of quietly hitting a real host.
    """
    real = httpx.Client

    def refuse(*a, **kw):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx, "Client", refuse)
    return real


@pytest.fixture
def http(monkeypatch, _no_network_by_default):
    """Install a request handler, keeping the real client's own arguments."""
    real = _no_network_by_default
    seen: list[httpx.Request] = []

    def install(handler):
        def make(*a, **kw):
            def wrapped(request):
                seen.append(request)
                return handler(request)
            kw["transport"] = httpx.MockTransport(wrapped)
            return real(*a, **kw)
        monkeypatch.setattr(httpx, "Client", make)
        return seen

    install.seen = seen
    return install


def _signed_in(monkeypatch):
    monkeypatch.setattr(kahalaauth, "access_token", lambda: "test-access-token")


def _bundle_of(slug: str) -> bytes:
    """A real interchange bundle produced from a real local wiki."""
    token = db.current_wiki.set(slug)
    try:
        return store.export_wiki_bundle()
    finally:
        db.current_wiki.reset(token)


# --- the credential ----------------------------------------------------------


def test_the_refresh_token_never_lands_in_a_wiki_or_in_app_config(
        wiki, http, monkeypatch, tmp_path):
    """The wiki file IS the export, so a token in it is a token you hand out."""
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(200, json={"access_token": "at",
                                         "refresh_token": "SECRET-REFRESH",
                                         "expires_in": 300})
    http(handler)

    url, state = kahalaauth.begin()
    assert "code_challenge_method=S256" in url
    assert kahalaauth.complete("the-code", state)["ok"]

    # Check the settings table through SQL, not by reading the .db file: in WAL
    # mode a just-written row still lives in the -wal sidecar, so a raw byte
    # scan of the .db passes while the token is very much in the wiki. (That is
    # not hypothetical -- the first version of this test did exactly that and
    # stayed green with the token stored via `store.set_setting`.)
    assert not any("SECRET-REFRESH" in str(v)
                   for v in store.all_settings().values()), \
        "the refresh token is in the wiki's settings table"

    # Then the claim that actually matters: it is not in what "Save wiki" hands
    # over. This is the real export path, WAL checkpointed and all.
    saved = tmp_path / "shared.wiki"
    wikis.export_to("main", str(saved))
    assert b"SECRET-REFRESH" not in saved.read_bytes(), \
        "the refresh token travels inside an exported wiki, which is exactly " \
        "what gets handed to whoever you share it with"

    cfg = config.DATA_DIR / "app_config.json"
    if cfg.exists():
        assert "SECRET-REFRESH" not in cfg.read_text(encoding="utf-8")
    assert "SECRET-REFRESH" not in (config.DATA_DIR / "wikis.json").read_text(encoding="utf-8")


def test_no_secure_store_means_no_sign_in_rather_than_a_file(monkeypatch):
    monkeypatch.setattr(secretstore, "available", lambda: False)
    ok, why = kahalaauth.can_sign_in()
    assert not ok
    # Names the store this machine would have used, whichever that is, and does
    # not offer a file. Asserting the word "Keychain" made this a macOS test
    # that would fail on the Windows runner for being right.
    assert secretstore.store_name().split()[-1] in why
    assert "file" not in why.lower() or "no secure place" in why
    with pytest.raises(ValueError):
        kahalaauth.begin()


def test_a_failed_write_on_rotation_signs_out_instead_of_keeping_a_dead_token(
        wiki, http, monkeypatch, _no_real_keychain):
    """Keycloak rotates, so the token we just spent is already dead.

    If the new one cannot be stored there is nothing left to stay signed in
    with. Keeping the spent one leaves an account that *reads* as signed in and
    fails every later refresh, with nothing on screen explaining why -- so the
    stored credential goes and the state reported is the true one.
    """
    vault = _no_real_keychain
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    vault[kahalaauth._account()] = "spent-refresh-token"
    assert kahalaauth.signed_in(), "the test did not manage to seed a sign-in"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(200, json={"access_token": "at",
                                         "refresh_token": "rotated-refresh",
                                         "expires_in": 300})
    http(handler)
    monkeypatch.setattr(secretstore, "set_secret", lambda a, s: False)

    assert kahalaauth.access_token() is None, \
        "a token was handed out while the rotated refresh token was lost"
    assert not kahalaauth.signed_in(), \
        "the Keychain still holds the spent refresh token, so the app reads " \
        "as signed in and every refresh from here on fails"
    assert "spent-refresh-token" not in vault.values()


def test_a_rejected_refresh_token_is_dropped_rather_than_kept(
        wiki, http, monkeypatch, _no_real_keychain):
    """A dead credential must not survive as a sign-in that reads as live (#100).

    `signed_in` only asks whether the Keychain holds something. If a refresh
    token the server has already rejected stays there, it answers True forever
    while every push and pull fails -- and the pane offers Sign out, which is
    the one button nobody presses when they are being told they are signed in.
    """
    vault = _no_real_keychain
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    vault[kahalaauth._account()] = "expired-refresh-token"
    assert kahalaauth.signed_in(), "the test did not manage to seed a sign-in"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "Token is not active"})
    http(handler)

    assert kahalaauth.access_token() is None
    assert not kahalaauth.signed_in(), \
        "the rejected refresh token is still stored, so the app reads as " \
        "signed in and every push and pull from here on fails"
    assert "expired-refresh-token" not in vault.values()


def test_an_unreachable_sign_in_server_does_not_sign_you_out(
        wiki, http, monkeypatch, _no_real_keychain):
    """The opposite case, and the reason the two are told apart.

    A dropped connection says nothing about the refresh token. Treating it like
    a rejection would cost a sign-in that is still perfectly good every time a
    laptop goes into a tunnel.
    """
    vault = _no_real_keychain
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    vault[kahalaauth._account()] = "good-refresh-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kahala is unreachable")
    http(handler)

    assert kahalaauth.access_token() is None, "no token without the server"
    assert kahalaauth.signed_in(), \
        "a network failure signed the person out of a session they still have"
    assert vault[kahalaauth._account()] == "good-refresh-token"


def test_a_server_error_does_not_sign_you_out(
        wiki, http, monkeypatch, _no_real_keychain):
    """A 5xx is Kahala's problem, not the credential's.

    Keycloak answers 400 for a dead grant *and* for a request we built wrong,
    so the status alone cannot decide this -- the machine-readable `error` can.
    """
    vault = _no_real_keychain
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    vault[kahalaauth._account()] = "good-refresh-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(503, text="upstream is having a day")
    http(handler)

    assert kahalaauth.access_token() is None
    assert kahalaauth.signed_in(), "a 5xx threw away a working refresh token"


def test_a_non_object_error_body_still_reports_as_a_refusal(
        wiki, http, monkeypatch, _no_real_keychain):
    """A JSON error body that isn't an object must not escape as AttributeError.

    `_post_token` promises that a non-200 always arrives as a written reason:
    `_clean` says it is only ever handed this module's own exceptions, and
    `complete` puts that text straight on screen. A bare array or string --
    from a proxy in front of the IdP rather than the IdP itself -- would
    otherwise make `.get` raise out of the JSON guard, and the person signing
    in would be shown "'list' object has no attribute 'get'".

    Driven through `complete` rather than `access_token`, because
    `access_token` catches everything and returns None either way: the sign-in
    flow is where the difference is something a person reads.
    """
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(400, json=["invalid_grant"])
    http(handler)

    _url, state = kahalaauth.begin()
    out = kahalaauth.complete("the-code", state)

    assert not out["ok"]
    assert "400" in out["error"], \
        f"the refusal lost its written reason: {out['error']}"
    assert "attribute" not in out["error"].lower(), \
        f"a Python attribute error reached the sign-in page: {out['error']}"


def test_a_malformed_request_does_not_delete_the_credential(
        wiki, http, monkeypatch, _no_real_keychain):
    """`invalid_request` is our bug, not a dead token (RFC 6749 §5.2).

    Only `invalid_grant` says the stored credential will never work again.
    Treating every 400 as that would sign people out over a request we built
    wrong, which is a bug they cannot see and did not cause.
    """
    vault = _no_real_keychain
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    vault[kahalaauth._account()] = "good-refresh-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": "https://kc.example/auth",
                "token_endpoint": "https://kc.example/token"})
        return httpx.Response(400, json={
            "error": "invalid_request",
            "error_description": "Missing form parameter: grant_type"})
    http(handler)

    assert kahalaauth.access_token() is None
    assert kahalaauth.signed_in(), \
        "a request we built wrong cost the person their sign-in"


def test_a_sign_in_state_is_single_use(wiki, http, monkeypatch):
    """A replayed callback must not complete a second sign-in."""
    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token",
        "access_token": "at", "refresh_token": "rt", "expires_in": 300}))
    _url, state = kahalaauth.begin()
    assert kahalaauth.complete("code", state)["ok"]
    again = kahalaauth.complete("code", state)
    assert not again["ok"] and "already used" in again["error"]


def test_an_unknown_state_is_refused(wiki):
    out = kahalaauth.complete("code", "never-minted-this")
    assert not out["ok"]


# --- the token is never handed to another host -------------------------------


def test_a_redirect_is_reported_not_followed(wiki, http, monkeypatch):
    """The whole point: httpx re-sends Authorization across a redirect.

    Constructing the client with ``follow_redirects=True`` makes this pass a
    302 through to ``evil.example`` with the bearer token attached, so this test
    is what pins that argument in ``kahala._client``.
    """
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kahala.example":
            return httpx.Response(302, headers={
                "location": "https://evil.example/collect"})
        return httpx.Response(200, content=b"should never get here")

    seen = http(handler)
    out = kahala.pull("main")

    assert not out["ok"]
    assert "redirect" in out["error"].lower()
    hosts = {r.url.host for r in seen}
    assert "evil.example" not in hosts, \
        "the redirect was followed, so the bearer token was sent to evil.example"


def test_an_http_address_is_refused_before_any_request(wiki):
    out = kahala.link("main", "http://kahala.example", "remote-wiki")
    assert not out["ok"] and "https" in out["error"]
    assert wikis.get_link("main") is None


def test_loopback_http_is_allowed_for_local_development():
    assert kahalaauth.secure_url("http://127.0.0.1:8787")
    assert kahalaauth.secure_url("https://kahala.example")
    assert not kahalaauth.secure_url("http://kahala.example")
    assert not kahalaauth.secure_url("ftp://kahala.example")


def test_every_request_carries_the_bearer_token(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    seen = http(lambda r: httpx.Response(500, json={"detail": "nope"}))
    kahala.push("main")
    assert seen and all(
        r.headers.get("authorization") == "Bearer test-access-token"
        for r in seen)


# --- the link record ---------------------------------------------------------


def test_the_link_lives_in_the_registry_not_in_the_wiki(wiki):
    """Otherwise it travels to whoever you share the wiki with."""
    assert kahala.link("main", "https://kahala.example", "remote-wiki")["ok"]

    reg = json.loads((config.DATA_DIR / "wikis.json").read_text(encoding="utf-8"))
    entry = [w for w in reg["wikis"] if w["slug"] == "main"][0]
    assert entry["kahala"]["remote"] == "remote-wiki"

    assert b"kahala.example" not in wikis.db_path("main").read_bytes(), \
        "the Kahala address was written into the wiki file, so it would " \
        "travel with an exported wiki and point someone else's copy at it"


def test_unlink_forgets_only_the_link(wiki):
    kahala.link("main", "https://kahala.example", "remote-wiki")
    store.create_page("Kept", "still here")
    kahala.unlink("main")
    assert wikis.get_link("main") is None
    assert store.get_page("kept") is not None


def test_status_reports_without_touching_the_network(wiki, monkeypatch):
    monkeypatch.setattr(kahalaauth, "signed_in", lambda: False)
    out = kahala.status("main")
    assert out["linked"] is False and out["signed_in"] is False
    kahala.link("main", "https://kahala.example", "remote-wiki")
    assert kahala.status("main")["remote"] == "remote-wiki"


@pytest.mark.parametrize("remote", ["team/secret", "wiki?x=1", "wiki#frag",
                                    "..", "a%2Fb", "back\\slash"])
def test_a_remote_name_that_isnt_one_segment_is_refused_and_not_recorded(
        wiki, remote):
    """Told to the person, rather than used to address something else.

    The name is typed by the owner, so this is a typo guard before it is a
    security one -- but the typo lands in a URL that carries a bearer token, so
    it has to be refused where it is entered instead of being sent.
    """
    out = kahala.link("main", "https://kahala.example", remote)
    assert not out["ok"], f"“{remote}” was accepted as a wiki name"
    assert wikis.get_link("main") is None, \
        "a link was recorded for a name that cannot be requested"
    assert not kahala.clone("https://kahala.example", remote)["ok"]


def test_a_stored_remote_name_cannot_change_which_route_is_asked_for(
        wiki, http, monkeypatch):
    """The second guard, for a link recorded before the first one existed.

    ``wikis.set_link`` writes the registry directly, as an older build did. The
    name must still be one path segment on the wire: escaped it addresses a wiki
    that doesn't exist and 404s, unescaped ``httpx`` resolves the ``..`` and the
    request -- with the ``Authorization`` header on it -- arrives at a different
    route on that host entirely.
    """
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "../../admin/wikis")
    seen = http(lambda r: httpx.Response(200, json={"pages": 0}))

    kahala.push("main", full=True)

    assert seen, "nothing was requested at all"
    for request in seen:
        # `raw_path`, not `path`: the latter percent-decodes, so it reads the
        # same whether the name was escaped or resolved away. What went on the
        # wire is what this is about.
        path = request.url.raw_path
        assert path.startswith(b"/api/interchange/wikis/"), \
            f"the remote name steered the request to {path!r}"
        assert b"/admin/" not in path, \
            f"the wiki name escaped its path segment: {path!r}"


# --- the transfers -----------------------------------------------------------


def test_push_sends_a_real_bundle_to_the_snapshot_route(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Travelling", "content that should be in the bundle")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body["url"] = str(request.url)
        body["raw"] = request.read()
        return httpx.Response(200, json={"pages": 1, "images": 0})

    http(handler)
    out = kahala.push("main")

    assert out["ok"], out.get("error")
    assert body["url"] == \
        "https://kahala.example/api/interchange/wikis/remote-wiki/snapshot"
    # It is a real interchange bundle, not an empty multipart shell.
    start = body["raw"].index(b"PK\x03\x04")
    names = zipfile.ZipFile(
        io.BytesIO(body["raw"][start:body["raw"].rindex(b"PK\x05\x06") + 22])
    ).namelist()
    assert "manifest.json" in names
    assert "pages/travelling.snapshot" in names, \
        f"the page never made it into the bundle: {names}"
    assert "deleted" not in out["note"].lower() or "Nothing" in out["note"]


def test_pull_merges_and_deletes_nothing_local(wiki, http, monkeypatch):
    """A pull must never remove a local page the remote doesn't have."""
    _signed_in(monkeypatch)
    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        store.create_page("From Kahala", "arrived from the server")
    finally:
        db.current_wiki.reset(token)
    payload = _bundle_of(other)

    store.create_page("Only Local", "must survive the pull")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(200, content=payload))

    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert store.get_page("only-local") is not None, \
        "a local page the remote never had was removed by a pull"
    assert store.get_page("from-kahala") is not None


def test_clone_creates_a_new_wiki_and_links_it(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        store.create_page("Cloned Page", "hello")
    finally:
        db.current_wiki.reset(token)
    payload = _bundle_of(other)

    before = {w["slug"] for w in wikis.list_wikis()}
    http(lambda r: httpx.Response(200, content=payload))
    out = kahala.clone("https://kahala.example", "remote-wiki", "My Clone")

    assert out["ok"], out.get("error")
    fresh = out["wiki"]
    assert fresh not in before
    assert wikis.get_link(fresh)["remote"] == "remote-wiki"


def test_a_failed_clone_leaves_no_empty_wiki_behind(wiki, http, monkeypatch):
    """An empty wiki reads as "it worked and there was nothing there"."""
    _signed_in(monkeypatch)
    before = {w["slug"] for w in wikis.list_wikis()}
    http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))

    out = kahala.clone("https://kahala.example", "missing", "Doomed")
    assert not out["ok"]
    assert {w["slug"] for w in wikis.list_wikis()} == before, \
        "a failed clone left a wiki behind, which reads as an empty success"


def test_an_unreachable_kahala_changes_nothing_here(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Local", "untouched")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def boom(request):
        raise httpx.ConnectError("no route to host")

    http(boom)
    out = kahala.pull("main")
    assert not out["ok"] and "Couldn't reach" in out["error"]
    assert store.get_page("local") is not None


# --- the refusals say which one it was ---------------------------------------


@pytest.mark.parametrize("code,expect", [
    (401, "expired"),
    (403, "owner or admin"),
    (404, "another tenant"),
    (409, "incompatible"),
])
def test_each_refusal_says_what_to_do_about_it(wiki, http, monkeypatch,
                                               code, expect):
    """Four different problems must not collapse into one message."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(code, json={"detail": "refused"}))

    out = kahala.push("main")
    assert not out["ok"]
    assert expect in out["error"], f"{code} said: {out['error']}"


def test_a_version_mismatch_is_refused_whole(wiki, http, monkeypatch):
    """A bundle we cannot read must not half-apply."""
    _signed_in(monkeypatch)
    store.create_page("Local", "untouched")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(200, content=b"not a bundle at all"))

    out = kahala.pull("main")
    assert not out["ok"] and "refused" in out["error"]
    assert store.get_page("local") is not None


def test_a_part_written_pull_says_so_rather_than_reporting_a_refusal(
        wiki, http, monkeypatch):
    """"Refused" is a promise that nothing changed. Here something did.

    A bad bundle is refused whole by the dry run, and that is the realistic
    failure -- but a local write can fail too (a full disk, a lock), and then
    some pages have merged. The import is deliberately not staged and swapped
    (``docs/data-safety.md`` question 4), so the honest report is "partly
    merged, run it again", and the two cases must not share a sentence.
    """
    _signed_in(monkeypatch)
    other = wikis.create_wiki("Remote Source")
    tok = db.current_wiki.set(other)
    try:
        db.init_db()
        for i in range(1, 5):
            store.create_page(f"Chapter {i}", f"chapter {i} body")
    finally:
        db.current_wiki.reset(tok)
    payload = _bundle_of(other)

    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(200, content=payload))

    real_create = store.create_page
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real_create(*a, **kw)

    # Swapped and restored by hand, not through monkeypatch: undoing a
    # monkeypatch mid-test also undoes what the `wiki` fixture set up with the
    # same instance (``config.DATA_DIR``, ``db._local``), and the assertions
    # below would then be reading a different data directory entirely.
    store.create_page = flaky
    try:
        out = kahala.pull("main", full=True)
    finally:
        store.create_page = real_create

    assert not out["ok"]
    assert "partly merged" in out["error"], \
        f"a part-written import reported itself as refused: {out['error']}"
    assert "again" in out["error"], \
        "the report doesn't say the one thing that fixes it"
    landed = {p["slug"] for p in store.list_pages(include_children=True)}
    assert landed & {"chapter-1", "chapter-2"}, \
        "nothing landed, so this test is no longer about a partial import"


def test_push_and_pull_refuse_before_the_network_when_not_linked(wiki):
    assert "isn't linked" in kahala.push("main")["error"]
    assert "isn't linked" in kahala.pull("main")["error"]


def test_they_refuse_when_signed_out(wiki, monkeypatch):
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    monkeypatch.setattr(kahalaauth, "access_token", lambda: None)
    assert "signed in" in kahala.push("main")["error"]


# --- a LAN guest may not use any of this -------------------------------------


@pytest.mark.parametrize("path", [
    "/kahala", "/kahala/signin", "/kahala/callback", "/kahala/push",
    "/kahala/pull", "/kahala/clone", "/kahala/link", "/kahala/signout",
])
def test_a_guest_cannot_reach_the_kahala_routes(path):
    """Owner-only, and not merely for tidiness.

    A guest who could reach ``/kahala/clone`` or ``/kahala/link`` could point
    this machine at a Kahala *they* name and push the owner's wiki to it. That
    is exfiltration, not a misconfiguration, so it sits with the other
    owner-only paths in ``auth``.
    """
    from jamgraf import auth
    assert not auth.guest_may(path), \
        f"a LAN guest can reach {path}, which is enough to send a wiki off-box"


# --- the pane renders --------------------------------------------------------


def test_the_kahala_pane_renders_in_every_state(wiki, monkeypatch):
    """A template error only shows at render time, so render it.

    Both states matter: signed-out (the sign-in button and no link form) and
    linked (the push/pull buttons plus the merge warning), because the second
    branch is the one nothing else in this file touches.
    """
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "signed_in", lambda: True)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/kahala")
        assert out.status_code == 200
        assert "Sign out" in out.text

        wikis.set_link("main", "https://kahala.example", "remote-wiki")
        out = client.get("/kahala?wiki=main")
        assert out.status_code == 200
        assert "Push to Kahala" in out.text
        # The one sentence that keeps a merge from being a surprise.
        assert "neither deletes" in out.text.lower()


def test_signing_in_does_not_bring_the_old_error_banner_back(wiki, monkeypatch):
    """The reload after a sign-in must drop the flash it was sent here by.

    The pane polls while the sign-in happens in another application, then
    reloads. `location.reload()` re-requests the CURRENT url -- which still
    carries the `?error=you aren't signed in` that sent the person to the
    sign-in button -- so the banner the sign-in just resolved reappears on top
    of a pane that now correctly says they are signed in.
    """
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "signed_in", lambda: False)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/kahala?error=You+aren%27t+signed+in+to+Kahala.")
        assert out.status_code == 200
        assert "You aren" in out.text, "the banner under test is not rendered"

        # Scoped to the sign-in poll's own handler: base.html reloads for
        # unrelated reasons, and this is about what happens on `signed_in`.
        assert "clearInterval(timer); location.reload();" not in out.text, \
            "the post-sign-in reload re-requests the url with its stale ?error="
        assert "clearInterval(timer); resume();" in out.text
        assert "searchParams.delete('error')" in out.text
        assert "searchParams.delete('ok')" in out.text


def test_the_pane_says_why_when_there_is_no_secure_store(wiki, monkeypatch):
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    monkeypatch.setattr(secretstore, "available", lambda: False)
    monkeypatch.setattr(secretstore, "unavailable_reason",
                        lambda: "No secure place to keep a sign-in token.")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.get("/kahala")
        assert out.status_code == 200
        assert "No secure place" in out.text
        assert "Sign in to Kahala" not in out.text, \
            "a sign-in button is offered that cannot possibly work"


# --- cross-site forgery ------------------------------------------------------


def _kahala_post_routes() -> list[str]:
    """Every mutating Kahala route, read off the app rather than hand-listed.

    Hand-listing is how a route added next year quietly ships unguarded.
    """
    from jamgraf.api import app
    return sorted({r.path for r in app.routes
                   if getattr(r, "path", "").startswith("/kahala")
                   and "POST" in getattr(r, "methods", set())})


def test_there_are_kahala_post_routes_to_check():
    """Guards the guard: an empty list would make the next test vacuous."""
    assert len(_kahala_post_routes()) >= 5


@pytest.mark.parametrize("path", _kahala_post_routes())
def test_a_cross_site_post_is_refused(wiki, path):
    """Loopback is owner, so any web page can POST here and be obeyed.

    For the rest of the app that risks local damage. Here a forged link+push
    would send the whole wiki to a host the attacker chose, so it is refused.
    """
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        out = client.post(path, data={"wiki": "main", "base_url":
                                      "https://evil.example", "remote": "x"},
                          headers={"sec-fetch-site": "cross-site"})
        assert out.status_code == 403, \
            f"a cross-site POST to {path} was accepted"

        # And the same request from Jamgraf's own page is not blocked by this.
        ours = client.post(path, data={"wiki": "main", "base_url":
                                       "https://kahala.example", "remote": "x"},
                           headers={"sec-fetch-site": "same-origin"},
                           follow_redirects=False)
        assert ours.status_code != 403, \
            f"{path} refuses Jamgraf's own form, so the feature is unusable"


# --- incremental sync ---------------------------------------------------------
#
# The bundle ships every page in full; a re-sync of a real wiki that differs by
# a paragraph pushes ~57MB. These pin the cheap path AND the two ways it is
# allowed to give up: a peer too old to carry definitions, and images that did
# not survive the trip. Both must fall back rather than leave the wiki subtly
# wrong, and both must SAY they fell back.


def _remote_wiki_with(pages=(("Alpha", "first body"),), element=True):
    """A second local wiki standing in for Kahala's copy."""
    from jamgraf import elements

    other = wikis.create_wiki("Remote Source")
    token = db.current_wiki.set(other)
    try:
        db.init_db()
        if element:
            elements.save_element("card", "Card", [{"name": "t"}],
                                  "<b></b>", ".card{}", "")
            store.template_save("Report", "# {{title}}")
        for title, body in pages:
            store.create_page(title, body)
    finally:
        db.current_wiki.reset(token)
    return other


def _as(slug, fn):
    token = db.current_wiki.set(slug)
    try:
        return fn()
    finally:
        db.current_wiki.reset(token)


def test_the_state_vector_and_changelog_move_definitions_not_just_pages(wiki):
    """The whole point of spec v3, exercised through the repository."""
    from jamgraf import elements

    remote = _remote_wiki_with()
    peer_sv = _as("main", store.wiki_state_vector)
    log = _as(remote, lambda: store.wiki_changelog_for(peer_sv))

    assert [e.slug for e in log.elements] == ["card"]
    assert "Report" in [t.name for t in log.templates]
    assert log.carries_definitions

    summary = _as("main", lambda: store.apply_wiki_changelog(log))
    assert summary["elements"] == 1
    assert _as("main", lambda: elements.get_element("card")) is not None, \
        "the page arrived but the element it renders with did not"
    assert _as("main", lambda: store.get_page("alpha")) is not None


def test_a_second_pass_resends_nothing(wiki):
    """If digests didn't work, every sync would ship every definition forever."""
    remote = _remote_wiki_with()
    first = _as(remote, lambda: store.wiki_changelog_for(
        _as("main", store.wiki_state_vector)))
    _as("main", lambda: store.apply_wiki_changelog(first))

    second = _as(remote, lambda: store.wiki_changelog_for(
        _as("main", store.wiki_state_vector)))
    assert second.elements == [] and second.templates == []
    assert not second.carries_definitions, \
        "an envelope with nothing new stamped the v3 floor, so an older peer " \
        "would reject it for no reason"


def test_an_incremental_pull_asks_for_a_changelog_and_applies_it(wiki, http,
                                                                 monkeypatch):
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/wiki-changelog")
        peer = wi.WikiStateVector.deserialize(request.read())
        log = _as(remote, lambda: store.wiki_changelog_for(peer))
        return httpx.Response(200, content=log.serialize())

    http(handler)
    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "incremental"
    assert store.get_page("alpha") is not None


def test_an_incremental_push_sends_only_what_the_peer_lacks(wiki, http,
                                                            monkeypatch):
    _signed_in(monkeypatch)
    store.create_page("Local Only", "body")
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wiki-state-vector"):
            # A peer that holds nothing, but does speak v3.
            return httpx.Response(200, content=wi.WikiStateVector().serialize())
        sent["log"] = wi.WikiChangelog.deserialize(request.read())
        return httpx.Response(200, json={"created": ["local-only"]})

    http(handler)
    out = kahala.push("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "incremental"
    assert "local-only" in [p.slug for p in sent["log"].pages]


def test_an_older_kahala_falls_back_to_the_whole_wiki_and_says_so(wiki, http,
                                                                  monkeypatch):
    """A v2 peer's envelope simply omits the sections — it never announces itself.

    Detecting that by the *absence of the key* rather than a version number is
    the load-bearing bit: a v3 peer with nothing new to send produces identical
    content, and only one of the two is safe to sync incrementally.
    """
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    payload = _bundle_of(remote)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wiki-changelog"):
            # Exactly what a pre-v3 Kahala answers: no definition sections.
            return httpx.Response(200, json={
                "format": "good-place.wiki-interchange/wiki-changelog",
                "spec_version": 1, "yjs_protocol": 1,
                "pages": [], "missing_from_server": []})
        return httpx.Response(200, content=payload)

    http(handler)
    out = kahala.pull("main")
    assert out["ok"], out.get("error")
    assert out["mode"] == "full"
    assert "older interchange" in out["note"], \
        "it silently used the slow path; a fallback nobody can see is one " \
        "nobody can question"


def test_a_real_error_does_not_quietly_retry_the_expensive_way(wiki, http,
                                                              monkeypatch):
    """Falling back on a genuine failure just fails again, slower."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(403, json={"detail": "nope"})

    http(handler)
    out = kahala.pull("main")
    assert not out["ok"] and "owner or admin" in out["error"]
    assert not any("snapshot" in c for c in calls), \
        "a 403 sent us round again for the whole wiki instead of reporting it"


def test_the_full_flag_skips_the_incremental_path_entirely(wiki, http,
                                                           monkeypatch):
    _signed_in(monkeypatch)
    remote = _remote_wiki_with()
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    payload = _bundle_of(remote)
    seen = http(lambda r: httpx.Response(200, content=payload))

    out = kahala.pull("main", full=True)
    assert out["ok"] and out["mode"] == "full"
    assert all("changelog" not in str(r.url) for r in seen)


def test_an_image_blob_that_lies_about_its_hash_is_refused(wiki):
    """Bytes and digest arrive together from a peer, so the digest proves nothing
    unless it is checked. Unverified, a payload lands under a trusted hash."""
    log = wi.WikiChangelog(images=[
        wi.BundleImage(sha256="ab" * 32, media_type="image/png", data=b"not that")])
    with pytest.raises(wi.MalformedEnvelopeError):
        store.apply_wiki_changelog(log)


def test_an_update_for_a_page_we_do_not_have_is_skipped_not_invented(wiki):
    """An incremental update cannot be applied to nothing."""
    log = wi.WikiChangelog(pages=[wi.WikiChangelogPage(
        slug="never-seen", changelog=wi.Changelog(ydoc_update=b"").serialize())])
    summary = store.apply_wiki_changelog(log)
    assert summary["skipped"] == ["never-seen"]
    assert store.get_page("never-seen") is None


# --- signing in from the packaged app ----------------------------------------
#
# In the desktop shell the sign-in must open the SYSTEM browser, not navigate
# the app's own WKWebView. RFC 8252 §8.12 says a native app must not run OAuth
# in an embedded user-agent, and the practical cost is the same shape as the
# principle: inside our window there is no Keycloak session, no password
# manager, and no second factor that depends on either.


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, client=("127.0.0.1", 12345))


def test_the_desktop_shell_gets_a_url_instead_of_a_redirect(wiki, http,
                                                            monkeypatch):
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token"}))

    with _client(app) as client:
        out = client.get("/kahala/signin?wiki=main&shell=desktop",
                         follow_redirects=False)
        assert out.status_code == 200, \
            "the desktop shell got a redirect, which would navigate the app's " \
            "own window to Keycloak — an embedded user-agent"
        body = out.json()
        assert body["ok"] and body["url"].startswith("https://kc.example/auth")
        assert "code_challenge_method=S256" in body["url"]


def test_a_browser_still_gets_the_ordinary_redirect(wiki, http, monkeypatch):
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token"}))

    with _client(app) as client:
        out = client.get("/kahala/signin?wiki=main", follow_redirects=False)
        assert out.status_code == 303
        assert out.headers["location"].startswith("https://kc.example/auth")


def test_an_external_callback_lands_on_a_page_not_in_the_app_ui(wiki, http,
                                                                monkeypatch):
    """The desktop flow finishes in a different application's window.

    Redirecting into /kahala there would leave the person looking at Jamgraf in
    a stray browser tab while the window they actually use sits unchanged.
    """
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token",
        "access_token": "at", "refresh_token": "rt", "expires_in": 300}))

    _url, state = kahalaauth.begin(external=True)
    with _client(app) as client:
        out = client.get(f"/kahala/callback?code=c&state={state}",
                         follow_redirects=False)
        assert out.status_code == 200, "an external callback redirected"
        assert "close this tab" in out.text


def test_an_in_window_callback_still_redirects_into_the_pane(wiki, http,
                                                             monkeypatch):
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    http(lambda r: httpx.Response(200, json={
        "authorization_endpoint": "https://kc.example/auth",
        "token_endpoint": "https://kc.example/token",
        "access_token": "at", "refresh_token": "rt", "expires_in": 300}))

    _url, state = kahalaauth.begin()          # external defaults to False
    with _client(app) as client:
        out = client.get(f"/kahala/callback?code=c&state={state}",
                         follow_redirects=False)
        assert out.status_code == 303 and out.headers["location"].startswith("/kahala")


def test_an_unknown_state_is_treated_as_external(wiki):
    """A callback we never started must not steer the app's own window."""
    assert kahalaauth.is_external("never-minted")


def test_the_pane_polls_rather_than_leaving_a_stale_answer(wiki, monkeypatch):
    """The desktop sign-in ends elsewhere, so this window gets no event."""
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "signed_in", lambda: False)
    with _client(app) as client:
        assert client.get("/kahala/state?wiki=main").json()["signed_in"] is False
        monkeypatch.setattr(kahalaauth, "signed_in", lambda: True)
        assert client.get("/kahala/state?wiki=main").json()["signed_in"] is True


# --- a push cannot create the wiki on Kahala ---------------------------------
#
# Creating one there is `POST /wikis/create`, a session-authenticated browser
# form, and Kahala only accepts our bearer token on `/api/*` — so Jamgraf
# genuinely cannot do it, and the interface has to say so rather than let
# someone link to a name that was never going to work.


def test_status_points_at_where_a_wiki_gets_created(wiki):
    kahala.link("main", "https://kahala.example", "remote-wiki")
    assert kahala.status("main")["manage_url"] == "https://kahala.example/wikis"


def test_an_unlinked_wiki_offers_no_stray_link(wiki):
    assert kahala.status("main")["manage_url"] == ""


def test_a_missing_remote_wiki_says_a_push_cannot_create_it(wiki, http,
                                                            monkeypatch):
    """The 404 used to send people hunting for a typo or the wrong account.

    Those are real causes, but the likeliest one by far — for anybody setting
    this up for the first time — is that they never made the wiki on Kahala.
    """
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))

    out = kahala.push("main")
    assert not out["ok"]
    assert "cannot create a wiki" in out["error"], out["error"]
    assert "No such wiki" in out["error"], \
        "the server's own words were dropped in favour of our interpretation"
    assert "another tenant" in out["error"], \
        "the other genuine causes were dropped rather than de-emphasised"


def test_the_pane_says_a_push_cannot_create_and_offers_the_way_there(wiki,
                                                                     monkeypatch):
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "signed_in", lambda: True)
    wikis.set_link("main", "https://kahala.example", "remote-wiki")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        body = client.get("/kahala?wiki=main").text
    assert "can’t create the wiki on Kahala" in body
    assert 'data-open-url="https://kahala.example/wikis"' in body, \
        "no way to reach the page where a wiki is actually created"


# --- the link form's defaults -------------------------------------------------


def test_the_remote_name_defaults_to_this_wikis_own_slug(wiki):
    """Not its display name: the field wants a slug.

    Kahala derives its slug from the name typed into *its* form, so "StartupOS"
    and "startupos" are not interchangeable there — and the placeholder used to
    say "beaconlight" no matter which wiki you were looking at, which is a
    suggestion that is wrong for everyone but one.
    """
    assert kahala.suggested_link("startupos")["remote"] == "startupos"


def test_the_address_defaults_to_one_already_in_use(wiki):
    """People have one Kahala, not one per wiki."""
    assert kahala.suggested_link("main")["base_url"] == ""
    wikis.set_link("beaconlight", "https://kahala.example", "beaconlight")
    assert kahala.suggested_link("main")["base_url"] == "https://kahala.example"
    assert kahala.suggested_link("main")["remote"] == "main", \
        "the borrowed address dragged the other wiki's name along with it"


def test_the_pane_prefills_both(wiki):
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    wikis.set_link("beaconlight", "https://kahala.example", "beaconlight")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        body = client.get("/kahala?wiki=startupos").text
    assert 'value="https://kahala.example"' in body
    assert 'value="startupos"' in body


# --- the slug is not the display name ----------------------------------------
#
# Kahala's Wikis page shows both ("StartupOS /startupos") and the interchange
# wire addresses by slug. Linking to the display name produces a 404 that reads
# like a permissions problem, at push time, long after the mistake. Both sides
# derive slugs with the same `slugify`, so we can catch it where it is typed and
# say what the answer is.


def test_linking_to_a_display_name_is_refused_with_the_slug(wiki):
    out = kahala.link("main", "https://kahala.example", "StartupOS")
    assert not out["ok"]
    assert "startupos" in out["error"] and "slug" in out["error"]
    assert wikis.get_link("main") is None, "the bad link was stored anyway"


@pytest.mark.parametrize("good", ["startupos", "beaconlight", "ever-afterlife"])
def test_a_real_slug_still_links(wiki, good):
    assert kahala.link("main", "https://kahala.example", good)["ok"]


def test_an_existing_bad_link_gets_the_answer_in_the_404(wiki, http, monkeypatch):
    """Links made before the check existed are still out there — like the one
    that prompted this."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "StartupOS")   # bypasses link()
    http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))

    out = kahala.push("main")
    assert not out["ok"]
    assert "probably “startupos”" in out["error"], out["error"]
    assert "Forget this link" in out["error"]


def test_a_404_for_a_slug_that_is_already_a_slug_offers_no_false_hint(wiki, http,
                                                                      monkeypatch):
    """Don't tell someone their correct slug is wrong."""
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "startupos")
    http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))
    out = kahala.push("main")
    assert "display name" not in out["error"]
    assert "another tenant" in out["error"]


# --- which Kahala am I signed in to? -----------------------------------------


def test_the_pane_names_the_sign_in_server(wiki, monkeypatch):
    """"Signed in." alone is a claim the reader cannot check, and there is more
    than one Kahala."""
    from fastapi.testclient import TestClient
    from jamgraf.api import app

    monkeypatch.setattr(kahalaauth, "issuer", lambda: "https://kc.example/realms/gp")
    monkeypatch.setattr(kahalaauth, "signed_in", lambda: True)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        body = client.get("/kahala").text
    assert "https://kc.example/realms/gp" in body
    assert "One sign-in per install" in body


# --- report what the server said, then interpret ------------------------------
#
# Two 404s wear the same clothes. FastAPI answers an unmatched route with
# {"detail": "Not Found"}; Kahala's own handler says "No such wiki". Collapsing
# them into one confident sentence about wiki names sent two separate
# investigations down the wrong road, so the server's words come first now.


def test_an_absent_route_is_not_reported_as_a_missing_wiki(wiki, http,
                                                            monkeypatch):
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "startupos")
    http(lambda r: httpx.Response(404, json={"detail": "Not Found"}))

    out = kahala.push("main")
    assert not out["ok"]
    assert "too old for the interchange API" in out["error"], out["error"]
    assert "Nothing is wrong with the link" in out["error"]
    assert "cannot create a wiki" not in out["error"], \
        "a framework 404 was still blamed on the wiki name"


def test_a_missing_wiki_during_the_probe_is_not_retried_as_a_full_push(
        wiki, http, monkeypatch):
    """Falling back would upload the whole wiki before failing for a reason we
    already had."""
    _signed_in(monkeypatch)
    store.create_page("Big", "x" * 500)
    wikis.set_link("main", "https://kahala.example", "startupos")
    seen = http(lambda r: httpx.Response(404, json={"detail": "No such wiki"}))

    out = kahala.push("main")
    assert not out["ok"] and "No such wiki" in out["error"]
    assert not any(r.method == "POST" and r.url.path.endswith("/snapshot")
                   for r in seen), "it uploaded the wiki anyway"


def test_the_servers_own_words_survive_into_every_404(wiki, http, monkeypatch):
    _signed_in(monkeypatch)
    wikis.set_link("main", "https://kahala.example", "startupos")
    http(lambda r: httpx.Response(404, json={"detail": "Wiki is archived"}))
    assert "Wiki is archived" in kahala.push("main")["error"]


# --- the changelog path, when BOTH sides already have the page ----------------
#
# The gap that let a real bug ship. Every other test here starts from a peer
# holding nothing, so every page travels as a SNAPSHOT and the changelog branch
# is never exercised end to end. The one shape that matters in practice — two
# peers that both already have a page — was untested, and in it we wrapped an
# already-serialized Changelog envelope a second time. The receiver's CRDT
# decoder then read past the end of the buffer and said so, and two layers of
# over-broad error handling turned that into "there is no wiki called startupos".


def _two_wikis_sharing_a_page(slug="jamgraf-repo"):
    """Sender and receiver both hold the page, with independent Y.Doc lineages."""
    sender, receiver = wikis.create_wiki("Sender"), wikis.create_wiki("Receiver")
    for w, body in ((sender, "written locally"), (receiver, "written on the server")):
        token = db.current_wiki.set(w)
        try:
            db.init_db()
            store.create_page("Jamgraf Repo", body)
        finally:
            db.current_wiki.reset(token)
    return sender, receiver


def test_a_page_both_sides_have_travels_as_a_usable_changelog(wiki):
    sender, receiver = _two_wikis_sharing_a_page()
    peer = _as(receiver, store.wiki_state_vector)
    log = _as(sender, lambda: store.wiki_changelog_for(peer))

    entry = next(p for p in log.pages if p.slug == "jamgraf-repo")
    assert entry.changelog is not None and entry.snapshot is None, \
        "the peer already had this page, so it should travel as an update"

    # The envelope must carry raw Yjs bytes. JSON here is the bug: it decodes as
    # an envelope and then runs off the end of the buffer inside pycrdt.
    inner = wi.Changelog.deserialize(entry.changelog).ydoc_update
    assert not inner.startswith(b"{"), \
        "ydoc_update contains a serialized envelope, not Yjs bytes — double-wrapped"

    summary = _as(receiver, lambda: store.apply_wiki_changelog(log))
    assert summary["updated"] == ["jamgraf-repo"], summary
    assert "double_wrapped" not in summary


def test_a_double_wrapped_changelog_from_a_peer_is_repaired_and_reported(wiki):
    """Kahala still ships this shape. Tolerate it — but never silently."""
    sender, receiver = _two_wikis_sharing_a_page()
    peer = _as(receiver, store.wiki_state_vector)
    log = _as(sender, lambda: store.wiki_changelog_for(peer))

    broken = wi.WikiChangelog(pages=[
        wi.WikiChangelogPage(
            slug=p.slug,
            changelog=wi.Changelog(ydoc_update=p.changelog).serialize(),
            title=p.title)
        for p in log.pages if p.changelog is not None])

    summary = _as(receiver, lambda: store.apply_wiki_changelog(broken))
    assert summary["updated"] == ["jamgraf-repo"]
    assert summary["double_wrapped"] == ["jamgraf-repo"], \
        "a peer's broken payload was straightened out without saying so"


def test_a_changelog_that_is_not_an_envelope_is_left_alone(wiki):
    """The repair must not mangle a payload it merely fails to parse."""
    junk = b"\x01\x02 not an envelope"
    assert store._unwrap_double_changelog(junk) == (junk, False)
