"""Shared layout and helpers for the "Offres" Google Sheet.

Column ownership: A..K and S..Z are written by the robot, L..R belong to the user and
are never written, except Statut = "À trier" on rows the robot inserts. S..Z were added
after the user columns (not next to K) so existing user data never had to shift.
Code finds columns by position: columns can be hidden in the sheet, never moved.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import requests
from dotenv import load_dotenv

from config import RUNS, TIMEZONE
from google.auth.exceptions import TransportError

ROOT = Path(__file__).resolve().parent
CREDENTIALS = ROOT / "credentials" / "service_account.json"
TAB = "Offres"
TZ = ZoneInfo(TIMEZONE)   # config.yaml timezone (the sheet is set to the same one)
# (connect, read) seconds for every Sheets call, token refresh included. A VPN that blocks
# sheets.googleapis.com makes the connect hang: fail fast instead.
SHEETS_TIMEOUT = (5, 60)

ROBOT_COLUMNS = ["Titre", "Entreprise", "Lieu", "Site", "Date publication", "Âge (jours)", "Lien",
                 "Première détection", "Dernière détection", "En ligne", "Nouveau"]
USER_COLUMNS = ["Statut", "Date candidature", "CV utilisé", "Contact", "Prochaine action",
                "Date prochaine action", "Notes"]
NEXT_STEP = "➜ Prochaine étape"
EXTRA_ROBOT_COLUMNS = ["Autres liens", "Doublon ?", "Région", "Score", "Score fiable", "Gratification",
                       "Durée", "Remarque", "Ville", NEXT_STEP]
# Robot columns computed by one formula in their header cell: never written by the sync/backfill
# (writing a cell under it would break the formula's output).
FORMULA_COLUMNS = [NEXT_STEP]
# Missing robot columns are appended at the end, except these, inserted right after their anchor.
PLACE_AFTER = {NEXT_STEP: "Statut"}
FOLLOW_UP_DAYS = RUNS["follow_up_days"]   # Postulé for this many days or more: follow-up due (everywhere)
# "➜ Prochaine étape" by Statut (the user's wording). Postulé depends on Date candidature:
# see setup_sheet.next_step_formula.
NEXT_STEPS = {
    "À trier": "Lire l'offre (2 min) → À postuler ou Pas intéressé",
    "À postuler": "CV ciblé + candidature + message à un ancien",
    "Réponse RH": "Répondre sous 24 h, proposer des créneaux",
    "Entretien RH": "Préparer pitch 90 s + 3 questions",
    "Entretien manager": "Réviser les 4 projets + questions techniques",
    "Test/case": "Bloquer 2 h, relire l'offre, rendre un travail propre",
    "Offre": "Vérifier dates, gratification, convention de stage",
    "Refus": "Remercier + demander un retour",
    "Ghosté": "Rien à faire. Suivante !",
    "Pas intéressé": "—",
    "Offre fermée": "—",
}
POSTULE_NO_DATE = "Ajouter la Date candidature"
POSTULE_WAIT = "Attendre · relance le "          # + Date candidature + 7 days, dd/mm
POSTULE_DUE = "🔴 Relancer aujourd'hui"
# Technical robot columns, kept together in one collapsible column group (collapsed by default).
FOCUS_GROUP = ["Lieu", "Site", "Première détection", "Dernière détection", "En ligne", "Nouveau",
               "Autres liens", "Doublon ?", "Score fiable", "Remarque"]
# Canonical order ("focus mode"): what you read first, your columns, then the technical group.
# Only new sheets and `setup_sheet.py --arrange` use it: every script locates columns by header.
COLUMNS = (["Titre", "Entreprise", "Ville", "Région", "Date publication", "Âge (jours)", "Lien", "Score"]
           + USER_COLUMNS[:1] + [NEXT_STEP] + USER_COLUMNS[1:] + ["Gratification", "Durée"] + FOCUS_GROUP)
assert sorted(COLUMNS) == sorted(ROBOT_COLUMNS + USER_COLUMNS + EXTRA_ROBOT_COLUMNS)
IDX = {name: i for i, name in enumerate(COLUMNS)}  # 0-based, canonical order

STATUSES = ["À trier", "À postuler", "Postulé", "Réponse RH", "Entretien RH", "Entretien manager",
            "Test/case", "Offre", "Refus", "Ghosté", "Pas intéressé", "Offre fermée"]
DEFAULT_STATUS = STATUSES[0]
# Status groups used by the dashboard (the user's definitions). Ghosté = applied, no reply;
# Refus = a reply; Offre fermée = not counted as applied (it may close before any application).
APPLIED = ["Postulé", "Réponse RH", "Entretien RH", "Entretien manager", "Test/case", "Offre", "Refus", "Ghosté"]
REPLIED = ["Réponse RH", "Entretien RH", "Entretien manager", "Test/case", "Offre", "Refus"]
REACHED_INTERVIEW = ["Entretien RH", "Entretien manager", "Test/case", "Offre"]
IN_INTERVIEW = ["Entretien RH", "Entretien manager", "Test/case"]
# Dropdowns in the user's columns (non-strict: other values stay allowed).
CV_OPTIONS = ["fr_master", "en_master", "ciblé", "autre"]
NEXT_ACTIONS = ["Postuler", "Relancer", "Préparer entretien", "Envoyer remerciement", "Contacter un ancien",
                "Attendre"]
DASHBOARD_TAB = "🚀 Tableau de bord"
APPLICATIONS_TAB = "Mes candidatures"

# Cyberpunk "cockpit" theme (all tabs). Every text/background pair the setup uses is checked
# for WCAG AA (>= 4.5:1) by tests/test_theme.py. No green anywhere.
COLORS = {"bg": "#0D0221", "panel": "#1A0B2E", "text": "#F0E6FF", "muted": "#8B93A1", "dark": "#0D0221",
          "pink": "#FF2A6D", "cyan": "#05D9E8", "yellow": "#FCEE0A", "violet": "#B967FF", "blue": "#3D7BFF",
          "red": "#FF3B3B", "grey": "#C9C3D6", "dim": "#1E1A26", "white": "#FFFFFF"}
FONT_DISPLAY, FONT_TITLE, FONT_MONO, FONT_BODY = "Audiowide", "Orbitron", "Share Tech Mono", "Roboto"


# Offres status colours, readable from far away: with "row", the whole row (A..Z) is a solid block
# of the colour with dark text; otherwise only the Statut cell is ("À trier" keeps the dark row).
# "dim" = finished offers: dark grey Statut cell, muted text on the row.
STATUS_STYLE = [
    {"statuses": ["À trier"], "block": "grey", "row": False},
    {"statuses": ["À postuler"], "block": "yellow", "row": True},
    {"statuses": ["Postulé"], "block": "cyan", "row": True},
    {"statuses": ["Réponse RH"], "block": "blue", "row": True},
    {"statuses": ["Entretien RH", "Entretien manager", "Test/case"], "block": "pink", "row": True},
    {"statuses": ["Offre"], "block": "violet", "row": True, "bold": True},
    {"statuses": ["Refus"], "block": "red", "row": True},
    {"statuses": ["Ghosté", "Pas intéressé", "Offre fermée"], "block": "dim", "row": False, "dim": True},
]

_EPOCH = datetime(1899, 12, 30)  # Sheets serial-number epoch


def col_letter(i: int) -> str:
    """0-based column index -> A, B, ..., Z, AA, AB..."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    """A -> 0, Z -> 25, AA -> 26."""
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class LayoutError(RuntimeError):
    """Row 1 of Offres doesn't hold every expected header exactly once: nothing may be written."""


@dataclass(frozen=True)
class Layout:
    """Where each column actually is, read from row 1 at every run (columns may have been moved).
    Columns are always located by header name, never by position. Headers that aren't in
    COLUMNS are the user's own extra columns: read-only for the robot, like L..R."""
    header: tuple[str, ...]

    @cached_property
    def idx(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(self.header) if name in IDX}

    def letter(self, name: str) -> str:
        return col_letter(self.idx[name])

    @property
    def last(self) -> str:
        return col_letter(len(self.header) - 1)

    def cell(self, row: list, name: str):
        i = self.idx[name]
        return row[i] if i < len(row) else ""

    @cached_property
    def robot(self) -> frozenset[int]:
        """Every robot column, formula columns included (their content is never the user's)."""
        return frozenset(self.idx[c] for c in ROBOT_COLUMNS + EXTRA_ROBOT_COLUMNS)

    @cached_property
    def writable(self) -> frozenset[int]:
        """Robot columns the scripts may write cell by cell: not the formula columns, whose single
        header formula fills the cells below (a written cell would break it)."""
        return frozenset(self.idx[c] for c in ROBOT_COLUMNS + EXTRA_ROBOT_COLUMNS if c not in FORMULA_COLUMNS)

    def check_robot_range(self, a1: str, allow_statut: bool = False) -> None:
        """Refuse any write that would touch a user column or an unknown one (the core safety
        rule). Every column covered by the range is checked, not just its two ends."""
        cols = re.findall(r"([A-Z]+)\d*", a1.split("!")[-1])
        if not cols or len(cols) > 2:
            raise RuntimeError(f"refusing to write {a1}: unexpected range")
        found = sorted(col_index(c) for c in cols)
        lo, hi = found[0], found[-1]
        allowed = self.writable | ({self.idx["Statut"]} if allow_statut else frozenset())
        if any(i not in allowed for i in range(lo, hi + 1)):
            raise RuntimeError(f"refusing to write {a1}: user columns are read-only")

    def runs(self, names: list[str]) -> list[list[str]]:
        """The given columns grouped into runs of adjacent columns, in sheet order, so a row can be
        written range by range without covering any other column."""
        out: list[list[str]] = []
        for name in sorted(names, key=lambda n: self.idx[n]):
            if out and self.idx[name] == self.idx[out[-1][-1]] + 1:
                out[-1].append(name)
            else:
                out.append([name])
        return out


def layout_from_header(header: list[str]) -> Layout:
    """Validate row 1: every expected header present exactly once, else LayoutError."""
    cells = [str(h).strip() for h in header]
    while cells and not cells[-1]:
        cells.pop()
    missing = [c for c in COLUMNS if c not in cells]
    doubled = sorted({c for c in cells if c in IDX and cells.count(c) > 1})
    if missing or doubled:
        parts = ([f"missing: {', '.join(missing)}"] if missing else []) + \
                ([f"duplicated: {', '.join(doubled)}"] if doubled else [])
        raise LayoutError(f"row 1 of '{TAB}' — " + "; ".join(parts))
    return Layout(tuple(cells))


def read_layout(ws: gspread.Worksheet) -> Layout:
    return layout_from_header(ws.row_values(1))


DEFAULT_LAYOUT = Layout(tuple(COLUMNS))   # the canonical order, for a new sheet and offline tests


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def open_spreadsheet(sheet_id: str | None = None) -> gspread.Spreadsheet:
    """The tracker (SHEET_ID in .env), or another spreadsheet by id (setup rehearsals)."""
    load_env()
    sheet_id = sheet_id or os.environ.get("SHEET_ID")
    if not sheet_id:
        raise RuntimeError("SHEET_ID is empty in .env")
    client = gspread.service_account(filename=str(CREDENTIALS))
    client.set_timeout(SHEETS_TIMEOUT)
    return client.open_by_key(sheet_id)


def sheets_unreachable(exc: BaseException) -> bool:
    """True for network failures (connect/read timeout, DNS, refused, token refresh transport)
    rather than API errors: the usual cause is the VPN blocking googleapis.com."""
    while exc is not None:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout, TransportError)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def now_paris() -> datetime:
    """Naive local wall-clock time in the configured time zone, the same clock the sheet uses."""
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def from_serial(value) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _EPOCH + timedelta(days=value)
    return None


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def as_text(value) -> str:
    """Text for a USER_ENTERED write: a leading ' stops Sheets from reading it as a formula."""
    s = "" if value is None or value != value else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s
