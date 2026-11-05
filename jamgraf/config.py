"""Configuration for Jamgraf.

Env-level knobs live here; per-install preferences that the UI can change
(active theme, which embedder to use) live in the `settings` table in the DB
so they can be edited from the browser without touching code.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import __version__ as VERSION  # single source of truth for the release

# The old name ---------------------------------------------------------------------
# Jamgraf was called Waikiki, and installs, Claude Desktop configs and doorman out in
# the world still say so. Every name below is read under Jamgraf first and Waikiki
# second, and the old names are never written. They stay indefinitely: removing
# one is its own decision, because the thing still saying it is usually a config
# file on somebody else's machine. See docs/rename-to-jamgraf.md.
LEGACY_NAME = "Waikiki"
_ENV_PREFIXES = ("JAMGRAF_", "WAIKIKI_")


def env(name: str, default: str | None = None) -> str | None:
    """`JAMGRAF_<name>`, else the old `WAIKIKI_<name>`, else `default`.

    An empty value counts as set. `JAMGRAF_UPDATE_PUBKEY=""` means "trust no
    keys", and falling through to a stale `WAIKIKI_UPDATE_PUBKEY` would quietly
    overrule the person who typed the new one.
    """
    for prefix in _ENV_PREFIXES:
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


_COOKIE_PREFIXES = ("jamgraf_", "waikiki_")


def cookie(cookies, name: str, default: str | None = None) -> str | None:
    """The `jamgraf_<name>` cookie, else the old `waikiki_<name>`.

    Read-only on purpose: nothing sets a `waikiki_` cookie any more, so a browser
    converges on the new name the next time that value is written. A browser
    visiting the app over HTTP — LAN sharing, or a tab on 127.0.0.1 — keeps its
    cookies across the rename, and a guest's share cookie is one of them.
    """
    for prefix in _COOKIE_PREFIXES:
        value = cookies.get(prefix + name)
        if value is not None:
            return value
    return default


def env_is_set(name: str) -> bool:
    """Whether either spelling of `name` is in the environment, even if empty."""
    return any(prefix + name in os.environ for prefix in _ENV_PREFIXES)


# Moving the data directory across the rename -----------------------------------
# What happened to the pre-rename data directory at startup, for Manage wikis to
# report: None (nothing to do), "moved", "both" or "failed". See below.
DATA_MIGRATION: dict | None = None


def migrate_legacy_data_dir(parent: Path, new_name: str = "Jamgraf",
                            old_name: str = LEGACY_NAME) -> tuple[Path, str | None]:
    """Move ``parent/Waikiki`` to ``parent/Jamgraf``. Returns (dir to use, outcome).

    This runs before the data directory is created, because creating it is the
    hazard: anything still naming the old path — a Claude Desktop config's
    WAIKIKI_DATA, an MCP server started before the upgrade, doorman — would be
    handed a fresh, EMPTY install there with nothing to say that its wikis were
    somewhere else. So the old path is left resolving to the new one:

      * old is a real directory, new absent -> rename, then symlink old -> new.
        Open files survive a rename (they follow the inode, not the path), and the
        symlink keeps every path-based caller working. If the symlink cannot be
        made the rename is undone, so the move is all or nothing.
      * new exists but is EMPTY -> remove it with rmdir, then as above. rmdir
        refuses a non-empty directory, so this cannot delete anything by
        construction.
      * both hold data -> touch neither. Use the new one and say so; merging two
        sets of wikis, or choosing one silently, is not a decision to make here.

    Serialised with flock on the parent, because the desktop app and an MCP
    server spawned by Claude can start in the same instant. Whichever is second
    finds the old path already a symlink and does nothing.
    """
    new, old = parent / new_name, parent / old_name
    if old.is_symlink() or not old.is_dir():
        return new, None                                  # nothing to move
    parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    fd = os.open(parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if old.is_symlink() or not old.is_dir():          # moved while we waited
            return new, None
        if new.is_symlink():
            return new, "both"        # somebody pointed it somewhere: theirs, not ours
        if new.exists():
            try:
                new.rmdir()                               # only ever succeeds if empty
            except OSError:
                return new, "both"
        try:
            os.rename(old, new)
        except OSError as exc:
            return old, f"failed: {exc}"
        try:
            os.symlink(new_name, old)                     # relative: survives a moved home
        except OSError as exc:
            try:
                os.rename(new, old)                       # put it back exactly as it was
            except OSError:
                return new, f"failed: moved, but could not link the old path back ({exc})"
            return old, f"failed: {exc}"
        return new, "moved"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# Project root and data location -------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Where a per-user install keeps its wikis, by platform convention.

    On macOS this is the same directory the packaged `.app` uses, deliberately:
    someone who installs the cask and also pip-installs the MCP server should
    find one set of wikis, not two.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local")
        return base / "Jamgraf"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Jamgraf"
    return Path(os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local" / "share") / "jamgraf"


def _installed_as_package() -> bool:
    """True when this copy lives in site-packages rather than a checkout."""
    if (ROOT / "pyproject.toml").is_file():
        return False
    return any(p.name in ("site-packages", "dist-packages") for p in ROOT.parents) \
        or ROOT.name in ("site-packages", "dist-packages")


def _checkout_or_user_data() -> Path:
    """The default data directory for an unfrozen install.

    A checkout keeps `./data`, which is what makes a dev copy self-contained and
    is why this was the only branch for a long time. A **pip install** must not:
    `ROOT` there is site-packages, so the default wrote the user's wikis inside
    the Python installation --- which `pip install -U` is entitled to replace,
    and which a machine-wide install does not let a normal user write at all.
    Since `DATA_DIR.mkdir()` runs at import, that was not a bad default but a
    `PermissionError` before the app could say anything.

    An existing `ROOT/data` is still used even for a package install. It should
    not be there, but if it is, it holds somebody's wikis --- and silently
    reading a different directory would look exactly like losing them.
    """
    if not _installed_as_package():
        return ROOT / "data"
    legacy = ROOT / "data"
    if legacy.is_dir() and any(legacy.iterdir()):
        print(f"[jamgraf] using the data directory already in place at {legacy}. "
              f"It sits inside the Python package, where an upgrade may remove "
              f"it: move it to {user_data_dir()} (or set JAMGRAF_DATA).",
              file=sys.stderr)
        return legacy
    return user_data_dir()
if env("DATA"):
    # Named explicitly, so used exactly as named. A pre-rename config naming the
    # old default still works, because after the move that path is a symlink.
    _default_data = Path(env("DATA"))
elif getattr(sys, "frozen", False):
    # Packaged .app: never write inside the (possibly read-only) bundle.
    _support = Path.home() / "Library" / "Application Support"
    _default_data, _outcome = migrate_legacy_data_dir(_support)
    if _outcome:
        DATA_MIGRATION = {"outcome": _outcome, "old": str(_support / LEGACY_NAME),
                          "new": str(_support / "Jamgraf")}
        print(f"[jamgraf] data directory from before the rename: {_outcome} "
              f"({_support / LEGACY_NAME} -> {_default_data})", file=sys.stderr)
else:
    _default_data = _checkout_or_user_data()
DATA_DIR = _default_data
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Legacy single-DB path (pre multi-wiki). Migrated into wikis/main.db on first run.
# The FILE keeps its historical name: it is whatever a pre-multi-wiki install left
# on disk, which was written long before the rename and is still called this.
DB_PATH = Path(env("DB") or DATA_DIR / "waikiki.db")
# Each wiki is an isolated SQLite file under here — no cross-wiki links possible.
WIKIS_DIR = DATA_DIR / "wikis"
# Wikis created automatically on first run (besides migrated "main").
SEED_WIKIS = ["Beaconlight", "Crosslake", "StartupOS"]
# Slug of the built-in Help wiki (seeded with help text + the AI system prompts).
HELP_WIKI = "help"

# True when running inside the packaged desktop shell (set by jamgraf_app before
# the server starts). The page uses it to choose the native dictation route,
# because WKWebView has no SpeechRecognition.
def native_dictation() -> bool:
    """Whether the desktop shell around us offers its own dictation route.

    Separate from :func:`is_desktop` because the page has to choose between a
    native route and the browser's speech API, and "I am in a window" does not
    answer that -- macOS has a native route, a WebView2 window has none and uses
    the web one. Set by ``jamgraf.desktop`` from the API class it actually
    installed, so there is one source of truth and the page never guesses.
    """
    return env("NATIVE_DICTATION") == "1"


def is_desktop() -> bool:
    return env("DESKTOP") == "1"


# Server -------------------------------------------------------------------------
HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8787"))

# Where the MCP server reaches the running web app to inject live edits.
WEB_URL = env("WEB_URL", f"http://{HOST}:{PORT}")

# AI (Claude) --------------------------------------------------------------------
# Resolves ANTHROPIC_API_KEY / ant-login profile via the SDK automatically.
ANTHROPIC_MODEL = env("MODEL", "claude-opus-4-8")

# Voyage (Anthropic-recommended cloud embeddings). Key read at call time.
VOYAGE_API_KEY_ENV = "VOYAGE_API_KEY"

# Defaults for the settings table (seeded on first run; editable in the UI) -------
DEFAULT_SETTINGS = {
    "theme": "default",
    # Active embedder: provider in {fastembed, local, voyage} + a model slug.
    # fastembed (ONNX, no torch) is the default so the packaged app stays small.
    "embedder_provider": "fastembed",
    "embedder_model": "BAAI/bge-small-en-v1.5",  # dim 384
    "model_library": '[{"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"}]',
    # Retention (per wiki): keep last N versions per page; purge trashed pages
    # after N days. 0 = unlimited / never.
    "retention_versions": "50",
    "retention_trash_days": "30",
    # Allow raw HTML in markdown (per wiki). On by default: all content is local
    # and only editable by the user and their local agent, so HTML is safe here.
    "allow_html": "1",
    # AI text generation (the editor's "Generate" button). Provider is
    # {anthropic (cloud), ollama (local, e.g. phi3)}.
    "gen_provider": "anthropic",
    "gen_model": ANTHROPIC_MODEL,        # used when gen_provider == anthropic
    "gen_model_local": "phi3",           # an Ollama model tag, when == ollama
    "ollama_url": "http://localhost:11434",
    # Chat-with-an-article, powered by a local CLI {claude, gemini}. An empty
    # model means "use the CLI's own default".
    "chat_provider": "claude",
    "chat_model": "",
    # Image generation: `claude` writes the prompt, then this CLI (agy/gemini)
    # renders the PNG into the article's own folder. Empty model = CLI default.
    "image_cli": "agy",
    "image_model": "",
    # A per-wiki house style applied to every generated image (e.g. "clockwork
    # punk pixel art RPG in the style of Dragon Warrior 7").
    "image_style_prompt": "",
}

# Built-in page templates seeded into each wiki (editable/removable in the UI).
DEFAULT_TEMPLATES = [
    ("Meeting notes",
     "# {{title}}\n\n**Date:** \n**Attendees:** \n\n## Agenda\n- \n\n"
     "## Notes\n\n## Action items\n- [ ] \n"),
    ("How-to",
     "# {{title}}\n\n> One-line summary.\n\n## Prerequisites\n- \n\n"
     "## Steps\n1. \n2. \n\n## See also\n- [[ ]]\n"),
    ("Person",
     "# {{title}}\n\n| | |\n|---|---|\n| Role | |\n| Team | |\n| Contact | |\n\n"
     "## About\n\n## Notes\n"),
]

# Built-in custom element(s) seeded into each wiki (editable/removable in the UI).
# Each: (slug, name, fields-json, shadow-DOM html, scoped css, encapsulated js).
# fields: [{"name","required","label"}]. Required fields must be present in a
# block or it renders an error. Extra fields flow through as rows.
DEFAULT_ELEMENTS = [
    (
        "infobox",
        "Infobox",
        '[{"name": "title", "required": true, "label": "Title"}]',
        '<div class="ib">\n'
        '  <div class="ib-title"></div>\n'
        '  <div class="ib-img"></div>\n'
        '  <table class="ib-rows"></table>\n'
        '</div>',
        ".ib{float:right;max-width:300px;margin:0 0 1rem 1.2rem;border:1px solid "
        "var(--border,#ccc);border-radius:10px;overflow:hidden;background:var(--surface,#fff);"
        "font-family:var(--font,sans-serif);font-size:.9rem;color:var(--text,#111)}\n"
        ".ib-title{background:var(--accent,#0969da);color:var(--accent-contrast,#fff);"
        "font-weight:700;padding:.5rem .7rem;text-align:center}\n"
        ".ib-img img{width:100%;display:block}\n"
        ".ib-rows{width:100%;border-collapse:collapse}\n"
        ".ib-rows th,.ib-rows td{border-top:1px solid var(--border,#eee);padding:.35rem .6rem;"
        "text-align:left;vertical-align:top}\n"
        ".ib-rows th{width:40%;color:var(--muted,#666);font-weight:600}",
        "// `html` holds each field already rendered by the server, so [[wiki\n"
        "// links]] in a value become real links — no link parsing needed here.\n"
        "root.querySelector('.ib-title').textContent = props.title || '';\n"
        "var img = props.image || props.Image;\n"
        "if (img) { var im = document.createElement('img'); im.src = img; "
        "im.alt = props.title || ''; root.querySelector('.ib-img').appendChild(im); }\n"
        "var tb = root.querySelector('.ib-rows');\n"
        "Object.keys(props).forEach(function (k) {\n"
        "  if (k.toLowerCase() === 'title' || k.toLowerCase() === 'image') return;\n"
        "  var tr = document.createElement('tr');\n"
        "  var th = document.createElement('th'); th.textContent = k;\n"
        "  var td = document.createElement('td');\n"
        "  td.innerHTML = (html && html[k] != null) ? html[k] : '';\n"
        "  tr.appendChild(th); tr.appendChild(td); tb.appendChild(tr);\n"
        "});",
    ),
]

# Retrieval / chunking -----------------------------------------------------------
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
RAG_TOP_K = 6
RRF_K = 60  # reciprocal-rank-fusion constant


def claude_desktop_config_path() -> Path:
    """Where Claude Desktop keeps the file the Connect page tells you to edit.

    Three different places, and the page is useless if it names the wrong one:
    someone follows it, finds nothing there, and creates a second config the app
    will never read. Here rather than in the route because it is a fact about
    this platform, which is what this module is for.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA")
                    or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Claude" / "claude_desktop_config.json"


def mcp_server_config(web_url: str | None = None) -> dict:
    """The MCP server definition for *this* install, ready to hand to a client.

    Shared by the Connect-Claude page (copy-paste into Claude Desktop) and by the
    chat feature, which passes it to the CLI with --mcp-config so the agent can
    read the wiki it is answering about instead of only seeing what we pasted
    into its prompt.

    Handles both shapes of install: the packaged .app relaunches its own binary
    in MCP mode, while a source checkout runs the module.
    """
    import sys

    env = {"JAMGRAF_DATA": str(DATA_DIR), "JAMGRAF_WEB_URL": web_url or WEB_URL}
    if getattr(sys, "frozen", False):
        server = {"command": sys.executable, "env": {**env, "JAMGRAF_MCP": "1"}}
    else:
        server = {"command": sys.executable, "args": ["-m", "jamgraf.mcp_server"],
                  "env": {**env, "PYTHONPATH": str(ROOT)}}
    return {"mcpServers": {"jamgraf": server}}
