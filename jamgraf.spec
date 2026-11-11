# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Jamgraf.app (macOS).

Bundles the FastAPI server + pywebview shell. Uses fastembed (ONNX) for
embeddings; PyTorch / sentence-transformers are excluded to keep the app small.
"""
from PyInstaller.utils.hooks import collect_all
import glob
import os
import sqlite_vec

# Stamp the real release into the bundle. The updater compares a downloaded
# bundle's CFBundleShortVersionString against the release tag before installing
# it, so a hardcoded version here would make every genuine update look like a
# mismatch (and Finder's Get Info would lie too).
#
# Read by parsing, not importing: PyInstaller exec's this spec without the repo
# root on sys.path, and importing the package here would drag its dependencies
# into the spec's own namespace.
import pathlib
import re as _re

JAMGRAF_VERSION = _re.search(
    r'^__version__\s*=\s*"([^"]+)"',
    pathlib.Path("jamgraf/__init__.py").read_text(),
    _re.M,
).group(1)

datas = [
    ("jamgraf/templates", "jamgraf/templates"),
    ("jamgraf/static", "jamgraf/static"),
]
binaries = []
hiddenimports = [
    "apsw",
    "webview.platforms.cocoa",
    "objc", "Foundation", "WebKit", "AppKit", "Quartz", "Cocoa", "PyObjCTools",
]

# Packages with native libs / data files that PyInstaller won't fully auto-detect.
for pkg in [
    "fastembed", "onnxruntime", "tokenizers", "huggingface_hub", "sqlite_vec",
    "pycrdt", "fastmcp", "anthropic", "markdown_it", "mdit_py_plugins",
    "linkify_it", "uvicorn", "anyio", "pydantic", "webview",
    "xhtml2pdf", "reportlab",
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# The sqlite-vec loadable extension must sit next to its package so
# sqlite_vec.loadable_path() resolves inside the bundle. loadable_path() omits
# the platform suffix, so glob for the real file (vec0.dylib / vec0.so).
for _ext in glob.glob(sqlite_vec.loadable_path() + ".*"):
    binaries += [(_ext, "sqlite_vec")]

a = Analysis(
    ["jamgraf_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The optional "local" embedding provider (requirements-local.txt) is
        # never bundled — it needs PyTorch and would dwarf the app.
        "torch", "sentence_transformers", "transformers", "tkinter",
        # ...but excluding sentence_transformers alone still left 32MB of scipy
        # in the bundle, pulled in through the graph rather than by anything we
        # call. Nothing in fastembed imports scipy, and it never loads while
        # rendering, embedding, searching or exporting a PDF. Its only declared
        # users here are sentence-transformers, scikit-learn and networkx — all
        # part of the stack above.
        "scipy", "sklearn", "networkx",
        # NOT excluded: hf_xet (7.4MB), huggingface_hub's download accelerator.
        # It looks safe on paper — is_xet_available() guards it and there is an
        # ImportError fallback — but the path it serves is the very first model
        # download on a new install, and that resisted every attempt to
        # reproduce here (fastembed ignores HF_HOME/HF_HUB_CACHE and fell back
        # to an existing cache each time). Breaking it would break a fresh
        # install, so it stays until someone can test a genuinely cold download.
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Jamgraf",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Jamgraf")

app = BUNDLE(
    coll,
    name="Jamgraf.app",
    icon="assets/Jamgraf.icns",
    bundle_identifier="com.verinfast.jamgraf",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSBackgroundOnly": False,
        "CFBundleShortVersionString": JAMGRAF_VERSION,
        "CFBundleVersion": JAMGRAF_VERSION,
        # So an older Mac refuses to open it with a clear message instead of
        # crashing at launch. build_macos.sh sets the floor and checks every
        # binary in the bundle against it.
        "LSMinimumSystemVersion": os.environ.get("MACOSX_DEPLOYMENT_TARGET", "14.0"),
        # jamgraf:// deep links. Registering the scheme is what makes macOS
        # deliver a GURL Apple Event to us; jamgraf_app._install_deeplink_handler
        # receives it and jamgraf/deeplink.py decides what it's allowed to mean.
        "CFBundleURLTypes": [{
            "CFBundleURLName": "com.verinfast.jamgraf.deeplink",
            "CFBundleTypeRole": "Viewer",
            "CFBundleURLSchemes": ["jamgraf", "waikiki"],   # waikiki: links made before the rename
        }],
    },
)
