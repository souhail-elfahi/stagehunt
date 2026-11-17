"""Cluster CVs: same facts as the master CV, reordered by what each role cluster asks for.

Demand comes from the same computation as market_report.md (analyze_market.skill_table on the
deduplicated offers), so the ordering always matches the report.

    python build_cluster_cvs.py   ->  cvs/CV_data_analyst.docx, CV_quant_economist.docx, CV_data_scientist.docx
"""

from analyze_market import load_offers, skill_table
from cv_lib import (H_COMPETENCES, H_EXPERIENCE, H_FORMATION, H_PROJETS, OUT_DIR, build_spec,
                    finalize, load_facts, load_tiers, report)

CLUSTERS = ["data analyst", "quant-economist", "data scientist"]
# Formation first for quant roles (degrees in econometrics are the main signal);
# practical work first for analyst/scientist roles.
FORMATION_FIRST = {"quant-economist"}


def cluster_weights(offers, cluster: str) -> dict[str, float]:
    t = skill_table(offers[offers["role_cluster"] == cluster])
    return dict(zip(t["skill"], t["demand"]))


def section_order(cluster: str, spec) -> list[str]:
    best = lambda s: max((i.score for i in spec.items.get(s, [])), default=0)  # noqa: E731
    practice = sorted([H_EXPERIENCE, H_PROJETS], key=lambda s: -best(s))
    if cluster in FORMATION_FIRST:
        return [H_FORMATION, *practice, H_COMPETENCES]
    return [*practice, H_FORMATION, H_COMPETENCES]


def build(cluster: str, facts, tiers, offers) -> None:
    sub = offers[offers["role_cluster"] == cluster]
    spec = build_spec(facts, tiers, [], cluster_weights(offers, cluster), cluster,
                      sub["text"].tolist())
    spec.sections = section_order(cluster, spec)
    path = OUT_DIR / f"CV_{cluster.replace(' ', '_').replace('-', '_')}.docx"
    report(finalize(spec, path, facts, tiers), path)


def main() -> None:
    facts, tiers = load_facts(), load_tiers()
    offers, _, _ = load_offers()
    for c in CLUSTERS:
        build(c, facts, tiers, offers)


if __name__ == "__main__":
    main()
