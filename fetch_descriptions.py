"""Fetch the full description of every offer in the latest internships CSV.

- LinkedIn: one request per offer through jobspy's own LinkedIn detail fetcher
  (the code path behind `fetch_description=True`), with slow random pauses and
  backoff on rate limits.
- Indeed: search results already carry the description, so the original Indeed
  searches are re-run and joined on the job key (`jk`).

Resumable: rows already marked `ok` in offers_with_descriptions.csv are kept,
so re-running only retries what's missing.
"""

import glob
import logging
import random
import re
import sys
import time

import pandas as pd
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.constant import headers as LINKEDIN_HEADERS
from jobspy.model import ScraperInput
from jobspy.util import RequestsRotating, create_session

from find_internships import COUNTRY, LOCATIONS, NARROW_LOCATIONS, build_query, search_plan

OUT = "offers_with_descriptions.csv"
LINKEDIN_PAUSE = (5, 10)        # seconds between LinkedIn detail requests
INDEED_PAUSE = 4                # seconds between Indeed searches
BACKOFFS = [60, 120]            # waits after a rate-limit before giving up
INDEED_HOURS_OLD = 30 * 24
INDEED_RESULTS = 50


def latest_csv() -> str:
    files = sorted(glob.glob("internships_*.csv"))  # ISO dates sort lexically
    if not files:
        sys.exit("No internships_*.csv found; run find_internships.py first.")
    return files[-1]


def load_work(src: str) -> pd.DataFrame:
    df = pd.read_csv(src, encoding="utf-8-sig")
    df["description"] = pd.NA
    df["fetch_status"] = "pending"
    try:
        prev = pd.read_csv(OUT, encoding="utf-8-sig")
    except FileNotFoundError:
        return df
    done = prev[prev["fetch_status"] == "ok"].set_index("job_url")["description"]
    hit = df["job_url"].isin(done.index)
    df.loc[hit, "description"] = df.loc[hit, "job_url"].map(done)
    df.loc[hit, "fetch_status"] = "ok"
    print(f"Resuming: {hit.sum()} descriptions already fetched, {(~hit).sum()} to go.")
    return df


def save(df: pd.DataFrame) -> None:
    df.to_csv(OUT, index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------- Indeed ----

def indeed_key(url: str) -> str | None:
    m = re.search(r"[?&]jk=([0-9a-f]+)", str(url))
    return m.group(1) if m else None


def fetch_indeed(df: pd.DataFrame) -> None:
    todo = df[(df["site"] == "indeed") & (df["fetch_status"] != "ok")]
    if todo.empty:
        return
    wanted = {indeed_key(u): i for i, u in todo["job_url"].items()}
    print(f"Indeed: {len(wanted)} offers; re-running searches to collect descriptions...")
    for loc, term, _ in search_plan(LOCATIONS, NARROW_LOCATIONS):  # same searches as the scrape
        if not wanted:
            break
        query = build_query(term)
        try:
            res = scrape_jobs(site_name=["indeed"], search_term=query, location=loc,
                              country_indeed=COUNTRY, results_wanted=INDEED_RESULTS,
                              hours_old=INDEED_HOURS_OLD, verbose=0)
        except Exception as exc:
            print(f"  ERROR {query!r} @ {loc}: {type(exc).__name__}: {exc}")
            continue
        found = 0
        for _, row in res.iterrows():
            idx = wanted.pop(indeed_key(row["job_url"]), None)
            if idx is not None and isinstance(row.get("description"), str) and row["description"].strip():
                df.at[idx, "description"] = row["description"]
                df.at[idx, "fetch_status"] = "ok"
                found += 1
        print(f"  {query!r:<38} @ {loc:<14} -> {len(res)} results, {found} matched")
        time.sleep(INDEED_PAUSE)
    for idx in wanted.values():
        df.at[idx, "fetch_status"] = "not_found"  # offer no longer in search results
    save(df)


# -------------------------------------------------------------- LinkedIn ----

class _WarningCollector(logging.Handler):
    """jobspy's `_fetch_details` logs failures instead of raising; keep the last one."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.last: str | None = None

    def emit(self, record):
        self.last = record.getMessage()


def make_linkedin() -> tuple[LinkedIn, _WarningCollector]:
    si = ScraperInput(site_name=["linkedin"], country_indeed=COUNTRY, fetch_description=True)
    scraper = LinkedIn()
    scraper.scraper_input = si
    scraper.session = create_session(RequestsRotating(si.proxies, clear_cookies=True), si,
                                     LINKEDIN_HEADERS)
    collector = _WarningCollector()
    log = logging.getLogger("jobspy.linkedin")
    log.setLevel(logging.WARNING)
    log.propagate = False  # we print our own status lines
    log.addHandler(collector)
    return scraper, collector


def fetch_linkedin(df: pd.DataFrame) -> None:
    todo = df[(df["site"] == "linkedin") & (df["fetch_status"] != "ok")].index
    if todo.empty:
        return
    print(f"LinkedIn: fetching {len(todo)} descriptions "
          f"(~{len(todo) * sum(LINKEDIN_PAUSE) / 2 / 60:.0f} min)...")
    scraper, collector = make_linkedin()
    backoff = 0
    pos = 0
    while pos < len(todo):
        idx = todo[pos]
        m = re.search(r"/jobs/view/(\d+)", str(df.at[idx, "job_url"]))
        if not m:
            df.at[idx, "fetch_status"] = "bad_url"
            pos += 1
            continue
        collector.last = None
        details = scraper._fetch_details(m.group(1))
        desc = details.get("description")
        label = f"  [{pos + 1:>2}/{len(todo)}] {str(df.at[idx, 'title'])[:55]:<55}"

        if desc:
            df.at[idx, "description"] = desc
            df.at[idx, "fetch_status"] = "ok"
            print(f"{label} ok ({len(desc)} chars)")
            backoff = 0
            pos += 1
            save(df)  # checkpoint after every success
        elif collector.last and re.search(r"\b(429|403)\b", collector.last):
            if backoff >= len(BACKOFFS):
                print(f"{label} rate-limited again; stopping. Re-run later to resume.")
                break
            wait = BACKOFFS[backoff]
            backoff += 1
            print(f"{label} rate-limited ({collector.last}); waiting {wait}s then retrying")
            time.sleep(wait)
            continue  # retry the same offer
        else:
            reason = collector.last or "no description on page"
            df.at[idx, "fetch_status"] = "failed"
            print(f"{label} FAILED: {reason}")
            pos += 1
        time.sleep(random.uniform(*LINKEDIN_PAUSE))
    save(df)


def main() -> None:
    src = latest_csv()
    print(f"Source: {src}")
    df = load_work(src)
    fetch_indeed(df)
    fetch_linkedin(df)
    save(df)

    print("=" * 70)
    print(f"Saved {len(df)} offers to {OUT}")
    print(df.groupby(["site", "fetch_status"]).size().to_string())


if __name__ == "__main__":
    main()
