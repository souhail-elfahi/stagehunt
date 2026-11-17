"""Fit score (0-100) of an internship offer for the user's profile, plus pay, duration, remark.

Score = Compétences + Type de poste + Lieu + Rémunération + Fraîcheur (weights below),
minus SHORT_PENALTY when the internship lasts under MIN_MONTHS. Without a description the
score uses the title only and is flagged unreliable (Score fiable = non).

In the sheet, Score is a formula: the base (everything except freshness) plus freshness
computed from the Âge column, so the score decays as the offer ages (score_formula()).

    python fit_score.py   # score the saved offers (offers_with_descriptions.csv), offline
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from config import FIT, PRIORITY
from regions import city_of, region_of
from skills_dictionary import find_skills, parse_profile

# ------------------------------------------------------------------ weights ----
# The user's weights and scores, from config.yaml (fit_score:, priority:).
W_SKILLS = FIT["skills"]      # x share of the offer's skills that are tier 1 or 2 in profile.md
# Smoothed share (held + 1) / (required + 2): an offer naming only 1-2 skills you hold doesn't get
# the full weight; with no skill detected it gives the neutral 1/2.
PRIOR_HELD, PRIOR_TOTAL = 1, 2
NEUTRAL_SKILL_SHARE = 0.5     # share used without a description (title-only scores)

ROLE_POINTS = dict(FIT["roles"])   # Type de poste; keys are the classifier's classes below

# Lieu: inside the priority metro area / elsewhere in its region / named regions / anywhere else.
PLACE_AREA = FIT["place"]["priority_area"]
PLACE_REGION = FIT["place"]["priority_region"]
PLACE_REGIONS = dict(FIT["place"].get("regions") or {})
PLACE_OTHER = FIT["place"]["other"]

# Rémunération (gratification, €/month). legal_min_hourly is the French legal minimum: UPDATE EACH JANUARY.
_PAY = FIT["pay"]
LEGAL_MIN_HOURLY = _PAY["legal_min_hourly"]
HOURS_PER_MONTH = _PAY["hours_per_month"]
LEGAL_MIN_MONTHLY = round(LEGAL_MIN_HOURLY * HOURS_PER_MONTH)
PAY_HIGH, PAY_MID = _PAY["high"], _PAY["mid"]
PAY_POINTS_HIGH, PAY_POINTS_MID, PAY_POINTS_LOW, PAY_POINTS_UNKNOWN, PAY_POINTS_LEGAL = (
    _PAY["points"][k] for k in ("high", "mid", "low", "unknown", "legal_minimum"))

FRESH_POINTS = [tuple(step) for step in FIT["freshness"]["steps"]]   # (max age in days, points); older = 0
FRESH_UNKNOWN = FIT["freshness"]["unknown"]                          # no publication date

MIN_MONTHS, SHORT_PENALTY = FIT["short_internship"]["min_months"], FIT["short_internship"]["penalty"]

RQTH_COMPANIES = ["talents handicap"]      # offers reserved for RQTH holders (Remarque)
COMPANY_CHECK = "Entreprise à vérifier"    # Remarque when the company is empty or is the title
RELIABLE_MIN_CHARS = 200                   # shorter "descriptions" are just snippets

# ------------------------------------------------------------- role classes ----
# Title signals weigh TITLE_WEIGHT; each distinct description signal weighs 1, so a description
# that is mostly about indicators and studies outweighs an "engineer" title (MBDA case).
TITLE_WEIGHT = 3
OTHER_BELOW = 2               # best class under this -> "autre"
ROLE_TITLE = {
    # "analyste" alone isn't enough: SOC / strategy / M&A analysts are not data roles.
    "analyste / études": r"data analy|analy\w* (?:de |des )?(?:donnees|data)|analy\w* (?:statisti|quanti|econom|"
                         r"marche|bi\b|pricing|risques?|credit|performance|reporting|business intelligence|"
                         r"decisionnel)|business intelligence|\bbi\b|charge\w* d.?etudes?|statisti|econom|"
                         r"actuari|reporting|pricing|\bquant|insight|market intelligence|"
                         r"etudes? (?:de marche|quantitatives?|economiques?)|biostat",
    "data scientist": r"data scien|science des donnees|scientifique des donnees|machine learning|"
                      r"apprentissage automatique|deep learning|\bnlp\b|vision par ordinateur|computer vision",
    "ingenieur": r"engineer|ingenieur|developpeu|developer|devops|mlops|dataops|architect|"
                 r"\b(?:ia|ai|llm|genai) (?:engineer|developer)|full ?stack|back ?end",
}
ROLE_DESC = {
    "analyste / études": [
        r"indicateurs?", r"\bkpis?\b", r"tableaux? de bord|dashboards?", r"reporting", r"etudes? statistiques?",
        r"enquetes?|sondages?|questionnaires?", r"analyse de marche|etudes? de marche", r"\bveille\b",
        r"intelligence (?:economique|marche|competitive)|market intelligence", r"econometr", r"benchmark",
        r"statistiques? descriptives?", r"extraction de donnees|extraire (?:les |des )?donnees|requetes? sql",
        r"power ?bi|tableau software|\btableau\b|looker|qlik", r"analyses? ad.?hoc", r"recommandations?",
        r"insights?", r"suivi de (?:la )?performance|analyse de (?:la )?performance", r"prevision|forecast",
        r"modeles? economiques?|politique economique|macroeconom|microeconom", r"actuari", r"pricing",
        r"controle de gestion|finance", r"echantillon|ponderation|redressement", r"visualisation",
    ],
    "data scientist": [
        r"machine learning|apprentissage automatique", r"modeles? (?:predictifs?|de prediction|de machine)",
        r"classification|clustering|segmentation", r"deep learning|reseaux? de neurones|neural",
        r"\bnlp\b|traitement (?:automatique )?du langage", r"scikit|xgboost|random forest|lightgbm|catboost",
        r"feature engineering|entrainer|entrainement", r"data science|science des donnees",
        r"computer vision|vision par ordinateur", r"series temporelles|time series", r"detection d.?anomalies",
    ],
    "ingenieur": [
        r"pipelines?", r"\betl\b|\belt\b", r"spark|hadoop|kafka|airflow|\bdbt\b|databricks|snowflake",
        r"data ?lake|data ?warehouse|entrepot de donnees|lakehouse", r"\b(?:aws|gcp|azure)\b|cloud",
        r"docker|kubernetes|terraform", r"ci/?cd|devops|mlops|dataops", r"\bapi\b|microservices?|back.?end",
        r"deploie|deploy|mise en production|industrialis", r"\bllms?\b|\brag\b|agents? (?:ia|ai|intelligents?)|genai|ia generative",
        r"developpement (?:d.?applications?|logiciel|web)|software engineering", r"infrastructure|architecture",
    ],
}
ROLE_OF_CLASS = {"analyste / études": "analyste / études", "data scientist": "data scientist",
                 "ingenieur": "ingénieur data / IA"}
# Finance jobs, not data / econometrics (the user's call): M&A, corporate finance and financial
# analyst titles are "autre" whatever the description says, unless the title is about risk,
# credit or quant work, which stays analyst.
FINANCE_TITLE = re.compile(r"\bm ?& ?a\b|fusions? (?:et|&) acquisitions?|mergers? (?:and|&) acquisitions?|"
                           r"corporate finance|finance d.entreprise|analyste? financ|financial analyst")
FINANCE_KEEP = re.compile(r"risque|\brisk|credit|\bquant")


def _norm(text) -> str:
    s = "" if text is None or (not isinstance(text, str) and pd.isna(text)) else str(text)
    s = unicodedata.normalize("NFKD", s.replace("’", "'"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\s\-]+", " ", s).strip()


def _plain(text) -> str:
    """Lowercase, no accents, punctuation kept (ranges like "4-6 mois" need the hyphen)."""
    s = "" if text is None or (not isinstance(text, str) and pd.isna(text)) else str(text)
    s = unicodedata.normalize("NFKD", s.replace("’", "'"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s).strip()


# The priority metro area's towns (config.yaml priority.towns), normalized like the locations.
PRIORITY_TOWNS = {_norm(t) for t in PRIORITY["towns"]}

_ROLE_TITLE = {k: re.compile(p) for k, p in ROLE_TITLE.items()}
_ROLE_DESC = {k: [re.compile(p) for p in ps] for k, ps in ROLE_DESC.items()}


def classify_role(title, description=None) -> str:
    """Role class from the title and, when present, the description (which weighs more)."""
    t, d = _norm(title), _norm(description)
    if FINANCE_TITLE.search(t) and not FINANCE_KEEP.search(t):
        return "autre"
    scores = {}
    for cls in _ROLE_TITLE:
        s = TITLE_WEIGHT if _ROLE_TITLE[cls].search(t) else 0
        s += sum(1 for p in _ROLE_DESC[cls] if p.search(d))
        scores[cls] = s
    best = max(scores, key=lambda c: (scores[c], c == "analyste / études"))  # ties favour analyst
    return ROLE_OF_CLASS[best] if scores[best] >= OTHER_BELOW else "autre"


# --------------------------------------------------------------------- pay ----
_NUM = r"\d{1,2}[ .,  ]\d{3}(?![\d,.])|\d{3,4}|\d{1,2}[,.]\d{1,2}|\d{1,2}"
_EURO = r"(?:€|euros?\b|eur\b)"
_AMOUNT = re.compile(rf"(?P<a>{_NUM})\s*(?:{_EURO})?\s*(?:-|–|à|a|to)\s*(?P<b>{_NUM})\s*(?P<k>k)?\s*{_EURO}"
                     rf"|(?P<c>{_NUM})\s*(?P<k2>k)?\s*{_EURO}"
                     rf"|€\s?(?P<d>{_NUM})", re.I)        # "€1,300" (English style)
_PAY_CONTEXT = re.compile(r"gratification|remuneration|salaire|indemnit|stipend|salary|compensation|paid|"
                          r"gross|brut|net\b|"
                          r"mensuel|par mois|/ ?mois|per month|/ ?month|par heure|de l.heure|/ ?h\b|horaire", re.I)
_HOURLY = re.compile(r"^\s*(?:brut |net )?(?:/ ?h\b|/ ?heure|par heure|de l.heure|per hour|/ ?hour|horaire)", re.I)
_YEARLY = re.compile(r"^\s*(?:brut |net )?(?:/ ?an\b|par an|annuel|per year|/ ?year|a year)", re.I)
_LEGAL = re.compile(r"gratification (?:legale|minimale|minimum|reglementaire|conventionnelle)|"
                    r"minimum legal|montant legal|taux legal|selon (?:la |les )?(?:legislation|reglementation|"
                    r"dispositions? legales?|loi)|conformement a la (?:legislation|reglementation|loi)|"
                    r"en vigueur.{0,20}gratification|gratification.{0,40}en vigueur", re.I)


def _to_float(s: str) -> float:
    """'1 200', '1.200', '1,300' (thousands) -> 1200 / 1300; '4,50' -> 4.5."""
    s = re.sub(r"[ .,  ](?=\d{3}$)", "", s.strip()).replace(",", ".")
    return float(s)


def _monthly(amount: float, interval: str) -> float | None:
    factor = {"hourly": HOURS_PER_MONTH, "daily": 21.67, "weekly": 52 / 12, "monthly": 1, "yearly": 1 / 12}
    return amount * factor[interval] if interval in factor else None


def pay_from_text(text) -> tuple[float | None, bool]:
    """(€/month, legal_minimum) from a description: the first amount in a pay context."""
    raw = "" if text is None or (not isinstance(text, str) and pd.isna(text)) else str(text)
    plain = unicodedata.normalize("NFKD", raw)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    for m in _AMOUNT.finditer(plain):
        window = plain[max(0, m.start() - 90): m.end() + 40]
        if not _PAY_CONTEXT.search(window):
            continue
        # A range gives its lower bound; "Master 2 – 1500 €" is not a range 2..1500, so an
        # implausible lower bound falls back to the upper one.
        candidates = [m.group("a"), m.group("b")] if m.group("a") else [m.group("c") or m.group("d")]
        after = plain[m.end(): m.end() + 30]
        for num in candidates:
            try:
                value = _to_float(num)
            except ValueError:
                continue
            if m.group("k") or m.group("k2"):
                value *= 1000
            if _HOURLY.search(after) or (value < 30 and re.search(r"heure|/ ?h\b|horaire", window, re.I)):
                value = _monthly(value, "hourly")
            elif _YEARLY.search(after) or value >= 6000:
                value = _monthly(value, "yearly")
            if 300 <= value <= 4000:
                return round(value), value <= LEGAL_MIN_MONTHLY * 1.01
    if _LEGAL.search(_norm(raw)):
        return LEGAL_MIN_MONTHLY, True
    return None, False


def pay_of(offer: dict) -> tuple[float | None, bool]:
    """jobspy's compensation fields (lower bound, converted to monthly), else the description."""
    for key in ("min_amount", "max_amount"):
        v = offer.get(key)
        if v is not None and not pd.isna(v) and float(v) > 0:
            monthly = _monthly(float(v), str(offer.get("interval") or "").lower())
            if monthly and 300 <= monthly <= 4000:
                return round(monthly), monthly <= LEGAL_MIN_MONTHLY * 1.01
    return pay_from_text(offer.get("description"))


def pay_points(monthly: float | None, legal: bool) -> int:
    if monthly is None:
        return PAY_POINTS_UNKNOWN
    if legal or monthly <= LEGAL_MIN_MONTHLY * 1.01:
        return PAY_POINTS_LEGAL
    if monthly >= PAY_HIGH:
        return PAY_POINTS_HIGH
    return PAY_POINTS_MID if monthly >= PAY_MID else PAY_POINTS_LOW


# ---------------------------------------------------------------- duration ----
_WORDS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
          "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six_": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_N = r"\d{1,2}|" + "|".join(k for k in _WORDS if not k.endswith("_"))
_DURATION = re.compile(rf"\b(?P<a>{_N})\s*(?:(?:a|-|–|ou|to|or)\s*(?P<b>{_N})\s*)?(?P<u>mois|months?)\b")
_DUR_CONTEXT = re.compile(r"stage|stagiaire|duree|duration|internship|pendant|periode|"
                          r"contrat|mission|intern\b|\bfor (?:a|an)\b|\bof\b|\bpour\b")
_DUR_EXCLUDE_AFTER = re.compile(r"^\s*(?:d.experience|d.essai|minimum d.experience|of experience|probation|"
                                r"apres|after|plus tard|later|derniers|ago)")
_DUR_EXCLUDE_BEFORE = re.compile(r"(?:il y a|depuis|dans les|tous les|chaque|every|within|au cours des|"
                                 r"experience (?:de|d.au moins)|au moins|minimum)\s*$")


def _num(token: str) -> int:
    return int(token) if token.isdigit() else _WORDS.get(token, 0)


def duration_of(title, description=None) -> tuple[int | None, int | None, str]:
    """(min months, max months, label) from the title, else the description (pay-free context)."""
    for text, need_context in ((_plain(title), False), (_plain(description), True)):
        for m in _DURATION.finditer(text):
            a, b = _num(m.group("a")), _num(m.group("b")) if m.group("b") else None
            lo, hi = (a, b) if b else (a, a)
            if not (1 <= lo <= 12 and 1 <= hi <= 12 and lo <= hi):
                continue
            before, after = text[max(0, m.start() - 40): m.start()], text[m.end(): m.end() + 30]
            if _DUR_EXCLUDE_AFTER.search(after) or _DUR_EXCLUDE_BEFORE.search(before):
                continue
            if need_context and not _DUR_CONTEXT.search(before + " " + after):
                continue
            return lo, hi, f"{lo} mois" if lo == hi else f"{lo} à {hi} mois"
    return None, None, ""


# ------------------------------------------------------------------- score ----
_TIERS: dict[str, int | None] | None = None


def tiers() -> dict[str, int | None]:
    global _TIERS
    if _TIERS is None:
        try:
            _TIERS = parse_profile()
        except (SystemExit, OSError):  # no profile.md: skills get the neutral share
            _TIERS = {}
    return _TIERS


def company_suspect(company, title) -> bool:
    """No company, or the title sitting in the company cell (e.g. an accidental paste)."""
    c = _norm(company)
    return not c or c == "nan" or c == _norm(title)


def place_points(location) -> int:
    city = _norm(city_of(location)).replace("(metropole) ", "")
    if city in PRIORITY_TOWNS or _norm(location).startswith(_norm(PRIORITY["name"])):
        return PLACE_AREA
    region = region_of(location)
    if region == PRIORITY["region"]:
        return PLACE_REGION
    return PLACE_REGIONS.get(region, PLACE_OTHER)


def fresh_points(age_days) -> int:
    if age_days is None or pd.isna(age_days):
        return FRESH_UNKNOWN
    for max_age, points in FRESH_POINTS:
        if age_days <= max_age:
            return points
    return 0


@dataclass
class Fit:
    base: int                   # score without freshness (penalty included, may go below 0)
    reliable: bool              # scored with a real description
    role: str
    skills: list[str] = field(default_factory=list)       # detected in the description
    held: list[str] = field(default_factory=list)         # of which tier 1-2 in profile.md
    pay: float | None = None    # €/month
    pay_legal: bool = False
    months_min: int | None = None
    months_max: int | None = None
    duration: str = ""
    remark: str = ""
    parts: dict = field(default_factory=dict)

    def total(self, age_days) -> int:
        return max(0, min(100, self.base + fresh_points(age_days)))

    @property
    def short(self) -> bool:
        return self.months_max is not None and self.months_max < MIN_MONTHS


def _desc(offer: dict) -> str:
    d = offer.get("description")
    return "" if d is None or (not isinstance(d, str) and pd.isna(d)) else str(d)


def fit(offer: dict) -> Fit:
    """Score an offer (clean() record: title, company, location, description, pay fields...)."""
    desc = _desc(offer)
    reliable = len(desc.strip()) >= RELIABLE_MIN_CHARS
    text = desc if reliable else ""
    # Title-only offers get the neutral share: one skill word in a title would otherwise give 100%.
    skills = sorted(find_skills(f"{offer.get('title') or ''}\n{text}")) if reliable else []
    t = tiers()
    held = [s for s in skills if t.get(s) in (1, 2)]
    share = (len(held) + PRIOR_HELD) / (len(skills) + PRIOR_TOTAL) if reliable and t else NEUTRAL_SKILL_SHARE
    role = classify_role(offer.get("title"), text)
    # Pay and duration come with their own context words, so a short text (an email alert's
    # card: "6 mois · 1 200 €/mois") is enough for them, unlike skills and role.
    pay, legal = pay_of({**offer, "description": desc})
    lo, hi, label = duration_of(offer.get("title"), desc)
    parts = {
        "compétences": round(W_SKILLS * share),
        "poste": ROLE_POINTS[role],
        "lieu": place_points(offer.get("location")),
        "rémunération": pay_points(pay, legal),
        "durée": -SHORT_PENALTY if hi is not None and hi < MIN_MONTHS else 0,
    }
    company = _norm(offer.get("company"))
    remarks = ["Réservée RQTH"] if any(c in company for c in RQTH_COMPANIES) else []
    if company_suspect(offer.get("company"), offer.get("title")):
        remarks.append(COMPANY_CHECK)
    remark = " · ".join(remarks)
    return Fit(base=sum(parts.values()), reliable=reliable, role=role, skills=skills, held=held, pay=pay,
               pay_legal=legal, months_min=lo, months_max=hi, duration=label, remark=remark, parts=parts)


def score_formula(base: int, row: int, age_col: str = "F") -> str:
    """Sheet formula: base + freshness from the Âge cell, clamped to 0..100 (fr_FR: ';')."""
    cell = f"{age_col}{row}"
    fresh = "0"
    for max_age, points in reversed(FRESH_POINTS):
        fresh = f"IF({cell}<={max_age};{points};{fresh})"
    return f'=MAX(0;MIN(100;{base}+IF({cell}="";{FRESH_UNKNOWN};{fresh})))'


def annotate(offer: dict) -> dict:
    """Fields added to an offer for the sheet and the digest."""
    f = fit(offer)
    return {"score": f.total(offer.get("age_days")), "score_base": f.base,
            "score_fiable": "oui" if f.reliable else "non", "gratification": f.pay,
            "duree": f.duration, "short": f.short, "remarque": f.remark,
            "region": region_of(offer.get("location")), "role": f.role}


def main() -> None:
    df = pd.read_csv("offers_with_descriptions.csv", encoding="utf-8-sig")
    today = pd.Timestamp(date.today())
    df["age_days"] = (today - pd.to_datetime(df["date_posted"], errors="coerce")).dt.days
    rows = []
    for o in df.to_dict("records"):
        f = fit(o)
        rows.append({"score": f.total(o["age_days"]), "base": f.base, "fiable": f.reliable, "role": f.role,
                     "pay": f.pay, "durée": f.duration, **{k: v for k, v in f.parts.items()},
                     "title": str(o["title"])[:45], "company": str(o["company"])[:20]})
    out = pd.DataFrame(rows).sort_values("score", ascending=False)
    with pd.option_context("display.width", 220, "display.max_rows", 200):
        print(out.to_string(index=False))


if __name__ == "__main__":
    main()
