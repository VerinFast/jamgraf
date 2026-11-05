"""Updater tests.

No network and no real bundle swap: the swap happens in a detached helper that
outlives the test process, so exercising it here would be swapping the app out
from under pytest. What is covered is everything that decides *whether* to swap
-- version comparison, signature verification, and the refusal paths -- because
that is the part whose failure mode is running attacker-supplied code.
"""
import subprocess

from cryptography.hazmat.primitives import serialization as _ser


def _pub_hex(priv) -> str:
    return priv.public_key().public_bytes(
        _ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()


import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

from jamgraf import appconfig, updater


@pytest.fixture
def signing_key(monkeypatch):
    """A throwaway keypair, pinned as the build's update key."""
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", pub.hex())
    return priv


# --- Version comparison -------------------------------------------------------

@pytest.mark.parametrize("remote,local,expected", [
    ("v0.13.1", "0.13.0", True),
    ("0.14.0", "0.13.9", True),
    ("v1.0.0", "0.99.99", True),
    ("0.14", "0.13.0", True),           # short remote still compares
    ("0.13.0", "0.13.0", False),        # same version is not an update
    ("0.12.9", "0.13.0", False),        # older is not an update
    ("0.13", "0.13.0", False),          # 0.13 == 0.13.0, not newer
])
def test_version_comparison(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


@pytest.mark.parametrize("junk", ["", "garbage", "v", "latest", "..."])
def test_unparseable_remote_version_is_never_an_update(junk):
    """A malformed tag must not read as newer, or a bad release triggers rollout."""
    assert updater.is_newer(junk, "0.13.0") is False


def test_string_comparison_would_be_wrong():
    """Guards the reason we parse instead of comparing strings: "0.9" > "0.10"."""
    assert updater.is_newer("0.10.0", "0.9.0") is True


# --- Signature verification ---------------------------------------------------

def test_valid_signature_passes(signing_key):
    payload = b"pretend release zip"
    assert updater.verify_signature(payload, signing_key.sign(payload)) is True


def test_tampered_payload_is_refused(signing_key):
    payload = b"pretend release zip"
    sig = signing_key.sign(payload)
    assert updater.verify_signature(payload + b"evil", sig) is False


def test_signature_from_another_key_is_refused(signing_key):
    """The whole point of pinning: someone else's valid signature is still invalid."""
    payload = b"pretend release zip"
    attacker = ed25519.Ed25519PrivateKey.generate()
    assert updater.verify_signature(payload, attacker.sign(payload)) is False


@pytest.mark.parametrize("sig", [b"", b"\x00" * 64, b"short"])
def test_malformed_signatures_are_refused(signing_key, sig):
    assert updater.verify_signature(b"payload", sig) is False


@pytest.mark.parametrize("key", ["", "nothex", "aa" * 31, "aa" * 33])
def test_missing_or_malformed_pinned_key_disables_verification(monkeypatch, key):
    """Fail closed: no usable key means nothing verifies, not everything does."""
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", key)
    priv = ed25519.Ed25519PrivateKey.generate()
    assert updater.verify_signature(b"payload", priv.sign(b"payload")) is False


def test_signature_file_accepts_raw_or_hex(signing_key):
    payload = b"pretend release zip"
    sig = signing_key.sign(payload)
    assert updater.verify_signature(payload, updater._parse_signature(sig)) is True
    assert updater.verify_signature(
        payload, updater._parse_signature(sig.hex().encode())) is True


# --- Refusal paths ------------------------------------------------------------

def test_unpinned_build_cannot_update(monkeypatch):
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", "")
    ok, why = updater.can_update()
    assert ok is False and "signing key" in why


def test_source_checkout_cannot_update(signing_key):
    """Running from a checkout has no bundle to swap; refuse rather than guess."""
    ok, why = updater.can_update()
    assert ok is False and ".app" in why


def test_download_and_apply_refuses_without_a_key(monkeypatch, wiki):
    """The orchestrator must bail before any network call when unpinned."""
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", "")

    def explode(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(updater, "check", explode)
    monkeypatch.setattr(updater, "_download", explode)
    res = updater.download_and_apply()
    assert res["ok"] is False and "signing key" in res["error"]


def test_apply_update_refuses_a_missing_staged_bundle(signing_key, wiki, monkeypatch):
    fake = updater._updates_dir() / "Nope.app"
    monkeypatch.setattr(updater, "can_update", lambda: (True, ""))
    monkeypatch.setattr(updater, "bundle_path",
                        lambda: updater._updates_dir() / "Jamgraf.app")
    res = updater.apply_update(fake)
    assert res["ok"] is False and "staged bundle missing" in res["error"]


# --- Staging ------------------------------------------------------------------

def _make_bundle(root, name="Jamgraf.app", version="0.13.0"):
    """A minimal but structurally real .app."""
    app = root / name
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "MacOS" / "Jamgraf").write_text("#!/bin/bash\ntrue\n")
    import plistlib
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "Jamgraf",
        "CFBundleShortVersionString": version,
    }))
    return app


def _zip(app, dest):
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                    str(app), str(dest)], check=True, capture_output=True)
    return dest


def test_stage_expands_and_accepts_a_matching_bundle(wiki, tmp_path):
    src = _make_bundle(tmp_path / "src", version="0.14.0")
    z = _zip(src, tmp_path / "Jamgraf-0.14.0.zip")
    staged = updater.stage(z, expect_version="v0.14.0")
    assert staged.is_dir() and (staged / "Contents" / "MacOS" / "Jamgraf").exists()


def test_stage_rejects_a_version_mismatch(wiki, tmp_path):
    """A zip whose Info.plist disagrees with the release tag is not installed."""
    src = _make_bundle(tmp_path / "src", version="0.9.0")
    z = _zip(src, tmp_path / "Jamgraf-0.14.0.zip")
    with pytest.raises(RuntimeError, match="does not match release"):
        updater.stage(z, expect_version="0.14.0")


def test_stage_rejects_an_archive_with_no_app(wiki, tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "README.txt").write_text("not an app")
    z = _zip(junk, tmp_path / "junk.zip")
    with pytest.raises(RuntimeError, match="no .app bundle"):
        updater.stage(z)


# --- Scheduling ---------------------------------------------------------------

def test_auto_check_defaults_on_and_can_be_disabled(wiki):
    assert updater.auto_check_enabled() is True
    appconfig.set("update_auto_check", False)
    assert updater.auto_check_enabled() is False
    assert updater.due() is False           # disabled means never due


def test_due_respects_the_interval(wiki):
    import time
    assert updater.due() is True            # never checked
    appconfig.set("update_last_check", time.time())
    assert updater.due() is False
    appconfig.set("update_last_check", time.time() - 25 * 3600)
    assert updater.due() is True


def test_maybe_check_is_a_noop_when_not_due(wiki, monkeypatch):
    import time
    appconfig.set("update_last_check", time.time())
    monkeypatch.setattr(updater, "check",
                        lambda: (_ for _ in ()).throw(AssertionError("checked")))
    assert updater.maybe_check() is None


def test_status_reports_an_unpinned_build(wiki, monkeypatch):
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", "")
    st = updater.status()
    assert st["signing_key_pinned"] is False and st["updatable"] is False
    assert st["current"]


# --- Streamed verification (signature covers the digest, not the bytes) --------

def test_verify_file_accepts_a_correctly_signed_file(signing_key, tmp_path):
    blob = tmp_path / "Jamgraf-macos.zip"
    blob.write_bytes(b"x" * (3 << 20))          # spans multiple read chunks
    sig = signing_key.sign(updater.file_digest(blob))
    assert updater.verify_file(blob, sig) is True


def test_verify_file_refuses_a_modified_file(signing_key, tmp_path):
    blob = tmp_path / "Jamgraf-macos.zip"
    blob.write_bytes(b"x" * 1024)
    sig = signing_key.sign(updater.file_digest(blob))
    blob.write_bytes(b"x" * 1023 + b"y")        # same length, one byte changed
    assert updater.verify_file(blob, sig) is False


def test_verify_file_refuses_a_signature_over_the_raw_bytes(signing_key, tmp_path):
    """Guards the release.sh <-> updater contract.

    If the signer ever goes back to signing archive bytes while the client
    verifies the digest, every release would be refused. Pin the mismatch.
    """
    blob = tmp_path / "Jamgraf-macos.zip"
    blob.write_bytes(b"payload bytes")
    assert updater.verify_file(blob, signing_key.sign(blob.read_bytes())) is False


def test_file_digest_matches_hashlib(tmp_path):
    import hashlib
    blob = tmp_path / "f.bin"
    blob.write_bytes(b"abc" * 100000)
    assert updater.file_digest(blob) == hashlib.sha256(blob.read_bytes()).digest()


def test_verify_file_refuses_a_missing_file(signing_key, tmp_path):
    assert updater.verify_file(tmp_path / "nope.zip", b"\x00" * 64) is False


# --- The install offer --------------------------------------------------------

def _fake_release(tag, assets):
    return {"tag_name": tag, "body": "notes",
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}"}
                       for n in assets]}


def _run_check(monkeypatch, tag, assets):
    monkeypatch.setattr(updater, "_get_json",
                        lambda *a, **k: _fake_release(tag, assets))
    return updater.check()


def test_a_newer_signed_release_is_offered(wiki, monkeypatch):
    res = _run_check(monkeypatch, "v99.0.0",
                     ["Jamgraf-macos.zip", "Jamgraf-macos.zip.sig"])
    assert res["available"] and res["installable"]
    assert updater.status()["last_available"] == "v99.0.0"


def test_a_newer_release_without_a_signature_is_not_offered(wiki, monkeypatch):
    """Seen, but not installable — Settings must not offer a doomed install."""
    res = _run_check(monkeypatch, "v99.0.0", ["Jamgraf-macos.zip"])
    assert res["available"] is True and res["installable"] is False
    assert "no signed .zip asset" in res["error"]
    st = updater.status()
    assert st["last_seen"] == "v99.0.0"
    assert st["last_available"] is None


def test_an_older_release_is_not_offered(wiki, monkeypatch):
    """A local build newer than the latest release must not offer a downgrade."""
    res = _run_check(monkeypatch, "v0.0.1",
                     ["Jamgraf-macos.zip", "Jamgraf-macos.zip.sig"])
    assert res["available"] is False and res["installable"] is False
    st = updater.status()
    assert st["last_seen"] == "v0.0.1"          # we did see it...
    assert st["last_available"] is None          # ...but it is not an upgrade


def test_last_available_is_cleared_when_a_release_is_pulled(wiki, monkeypatch):
    """A stale offer must not survive a later check that finds nothing."""
    _run_check(monkeypatch, "v99.0.0",
               ["Jamgraf-macos.zip", "Jamgraf-macos.zip.sig"])
    assert updater.status()["last_available"] == "v99.0.0"
    _run_check(monkeypatch, "v0.0.1",
               ["Jamgraf-macos.zip", "Jamgraf-macos.zip.sig"])
    assert updater.status()["last_available"] is None


# --- Swap helper --------------------------------------------------------------

def test_the_backup_stays_on_the_bundles_own_volume(signing_key, wiki, tmp_path,
                                                    monkeypatch):
    """`mv` across filesystems fails with EXDEV, aborting a valid update.

    The backup must be a sibling of the bundle, not under DATA_DIR (which can be
    on a different volume when the app lives on an external drive).
    """
    target = tmp_path / "Applications" / "Jamgraf.app"
    (target / "Contents" / "MacOS").mkdir(parents=True)
    staged = updater._updates_dir() / "Jamgraf.app"
    (staged / "Contents" / "MacOS").mkdir(parents=True)

    monkeypatch.setattr(updater, "can_update", lambda: (True, ""))
    monkeypatch.setattr(updater, "bundle_path", lambda: target)
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: None)      # don't actually detach
    res = updater.apply_update(staged)
    assert res["ok"], res

    script = (updater._updates_dir() / "swap.sh").read_text()
    bak_line = next(ln for ln in script.splitlines() if ln.startswith("BAK="))
    assert str(target.parent) in bak_line, \
        f"backup is not beside the bundle: {bak_line}"
    assert str(updater._updates_dir()) not in bak_line, \
        f"backup lives under DATA_DIR and can cross filesystems: {bak_line}"


def test_an_empty_env_override_disables_a_pinned_build(monkeypatch):
    """Setting JAMGRAF_UPDATE_PUBKEY="" must disable updating, not fall back.

    Once a real key is pinned in the source, an override that resolved to the
    constant when empty would make it impossible to run a build with updates
    off — and would silently re-enable trust the operator meant to remove.
    """
    assert updater.PUBLIC_KEYS_HEX, "this test is meaningless without a pinned key"
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", "")
    assert updater._pinned_keys() == []
    ok, why = updater.can_update()
    assert ok is False and "signing key" in why


def test_the_pinned_keys_are_used_when_no_override_is_set(monkeypatch):
    monkeypatch.delenv("JAMGRAF_UPDATE_PUBKEY", raising=False)
    assert updater._pinned_keys() == [bytes.fromhex(h)
                                      for h in updater.PUBLIC_KEYS_HEX]


# --- Rotation (issue #69) -----------------------------------------------------
#
# A build trusts what was pinned into it for its whole life, so rotation only
# works if a shipped build already trusts more than one key. These pin the
# overlap that makes that possible.

def test_a_successor_key_verifies_alongside_the_current_one(monkeypatch):
    """The overlap window: sign with either, both are accepted."""
    current, successor = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", " ".join(
        _pub_hex(k) for k in (current, successor)))
    for key in (current, successor):
        assert updater.verify_signature(b"payload", key.sign(b"payload")) is True


def test_a_key_outside_the_set_is_still_refused(monkeypatch):
    """Trusting several keys must not drift into trusting any key."""
    trusted, attacker = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", _pub_hex(trusted))
    assert updater.verify_signature(b"payload", attacker.sign(b"payload")) is False


def test_verifying_key_names_which_key_signed(monkeypatch):
    """The release script reports the signer, so a rotation is visible."""
    current, successor = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", " ".join(
        _pub_hex(k) for k in (current, successor)))
    signed = updater.verifying_key(b"payload", successor.sign(b"payload"))
    assert signed == successor.public_key().public_bytes(
        _ser.Encoding.Raw, _ser.PublicFormat.Raw)
    assert updater.verifying_key(b"payload", b"\x00" * 64) is None


def test_one_malformed_entry_does_not_disarm_the_rest(monkeypatch):
    """A typo in one key must not take out the others, nor mean 'trust all'."""
    good = ed25519.Ed25519PrivateKey.generate()
    monkeypatch.setenv("JAMGRAF_UPDATE_PUBKEY", f"nothex, {_pub_hex(good)}, aa")
    assert updater.verify_signature(b"payload", good.sign(b"payload")) is True
    other = ed25519.Ed25519PrivateKey.generate()
    assert updater.verify_signature(b"payload", other.sign(b"payload")) is False


# --- What "update" means for the way this copy was installed -------------------
#
# Rule 8's refusal is unchanged on every path below: no verified .app bundle, no
# self-update. What changes is the next step a person can actually take, and
# naming the wrong one is worse than naming none (rule 9).

def test_a_pip_install_is_told_about_its_package_manager(signing_key, monkeypatch):
    monkeypatch.setattr(updater, "install_kind", lambda: "package")
    ok, why = updater.can_update()

    assert ok is False
    assert ".app" in why, "the refusal itself still has to be stated"
    assert "Python package" in why


def test_an_install_shape_we_do_not_recognise_is_not_guessed_at(
        signing_key, monkeypatch):
    """Telling someone to run `pip install -U` when they did not use pip sends
    them somewhere that cannot work. Unknown stays unknown."""
    monkeypatch.setattr(updater, "install_kind", lambda: "unknown")
    ok, why = updater.can_update()

    assert ok is False
    assert "pip" not in why and "git" not in why


def test_a_bundle_is_never_mistaken_for_a_package(monkeypatch, tmp_path):
    """install_kind must answer "bundle" whenever there is one, whatever the
    code happens to be sitting next to: the .app is the one shape that *can*
    update itself, and missing it would turn automatic updates off."""
    bundle = tmp_path / "Jamgraf.app"
    bundle.mkdir()
    monkeypatch.setattr(updater, "bundle_path", lambda: bundle)

    assert updater.install_kind() == "bundle"
