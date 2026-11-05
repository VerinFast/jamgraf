"""A capability the machine can't do must be *reported*, not discovered on click.

The bug these guard: a user without the ``claude`` CLI pressed Chat, waited, and
got an error with a shell command in it to copy. Everything below is one of the
three halves of replacing that — say it before the click, offer a button that
actually fixes it, and be honest when the fix fails.

Nothing here may depend on what is installed on the machine running the suite.
Every process launch and every HTTP call is faked; ``conftest``'s ``_bare_machine``
fixture already makes the default answer "you have nothing", and a test that
wants a tool present says so itself.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from jamgraf import capabilities, remedies, store
from jamgraf.api import app


def _client():
    return TestClient(app, client=("127.0.0.1", 1))


def _have(monkeypatch, *tools: str):
    """Pretend exactly `tools` are installed on this machine."""
    present = set(tools)
    monkeypatch.setattr(capabilities, "_which",
                        lambda name: f"/usr/local/bin/{name}" if name in present else None)
    capabilities.refresh()


class _Proc:
    """A finished subprocess, without running one."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _cap(cid: str) -> dict:
    got = capabilities.get(cid)
    assert got is not None, f"no capability {cid!r}"
    return got


# --- reported before the click ----------------------------------------------

def test_chat_is_unavailable_when_its_cli_is_missing(wiki, monkeypatch):
    """The whole point: this is knowable without running anything."""
    _have(monkeypatch)
    chat = _cap("chat")
    assert chat["state"] == capabilities.UNAVAILABLE
    assert "claude" in chat["reason"]
    # A reason a person can read, not a command to paste.
    assert "npm i -g" not in chat["reason"]
    assert "npm install" not in chat["reason"]


def test_chat_is_ready_once_the_cli_is_there(wiki, monkeypatch):
    _have(monkeypatch, "claude")
    assert _cap("chat")["state"] == capabilities.OK


def test_the_chat_capability_follows_the_configured_cli(wiki, monkeypatch):
    """Chat set to Gemini is about `gemini`, not about `claude`."""
    store.set_setting("chat_provider", "gemini")
    _have(monkeypatch, "claude")
    chat = _cap("chat")
    assert chat["state"] == capabilities.UNAVAILABLE
    assert "gemini" in chat["reason"]


def test_drafting_is_not_rescued_by_doorman(wiki, monkeypatch):
    """``authoring.draft`` has no Doorman path, so it must not claim one.

    Chat can be answered by a Doorman agent; drafting an element cannot — it
    reads the wiki's existing elements over MCP. Reporting drafting as ready
    because Doorman is up would be the same lie in a new place.
    """
    _have(monkeypatch)
    monkeypatch.setattr(capabilities, "_which", lambda name: None)

    from jamgraf import doorman
    monkeypatch.setattr(doorman, "status", lambda: {
        "running": True, "enabled": True, "voices": [],
        "ask": {"label": "Doorman · agent", "agent": "agent"}, "image": None})
    capabilities.refresh()

    assert _cap("chat")["state"] == capabilities.OK
    assert _cap("drafting")["state"] == capabilities.UNAVAILABLE


# --- the prerequisite chain --------------------------------------------------

def test_the_remedy_is_the_cli_when_npm_is_there(wiki, monkeypatch):
    _have(monkeypatch, "npm")
    remedy = _cap("chat")["remedy"]
    assert remedy["id"] == "install-claude-cli"
    assert remedy["kind"] == "install"


def test_without_npm_the_remedy_becomes_node_not_the_cli(wiki, monkeypatch):
    """The crux. Offering "install the Claude CLI" with no npm is a dead end.

    The actionable step is the prerequisite, and it has to be *offered* as the
    button — not mentioned in the small print under a button that would fail.
    """
    _have(monkeypatch, "brew")
    remedy = _cap("chat")["remedy"]
    assert remedy["id"] == "install-node"
    assert remedy["needs"] == "brew"
    assert remedy["step"] == "prerequisite"
    assert "npm" not in remedy["argv"]


def test_with_neither_npm_nor_brew_node_still_gets_a_button_via_nvm(wiki, monkeypatch):
    """nvm installs Node without a package manager, so the chain does not dead-end.

    It used to stop here with a link. It doesn't have to: curl is enough.
    """
    _have(monkeypatch, "curl")
    remedy = _cap("chat")["remedy"]
    assert remedy["kind"] == "script"
    assert remedy["id"] == "install-node-nvm"
    assert remedy["step"] == "prerequisite"      # Node first, then the CLI
    assert remedy["provides"] == "npm"
    # The confirmation must name who actually serves the script, not the
    # friendlier page it is documented on.
    assert remedy["host"] == "raw.githubusercontent.com"
    assert "nodejs.org" not in remedy["detail"]
    # Pinned: install.sh off a moving ref is a different script every time.
    assert "/nvm-sh/nvm/v" in remedy["url"]


def test_without_even_curl_the_node_button_is_withheld(wiki, monkeypatch):
    """A script remedy needs something to fetch with; no curl, no button."""
    _have(monkeypatch)                            # nothing at all
    remedy = _cap("chat")["remedy"]
    assert remedy["kind"] == "manual"
    assert remedy["id"] == ""                     # nothing for a POST route to run
    assert remedy["url"] == "https://nodejs.org/en/download"


def test_the_public_link_chain_stops_at_homebrew(wiki, monkeypatch):
    """Same shape elsewhere: cloudflared needs brew, and brew is not ours to install."""
    _have(monkeypatch)
    assert _cap("public-link")["remedy"]["kind"] == "manual"
    _have(monkeypatch, "brew")
    assert _cap("public-link")["remedy"]["id"] == "install-cloudflared"


def test_a_remedy_refuses_to_run_once_its_prerequisite_is_gone(wiki, monkeypatch):
    """The button was rendered an hour ago; this side is the one that runs things."""
    _have(monkeypatch)          # no npm
    ran = []
    monkeypatch.setattr(remedies.clirun, "run",
                        lambda *a, **k: ran.append(a) or _Proc())
    res = capabilities.apply("install-claude-cli")
    assert res["ok"] is False
    assert "npm" in res["error"]
    assert ran == []


# --- image generation: match the remedy to the configured tool ---------------

def test_the_default_image_cli_gets_the_vendors_installer(wiki, monkeypatch):
    _have(monkeypatch, "curl")
    remedy = _cap("images")["remedy"]
    assert remedy["id"] == "install-agy-cli"
    assert remedy["host"] == "antigravity.google"


def test_a_custom_image_cli_never_gets_the_antigravity_installer(wiki, monkeypatch):
    """`agy` is only the default. Installing it wouldn't help someone who named
    a different tool, and guessing an install for their tool is worse."""
    store.set_setting("image_cli", "my-renderer")
    _have(monkeypatch, "curl")
    remedy = _cap("images")["remedy"]
    assert remedy["kind"] == "manual"
    assert "my-renderer" in remedy["why"]
    assert "antigravity" not in str(remedy).lower()


def test_the_installer_url_is_a_constant_never_a_setting(wiki, monkeypatch):
    """Nothing a user, a page or an agent supplies may reach the shell.

    Piping a download into bash is the highest-privilege thing in this module;
    the URL has to be unreachable from outside the file.
    """
    store.set_setting("image_cli", "https://evil.example/x.sh")
    _have(monkeypatch, "curl")
    plan = capabilities.describe("install-agy-cli")
    assert plan["url"] == "https://antigravity.google/cli/install.sh"
    assert "evil.example" not in " ".join(plan["argv"])
    # And an id that isn't in the registry can never become a command.
    assert capabilities.describe("../../bin/sh") is None
    assert capabilities.apply("rm -rf /")["ok"] is False


def test_the_script_installer_never_runs_from_a_probe(wiki, monkeypatch):
    """Only a deliberate click may run it — never a page render or a status read."""
    ran = []
    monkeypatch.setattr(remedies.clirun, "run",
                        lambda label, argv, t: ran.append(label) or _Proc())
    _have(monkeypatch, "curl")
    capabilities.report()
    capabilities.states()
    with _client() as c:
        c.get("/settings")
        c.get("/")
    assert ran == []


# --- failure has to look like failure ----------------------------------------

def test_a_failed_install_is_reported_as_a_failure(wiki, monkeypatch):
    """npm global installs fail on permissions constantly. That must be visible."""
    _have(monkeypatch, "npm")
    monkeypatch.setattr(remedies.clirun, "run", lambda *a, **k: _Proc(
        returncode=243, stderr="npm ERR! code EACCES\nnpm ERR! permission denied"))
    res = capabilities.apply("install-claude-cli")
    assert res["ok"] is False
    assert "permission" in res["error"].lower()
    assert "EACCES" in res["detail"]          # what it actually said, not a shrug


def test_an_install_that_says_ok_but_installs_nothing_is_not_a_success(wiki, monkeypatch):
    """Exit 0 is a claim, not evidence. The tool has to actually be there."""
    _have(monkeypatch, "npm")                 # ...and never `claude`
    monkeypatch.setattr(remedies.clirun, "run", lambda *a, **k: _Proc(0, "done"))
    res = capabilities.apply("install-claude-cli")
    assert res["ok"] is False
    assert "claude" in res["error"]


def test_a_timeout_is_a_reported_outcome_not_a_hang(wiki, monkeypatch):
    _have(monkeypatch, "npm")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("npm", 1)

    monkeypatch.setattr(remedies.clirun, "run", boom)
    res = capabilities.apply("install-claude-cli")
    assert res["ok"] is False and "still running" in res["error"]


def test_a_successful_install_reprobes_without_a_restart(wiki, monkeypatch):
    """The view must not still say "not installed" a second after installing it."""
    _have(monkeypatch, "npm")
    assert _cap("chat")["state"] == capabilities.UNAVAILABLE

    def install(*a, **k):
        _have(monkeypatch, "npm", "claude")   # the machine changed underneath us
        return _Proc(0, "added 1 package")

    monkeypatch.setattr(remedies.clirun, "run", install)
    assert capabilities.apply("install-claude-cli")["ok"] is True
    assert _cap("chat")["state"] == capabilities.OK


# --- Doorman is never installed or launched ----------------------------------

def test_doorman_is_reported_but_never_remedied(wiki):
    """It is the user's own app. Detected, described — never offered as a fix."""
    door = _cap("doorman")
    assert door["remedy"] is None
    assert door["optional"] is True


def test_no_remedy_anywhere_touches_doorman(wiki, monkeypatch):
    """Not by name, not by package, not on any platform."""
    ids = (list(remedies.PACKAGE_INSTALLS)
           + list(remedies.SCRIPT_INSTALLS)
           + list(remedies.OPEN_REMEDIES))
    for rid in ids:
        assert "doorman" not in rid.lower()
        plan = capabilities.describe(rid)
        if plan:
            assert "doorman" not in " ".join(plan["argv"]).lower()


def test_the_capabilities_module_cannot_start_doorman(wiki):
    """Source-level, like tests/test_doorman.py: no spawning it, at all."""
    import inspect

    for mod in (capabilities, remedies):
        src = inspect.getsource(mod)
        for shape in ("open -a", "Doorman.app", "osascript", "launchctl"):
            assert shape not in src, f"{mod.__name__} must not {shape}"


def test_a_doorman_that_is_absent_is_not_reported_as_broken(wiki):
    """A machine without Doorman is the ordinary case, not a misconfiguration."""
    door = _cap("doorman")
    assert door["optional"] is True
    assert "ordinary case" in door["reason"]


# --- the view ----------------------------------------------------------------

def test_settings_lists_every_capability_with_its_state(wiki, monkeypatch):
    _have(monkeypatch)
    with _client() as c:
        body = c.get("/settings").text
    assert 'id="capabilities"' in body
    for cap in capabilities.report():
        assert f'id="cap-{cap["id"]}"' in body
        assert cap["label"] in body


def test_the_view_offers_a_button_not_a_command(wiki, monkeypatch):
    _have(monkeypatch, "npm")
    with _client() as c:
        body = c.get("/settings").text
    assert "/settings/capabilities/install-claude-cli/fix" in body
    # The command exists only behind the confirmation's own disclosure, never as
    # the thing the user is expected to copy.
    assert "npm install -g @anthropic-ai/claude-code" not in body


def test_a_remedy_we_cannot_run_renders_as_a_disabled_button(wiki, monkeypatch):
    _have(monkeypatch)
    with _client() as c:
        body = c.get("/settings").text
    assert "disabled" in body and "Install Node.js" in body
    assert "/settings/capabilities//fix" not in body     # no empty-id form


# --- greyed-out feature affordances ------------------------------------------

def test_the_chat_button_renders_disabled_and_points_at_the_fix(wiki, monkeypatch):
    """Not hidden — a control that vanishes teaches the user nothing."""
    _have(monkeypatch)
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru").text
    assert 'id="wk-chat-launch"' in body
    assert 'aria-disabled="true"' in body
    assert '/settings#capabilities' in body


def test_the_chat_button_is_live_again_once_the_cli_is_installed(wiki, monkeypatch):
    _have(monkeypatch, "claude")
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru").text
    assert 'id="wk-chat-panel"' in body
    assert 'data-cap-off="1"' not in body


def test_the_editor_hands_the_capability_states_to_its_script(wiki, monkeypatch):
    """The image toolbar button is built in JS, so it needs the states in the page."""
    _have(monkeypatch)
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru/edit").text
    assert '"images": "unavailable"' in body or '"images":"unavailable"' in body


def test_generate_renders_disabled_when_nothing_can_answer(wiki, monkeypatch):
    _have(monkeypatch)
    store.set_setting("gen_provider", "ollama")     # and nothing is listening
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru/edit").text
    assert _cap("generation")["state"] == capabilities.UNAVAILABLE
    assert 'id="ai-go"' not in body                 # nothing for the script to bind
    assert 'class="btn cap-off"' in body


# --- the remedy route --------------------------------------------------------

def test_installing_asks_first(wiki, monkeypatch):
    """It changes the user's machine, so consent is enforced server-side."""
    _have(monkeypatch, "npm")
    ran = []
    monkeypatch.setattr(remedies.clirun, "run",
                        lambda *a, **k: ran.append(a) or _Proc())
    with _client() as c:
        r = c.post("/settings/capabilities/install-claude-cli/fix",
                   follow_redirects=False)
    assert r.status_code == 303
    assert "confirm=install-claude-cli" in r.headers["location"]
    assert ran == []


def test_the_confirmation_names_the_host_a_script_comes_from(wiki, monkeypatch):
    """Informed consent for the one remedy that pipes a download into a shell."""
    _have(monkeypatch, "curl")
    with _client() as c:
        body = c.get("/settings?confirm=install-agy-cli").text
    assert "antigravity.google" in body
    assert "downloads code and runs it" in body.lower() or "downloads an installer" in body


def test_confirming_runs_it_and_reports_the_outcome(wiki, monkeypatch):
    _have(monkeypatch, "npm")
    seen = {}

    def install(label, argv, timeout):
        seen["argv"] = argv
        _have(monkeypatch, "npm", "claude")
        return _Proc(0, "added 1 package")

    monkeypatch.setattr(remedies.clirun, "run", install)
    with _client() as c:
        r = c.post("/settings/capabilities/install-claude-cli/fix",
                   data={"confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" not in r.headers["location"]
    assert seen["argv"][:2] == ["npm", "install"]


def test_a_failure_comes_back_as_a_failure_the_user_can_read(wiki, monkeypatch):
    _have(monkeypatch, "npm")
    monkeypatch.setattr(remedies.clirun, "run", lambda *a, **k: _Proc(
        1, "", "npm ERR! code EACCES"))
    with _client() as c:
        r = c.post("/settings/capabilities/install-claude-cli/fix",
                   data={"confirm": "yes"}, follow_redirects=True)
    assert r.status_code == 200
    assert "permission" in r.text.lower()


def test_an_unknown_remedy_id_runs_nothing(wiki, monkeypatch):
    ran = []
    monkeypatch.setattr(remedies.clirun, "run",
                        lambda *a, **k: ran.append(a) or _Proc())
    with _client() as c:
        r = c.post("/settings/capabilities/install-everything/fix",
                   data={"confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert ran == []


def test_the_remedy_route_is_owner_only(wiki):
    """Guests can't run installers on somebody else's computer."""
    from jamgraf import auth
    assert auth.guest_may("/settings/capabilities/install-claude-cli/fix") is False


# --- probing must stay cheap and must not reach the network ------------------

def test_a_page_render_never_probes_ollama_when_it_is_not_selected(wiki, monkeypatch):
    calls = []
    monkeypatch.setattr(capabilities, "_reachable",
                        lambda url, **k: calls.append(url) or False)
    capabilities.refresh()
    with _client() as c:
        c.get("/")
    assert calls == []


def test_an_unreachable_ollama_is_probed_once_not_per_render(wiki, monkeypatch):
    store.set_setting("gen_provider", "ollama")
    calls = []

    def counted(url, **k):
        calls.append(url)
        return False

    monkeypatch.setattr(capabilities, "_reachable", counted)
    capabilities.refresh()
    for _ in range(5):
        capabilities.report()
    assert len(calls) == 1


@pytest.mark.parametrize("cap_id", [
    "chat", "drafting", "generation", "images", "speech", "dictation",
    "doorman", "updates", "public-link", "semantic-search",
])
def test_every_capability_says_what_it_powers(wiki, cap_id):
    """A row nobody can connect to a button they've seen is not a status view."""
    cap = _cap(cap_id)
    assert cap["powers"] and cap["reason"]
    assert cap["state"] in (capabilities.OK, capabilities.DEGRADED,
                            capabilities.UNAVAILABLE)


def test_a_probe_that_throws_never_takes_the_page_down(wiki, monkeypatch):
    """This runs on every render now, so it has to fail open, not fail shut.

    "We couldn't work it out" must leave the feature's own button alone — greying
    out something that actually works would be the same broken promise pointing
    the other way.
    """
    def explode(name):
        raise OSError("PATH is on fire")

    monkeypatch.setattr(capabilities, "_which", explode)
    capabilities.refresh()
    store.create_page("Meru", "body")
    with _client() as c:
        r = c.get("/wiki/meru")
    assert r.status_code == 200
    assert _cap("chat")["state"] == capabilities.DEGRADED
    assert 'id="wk-chat-panel"' in r.text          # not greyed out on a bad probe


# --- Windows: the same three halves, a different package manager --------------
#
# Rule 9 is about the step that is actionable *now*, which is a per-machine
# question — and a machine's package manager is part of what it is. These pin
# the Windows answers, and that nothing macOS-shaped leaks into them: a brew
# step no Windows box can run, or a `curl | bash` on a platform where winget
# installs Node outright, would each be a button that fails.

@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(capabilities.sys, "platform", "win32")
    return monkeypatch


def test_on_windows_node_comes_from_winget_not_homebrew(wiki, windows):
    _have(windows, "winget")
    remedy = _cap("chat")["remedy"]

    assert remedy["id"] == "install-node-winget"
    assert remedy["needs"] == "winget"
    assert remedy["step"] == "prerequisite"


def test_on_windows_without_winget_the_instructions_do_not_mention_homebrew(
        wiki, windows):
    """A Homebrew instruction on Windows is advice that cannot be followed."""
    _have(windows)
    remedy = _cap("chat")["remedy"]

    assert remedy["kind"] == "manual"
    assert "Homebrew" not in remedy["why"]
    assert "winget" in remedy["why"]
    assert remedy["url"]


def test_on_windows_nothing_is_piped_into_a_shell(wiki, windows):
    """The nvm `curl | bash` path exists because macOS may have no package
    manager at all. Windows ships one, so the riskiest remedy in the app has no
    reason to appear here — and `curl` being present must not conjure it.
    """
    _have(windows, "curl")
    remedy = _cap("chat")["remedy"]

    assert remedy["kind"] != "script", "a shell pipeline was offered on Windows"
    assert remedy["kind"] == "manual"


def test_the_public_link_chain_follows_the_platforms_package_manager(wiki, windows):
    _have(windows)
    assert _cap("public-link")["remedy"]["kind"] == "manual"
    _have(windows, "winget")
    assert _cap("public-link")["remedy"]["id"] == "install-cloudflared-winget"


@pytest.mark.parametrize("remedy_id", ["install-node-winget",
                                       "install-cloudflared-winget"])
def test_every_winget_install_is_exact_and_non_interactive(remedy_id):
    """Two ways this argv goes wrong, both silent.

    Without ``-e`` winget treats the id as a search and may install a different
    package whose name is close enough. Without the two ``--accept`` flags it
    stops on an agreement prompt nobody can see and the remedy hangs until the
    timeout, which reads as "Jamgraf is broken" rather than "press yes".
    """
    argv = remedies.describe(remedy_id)["argv"]

    assert argv[0] == "winget"
    assert "-e" in argv and "--id" in argv
    assert argv[argv.index("--id") + 1].count(".") == 1, \
        "not a winget package identifier"
    assert "--accept-package-agreements" in argv
    assert "--accept-source-agreements" in argv


def test_dictation_in_a_windows_window_does_not_promise_what_it_cannot_give(
        wiki, windows, monkeypatch):
    """There is no macOS Dictation to click here, and whether an embedded
    WebView2 provides the web speech API is not ours to promise. So the state is
    degraded, the words say "may not", and the route that definitely works — a
    browser at the same address — is named.
    """
    monkeypatch.setenv("JAMGRAF_DESKTOP", "1")
    _have(windows)
    cap = _cap("dictation")

    assert cap["state"] == "degraded"
    assert "browser" in cap["reason"]
    assert "Accessibility" not in cap["reason"], "macOS wording on Windows"


def test_a_pip_install_is_pointed_at_pip_not_at_a_release_build(wiki, monkeypatch):
    """"Replace it with a signed release build" is not a step a pip user can
    take, and it is the only thing this row used to say. The remedy stays
    manual: upgrading someone's Python environment is theirs to run, not a
    button we press for them.
    """
    from jamgraf import updater

    monkeypatch.setattr(updater, "install_kind", lambda: "package")
    monkeypatch.setattr(updater, "can_update",
                        lambda: (False, "not running from a packaged .app"))
    _have(monkeypatch)
    remedy = _cap("updates")["remedy"]

    assert remedy["kind"] == "manual"
    assert "pip install -U jamgraf" in remedy["why"]
    assert "signed release build" not in remedy["why"]
