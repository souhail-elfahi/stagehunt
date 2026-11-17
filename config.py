"""The user's settings, read once from config.yaml (generic template: config.example.yaml).

Everything specific to one person (search terms and locations, priority area, request budget,
fit-score weights, legal minimum pay, schedule, goals, time zone) lives in that file, so the
code itself holds no personal values. INTERNSHIP_HUNTER_CONFIG=<path> reads another file
(used by tests/test_config.py to check the template).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("INTERNSHIP_HUNTER_CONFIG") or ROOT / "config.yaml")


def load(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        raise SystemExit(f"{path.name} not found: copy config.example.yaml to config.yaml and fill it in "
                         "(the /setup command walks you through it).")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


CONFIG = load()
SEARCH, PRIORITY, FIT, RUNS, GOALS = (CONFIG["search"], CONFIG["priority"], CONFIG["fit_score"], CONFIG["runs"],
                                      CONFIG["goals"])
TIMEZONE = CONFIG["timezone"]
