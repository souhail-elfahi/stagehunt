"""Market analysis of internship offers vs profile.md -> market_report.md + charts/.

Inputs
  offers_structured.jsonl      one structured record per offer (role cluster, missions, skills...)
  offers_with_descriptions.csv full descriptions, used for the skill-dictionary cross-check
  profile.md                   skills table with tiers (1 = interview-ready, 2 = refresh, 3 = to learn)

Demand = % of unique offers (with a description) whose extraction lists the skill as
required or nice-to-have. Cross-site duplicates are removed by identical mission bullets.
"""

import json
import re
from collections import Counter
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from skills_dictionary import SKILLS, find_skills, parse_profile  # noqa: E402,F401 (parse_profile re-exported for cv_lib)

JSONL = "offers_structured.jsonl"
CSV = "offers_with_descriptions.csv"
PROFILE = "profile.md"
REPORT = "market_report.md"
CHARTS = Path("charts")

CLUSTERS = ["data analyst", "data scientist", "quant-economist", "AI-ML engineer", "other"]
REFRESH_MIN = 20   # tier-2 skills asked in >= this % of a cluster's offers -> refresh
LEARN_MIN = 15     # missing / tier-3 skills asked in >= this % -> learn
LOW_DEMAND = 5     # skills you have that < this % of offers mention
TOP_N = 15

# Reference palette (dataviz skill, light mode)
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SEQ = ["#fcfcfb", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
TIER_COLORS = {1: "#184f95", 2: "#3987e5", 3: "#86b6ef", None: "#b9b8b1"}
TIER_LABELS = {1: "Tier 1 · interview-ready", 2: "Tier 2 · needs a refresh",
               3: "Tier 3 · to learn", None: "Not in profile"}

MISSION_THEMES = {
    "Dashboards & reporting": r"dashboard|report|kpi|indicator|visuali",
    "Data cleaning, quality & structuring": r"clean|quality|consolidat|structur|prepar|document",
    "ML modelling": r"machine learning|\bml\b|train|predict|classif|recommendation (?:algorithm|system)|"
                    r"product recommendation|feature",
    "GenAI / LLM / agents": r"llm|genai|generative|\brag\b|agent|gpt|nlp|text analytics",
    "Forecasting & time series": r"forecast|time series|price",
    "Statistical / econometric analysis": r"statistic|econometr|hypothesis|regression|causal|quantitative",
    "Scoring, risk & fraud": r"scor|risk|credit|fraud|backtest|stress",
    "Customer & product analytics": r"segment|customer|client|user|player|behaviour|journey|crm|campaign",
    "Optimisation & simulation": r"optimi[sz]|simulat|monte carlo|synthetic",
    "Pipelines, databases & data models": r"pipeline|etl|ingest|database|data model|datamart|datalake|data-lake|sql",
    "Business recommendations & stakeholders": r"recommendations|stakeholder|present|advise|management|decision",
    "Experiments & A/B tests": r"a/b|experiment",
}


# ------------------------------------------------------------------ loading ----

def load_offers() -> tuple[pd.DataFrame, pd.DataFrame, int]:
    rows = [json.loads(line) for line in open(JSONL, encoding="utf-8") if line.strip()]
    df = pd.DataFrame(rows)
    desc = pd.read_csv(CSV, encoding="utf-8-sig").set_index("job_url")["description"]
    df["text"] = df["title"] + "\n" + df["job_url"].map(desc).fillna("")
    title_only = df[df["source"] == "title_only"]
    full = df[df["source"] == "description"]
    dup = full["missions"].map(tuple).duplicated()
    return full[~dup].reset_index(drop=True), title_only, int(dup.sum())


# ------------------------------------------------------------------ metrics ----

def skill_table(sub: pd.DataFrame, extra: list[str] | None = None) -> pd.DataFrame:
    """Demand per skill; `extra` (profile skills) are always listed, even at 0%."""
    n = len(sub)
    req, anyc, dic = Counter(), Counter(), Counter()
    for _, r in sub.iterrows():
        req.update(set(r["required_skills"]))
        anyc.update(set(r["required_skills"]) | set(r["nice_to_have"]))
        dic.update(find_skills(r["text"]))
    skills = set(anyc) | set(SKILLS) | set(extra or [])
    t = pd.DataFrame({"skill": sorted(skills)})
    t["demand"] = t["skill"].map(lambda s: 100 * anyc[s] / n)
    t["required"] = t["skill"].map(lambda s: 100 * req[s] / n)
    # NaN = no dictionary pattern for this skill (shown as "—"), not "never mentioned"
    t["dictionary"] = t["skill"].map(lambda s: 100 * dic[s] / n if s in SKILLS else float("nan"))
    return t.sort_values(["demand", "dictionary"], ascending=False).reset_index(drop=True)


def mission_themes(sub: pd.DataFrame) -> pd.DataFrame:
    out = []
    for theme, pat in MISSION_THEMES.items():
        rx = re.compile(pat, re.IGNORECASE)
        hits = [(r["company"], m) for _, r in sub.iterrows() for m in r["missions"] if rx.search(m)]
        offers = {c for c, _ in hits}
        n_offers = sum(any(rx.search(m) for m in r["missions"]) for _, r in sub.iterrows())
        example = f"{hits[0][1]} ({hits[0][0]})" if hits else ""
        out.append((theme, 100 * n_offers / len(sub), example, len(offers)))
    t = pd.DataFrame(out, columns=["theme", "pct", "example", "companies"])
    return t.sort_values("pct", ascending=False).reset_index(drop=True)


def level_bucket(level: str) -> str:
    lv = level.lower()
    if "bac+3" in lv:
        return "Bac+3 accepted"
    if "m1" in lv or "bac+4" in lv:
        return "Bac+4 / M1 accepted"
    if "césure" in lv and "fin" not in lv:
        return "Gap year (césure)"
    if any(k in lv for k in ("m2", "bac+5", "fin d'études")):
        return "Bac+5 / M2 / final year"
    if "master" in lv:
        return "Master (year unspecified)"
    return "Unspecified"


def english_bucket(langs: list[str]) -> str:
    eng = " ".join(l for l in langs if l.lower().startswith("english")).lower()
    if not eng:
        return "Not stated"
    if re.search(r"fluent|c1|c2|bilingual|flawless|perfect|excellent|full professional|mandatory", eng):
        return "Fluent / C1+"
    if re.search(r"professional|b2|good|comfortable|operational|advanced", eng):
        return "Professional / B2"
    return "Plus / reading only"


def gap(table: pd.DataFrame, tiers: dict) -> dict[str, pd.DataFrame]:
    t = table.copy()
    t["tier"] = t["skill"].map(lambda s: tiers.get(s))
    has = t["skill"].isin([s for s, v in tiers.items() if v])
    return {
        "highlight": t[(t["tier"] == 1) & (t["demand"] > 0)],
        "refresh": t[(t["tier"] == 2) & (t["demand"] >= REFRESH_MIN)],
        "learn": t[(~has | (t["tier"] == 3)) & (t["demand"] >= LEARN_MIN)],
        "low": t[t["tier"].isin([1, 2]) & (t["demand"] < LOW_DEMAND)],
    }


# ------------------------------------------------------------------- charts ----

def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0)


def chart_clusters(counts: pd.Series, path: Path):
    fig, ax = plt.subplots(figsize=(7, 3.2), facecolor=SURFACE)
    _style(ax)
    counts = counts.sort_values()
    ax.barh(counts.index, counts.values, height=0.55, color="#2a78d6", edgecolor=SURFACE, linewidth=2)
    for y, v in enumerate(counts.values):
        ax.text(v + 0.4, y, str(v), va="center", color=INK, fontsize=10)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlabel("Unique offers", color=INK_2)
    ax.set_title("Offers per role cluster", loc="left", color=INK, fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_heatmap(by_cluster: dict[str, pd.DataFrame], overall: pd.DataFrame, ns: dict, path: Path):
    skills = overall.head(20)["skill"].tolist()
    cols = [c for c in CLUSTERS if c in by_cluster]
    mat = pd.DataFrame({c: by_cluster[c].set_index("skill")["demand"].reindex(skills).fillna(0)
                        for c in cols})
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("seq", SEQ)
    fig, ax = plt.subplots(figsize=(8, 8.5), facecolor=SURFACE)
    ax.imshow(mat.values, cmap=cmap, vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(cols)), [f"{c}\n(n={ns[c]})" for c in cols], color=INK_2, fontsize=9)
    ax.set_yticks(range(len(skills)), skills, color=INK, fontsize=9)
    ax.xaxis.tick_top()
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(which="both", length=0)
    ax.set_xticks([x - 0.5 for x in range(1, len(cols))], minor=True)
    ax.set_yticks([y - 0.5 for y in range(1, len(skills))], minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    for i in range(len(skills)):
        for j in range(len(cols)):
            v = mat.values[i, j]
            ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=8,
                    color="#ffffff" if v >= 55 else INK_2)
    ax.set_title("Top 20 skills: % of offers asking for them, by role cluster",
                 loc="left", color=INK, fontsize=12, fontweight="bold", pad=42)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_vs_profile(overall: pd.DataFrame, tiers: dict, path: Path):
    top = overall.head(20).iloc[::-1]
    tier_of = [tiers.get(s) for s in top["skill"]]
    fig, ax = plt.subplots(figsize=(8, 7), facecolor=SURFACE)
    _style(ax)
    ax.barh(top["skill"], top["demand"], height=0.6, color=[TIER_COLORS[t] for t in tier_of],
            edgecolor=SURFACE, linewidth=2)
    for y, v in enumerate(top["demand"]):
        ax.text(v + 0.8, y, f"{v:.0f}%", va="center", color=INK_2, fontsize=9)
    ax.tick_params(axis="y", colors=INK, labelsize=9)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlim(0, max(top["demand"]) * 1.12)
    ax.set_xlabel("% of offers (required or nice-to-have)", color=INK_2)
    handles = [plt.Rectangle((0, 0), 1, 1, color=TIER_COLORS[t]) for t in TIER_LABELS]
    ax.legend(handles, TIER_LABELS.values(), loc="lower right", frameon=False, fontsize=9,
              labelcolor=INK_2)
    ax.set_title("Most-demanded skills, coloured by your tier", loc="left", color=INK,
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------- report ----

def md_table(df: pd.DataFrame, cols: dict[str, str], fmt: dict | None = None) -> str:
    fmt = fmt or {}
    lines = ["| " + " | ".join(cols.values()) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        cells = [fmt[c](r[c]) if c in fmt else str(r[c]) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pct(v: float) -> str:
    return "—" if pd.isna(v) else f"{v:.0f}%"


def tier_str(v) -> str:
    return "—" if v is None or pd.isna(v) else str(int(v))


def skill_list(df: pd.DataFrame) -> str:
    if df.empty:
        return "_none_"
    return ", ".join(f"**{r.skill}** ({r.demand:.0f}%)" for r in df.itertuples())


def section(name: str, sub: pd.DataFrame, table: pd.DataFrame, tiers: dict) -> str:
    g = gap(table, tiers)
    t = table.head(TOP_N).copy()
    t["tier"] = t["skill"].map(lambda s: tiers.get(s))
    themes = mission_themes(sub).head(6)
    levels = sub["level"].map(level_bucket).value_counts()
    english = sub["languages"].map(english_bucket).value_counts()
    out = [f"## {name} ({len(sub)} offers)\n",
           "### Skills in demand\n",
           md_table(t, {"skill": "Skill", "demand": "Asked (req. or nice)", "required": "Required",
                        "dictionary": "Text mentions", "tier": "Your tier"},
                    {"demand": pct, "required": pct, "dictionary": pct, "tier": tier_str}),
           "\n### What you would actually do\n",
           md_table(themes, {"theme": "Mission theme", "pct": "% of offers", "example": "Example"},
                    {"pct": pct}),
           "\n### Gap analysis\n",
           f"- **Lead with (tier 1, in demand):** {skill_list(g['highlight'])}",
           f"- **Refresh before applying (tier 2, ≥{REFRESH_MIN}% of offers):** {skill_list(g['refresh'])}",
           f"- **Learn / missing (tier 3 or not in profile, ≥{LEARN_MIN}%):** {skill_list(g['learn'])}",
           "\n**Level asked:** " + ", ".join(f"{k} {v}" for k, v in levels.items()),
           "\n**English:** " + ", ".join(f"{k} {v}" for k, v in english.items()) + "\n"]
    return "\n".join(out)


def main() -> None:
    offers, title_only, n_dup = load_offers()
    tiers = parse_profile()
    CHARTS.mkdir(exist_ok=True)

    overall = skill_table(offers, list(tiers))
    by_cluster = {c: skill_table(offers[offers["role_cluster"] == c], list(tiers))
                  for c in CLUSTERS if (offers["role_cluster"] == c).any()}
    ns = offers["role_cluster"].value_counts().to_dict()

    chart_clusters(offers["role_cluster"].value_counts(), CHARTS / "clusters.png")
    chart_heatmap(by_cluster, overall, ns, CHARTS / "skills_by_cluster.png")
    chart_vs_profile(overall, tiers, CHARTS / "top_skills_vs_profile.png")

    g = gap(overall, tiers)
    unknown = overall[~overall["skill"].isin(tiers) & (overall["demand"] >= 3)]
    parts = [
        "# Internship market report\n",
        f"Source: {len(offers)} unique offers with a full description "
        f"({n_dup} cross-site duplicates removed, {len(title_only)} title-only offers excluded "
        f"from skill counts). Demand = % of offers listing the skill as required or nice-to-have "
        f"in the structured extraction; *Text mentions* = regex dictionary hits in the full "
        f"description, as a cross-check.\n",
        "![Offers per cluster](charts/clusters.png)\n",
        "## Overall gap analysis\n",
        "![Top skills vs profile](charts/top_skills_vs_profile.png)\n",
        f"- **Lead with (tier 1, in demand):** {skill_list(g['highlight'])}",
        f"- **Refresh before applying (tier 2, ≥{REFRESH_MIN}%):** {skill_list(g['refresh'])}",
        f"- **Learn / missing (tier 3 or not in profile, ≥{LEARN_MIN}%):** {skill_list(g['learn'])}",
        f"- **Rarely asked (<{LOW_DEMAND}%) — keep for targeted CVs, don't lead with them:** "
        f"{skill_list(g['low'])}",
        f"- **Asked in ≥3% of offers but not in profile.md:** {skill_list(unknown)}\n",
        "![Skills by cluster](charts/skills_by_cluster.png)\n",
    ]
    for c in CLUSTERS:
        sub = offers[offers["role_cluster"] == c]
        if len(sub):
            parts.append(section(c.capitalize() if c[0].islower() else c, sub, by_cluster[c], tiers))
    parts.append("## Title-only offers (no description fetched)\n")
    parts += [f"- {r.title} — {r.company} ({r.role_cluster})" for r in title_only.itertuples()]
    Path(REPORT).write_text("\n".join(parts) + "\n", encoding="utf-8")

    print(f"{len(offers)} unique offers, {n_dup} duplicates removed, {len(title_only)} title-only")
    print(offers["role_cluster"].value_counts().to_string())
    print(f"\nWrote {REPORT} and {len(list(CHARTS.glob('*.png')))} charts in {CHARTS}/")


if __name__ == "__main__":
    main()
