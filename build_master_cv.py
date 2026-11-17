"""Master CV (French, ATS-friendly, 1 page) from the approved fact bank + profile.md tiers.

    python build_master_cv.py   ->  cvs/CV_master.docx (+ .pdf)
"""

from cv_lib import (H_COMPETENCES, H_EXPERIENCE, H_FORMATION, H_PROJETS, OUT_DIR, build_spec,
                    finalize, load_facts, load_tiers, report)


def main() -> None:
    facts, tiers = load_facts(), load_tiers()
    spec = build_spec(facts, tiers, [H_FORMATION, H_EXPERIENCE, H_PROJETS, H_COMPETENCES])
    path = OUT_DIR / "CV_master.docx"
    report(finalize(spec, path, facts, tiers), path)


if __name__ == "__main__":
    main()
