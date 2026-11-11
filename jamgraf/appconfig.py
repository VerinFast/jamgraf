"""App-global config (not per-wiki), stored in DATA_DIR/app_config.json.

Per-wiki preferences live in each wiki's `settings` table; things that are truly
global to the install — like the access-log retention — live here so they don't
have to be duplicated per wiki.
"""
from __future__ import annotations

import json
import os
import threading

from . import config

_lock = threading.Lock()


def _path():
    return config.DATA_DIR / "app_config.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(key: str, default=None):
    return _load().get(key, default)


def set(key: str, value) -> None:
    """Write one key, replacing the file atomically.

    ``_load`` treats an unparseable file as an empty config, so a torn write here
    silently resets every app-global preference — including whether backups run.
    Write beside it and rename, same as the wiki registry.
    """
    with _lock:
        data = _load()
        data[key] = value
        path = _path()
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
