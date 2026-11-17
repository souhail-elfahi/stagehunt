"""Skill dictionary: detect tools/skills in offer text, French and English variants.

Canonical names match the `Skill` column of profile.md exactly, so the gap
analysis can join on them. Patterns are case-insensitive unless the skill is
listed in CASE_SENSITIVE (short acronyms that collide with ordinary words).

    python skills_dictionary.py   # top skills over offers_with_descriptions.csv
"""

import re
from pathlib import Path

import pandas as pd

PROFILE = Path(__file__).resolve().parent / "profile.md"

# canonical name -> regex variants (FR + EN)
SKILLS: dict[str, list[str]] = {
    # --- languages & libraries ---
    "SQL": [r"\bsql\b", r"\bpostgre(?:s|sql)\b", r"\bmysql\b", r"\bt-?sql\b", r"\bpl/?sql\b",
            r"requêtes? sql"],
    "Python": [r"\bpython\b"],
    "pandas": [r"\bpandas\b"],
    "NumPy": [r"\bnumpy\b"],
    "statsmodels": [r"\bstatsmodels\b"],
    "scikit-learn": [r"\bscikit[- ]?learn\b", r"\bsklearn\b"],
    # R: bare "R" only in a tech-list context, to avoid "R&D", "R." initials, etc.
    "R": [r"\bR\s*(?:/|,|et|and|ou|or)\s*(?:python|sql|sas|stata)\b",
          r"\b(?:python|sql|sas|stata|vba)\s*(?:/|,|et|and|ou|or)\s*R\b(?![&'])",
          r"\b(?:langage|language|logiciel|software)s?\s+R\b", r"\bRStudio\b", r"\bR\s+Shiny\b",
          r"\bR\s*\((?:tidyverse|dplyr|ggplot)", r"\bR\s+markdown\b",
          r"(?<=/)\s?R\b(?![&'])", r"\bR\s?/\s?(?!D\b)\w"],  # "C/R", "R/Python" (not "R/D")
    "tidyverse": [r"\btidyverse\b", r"\bdplyr\b", r"\btidyr\b"],
    "ggplot2": [r"\bggplot2?\b"],
    "Shiny": [r"\bshiny\b"],
    # SAS: only in a tool context, never bare (it's also a company legal form: "Acme SAS").
    "SAS": [r"\bSAS\s*(?:/|,|et|and|ou|or)\s*(?:R\b|python|sql|spss|stata|excel)",
            r"\b(?:R|python|sql|spss|stata|excel|vba)\s*(?:/|,|et|and|ou|or)\s*SAS\b",
            r"\b(?:logiciels?|langages?|software|sous|maîtrise de|connaissances? (?:de|en)|programmation)\s+SAS\b",
            r"\bSAS\s+(?:Enterprise|Guide|Viya|Base|Studio)\b"],
    "SPSS": [r"\bspss\b"],
    "Power BI": [r"\bpower ?bi\b", r"\bpowerbi\b", r"\bdax\b"],
    "Excel": [r"\bexcel\b", r"tableurs?\b", r"\bspreadsheets?\b"],
    "VBA": [r"\bvba\b", r"\bmacros? excel\b"],
    "Tableau": [r"\btableau (?:software|desktop|server|cloud)\b", r"\btableau(?=\s*[,/)]|\s+(?:ou|or|et|and)\s+(?:power ?bi|qlik|looker))",
                r"(?:power ?bi|qlik|looker)\s*(?:/|,|ou|or|et|and)\s*tableau\b"],
    "Looker": [r"\blooker(?: studio)?\b", r"\bdata studio\b"],
    "GAMS": [r"\bGAMS\b"],
    "Git": [r"\bgit\b", r"\bgithub\b", r"\bgitlab\b"],
    # --- methods ---
    "Statistics": [r"\bstatisti\w*", r"\béconométri\w*", r"\beconometric\w*"],
    "Endogeneity": [r"endog[ée]n\w*", r"variables? instrumentales?", r"instrumental variables?"],
    "Linear regression": [r"régressions? linéaires?", r"linear regressions?"],
    "Logistic regression": [r"régressions? logistiques?", r"logistic regressions?"],
    "Scoring": [r"\bscoring\b", r"\bcredit scor\w*", r"modèles? de score"],
    "RFM": [r"\bRFM\b"],
    "K-Means": [r"\bk-?means\b", r"\bk-?moyennes\b"],
    "HCA": [r"\bCAH\b", r"classification ascendante hiérarchique", r"hierarchical clustering"],
    "ARIMA": [r"\bs?arima\w*\b"],
    "PCA": [r"\bACP\b", r"\bPCA\b", r"analyse en composantes principales", r"principal component"],
    "Sampling design": [r"échantillonnage", r"\bsampling\b", r"plans? de sondage", r"\bsondages?\b"],
    "Random Forest": [r"random forests?", r"forêts? aléatoires?"],
    "XGBoost": [r"\bxgboost\b", r"\blightgbm\b", r"\bcatboost\b", r"gradient boost\w*"],
    "Monte Carlo simulation": [r"monte[- ]carlo"],
    "Ridge/LASSO": [r"\blasso\b", r"\bridge regression", r"régression ridge", r"elastic ?net"],
    "Machine learning": [r"machine learning", r"apprentissage automatique", r"\bML\b"],
    "Deep learning": [r"deep learning", r"apprentissage profond", r"réseaux? de neurones",
                      r"neural networks?", r"\bpytorch\b", r"\btensorflow\b", r"\bkeras\b"],
    "NLP/LLM": [r"\bNLP\b", r"\bLLMs?\b", r"traitement (?:automatique )?du langage", r"natural language",
                r"\bGenAI\b", r"IA générative", r"generative AI", r"\bRAG\b", r"\bGPT\b"],
    "Time series": [r"séries? temporelles?", r"time[- ]series", r"\bforecasting\b", r"prévisions?\b"],
    "A/B testing": [r"\bA/B\s*test\w*", r"\bAB test\w*", r"tests? A/B"],
    "Clustering/segmentation": [r"\bclustering\b", r"segmentation"],
    "Data visualisation": [r"data ?vi[sz]\w*", r"visuali[sz]ations?", r"tableaux? de bord", r"dashboards?"],
    # --- data engineering / platforms ---
    "ETL/data pipelines": [r"\bETL\b", r"\bELT\b", r"pipelines? de données", r"data pipelines?"],
    "dbt": [r"\bdbt\b"],
    "Spark": [r"\bspark\b", r"\bpyspark\b", r"\bdatabricks\b"],
    "Hadoop": [r"\bhadoop\b", r"\bhdfs\b", r"\bhive\b"],
    "Airflow": [r"\bairflow\b"],
    "Cloud (AWS/GCP/Azure)": [r"\baws\b", r"\bazure\b", r"\bgcp\b", r"google cloud", r"\bcloud\b"],
    "Snowflake/BigQuery": [r"\bsnowflake\b", r"\bbig ?query\b", r"\bredshift\b"],
    "Qlik": [r"\bqlik\w*\b"],
    "Stata": [r"\bstata\b"],
    "MATLAB": [r"\bmatlab\b"],
}

CASE_SENSITIVE = {"R", "SAS", "RFM", "HCA", "PCA", "GAMS", "Machine learning", "NLP/LLM", "ETL/data pipelines"}

# Per-pattern compile: within a case-sensitive skill, lowercase long phrases still need IGNORECASE.
def _compile(skill: str, pat: str) -> re.Pattern:
    is_acronym_pattern = skill in CASE_SENSITIVE and not re.search(r"[a-z]{4,}", pat.replace(r"\b", ""))
    return re.compile(pat, 0 if is_acronym_pattern else re.IGNORECASE)


_COMPILED = {s: [_compile(s, p) for p in pats] for s, pats in SKILLS.items()}

# English level requirement, ordered strongest first; first match wins.
ENGLISH_LEVELS: list[tuple[str, str]] = [
    ("bilingual/C2", r"anglais\s+(?:bilingue|natif)|bilingual\s+(?:in\s+)?english|english\s*\(?(?:bilingual|native)|\bC2\b|bilingue\s+(?:français[- /]anglais|anglais)|native english"),
    ("fluent/C1", r"anglais\s+(?:courant|fluide|parfait|excellent)|fluent\s+(?:in\s+)?english|english\s*[:(]?\s*fluent|fluency in english|maîtrise (?:parfaite |courante )?de l'anglais|excellent english|\bC1\b|toeic\s*(?:>|≥|de|min\w*)?\s*9\d\d"),
    ("professional/B2", r"anglais\s+(?:professionnel|opérationnel|technique|lu,? écrit)|professional (?:working )?english|good (?:level of |command of )?english|bon(?:ne)? (?:niveau|maîtrise) (?:d'|en )anglais|\bB2\b|toeic\s*(?:>|≥|de|min\w*)?\s*[78]\d\d"),
    ("mentioned", r"\banglais\b|\benglish\b"),
]
_ENGLISH = [(lvl, re.compile(p, re.IGNORECASE)) for lvl, p in ENGLISH_LEVELS]


def find_skills(text: str | None) -> set[str]:
    if not isinstance(text, str) or not text:
        return set()
    return {s for s, pats in _COMPILED.items() if any(p.search(text) for p in pats)}


_EN_WORDS = re.compile(r"(?:the|and|with|you|will|your|of|to)", re.IGNORECASE)
_FR_WORDS = re.compile(r"(?:le|la|les|et|avec|vous|des|du|une)", re.IGNORECASE)


def english_level(text: str | None) -> str:
    if not isinstance(text, str) or not text:
        return "none"
    for lvl, pat in _ENGLISH:
        if pat.search(text):
            return lvl
    if len(_EN_WORDS.findall(text)) > 2 * len(_FR_WORDS.findall(text)):
        return "implicit (offer in English)"
    return "none"


def parse_profile(path: str | Path = PROFILE) -> dict[str, int | None]:
    """Skill -> tier (None when blank) from every table under '## Skills' of profile.md.
    Lives here (no matplotlib) because the scheduled hunt scores offers with it."""
    if not Path(path).exists():
        raise SystemExit(f"{path} not found: copy profile.example.md to {path} and fill it in.")
    text = Path(path).read_text(encoding="utf-8")
    skills_part = text.split("## Skills", 1)[1]
    tiers = {}
    for name, tier in re.findall(r"^\|\s*([^|]+?)\s*\|\s*([123]?)\s*\|", skills_part, re.M):
        if name == "Skill" or set(name) <= set("-: "):
            continue
        tiers[name] = int(tier) if tier else None
    return tiers


def count_skills(texts: pd.Series) -> pd.DataFrame:
    """Number and % of offers (non-empty texts) mentioning each skill."""
    texts = texts.dropna()
    texts = texts[texts.str.strip() != ""]
    n = len(texts)
    counts = pd.Series(0, index=list(SKILLS))
    for t in texts:
        for s in find_skills(t):
            counts[s] += 1
    out = counts.rename("n_offers").to_frame()
    out["pct"] = (100 * out["n_offers"] / max(n, 1)).round(1)
    return out.sort_values("n_offers", ascending=False).rename_axis("skill").reset_index()


if __name__ == "__main__":
    df = pd.read_csv("offers_with_descriptions.csv", encoding="utf-8-sig")
    text = df["title"].fillna("") + "\n" + df["description"].fillna("")
    has_desc = df["description"].notna()
    print(f"{has_desc.sum()} offers with a description (of {len(df)})\n")
    with pd.option_context("display.max_rows", 100):
        print(count_skills(text[has_desc]).to_string(index=False))
    print("\nEnglish level:")
    print(text[has_desc].map(english_level).value_counts().to_string())
