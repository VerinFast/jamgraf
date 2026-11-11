"""Self-update for the packaged Jamgraf.app.

A running .app cannot overwrite its own bundle, so an update is always:
download -> verify -> stage -> hand off to a detached helper -> quit -> the
helper swaps the bundle and relaunches. The helper has to outlive the process
that spawned it, which is why it is a standalone script and not a thread.

User data is never touched. Wikis live in ``config.DATA_DIR``
(~/Library/Application Support/Jamgraf), outside the bundle, so an update
replaces code only. A backup still runs first: schema migrations are
forward-only, so an older binary may not understand a newer wiki.

Trust model
-----------
The bundle is unsigned (``codesign_identity=None`` in jamgraf.spec), so macOS
gives us no authenticity guarantee about a downloaded zip. This code path
downloads a blob and then executes it as the user, which makes it the highest
privilege path in the app -- HTTPS protects the transport, not the artifact.

So we carry our own trust root: every release zip must be signed with an
Ed25519 key whose *public* half is pinned in ``PUBLIC_KEY_HEX`` below. The
private half never lives in this repo. An unsigned payload, a payload signed
by another key, or a payload that does not match its signature is refused --
and if no public key is pinned, updating is disabled entirely rather than
falling back to trusting the download. Fail closed, always: the failure mode of
getting this wrong is arbitrary code execution on every install.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import appconfig, config

REPO = config.env("UPDATE_REPO", "VerinFast/jamgraf")
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Ed25519 public keys (64 hex chars each) that release zips must verify against.
# A release needs to satisfy ONE of them.
#
# Generate a keypair with scripts/release.sh --genkey. Paste the PUBLIC half
# here and commit it; keep the private half outside the repo (the script reads
# it from JAMGRAF_UPDATE_KEY). An empty set means "updates disabled" -- see
# _pinned_keys(). Never populate this from the network or from app config: a
# pinned key an attacker can rewrite is not a trust root.
#
# WHY A SET AND NOT ONE KEY. Each build trusts what is pinned into it, for its
# whole life. With a single key there is no way out of either disaster: lose the
# private half and every copy in the field can never take another update, and
# there is no revoking a stolen one -- those copies will accept whatever it signs
# until each is reinstalled by hand. A set makes rotation possible without
# abandoning anyone: publish a build trusting {current, next}, wait for it to
# spread, start signing with `next`, and later drop `current`. That overlap only
# exists if the shipped builds already carry more than one slot, which is why
# this is a tuple today, while the only installs are ours.
#
# SIGN WITH THE FIRST ONE. Order is meaningful: index 0 is the current signing
# key. Successors are appended, never inserted.
PUBLIC_KEYS_HEX: tuple[str, ...] = (
    "1e2c9d83ba11d5af155d6e0bb71f8a22d4da2146400a2fd5f0d872c285076937",
)

DEFAULT_INTERVAL_HOURS = 24
_lock = threading.Lock()


# --- Version comparison -------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    """"v0.13.0" -> (0, 13, 0). Trailing non-numeric junk is ignored.

    Returns () for anything unparseable, which never compares as newer.
    """
    s = (text or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in s.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    """True when `remote` is a strictly later version than `local`."""
    r, l = parse_version(remote), parse_version(local)
    if not r:
        return False            # unparseable remote is never an upgrade
    width = max(len(r), len(l))
    return r + (0,) * (width - len(r)) > l + (0,) * (width - len(l))


# --- Config -------------------------------------------------------------------

def auto_check_enabled() -> bool:
    val = appconfig.get("update_auto_check")
    return True if val is None else bool(val)


def interval_hours() -> int:
    try:
        return max(1, int(appconfig.get("update_interval_hours",
                                        DEFAULT_INTERVAL_HOURS)))
    except Exception:
        return DEFAULT_INTERVAL_HOURS


def _pinned_keys() -> list[bytes]:
    """Every Ed25519 public key this build trusts. Empty means updates are off.

    JAMGRAF_UPDATE_PUBKEY overrides the pinned set so tests (and a private build)
    can supply their own without editing the source; separate several with commas
    or whitespace. Setting it to an empty value is a deliberate override too — it
    means "no keys", which disables updating rather than falling back to what is
    pinned. That keeps the fail-closed direction: an override can only ever
    remove trust from a shipped build, never quietly add to it.

    Malformed entries are dropped rather than raising: a typo in one key must not
    take out the others, and it must never end up meaning "trust everything".
    """
    env = config.env("UPDATE_PUBKEY")
    raw = list(PUBLIC_KEYS_HEX) if env is None else env.replace(",", " ").split()
    out: list[bytes] = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        try:
            key = bytes.fromhex(item)
        except ValueError:
            continue
        if len(key) == 32 and key not in out:
            out.append(key)
    return out


# --- Paths --------------------------------------------------------------------

def _updates_dir() -> Path:
    d = config.DATA_DIR / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


BUNDLE_NAME = "Jamgraf.app"
LEGACY_BUNDLE_NAME = "Waikiki.app"


def finish_bundle_rename(bundle: Path | None = None) -> str | None:
    """Give an in-place-updated Waikiki.app its new name. Returns the outcome.

    A copy installed before the rename updates itself by swapping the new bundle
    into the path it is RUNNING from (`bundle_path`), so the update that carries
    the rename lands as Jamgraf's code inside a folder still called Waikiki.app.
    This finishes the job on the first launch: rename the folder, then leave
    Waikiki.app as a symlink to it. The symlink is load-bearing, not a courtesy —
    a Claude Desktop config or doorman naming .../Waikiki.app/Contents/MacOS/...
    keeps launching, and this very process, whose files were loaded from the old
    path, keeps resolving them through it.

    None means nothing to do (already Jamgraf.app, or not running from a bundle).
    "both" means a Jamgraf.app is already beside it: two installs, and choosing
    between them is not this function's call. A failure — a read-only volume, a
    translocated quarantined app — is reported and never stops the app starting;
    a half-done rename is undone so the bundle is left exactly as it was found.
    """
    bundle = bundle if bundle is not None else bundle_path()
    if bundle is None or bundle.name != LEGACY_BUNDLE_NAME:
        return None
    target = bundle.parent / BUNDLE_NAME
    if target.exists() or target.is_symlink():
        return "both"
    try:
        os.rename(bundle, target)
    except OSError as exc:
        return f"failed: {exc}"
    try:
        os.symlink(BUNDLE_NAME, bundle)                   # relative, beside it
    except OSError as exc:
        try:
            os.rename(target, bundle)
        except OSError:
            return f"failed: renamed, but could not link the old path back ({exc})"
        return f"failed: {exc}"
    return "renamed"


def bundle_path() -> Path | None:
    """The running .app bundle, or None when not running from one.

    sys.executable inside the bundle is
    Jamgraf.app/Contents/MacOS/Jamgraf, so the bundle is three parents up.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_kind() -> str:
    """How this copy of Jamgraf got here: "bundle", "package" or "unknown".

    This decides what "update" even means, which is why it lives beside the
    updater rather than in a view: a signed .app swaps itself, a pip install is
    its package manager's to update, and a checkout is git's. Telling someone to
    run the wrong one of those is worse than saying nothing, so a shape we do
    not recognise is reported as unknown rather than guessed at.
    """
    if bundle_path() is not None:
        return "bundle"
    here = Path(__file__).resolve().parent
    if (here.parent / "pyproject.toml").is_file():
        return "checkout"
    if any(p.name in ("site-packages", "dist-packages") for p in here.parents):
        return "package"
    return "unknown"


def _no_bundle_reason() -> str:
    """Why there is nothing to swap, in terms of how this copy was installed.

    Every branch keeps the words "packaged .app": the refusal is the same one
    (rule 8 -- no verified bundle, no update), and only the next step differs.
    """
    kind = install_kind()
    if kind == "package":
        return ("not running from a packaged .app: Jamgraf is installed here as "
                "a Python package, so updates come from whichever tool "
                "installed it")
    if kind == "checkout":
        return ("not running from a packaged .app: this is a source checkout, "
                "so updates come from git")
    return "not running from a packaged .app"


def can_update() -> tuple[bool, str]:
    """Whether this install is updatable, and why not when it isn't."""
    if not _pinned_keys():
        return False, ("no update signing key is pinned in this build "
                       "(see jamgraf/updater.py)")
    bundle = bundle_path()
    if bundle is None:
        return False, _no_bundle_reason()
    if not os.access(bundle, os.W_OK) or not os.access(bundle.parent, os.W_OK):
        return False, f"{bundle} is not writable by this user"
    return True, ""


# --- Check --------------------------------------------------------------------

def _get_json(url: str, timeout: float = 10.0) -> dict:
    import httpx

    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": f"Jamgraf/{config.VERSION}"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def check() -> dict:
    """Ask GitHub for the latest release. Network call; never raises.

    Returns {ok, current, latest?, available, zip_url?, sig_url?, notes?, error?}
    """
    result: dict = {"ok": False, "current": config.VERSION, "available": False}
    try:
        rel = _get_json(LATEST_URL)
        tag = str(rel.get("tag_name") or "")
        assets = {a.get("name"): a.get("browser_download_url")
                  for a in (rel.get("assets") or [])}
        zip_name = next((n for n in assets if n and n.endswith(".zip")), None)
        sig_name = f"{zip_name}.sig" if zip_name else None
        result.update({
            "ok": True,
            "latest": tag,
            "available": is_newer(tag, config.VERSION),
            "zip_url": assets.get(zip_name) if zip_name else None,
            "sig_url": assets.get(sig_name) if sig_name else None,
            "notes": rel.get("body") or "",
        })
        # An update with no signed asset is not installable. Say so plainly
        # rather than offering a button that will fail at verification.
        installable = bool(result["available"]
                           and result["zip_url"] and result["sig_url"])
        if result["available"] and not installable:
            result["error"] = (f"release {tag} has no signed .zip asset "
                              "(needs both the zip and its .sig)")
        result["installable"] = installable
        appconfig.set("update_last_check", time.time())
        appconfig.set("update_last_seen", tag)
        # Persist "installable upgrade" separately from "tag we saw". Settings
        # must not derive the offer from last_seen != current: that is true when
        # this build is NEWER than the latest release (offering a downgrade) and
        # when the release has no signature (offering an install that must fail).
        appconfig.set("update_last_available", tag if installable else None)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def due() -> bool:
    if not auto_check_enabled():
        return False
    last = appconfig.get("update_last_check")
    if not last:
        return True
    try:
        return (time.time() - float(last)) >= interval_hours() * 3600
    except Exception:
        return True


def maybe_check() -> dict | None:
    """Check if one is due (called from the hourly maintenance loop)."""
    if not due():
        return None
    return check()


# --- Verify -------------------------------------------------------------------

def verify_signature(payload: bytes, signature: bytes) -> bool:
    """Ed25519-verify `payload` against any pinned public key.

    False for a bad signature, a key we do not trust, a malformed key, or no
    pinned keys at all. One key out of the set is enough — that is what makes a
    rotation survivable (see PUBLIC_KEYS_HEX).
    """
    return verifying_key(payload, signature) is not None


def verifying_key(payload: bytes, signature: bytes) -> bytes | None:
    """Which pinned key verifies `payload`, or None if none of them does.

    Same answer as verify_signature, but it names the key — the release script
    reports which one signed, so a rotation is visible rather than something you
    infer from a release going quiet.
    """
    keys = _pinned_keys()
    if not keys or not signature:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception:
        return None
    for key_bytes in keys:
        try:
            key = ed25519.Ed25519PublicKey.from_public_bytes(key_bytes)
        except Exception:
            continue          # a bad entry must not mask a good one
        try:
            key.verify(signature, payload)
            return key_bytes
        except InvalidSignature:
            continue
        except Exception:
            continue
    return None


def _parse_signature(raw: bytes) -> bytes:
    """Accept a raw 64-byte signature or its hex encoding."""
    if len(raw) == 64:
        return raw
    try:
        return bytes.fromhex(raw.decode("ascii").strip())
    except Exception:
        return b""


def file_digest(path: Path, chunk: int = 1 << 20) -> bytes:
    """Streamed SHA-256 of a file.

    Release zips are ~100MB+, so the signature covers this digest rather than
    the archive bytes -- verification then costs constant memory instead of
    holding the whole download in RAM. Signing a hash is the normal
    construction; substituting content would require a SHA-256 collision.

    scripts/release.sh signs exactly this value; the two must not drift.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.digest()


def verify_file(path: Path, signature: bytes) -> bool:
    """Ed25519-verify a file against the pinned key, without loading it all."""
    try:
        digest = file_digest(path)
    except Exception:
        return False
    return verify_signature(digest, signature)


# --- Download + stage ---------------------------------------------------------

def _download(url: str, dest: Path, timeout: float = 120.0) -> None:
    import httpx

    headers = {"User-Agent": f"Jamgraf/{config.VERSION}"}
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(65536):
                    fh.write(chunk)
    tmp.replace(dest)           # only a complete download gets the real name


def _staged_version(app: Path) -> str | None:
    try:
        data = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except Exception:
        return None
    return data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")


def stage(zip_path: Path, expect_version: str | None = None) -> Path:
    """Expand a verified zip and sanity-check the bundle inside it.

    Raises RuntimeError if the archive does not contain a plausible .app, so a
    truncated or unexpected payload fails here rather than after the swap.
    """
    out = _updates_dir() / "staged"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zip_path), str(out)],
                   check=True, capture_output=True)
    apps = sorted(out.glob("*.app"))
    if not apps:
        raise RuntimeError("archive contains no .app bundle")
    app = apps[0]
    if not (app / "Contents" / "MacOS").is_dir():
        raise RuntimeError(f"{app.name} has no Contents/MacOS")
    if expect_version:
        got = _staged_version(app)
        if got and not _versions_match(got, expect_version):
            raise RuntimeError(
                f"bundle version {got} does not match release {expect_version}")
    # We downloaded this ourselves, so it has no quarantine flag -- but strip it
    # defensively in case a future path (a browser download, an AirDrop) does.
    subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)],
                   capture_output=True)
    return app


def _versions_match(a: str, b: str) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa) and pa == pb


# --- Swap ---------------------------------------------------------------------

_HELPER = """#!/bin/bash
# Jamgraf update helper. Waits for the app to exit, swaps the bundle, relaunches.
# Written by jamgraf/updater.py -- not meant to be run by hand.
set -u
PID={pid}
NEW={new}
TARGET={target}
BAK={bak}
LOG={log}

exec >>"$LOG" 2>&1
echo "--- $(date) swap start (pid $PID)"

for _ in $(seq 1 200); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.3
done
if kill -0 "$PID" 2>/dev/null; then
    echo "app (pid $PID) still running after 60s; aborting"
    exit 1
fi

rm -rf "$BAK"
if ! mv "$TARGET" "$BAK"; then
    echo "could not move the old bundle aside; aborting"
    exit 1
fi
if /usr/bin/ditto "$NEW" "$TARGET"; then
    echo "swap ok"
    rm -rf "$BAK" "$NEW"
else
    echo "ditto failed; rolling back"
    rm -rf "$TARGET"
    mv "$BAK" "$TARGET"
    open "$TARGET"
    exit 1
fi

open "$TARGET"
echo "relaunched"
"""


def apply_update(staged_app: Path) -> dict:
    """Back up, then hand the swap to a detached helper. Never returns swapped.

    The caller must quit promptly after this returns ok -- the helper is already
    waiting on our PID. Quit cleanly so collab.py's flusher lands its snapshot.
    """
    ok, why = can_update()
    if not ok:
        return {"ok": False, "error": why}
    target = bundle_path()
    if target is None:                      # can_update() already covers this
        return {"ok": False, "error": "not running from a packaged .app"}
    if not staged_app.is_dir():
        return {"ok": False, "error": f"staged bundle missing: {staged_app}"}

    from . import backups
    snap = backups.run_backup()
    if not snap.get("ok"):
        # A failed backup is not fatal on its own (a wiki may simply never have
        # been opened), but it is worth surfacing next to an update.
        print(f"[jamgraf] pre-update backup did not run: {snap.get('error')}",
              file=sys.stderr)

    d = _updates_dir()
    script = d / "swap.sh"
    script.write_text(_HELPER.format(
        pid=os.getpid(),
        new=shlex.quote(str(staged_app)),
        target=shlex.quote(str(target)),
        # Sibling of the bundle, not under DATA_DIR: `mv` across filesystems
        # fails with EXDEV, and an app on another volume (or an external drive)
        # would abort an otherwise valid update. can_update() already requires
        # the parent directory be writable, which is what makes this safe.
        bak=shlex.quote(str(target.parent / f"{target.name}.previous")),
        log=shlex.quote(str(d / "swap.log")),
    ))
    script.chmod(0o755)
    # start_new_session detaches it from our process group, so it survives the
    # quit it is waiting for.
    subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "backup": snap.get("name"), "log": str(d / "swap.log")}


# --- Orchestration ------------------------------------------------------------

def download_and_apply(info: dict | None = None) -> dict:
    """Full path: check -> download -> verify -> stage -> hand off.

    Returns {ok, error?}. On ok the caller should quit so the helper can swap.
    Refuses before touching the bundle if anything about the payload is off.
    """
    with _lock:
        ok, why = can_update()
        if not ok:
            return {"ok": False, "error": why}
        info = info or check()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "update check failed"}
        if not info.get("available"):
            return {"ok": False, "error": "already up to date"}
        zip_url, sig_url = info.get("zip_url"), info.get("sig_url")
        if not zip_url or not sig_url:
            return {"ok": False,
                    "error": info.get("error") or "release has no signed zip asset"}

        d = _updates_dir()
        tag = str(info.get("latest") or "")
        zip_path, sig_path = d / f"Jamgraf-{tag}.zip", d / f"Jamgraf-{tag}.zip.sig"
        try:
            _download(zip_url, zip_path)
            _download(sig_url, sig_path)
        except Exception as exc:
            return {"ok": False, "error": f"download failed: {exc}"}

        # Verify BEFORE expanding: never let an unverified archive write files.
        sig = _parse_signature(sig_path.read_bytes())
        if not verify_file(zip_path, sig):
            zip_path.unlink(missing_ok=True)
            sig_path.unlink(missing_ok=True)
            return {"ok": False,
                    "error": "signature verification failed -- update refused"}
        try:
            staged = stage(zip_path, expect_version=tag)
        except Exception as exc:
            return {"ok": False, "error": f"staging failed: {exc}"}
        return apply_update(staged)


def status() -> dict:
    """Everything the Settings page needs, with no network call."""
    ok, why = can_update()
    return {
        "current": config.VERSION,
        "updatable": ok,
        "reason": why,
        "auto_check": auto_check_enabled(),
        "interval_hours": interval_hours(),
        "last_check": appconfig.get("update_last_check"),
        "last_seen": appconfig.get("update_last_seen"),
        # The only field Settings may gate the install offer on: set when the
        # last check found a release that is both newer AND signed.
        "last_available": appconfig.get("update_last_available"),
        "signing_key_pinned": bool(_pinned_keys()),
        "signing_keys_trusted": len(_pinned_keys()),
    }
