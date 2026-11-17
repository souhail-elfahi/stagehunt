"""Shared CV machinery: fact bank -> spec -> ATS-friendly .docx, plus honesty and page checks.

Golden rule: this module never writes prose. Every line of a CV is either text from the
user-approved fact bank (cv_facts.fr.yaml), a fixed heading, or a formatting of bank fields
(contact, dates, "Outils : ...", skills lines built from profile.md tiers).
Other languages are a translation file (cv_facts.<lang>.yaml) laid over the French bank by id:
same entries, tags, dates and order, only the texts change (see `translate`).
`check_honesty` re-reads the produced .docx and rejects any paragraph that isn't one of those.

The design lives entirely in `_setup` / `render_docx`, so every CV of the pipeline shares it:
single column, real text only (no tables, text boxes, images, header/footer), one accent colour.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from analyze_market import parse_profile

FACTS = "cv_facts.fr.yaml"
TRANSLATION = "cv_facts.{lang}.yaml"   # texts only, keyed by the ids of the French bank
OUT_DIR = Path("cvs")

H_FORMATION = "Formation"
H_EXPERIENCE = "Expérience professionnelle"
H_PROJETS = "Projets"
H_COMPETENCES = "Compétences"
ADVANCED_CATEGORY = "Programmation"   # "(avancé)" only for tier-1 skills of this category
# Fixed labels per language. Sections and categories keep their French names as internal keys.
LOCALES = {
    "fr": {"headings": {H_FORMATION: "Formation", H_EXPERIENCE: "Expérience professionnelle",
                        H_PROJETS: "Projets", H_COMPETENCES: "Compétences"},
           "courses": "Cours", "tools": "Outils", "colon": " :", "advanced": "avancé",
           "languages": "Langues"},
    "en": {"headings": {H_FORMATION: "Education", H_EXPERIENCE: "Professional Experience",
                        H_PROJETS: "Projects", H_COMPETENCES: "Skills"},
           "courses": "Courses", "tools": "Tools", "colon": ":", "advanced": "advanced",
           "languages": "Languages"},
}
SEP = " · "                            # skills separator (labels may contain commas)
DASH = " — "

# ---- design tokens ----
FONT = "Calibri"
NAME_PT, HEADLINE_PT, HEADING_PT = 24, 11, 10.5
BODY_SIZES = (10.5, 10)                # largest body size that fits one page wins; never below 10
SMALL_DELTA = 0.5                      # dates, contact, "Outils :" / "Cours :" lines
ACCENT_HEX = "1F3A5F"                  # the one accent colour: name, headings, rules
ACCENT = RGBColor.from_string(ACCENT_HEX)
INK, MUTED = RGBColor(0x1A, 0x1A, 0x1A), RGBColor(0x59, 0x5F, 0x69)
GAP_SECTION, GAP_ENTRY, GAP_LINE = Pt(9), Pt(5), Pt(1)   # the only vertical gaps used


# ------------------------------------------------------------------- loading ----

def load_facts(lang: str = "fr", path: str = FACTS) -> dict:
    facts = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    pending = [e["id"] for s in ("formation", "experiences", "projets") for e in facts[s] if "check" in e]
    if pending:
        raise SystemExit(f"Fact bank entries still marked `check:` (resolve them first): {pending}")
    facts["lang"] = lang
    if lang != "fr":
        tr_path = Path(path).with_name(TRANSLATION.format(lang=lang))
        translate(facts, yaml.safe_load(tr_path.read_text(encoding="utf-8")), tr_path.name)
    return facts


def translate(facts: dict, tr: dict, name: str) -> None:
    """Replace every text of the French bank by its translation, in place. Structure, ids, skill
    tags, `start` dates and saved skills lists stay those of the French bank, so a translation
    can't add an entry or a bullet. Anything missing or unknown stops the build."""
    errors: list[str] = []

    def take(src, key: str, where: str):
        if not isinstance(src, dict) or not src.get(key):
            errors.append(f"missing: {where}.{key}")
            return None
        return src[key]

    def unknown(given, known, where: str) -> None:
        errors.extend(f"unknown: {where}.{k}" for k in (given or {}) if k not in known)

    if facts.get("headline"):
        facts["headline"] = take(tr, "headline", "top level")
    for sec, fields in (("formation", ("degree", "institution", "dates", "courses")),
                        ("experiences", ("role", "organisation", "dates", "tools")),
                        ("projets", ("role", "organisation", "dates", "tools"))):
        tsec = tr.get(sec) or {}
        unknown(tsec, {e["id"] for e in facts[sec]}, sec)
        for e in facts[sec]:
            te, where = tsec.get(e["id"]) or {}, f"{sec}.{e['id']}"
            for f in fields:
                if e.get(f):
                    e[f] = take(te, f, where)
            tb = te.get("bullets") or {}
            unknown(tb, {b["id"] for b in e.get("bullets", [])}, where + ".bullets")
            for b in e.get("bullets", []):
                t = tb.get(b["id"])
                t = {"text": t} if isinstance(t, str) else (t or {})
                b["text"] = take(t, "text", f"{where}.bullets.{b['id']}")
                b["variants"] = t.get("variants") or {}
    items = tr.get("langues") or []
    if len(items) != len(facts["langues"]["items"]):
        errors.append("langues: not the same number of languages as the French bank")
    facts["langues"]["items"] = items
    tcats = tr.get("categories") or {}
    unknown(tcats, facts["categories"], "categories")
    facts["category_names"] = {}
    for cat, mapping in facts["categories"].items():
        tc = tcats.get(cat) or {}
        facts["category_names"][cat] = take(tc, "name", f"categories.{cat}")
        unknown(tc.get("labels"), mapping, f"categories.{cat}.labels")
        for skill in mapping:
            mapping[skill] = take(tc.get("labels"), skill, f"categories.{cat}.labels")
    facts["aliases"] = tr.get("aliases") or {}
    facts["tailoring"] = {**(facts.get("tailoring") or {}), **(tr.get("tailoring") or {})}
    if errors:
        raise SystemExit(f"{name} doesn't match the French bank:\n  " + "\n  ".join(errors))


def locale(facts: dict) -> dict:
    return LOCALES[facts.get("lang", "fr")]


def category_name(facts: dict, cat: str) -> str:
    """Displayed name of a skills category (bank keys are the French names)."""
    return facts.get("category_names", {}).get(cat, cat)


def load_tiers(path: str = "profile.md") -> dict[str, int | None]:
    return parse_profile(path)


def headline(facts: dict) -> str:
    """The user's own headline; without one, the current degree title verbatim."""
    return facts.get("headline") or facts["formation"][0]["degree"]


# ---------------------------------------------------------------------- spec ----

@dataclass
class Item:
    """One formation / experience / project entry with its selected bullets."""
    kind: str                      # formation | experience | projet
    id: str
    title: str                     # role, or degree title
    org: str | None                # organisation (None for formation: institution is `subline`)
    dates: str
    detail: str | None = None      # "Cours : ..." (formation) or "Outils : ..." (others)
    bullets: list[tuple[str, str, float]] = field(default_factory=list)  # (id, text, score)
    score: float = 0.0
    subline: str | None = None     # institution line under a degree
    skills: list[str] = field(default_factory=list)


@dataclass
class CVSpec:
    name: str
    headline: str
    contact: str
    sections: list[str]
    items: dict[str, list[Item]]
    skills: list[tuple[str, list[str]]]     # (category, labels), Langues included
    shown_skills: set[str] = field(default_factory=set)
    vocab_swaps: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    lang: str = "fr"


def contact_line(c: dict) -> str:
    strip = lambda u: re.sub(r"^https?://(www\.)?", "", u).rstrip("/")  # noqa: E731
    return SEP.join([c["city"], c["phone"], c["email"], strip(c["linkedin"]), strip(c["github"])])


def bullet_text(b: dict, cluster: str | None) -> str:
    return (b.get("variants") or {}).get(cluster, b["text"]) if cluster else b["text"]


TOOL_CATEGORIES = {"Programmation", "Outils"}
TOOL_FACTOR = 0.5   # tools already show in "Outils :" lines and Compétences; methods drive order


def skill_weight(skill: str, weights: dict[str, float], tools: set[str]) -> float:
    return weights.get(skill, 0.0) * (TOOL_FACTOR if skill in tools else 1.0)


def tool_skills(facts: dict) -> set[str]:
    return {s for c in TOOL_CATEGORIES for s in facts["categories"].get(c, {})}


def build_items(facts: dict, weights: dict[str, float], cluster: str | None) -> dict[str, list[Item]]:
    """Expérience professionnelle: most recent first (bank `start`), always.
    Projets: most relevant first when there are weights. Formation: bank order (chronology).
    Bullets keep the bank order inside an entry; scores only decide what is dropped first."""
    tools, loc = tool_skills(facts), locale(facts)
    score = lambda skills: sum(skill_weight(s, weights, tools) for s in skills)  # noqa: E731
    detail = lambda key, value: f"{loc[key]}{loc['colon']} {value}" if value else None  # noqa: E731
    items: dict[str, list[Item]] = {H_FORMATION: [], H_EXPERIENCE: [], H_PROJETS: []}
    for f in facts["formation"]:
        items[H_FORMATION].append(Item(
            "formation", f["id"], f["degree"], None, f["dates"],
            detail("courses", f.get("courses")),
            score=score(f.get("skills", [])), subline=f["institution"], skills=f.get("skills", [])))
    for section, key in ((H_EXPERIENCE, "experiences"), (H_PROJETS, "projets")):
        entries = facts[key]
        if section == H_EXPERIENCE:
            entries = sorted(entries, key=lambda e: e.get("start", ""), reverse=True)
        for e in entries:
            bullets = [(b["id"], bullet_text(b, cluster), score(b["skills"])) for b in e["bullets"]]
            items[section].append(Item(
                key[:-1], e["id"], e["role"], e["organisation"], e["dates"],
                detail("tools", e.get("tools")), bullets,
                score=sum(b[2] for b in bullets)))
    if weights:
        items[H_PROJETS].sort(key=lambda i: -i.score)
    return items


def pick_label(skill: str, default: str, aliases: dict, texts: list[str]) -> str:
    """Default label unless a listed synonym is strictly more frequent in the target offers."""
    if not texts or skill not in aliases:
        return default
    blob = "\n".join(texts)
    base = re.escape(re.sub(r"\s*\(.*?\)", "", default).lower())
    best, best_n = default, len(re.findall(base, blob, re.IGNORECASE))
    for a in aliases[skill]:
        n = len(re.findall(a["match"], blob, re.IGNORECASE))
        if n > best_n:
            best, best_n = a["label"], n
    return best


def build_skills(facts: dict, tiers: dict, weights: dict[str, float], vocab_texts: list[str],
                 priority: dict[str, float] | None = None,
                 only: list[str] | None = None) -> tuple[list, list[str], set[str]]:
    """Category lines: tier 1 then tier 2; within a tier by priority (offer) then weights (demand).
    `only` restricts the lines to a selection (tailored CVs) and gives the order within a tier;
    tier 3 / blank never appear."""
    aliases, swaps, lines, shown_all = facts.get("aliases", {}), [], [], set()
    priority, loc = priority or {}, locale(facts)
    for cat, mapping in facts["categories"].items():
        shown = [s for s in mapping if tiers.get(s) in (1, 2) and (only is None or s in only)]
        if only is None:
            shown.sort(key=lambda s: (tiers[s], -priority.get(s, 0), -weights.get(s, 0)))
        else:
            shown.sort(key=lambda s: (tiers[s], only.index(s)))
        labels = []
        for s in shown:
            label = pick_label(s, mapping[s], aliases, vocab_texts)
            if label != mapping[s]:
                swaps.append(f"{mapping[s]} → {label}")
            if tiers[s] == 1 and cat == ADVANCED_CATEGORY:
                label += f" ({loc['advanced']})"
            labels.append(label)
        if labels:
            lines.append((cat, labels))
            shown_all |= set(shown)
    if weights:  # category with the most demand first (tools at TOOL_FACTOR, as for entries)
        tools = tool_skills(facts)
        demand = {c: sum(skill_weight(s, weights, tools) for s in m if s in shown_all)
                  for c, m in facts["categories"].items()}
        lines.sort(key=lambda cl: -demand.get(cl[0], 0))
    lines = [(category_name(facts, cat), labels) for cat, labels in lines]
    lines.append((loc["languages"], language_labels(facts)))
    return lines, swaps, shown_all


def language_labels(facts: dict) -> list[str]:
    return [re.sub(r"^(.*?)\s*:\s*(.*)$", r"\1 (\2)", x) for x in facts["langues"]["items"]]


def build_spec(facts: dict, tiers: dict, sections: list[str],
               weights: dict[str, float] | None = None, cluster: str | None = None,
               vocab_texts: list[str] | None = None, priority: dict[str, float] | None = None,
               only_skills: list[str] | None = None) -> CVSpec:
    weights = weights or {}
    skills, swaps, shown = build_skills(facts, tiers, weights, vocab_texts or [], priority, only_skills)
    return CVSpec(facts["contact"]["name"], headline(facts), contact_line(facts["contact"]),
                  sections, build_items(facts, weights, cluster), skills, shown, swaps,
                  lang=facts.get("lang", "fr"))


# -------------------------------------------------------------------- render ----

# children of w:pPr that must come after w:pBdr (schema order)
_AFTER_PBDR = ("w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
               "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi",
               "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
               "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection",
               "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
               "w:rPr", "w:sectPr", "w:pPrChange")


def _rule_below(pPr, size_eighths: int, space_pt: int) -> None:
    """A thin accent rule under a paragraph (paragraph border: plain text for an ATS)."""
    old = pPr.find(qn("w:pBdr"))
    if old is not None:
        pPr.remove(old)
    bdr, bottom = OxmlElement("w:pBdr"), OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": str(size_eighths), "w:space": str(space_pt),
                 "w:color": ACCENT_HEX}.items():
        bottom.set(qn(k), v)
    bdr.append(bottom)
    pPr.insert_element_before(bdr, *_AFTER_PBDR)


def _own_font(style) -> None:
    """Replace the template's theme fonts with FONT on a built-in style."""
    style.font.name = FONT
    rf = style.element.rPr.find(qn("w:rFonts"))
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rf.attrib.pop(qn(a), None)
    for a in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rf.set(qn(a), FONT)


def _setup(doc: Document, body_pt: float):
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.top_margin = sec.bottom_margin = Cm(1.4)
    sec.left_margin = sec.right_margin = Cm(1.8)
    st = doc.styles

    normal = st["Normal"]
    _own_font(normal)
    normal.font.size, normal.font.color.rgb = Pt(body_pt), INK
    pf = normal.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(0), 1.0

    title = st["Title"]                                   # the name
    _own_font(title)
    title.font.size, title.font.bold, title.font.color.rgb = Pt(NAME_PT), True, ACCENT
    bdr = title.element.pPr.find(qn("w:pBdr"))            # template draws a rule here: drop it
    if bdr is not None:
        title.element.pPr.remove(bdr)
    title.paragraph_format.space_before = Pt(0)
    title.paragraph_format.space_after = Pt(0)
    title.paragraph_format.line_spacing = 1.0

    h1 = st["Heading 1"]                                  # section headings
    _own_font(h1)
    h1.font.size, h1.font.bold, h1.font.color.rgb = Pt(HEADING_PT), True, ACCENT
    h1.font.all_caps = True                               # display only: the text stays "Formation"
    tracking = OxmlElement("w:spacing")
    tracking.set(qn("w:val"), "16")                       # +0.8pt letter spacing
    h1.element.rPr.find(qn("w:color")).addnext(tracking)
    hp = h1.paragraph_format
    hp.space_before, hp.space_after, hp.keep_with_next = GAP_SECTION, Pt(4), True
    _rule_below(h1.element.pPr, size_eighths=4, space_pt=2)

    lb = st["List Bullet"]
    _own_font(lb)
    lb.font.size = Pt(body_pt)
    lb.paragraph_format.space_after = GAP_LINE
    return sec


def _run(paragraph, text: str, *, bold=False, size=None, color=None):
    r = paragraph.add_run(text)
    r.bold = bold
    if size:
        r.font.size = Pt(size)
    if color is not None:
        r.font.color.rgb = color
    return r


def render_docx(spec: CVSpec, path: Path, body_pt: float = BODY_SIZES[0]) -> Path:
    doc = Document()
    sec = _setup(doc, body_pt)
    loc = LOCALES[spec.lang]
    small = body_pt - SMALL_DELTA
    text_width = sec.page_width - sec.left_margin - sec.right_margin
    doc.core_properties.author = spec.name
    doc.core_properties.title = f"CV {spec.name}"

    # ---- header: name / headline / contact, closed by one accent rule ----
    doc.add_paragraph(spec.name, style="Title")
    p = doc.add_paragraph()
    p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(1), Pt(3)
    lead, sep, rest = spec.headline.partition(DASH)
    _run(p, lead, bold=True, size=HEADLINE_PT)
    if sep:
        _run(p, sep + rest, size=HEADLINE_PT)
    p = doc.add_paragraph()
    _run(p, spec.contact, size=small, color=MUTED)
    _rule_below(p._p.get_or_add_pPr(), size_eighths=8, space_pt=6)
    p.paragraph_format.space_after = Pt(2)

    for section in spec.sections:
        if section != H_COMPETENCES and not spec.items.get(section):
            continue
        doc.add_paragraph(loc["headings"][section], style="Heading 1")
        if section == H_COMPETENCES:
            # label, tab, values with a hanging indent: wrapped values stay aligned (no table)
            longest = max(len(cat) for cat, _ in spec.skills) + 2
            col = Pt(round(longest * 0.46 * body_pt + 6))   # bold label width estimate + gap
            for cat, labels in spec.skills:
                p = doc.add_paragraph()
                pf = p.paragraph_format
                pf.space_after, pf.left_indent, pf.first_line_indent = Pt(2), col, -col
                pf.tab_stops.add_tab_stop(col)
                _run(p, cat + loc["colon"], bold=True)
                _run(p, "\t" + SEP.join(labels))
            continue
        for n, it in enumerate(spec.items[section]):
            p = doc.add_paragraph()                       # title — organisation ......... dates
            p.paragraph_format.space_before = GAP_ENTRY if n else Pt(0)
            p.paragraph_format.keep_with_next = True
            p.paragraph_format.tab_stops.add_tab_stop(text_width, WD_TAB_ALIGNMENT.RIGHT)
            _run(p, it.title, bold=True)
            if it.org:
                _run(p, DASH + it.org)
            _run(p, "\t" + it.dates, size=small, color=MUTED)
            if it.subline:
                s = doc.add_paragraph(it.subline)
                s.paragraph_format.keep_with_next = bool(it.detail)
            if it.detail:
                d = doc.add_paragraph()
                _run(d, it.detail, size=small, color=MUTED)
                d.paragraph_format.space_after = GAP_LINE
                d.paragraph_format.keep_with_next = bool(it.bullets)
            for _, text, _ in it.bullets:
                b = doc.add_paragraph(text, style="List Bullet")
                b.paragraph_format.left_indent = Cm(0.55)
                b.paragraph_format.first_line_indent = Cm(-0.4)
    path.parent.mkdir(exist_ok=True)
    doc.save(path)
    return path


# ------------------------------------------------------------------- checks ----

_WORD = None


def _word():
    """A private, hidden Word instance (DispatchEx), never the user's open Word session:
    attaching to the user's Word fails whenever they have a CV open or a dialog showing."""
    global _WORD
    if _WORD is None:
        import atexit

        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        _WORD = win32com.client.DispatchEx("Word.Application")
        _WORD.Visible = False
        _WORD.DisplayAlerts = 0
        atexit.register(lambda: _WORD.Quit())
    return _WORD


def page_count(docx_path: Path) -> int:
    """docx -> PDF (next to the docx) with the private Word instance; returns the page count."""
    from pypdf import PdfReader
    pdf = docx_path.with_suffix(".pdf")
    doc = _word().Documents.Open(str(docx_path.resolve()), ReadOnly=True,
                                 AddToRecentFiles=False, Visible=False)
    try:
        doc.ExportAsFixedFormat(str(pdf.resolve()), 17)  # 17 = wdExportFormatPDF
    finally:
        doc.Close(0)
    return len(PdfReader(str(pdf)).pages)


def fit_one_page(spec: CVSpec, path: Path) -> CVSpec:
    """Largest body size of BODY_SIZES that fits one page. If even the smallest overflows, drop
    the lowest-scoring bullet (never the last of an entry), then the course lines of the oldest
    degree, until it fits."""
    for size in BODY_SIZES:
        render_docx(spec, path, size)
        if page_count(path) == 1:
            return spec
    while page_count(path) > 1:
        candidates = [(b[2], it, b) for sec in (H_PROJETS, H_EXPERIENCE)
                      for it in spec.items.get(sec, []) if len(it.bullets) > 1 for b in it.bullets]
        if candidates:
            _, it, b = min(candidates, key=lambda c: c[0])
            it.bullets.remove(b)
            spec.dropped.append(f"bullet {b[0]}: {b[1]}")
        else:
            with_courses = [it for it in spec.items.get(H_FORMATION, []) if it.detail]
            if not with_courses:
                raise SystemExit(f"{path.name}: still > 1 page with nothing left to drop")
            it = with_courses[-1]
            spec.dropped.append(f"courses of {it.id}")
            it.detail = None
        render_docx(spec, path, BODY_SIZES[-1])
    return spec


def allowed_lines(facts: dict) -> set[str]:
    loc = locale(facts)
    ok = set(loc["headings"].values()) | {facts["contact"]["name"], contact_line(facts["contact"]),
                                          headline(facts)}
    for f in facts["formation"]:
        ok |= {f"{f['degree']}\t{f['dates']}", f["institution"],
               f"{loc['courses']}{loc['colon']} {f.get('courses')}"}
    for key in ("experiences", "projets"):
        for e in facts[key]:
            ok.add(f"{e['role']}{DASH}{e['organisation']}\t{e['dates']}")
            if e.get("tools"):
                ok.add(f"{loc['tools']}{loc['colon']} {e['tools']}")
            for b in e["bullets"]:
                ok.add(b["text"])
                ok |= set((b.get("variants") or {}).values())
    return ok


def _skill_label_ok(cat: str, label: str, facts: dict, tiers: dict) -> bool:
    """`cat` is the displayed category name."""
    loc = locale(facts)
    if cat == loc["languages"]:
        return label in language_labels(facts)
    key = next((c for c in facts["categories"] if category_name(facts, c) == cat), None)
    for skill, default in facts["categories"].get(key, {}).items():
        names = {default} | {a["label"] for a in facts.get("aliases", {}).get(skill, [])}
        if tiers.get(skill) == 1 and key == ADVANCED_CATEGORY:
            names = {f"{n} ({loc['advanced']})" for n in names}
        if label in names and tiers.get(skill) in (1, 2):
            return True
    return False


def check_honesty(path: Path, facts: dict, tiers: dict) -> None:
    """Raise if any paragraph of the .docx is not traceable to the fact bank / profile tiers."""
    ok, loc = allowed_lines(facts), locale(facts)
    cats = {category_name(facts, c) for c in facts["categories"]} | {loc["languages"]}
    bad = []
    for p in Document(path).paragraphs:
        t = p.text
        if not t.strip() or t in ok:
            continue
        m = re.match(rf"^(.+?){re.escape(loc['colon'])}\t(.+)$", t)
        if m and m.group(1) in cats:
            wrong = [lbl for lbl in m.group(2).split(SEP)
                     if not _skill_label_ok(m.group(1), lbl, facts, tiers)]
            if not wrong:
                continue
            bad.append(f"{t!r} (unknown/unauthorised skills: {wrong})")
        else:
            bad.append(repr(t))
    if bad:
        raise AssertionError(f"{path.name}: lines not traceable to the fact bank:\n  " + "\n  ".join(bad))


def check_ats(path: Path, lang: str = "fr") -> list[str]:
    """Structural ATS checks; returns a list of problems (empty = OK)."""
    doc = Document(path)
    problems = []
    if doc.tables:
        problems.append("contains tables")
    if doc.inline_shapes:
        problems.append("contains images")
    body = doc.element.body.xml
    if "<w:txbxContent" in body or "<w:drawing" in body or "<w:pict" in body:
        problems.append("contains text boxes or drawings")
    cols = doc.sections[0]._sectPr.find(qn("w:cols"))
    if cols is not None and int(cols.get(qn("w:num"), "1")) > 1:
        problems.append("multi-column layout")
    if len(doc.sections) != 1:
        problems.append("more than one section")
    for s in doc.sections:
        if any(p.text.strip() for p in s.header.paragraphs + s.footer.paragraphs):
            problems.append("text in header/footer")
    heads = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    if not heads or set(heads) - set(LOCALES[lang]["headings"].values()):
        problems.append(f"unexpected headings: {heads}")
    return problems


def finalize(spec: CVSpec, path: Path, facts: dict, tiers: dict) -> CVSpec:
    """Fit + check in cvs/.build/ (the user may have cvs/ files open in Word), then copy to `path`.
    Not the system Temp folder: Word opens files from there in Protected View."""
    work = OUT_DIR / ".build" / path.name
    work.parent.mkdir(parents=True, exist_ok=True)
    fit_one_page(spec, work)
    check_honesty(work, facts, tiers)
    problems = check_ats(work, spec.lang)
    if problems:
        raise AssertionError(f"{path.name}: ATS problems: {problems}")
    for src in (work, work.with_suffix(".pdf")):
        dst = path.with_suffix(src.suffix)
        try:
            shutil.copyfile(src, dst)
        except PermissionError:
            raise SystemExit(f"{dst} is open in another program (Word?): close it and re-run.")
    return spec


def report(spec: CVSpec, path: Path) -> None:
    print(f"✓ {path}  (1 page, honesty + ATS checks passed)")
    for s in spec.vocab_swaps:
        print(f"    vocabulary: {s}")
    for d in spec.dropped:
        print(f"    dropped to fit one page: {d}")
