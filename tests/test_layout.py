"""Offline tests: columns are located by header name, never by position (no network).

    .venv/Scripts/python tests/test_layout.py

An in-memory sheet applies the sync's writes and row inserts for real, so the checks read the
result back by header: values must land under the right header wherever the columns are, the
user's columns and the user's own extra columns must stay byte-identical, and a missing or
duplicated header must abort before any write, with the offers queued and a digest warning.
"""

from __future__ import annotations

import argparse
import copy
import logging
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import sync_sheet  # noqa: E402
from sheet_lib import COLUMNS, USER_COLUMNS, LayoutError, col_index, layout_from_header  # noqa: E402

EXTRA = "Mon salaire négocié"   # a column the user added: the robot must never write it


def serial(dt: datetime) -> float:
    return (dt - datetime(1899, 12, 30)).total_seconds() / 86400


class FakeSheet:
    """Worksheet + spreadsheet in one: row 1 = header; writes and row inserts are applied."""
    id = 1

    def __init__(self, header: list[str], rows: list[dict]):
        self.grid = [list(header)] + [[r.get(h, "") for h in header] for r in rows]
        self.writes: list[str] = []

    # worksheet API used by sync()
    def worksheet(self, name):
        return self

    def row_values(self, n):
        return list(self.grid[n - 1])

    def get(self, rng, **kw):
        return [list(r) for r in self.grid[1:]]

    def get_all_values(self):
        return [[str(c) for c in r] for r in self.grid]

    def batch_update(self, data, **kw):
        if isinstance(data, dict):  # spreadsheet.batch_update: row inserts
            for req in data["requests"]:
                ins = req["insertDimension"]["range"]
                for _ in range(ins["endIndex"] - ins["startIndex"]):
                    self.grid.insert(ins["startIndex"], [""] * len(self.grid[0]))
                self.writes.append("insertDimension")
            return
        for d in data:
            self.writes.append(d["range"])
            m = re.fullmatch(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", d["range"])
            c0, r0 = col_index(m.group(1)), int(m.group(2)) - 1
            for i, values in enumerate(d["values"]):
                for j, v in enumerate(values):
                    self.grid[r0 + i][c0 + j] = v

    def by_header(self):
        header = self.grid[0]
        return [dict(zip(header, r)) for r in self.grid[1:]]


def existing_rows() -> list[dict]:
    first = serial(datetime(2026, 11, 16, 10))
    base = {"Date publication": serial(datetime(2026, 11, 16)), "Première détection": first,
            "Dernière détection": first, "En ligne": "oui", "Nouveau": "non"}
    return [
        {**base, "Titre": "STAGE - Business Data Analyst H/F", "Entreprise": "AXA Partners",
         "Lieu": "Malakoff, Île-de-France", "Lien": "u-axa", "Statut": "Postulé", "Notes": "my note",
         "Date candidature": serial(datetime(2026, 11, 10)), EXTRA: "1400"},
        {**base, "Titre": "Final year internship - Data Scientist & AI Consultant", "Entreprise": "Sia",
         "Lieu": "Paris, France", "Lien": "u-sia", "Statut": "À trier"},
        {**base, "Titre": "STAGE - Assistant Supply Analyst (H/F)", "Entreprise": "STAGE - Assistant Supply Analyst (H/F)",
         "Lieu": "Villeneuve d'Ascq, Hauts-de-France, France", "Lien": "u-pasted", "Statut": "À trier",
         "Ville": "Villeneuve-d'Ascq"},   # the title pasted over the company by hand
    ]


OFFERS = pd.DataFrame([
    dict(title="STAGE - Business Data Analyst H/F", company="AXA / France", location="Malakoff, A8, France",
         date_posted="2026-11-17", job_url="u-axa-indeed", site="indeed", age_days=0),
    dict(title="Stage - Data Analyste H/F", company="Caisse d'Epargne", location="Paris, A8",
         date_posted="2026-11-17", job_url="u-ce-indeed", site="indeed", age_days=0),
    dict(title="Stage Statisticien", company="Insee", location="Lille, Hauts-de-France, France",
         date_posted="2026-11-17", job_url="u-insee", site="linkedin", age_days=0),
    dict(title="Stage Data Analyst", company="Decathlon", location="VILLENEUVE D'ASCQ, 59",
         date_posted="2026-11-17", job_url="u-decat", site="indeed", age_days=0),
])


def run_sync(header: list[str]) -> FakeSheet:
    sheet = FakeSheet(header, existing_rows())
    sync_sheet.open_spreadsheet = lambda sheet_id=None: sheet
    sync_sheet.backup = lambda ws, now, **kw: "skip"
    sync_sheet.sync(OFFERS)
    return sheet


def moved_header() -> list[str]:
    h = list(COLUMNS)
    h.insert(h.index("Contact"), h.pop(h.index("Titre")))         # a robot column among the user's
    h.insert(h.index("Site"), h.pop(h.index("Notes")))            # a user column among the robot's
    h.insert(h.index("Région"), h.pop(h.index("Score")))          # robot columns reordered
    h.insert(5, EXTRA)                                            # the user's own column, mid-sheet
    return h


def check(sheet: FakeSheet, label: str) -> None:
    rows = {r["Lien"]: r for r in sheet.by_header()}
    print(f"{label}: writes {sheet.writes}")
    new = rows["u-insee"]
    assert new["Titre"] == "Stage Statisticien" and new["Entreprise"] == "Insee", new
    assert new["Statut"] == "À trier" and new["Région"] == "Hauts-de-France" and new["Nouveau"] == "oui"
    assert new["Ville"] == "Lille", new["Ville"]
    assert rows["u-sia"]["Ville"] == "Paris"                                   # backfilled on an existing row
    assert "Entreprise à vérifier" in rows["u-pasted"]["Remarque"]              # suspect company flagged
    assert rows["u-pasted"]["Entreprise"] == existing_rows()[2]["Entreprise"]  # never "fixed" by the sync
    age, date_col = new["Âge (jours)"], layout_from_header(sheet.grid[0]).letter("Date publication")
    assert date_col in age, f"Âge must reference Date publication ({date_col}): {age}"
    assert str(new["Score"]).startswith("=MAX(0;MIN(100;"), new["Score"]
    # Everything the user owns is untouched: L..R and the extra column, on every pre-existing row.
    before = {r["Lien"]: r for r in existing_rows()}
    for url, old in before.items():
        for col in USER_COLUMNS + [EXTRA]:
            assert rows[url].get(col, "") == old.get(col, ""), (label, url, col, rows[url].get(col), old.get(col))
    # New rows: user columns empty except Statut, the extra column empty.
    new_rows = [r for url, r in rows.items() if url not in before]
    assert {r["Lien"] for r in new_rows} == {"u-ce-indeed", "u-insee", "u-decat"}   # AXA merges into its row
    assert rows["u-decat"]["Ville"] == "Villeneuve-d'Ascq", rows["u-decat"]["Ville"]  # one spelling per city
    for r in new_rows:
        assert all(not r.get(c) for c in USER_COLUMNS if c != "Statut") and not r.get(EXTRA, "")
    # The formula column is never written (its header formula fills it).
    nxt = layout_from_header(sheet.grid[0]).idx["➜ Prochaine étape"]
    for w in sheet.writes:
        m = re.fullmatch(r"([A-Z]+)\d+(?::([A-Z]+)\d+)?", w)
        if m:
            lo, hi = col_index(m.group(1)), col_index(m.group(2) or m.group(1))
            assert not lo <= nxt <= hi, f"{label}: {w} covers the formula column"
    # Existing row seen again: Dernière détection updated in its own column.
    assert rows["u-axa"]["Dernière détection"] != existing_rows()[0]["Dernière détection"]


def test_sync_by_header() -> None:
    check(run_sync(list(COLUMNS) + [EXTRA]), "canonical order")
    check(run_sync(moved_header()), "moved columns")


def test_abort() -> None:
    for label, header in (("missing Statut", [h for h in COLUMNS if h != "Statut"]),
                          ("duplicated Notes", list(COLUMNS) + ["Notes"]),
                          ("renamed header", [("Status" if h == "Statut" else h) for h in COLUMNS])):
        sheet = FakeSheet(header, existing_rows())
        before = copy.deepcopy(sheet.grid)
        sync_sheet.open_spreadsheet = lambda sheet_id=None: sheet
        try:
            sync_sheet.sync(OFFERS)
            raise AssertionError(f"{label}: sync should have refused")
        except LayoutError as exc:
            print(f"{label}: LayoutError: {exc} | writes: {sheet.writes or 'none'}")
        assert sheet.writes == [] and sheet.grid == before


def test_run_hunt_queues() -> None:
    """A layout problem: offers queued, error logged, digest warning, nothing written."""
    import linkedin_access as la
    import pending_sync as ps
    import run_hunt as rh
    tmp = Path(tempfile.mkdtemp())
    rh.LAST_SCRAPE, rh.LAST_FULL_SCAN, rh.LAST_SCAN_CSV = tmp / "a", tmp / "b", tmp / "c.csv"
    ps.QUEUE, ps.BACKUP_DIR, la.SKIP_FILE = tmp / "q.jsonl", tmp / "backups", tmp / "skip"
    logs, sent = [], []

    class H(logging.Handler):
        def emit(self, r):
            logs.append(f"{r.levelname} {r.getMessage()}")
    logging.getLogger("hunt").addHandler(H())
    logging.getLogger("hunt").setLevel(logging.INFO)
    rh.scrape = lambda *a: (OFFERS.copy(), [], [{"site": "indeed", "location": "France", "got": 3, "wanted": 100,
                                                 "skipped": False}])
    rh.sync = lambda df, **kw: (_ for _ in ()).throw(LayoutError("row 1 of 'Offres' — missing: Statut"))
    rh.send_telegram = lambda msgs: sent.append(msgs)
    rc = rh.hunt(argparse.Namespace(force=True, full=False, no_alert=False))
    queued = ps.load()
    print(f"run_hunt: rc {rc}, {len(queued)} queued, log {[l for l in logs if 'layout' in l]}")
    print("digest header:", sent[0][0].split("\n")[1] if sent else None)
    assert rc == 1 and len(queued) == len(OFFERS)
    assert sent and "Colonnes du Sheet introuvables ou en double" in sent[0][0]
    assert not any("VPN" in l for l in logs)


if __name__ == "__main__":
    test_sync_by_header()
    test_abort()
    test_run_hunt_queues()
    print("ALL OK")
