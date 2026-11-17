"""Offline checks of the settings files (no network).

    .venv/Scripts/python tests/test_config.py

- config.yaml and config.example.yaml have the same structure (same keys, same value types),
  so the template never falls behind the real file.
- With config.example.yaml alone (as a new user starts), every module imports and the search
  plans, request counts and fit scores work: run in a subprocess with INTERNSHIP_HUNTER_CONFIG.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def shape(node, path=""):
    """Every key path with the type of its value (lists and dicts of free keys are not walked)."""
    if isinstance(node, dict) and not path.endswith(("locations", "regions", "points", "roles")):
        return {k: v for key, val in node.items() for k, v in shape(val, f"{path}.{key}").items()}
    return {path: type(node).__name__}


def test_same_structure() -> None:
    real = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    a, b = shape(real), shape(example)
    missing, extra = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    types = sorted(k for k in set(a) & set(b) if a[k] != b[k] and {a[k], b[k]} != {"int", "float"})
    print(f"structure: {len(a)} settings in config.yaml; missing from the example: {missing or 'none'}; "
          f"only in the example: {extra or 'none'}; type mismatches: {types or 'none'}")
    assert not missing and not extra and not types


CHECK = r"""
import sys
sys.path.insert(0, sys.argv[1])
import find_internships as fi, fit_score as fs, run_hunt as rh, setup_sheet as ss, sheet_lib as sl
plans = {"cli": (fi.LOCATIONS, fi.NARROW_LOCATIONS), "full": rh.FULL_SCAN[1:], "quick": rh.QUICK_SCAN[1:]}
counts = {k: fi.request_count(*v) for k, v in plans.items()}
inside = fs.place_points(fs.PRIORITY["name"] + ", France")
f = fs.fit({"title": "Stage Data Analyst", "company": "Acme", "location": "Villeurbanne, France"})
print("priority:", fs.PRIORITY["name"], fs.PRIORITY["region"], "| requests", counts,
      "| place inside", inside, "| score", f.total(0), "| goals", ss.GOALS, "| tz", sl.TZ)
assert inside == fs.PLACE_AREA and all(1 <= c <= 45 for c in counts.values()) and f.total(0) > 0
assert ss.FILTER_VIEWS[2][0] == "🏠 " + fs.PRIORITY["region"]
"""


def test_example_works_alone() -> None:
    env = {**os.environ, "INTERNSHIP_HUNTER_CONFIG": str(ROOT / "config.example.yaml")}
    out = subprocess.run([sys.executable, "-c", CHECK, str(ROOT)], env=env, capture_output=True, text=True,
                         cwd=ROOT)
    print("example config:", out.stdout.strip() or out.stderr.strip()[-600:])
    assert out.returncode == 0, out.stderr[-1500:]


if __name__ == "__main__":
    test_same_structure()
    test_example_works_alone()
    print("ALL OK")
