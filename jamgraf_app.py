#!/usr/bin/env python3
"""Jamgraf desktop app — the entry point PyInstaller bundles into Jamgraf.app.

The shell itself lives in :mod:`jamgraf.desktop`, which is shared with the
``jamgraf-app`` console script a ``pip install jamgraf[desktop]`` provides. This
file stays at the repo root because ``jamgraf.spec`` names it, and because
``python jamgraf_app.py`` is the documented way to run the app from a checkout.

    python jamgraf_app.py
"""
from __future__ import annotations

from jamgraf.desktop import main

if __name__ == "__main__":
    main()
