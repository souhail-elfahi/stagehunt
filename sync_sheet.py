"""Upsert scraped offers into the "Offres" sheet without touching the user's columns.

- New offers are inserted as a block at row 2 (Nouveau = oui, Statut = À trier).
- Offers already in the sheet (same Lien or Autres liens, or same title|company|city)
  only get Dernière détection and En ligne updated, cell by cell.
- Cross-site duplicates (dedup.classify "merge") are not inserted: their link goes to
  the matching row's Autres liens. Look-alikes from unrelated companies ("twin") are
  inserted with Doublon ? pointing at each other.
- Every run: Nouveau -> non after 24h, En ligne -> ? after 7 days without detection.
- The sheet is backed up to backups/ before anything is written.

First run: python sync_sheet.py --import internships_2026-11-16.csv
Fill derived cells: python sync_sheet.py --backfill [--apply]   (Ville, Région, suspect companies)
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import gspread
import pandas as pd
from gspread.utils import ValueRenderOption

from dedup import classify
from fit_score import COMPANY_CHECK, annotate, company_suspect, score_formula
from regions import UNKNOWN, UNMAPPED, canonical_villes, region_of, ville_key, ville_of
from find_internships import dedup_key
from sheet_lib import (DEFAULT_STATUS, ROOT, TAB, Layout, as_text, fmt_dt,
                       from_serial, now_paris, open_spreadsheet, read_layout)

log = logging.getLogger("sync")

BACKUP_DIR = ROOT / "backups"
REHEARSAL_BACKUP_DIR = BACKUP_DIR / "rehearsal"
KEEP_BACKUPS = 20
NEW_FOR = timedelta(hours=24)
ONLINE_FOR = timedelta(days=7)


@dataclass
class SyncResult:
    new: list[dict] = field(default_factory=list)  # inserted offers, newest first
    seen: int = 0                                   # existing rows detected again
    merged: int = 0                                 # offers merged as cross-site duplicates
    flags: int = 0                                  # Nouveau / En ligne cells reset
    backup: str = ""


def backup(ws: gspread.Worksheet, stamp: datetime, rehearsal: bool = False) -> str:
    """CSV copy of the sheet. A rehearsal spreadsheet's backups go to backups/rehearsal/, never
    among the tracker's (only the last KEEP_BACKUPS are kept, and pending_sync reads the latest)."""
    folder = REHEARSAL_BACKUP_DIR if rehearsal else BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    # Seconds in the name: a queue flush and the run's own sync can happen in the same minute.
    path = folder / f"sheet_{stamp:%Y-%m-%d_%H%M%S}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(ws.get_all_values())
    for old in sorted(folder.glob("sheet_*.csv"))[:-KEEP_BACKUPS]:
        old.unlink()
    return str(path.relative_to(ROOT))


def is_blank(row: list) -> bool:
    return not any(str(v).strip() for v in row)


def has_user_data(row: list, layout: Layout) -> bool:
    """True if the user typed anything outside the robot columns: L..R ("À trier" alone counts as
    empty) and the user's own extra columns."""
    for i, value in enumerate(row):
        if i in layout.robot or not str(value).strip():
            continue
        if i == layout.idx["Statut"] and str(value).strip() == DEFAULT_STATUS:
            continue
        return True
    return False


def split_links(cell) -> list[str]:
    return [u.strip() for u in str(cell or "").splitlines() if u.strip()]


def sheet_offer(row: list, layout: Layout) -> dict:
    """A sheet row (unformatted read) as a dedup.classify() input."""
    c = layout.cell
    published = from_serial(c(row, "Date publication"))
    return {"title": c(row, "Titre"), "company": c(row, "Entreprise"), "location": c(row, "Lieu"),
            "date": published.date() if published else None, "url": str(c(row, "Lien")).strip(),
            "site": str(c(row, "Site")).strip()}


def scraped_offer(offer: dict) -> dict:
    """A clean() record as a dedup.classify() input."""
    try:
        published = date.fromisoformat(str(offer.get("date_posted")))
    except ValueError:
        published = None
    return {"title": offer.get("title"), "company": offer.get("company"), "location": offer.get("location"),
            "date": published, "url": str(offer.get("job_url") or "").strip(),
            "site": str(offer.get("site") or "").strip()}


def twin_formula(url: str, company, layout: Layout) -> str:
    """'ligne N · Company' for the twin, found by its link so it stays right when rows move."""
    u = str(url).replace('"', '""')
    c = as_text(company).lstrip("'").replace('"', '""')
    lien = layout.letter("Lien")
    return f'=IFERROR("ligne "&MATCH("{u}";${lien}:${lien};0)&" · {c}";"{c}")'


SCORE_COLUMNS = ["Score", "Score fiable", "Gratification", "Durée", "Remarque"]


def score_cells(fit: dict, row: int, layout: Layout) -> dict:
    """Score (live formula) .. Remarque, from fit_score.annotate(), by column name."""
    pay = fit.get("gratification")
    values = [score_formula(fit["score_base"], row, layout.letter("Âge (jours)")), fit["score_fiable"],
              "" if pay is None or pd.isna(pay) else int(pay), fit["duree"], fit["remarque"]]
    return dict(zip(SCORE_COLUMNS, values))


def _new_row(offer: dict, r: int, detected: datetime, mark_new: bool, layout: Layout, ville: str) -> dict:
    """Every robot column of an inserted offer, plus Statut = "À trier", by column name."""
    date_col = layout.letter("Date publication")
    return {
        "Titre": as_text(offer.get("title")), "Entreprise": as_text(offer.get("company")),
        "Lieu": as_text(offer.get("location")), "Site": as_text(offer.get("site")),
        "Date publication": as_text(offer.get("date_posted")),
        "Âge (jours)": f'=IF({date_col}{r}="";"";TODAY()-{date_col}{r})',  # fr_FR: ';' separators
        "Lien": as_text(offer.get("job_url")), "Première détection": fmt_dt(detected),
        "Dernière détection": fmt_dt(detected),
        "En ligne": "oui" if detected >= now_paris() - ONLINE_FOR else "?",
        "Nouveau": "oui" if mark_new else "non",
        "Statut": DEFAULT_STATUS,
        "Autres liens": "\n".join(offer["_links"]), "Doublon ?": offer["_twin"],
        "Région": region_of(offer.get("location")), "Ville": ville,
        **score_cells(offer, r, layout),
    }



def block_writes(rows: list[dict], first_row: int, layout: Layout) -> list[dict]:
    """batch_update entries for consecutive rows given by column name: one range per run of
    adjacent columns in the actual layout, each checked so it never covers a user column."""
    updates = []
    names = [c for c in rows[0] if c in layout.idx]
    for run in layout.runs(names):
        a1 = f"{layout.letter(run[0])}{first_row}:{layout.letter(run[-1])}{first_row + len(rows) - 1}"
        layout.check_robot_range(a1, allow_statut="Statut" in run)
        updates.append({"range": a1, "values": [[r[c] for c in run] for r in rows]})
    return updates


def sync(offers: pd.DataFrame, *, detected: datetime | None = None, mark_new: bool = True,
         prepare: Callable[[list[dict]], None] | None = None, sheet_id: str | None = None) -> SyncResult:
    """offers: output of find_internships.clean(). detected: detection time (default now).
    prepare(new_offers) runs on the offers about to be inserted, before they are scored
    (run_hunt fetches their LinkedIn descriptions there). Columns are located by their header
    (row 1, read now); a missing or duplicated header raises LayoutError before any write."""
    now = now_paris()
    detected = detected or now
    sh = open_spreadsheet(sheet_id)
    ws = sh.worksheet(TAB)
    layout = read_layout(ws)   # LayoutError: nothing written
    c = layout.cell
    res = SyncResult(backup=backup(ws, now, rehearsal=sheet_id is not None))

    # Unformatted read: dates come back as serial numbers, independent of the sheet locale.
    rows = ws.get(f"A2:{layout.last}", value_render_option=ValueRenderOption.unformatted, pad_values=True)
    by_url, by_key = {}, {}
    existing: list[tuple[int, dict]] = []
    for n, row in enumerate(rows, start=2):
        if is_blank(row):
            continue
        for url in split_links(c(row, "Lien")) + split_links(c(row, "Autres liens")):
            by_url[url] = n  # links merged into this row still point to it
        by_key[dedup_key(c(row, "Titre"), c(row, "Entreprise"), c(row, "Lieu"))] = n
        existing.append((n, sheet_offer(row, layout)))
    # One spelling per city across the sheet ("Villeneuve d'Ascq" / "Villeneuve-d'Ascq" merge).
    villes = canonical_villes([c(r, "Ville") for r in rows] + [ville_of(c(r, "Lieu")) for r in rows]
                              + [ville_of(o) for o in offers.get("location", pd.Series(dtype=str))])

    def ville_for(location) -> str:
        v = ville_of(location)
        return villes.get(ville_key(v), v)

    seen_rows: set[int] = set()
    more_links: dict[int, list[str]] = {}  # row -> links of cross-site duplicates merged into it
    twin_of_row: dict[int, str] = {}       # row -> Doublon ? formula pointing to a new offer
    fresh: list[dict] = []
    fresh_ids: set[str] = set()
    for offer in offers.to_dict("records"):
        url = str(offer.get("job_url") or "").strip()
        key = dedup_key(offer.get("title"), offer.get("company"), offer.get("location"))
        if n := by_url.get(url) or by_key.get(key):
            seen_rows.add(n)
            continue
        if url in fresh_ids or key in fresh_ids:
            continue
        fresh_ids.update({url, key} - {""})
        o = scraped_offer(offer)

        # Fuzzy rules (dedup.classify): merge into a sheet row or an earlier new offer, or flag a twin.
        verdicts = [(classify(o, so), ("row", n, so)) for n, so in existing]
        verdicts += [(classify(o, f["_o"]), ("fresh", f, f["_o"])) for f in fresh]
        merge = next((t for v, t in verdicts if v == "merge"), None)
        twin = next((t for v, t in verdicts if v == "twin"), None)
        if merge and merge[0] == "row":
            n = merge[1]
            seen_rows.add(n)
            more_links.setdefault(n, []).append(url)
            res.merged += 1
            continue
        if merge:
            merge[1]["_links"].append(url)
            res.merged += 1
            continue
        offer.update(_o=o, _links=[], _twin="")
        if twin:
            kind, target, to = twin
            offer["_twin"] = twin_formula(to["url"], to["company"], layout)
            if kind == "row":
                twin_of_row.setdefault(target, twin_formula(url, o["company"], layout))
            elif not target["_twin"]:
                target["_twin"] = twin_formula(url, o["company"], layout)
        fresh.append(offer)

    # ---- Updates on existing rows (row numbers are still valid: nothing inserted yet) ----
    updates: list[dict] = []

    def put(name: str, n: int, value: str) -> None:
        a1 = f"{layout.letter(name)}{n}"
        layout.check_robot_range(a1)
        updates.append({"range": a1, "values": [[value]]})

    for n, row in enumerate(rows, start=2):
        if is_blank(row):
            continue
        if n in more_links:
            links = split_links(c(row, "Autres liens"))
            links += [u for u in more_links[n] if u not in links and u != str(c(row, "Lien")).strip()]
            put("Autres liens", n, "\n".join(links))
        if n in twin_of_row and not str(c(row, "Doublon ?")).strip():
            put("Doublon ?", n, twin_of_row[n])
        region = str(c(row, "Région")).strip()
        if not region or region == UNKNOWN:  # backfill, and retry "?" after regions.py gains a mapping
            if (found := region_of(c(row, "Lieu"))) != region:
                put("Région", n, found)
        if not str(c(row, "Ville")).strip() and (v := ville_for(c(row, "Lieu"))):
            put("Ville", n, v)
        remark = str(c(row, "Remarque")).strip()
        if company_suspect(c(row, "Entreprise"), c(row, "Titre")) and COMPANY_CHECK not in remark:
            put("Remarque", n, f"{remark} · {COMPANY_CHECK}" if remark else COMPANY_CHECK)
        last = from_serial(c(row, "Dernière détection"))
        first = from_serial(c(row, "Première détection"))
        if n in seen_rows:
            if last is None or detected > last:
                put("Dernière détection", n, fmt_dt(detected))
                last = detected
            if c(row, "En ligne") != "oui":
                put("En ligne", n, "oui")
            res.seen += 1
        if c(row, "Nouveau") == "oui" and (first is None or now - first > NEW_FOR):
            put("Nouveau", n, "non")
            res.flags += 1
        if n not in seen_rows and c(row, "En ligne") != "?" and (last is None or now - last > ONLINE_FOR):
            put("En ligne", n, "?")
            res.flags += 1
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    # ---- Insert new offers as a block at row 2, newest first ----
    if fresh:
        if prepare:  # after the cell updates: their row numbers are not held across a slow fetch
            prepare(fresh)
        for o in fresh:
            o.update(annotate(o))
        fresh.sort(key=lambda o: str(o.get("date_posted") or ""), reverse=True)  # undated last
        count = len(fresh)
        sh.batch_update({"requests": [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 1, "endIndex": 1 + count},
            "inheritFromBefore": False}}]})  # inherit data-row formats/dropdown, not the header style
        new_rows = [_new_row(o, r, detected, mark_new, layout, ville_for(o.get("location")))
                    for r, o in enumerate(fresh, start=2)]
        ws.batch_update(block_writes(new_rows, 2, layout), value_input_option="USER_ENTERED")
        res.new = [{k: v for k, v in o.items() if not k.startswith("_")} for o in fresh]

    log.info("sync: %d new, %d seen again, %d merged as cross-site duplicates, %d flags reset, backup %s",
             len(res.new), res.seen, res.merged, res.flags, res.backup)
    if UNMAPPED:
        log.warning("Région = '?' for unmapped locations (add them to regions.py): %s", sorted(UNMAPPED))
    return res


def saved_companies() -> dict[str, str]:
    """Offer link -> company, from the data saved locally (scans, the queue, sheet backups),
    oldest first so the most recent good value wins. Suspect values are skipped."""
    found: dict[str, str] = {}
    for path in sorted(ROOT.glob("internships_*.csv")) + [ROOT / "offers_with_descriptions.csv",
                                                          ROOT / "logs" / "last_scan.csv"]:
        if path.exists():
            for r in pd.read_csv(path, encoding="utf-8-sig").to_dict("records"):
                if not company_suspect(r.get("company"), r.get("title")):
                    found[str(r.get("job_url")).strip()] = str(r["company"]).strip()
    from pending_sync import load as load_queue
    for e in load_queue():
        if not company_suspect(e.get("company"), e.get("title")):
            found[str(e.get("job_url")).strip()] = str(e["company"]).strip()
    for path in sorted(BACKUP_DIR.glob("sheet_*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            table = list(csv.reader(f))
        if not table or "Lien" not in table[0] or "Entreprise" not in table[0]:
            continue
        h = {name: i for i, name in enumerate(table[0])}
        for r in table[1:]:
            cell = lambda name: r[h[name]] if h.get(name) is not None and h[name] < len(r) else ""  # noqa: E731
            if not company_suspect(cell("Entreprise"), cell("Titre")):
                found[cell("Lien").strip()] = cell("Entreprise").strip()
    return found


def backfill(apply: bool = False, sheet_id: str | None = None) -> None:
    """Fill empty Ville / Région cells, put back Lien URLs replaced by their page title, and repair
    suspect companies (empty, or the title) from
    saved data; when a company can't be recovered, Remarque gets "Entreprise à vérifier".
    Robot columns only, cell by cell; dry run unless apply."""
    sh = open_spreadsheet(sheet_id)
    ws = sh.worksheet(TAB)
    layout = read_layout(ws)
    c = layout.cell
    rows = ws.get(f"A2:{layout.last}", value_render_option=ValueRenderOption.unformatted, pad_values=True)
    villes = canonical_villes([c(r, "Ville") for r in rows] + [ville_of(c(r, "Lieu")) for r in rows])
    companies = saved_companies()
    # Lien cells whose text was replaced by the page title (Sheets' link preview "replace URL with
    # its title"): the URL survives only as link metadata, which plain reads don't return.
    lien = layout.letter("Lien")
    meta = sh.fetch_sheet_metadata({"ranges": [f"'{TAB}'!{lien}2:{lien}{len(rows) + 1}"],
                                    "fields": "sheets.data.rowData.values.hyperlink"})
    linked = [(rd.get("values") or [{}])[0].get("hyperlink")
              for rd in meta["sheets"][0]["data"][0].get("rowData", [])]
    plan: list[tuple[int, str, str]] = []   # (row, column, value)
    for n, row in enumerate(rows, start=2):
        if is_blank(row):
            continue
        text, url = str(c(row, "Lien")).strip(), linked[n - 2] if n - 2 < len(linked) else None
        if text and not text.startswith("http") and url and url.startswith("http"):
            plan.append((n, "Lien", url))
            print(f"  row {n}: Lien shows {text[:50]!r} -> its URL {url}")
        if not str(c(row, "Ville")).strip() and (v := ville_of(c(row, "Lieu"))):
            plan.append((n, "Ville", villes.get(ville_key(v), v)))
        region = str(c(row, "Région")).strip()
        if (not region or region == UNKNOWN) and (found := region_of(c(row, "Lieu"))) != region:
            plan.append((n, "Région", found))
        if company_suspect(c(row, "Entreprise"), c(row, "Titre")):
            remark = str(c(row, "Remarque")).strip()
            links = split_links(c(row, "Lien")) + split_links(c(row, "Autres liens"))
            real = next((companies[u] for u in links if u in companies), None)
            if real:
                plan.append((n, "Entreprise", as_text(real)))
                cleaned = " · ".join(p for p in remark.split(" · ") if p and p != COMPANY_CHECK)
                if cleaned != remark:
                    plan.append((n, "Remarque", cleaned))
                print(f"  row {n}: Entreprise {c(row, 'Entreprise')!r} -> {real!r} (recovered from saved data)")
            elif COMPANY_CHECK not in remark:
                plan.append((n, "Remarque", f"{remark} · {COMPANY_CHECK}" if remark else COMPANY_CHECK))
                print(f"  row {n}: company not recoverable -> Remarque '{COMPANY_CHECK}'")
    by_col = pd.Series([col for _, col, _ in plan]).value_counts().to_dict() if plan else {}
    print(f"{len(plan)} cells to write: {by_col}")
    if not apply:
        print("Dry run: nothing written. Re-run with --apply to write.")
        return
    print(f"Backup: {backup(ws, now_paris(), rehearsal=sheet_id is not None)}")
    updates = []
    for n, col, value in plan:
        a1 = f"{layout.letter(col)}{n}"
        layout.check_robot_range(a1)
        updates.append({"range": a1, "values": [[value]]})
    lien = layout.letter("Lien")  # rows are matched again right before writing
    fresh = ws.get(f"{lien}2:{lien}", value_render_option=ValueRenderOption.unformatted)
    for n, _, _ in plan:
        if str((fresh[n - 2] or [""])[0] if n - 2 < len(fresh) else "").strip() != str(c(rows[n - 2], "Lien")).strip():
            raise SystemExit(f"row {n} changed since the read (rows inserted or sorted): nothing written")
    for i in range(0, len(updates), 500):
        ws.batch_update(updates[i:i + 500], value_input_option="USER_ENTERED")
    print(f"Written: {len(updates)} cells.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--import", dest="csv", help="internships_YYYY-MM-DD.csv to import")
    group.add_argument("--backfill", action="store_true",
                       help="fill empty Ville / Région, repair suspect companies from saved data (dry run)")
    parser.add_argument("--apply", action="store_true", help="with --backfill: back up, then write")
    parser.add_argument("--sheet-id", help="another spreadsheet (rehearsal) instead of SHEET_ID")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.backfill:
        backfill(apply=args.apply, sheet_id=args.sheet_id)
        return

    m = re.search(r"(\d{4}-\d{2}-\d{2})", args.csv)
    detected = datetime.fromisoformat(m.group(1)) if m else now_paris()
    df = pd.read_csv(args.csv, encoding="utf-8-sig")
    res = sync(df, detected=detected, mark_new=False)
    print(f"Imported {len(res.new)} new offers ({res.seen} already in the sheet). Backup: {res.backup}")


if __name__ == "__main__":
    main()
