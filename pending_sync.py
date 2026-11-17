"""Local queue of scraped offers whose sheet sync failed (Sheets unreachable, e.g. VPN on).

pending_sync.jsonl (git-ignored), one offer per line: the clean() fields plus
  detected  ISO time of the run that scraped it (becomes Première détection on flush)
  alerted   true once a Telegram digest listed it, so it is never sent twice

run_hunt queues every offer of a run whose sync failed, then flushes the queue on the
next run that reaches the sheet: one sync_sheet.sync() per detection time, oldest first,
so the normal dedup rules, backups and column ownership apply.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

import pandas as pd

from find_internships import dedup_key
from sheet_lib import ROOT

QUEUE = ROOT / "pending_sync.jsonl"
BACKUP_DIR = ROOT / "backups"


def _plain(value):
    """JSON-safe scalar: NaN/NaT -> None, numpy numbers -> int/float."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return value.item() if hasattr(value, "item") else value


def load(path: Path | None = None) -> list[dict]:
    path = path or QUEUE
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def save(entries: list[dict], path: Path | None = None) -> None:
    """Atomic rewrite (temp file + rename): a crash never leaves a half-written queue."""
    path = path or QUEUE
    if not entries:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def add(offers: pd.DataFrame, detected: datetime, alerted: bool = False, path: Path | None = None) -> list[dict]:
    """Append a run's offers; returns the whole queue. alerted=True for a --no-alert run, so
    its offers aren't sent later either."""
    entries = load(path)
    for rec in offers.to_dict("records"):
        entries.append({**{k: _plain(v) for k, v in rec.items()},
                        "detected": detected.isoformat(), "alerted": alerted})
    save(entries, path)
    return entries


def batches(entries: list[dict]) -> list[tuple[datetime, pd.DataFrame]]:
    """Queue grouped by detection time, oldest first, as sync()-ready DataFrames with a
    fresh age_days (the digest shows the age on the day it is sent)."""
    out = []
    ordered = sorted(entries, key=lambda e: e["detected"])
    for stamp, group in groupby(ordered, key=lambda e: e["detected"]):
        df = pd.DataFrame(list(group))
        posted = pd.to_datetime(df.get("date_posted"), errors="coerce")
        df["age_days"] = (pd.Timestamp(date.today()) - posted).dt.days
        out.append((datetime.fromisoformat(stamp), df))
    return out


def sheet_index(backup_dir: Path | None = None) -> tuple[set[str], set[str]]:
    """Links and title|company|city keys of the latest sheet backup: what the sheet held at the
    last successful sync. Empty when there is no backup."""
    files = sorted((backup_dir or BACKUP_DIR).glob("sheet_*.csv"))
    if not files:
        return set(), set()
    with open(files[-1], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return set(), set()
    col = {name: i for i, name in enumerate(rows[0])}

    def cell(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    urls, keys = set(), set()
    for row in rows[1:]:
        for name in ("Lien", "Autres liens"):
            urls.update(u.strip() for u in cell(row, name).splitlines() if u.strip())
        if any(row):
            keys.add(dedup_key(cell(row, "Titre"), cell(row, "Entreprise"), cell(row, "Lieu")))
    return urls, keys


def offer_ids(offer: dict) -> set[str]:
    """Link and title|company|city key: either one identifies an offer across runs."""
    ids = {str(offer.get("job_url") or "").strip(),
           dedup_key(offer.get("title"), offer.get("company"), offer.get("location"))}
    return ids - {""}


def alerted_ids(entries: list[dict]) -> set[str]:
    """Ids of every queued offer already sent in a digest (or queued by a --no-alert run)."""
    return set().union(*(offer_ids(e) for e in entries if e.get("alerted")))


def probably_new(entries: list[dict], index: tuple[set[str], set[str]]) -> list[dict]:
    """Queued offers to alert while the sheet is unreachable: not alerted yet, not in the last
    backup (exact link or key match only: the fuzzy merge rules need the live sheet)."""
    urls, keys = index
    seen = set(urls) | set(keys)
    out = []
    for e in entries:  # file order = oldest first, so an alerted earlier copy hides later ones
        ids = offer_ids(e)
        if ids & seen:
            continue
        seen |= ids
        if not e.get("alerted"):
            out.append(e)
    return out


def mark_alerted(urls: set[str], path: Path | None = None) -> None:
    entries = load(path)
    for e in entries:
        if str(e.get("job_url") or "").strip() in urls:
            e["alerted"] = True
    save(entries, path)
