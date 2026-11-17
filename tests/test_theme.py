"""Offline checks of the sheet theme built by setup_sheet.py (no network).

    .venv/Scripts/python tests/test_theme.py

- Every text/background pair the setup writes passes WCAG AA (>= 4.5:1). Text whose
  background is not set by the same format is checked against both the page background
  and the panel colour, the two backgrounds it can sit on.
- The Score column's dark text is checked against the colour scale's three points.
- No colour is green (hue 75°-165°): the palette has no lime/pistachio left.
"""

from __future__ import annotations

import colorsys
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import setup_sheet as s  # noqa: E402
from sheet_lib import COLORS, IDX  # noqa: E402

AA = 4.5


def lum(c: dict) -> float:
    def ch(x):
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.get("red", 0)) + 0.7152 * ch(c.get("green", 0)) + 0.0722 * ch(c.get("blue", 0))


def ratio(a: dict, b: dict) -> float:
    la, lb = sorted([lum(a), lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def name(c: dict) -> str:
    return "#" + "".join(f"{round(c.get(k, 0) * 255):02X}" for k in ("red", "green", "blue"))


def formats(requests: list[dict]):
    """(where, format) for every repeatCell and conditional-format rule."""
    for r in requests:
        if "repeatCell" in r:
            yield r["repeatCell"]["range"], r["repeatCell"]["cell"]["userEnteredFormat"]
        elif "addConditionalFormatRule" in r and "booleanRule" in r["addConditionalFormatRule"]["rule"]:
            rule = r["addConditionalFormatRule"]["rule"]
            yield rule["ranges"][0], rule["booleanRule"]["format"]


def all_colors(obj):
    if isinstance(obj, dict):
        if set(obj) <= {"red", "green", "blue", "alpha"} and obj:
            yield obj
        for v in obj.values():
            yield from all_colors(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_colors(v)


def main() -> None:
    bg, panel = s.rgb(COLORS["bg"]), s.rgb(COLORS["panel"])
    sheets = {"Offres": s.offres_requests(1, {}),
              "dashboard": s.dashboard_requests(2, {}), "Mes candidatures": s.applications_requests(3, {}),
              "Guide": s.guide_requests(4, {}, s.load_guide())}
    score_col = IDX["Score"]
    failures, checked = [], 0
    for sheet, requests in sheets.items():
        for where, f in formats(requests):
            fg = f.get("textFormat", {}).get("foregroundColor")
            if not fg:
                continue
            if sheet == "Offres" and where.get("startColumnIndex") == score_col and "backgroundColor" not in f:
                continue  # Score text sits on the colour scale: checked below
            backs = [f["backgroundColor"]] if "backgroundColor" in f else [bg, panel]
            for back in backs:
                checked += 1
                if ratio(fg, back) < AA:
                    failures.append(f"{sheet} {where}: {name(fg)} on {name(back)} = {ratio(fg, back):.1f}")
    score_fg = s.rgb(COLORS["bg"])
    for point in ("minpoint", "midpoint", "maxpoint"):
        checked += 1
        back = s.SCALE[point]["color"]
        if ratio(score_fg, back) < AA:
            failures.append(f"Score scale {point}: {name(score_fg)} on {name(back)} = {ratio(score_fg, back):.1f}")
    print(f"contrast: {checked} text/background pairs checked, {len(failures)} under {AA}:1")
    for line in failures:
        print("  FAIL", line)

    greens = set()
    for requests in sheets.values():
        for c in all_colors(requests):
            h, l_, sat = colorsys.rgb_to_hls(c.get("red", 0), c.get("green", 0), c.get("blue", 0))
            if sat > 0.25 and 75 <= h * 360 <= 165 and 0.15 < l_ < 0.95:
                greens.add(name(c))
    print("green colours:", sorted(greens) or "none")
    assert not failures and not greens


if __name__ == "__main__":
    main()
    print("ALL OK")
