"""Scheduled hunt: scrape -> sync the Google Sheet -> Telegram digest of new offers.

- First run of each calendar day (Paris time): 7-day window. Other runs: last 24h.
  Searches, priority area and request budget come from config.yaml (search:, priority:).
- Never scrapes more often than every 3h (MIN_INTERVAL) unless --force is given, and
  never runs twice at once (logs/hunt.lock).
  --full forces the 7-day scan, --no-alert syncs without sending Telegram messages.
- A failing stage is logged and doesn't stop the next one: scrape errors still sync
  whatever came back. If the sheet is unreachable (VPN), the offers go to
  pending_sync.jsonl, the digest is still sent ("sheet sync pending"), and the next run
  that reaches the sheet flushes the queue before its own sync. The 7-day scan counts as
  done when its scrape worked (at most half the searches errored or returned nothing) and
  the offers are in the sheet or the queue. Log: logs/hunt.log.

No auto-apply: this only finds offers and notifies.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # Task Scheduler doesn't set a working directory

import email_alerts  # noqa: E402
from config import PRIORITY, RUNS, SEARCH  # noqa: E402
import pending_sync  # noqa: E402
from find_internships import SCORING_COLUMNS, _ErrorCollector, clean, run_searches  # noqa: E402
from fit_score import annotate, fit  # noqa: E402
from linkedin_access import LinkedInGate, fetch_descriptions  # noqa: E402
from sheet_lib import LayoutError, load_env, now_paris, sheets_unreachable  # noqa: E402
from sync_sheet import sync  # noqa: E402

LOG_DIR = ROOT / "logs"
LAST_SCRAPE = LOG_DIR / "last_scrape.txt"
LAST_FULL_SCAN = LOG_DIR / "last_full_scan.txt"
MIN_INTERVAL = timedelta(minutes=RUNS["min_interval_minutes"])  # 3h cadence, small margin for jitter
# (hours_old, {location: LinkedIn results} for broad terms, same for NARROW_TERMS).
# Indeed always takes a full page (100). Requests (find_internships.request_count):
# 39 for the full scan, 18 for a quick one; the approved budget is in CLAUDE.md.
FULL_SCAN = (SEARCH["full_scan"]["hours"], dict(SEARCH["full_scan"]["locations"]),
             dict(SEARCH["full_scan"]["narrow_locations"]))                  # first run of the day
QUICK_SCAN = (SEARCH["quick_scan"]["hours"], dict(SEARCH["quick_scan"]["locations"]),
              dict(SEARCH["quick_scan"]["narrow_locations"]))                # every other run
WATCH_CAP = SEARCH["watch_cap"]        # quick-scan searches hitting their cap here are reported
LAST_SCAN_CSV = LOG_DIR / "last_scan.csv"  # clean offers of the latest scrape, for analysis
LOCK = LOG_DIR / "hunt.lock"            # one run at a time, see acquire_lock()
STALE_LOCK = timedelta(hours=2)         # a run takes minutes: an older lock was left by a killed run
TELEGRAM_LIMIT = 4000                  # API max is 4096 characters

log = logging.getLogger("hunt")


class RedactingFormatter(logging.Formatter):
    """Last line of defence: the bot token is part of Telegram URLs, never let it reach a log."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(LOG_DIR / "hunt.log", maxBytes=1_000_000,
                                                           backupCount=5, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in ("urllib3", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def redact(text: str) -> str:
    imap = os.environ.get("IMAP_APP_PASSWORD") or ""
    for secret in (os.environ.get("TELEGRAM_TOKEN"), imap, imap.replace(" ", "")):
        if secret:
            text = text.replace(secret, "***")
    return text


def read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def acquire_lock() -> bool:
    """Two runs starting together (e.g. both slowed down by low memory) would both pass the
    3h guard and sync the sheet concurrently. Creating the lock file is atomic, so only one
    run gets through; the lock is removed when the run ends."""
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = timedelta(seconds=time.time() - LOCK.stat().st_mtime)
            except OSError:  # released in the meantime: try again
                continue
            if age < STALE_LOCK:
                log.info("skipped (another run in progress): %s", read_stamp(LOCK) or "lock just taken")
                return False
            log.warning("removing a stale lock (%d min old): %s", age.total_seconds() // 60, read_stamp(LOCK))
            LOCK.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid {os.getpid()} since {now_paris().isoformat()}")
        return True
    return False


# ---------------------------------------------------------------- scrape
def scrape(hours_old: int, locations: dict[str, int], narrow: dict[str, int],
           gate: LinkedInGate | None = None) -> tuple[pd.DataFrame, list[str], list[dict]]:
    errors: list[str] = []
    outcomes: list[dict] = []
    collector = _ErrorCollector(errors)
    jobspy_logger = logging.getLogger("jobspy")
    jobspy_logger.addHandler(collector)
    try:
        raw = run_searches(hours_old, errors, collector, locations=locations, narrow=narrow, outcomes=outcomes,
                           linkedin_gate=gate)
        offers = clean(raw)
        log.info("scrape: %d raw -> %d clean offers (%dh window, LinkedIn per term: %s, narrow terms: %s, "
                 "Indeed: 100)", len(raw), len(offers), hours_old, locations, narrow)
        offers.drop(columns=[c for c in SCORING_COLUMNS if c in offers.columns]).to_csv(
            LAST_SCAN_CSV, index=False, encoding="utf-8-sig")
        return offers, errors, outcomes
    finally:
        jobspy_logger.removeHandler(collector)


# ---------------------------------------------------------------- telegram
def age_label(age) -> str:
    if age is None or pd.isna(age):
        return "date inconnue"
    return "aujourd'hui" if int(age) <= 0 else f"{int(age)} j"


def digest_order(offers: list[dict]) -> list[tuple[str, dict]]:
    """(group, offer): the priority region first (🏠), then the rest, then internships under 4 months;
    highest score first inside each group. Offers not scored yet are scored here."""
    for o in offers:
        if "score" not in o:
            o.update(annotate(o))
    groups = {"home": [], "rest": [], "short": []}
    for o in offers:
        g = "short" if o.get("short") else "home" if o.get("region") == PRIORITY["region"] else "rest"
        groups[g].append(o)
    return [(g, o) for g in groups for o in sorted(groups[g], key=lambda o: -o["score"])]


def _euros(v) -> str:
    return f"{int(v):,} €/mois".replace(",", " ")


def digest(offers: list[dict], notes: list[str] | None = None) -> list[str]:
    header = f"🆕 <b>{len(offers)} nouvelle{'s' if len(offers) > 1 else ''} offre{'s' if len(offers) > 1 else ''}</b>"
    for note in notes or []:
        header += f"\n<i>{html.escape(note)}</i>"
    lines = []
    def e(v) -> str:
        return html.escape("?" if v is None or pd.isna(v) or not str(v).strip() else str(v))

    previous = None
    for group, o in digest_order(offers):
        if group == "short" and previous != "short":
            lines.append("<i>— Stages de moins de 4 mois —</i>")
        previous = group
        loc = o.get("location")
        city = None if loc is None or pd.isna(loc) else str(loc).split(",")[0].strip()
        score = f"{o['score']}" if o.get("score_fiable") == "oui" else f"~{o['score']}"  # ~ = title only
        facts = [e(o.get("company")), e(city), age_label(o.get("age_days"))]
        pay = o.get("gratification")
        if pay is not None and not pd.isna(pay):
            facts.append(_euros(pay))
        facts += [html.escape(v) for v in (o.get("duree"), o.get("remarque")) if v]
        lines.append(f"{'🏠 ' if group == 'home' else '• '}<b>{score}</b> · <b>{e(o.get('title'))}</b>\n"
                     f"{' · '.join(facts)}\n"
                     f"<a href=\"{html.escape(str(o.get('job_url', '')), quote=True)}\">Voir l'offre</a>")
    messages, current = [], header
    for line in lines:
        if len(current) + 2 + len(line) > TELEGRAM_LIMIT:
            messages.append(current)
            current = line
        else:
            current += "\n\n" + line
    messages.append(current)
    return messages


def send_telegram(messages: list[str]) -> None:
    token, chat_id = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    for text in messages:
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=(5, 20), data={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if not resp.ok:
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
        time.sleep(1)


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="bypass the 3h guard (manual runs, tests)")
    parser.add_argument("--full", action="store_true", help="run the 7-day scan even if today's was done")
    parser.add_argument("--no-alert", action="store_true", help="sync the sheet but send no Telegram message")
    args = parser.parse_args()
    setup_logging()
    load_env()
    if not acquire_lock():
        return 0
    try:
        return hunt(args)
    finally:
        LOCK.unlink(missing_ok=True)


def hunt(args: argparse.Namespace) -> int:
    now = now_paris()
    last = read_stamp(LAST_SCRAPE)
    if last and not args.force:
        try:
            elapsed = now - datetime.fromisoformat(last)
        except ValueError:
            elapsed = MIN_INTERVAL
        if elapsed < MIN_INTERVAL:
            log.info("skipped (3h guard): last scrape %s, %d min ago", last, elapsed.total_seconds() // 60)
            return 0

    full = args.full or read_stamp(LAST_FULL_SCAN) < now.date().isoformat()  # ISO dates compare as strings
    hours_old, locations, narrow = FULL_SCAN if full else QUICK_SCAN
    flags = [f for f, on in (("forced", args.force), ("--full", args.full), ("no alert", args.no_alert)) if on]
    log.info("run start%s: %s scan", f" ({', '.join(flags)})" if flags else "", "7-day" if full else "24h")
    LAST_SCRAPE.write_text(now.isoformat(), encoding="utf-8")  # before scraping: a crash still counts
    gate = LinkedInGate()  # closed if the previous run hit a 429 (consumes logs/linkedin_skip.txt)

    offers, errors, outcomes = pd.DataFrame(), [], []
    try:
        offers, errors, outcomes = scrape(hours_old, locations, narrow, gate)
    except Exception as exc:
        log.exception("scrape crashed: %s", redact(str(exc)))
    for err in errors:
        log.warning("scrape error: %s", redact(err))
    watched_caps = [o for o in outcomes if not full and o["location"] == WATCH_CAP
                    and o["got"] is not None and o["got"] >= o["wanted"]]
    for c in watched_caps:
        log.warning("cap reached: %r @ %s on %s returned the full %d results (more probably exist)",
                    c["term"], c["location"], c["site"], c["wanted"])
    # LinkedIn searches skipped after a 429 don't count: the gate already handles LinkedIn.
    attempted = [o for o in outcomes if not o.get("skipped")]
    failed_searches = sum(1 for o in attempted if not o["got"])  # raised (None) or nothing returned (0)
    scrape_ok = bool(attempted) and failed_searches * 2 <= len(attempted)
    if not scrape_ok:
        log.warning("scrape failed: %d of %d searches errored or returned nothing%s", failed_searches,
                    len(attempted), " - the 7-day scan will be retried next run" if full else "")
    if skipped := len(outcomes) - len(attempted):
        log.info("LinkedIn: %d searches skipped (rate limit)", skipped)

    # ---- Email alerts (WTTJ, HelloWork, JobTeaser): optional, off unless IMAP is set in .env.
    # Read-only IMAP, never a request to those sites. Not configured = skipped silently.
    alerts = None
    try:
        alerts = email_alerts.collect()
        if alerts.configured:
            log.info("email alerts: %d emails read, offers per source %s", alerts.emails, alerts.per_source)
            for err in alerts.errors:
                log.warning("email alerts: %s", redact(err))
            if not alerts.offers.empty:
                offers = pd.concat([offers, alerts.offers], ignore_index=True)
    except Exception as exc:  # IMAP down or login refused: the run goes on without alerts
        log.error("email alerts failed (IMAP): %s: %s", type(exc).__name__, redact(str(exc)))

    def prepare(new_offers: list[dict]) -> None:  # LinkedIn descriptions for offers about to be inserted
        fetch_descriptions(new_offers, gate, rank=lambda o: fit(o).base)

    # ---- Sheet: flush the queue of earlier failed syncs first, then this run's offers.
    to_alert: list[dict] = []
    result, flushed = None, 0
    layout_problem = ""   # set when row 1 of Offres is missing or duplicating a header
    try:
        queue = pending_sync.load()
        alerted = pending_sync.alerted_ids(queue)  # an offer queued twice is alerted at most once
        for detected, batch in pending_sync.batches(queue):
            res = sync(batch, detected=detected, prepare=prepare)
            stamp = detected.isoformat()
            pending_sync.save([e for e in pending_sync.load() if e["detected"] != stamp])
            to_alert += [o for o in res.new if not pending_sync.offer_ids(o) & alerted]
            flushed += len(batch)
            log.info("queue: flushed %d offers detected %s (%d new)", len(batch), stamp, len(res.new))
        result = sync(offers, prepare=prepare)
        to_alert += result.new
    except Exception as exc:
        if isinstance(exc, LayoutError):  # raised before any write: the offers are queued below
            layout_problem = str(exc)
            log.error("sheet layout problem, nothing written, offers queued: %s", exc)
        elif sheets_unreachable(exc):
            log.warning("Sheets unreachable, VPN suspected: %s: %s", type(exc).__name__, redact(str(exc)))
        else:
            log.error("sync failed: %s: %s", type(exc).__name__, redact(str(exc)))

    secured = result is not None  # offers are in the sheet, or safely queued below
    if result is None:
        try:
            entries = pending_sync.add(offers, now, alerted=args.no_alert)
            secured = True
            to_alert += pending_sync.probably_new(entries, pending_sync.sheet_index())
        except Exception as exc:
            log.error("could not queue the offers: %s: %s", type(exc).__name__, exc)
    if full and scrape_ok and secured:
        LAST_FULL_SCAN.write_text(now.date().isoformat(), encoding="utf-8")
    if secured and alerts is not None:
        alerts.commit()  # the alert emails are only marked as processed once their offers are safe

    to_alert = unique_offers(to_alert)
    notes = gate.digest_lines()
    if result is None:
        notes.insert(0, f"⚠️ Colonnes du Sheet introuvables ou en double ({layout_problem}) : synchro en attente"
                     if layout_problem else "⚠️ sheet sync pending (Sheets unreachable)")
    if to_alert and args.no_alert:
        log.info("telegram: %d new offers, not sent (--no-alert)", len(to_alert))
    elif to_alert:
        try:
            msgs = digest(to_alert, notes)
            send_telegram(msgs)
            log.info("telegram: %d offers in %d message(s)%s", len(to_alert), len(msgs),
                     "" if result is not None else ", sheet sync pending")
            if result is None:
                pending_sync.mark_alerted({str(o.get("job_url") or "").strip() for o in to_alert})
        except Exception as exc:
            log.error("telegram failed: %s: %s", type(exc).__name__, redact(str(exc)))
    else:
        log.info("telegram: no new offers, nothing sent")

    if result is None:
        log.info("run end: %d offers, sync FAILED, %d in queue (%d flushed before the failure), "
                 "%d scrape errors, %d priority-area cap hits", len(offers), len(pending_sync.load()), flushed,
                 len(errors), len(watched_caps))
        return 1
    log.info("run end: %d offers, %d new, %d seen again, %d merged, %d flags reset, %d flushed from queue, "
             "%d scrape errors, %d priority-area cap hits", len(offers), len(result.new), result.seen, result.merged,
             result.flags, flushed, len(errors), len(watched_caps))
    return 0


def unique_offers(offers: list[dict]) -> list[dict]:
    """Drop repeats by link or title|company|city (a flushed offer can also be in this run)."""
    seen, out = set(), []
    for o in offers:
        ids = pending_sync.offer_ids(o)
        if not ids & seen:
            out.append(o)
        seen |= ids
    return out


if __name__ == "__main__":
    sys.exit(main())
