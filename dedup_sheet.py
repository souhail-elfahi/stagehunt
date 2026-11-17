"""Merge the cross-site duplicates already in the sheet (same rules as the sync, see dedup.py).

    python dedup_sheet.py           dry run: prints the plan, saves it to backups/dedup_plan.json
    python dedup_sheet.py --apply   backs up, re-computes the plan, refuses if it differs from
                                    the saved dry run, then merges

Per group of duplicates, one row is kept: the one holding your data if any, otherwise the
first detected. The other links go to its "Autres liens"; detection dates are combined.
A row is deleted only if all your columns are empty ("À trier" counts as empty). A duplicate
that holds your data is never deleted: it gets "Doublon ?" pointing at the kept row instead.
Look-alikes from unrelated companies ("twins") are only flagged in "Doublon ?".
"""

from __future__ import annotations

import argparse
import json
import sys

from gspread.utils import ValueRenderOption

from dedup import classify, near_miss, norm_title, title_similarity
from sheet_lib import ROOT, TAB, Layout, fmt_dt, from_serial, now_paris, open_spreadsheet, read_layout
from sync_sheet import NEW_FOR, backup, has_user_data, is_blank, sheet_offer, split_links, twin_formula

PLAN_FILE = ROOT / "backups" / "dedup_plan.json"


def read_rows(ws) -> tuple[Layout, dict[int, list]]:
    """Columns located by header (row 1, read now): LayoutError if one is missing or doubled."""
    layout = read_layout(ws)
    rows = ws.get(f"A2:{layout.last}", value_render_option=ValueRenderOption.unformatted, pad_values=True)
    return layout, {n: row for n, row in enumerate(rows, start=2) if not is_blank(row)}


def make_plan(rows: dict[int, list], layout: Layout) -> dict:
    _cell = layout.cell
    offers = {n: sheet_offer(row, layout) for n, row in rows.items()}
    ns = sorted(offers)
    parent = {n: n for n in ns}

    def find(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    reasons: dict[tuple[int, int], str] = {}
    for i, a in enumerate(ns):
        for b in ns[i + 1:]:
            if classify(offers[a], offers[b]) == "merge":
                parent[find(b)] = find(a)
                same = norm_title(offers[a]["title"]) == norm_title(offers[b]["title"])
                reasons[(a, b)] = "même titre" if same else \
                    f"même entreprise, titre similaire ({title_similarity(offers[a]['title'], offers[b]['title']):.2f})"

    groups: dict[int, list[int]] = {}
    for n in ns:
        groups.setdefault(find(n), []).append(n)

    merges, deleted = [], set()
    for members in (g for g in groups.values() if len(g) > 1):
        with_data = [n for n in members if has_user_data(rows[n], layout)]
        keep = with_data[0] if with_data else min(
            members, key=lambda n: (from_serial(_cell(rows[n], "Première détection")) or now_paris(), n))
        delete = [n for n in members if n != keep and n not in with_data]
        deleted.update(delete)
        merges.append({
            "keep": offers[keep]["url"], "delete": [offers[n]["url"] for n in delete],
            "kept_with_data": [offers[n]["url"] for n in with_data if n != keep],
            "why": sorted({r for (a, b), r in reasons.items() if a in members and b in members}),
        })

    twins, near = [], []
    for i, a in enumerate(ns):
        for b in ns[i + 1:]:
            if a in deleted or b in deleted or find(a) == find(b):
                continue
            if classify(offers[a], offers[b]) == "twin":
                twins.append([offers[a]["url"], offers[b]["url"]])
            elif why := near_miss(offers[a], offers[b]):
                near.append([offers[a]["url"], offers[b]["url"], why])
    return {"merges": merges, "twins": twins, "near_misses": near}


def describe(plan: dict, rows: dict[int, list], layout: Layout) -> str:
    _cell = layout.cell
    at = {}
    for n, row in rows.items():
        at[str(_cell(row, "Lien")).strip()] = n

    def label(url: str) -> str:
        row = rows[at[url]]
        published = from_serial(_cell(row, "Date publication"))
        when = f", {published:%d/%m}" if published else ""
        return f"L{at[url]} {_cell(row, 'Entreprise')} ({_cell(row, 'Site')}{when})"

    out = [f"FUSIONS ({len(plan['merges'])} groupes, {sum(len(m['delete']) for m in plan['merges'])} lignes supprimées)"]
    for m in plan["merges"]:
        row = rows[at[m["keep"]]]
        out.append(f"- {_cell(row, 'Titre')} · {str(_cell(row, 'Lieu')).split(',')[0]}  [{'; '.join(m['why'])}]")
        out.append(f"    garder    {label(m['keep'])}")
        out += [f"    supprimer {label(u)}  -> lien dans Autres liens" for u in m["delete"]]
        out += [f"    GARDÉE (contient tes données) {label(u)} -> Doublon ?" for u in m["kept_with_data"]]
    out.append(f"\nDOUBLON ? ({len(plan['twins'])} paires, rien supprimé)")
    for a, b in plan["twins"]:
        out.append(f"- {_cell(rows[at[a]], 'Titre')}\n    {label(a)}  <->  {label(b)}")
    out.append(f"\nPAS FUSIONNÉS, à vérifier ({len(plan['near_misses'])})")
    for a, b, why in plan["near_misses"]:
        out.append(f"- {_cell(rows[at[a]], 'Titre')}: {label(a)} / {label(b)}  [{why}]")
    return "\n".join(out)


def apply(plan: dict, ws, sh) -> None:
    layout, rows = read_rows(ws)
    _cell = layout.cell
    at = {str(_cell(row, "Lien")).strip(): n for n, row in rows.items()}
    now = now_paris()
    updates: list[dict] = []

    def put(name: str, n: int, value: str) -> None:
        a1 = f"{layout.letter(name)}{n}"
        layout.check_robot_range(a1)
        updates.append({"range": a1, "values": [[value]]})

    def set_twin(n: int, other_url: str) -> None:
        if not str(_cell(rows[n], "Doublon ?")).strip():
            put("Doublon ?", n, twin_formula(other_url, _cell(rows[at[other_url]], "Entreprise"), layout))

    for m in plan["merges"]:
        k = at[m["keep"]]
        group = [k] + [at[u] for u in m["delete"]]
        links = split_links(_cell(rows[k], "Autres liens"))
        for n in group[1:]:
            for u in split_links(_cell(rows[n], "Lien")) + split_links(_cell(rows[n], "Autres liens")):
                if u not in links and u != m["keep"]:
                    links.append(u)
        put("Autres liens", k, "\n".join(links))
        firsts = [d for n in group if (d := from_serial(_cell(rows[n], "Première détection")))]
        lasts = [d for n in group if (d := from_serial(_cell(rows[n], "Dernière détection")))]
        if firsts:
            put("Première détection", k, fmt_dt(min(firsts)))
            put("Nouveau", k, "oui" if now - min(firsts) <= NEW_FOR else "non")
        if lasts:
            put("Dernière détection", k, fmt_dt(max(lasts)))
        if any(_cell(rows[n], "En ligne") == "oui" for n in group):
            put("En ligne", k, "oui")
        for u in m["kept_with_data"]:
            set_twin(at[u], m["keep"])
    for a, b in plan["twins"]:
        set_twin(at[a], b)
        set_twin(at[b], a)
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    # Re-read right before deleting: only rows whose user columns are still empty go.
    layout, rows = read_rows(ws)
    _cell = layout.cell
    at = {str(_cell(row, "Lien")).strip(): n for n, row in rows.items()}
    to_delete = []
    for m in plan["merges"]:
        for u in m["delete"]:
            n = at.get(u)
            if n is None:
                continue
            if has_user_data(rows[n], layout):
                print(f"  not deleted (you edited it meanwhile): L{n} {u}")
                continue
            to_delete.append(n)
    sh.batch_update({"requests": [
        {"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
        for n in sorted(to_delete, reverse=True)]})  # bottom-up so row numbers stay valid
    print(f"Merged {len(plan['merges'])} groups, deleted {len(to_delete)} rows, "
          f"flagged {len(plan['twins'])} twin pairs.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="apply the plan saved by the last dry run")
    args = parser.parse_args()

    sh = open_spreadsheet()
    ws = sh.worksheet(TAB)
    layout, rows = read_rows(ws)
    plan = make_plan(rows, layout)

    if not args.apply:
        print(describe(plan, rows, layout))
        PLAN_FILE.parent.mkdir(exist_ok=True)
        PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nDry run only. Plan saved to {PLAN_FILE.relative_to(ROOT)}; apply with --apply.")
        return

    if not PLAN_FILE.exists():
        sys.exit("No saved dry run: run without --apply first.")
    saved = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    if saved != plan:
        sys.exit("The sheet changed since the dry run (plan differs). Run the dry run again and review it.")
    print(f"Backup: {backup(ws, now_paris())}")
    apply(plan, ws, sh)
    PLAN_FILE.unlink()


if __name__ == "__main__":
    main()
