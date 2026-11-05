"""Where `scripts/release.sh` gets the signing key, and what it does with it.

The private key that signs updates lives in Google Secret Manager as `waikikey`.
These tests run the real script against a stand-in `gcloud` on `PATH` that serves
a throwaway key.

They run it in a **sandbox**: a copy of the script and the `jamgraf` package with
the throwaway key's public half genuinely compiled into its `updater.py`, and a
stub `build_macos.sh` that records whether it was reached and then fails. So the
success path is a real Ed25519 match against compiled keys — not an environment
override, which the script now refuses — and the release path can be exercised
up to the build without ever running one.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

REPO = Path(__file__).resolve().parent.parent
VERSION = re.search(r'__version__ = "([^"]+)"',
                    (REPO / "jamgraf" / "__init__.py").read_text(encoding="utf-8")).group(1)


def _keypair() -> tuple[str, str]:
    """(PEM private key, hex public key) — a key no real install trusts."""
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                             ser.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return pem, pub.hex()


def _executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _key_line(pem: str) -> str:
    """One line of the key material itself — what must never show up anywhere."""
    return pem.split("-----")[2].strip().splitlines()[0]


class Sandbox:
    """A throwaway checkout: the script, the package, a stub build."""

    def __init__(self, tmp: Path):
        self.root = tmp / "repo"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(REPO / "scripts" / "release.sh", self.root / "scripts")
        shutil.copytree(REPO / "jamgraf", self.root / "jamgraf",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.built = tmp / "build-was-run"
        _executable(self.root / "scripts" / "build_macos.sh",
                    f'#!/bin/sh\ntouch "{self.built}"\necho "stub build: stopping here" >&2\nexit 1\n')
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.secret, self.log = tmp / "secret.pem", tmp / "gcloud.log"
        _executable(self.bin / "gcloud",
                    "#!/bin/sh\n"
                    'printf \'%s\\n\' "$*" >> "$FAKE_GCLOUD_LOG"\n'
                    'if [ -n "$FAKE_GCLOUD_FAIL" ]; then echo "$FAKE_GCLOUD_FAIL" >&2; exit 1; fi\n'
                    'cat "$FAKE_GCLOUD_SECRET"\n')
        self.scratch = tmp / "tmp"
        self.scratch.mkdir()
        self.data = tmp / "data"
        self.tmp = tmp

    def pin(self, *pub_hex: str) -> None:
        """Compile these public keys into the sandbox's updater, as a build would."""
        updater = self.root / "jamgraf" / "updater.py"
        text = updater.read_text(encoding="utf-8")
        pinned = "".join(f'    "{k}",\n' for k in pub_hex)
        text, n = re.subn(r"PUBLIC_KEYS_HEX: tuple\[str, \.\.\.\] = \(\n.*?\)",
                          f"PUBLIC_KEYS_HEX: tuple[str, ...] = (\n{pinned})",
                          text, count=1, flags=re.S)
        assert n == 1, "could not find PUBLIC_KEYS_HEX to pin the test key"
        updater.write_text(text)

    def serve(self, pem: str) -> None:
        self.secret.write_text(pem)

    @property
    def gcloud_calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, *args: str, path: str | None = None, shell: tuple = ("bash",),
            **env) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("JAMGRAF_", "WAIKIKI_", "FAKE_GCLOUD"))
                and k not in ("SIGNING_KEY_PEM", "SIGNING_KEY_SOURCE")}
        base.update(PYTHON=sys.executable, TMPDIR=str(self.scratch),
                    JAMGRAF_DATA=str(self.data),
                    PATH=f"{self.bin}{os.pathsep}{base['PATH']}",
                    FAKE_GCLOUD_LOG=str(self.log),
                    FAKE_GCLOUD_SECRET=str(self.secret))
        if path is not None:
            base["PATH"] = path
        base.update(env)
        return subprocess.run([*shell, str(self.root / "scripts" / "release.sh"), *args],
                              env=base, capture_output=True, text=True, timeout=120)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


@pytest.fixture
def good(sandbox):
    """A sandbox whose Secret Manager serves a key its build trusts."""
    pem, pub = _keypair()
    sandbox.pin(pub)
    sandbox.serve(pem)
    sandbox.pem = pem
    return sandbox


# --- The default: Google Secret Manager --------------------------------------

def test_the_key_comes_from_secret_manager_by_default(good):
    r = good.run("--check-key")

    assert r.returncode == 0, r.stderr
    assert "Google Secret Manager: waikikey (project startupos-328814)" in r.stdout
    assert "matches pinned key #0 of 1" in r.stdout
    # The project is named explicitly, never left to gcloud's per-machine default.
    assert good.gcloud_calls == [
        "secrets versions access latest --secret=waikikey --project=startupos-328814"]


def test_the_secret_name_and_project_can_be_chosen(good):
    r = good.run("--check-key", JAMGRAF_UPDATE_SECRET="other-key",
                 JAMGRAF_GCP_PROJECT="verinfast-prod")

    assert r.returncode == 0, r.stderr
    assert good.gcloud_calls == [
        "secrets versions access latest --secret=other-key --project=verinfast-prod"]


def test_a_key_no_client_trusts_is_refused(sandbox):
    pem, _ = _keypair()
    _, someone_elses = _keypair()
    sandbox.pin(someone_elses)
    sandbox.serve(pem)

    r = sandbox.run("--check-key")

    assert r.returncode != 0
    assert "NOT one of the 1 key(s) pinned" in (r.stdout + r.stderr)


def test_a_key_that_is_not_ed25519_is_named_for_what_it_is(sandbox):
    from cryptography.hazmat.primitives.asymmetric import rsa

    sandbox.serve(rsa.generate_private_key(public_exponent=65537, key_size=2048)
                  .private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                                 ser.NoEncryption()).decode())
    sandbox.pin(_keypair()[1])

    r = sandbox.run("--check-key")

    assert r.returncode != 0
    assert "not an Ed25519 key" in (r.stdout + r.stderr)


# --- When Secret Manager can't be reached ------------------------------------

def test_gclouds_own_explanation_reaches_the_person(sandbox):
    """Logged out, wrong project, no permission — gcloud says which; so do we."""
    sandbox.serve("unused")

    r = sandbox.run("--check-key",
                    FAKE_GCLOUD_FAIL="ERROR: (gcloud.secrets.versions.access) PERMISSION_DENIED")

    assert r.returncode != 0
    assert "could not read 'waikikey'" in r.stderr
    assert "PERMISSION_DENIED" in r.stderr


def test_no_gcloud_says_how_to_get_it_and_how_to_do_without(sandbox):
    r = sandbox.run("--check-key", path="/usr/bin:/bin")    # nowhere gcloud could be

    assert r.returncode != 0
    assert "gcloud is not installed" in r.stderr
    assert "JAMGRAF_UPDATE_KEY" in r.stderr                  # the other way to sign


def test_an_empty_secret_is_refused(sandbox):
    sandbox.serve("")

    r = sandbox.run("--check-key")

    assert r.returncode != 0
    assert "is empty" in r.stderr


# --- The explicit local file -------------------------------------------------

def test_an_explicit_key_file_wins_and_secret_manager_is_not_asked(sandbox):
    pem, pub = _keypair()
    sandbox.pin(pub)
    key = sandbox.tmp / "keys" / "update-key.pem"
    key.parent.mkdir()
    key.write_text(pem)

    r = sandbox.run("--check-key", JAMGRAF_UPDATE_KEY=str(key))

    assert r.returncode == 0, r.stderr
    assert f"the file {key}" in r.stdout
    assert sandbox.gcloud_calls == []                       # never consulted


def test_a_key_file_inside_the_repo_is_still_refused(sandbox):
    """Where it could be committed by accident — the rule this script had already."""
    pem, pub = _keypair()
    sandbox.pin(pub)
    inside = sandbox.root / "update-key.pem"
    inside.write_text(pem)

    r = sandbox.run("--check-key", JAMGRAF_UPDATE_KEY=str(inside))

    assert r.returncode != 0
    assert "inside the repo" in r.stderr


def test_a_key_file_that_does_not_exist_says_so(sandbox):
    r = sandbox.run("--check-key", JAMGRAF_UPDATE_KEY=str(sandbox.tmp / "nope.pem"))

    assert r.returncode != 0
    assert "does not exist" in r.stderr


# --- Verification is against the compiled keys, never an override ------------

@pytest.mark.parametrize("override", ["matching", ""])
def test_a_public_key_override_is_refused_not_trusted(good, override):
    """JAMGRAF_UPDATE_PUBKEY would let a release verify against anything at all.

    It replaces the pinned keys at runtime — fine for tests and private builds of
    the app, a trapdoor here: both checks would measure against the environment
    and approve a release no installed copy accepts. Even set to the RIGHT key it
    is refused, because the script cannot tell that from the wrong one; and set
    but empty counts, because to the updater that is a deliberate override too.
    """
    value = _keypair()[1] if override == "matching" else ""

    r = good.run("--check-key", JAMGRAF_UPDATE_PUBKEY=value)

    assert r.returncode != 0
    assert "UPDATE_PUBKEY" in r.stderr and "overrides the public keys" in r.stderr
    assert good.gcloud_calls == []                # refused before the key is fetched


def test_the_release_path_refuses_the_override_too(good):
    r = good.run(f"v{VERSION}", "--dry-run", JAMGRAF_UPDATE_PUBKEY="")

    assert r.returncode != 0
    assert "UPDATE_PUBKEY" in r.stderr and "overrides the public keys" in r.stderr
    assert not good.built.exists()


# --- A bad key costs seconds, not a build ------------------------------------

def test_an_untrusted_key_stops_the_release_before_the_build(sandbox):
    """build_macos.sh starts by wiping dist/; a doomed release must not reach it.

    Fetching the key early only ever saved the build when the key was MISSING. A
    malformed or untrusted one went straight through to the post-build check —
    after the previous build had been thrown away.
    """
    pem, _ = _keypair()
    sandbox.pin(_keypair()[1])
    sandbox.serve(pem)

    r = sandbox.run(f"v{VERSION}", "--dry-run")

    assert r.returncode != 0
    assert "NOT one of the 1 key(s) pinned" in (r.stdout + r.stderr)
    assert not sandbox.built.exists(), "the build ran for a key that could never sign"


def test_a_trusted_key_lets_the_release_reach_the_build(good):
    """The positive half, so the test above cannot pass by stopping everything."""
    r = good.run(f"v{VERSION}", "--dry-run")

    assert good.built.exists(), r.stdout + r.stderr
    assert "matches pinned key #0" in r.stdout


# --- The key never leaks -----------------------------------------------------

def test_the_fetched_key_never_touches_disk(good):
    """The point of keeping it in Secret Manager is that there is no copy lying around.

    The script makes exactly one temporary file — gcloud's stderr, so a failure
    can be explained — and it must be gone afterwards. Nothing in the temp dir or
    the checkout may hold the key, and it must not be printed.
    """
    r = good.run("--check-key")

    assert r.returncode == 0, r.stderr
    assert list(good.scratch.iterdir()) == [], "temp files left behind"
    line = _key_line(good.pem)
    for f in good.root.rglob("*"):
        if f.is_file():
            assert line not in f.read_text(errors="ignore"), f"key written to {f}"
    assert line not in r.stdout and line not in r.stderr, "the key was printed"


def test_bash_xtrace_does_not_print_the_key(good):
    """`bash -x` is the first thing anyone reaches for when a release misbehaves.

    xtrace prints assignments WITH their values, so without care it would dump the
    PEM to the terminal — and to a CI log, if this ever runs in one.
    """
    r = good.run("--check-key", shell=("bash", "-x"))

    assert r.returncode == 0, r.stderr
    assert "+ " in r.stderr                       # tracing really was on
    line = _key_line(good.pem)
    assert line not in r.stderr and line not in r.stdout, "xtrace printed the key"


def test_a_caller_exported_variable_cannot_carry_the_key_onward(good):
    """bash keeps the export flag when a script reassigns an inherited variable.

    So a SIGNING_KEY_PEM the caller happened to export would, once the script put
    the real key in it, ride into build_macos.sh and everything it starts. A
    stand-in Python records the environment of every process the script runs: the
    key may arrive through the one variable meant for it, and no other.
    """
    dumps = good.tmp / "env-dumps"
    dumps.mkdir()
    spy = _executable(good.tmp / "python-spy",
                      f'#!/bin/sh\nenv -0 > "{dumps}/$$.env"\nexec "{sys.executable}" "$@"\n')

    r = good.run("--check-key", PYTHON=str(spy),
                 SIGNING_KEY_PEM="exported-by-the-caller",
                 JAMGRAF_UPDATE_KEY_PEM="also-exported-by-the-caller")

    assert r.returncode == 0, r.stderr
    envs = [p.read_text(encoding="utf-8") for p in dumps.iterdir()]
    assert envs, "the spy never ran"
    line = _key_line(good.pem)
    carriers = [e for e in envs if line in e]
    assert carriers, "the signing process never received the key at all"
    for env in carriers:
        # NUL-separated, because the key is a multi-line value and plain `env`
        # output cannot say where one variable ends and the next begins.
        holders = [v.split("=", 1)[0] for v in env.split("\0") if line in v]
        assert holders == ["JAMGRAF_UPDATE_KEY_PEM"], f"key also carried by {holders}"
    assert not any("exported-by-the-caller" in e for e in envs), \
        "a caller's exported variable reached a child process"


# --- --genkey's instructions -------------------------------------------------

def test_genkey_says_how_to_store_a_first_key_and_how_to_rotate(sandbox):
    """`secrets create` refuses an existing secret, so it cannot rotate.

    Followed literally during a rotation it would fail and leave Secret Manager
    serving the old key — every release still signed with it. The first-key form
    also names its replication policy rather than leaning on a gcloud default.
    """
    key = sandbox.tmp / "fresh" / "update-key.pem"

    r = sandbox.run("--genkey", JAMGRAF_UPDATE_KEY=str(key))

    assert r.returncode == 0, r.stderr
    assert key.exists()
    assert "secrets create waikikey --replication-policy=automatic" in r.stdout
    assert "secrets versions add waikikey" in r.stdout
    assert "--project=startupos-328814" in r.stdout


# --- The old names, after the rename -----------------------------------------

@pytest.mark.parametrize("override", ["matching", ""])
def test_the_old_spelling_of_the_override_is_refused_too(good, override):
    """The app reads WAIKIKI_UPDATE_PUBKEY as a fallback (config.env).

    So refusing only JAMGRAF_UPDATE_PUBKEY would leave the old spelling as a
    working trapdoor: both verification steps would measure against it and
    approve a release no installed copy accepts.
    """
    value = _keypair()[1] if override == "matching" else ""

    r = good.run("--check-key", WAIKIKI_UPDATE_PUBKEY=value)

    assert r.returncode != 0
    assert "WAIKIKI_UPDATE_PUBKEY" in r.stderr
    assert good.gcloud_calls == []


def test_the_old_spelling_of_the_key_file_still_names_one(sandbox):
    pem, pub = _keypair()
    sandbox.pin(pub)
    key = sandbox.tmp / "keys" / "update-key.pem"
    key.parent.mkdir()
    key.write_text(pem)

    r = sandbox.run("--check-key", WAIKIKI_UPDATE_KEY=str(key))

    assert r.returncode == 0, r.stderr
    assert sandbox.gcloud_calls == []


def test_a_caller_exported_old_key_variable_is_scrubbed_too(good):
    """WAIKIKI_UPDATE_KEY_PEM was the channel's old name; a caller may export it."""
    dumps = good.tmp / "env-dumps-legacy"
    dumps.mkdir()
    spy = _executable(good.tmp / "python-spy-legacy",
                      f'#!/bin/sh\nenv -0 > "{dumps}/$$.env"\nexec "{sys.executable}" "$@"\n')

    r = good.run("--check-key", PYTHON=str(spy),
                 WAIKIKI_UPDATE_KEY_PEM="old-name-exported-by-the-caller")

    assert r.returncode == 0, r.stderr
    envs = [p.read_text(encoding="utf-8") for p in dumps.iterdir()]
    assert envs
    assert not any("old-name-exported-by-the-caller" in e for e in envs)
