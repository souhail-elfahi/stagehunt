"""Internship finder for data roles (search terms and locations from config.yaml) using python-jobspy.

Freshness strategy: search the last 7 days first; if that yields too few
offers, widen the window to 30 days. Results are sorted newest first.
"""

import logging
import re
import time
from datetime import date

import pandas as pd
from jobspy import scrape_jobs

from config import SEARCH

# All from config.yaml (search:). Narrow terms are rarer wording: fewer LinkedIn results and no
# priority-area search. Locations map to LinkedIn results per term; LinkedIn costs 1 request per
# 10 results, Indeed 1 per search. Keep the request budget when changing them (request_count()).
SEARCH_TERMS = list(SEARCH["terms"])
NARROW_TERMS = set(SEARCH["narrow_terms"])
LOCATIONS = dict(SEARCH["cli"]["locations"])
NARROW_LOCATIONS = dict(SEARCH["cli"]["narrow_locations"])
INDEED_RESULTS = SEARCH["indeed_results"]
COUNTRY = SEARCH["country"]          # Indeed's country site
PAUSE_SECONDS = SEARCH["pause_seconds"]
WINDOWS_HOURS = [7 * 24, 30 * 24]  # try 7 days first, then 30 days
MIN_OFFERS = 30                     # widen the window if fewer than this

INTERN_PATTERN = r"\b(?:stage|stagiaire|intern|internship|trainee)\b"
# Title must look like a data role (drops off-topic matches like "assistant évènementiel").
RELEVANCE_PATTERN = (r"data|donn[ée]es|analy|scien|[ée]conom|statisti|machine learning|"
                     r"\bml\b|\bia\b|\bai\b|business intelligence|\bbi\b|quant")


class _ErrorCollector(logging.Handler):
    """jobspy logs scraper failures (e.g. LinkedIn 429) instead of raising; capture them."""

    def __init__(self, sink: list):
        super().__init__(level=logging.ERROR)
        self.sink = sink
        self.context = ""

    def emit(self, record):
        self.sink.append(f"{self.context}: {record.name}: {record.getMessage()}")
# search_location: which search found the offer (the first one wins when deduplicating,
# and the nationwide search runs first), used to keep the priority area's own search out of shares.
KEEP_COLUMNS = ["title", "company", "location", "date_posted", "job_url", "site", "search_location"]
# Kept in memory for fit_score (description, jobspy pay fields) but not written to the CSVs.
SCORING_COLUMNS = ["description", "min_amount", "max_amount", "interval", "currency"]


def build_query(term: str) -> str:
    # Make sure every query is internship-oriented.
    return term if "stage" in term.lower() else f"stage {term}"


def _norm(text) -> str:
    return "" if text is None or pd.isna(text) else " ".join(str(text).lower().split())


def dedup_key(title, company, location) -> str:
    """Cross-site duplicate key. Location is reduced to the city because the sites
    disagree on the rest ("Paris, A8, France" vs "Paris, Île-de-France, France")."""
    city = _norm(location).split(",")[0].strip()
    return f"{_norm(title)}|{_norm(company)}|{city}"


def search_plan(locations: dict[str, int], narrow: dict[str, int]) -> list[tuple[str, str, int]]:
    """(location, term, LinkedIn results) in run order. Locations keep their order, nationwide
    first, so it wins attribution on dedup. Broad terms use `locations`, NARROW_TERMS `narrow`."""
    plan = []
    for loc in dict.fromkeys([*locations, *narrow]):
        for term in SEARCH_TERMS:
            counts = narrow if term in NARROW_TERMS else locations
            if loc in counts:
                plan.append((loc, term, counts[loc]))
    return plan


def request_count(locations: dict[str, int], narrow: dict[str, int]) -> int:
    """HTTP requests of one run: 1 Indeed page + ceil(n / 10) LinkedIn pages per search."""
    return sum(1 + -(-n // 10) for _, _, n in search_plan(locations, narrow))


def run_searches(hours_old: int, errors: list, collector: _ErrorCollector,
                 locations: dict[str, int] | None = None, narrow: dict[str, int] | None = None,
                 outcomes: list | None = None, linkedin_gate=None) -> pd.DataFrame:
    """One scrape_jobs call per site, so Indeed can take a full page (INDEED_RESULTS) while
    LinkedIn gets the planned count. Every call is appended to `outcomes` as
    {term, location, site, wanted, got, skipped}: got == wanted means the cap was reached (more
    offers probably exist), got is None when the call raised. With a linkedin_gate
    (linkedin_access.LinkedInGate), a 429 closes it and the remaining LinkedIn searches are
    skipped (skipped=True)."""
    locations = LOCATIONS if locations is None else locations
    narrow = NARROW_LOCATIONS if narrow is None else narrow
    frames = []
    for loc, term, linkedin_results in search_plan(locations, narrow):
        query = build_query(term)
        for site, wanted in (("indeed", INDEED_RESULTS), ("linkedin", linkedin_results)):
            collector.context = f"{query!r} @ {loc} [{site}] ({hours_old // 24}d)"
            outcome = {"term": term, "location": loc, "site": site, "wanted": wanted, "got": None,
                       "skipped": False}
            if outcomes is not None:
                outcomes.append(outcome)
            if site == "linkedin" and linkedin_gate is not None and not linkedin_gate.open:
                outcome["skipped"] = True
                continue
            errors_before = len(errors)
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=query,
                    location=loc,
                    country_indeed=COUNTRY,
                    results_wanted=wanted,
                    hours_old=hours_old,
                    verbose=0,
                )
                print(f"  [{hours_old // 24:>2}d] {query!r:<38} @ {loc:<14} {site:<8} -> {len(df)}/{wanted}")
                outcome["got"] = len(df)
                if not df.empty:
                    df["search_term"] = term
                    df["search_location"] = loc
                    frames.append(df)
            except Exception as exc:  # keep going if one search fails
                msg = f"{collector.context}: {type(exc).__name__}: {exc}"
                print(f"  ERROR {msg}")
                errors.append(msg)
            if (site == "linkedin" and linkedin_gate is not None
                    and any(re.search(r"\b429\b", e) for e in errors[errors_before:])):
                linkedin_gate.hit_429(collector.context)
            time.sleep(PAUSE_SECONDS)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def clean(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=KEEP_COLUMNS + ["age_days"])
    df = raw.copy()

    # Keep internship offers only (title or description mentions it).
    text = df["title"].fillna("") + " " + df.get("description", pd.Series("", index=df.index)).fillna("")
    df = df[text.str.contains(INTERN_PATTERN, case=False, regex=True)]
    df = df[df["title"].fillna("").str.contains(RELEVANCE_PATTERN, case=False, regex=True)]

    # Deduplicate: same URL, or same title+company+location across sites.
    df = df.drop_duplicates(subset="job_url")
    key = [dedup_key(t, c, l) for t, c, l in zip(df["title"], df["company"], df["location"])]
    df = df.loc[~pd.Series(key, index=df.index).duplicated()]

    df = df[[c for c in KEEP_COLUMNS + SCORING_COLUMNS if c in df.columns]].copy()
    df["date_posted"] = pd.to_datetime(df["date_posted"], errors="coerce")
    df["age_days"] = (pd.Timestamp(date.today()) - df["date_posted"]).dt.days

    # Newest first, undated at the bottom.
    df = df.sort_values("date_posted", ascending=False, na_position="last").reset_index(drop=True)
    df["date_posted"] = df["date_posted"].dt.strftime("%Y-%m-%d")
    return df


def main() -> None:
    errors: list[str] = []
    collector = _ErrorCollector(errors)
    logging.getLogger("jobspy").addHandler(collector)  # child loggers propagate here
    result = pd.DataFrame()
    for hours in WINDOWS_HOURS:
        print(f"Searching offers from the last {hours // 24} days...")
        result = clean(run_searches(hours, errors, collector))
        print(f"-> {len(result)} unique internship offers in the last {hours // 24} days\n")
        if len(result) >= MIN_OFFERS:
            break

    out = f"internships_{date.today().isoformat()}.csv"
    result = result.drop(columns=[c for c in SCORING_COLUMNS if c in result.columns])
    result.to_csv(out, index=False, encoding="utf-8-sig")  # utf-8-sig so Excel shows accents

    # ---- Summary ----
    print("=" * 70)
    print(f"Saved {len(result)} offers to {out}")
    if not result.empty:
        print(f"  < 7 days old : {(result['age_days'] <= 7).sum()}")
        print(f"  no date      : {result['age_days'].isna().sum()}")
        print(f"  by site      : {result['site'].value_counts().to_dict()}")
        print("\n10 most recent:")
        with pd.option_context("display.max_colwidth", 45, "display.width", 200):
            print(result.head(10)[["date_posted", "title", "company", "location", "site"]].to_string(index=False))
        print("\nTop companies:")
        print(result["company"].value_counts().head(10).to_string())
    print(f"\nErrors ({len(errors)}):")
    for e in errors:
        print("  -", e)


if __name__ == "__main__":
    main()
