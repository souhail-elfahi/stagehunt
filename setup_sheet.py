"""Idempotent setup of the tracker: cyberpunk "cockpit" theme, Offres, dashboard, "Mes candidatures".

Re-running it rebuilds only what it owns: its conditional-format rules, dropdowns, formats,
the dashboard and "Mes candidatures" tabs (formulas only), its slicers and its protection.
It never writes a value in the user's columns (L..R of Offres) nor in the dashboard's goal
cells once they hold a value. Formatting on Offres (colours, fonts) is the theme's.

    python setup_sheet.py                      # the tracker (SHEET_ID in .env)
    python setup_sheet.py --sheet-id <id>      # a rehearsal spreadsheet
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import gspread
import yaml

from config import GOALS as CFG_GOALS
from config import PRIORITY, TIMEZONE
from gspread.utils import ValueRenderOption

from sheet_lib import (APPLICATIONS_TAB, APPLIED, COLORS, COLUMNS, CV_OPTIONS, DASHBOARD_TAB, DEFAULT_LAYOUT,
                       FONT_BODY, FONT_DISPLAY, FONT_MONO, FONT_TITLE, IN_INTERVIEW, NEXT_ACTIONS, REACHED_INTERVIEW,
                       REPLIED, STATUS_STYLE, STATUSES, TAB, EXTRA_ROBOT_COLUMNS, FOCUS_GROUP, ROBOT_COLUMNS, Layout,
                       FOLLOW_UP_DAYS, FORMULA_COLUMNS, NEXT_STEP, NEXT_STEPS, PLACE_AFTER, POSTULE_DUE,
                       POSTULE_NO_DATE, POSTULE_WAIT, LayoutError, col_letter, layout_from_header,
                       open_spreadsheet)

C = COLORS
DATE_COLS = ["Date publication", "Date candidature", "Date prochaine action"]
DATETIME_COLS = ["Première détection", "Dernière détection"]
MONO_COLS = DATE_COLS + DATETIME_COLS + ["Âge (jours)", "Score", "Gratification"]
LINK_COLS = ["Lien", "Autres liens"]
WIDTHS = {"Titre": 380, "Ville": 150, NEXT_STEP: 330, "Entreprise": 160, "Lieu": 170, "Site": 70, "Date publication": 95,
          "Âge (jours)": 60, "Lien": 220, "Première détection": 120, "Dernière détection": 120,
          "En ligne": 60, "Nouveau": 65, "Statut": 130, "Date candidature": 100, "CV utilisé": 120,
          "Contact": 160, "Prochaine action": 180, "Date prochaine action": 100, "Notes": 300, "Autres liens": 220,
          "Doublon ?": 200, "Région": 170, "Score": 60, "Score fiable": 75, "Gratification": 100, "Durée": 90,
          "Remarque": 160}
HEADER_HEIGHT = 40
ARRAY_COL_SEP = "\\"        # fr_FR array literals: {"a"\1;"b"\2} (';' separates rows)


def rgb(hex_: str) -> dict:
    h = hex_.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in zip(("red", "green", "blue"), (0, 2, 4))}


def statut_is(*values: str, col: str = "L") -> str:
    tests = [f'${col}1="{v}"' for v in values]
    return "=" + tests[0] if len(tests) == 1 else f"=OR({';'.join(tests)})"


def fmt(bg: str | None = None, fg: str | None = None, bold: bool = False) -> dict:
    f: dict = {}
    if bg:
        f["backgroundColor"] = rgb(bg)
    text = {**({"foregroundColor": rgb(fg)} if fg else {}), **({"bold": True} if bold else {})}
    if text:
        f["textFormat"] = text
    return f


def follow_up(layout: Layout) -> str:
    """Postulé for FOLLOW_UP_DAYS days or more: follow-up due (same test as the column and the dashboard)."""
    s, d = layout.letter("Statut"), layout.letter("Date candidature")
    return f'=AND(${s}1="Postulé";ISNUMBER(${d}1);${d}1<=TODAY()-{FOLLOW_UP_DAYS})'


def block_format(style: dict) -> dict:
    """A solid neon block with dark text (dim group: dark grey, muted text)."""
    if style.get("dim"):
        return fmt(C["dim"], C["muted"])
    return fmt(C[style["block"]], C["dark"], style.get("bold", False))


def status_rules(layout: Layout | None = None, col: str | None = None) -> list[tuple[str, dict, str]]:
    """(formula, format, scope) in priority order: Sheets applies only the first matching rule
    to a cell. scope: "status" = the Statut column, "date" = Date candidature, "row" = every column.
    Formulas use the columns' actual letters (layout). With col, only the Statut blocks are
    returned, for that column ("Mes candidatures").

    Order: Statut blocks; follow-up date block (yellow); dimmed rows; solid-colour rows (the whole
    row, Score included, takes its status colour with dark text); then the Score scale at
    SCALE_INDEX (so only "À trier" rows show it); then bold for new offers."""
    layout = layout or DEFAULT_LAYOUT
    s = col or layout.letter("Statut")
    rules = [(statut_is(*st["statuses"], col=s), block_format(st), "status") for st in STATUS_STYLE]
    if col:
        return rules
    rules.append((follow_up(layout), fmt(C["yellow"], C["dark"], True), "date"))   # the row itself stays cyan
    dim = next(st for st in STATUS_STYLE if st.get("dim"))
    rules.append((statut_is(*dim["statuses"], col=s), fmt(fg=C["muted"]), "row"))
    for st in STATUS_STYLE:
        if st.get("row"):
            rules.append((statut_is(*st["statuses"], col=s), block_format(st), "row"))
    rules.append((f'=${layout.letter("Nouveau")}1="oui"', fmt(fg=C["white"], bold=True), "row"))  # new: bold
    return rules


RULES = status_rules()
SCALE_INDEX = len(RULES) - 1   # just before the bold rule: after every status colour


def shape(formula: str) -> str:
    """A rule's formula with its column letters blanked, so a rule is recognised as ours even
    after its columns were moved (Sheets rewrites the letters) or the layout changed."""
    return re.sub(r"\$[A-Z]{1,3}1\b|#REF!", "$?1", formula)   # #REF!: a deleted column's reference


# Rules written by earlier versions (formulas no longer produced), removed on re-run so they
# don't pile up. Matching is by shape, so recolouring or moving columns needs no entry.
LEGACY_FORMULAS = {statut_is("Pas intéressé", "Ghosté"),                    # V1 grey rule
                   statut_is("Pas intéressé", "Ghosté", "Offre fermée"),    # V2a/V2c order
                   '=AND($L1="Postulé";ISNUMBER($M1);$M1<TODAY()-7)'}       # follow-up "> 7 days" (before ≥)
OUR_SHAPES = {shape(f) for f, _, _ in RULES} | {shape(f) for f in LEGACY_FORMULAS}
SCALE = {"minpoint": {"type": "MIN", "color": rgb(C["red"])},
         "midpoint": {"type": "PERCENTILE", "value": "50", "color": rgb(C["yellow"])},
         "maxpoint": {"type": "MAX", "color": rgb(C["cyan"])}}
OLD_SLICERS = {"Statut", "Région", "Score"}   # titles of the slicers earlier versions added: removed
# Saved filter views (Données > Vues filtrées): (title, column, criteria, sort column, descending).
FILTER_VIEWS = [
    ("🔍 Triage", "Statut", {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "À trier"}]}},
     "Score", True),
    ("⚡ À postuler", "Statut", {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "À postuler"}]}},
     "Score", True),
    (f"🏠 {PRIORITY['region']}", "Région",
     {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": PRIORITY["region"]}]}}, "Score", True),
    ("📨 En cours", "Statut", {"hiddenValues": [st for st in STATUSES if st not in APPLIED] + [""]},
     "Date candidature", True),
]
ROBOT_PROTECTION = "Colonne robot : remplie par le script (modifier quand même ?)"


# ------------------------------------------------------------------ helpers ----
def grid(sid: int, r0: int, r1: int | None, c0: int, c1: int | None) -> dict:
    """0-based, end-exclusive grid range; None = to the end."""
    g = {"sheetId": sid, "startRowIndex": r0, "startColumnIndex": c0}
    if r1 is not None:
        g["endRowIndex"] = r1
    if c1 is not None:
        g["endColumnIndex"] = c1
    return g


def col_range(sid: int, name: str, layout: Layout, start_row: int = 1) -> dict:
    i = layout.idx[name]
    return grid(sid, start_row, None, i, i + 1)


def style(rng: dict, *, bg=None, fg=None, font=None, size=None, bold=None, italic=None, h=None, v=None, wrap=None,
          number=None) -> dict:
    """repeatCell request that sets only the given properties (the field mask lists exactly them)."""
    fmt, fields, text, tfields = {}, [], {}, []
    if bg:
        fmt["backgroundColor"] = rgb(bg); fields.append("backgroundColor")
    for key, val, name in ((fg, fg and rgb(fg), "foregroundColor"), (font, font, "fontFamily"),
                           (size, size, "fontSize"), (bold, bold, "bold"), (italic, italic, "italic")):
        if key is not None:
            text[name] = val; tfields.append(name)
    if text:
        fmt["textFormat"] = text; fields += [f"textFormat.{f}" for f in tfields]
    for key, name in ((h, "horizontalAlignment"), (v, "verticalAlignment"), (wrap, "wrapStrategy")):
        if key:
            fmt[name] = key; fields.append(name)
    if number:
        fmt["numberFormat"] = {"type": number[0], "pattern": number[1]}; fields.append("numberFormat")
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt},
                           "fields": ",".join(f"userEnteredFormat.{f}" for f in fields)}}


def widths(sid: int, px: dict[int, int]) -> list[dict]:
    return [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i,
                                                     "endIndex": i + 1},
                                           "properties": {"pixelSize": w}, "fields": "pixelSize"}}
            for i, w in px.items()]


def heights(sid: int, px: dict[int, int]) -> list[dict]:
    return [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": i,
                                                     "endIndex": i + 1},
                                           "properties": {"pixelSize": h}, "fields": "pixelSize"}}
            for i, h in px.items()]


def sheet_props(sid: int, **props) -> dict:
    """updateSheetProperties for gridProperties.* / tabColor / index."""
    properties, fields = {"sheetId": sid}, []
    for key, value in props.items():
        if key in ("hideGridlines", "frozenRowCount", "frozenColumnCount", "rowCount", "columnCount"):
            properties.setdefault("gridProperties", {})[key] = value; fields.append(f"gridProperties.{key}")
        elif key == "tabColor":
            properties["tabColorStyle"] = {"rgbColor": rgb(value)}; fields.append("tabColorStyle")
        else:
            properties[key] = value; fields.append(key)
    return {"updateSheetProperties": {"properties": properties, "fields": ",".join(fields)}}


def dropdown(rng: dict, values: list[str]) -> dict:
    return {"setDataValidation": {"range": rng, "rule": {
        "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in values]},
        "strict": False, "showCustomUi": True}}}


def opts(**pairs) -> str:
    """SPARKLINE options as a fr_FR array literal; values are formula fragments."""
    return "{" + ";".join(f'"{k}"{ARRAY_COL_SEP}{v}' for k, v in pairs.items()) + "}"


def q(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def rx(statuses: list[str]) -> str:
    return "^(" + "|".join(statuses) + ")$"


def get_or_create(sh: gspread.Spreadsheet, title: str, rows: int, cols: int) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title, rows=rows, cols=cols)


def get_or_create_offres(sh: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        pass
    sheets = sh.worksheets()
    if len(sheets) == 1 and not any(any(r) for r in sheets[0].get_all_values()):
        sheets[0].update_title(TAB)  # rename the empty default tab
        return sheets[0]
    return sh.add_worksheet(TAB, rows=1000, cols=len(COLUMNS))


def conditional_rules(rules: list[tuple[str, dict, str]], scopes: dict[str, dict]) -> list[dict]:
    """addConditionalFormatRule requests, in priority order (index 0 = first applied).
    Ranges start at row 1 so inserting rows at row 2 never shifts them."""
    return [{"addConditionalFormatRule": {"index": i, "rule": {
        "ranges": [scopes[scope]],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                        "format": cell_format}}}}
            for i, (formula, cell_format, scope) in enumerate(rules)]


def is_our_scale(rule: dict, score_col: int) -> bool:
    """Our Score colour scale: on the Score column, or anywhere with our shape (one column,
    MIN / PERCENTILE 50 / MAX). Sheets moves a rule's range with its column, so a scale can sit
    elsewhere after columns were moved or the layout was rewritten."""
    g = rule.get("gradientRule")
    if not g:
        return False
    ranges = rule.get("ranges", [])
    if any(r.get("startColumnIndex") == score_col for r in ranges):
        return True
    one_column = len(ranges) == 1 and ranges[0].get("endColumnIndex", 0) - ranges[0].get("startColumnIndex", 0) == 1
    points = [g.get(p, {}).get("type") for p in ("minpoint", "midpoint", "maxpoint")]
    return one_column and points == ["MIN", "PERCENTILE", "MAX"] and g["midpoint"].get("value") == "50"


# ------------------------------------------------------------------- Offres ----
def offres_requests(sid: int, this: dict, layout: Layout = DEFAULT_LAYOUT) -> list[dict]:
    requests: list[dict] = []
    n_cols = len(layout.header)
    score_col = layout.idx["Score"]
    for i, rule in reversed(list(enumerate(this.get("conditionalFormats", [])))):
        values = rule.get("booleanRule", {}).get("condition", {}).get("values", [])
        ours = values and shape(values[0].get("userEnteredValue", "")) in OUR_SHAPES
        ours = ours or is_our_scale(rule, score_col)
        if ours:
            requests.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})

    whole_rows = grid(sid, 0, None, 0, n_cols)
    scopes = {"row": whole_rows, "status": col_range(sid, "Statut", layout, 0),
              "date": col_range(sid, "Date candidature", layout, 0)}
    requests += conditional_rules(status_rules(layout), scopes)
    requests.append({"addConditionalFormatRule": {"index": SCALE_INDEX, "rule": {
        "ranges": [col_range(sid, "Score", layout)], "gradientRule": SCALE}}})

    requests += [dropdown(col_range(sid, "Statut", layout), STATUSES),
                 dropdown(col_range(sid, "CV utilisé", layout), CV_OPTIONS),
                 dropdown(col_range(sid, "Prochaine action", layout), NEXT_ACTIONS)]

    # Theme: formatting only (field masks list exactly what is set), never values.
    requests.append(style(grid(sid, 0, None, 0, None), bg=C["bg"], fg=C["text"], font=FONT_BODY))
    requests += [style(col_range(sid, c, layout), font=FONT_MONO) for c in MONO_COLS]
    requests += [style(col_range(sid, c, layout), fg=C["cyan"]) for c in LINK_COLS]
    requests.append(style(col_range(sid, "Score", layout), fg=C["bg"], bold=True, h="CENTER"))
    requests += [style(col_range(sid, c, layout), number=("DATE", "dd/mm/yyyy")) for c in DATE_COLS]
    requests += [style(col_range(sid, c, layout), number=("DATE_TIME", "dd/mm/yyyy hh:mm")) for c in DATETIME_COLS]
    requests += [style(col_range(sid, "Âge (jours)", layout), number=("NUMBER", "0")),
                 style(col_range(sid, "Score", layout), number=("NUMBER", "0")),
                 style(col_range(sid, "Gratification", layout), number=("CURRENCY", '#,##0 "€"'))]  # ',' = locale grouping
    requests.append(style(grid(sid, 0, 1, 0, n_cols), bg=C["panel"], fg=C["cyan"], font=FONT_TITLE, size=8, bold=True,
                          v="MIDDLE", wrap="WRAP"))
    requests.append(style(col_range(sid, "Titre", layout), wrap="CLIP"))
    requests.append(style(col_range(sid, NEXT_STEP, layout), wrap="CLIP"))
    # Header row frozen, and the Titre column too when it is the first one (only leading columns freeze).
    requests.append(sheet_props(sid, frozenRowCount=1, frozenColumnCount=1 if layout.idx["Titre"] == 0 else 0,
                                tabColor=C["cyan"]))
    requests += heights(sid, {0: HEADER_HEIGHT})
    requests += widths(sid, {layout.idx[name]: px for name, px in WIDTHS.items()})

    old_filter = this.get("basicFilter")
    if not old_filter:
        requests.append({"setBasicFilter": {"filter": {"range": whole_rows}}})
    elif old_filter["range"].get("endColumnIndex", n_cols) < n_cols:  # widen it, keeping its criteria and sort
        kept = {k: v for k, v in old_filter.items() if k in ("sortSpecs", "filterSpecs")}
        requests.append({"setBasicFilter": {"filter": {**kept, "range": whole_rows}}})
    return requests


def remove_slicers(sid: int, this: dict) -> list[dict]:
    """The slicers of earlier versions overlapped the header; the filter views replace them."""
    return [{"deleteEmbeddedObject": {"objectId": sl["slicerId"]}} for sl in this.get("slicers", [])
            if sl.get("spec", {}).get("title") in OLD_SLICERS]


def filter_view_requests(sid: int, this: dict, layout: Layout) -> list[dict]:
    """The saved filter views, rebuilt on each run (recognised by title)."""
    titles = {t for t, *_ in FILTER_VIEWS}
    requests = [{"deleteFilterView": {"filterId": fv["filterViewId"]}} for fv in this.get("filterViews", [])
                if fv.get("title") in titles]
    for title, column, criteria, sort_by, descending in FILTER_VIEWS:
        requests.append({"addFilterView": {"filter": {
            "title": title, "range": grid(sid, 0, None, 0, len(layout.header)),
            "filterSpecs": [{"columnIndex": layout.idx[column], "filterCriteria": criteria}],
            "sortSpecs": [{"dimensionIndex": layout.idx[sort_by],
                           "sortOrder": "DESCENDING" if descending else "ASCENDING"}]}}})
    return requests


def group_requests(sid: int, this: dict, layout: Layout) -> list[dict]:
    """One collapsible column group over the technical robot columns (FOCUS_GROUP), collapsed.
    Groups need adjacent columns: if they aren't (columns moved), no group and a warning."""
    requests = [{"deleteDimensionGroup": {"range": g["range"]}}
                for g in sorted(this.get("columnGroups", []), key=lambda g: -g.get("depth", 1))]
    idx = sorted(layout.idx[c] for c in FOCUS_GROUP)
    if idx != list(range(idx[0], idx[0] + len(idx))):
        print("  note: the technical columns aren't adjacent any more, so no column group "
              "(setup_sheet.py --arrange puts them back together)")
        return requests
    rng = {"sheetId": sid, "dimension": "COLUMNS", "startIndex": idx[0], "endIndex": idx[-1] + 1}
    return requests + [{"addDimensionGroup": {"range": rng}},
                       {"updateDimensionGroup": {"dimensionGroup": {"range": rng, "depth": 1, "collapsed": True},
                                                 "fields": "collapsed"}}]


def protection_requests(sid: int, this: dict, layout: Layout) -> list[dict]:
    """Warning-only protection on the robot columns: editing one by hand asks for a confirmation
    (it prevents accidental pastes; the script's writes are unaffected)."""
    requests = [{"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
                for p in this.get("protectedRanges", []) if p.get("description") == ROBOT_PROTECTION]
    for run in layout.runs(ROBOT_COLUMNS + EXTRA_ROBOT_COLUMNS):
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": grid(sid, 0, None, layout.idx[run[0]], layout.idx[run[-1]] + 1),
            "description": ROBOT_PROTECTION, "warningOnly": True}}})
    return requests


def arrange(sh: gspread.Spreadsheet, ws: gspread.Worksheet, layout: Layout) -> None:
    """One-time reorder into the canonical "focus mode" order (COLUMNS), the user's own extra
    columns kept just before the technical group. Moves columns (data, rules and formulas follow);
    never writes a value."""
    known = [c for c in COLUMNS if c not in FOCUS_GROUP]
    extras = [h for h in layout.header if h not in layout.idx]
    target = known + extras + FOCUS_GROUP
    current = list(layout.header)
    requests = []
    for i, name in enumerate(target):
        j = current.index(name)
        if j != i:  # move it left to position i (destination counted before removal)
            requests.append({"moveDimension": {"source": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                          "startIndex": j, "endIndex": j + 1},
                                               "destinationIndex": i}})
            current.insert(i, current.pop(j))
    if requests:
        sh.batch_update({"requests": requests})
    print(f"arrange: {len(requests)} column moves")


# ---------------------------------------------------------------- dashboard ----
O = f"'{TAB}'!"
DASH_ROWS, DASH_COLS = 22, 14             # A..N; content in B..M
GOALS = {"D3": CFG_GOALS["daily"], "J3": CFG_GOALS["weekly"]}   # user-editable cells, config.yaml defaults


def rng(col: str) -> str:
    """A whole column of Offres, e.g. 'Offres'!$L:$L. Never $L$2:$L: the sync inserts new rows at
    row 2, and Sheets would shift such a range down to row 3, leaving the new offers out of every
    count. The header cell never matches a count (it holds the column's name)."""
    return f"{O}${col}:${col}"


BAR_SEGMENTS = 10   # ▰▱ progress bars


def bar(value: str, total: str) -> str:
    """Formula fragment: a 10-segment ▰▱ bar for value / total (empty bar when total is 0)."""
    n = f"ROUND({BAR_SEGMENTS}*MIN(1;MAX(0;{value})/MAX(1;{total})))"
    return f'REPT("▰";{n})&REPT("▱";{BAR_SEGMENTS}-{n})'


def dashboard_cells(layout: Layout = DEFAULT_LAYOUT) -> dict[str, str]:
    """A1 -> value or formula (fr_FR: ';' separators, English function names). Columns come from
    the layout; Sheets then keeps these references right if columns are moved."""
    def col(name: str) -> str:
        return rng(layout.letter(name))
    L, M, H = col("Statut"), col("Date candidature"), col("Première détection")
    A, B, G = col("Titre"), col("Entreprise"), col("Lien")
    U, V = col("Région"), col("Score")

    def count_in(statuses):
        return f'=SUMPRODUCT(REGEXMATCH({L};{q(rx(statuses))})*1)'

    today = f"COUNTIF({M};TODAY())"
    week = f'COUNTIFS({M};">="&(TODAY()-WEEKDAY(TODAY();2)+1);{M};"<="&TODAY())'

    # Follow-ups due (oldest first), then the best "À postuler" by Score; at most 5 lines.
    # An empty part falls back to a blank 3-column row (CHOOSECOLS needs 3 columns), dropped below.
    empty = 'HSTACK("";"";"")'
    todo = (
        "=ARRAYFORMULA(LET("
        f"late;IFNA(SORT(FILTER(HSTACK(\"⏰ J+\"&(TODAY()-{M});HYPERLINK({G};\"Relancer · \"&{A}&\" — \"&{B});{M});"
        f"{L}=\"Postulé\";ISNUMBER({M});{M}<=TODAY()-{FOLLOW_UP_DAYS});3;TRUE);{empty});"
        f"best;IFNA(SORT(FILTER(HSTACK(IF({U}={q(PRIORITY['region'])};\"🏠 \";\"• \")&{V};HYPERLINK({G};{A}&\" — \"&{B});{V});"
        f"{L}=\"À postuler\");3;FALSE);{empty});"
        "all;VSTACK(CHOOSECOLS(late;1;2);CHOOSECOLS(best;1;2));"
        "kept;FILTER(all;INDEX(all;0;2)<>\"\");"
        "IFERROR(ARRAY_CONSTRAIN(kept;5;2);\"> aucune cible en attente\")))"
    )
    cells = {
        "B2": "🚀 COCKPIT · CANDIDATURES", "J2": '="SYS://ONLINE ▮ · "&TEXT(TODAY();"dddd dd/mm/yyyy")',
        "B3": "> MISSION DU JOUR", "E3": f"={bar(today, '$D$3')}", "G3": f'={today}&" / "&$D$3',
        "H3": "> MISSION SEMAINE", "K3": f"={bar(week, '$J$3')}", "M3": f'={week}&" / "&$J$3',
        # KPI cards: label / value / sub-line
        "B5": "À TRIER", "B6": f'=COUNTIF({L};"À trier")', "B7": f'="+"&COUNTIF({H};">="&TODAY())&" aujourd\'hui"',
        "D5": "À POSTULER", "D6": f'=COUNTIF({L};"À postuler")', "D7": "à envoyer",
        "F5": "POSTULÉES", "F6": count_in(APPLIED), "F7": f'={week}&" cette semaine"',
        "H5": "EN ENTRETIEN", "H6": count_in(IN_INTERVIEW), "H7": "RH · manager · test",
        "J5": "OFFRES", "J6": f'=COUNTIF({L};"Offre")', "J7": "🎯",
        "L5": "RELANCES", "L6": f'=COUNTIFS({L};"Postulé";{M};"<="&TODAY()-{FOLLOW_UP_DAYS})', "L7": f"postulé ≥ {FOLLOW_UP_DAYS} jours",
        "B9": "/// CIBLES PRIORITAIRES", "B10": todo,
        "B16": "/// ENTONNOIR", "H16": "/// FLUX · 14 JOURS",
        "B17": "Postulées", "D17": "=F6", "E17": '=IF($D$17=0;"–";1)',
        "B18": "Réponses", "D18": count_in(REPLIED), "E18": '=IF($D$17=0;"–";D18/$D$17)',
        "B19": "Entretiens", "D19": count_in(REACHED_INTERVIEW), "E19": '=IF($D$17=0;"–";D19/$D$17)',
        "B20": "Offres", "D20": "=J6", "E20": '=IF($D$17=0;"–";D20/$D$17)',
        "H17": (f'=SPARKLINE(ARRAYFORMULA(COUNTIF({M};SEQUENCE(14;1;TODAY()-13)));'
                f'{opts(charttype=q("column"), color=q(C["cyan"]), highcolor=q(C["pink"]), empty=q("zero"))})'),
        "H21": '=TEXT(TODAY()-13;"dd/mm")', "M21": '=TEXT(TODAY();"dd/mm")',
    }
    for row in range(17, 21):
        cells[f"F{row}"] = f"={bar(f'D{row}', '$D$17')}"
    return cells


# KPI cards: (first column, last column, border + label colour), pink and cyan alternating.
CARDS = [("B", "C", C["pink"]), ("D", "E", C["cyan"]), ("F", "G", C["pink"]), ("H", "I", C["cyan"]),
         ("J", "K", C["pink"]), ("L", "M", C["cyan"])]
MERGES = ["B2:I2", "J2:M2", "B3:C3", "E3:F3", "H3:I3", "K3:L3", "B4:M4", "H16:M16", "H17:M20"] + \
         [f"{a}{r}:{b}{r}" for a, b, _ in CARDS for r in (5, 6, 7)] + \
         [f"B{r}:C{r}" for r in range(17, 21)] + [f"F{r}:G{r}" for r in range(17, 21)]


def a1_grid(sid: int, a1: str) -> dict:
    start, _, end = a1.partition(":")
    end = end or start

    def split(cell):
        col = "".join(ch for ch in cell if ch.isalpha())
        return int(cell[len(col):]) - 1, ord(col) - ord("A")
    (r0, c0), (r1, c1) = split(start), split(end)
    return grid(sid, r0, r1 + 1, c0, c1 + 1)


def dashboard_requests(sid: int, this: dict) -> list[dict]:
    g = lambda a1: a1_grid(sid, a1)  # noqa: E731
    requests: list[dict] = [sheet_props(sid, rowCount=DASH_ROWS, columnCount=DASH_COLS, hideGridlines=True,
                                        tabColor=C["pink"], index=0)]
    requests += [{"deleteConditionalFormatRule": {"sheetId": sid, "index": i}}
                 for i in reversed(range(len(this.get("conditionalFormats", []))))]
    whole = grid(sid, 0, DASH_ROWS, 0, DASH_COLS)
    requests.append({"unmergeCells": {"range": whole}})
    requests.append({"repeatCell": {"range": whole, "cell": {"userEnteredFormat": {
        "backgroundColor": rgb(C["bg"]), "verticalAlignment": "MIDDLE", "wrapStrategy": "OVERFLOW_CELL",
        "textFormat": {"foregroundColor": rgb(C["text"]), "fontFamily": FONT_BODY, "fontSize": 10}}},
        "fields": "userEnteredFormat"}})  # the dashboard is the robot's: reset all formats, borders included
    requests += [{"mergeCells": {"range": g(m), "mergeType": "MERGE_ALL"}} for m in MERGES]
    requests += widths(sid, {0: 18, **{i: 92 for i in range(1, 13)}, 13: 18})
    requests += heights(sid, {0: 8, 1: 44, 2: 30, 3: 4, 4: 22, 5: 48, 6: 22, 7: 14, 8: 28,
                              **{r: 24 for r in range(9, 14)}, 14: 14, 15: 26, **{r: 24 for r in range(16, 20)},
                              20: 18, 21: 8})

    requests += [style(g("B2"), fg=C["pink"], font=FONT_DISPLAY, size=18),
                 style(g("J2"), fg=C["cyan"], font=FONT_MONO, size=11, h="RIGHT"),
                 style(g("B4:M4"), bg=C["yellow"]),                                   # yellow accent stripe
                 style(g("B3"), fg=C["yellow"], font=FONT_TITLE, size=8, bold=True, h="RIGHT"),
                 style(g("H3"), fg=C["yellow"], font=FONT_TITLE, size=8, bold=True, h="RIGHT"),
                 style(g("D3"), bg=C["panel"], fg=C["yellow"], font=FONT_MONO, size=13, bold=True, h="CENTER"),
                 style(g("J3"), bg=C["panel"], fg=C["yellow"], font=FONT_MONO, size=13, bold=True, h="CENTER"),
                 style(g("E3"), fg=C["yellow"], font=FONT_MONO, size=12, h="CENTER"),
                 style(g("K3"), fg=C["yellow"], font=FONT_MONO, size=12, h="CENTER"),
                 style(g("G3"), fg=C["text"], font=FONT_MONO, size=11),
                 style(g("M3"), fg=C["text"], font=FONT_MONO, size=11)]
    for a, b, color in CARDS:
        card = g(f"{a}5:{b}7")
        requests += [style(card, bg=C["panel"], h="CENTER"),
                     style(g(f"{a}5"), fg=color, font=FONT_TITLE, size=8, bold=True, v="BOTTOM"),
                     style(g(f"{a}6"), fg=C["white"], font=FONT_MONO, size=26, bold=True),
                     style(g(f"{a}7"), fg=C["muted"], font=FONT_MONO, size=9, v="TOP"),
                     {"updateBorders": {"range": card, **{side: {"style": "SOLID_MEDIUM", "colorStyle": {
                         "rgbColor": rgb(color)}} for side in ("top", "bottom", "left", "right")}}}]
    requests.append({"addConditionalFormatRule": {"index": 0, "rule": {   # follow-ups due: solid red card
        "ranges": [g("L5:M7")], "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=$L$6>0"}]},
            "format": fmt(C["red"], C["dark"], True)}}}})

    requests += [style(g("A9"), bg=C["yellow"]), style(g("A16"), bg=C["yellow"]),   # section stripes
                 style(g("B9"), fg=C["yellow"], font=FONT_TITLE, size=11, bold=True),
                 style(g("B10:M14"), bg=C["panel"]),
                 style(g("B10:B14"), fg=C["yellow"], font=FONT_MONO, size=11, bold=True, h="CENTER"),
                 style(g("C10:C14"), fg=C["cyan"], font=FONT_BODY, size=10),
                 style(g("B16"), fg=C["cyan"], font=FONT_TITLE, size=10, bold=True),
                 style(g("H16"), fg=C["cyan"], font=FONT_TITLE, size=10, bold=True),
                 style(g("B17:C20"), fg=C["text"], font=FONT_BODY, size=10),
                 style(g("D17:D20"), fg=C["white"], font=FONT_MONO, size=12, bold=True, h="RIGHT"),
                 style(g("E17:E20"), fg=C["muted"], font=FONT_MONO, size=11, h="RIGHT", number=("PERCENT", "0 %")),
                 style(g("F17:F20"), fg=C["yellow"], font=FONT_MONO, size=12, h="LEFT"),
                 style(g("H17:M20"), bg=C["panel"]),
                 style(g("H21"), fg=C["muted"], font=FONT_MONO, size=9),
                 style(g("M21"), fg=C["muted"], font=FONT_MONO, size=9, h="RIGHT")]
    return requests


# ------------------------------------------------------------ Prochaine étape ----
def next_step_formula(layout: Layout = DEFAULT_LAYOUT) -> str:
    """The whole "➜ Prochaine étape" column as ONE formula in its header cell, over whole columns:
    rows the sync inserts at row 2 are covered at once, and the robot never writes below it."""
    S, D = rng(layout.letter("Statut")), rng(layout.letter("Date candidature"))
    postule = (f'IF(NOT(ISNUMBER({D}));{q(POSTULE_NO_DATE)};IF(TODAY()-{D}>={FOLLOW_UP_DAYS};{q(POSTULE_DUE)};'
               f'{q(POSTULE_WAIT)}&TEXT({D}+{FOLLOW_UP_DAYS};"dd/mm")))')
    expr = '""'   # an unknown status: nothing
    for status, text in reversed(list(NEXT_STEPS.items())):
        expr = f"IF({S}={q(status)};{q(text)};{expr})"
    return f'=ARRAYFORMULA(IF(ROW({S})=1;{q(NEXT_STEP)};IF({S}="";"";IF({S}="Postulé";{postule};{expr}))))'


# ------------------------------------------------------------------- Guide ----
GUIDE_TAB = "📘 Guide"
GUIDE_FILE = Path(__file__).resolve().parent / "guide.fr.yaml"
GUIDE_COLS = 5                                  # A margin | B box/number | C | D | E margin
GUIDE_WIDTHS = {0: 18, 1: 46, 2: 230, 3: 640, 4: 18}


def load_guide() -> list[dict] | None:
    if not GUIDE_FILE.exists():
        return None
    return yaml.safe_load(GUIDE_FILE.read_text(encoding="utf-8"))["sections"]


def guide_plan(sections: list[dict]) -> tuple[dict[str, object], list[tuple[str, dict]], list[str], list[str], dict]:
    """(cells A1 -> value, [(A1 range, style kwargs)], merges, checkbox cells, checkbox A1 -> its text).
    The texts are copied verbatim from guide.fr.yaml."""
    cells: dict[str, object] = {"B2": "📘 GUIDE"}
    styles: list[tuple[str, dict]] = [("B2", dict(fg=C["pink"], font=FONT_DISPLAY, size=20))]
    merges, boxes, box_text = ["B2:D2"], [], {}
    r = 4
    for sec in sections:
        cells[f"B{r}"] = sec["title"]
        styles += [(f"A{r}", dict(bg=C["yellow"])), (f"B{r}", dict(fg=C["yellow"], font=FONT_TITLE, size=14, bold=True))]
        merges.append(f"B{r}:D{r}")
        r += 1
        for text in sec.get("checklist", []):
            cells[f"C{r}"] = text
            boxes.append(f"B{r}")
            box_text[f"B{r}"] = text
            merges.append(f"C{r}:D{r}")
            styles += [(f"B{r}", dict(fg=C["cyan"], h="CENTER")), (f"C{r}", dict(fg=C["text"], size=13, wrap="WRAP"))]
            r += 1
        if sec.get("note"):
            cells[f"C{r}"] = sec["note"]
            merges.append(f"C{r}:D{r}")
            styles.append((f"C{r}", dict(fg=C["muted"], size=11, italic=True)))
            r += 1
        for status, meaning in sec.get("table", []):
            style = next((st for st in STATUS_STYLE if status in st["statuses"]), None)
            block = (dict(bg=C["dim"], fg=C["muted"]) if style and style.get("dim")
                     else dict(bg=C[style["block"]], fg=C["dark"]) if style else dict(fg=C["text"]))
            cells[f"C{r}"], cells[f"D{r}"] = status, meaning
            styles += [(f"C{r}", dict(**block, size=12, bold=True, h="CENTER")),
                       (f"D{r}", dict(fg=C["text"], size=13, wrap="WRAP"))]
            r += 1
        for i, rule in enumerate(sec.get("numbered", []), 1):
            cells[f"B{r}"], cells[f"C{r}"] = f"{i}.", rule
            merges.append(f"C{r}:D{r}")
            styles += [(f"B{r}", dict(fg=C["yellow"], font=FONT_MONO, size=14, bold=True, h="CENTER")),
                       (f"C{r}", dict(fg=C["text"], size=13, wrap="WRAP"))]
            r += 1
        r += 1  # spacer between sections
    return cells, styles, merges, boxes, {"rows": r, **box_text}


def guide_requests(sid: int, this: dict, sections: list[dict]) -> list[dict]:
    cells, styles, merges, boxes, info = guide_plan(sections)
    g = lambda a1: a1_grid(sid, a1)  # noqa: E731
    rows = info["rows"] + 1
    whole = grid(sid, 0, rows, 0, GUIDE_COLS)
    requests = [sheet_props(sid, rowCount=rows, columnCount=GUIDE_COLS, hideGridlines=True, tabColor=C["violet"],
                            index=1),
                {"unmergeCells": {"range": whole}},
                {"setDataValidation": {"range": whole}},          # clears any old validation
                {"repeatCell": {"range": whole, "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb(C["bg"]), "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                    "textFormat": {"foregroundColor": rgb(C["text"]), "fontFamily": FONT_BODY, "fontSize": 12}}},
                    "fields": "userEnteredFormat"}}]                # the Guide is the robot's: reset its formats
    requests += [{"mergeCells": {"range": g(m), "mergeType": "MERGE_ALL"}} for m in merges]
    requests += [style(g(a1), **kw) for a1, kw in styles]
    requests += [{"setDataValidation": {"range": g(b), "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}}
                 for b in boxes]
    requests += widths(sid, GUIDE_WIDTHS)
    requests += heights(sid, {0: 8, 1: 48, 2: 10, **{i: 34 for i in range(3, rows)}})
    return requests


# ---------------------------------------------------------- Mes candidatures ----
APP_COLUMNS = ["Titre", "Entreprise", "Lieu", "Score", "Statut", "Date candidature", "CV utilisé", "Contact",
               "Prochaine action", "Date prochaine action", "Notes", "Lien"]
APP_WIDTHS = [300, 150, 150, 55, 130, 100, 100, 150, 160, 100, 280, 220]
PROTECTION = "Vue générée par setup_sheet.py : modifier dans Offres"


def applications_formula(layout: Layout = DEFAULT_LAYOUT) -> str:
    """QUERY over an array of column references, not column letters inside the query text, so
    Sheets keeps it right when columns are moved. The query addresses the array's Col1..Col12."""
    array = "{" + ARRAY_COL_SEP.join(rng(layout.letter(c)) for c in APP_COLUMNS) + "}"
    status, applied_on = APP_COLUMNS.index("Statut") + 1, APP_COLUMNS.index("Date candidature") + 1
    query = f"select * where Col{status} matches '{rx(APPLIED)}' order by Col{applied_on} desc"
    return f'=IFERROR(QUERY({array};{q(query)};0);"Aucune candidature pour l\'instant")'


def applications_requests(sid: int, this: dict) -> list[dict]:
    n = len(APP_COLUMNS)
    requests = [sheet_props(sid, frozenRowCount=1, hideGridlines=True, tabColor=C["cyan"], columnCount=n, index=2)]
    requests += [{"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
                 for p in this.get("protectedRanges", []) if p.get("description") == PROTECTION]
    requests.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sid}, "description": PROTECTION, "warningOnly": True}}})
    requests.append(style(grid(sid, 0, None, 0, n), bg=C["bg"], fg=C["text"], font=FONT_BODY, size=10))
    requests.append(style(grid(sid, 0, 1, 0, n), bg=C["panel"], fg=C["cyan"], font=FONT_TITLE, size=8, bold=True,
                          v="MIDDLE", wrap="WRAP"))
    idx = {c: i for i, c in enumerate(APP_COLUMNS)}
    for c in ("Date candidature", "Date prochaine action"):
        requests.append(style(grid(sid, 1, None, idx[c], idx[c] + 1), font=FONT_MONO, number=("DATE", "dd/mm/yyyy")))
    requests.append(style(grid(sid, 1, None, idx["Score"], idx["Score"] + 1), font=FONT_MONO, bold=True,
                          fg=C["yellow"], h="CENTER", number=("NUMBER", "0")))
    # The same Statut blocks as Offres, so both tabs read the same from far away. This tab's
    # rules are all the robot's: replaced wholesale.
    requests += [{"deleteConditionalFormatRule": {"sheetId": sid, "index": i}}
                 for i in reversed(range(len(this.get("conditionalFormats", []))))]
    statut = idx["Statut"]
    requests += conditional_rules(status_rules(col=col_letter(statut)),
                                  {"status": grid(sid, 0, None, statut, statut + 1)})
    requests.append(style(grid(sid, 1, None, idx["Lien"], idx["Lien"] + 1), fg=C["cyan"]))
    requests += widths(sid, dict(enumerate(APP_WIDTHS)))
    requests += heights(sid, {0: 36})
    return requests


# --------------------------------------------------------------------- main ----
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet-id", help="set up another spreadsheet (rehearsal) instead of SHEET_ID")
    parser.add_argument("--arrange", action="store_true",
                        help="one-time: reorder the Offres columns into the canonical focus-mode order")
    args = parser.parse_args()
    sh = open_spreadsheet(args.sheet_id)
    sh.batch_update({"requests": [{"updateSpreadsheetProperties": {
        "properties": {"timeZone": TIMEZONE, "locale": "fr_FR",
                       "spreadsheetTheme": {"primaryFontFamily": FONT_BODY, "themeColors": [
                           {"colorType": t, "color": {"rgbColor": rgb(c)}} for t, c in (
                               ("TEXT", C["text"]), ("BACKGROUND", C["bg"]), ("ACCENT1", C["cyan"]),
                               ("ACCENT2", C["pink"]), ("ACCENT3", C["yellow"]), ("ACCENT4", C["violet"]),
                               ("ACCENT5", C["blue"]), ("ACCENT6", C["red"]), ("LINK", C["cyan"]))]}},
        "fields": "timeZone,locale,spreadsheetTheme"}}]})

    ws = get_or_create_offres(sh)
    header = [str(h).strip() for h in ws.row_values(1)]
    while header and not header[-1]:
        header.pop()
    missing = [col for col in COLUMNS if col not in header]
    if not header:  # empty sheet: the canonical header
        if ws.col_count < len(COLUMNS):
            ws.resize(cols=len(COLUMNS))
        ws.update(range_name="A1", values=[COLUMNS], value_input_option="RAW")
        header = list(COLUMNS)
    elif missing and set(missing) <= set(ROBOT_COLUMNS + EXTRA_ROBOT_COLUMNS):
        # Robot columns added since the last setup. A missing *user* header is never invented
        # (LayoutError below). PLACE_AFTER columns are inserted right after their anchor (the
        # columns to the right shift, their data and formulas with them); others go at the end.
        for col in missing:
            anchor = PLACE_AFTER.get(col)
            at = header.index(anchor) + 1 if anchor in header else len(header)
            if at < len(header):
                sh.batch_update({"requests": [
                    {"insertDimension": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": at,
                                                   "endIndex": at + 1}, "inheritFromBefore": False}},
                    {"setDataValidation": {"range": grid(ws.id, 0, None, at, at + 1)}}]})  # no inherited dropdown
            elif ws.col_count < len(header) + 1:
                ws.resize(cols=len(header) + 1)
            ws.update(range_name=f"{col_letter(at)}1", values=[[col]], value_input_option="RAW")
            header.insert(at, col)
            print(f"  added column {col!r} at {col_letter(at)}")
    try:  # columns may have been moved: everything below locates them by header
        layout = layout_from_header(header)
    except LayoutError as exc:
        sys.exit(f"{exc}. Fix row 1 (each expected header exactly once); nothing was changed.")
    if args.arrange:
        arrange(sh, ws, layout)
        layout = layout_from_header(ws.row_values(1))

    dash = get_or_create(sh, DASHBOARD_TAB, DASH_ROWS, DASH_COLS)
    sections = load_guide()
    guide = get_or_create(sh, GUIDE_TAB, 40, GUIDE_COLS) if sections else None
    apps = get_or_create(sh, APPLICATIONS_TAB, 1000, len(APP_COLUMNS))
    ticks: dict[str, bool] = {}   # routine checkboxes: the user's, kept by their text
    if guide:
        for row in guide.get("B1:C200", value_render_option=ValueRenderOption.unformatted, pad_values=True):
            if len(row) >= 2 and isinstance(row[0], bool):
                ticks[str(row[1])] = row[0]
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties.sheetId,conditionalFormats,basicFilter,slicers,"
                                              "protectedRanges,filterViews,columnGroups)"})
    by_id = {s["properties"]["sheetId"]: s for s in meta["sheets"]}

    # Goal cells: the user's once set; read before the dashboard is rewritten.
    found = dash.batch_get(list(GOALS), value_render_option=ValueRenderOption.unformatted)
    goals = {cell: (vr[0][0] if vr and vr[0] else None) for cell, vr in zip(GOALS, found)}
    this = by_id[ws.id]
    requests = (offres_requests(ws.id, this, layout) + remove_slicers(ws.id, this)
                + filter_view_requests(ws.id, this, layout) + group_requests(ws.id, this, layout)
                + protection_requests(ws.id, this, layout)
                + dashboard_requests(dash.id, by_id[dash.id]) + applications_requests(apps.id, by_id[apps.id]))
    if guide:
        requests += guide_requests(guide.id, by_id[guide.id], sections)
    sh.batch_update({"requests": requests})

    # Formula columns: one formula in the header cell; the cells below must stay empty for it.
    for col in FORMULA_COLUMNS:
        letter = layout.letter(col)
        ws.batch_clear([f"{letter}2:{letter}"])
        ws.update(range_name=f"{letter}1", values=[[next_step_formula(layout)]], value_input_option="USER_ENTERED")

    if guide:
        g_cells, _, _, boxes, info = guide_plan(sections)
        g_cells.update({b: ticks.get(info[b], False) for b in boxes})
        guide.batch_clear([f"A1:{col_letter(GUIDE_COLS - 1)}{info['rows'] + 1}"])
        sh.values_batch_update({"valueInputOption": "USER_ENTERED", "data": [
            {"range": f"'{GUIDE_TAB}'!{a1}", "values": [[v]]} for a1, v in g_cells.items()]})

    cells = dashboard_cells(layout)
    for cell, default in GOALS.items():
        cells[cell] = goals.get(cell) if goals.get(cell) not in (None, "") else default
    dash.batch_clear([f"A1:{chr(ord('A') + DASH_COLS - 1)}{DASH_ROWS}"])
    sh.values_batch_update({"valueInputOption": "USER_ENTERED", "data": [
        {"range": f"'{DASHBOARD_TAB}'!{a1}", "values": [[v]]} for a1, v in cells.items()]})
    apps.batch_clear([f"A:{chr(ord('A') + len(APP_COLUMNS) - 1)}"])
    apps.update(range_name="A1", values=[APP_COLUMNS, [applications_formula(layout)]],
                value_input_option="USER_ENTERED")
    print(f"Ready: '{TAB}' ({len(RULES)} colour rules + score scale, 3 dropdowns, {len(FILTER_VIEWS)} filter "
          f"views, technical columns grouped), '{DASHBOARD_TAB}', '{APPLICATIONS_TAB}' (read-only view).")


if __name__ == "__main__":
    main()
