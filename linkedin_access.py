"""LinkedIn access for one hunt run: the 429 gate and description fetches for new offers.

- The first 429 of a run closes the gate: every later LinkedIn request of that run is
  skipped (searches and description fetches); Indeed is not affected. The gate also writes
  logs/linkedin_skip.txt so the NEXT run skips LinkedIn entirely; that run deletes the file.
- Descriptions: only for offers about to be inserted (sync_sheet.sync's prepare hook), at
  most MAX_DESCRIPTIONS per run, best title-only scores first, PAUSE seconds apart. Offers
  left without a description are scored from the title (Score fiable = non).
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Callable

from config import SEARCH
from sheet_lib import ROOT, now_paris

SKIP_FILE = ROOT / "logs" / "linkedin_skip.txt"
MAX_DESCRIPTIONS = SEARCH["linkedin_descriptions"]   # detail requests per run, on top of the searches
COUNTRY = SEARCH["country"]
PAUSE = (5, 10)                # seconds between detail requests
RATE_LIMITED = re.compile(r"\b429\b")

log = logging.getLogger("hunt")


class LinkedInGate:
    """Open until the first 429 of the run; closed for the whole run if the previous run hit one."""

    def __init__(self, skip_file: Path | None = None):
        self.skip_file = skip_file or SKIP_FILE
        self.blocked_at: str | None = None          # time of this run's 429
        self.skipped_for: str | None = None         # previous run's 429, read from the skip file
        try:
            self.skipped_for = self.skip_file.read_text(encoding="utf-8").strip() or "?"
            self.skip_file.unlink()
            log.warning("LinkedIn skipped this run (rate limit in the previous run, %s)", self.skipped_for)
        except FileNotFoundError:
            pass

    @property
    def open(self) -> bool:
        return not (self.blocked_at or self.skipped_for)

    def hit_429(self, where: str) -> None:
        if self.blocked_at:
            return
        self.blocked_at = now_paris().strftime("%H:%M")
        self.skip_file.parent.mkdir(exist_ok=True)
        self.skip_file.write_text(now_paris().isoformat(), encoding="utf-8")
        log.warning("LinkedIn 429 (%s): no more LinkedIn requests this run, and the next run skips LinkedIn",
                    where)

    def digest_lines(self) -> list[str]:
        if self.blocked_at:
            return [f"⚠️ LinkedIn 429 à {self.blocked_at} : LinkedIn en pause pour ce run et le suivant"]
        if self.skipped_for:
            when = self.skipped_for[11:16] if len(self.skipped_for) >= 16 else self.skipped_for
            return [f"ℹ️ LinkedIn ignoré ce run (limite atteinte au run précédent, {when})"]
        return []


class _LastWarning(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.last: str | None = None

    def emit(self, record):
        self.last = record.getMessage()


def _jobspy_details() -> Callable[[str], tuple[str | None, str | None]]:
    """job_id -> (description, warning) through jobspy's own detail fetcher (private API,
    python-jobspy is pinned). The logger is only listened to, never silenced, so run_hunt's
    error collector still sees what jobspy reports."""
    from jobspy.linkedin import LinkedIn
    from jobspy.linkedin.constant import headers
    from jobspy.model import ScraperInput
    from jobspy.util import RequestsRotating, create_session

    si = ScraperInput(site_name=["linkedin"], country_indeed=COUNTRY, fetch_description=True)
    scraper = LinkedIn()
    scraper.scraper_input = si
    scraper.session = create_session(RequestsRotating(si.proxies, clear_cookies=True), si, headers)

    def fetch(job_id: str):
        listener = _LastWarning()
        jl = logging.getLogger("jobspy.linkedin")
        jl.addHandler(listener)
        try:
            desc = scraper._fetch_details(job_id).get("description")
        finally:
            jl.removeHandler(listener)
        return desc, listener.last

    return fetch


def fetch_descriptions(offers: list[dict], gate: LinkedInGate, *, rank: Callable[[dict], float],
                       limit: int = MAX_DESCRIPTIONS, pause: tuple[float, float] = PAUSE,
                       details: Callable[[str], tuple[str | None, str | None]] | None = None) -> int:
    """Fill `description` on LinkedIn offers that have none, best `rank` first. Returns the count."""
    todo = [o for o in offers if str(o.get("site")) == "linkedin" and not str(o.get("description") or "").strip()]
    if not todo:
        return 0
    if not gate.open:
        log.info("descriptions: %d LinkedIn offers left title-only (LinkedIn paused)", len(todo))
        return 0
    todo.sort(key=rank, reverse=True)
    details = details or _jobspy_details()
    fetched = 0
    for n, o in enumerate(todo[:limit]):
        m = re.search(r"/jobs/view/(\d+)", str(o.get("job_url") or ""))
        if not m:
            continue
        if n:
            time.sleep(random.uniform(*pause))
        desc, warning = details(m.group(1))
        if desc:
            o["description"] = desc
            fetched += 1
        elif warning and RATE_LIMITED.search(warning):
            gate.hit_429("description fetch")
            break
    log.info("descriptions: %d of %d new LinkedIn offers fetched (limit %d)%s", fetched, len(todo), limit,
             "" if gate.open else ", stopped by a 429")
    return fetched
