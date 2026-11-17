"""CV tailored to one offer + an honest keyword coverage report.

    python tailor_cv.py <job_url> [--lang en]
      -> cvs/CV_<company>_<slug>.docx (+ .pdf) and cvs/CV_<company>_<slug>_keywords.md
         (--lang en: file names end in _en, texts come from the translation cv_facts.en.yaml)

Same facts as every other CV: only order, bullet selection, the skills shortlist and synonym
labels change. Skills the offer asks for that are tier 3 / absent from profile.md are
reported, never added.

Skills shortlist: the list saved for this URL under `tailoring:` in the fact bank, otherwise
the MAX_SKILLS skills most relevant to the offer.
"""

import json
import re
import sys
from difflib import get_close_matches

import pandas as pd

from analyze_market import load_offers
from build_cluster_cvs import cluster_weights, section_order
from cv_lib import (H_COMPETENCES, H_FORMATION, LOCALES, OUT_DIR, build_spec, category_name,
                    finalize, load_facts, load_tiers, report)
from skills_dictionary import find_skills

JSONL, CSV = "offers_structured.jsonl", "offers_with_descriptions.csv"
MAX_SKILLS = 10
# clusters without their own CV config borrow the closest one
CONFIG_OF = {"AI-ML engineer": "data scientist", "other": "data analyst"}


def load_offer(url: str) -> tuple[dict, str]:
    rows = [json.loads(line) for line in open(JSONL, encoding="utf-8") if line.strip()]
    by_url = {r["job_url"]: r for r in rows}
    if url not in by_url:
        hint = get_close_matches(url, by_url, n=3, cutoff=0.5)
        companies = sorted({r["company"] for r in rows})
        raise SystemExit(f"Unknown job_url: {url}\nClosest URLs: {hint}\nCompanies: {companies}")
    desc = pd.read_csv(CSV, encoding="utf-8-sig").set_index("job_url")["description"]
    text = by_url[url]["title"] + "\n" + str(desc.get(url, "") or "")
    return by_url[url], text


def offer_keywords(offer: dict, text: str) -> dict[str, float]:
    """required = 2, nice-to-have = 1, only mentioned in the description = 0.5"""
    kw: dict[str, float] = {}
    for s in find_skills(text):
        kw[s] = 0.5
    for s in offer["nice_to_have"]:
        kw[s] = max(kw.get(s, 0), 1.0)
    for s in offer["required_skills"]:
        kw[s] = 2.0
    return kw


def skills_shortlist(url: str, facts: dict, tiers: dict, weights: dict) -> tuple[list[str], str]:
    """(canonical skills to show, where the list comes from)."""
    showable = [s for m in facts["categories"].values() for s in m if tiers.get(s) in (1, 2)]
    saved = (facts.get("tailoring") or {}).get(url, {}).get("skills")
    if saved:
        refused = [s for s in saved if s not in showable]
        if refused:
            print(f"    not shown (tier 3, blank or uncategorised in the bank): {refused}")
        return [s for s in saved if s in showable], "your saved list (fact bank, `tailoring:`)"
    ranked = sorted(showable, key=lambda s: (-weights.get(s, 0), tiers[s]))
    return ranked[:MAX_SKILLS], f"the {MAX_SKILLS} skills most relevant to the offer"


def slug(s: str) -> str:
    s = re.sub(r"[^\w]+", "-", s.lower(), flags=re.UNICODE).strip("-")
    return s[:50]


def coverage_report(offer, kw, spec, facts, tiers, shortlist_source) -> str:
    shown_bullets = {b[0]: b[1] for sec in spec.items.values() for it in sec for b in it.bullets}
    tags = {b["id"]: b["skills"] for k in ("experiences", "projets") for e in facts[k]
            for b in e["bullets"]}
    courses = {it.title: it.skills for it in spec.items.get(H_FORMATION, []) if it.detail}
    head = LOCALES[spec.lang]["headings"]
    dropped_ids = {d.split(":")[0].replace("bullet ", "") for d in spec.dropped if d.startswith("bullet")}
    cat_of = {s: category_name(facts, cat) for cat, m in facts["categories"].items() for s in m}
    weight_name = {2.0: "required", 1.0: "nice-to-have", 0.5: "mentioned"}

    covered, hidden, missing = [], [], []
    for s, w in sorted(kw.items(), key=lambda x: (-x[1], x[0])):
        tier = tiers.get(s)
        tag = f"**{s}** ({weight_name[w]})"
        if tier in (1, 2):
            where = []
            if s in spec.shown_skills:
                where.append(f"{head[H_COMPETENCES]} › {cat_of[s]}")
            where += [f"{head[H_FORMATION]} › courses ({deg})" for deg, sk in courses.items() if s in sk]
            where += [f"« {txt[:70]}… »" for bid, txt in shown_bullets.items() if s in tags.get(bid, [])]
            lost = [bid for bid in dropped_ids if s in tags.get(bid, [])]
            if where:
                covered.append(f"- ✅ {tag}, tier {tier}: " + "; ".join(where))
            else:
                hidden.append(f"- ⚠️ {tag}, tier {tier}: in profile but not on this CV"
                              + (f" (dropped bullets: {lost})" if lost else ""))
        else:
            why = "tier 3 (to learn)" if tier == 3 else "not in profile.md"
            missing.append(f"- ❌ {tag}: {why}")

    langs = "; ".join(offer["languages"]) or "not stated"
    skills_lines = [f"- {cat}: " + " · ".join(labels) for cat, labels in spec.skills]
    lines = [f"# Keyword coverage: {offer['title']} — {offer['company']}\n",
             f"Offer: {offer['job_url']}  \nCluster: {offer['role_cluster']}\n",
             "## Covered", *(covered or ["- none"]),
             "\n## In your profile but not shown", *(hidden or ["- none"]),
             "\n## Honestly missing (not added to the CV)", *(missing or ["- none"]),
             f"\n## Skills shown ({shortlist_source})", *skills_lines,
             "\n## Requirements vs profile",
             f"- Level asked: {offer['level']} — profile: {facts['formation'][0]['degree']}",
             f"- Languages asked: {langs} — profile: " + ", ".join(facts["langues"]["items"]),
             f"- Profile asked: {offer['profile']}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    args, lang = sys.argv[1:], "fr"
    if "--lang" in args:
        i = args.index("--lang")
        lang = "".join(args[i + 1:i + 2])
        del args[i:i + 2]
    if len(args) != 1 or lang not in LOCALES:
        raise SystemExit(__doc__)
    url = args[0]
    offer, text = load_offer(url)
    facts, tiers = load_facts(lang), load_tiers()
    offers, _, _ = load_offers()
    config = CONFIG_OF.get(offer["role_cluster"], offer["role_cluster"])

    kw = offer_keywords(offer, text)
    demand = cluster_weights(offers, config)
    # the offer dominates; cluster demand (0–1) only breaks ties
    weights = {s: kw.get(s, 0) * 10 + demand.get(s, 0) / 100 for s in set(kw) | set(demand)}
    shortlist, source = skills_shortlist(url, facts, tiers, weights)
    spec = build_spec(facts, tiers, [], weights, config, [text], priority=kw, only_skills=shortlist)
    spec.sections = section_order(config, spec)

    base = OUT_DIR / (f"CV_{slug(offer['company'])}_{slug(offer['title'])}"
                      + ("" if lang == "fr" else f"_{lang}"))
    path = base.with_suffix(".docx")
    report(finalize(spec, path, facts, tiers), path)
    md = base.parent / (base.name + "_keywords.md")
    md.write_text(coverage_report(offer, kw, spec, facts, tiers, source), encoding="utf-8")
    print(f"✓ {md}")
    print(md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
