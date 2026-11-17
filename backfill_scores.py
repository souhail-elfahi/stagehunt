"""Fill Score .. Remarque (V..Z) on existing sheet rows. Robot columns only, never LinkedIn.

A row is written when its Score is empty, or when it was scored from the title only
(Score fiable = non) and a description is now available. Reliable scores are never touched.
Descriptions come from:
  - offers_with_descriptions.csv (rows with fetch_status ok), matched on Lien / Autres liens;
  - logs/indeed_descriptions.csv, written by --indeed: the Indeed searches of the daily scan
    re-run (Indeed only, one request per search), matched on the Indeed job key (jk).
Rows without a description are scored from the title (Score fiable = non).

    python backfill_scores.py                 # dry run: what would be written
    python backfill_scores.py --indeed        # re-run the Indeed searches first (no LinkedIn)
    python backfill_scores.py --apply         # backup, then write
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter

import pandas as pd
from gspread.utils import ValueRenderOption

from find_internships import COUNTRY, INDEED_RESULTS, LOCATIONS, NARROW_LOCATIONS, PAUSE_SECONDS, build_query, search_plan
from fit_score import fit
from sheet_lib import ROOT, TAB, Layout, now_paris, open_spreadsheet, read_layout
from sync_sheet import SCORE_COLUMNS, backup, block_writes, is_blank, score_cells, split_links

SAVED = ROOT / "offers_with_descriptions.csv"
INDEED_CACHE = ROOT / "logs" / "indeed_descriptions.csv"
INDEED_HOURS_OLD = 14 * 24          # covers the sheet's recent rows
PAY_FIELDS = ["min_amount", "max_amount", "interval", "currency"]


def indeed_key(url) -> str | None:
    m = re.search(r"[?&]jk=([0-9a-f]+)", str(url or ""))
    return m.group(1) if m else None


def fetch_indeed() -> None:
    """Re-run the daily scan's searches on Indeed only and cache descriptions + pay fields."""
    from jobspy import scrape_jobs
    frames = []
    plan = search_plan(LOCATIONS, NARROW_LOCATIONS)
    for n, (loc, term, _) in enumerate(plan, 1):
        query = build_query(term)
        try:
            df = scrape_jobs(site_name=["indeed"], search_term=query, location=loc, country_indeed=COUNTRY,
                             results_wanted=INDEED_RESULTS, hours_old=INDEED_HOURS_OLD, verbose=0)
            print(f"  [{n}/{len(plan)}] {query!r} @ {loc}: {len(df)} results")
            frames.append(df)
        except Exception as exc:
            print(f"  [{n}/{len(plan)}] {query!r} @ {loc}: ERROR {type(exc).__name__}: {exc}")
        time.sleep(PAUSE_SECONDS)
    if not frames:
        raise SystemExit("no Indeed results")
    df = pd.concat(frames, ignore_index=True)
    df = df[[c for c in ["job_url", "description"] + PAY_FIELDS if c in df.columns]].drop_duplicates("job_url")
    INDEED_CACHE.parent.mkdir(exist_ok=True)
    df.to_csv(INDEED_CACHE, index=False, encoding="utf-8-sig")
    print(f"Indeed: {len(df)} offers with descriptions cached in {INDEED_CACHE.relative_to(ROOT)}")


def known_descriptions() -> tuple[dict[str, dict], dict[str, dict]]:
    """(by url, by Indeed jk) -> {description, pay fields}."""
    by_url, by_jk = {}, {}
    if SAVED.exists():
        d = pd.read_csv(SAVED, encoding="utf-8-sig")
        for r in d[d["fetch_status"] == "ok"].to_dict("records"):
            by_url[str(r["job_url"]).strip()] = {"description": r["description"]}
    if INDEED_CACHE.exists():
        for r in pd.read_csv(INDEED_CACHE, encoding="utf-8-sig").to_dict("records"):
            if isinstance(r.get("description"), str) and (jk := indeed_key(r["job_url"])):
                by_jk[jk] = {k: r.get(k) for k in ["description"] + PAY_FIELDS}
    return by_url, by_jk


def cells_of(f, n: int, layout: Layout) -> dict:
    return score_cells({"score_base": f.base, "score_fiable": "oui" if f.reliable else "non",
                        "gratification": f.pay, "duree": f.duration, "remarque": f.remark}, n, layout)


def plan_rows(rows: list[list], by_url: dict, by_jk: dict, layout: Layout, rescore: bool = False,
              formulas: list[list] | None = None) -> list[tuple[int, dict, object, str]]:
    """(row number, offer, Fit, reason) for every row to write. With rescore, rows whose score
    cells would change after a change of fit_score rules are rewritten too; a reliable score is
    still never replaced by a title-only one (its description must be available)."""
    out = []
    _cell = layout.cell
    for n, row in enumerate(rows, start=2):
        if is_blank(row):
            continue
        score, fiable = str(_cell(row, "Score")).strip(), str(_cell(row, "Score fiable")).strip()
        links = split_links(_cell(row, "Lien")) + split_links(_cell(row, "Autres liens"))
        extra = next((by_url[u] for u in links if u in by_url), None)
        extra = extra or next((by_jk[k] for u in links if (k := indeed_key(u)) and k in by_jk), None)
        offer = {"title": _cell(row, "Titre"), "company": _cell(row, "Entreprise"), "location": _cell(row, "Lieu"),
                 **(extra or {})}
        if not score:
            out.append((n, offer, fit(offer), "new"))
        elif fiable == "non" and extra:
            out.append((n, offer, fit(offer), "upgrade"))
        elif rescore and (extra or fiable == "non"):  # reliable rows only with their description
            f = fit(offer)
            current = [(formulas[n - 2][0] if formulas and n - 2 < len(formulas) and formulas[n - 2] else score),
                       fiable, _cell(row, "Gratification"), _cell(row, "Durée"), _cell(row, "Remarque")]
            new = cells_of(f, n, layout)
            if [str(new[c]) for c in SCORE_COLUMNS] != [str(x) for x in current]:
                out.append((n, offer, f, "rescore"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--indeed", action="store_true", help="re-run the Indeed searches first (no LinkedIn)")
    parser.add_argument("--apply", action="store_true", help="backup the sheet, then write V..Z")
    parser.add_argument("--rescore", action="store_true",
                        help="also rewrite scored rows whose cells change under the current fit_score rules")
    args = parser.parse_args()
    if args.indeed:
        fetch_indeed()

    ws = open_spreadsheet().worksheet(TAB)
    layout = read_layout(ws)   # columns by header; LayoutError before any write
    rows = ws.get(f"A2:{layout.last}", value_render_option=ValueRenderOption.unformatted, pad_values=True)
    score_col = layout.letter("Score")
    formulas = ws.get(f"{score_col}2:{score_col}", value_render_option=ValueRenderOption.formula) if args.rescore else None
    by_url, by_jk = known_descriptions()
    todo = plan_rows(rows, by_url, by_jk, layout, rescore=args.rescore, formulas=formulas)
    if args.rescore:
        for n, o, f, why in todo:
            if why == "rescore":
                print(f"  rescore row {n:>3}: {f.role:<20} {str(o['title'])[:55]:<55} {str(o['company'])[:20]}")
    age = {n: layout.cell(rows[n - 2], "Âge (jours)") for n, *_ in todo}

    reliable = sum(f.reliable for _, _, f, _ in todo)
    print(f"{len(todo)} rows to write: {reliable} with a description (Score fiable = oui), "
          f"{len(todo) - reliable} title only; {Counter(r for *_, r in todo)}")
    if todo:
        totals = pd.Series([f.total(age[n] if age[n] != "" else None) for n, _, f, _ in todo])
        print(f"scores: min {totals.min()}, median {totals.median():.0f}, max {totals.max()}")
        print(f"roles: {dict(Counter(f.role for _, _, f, _ in todo))}")
        print(f"pay known: {sum(f.pay is not None for *_, f, _ in todo)}, durations: "
              f"{dict(Counter(f.duration or '?' for *_, f, _ in todo).most_common(6))}, "
              f"RQTH: {sum(bool(f.remark) for *_, f, _ in todo)}")
        print("\ntop 10:")
        best = sorted(todo, key=lambda t: -t[2].total(age[t[0]] if age[t[0]] != "" else None))[:10]
        for n, o, f, _ in best:
            print(f"  row {n:>3} {f.total(age[n] if age[n] != '' else None):>3}{'' if f.reliable else '~'} "
                  f"{f.role:<20} {str(o['title'])[:48]:<48} {str(o['company'])[:22]}")
    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to write.")
        return

    print(f"Backup: {backup(ws, now_paris())}")
    updates = []
    for n, offer, f, _ in todo:
        updates += block_writes([cells_of(f, n, layout)], n, layout)
    # Rows are matched again right before writing: refuse if the sheet moved since the read.
    fresh = ws.get(f"{layout.letter('Lien')}2:{layout.letter('Lien')}", value_render_option=ValueRenderOption.unformatted)
    for n, *_ in todo:
        now_link = fresh[n - 2][0] if n - 2 < len(fresh) and fresh[n - 2] else ""
        if str(now_link).strip() != str(layout.cell(rows[n - 2], "Lien")).strip():
            raise SystemExit(f"row {n} changed since the read (rows were inserted or sorted): nothing written")
    for i in range(0, len(updates), 200):
        ws.batch_update(updates[i:i + 200], value_input_option="USER_ENTERED")
    print(f"Written: {len(updates)} rows (V..Z).")


if __name__ == "__main__":
    main()
