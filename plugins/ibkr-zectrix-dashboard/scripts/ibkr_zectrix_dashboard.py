#!/usr/bin/env python3
"""Compatibility entrypoint for the bundled IBKR ZECTRIX dashboard skill."""

from pathlib import Path
import runpy


SKILL_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "ibkr-zectrix-dashboard"
    / "scripts"
    / "ibkr_zectrix_dashboard.py"
)


if __name__ == "__main__":
    runpy.run_path(str(SKILL_SCRIPT), run_name="__main__")
else:
    exported = runpy.run_path(str(SKILL_SCRIPT))
    globals().update({name: value for name, value in exported.items() if not name.startswith("__")})
