#!/usr/bin/env bash
# Build Jamgraf.app (macOS) with PyInstaller, then zip it for release.
set -euo pipefail
cd "$(dirname "$0")/.."

# Build in a venv of its own, never the dev `.venv`. Claude Desktop launches
# Jamgraf's MCP server from `.venv` in the source tree, so pip-installing here
# used to mutate a live dependency set: `pip install` briefly uninstalls a
# package before replacing it, and a launch landing in that window fails to
# import and the server never starts. Cutting a release must not be able to
# break the editor you are cutting it from.
BUILD_VENV=".venv-build"

# Pick the interpreter deliberately instead of trusting bare `python3`. On macOS
# that name is whatever CommandLineTools ships (3.9 here), while pyproject.toml
# declares a floor of >=3.10 — so a build that trusts it installs an
# unsupported interpreter and then fails somewhere that says nothing about
# versions. Order: an explicit PYTHON override, else the newest pythonX.Y on
# PATH that clears the floor, else `python3` so the check below can name what
# it found. The floor is read from pyproject.toml rather than duplicated, so
# raising it there raises it here too.
PY_FLOOR="$(sed -n 's/^requires-python[^0-9]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' pyproject.toml | head -1)"
PY_FLOOR="${PY_FLOOR:-3.10}"
PY_FLOOR_MAJOR="${PY_FLOOR%%.*}"
PY_FLOOR_MINOR="${PY_FLOOR#*.}"

# The oldest macOS the app runs on. Every Mach-O in the bundle is checked against
# it after the build, and it lands in Info.plist as LSMinimumSystemVersion (the
# spec reads it from MACOSX_DEPLOYMENT_TARGET), so an older Mac is told plainly
# instead of crashing at launch. 14.0 is set by onnxruntime: every release since
# 1.24 ships macOS 14 arm64 wheels only.
MACOS_FLOOR="14.0"
export MACOSX_DEPLOYMENT_TARGET="$MACOS_FLOOR"

# The interpreter is bundled, so the macOS it was *built for* is the app's floor.
# Homebrew builds Python for the machine it is installed on: 1.2.0 shipped a
# Python.framework, libssl and libsqlite3 that needed macOS 27, because that is
# what the release machine ran. python.org's installer targets old macOS, which
# is why it is the build interpreter. Reads sysconfig's platform tag,
# e.g. macosx-10.15-universal2 (python.org) vs macosx-27.0-arm64 (Homebrew).
python_macos_ok() {
    "$1" -c "
import sys, sysconfig
v = sysconfig.get_platform().split('-')[1]
sys.exit(0 if tuple(map(int, v.split('.'))) <= tuple(map(int, '$MACOS_FLOOR'.split('.'))) else 1)
" 2>/dev/null
}

# Does $1 exist, run, clear the Python floor, and target an old enough macOS?
python_ok() {
    [ -n "${1:-}" ] || return 1
    "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PY_FLOOR_MAJOR, $PY_FLOOR_MINOR) else 1)" 2>/dev/null \
        && python_macos_ok "$1"
}

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    # python.org framework installs first, by full path: Homebrew's pythonX.Y
    # usually comes earlier on PATH and would otherwise win.
    for minor in 14 13 12 11 10; do
        for candidate in "/Library/Frameworks/Python.framework/Versions/3.$minor/bin/python3.$minor" \
                         "python3.$minor"; do
            command -v "$candidate" >/dev/null 2>&1 || continue
            python_ok "$candidate" || continue
            PYTHON="$(command -v "$candidate")"
            break 2
        done
    done
fi
PYTHON="${PYTHON:-python3}"

if ! python_ok "$PYTHON"; then
    echo "ERROR: $PYTHON is $("$PYTHON" -V 2>&1 || echo 'not runnable') for" >&2
    echo "       $("$PYTHON" -c 'import sysconfig; print(sysconfig.get_platform())' 2>/dev/null || echo '?')." >&2
    echo "       The app needs Python >= $PY_FLOOR (pyproject.toml) built for" >&2
    echo "       macOS $MACOS_FLOOR or older, because the interpreter is bundled and its" >&2
    echo "       floor becomes the app's. Homebrew's Python targets this Mac's own" >&2
    echo "       macOS, so it does not qualify. Install one from python.org:" >&2
    echo "         https://www.python.org/downloads/macos/" >&2
    echo "       or point the build at one you already have:" >&2
    echo "         PYTHON=/path/to/python3.X $0" >&2
    exit 1
fi

# A `.venv-build` from an earlier run is reused as-is, so a single build on the
# wrong interpreter would otherwise keep poisoning every later build — even
# after the PATH is fixed. Check what the existing one actually is.
if [ -d "$BUILD_VENV" ] && ! python_ok "$BUILD_VENV/bin/python"; then
    echo "==> $BUILD_VENV was built with an unsupported Python — recreating it"
    rm -rf "$BUILD_VENV"
fi
# No `|| true` here: if venv creation fails, the next line's `source` is what
# breaks, and its error points at activate instead of at the real cause.
if [ ! -d "$BUILD_VENV" ]; then
    echo "==> creating $BUILD_VENV with $PYTHON ($("$PYTHON" -V 2>&1))"
    "$PYTHON" -m venv "$BUILD_VENV"
fi
source "$BUILD_VENV/bin/activate"
pip install -q -r requirements.txt
pip install -q pywebview pyinstaller

# A running instance must be quit BEFORE the bundle is deleted below. Rebuilding
# over a live app leaves that process executing a deleted inode, and LaunchServices
# then treats Jamgraf as "already running" — so clicking the icon silently
# activates the zombie instead of launching the new build, which looks exactly
# like the app refusing to open. Quit it gracefully (never SIGKILL): the shutdown
# path flushes live CRDT edits, so an in-progress edit is saved rather than lost.
# Both names: during the rename the app running from dist/ is the OLD build,
# dist/Waikiki.app, and matching only the new name would wipe it out from under
# itself — exactly the zombie described above.
for APP in Jamgraf Waikiki; do
    EXE="$PWD/dist/$APP.app/Contents/MacOS/$APP"
    pgrep -f "$EXE" >/dev/null 2>&1 || continue
    echo "==> $APP is running from dist/ — quitting it before the rebuild"
    echo "    (This disconnects you. It happens because the app you are USING is"
    echo "     the build output. Install a copy and it stops happening for good:"
    echo "       brew install --cask jamgraf --no-quarantine"
    echo "     Your wikis live in ~/Library/Application Support/Jamgraf and are"
    echo "     shared, so an installed copy opens everything you already have,"
    echo "     keeps running through every release, and self-updates.)"
    osascript -e "quit app \"$APP\"" >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
        pgrep -f "$EXE" >/dev/null 2>&1 || break
        sleep 0.25
    done
    if pgrep -f "$EXE" >/dev/null 2>&1; then
        echo "ERROR: $APP is still running and would be rebuilt out from under" >&2
        echo "       itself. Quit it and re-run. (Not killing it — that would drop" >&2
        echo "       unsaved edits.)" >&2
        exit 1
    fi
done

rm -rf build dist
pyinstaller --noconfirm --clean jamgraf.spec

# Ad-hoc sign (no Apple Developer cert available for real notarization). This
# quiets some macOS security checks but does NOT bypass Gatekeeper for a
# downloaded app — first launch still needs right-click -> Open.
# The old executable name, as a symlink beside the real one. Every Claude Desktop
# config and doorman setup written before the rename launches
# .../Contents/MacOS/Waikiki; after an in-place update the bundle folder may be
# a symlink too (updater.finish_bundle_rename), so that full old path resolves
# here. Inside the bundle, so it is sealed by the signature like everything else.
ln -sf Jamgraf dist/Jamgraf.app/Contents/MacOS/Waikiki

# Every binary must run on MACOS_FLOOR. The interpreter check above covers
# Python itself; this covers wheels too. pip picks the newest wheel the *build*
# machine accepts, so a dependency that starts shipping macosx_15 wheels would
# quietly raise the floor, and the app would still pass the smoke test below on
# this Mac. Reads LC_BUILD_VERSION minos (vtool, from the Xcode command line tools).
too_new="$(find dist/Jamgraf.app -type f \( -name '*.so' -o -name '*.dylib' -o -perm +111 \) -print0 \
    | xargs -0 file | awk -F: '/Mach-O/{print $1}' \
    | while read -r bin; do
        minos="$(vtool -show-build "$bin" 2>/dev/null | awk '/minos/{print $2}' | sort -V | tail -1)"
        [ -n "$minos" ] || continue
        if [ "$(printf '%s\n%s\n' "$minos" "$MACOS_FLOOR" | sort -V | tail -1)" != "$MACOS_FLOOR" ]; then
            echo "  needs macOS $minos: ${bin#dist/Jamgraf.app/}"
        fi
    done)"
if [ -n "$too_new" ]; then
    echo "ERROR: the bundle will not run on macOS $MACOS_FLOOR:" >&2
    echo "$too_new" >&2
    exit 1
fi
echo "==> every binary runs on macOS $MACOS_FLOOR"

codesign --force --deep --sign - dist/Jamgraf.app 2>/dev/null || true
codesign --verify --deep --strict dist/Jamgraf.app 2>/dev/null \
    || echo "WARNING: dist/Jamgraf.app does not pass a strict signature check" >&2

# Smoke test: boot it headless (server only, no window) on a scratch data dir and
# a port nothing else uses, then check it serves its own version.
SOURCE_VER="$(python3 -c \
    "import re,pathlib;print(re.search(r'__version__ = \"([^\"]+)\"',pathlib.Path('jamgraf/__init__.py').read_text()).group(1))")"

# smoke_boot <label> <executable> <env prefix> <port>
smoke_boot() {
    local label="$1" exe="$2" pfx="$3" port="$4" data pid ok="" ver
    data="$(mktemp -d)"
    echo "==> smoke test ($label): booting headless on :$port"
    env "${pfx}HEADLESS=1" "${pfx}DATA=$data" "${pfx}PORT=$port" \
        "$exe" > "$data/smoke.log" 2>&1 &
    pid=$!
    # Generous: a cold first run may fetch the local embedding model.
    for _ in $(seq 1 120); do
        kill -0 "$pid" 2>/dev/null || break          # died: stop waiting
        if curl -sf -m 2 -o /dev/null "http://127.0.0.1:$port/"; then ok=1; break; fi
        sleep 1
    done
    if [ -n "$ok" ]; then
        # There is no /api/version route; the shell renders the version beside the
        # logo, which is the same string a user reads, so assert on that.
        ver="$(curl -sf -m 5 "http://127.0.0.1:$port/" 2>/dev/null \
            | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)"
    else
        echo "ERROR: the built app did not serve on :$port ($label) — refusing to ship it." >&2
        echo "--- last 30 lines of its output ---" >&2
        tail -30 "$data/smoke.log" >&2 || true
    fi
    # Wait for it to actually exit before removing the dir: kill is asynchronous,
    # and a still-writing process makes rm fail — which, under `set -e`, would
    # abort the build over nothing but cleanup.
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    rm -rf "$data" 2>/dev/null || true
    [ -n "$ok" ] || exit 1
    if [ "$ver" != "$SOURCE_VER" ]; then
        echo "ERROR: the built app serves version $ver, not $SOURCE_VER ($label)." >&2
        echo "       dist/ is stale or half-replaced. Refusing to ship it." >&2
        exit 1
    fi
    echo "==> smoke test passed ($label, serving $ver)"
}

# Prove the thing we just built actually RUNS before it can become a release
# asset. v0.21.0 shipped a signed, verified bundle that could not be opened: the
# signature proved the bytes were ours, nothing proved they started.
smoke_boot "Jamgraf" dist/Jamgraf.app/Contents/MacOS/Jamgraf JAMGRAF_ 8899
# And exactly as a pre-rename config or doorman starts it: the OLD executable
# name and the OLD env var names. If either compatibility path is broken, every
# existing Claude Desktop config breaks with it — so it fails the build here.
smoke_boot "as Waikiki" dist/Jamgraf.app/Contents/MacOS/Waikiki WAIKIKI_ 8898

# Zip the .app for a GitHub release asset (preserves the bundle structure).
cd dist
ditto -c -k --sequesterRsrc --keepParent "Jamgraf.app" "Jamgraf-macos.zip"
echo "Built: dist/Jamgraf.app"
echo "Asset: dist/Jamgraf-macos.zip"
du -sh Jamgraf.app Jamgraf-macos.zip
