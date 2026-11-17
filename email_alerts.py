"""Offers from job-alert emails (Welcome to the Jungle, HelloWork, JobTeaser), read over IMAP.

These sites forbid automated extraction in their terms, so the hunt never requests them:
the user subscribes to their own email alerts on a dedicated Gmail account, and this module
reads that mailbox. It only parses the emails, it never follows a link.

- IMAP is opened READ-ONLY (EXAMINE + BODY.PEEK): nothing is marked read, moved or deleted.
- Only emails newer than the last processed UID (logs/email_alerts_state.json) are read, at
  most MAX_EMAILS per run, oldest first. The state advances only when run_hunt calls
  batch.commit(), i.e. once the offers are in the sheet or the queue.
- Each source is parsed in isolation: a parser error or an alert email that yields no offer
  (layout change?) is logged with the source's name and the run goes on.
- Output: the same fields as find_internships.clean() (title, company, location, date_posted,
  job_url, site, search_location, description, age_days), so dedup, Région, scoring and the
  queue work unchanged. description is the offer's card text from the email (short, so the
  score is title-only, Score fiable = non); the date is the email's date.

.env: IMAP_USER, IMAP_APP_PASSWORD (app password, or the account password where the provider
allows IMAP with it), IMAP_FOLDER (default INBOX),
IMAP_HOST (default imap.gmail.com). Never printed or logged.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

import pandas as pd
from bs4 import BeautifulSoup

import fit_score
import regions
from find_internships import RELEVANCE_PATTERN
from sheet_lib import ROOT

log = logging.getLogger("hunt")

STATE_FILE = ROOT / "logs" / "email_alerts_state.json"
MAX_EMAILS = 30            # per run; the rest waits for the next run
FIRST_RUN_DAYS = 7         # first run (no state): only alerts from the last week
IMAP_TIMEOUT = 20          # seconds
SEARCH_LOCATION = "alerte email"


@dataclass(frozen=True)
class Source:
    name: str                       # value of the "site" column
    senders: tuple[str, ...]        # sender domains (auto-forwarding keeps the original From)
    offer: re.Pattern               # direct offer URL; a redirect that embeds it is decoded too
    hosts: tuple[str, ...]          # link hosts of this source (tracking links included)


SOURCES = [
    Source("wttj", ("welcometothejungle.com", "wttj.co"),
           re.compile(r"https?://(?:www\.)?welcometothejungle\.com/(?:fr|en|es|cs|sk)/companies/"
                      r"(?P<company>[\w-]+)/jobs/(?P<slug>[\w-]+)"),
           ("welcometothejungle.com", "wttj.co")),
    Source("hellowork", ("hellowork.com", "hellowork-group.com"),
           re.compile(r"https?://(?:www\.)?hellowork\.com/fr-fr/emplois/(?P<id>\d+)\.html"),
           ("hellowork.com",)),
    Source("jobteaser", ("jobteaser.com",),
           re.compile(r"https?://(?:www\.)?jobteaser\.com/(?:fr|en)/job-offers/(?P<id>[0-9a-f]{8}-[\w-]+)"),
           ("jobteaser.com",)),
]

# Link texts that are buttons or navigation, never an offer title.
CTA = re.compile(r"^(?:voir|consulter|postuler|candidater|d[ée]couvr|en savoir|lire|apply|view|see|show|"
                 r"g[ée]rer|modifier|se d[ée]sabonner|d[ée]sinscri|unsubscribe|manage|param[eè]tr|"
                 r"toutes les|all jobs|plus d.offres|more jobs|ouvrir|t[ée]l[ée]charger|t[ée]l[ée]charge|"
                 r"cliquez|click|ici\b|here\b|mon compte|my account|confidentialit|privacy|aide|help|"
                 r"cr[ée]er|create|nous contacter|contact)", re.I)
NOT_OFFER_URL = re.compile(r"unsubscribe|desabonn|desinscri|preferences|settings|alert|/companies/[\w-]+/?$|"
                           r"facebook|twitter|linkedin\.com|instagram|youtube|tiktok|apple\.com|google\.com|"
                           r"play\.google|mailto:|privacy|confidentialite|/help|/aide", re.I)
NOT_INTERNSHIP = re.compile(r"\b(?:alternan\w*|apprenti\w*|cdi|cdd|freelance|int[ée]rim)\b", re.I)
IS_INTERNSHIP = re.compile(r"\b(?:stage|stagiaire|intern(?:ship)?|trainee|pfe)\b", re.I)
NOISE_LINE = re.compile(r"^(?:stage|internship|stagiaire|cdi|cdd|alternance|t[ée]l[ée]travail.*|remote.*|"
                        r"\d+\s*(?:mois|months?).*|il y a .*|publi[ée]e? .*|posted .*|new|nouveau|nouvelle|"
                        r"\d{1,2}/\d{1,2}/\d{2,4}|[·•|\-–]+|.*€.*)$", re.I)
ALERT_SUBJECT = re.compile(r"offre|offer|job|emploi|stage|alerte|alert|opportunit|poste", re.I)


class SourceError(Exception):
    """A parser failure, tagged with its source so the log can name it."""

    def __init__(self, source: str, exc: Exception):
        super().__init__(f"{type(exc).__name__}: {exc}")
        self.source = source


@dataclass
class AlertBatch:
    offers: pd.DataFrame
    configured: bool = True
    emails: int = 0
    per_source: dict[str, int] = field(default_factory=dict)   # offers kept per source
    errors: list[str] = field(default_factory=list)            # "source X failed: ..." lines
    commit: Callable[[], None] = lambda: None                  # advance the read state


# ------------------------------------------------------------------ parsing ----
def source_of(msg: EmailMessage, html: str) -> Source | None:
    """By the sender's domain, else (manual forward) by the links in the body."""
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    for s in SOURCES:
        if any(sender.endswith(d) for d in s.senders):
            return s
    counts = {s.name: sum(html.count(h) for h in s.hosts) for s in SOURCES}
    best = max(counts, key=counts.get)
    return next(s for s in SOURCES if s.name == best) if counts[best] else None


def canonical(href: str, src: Source) -> str | None:
    """Direct offer URL without tracking parameters, from the link or a redirect that embeds it."""
    text = href
    for _ in range(3):  # tracking links may encode the target once or twice
        m = src.offer.search(text)
        if m:
            return m.group(0)
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_location(text: str) -> bool:
    # "France" only as ", France" or alone: "Banque de France" is a company.
    if re.search(r"\b\d{5}\b|,\s*france\b|^france$|t[ée]l[ée]travail|remote|\(\d{2}\)|\s-\s\d{2}$", text, re.I):
        return True
    if fit_score._norm(text) in fit_score.PRIORITY_TOWNS:
        return True
    before = set(regions.UNMAPPED)
    found = regions.region_of(text) not in (regions.UNKNOWN,)
    regions.UNMAPPED.clear()
    regions.UNMAPPED.update(before)  # probing must not pollute the "unmapped" warning
    return found


def _split_facts(line: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s+[·•|–]\s+|\s*[·•|]\s*", line) if p.strip()]


def _location(lines: list[str]) -> str:
    """A part that looks like a place; else the non-noise part of a "City · Stage · 6 mois" line."""
    facts = [p for t in lines for p in _split_facts(t)]
    found = next((p for p in facts if _is_location(p) and not NOISE_LINE.match(p)), "")
    if found:
        return found
    for t in lines:
        parts = _split_facts(t)
        if len(parts) >= 2 and any(NOISE_LINE.match(p) for p in parts):
            rest = [p for p in parts if not NOISE_LINE.match(p)]
            if len(rest) == 1:
                return rest[0]
    return ""


def _slug_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def parse_html(html: str, src: Source) -> list[dict]:
    """Offers in an alert email body: one per distinct offer link, with its card's text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head"]):
        tag.decompose()
    links: dict[str, dict] = {}   # job key -> {url, titles, anchors}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        url = canonical(href, src)
        text = _clean(a.get_text(" "))
        is_title = 6 <= len(text) <= 160 and not CTA.search(text)
        if not url:
            parts = urlsplit(unquote(href))
            host = parts.netloc.lower()
            if NOT_OFFER_URL.search(host + parts.path):
                continue                  # unsubscribe, settings, social... (path only: utm=alerte is fine)
            if any(host in (h, "www." + h) for h in src.hosts):
                continue                  # a direct page of the site that is not an offer (article, company)
            if not is_title:
                continue                  # a tracking link counts only with a real title
        key = url or href
        entry = links.setdefault(key, {"url": url or href, "titles": [], "anchors": []})
        entry["anchors"].append(a)
        if is_title:
            entry["titles"].append(text)

    offers = []
    keys = list(links)
    for key, entry in links.items():
        others = [x for k in keys if k != key for x in links[k]["anchors"]]
        card = _card(entry["anchors"][0], others)
        lines = [_clean(t) for t in card.get_text("\n").split("\n")]
        lines = [t for t in lines if t and not CTA.search(t)]
        title = max(entry["titles"], key=len) if entry["titles"] else (lines[0] if lines else "")
        if not title:
            continue
        others = [t for t in lines if t != title]
        facts = [p for t in others for p in _split_facts(t)]
        location = _location(others)
        company = next((p for p in facts if p != location and not NOISE_LINE.match(p)
                        and not _is_location(p) and len(p) <= 80), "")
        if not company and (m := src.offer.search(entry["url"])) and "company" in m.groupdict():
            company = _slug_name(m.group("company"))
        offers.append({"title": title, "company": company, "location": location, "job_url": entry["url"],
                       "site": src.name, "description": "\n".join(lines)})
    return offers


def _card(anchor, other_anchors) -> object:
    """Largest ancestor of the anchor (up to 8 levels) that contains no other offer's link."""
    others = set(map(id, other_anchors))
    node, best = anchor, anchor
    for _ in range(8):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        if any(id(a) in others for a in node.find_all("a", href=True)):
            break
        best = node
    return best


def keep(offer: dict) -> bool:
    """Data-role titles only; alerts are internship searches, but drop obvious other contracts."""
    title = offer.get("title") or ""
    if not re.search(RELEVANCE_PATTERN, title, re.I):
        return False
    return bool(IS_INTERNSHIP.search(title)) or not NOT_INTERNSHIP.search(title)


def parse_text(text: str, src: Source) -> list[dict]:
    """Plain-text alert (or a manual forward): blocks separated by blank lines, each with a URL;
    the block's first other line is the title, the next ones company / location."""
    offers = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [_clean(re.sub(r"^[>\s]+", "", t)) for t in block.split("\n")]
        lines = [t for t in lines if t]
        urls = [u for t in lines for u in re.findall(r"https?://[^\s<>\"]+", t)]
        url = next((c for u in urls if (c := canonical(u, src))), None)
        if not url:
            continue
        rest = [t for t in lines if not re.search(r"https?://", t) and not CTA.search(t)]
        if not rest:
            continue
        facts = [p for t in rest[1:] for p in _split_facts(t)]
        location = _location(rest[1:])
        company = next((p for p in facts if p != location and not NOISE_LINE.match(p) and not _is_location(p)), "")
        offers.append({"title": rest[0], "company": company, "location": location, "job_url": url,
                       "site": src.name, "description": "\n".join(rest)})
    return offers


def parse_message(raw: bytes) -> tuple[Source | None, list[dict], str]:
    """(source, offers, subject) for one RFC 822 message."""
    msg = email.message_from_bytes(raw, policy=default_policy)
    part = msg.get_body(preferencelist=("html", "plain"))
    body = part.get_content() if part else ""
    is_text = part is not None and part.get_content_type() == "text/plain"
    src = source_of(msg, body)
    subject = _clean(str(msg.get("Subject", "")))
    if src is None:
        return None, [], subject
    try:
        sent = parsedate_to_datetime(msg["Date"]).date() if msg.get("Date") else date.today()
    except (TypeError, ValueError):
        sent = date.today()
    try:
        offers = parse_text(body, src) if is_text else parse_html(body, src)
    except Exception as exc:
        raise SourceError(src.name, exc) from exc
    if not ALERT_SUBJECT.search(subject) and not any(canonical(o["job_url"], src) for o in offers):
        offers = []  # a newsletter: tracked links with article titles are not offers
    for o in offers:
        o["date_posted"] = sent.isoformat()
    return src, offers, subject


# --------------------------------------------------------------------- IMAP ----
def _config() -> dict | None:
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_APP_PASSWORD")
    if not user or not password:
        return None
    host = os.environ.get("IMAP_HOST") or "imap.gmail.com"
    if host == "imap.gmail.com":
        password = password.replace(" ", "")  # Gmail shows app passwords as "abcd efgh ijkl mnop"
    return {"user": user, "password": password, "folder": os.environ.get("IMAP_FOLDER") or "INBOX",
            "host": host}


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def collect(max_emails: int = MAX_EMAILS, state_file: Path | None = None,
            imap_factory: Callable[..., imaplib.IMAP4] | None = None) -> AlertBatch:
    """Read new alert emails and parse their offers. Raises only for connection/login errors."""
    cfg = _config()
    if cfg is None:
        return AlertBatch(pd.DataFrame(), configured=False)
    state_file = state_file or STATE_FILE
    state = _load_state(state_file)
    imap = (imap_factory or imaplib.IMAP4_SSL)(cfg["host"], timeout=IMAP_TIMEOUT)
    try:
        imap.login(cfg["user"], cfg["password"])
        status, _ = imap.select(_quote(cfg["folder"]), readonly=True)   # EXAMINE: read-only
        if status != "OK":
            raise RuntimeError(f"IMAP folder {cfg['folder']!r} not found")
        uidvalidity = _uidvalidity(imap)
        last = int(state.get("last_uid", 0)) if state.get("uidvalidity") == uidvalidity else 0
        if last:
            _, data = imap.uid("SEARCH", None, f"UID {last + 1}:*")
        else:
            since = (date.today() - timedelta(days=FIRST_RUN_DAYS)).strftime("%d-%b-%Y")
            _, data = imap.uid("SEARCH", None, f"SINCE {since}")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)
        todo, left = uids[:max_emails], len(uids) - min(len(uids), max_emails)

        batch = AlertBatch(pd.DataFrame(), emails=len(todo))
        rows: list[dict] = []
        for uid in todo:
            _, fetched = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")   # PEEK: never sets \Seen
            raw = next((p[1] for p in fetched if isinstance(p, tuple)), b"")
            try:
                src, offers, subject = parse_message(raw)
            except SourceError as exc:  # one source's layout change must not stop the others
                batch.errors.append(f"source {exc.source} failed on email uid {uid}: {exc}")
                continue
            except Exception as exc:    # unreadable message
                batch.errors.append(f"email uid {uid} unreadable: {type(exc).__name__}: {exc}")
                continue
            if src is None:
                continue                # not from a known source
            if not offers and ALERT_SUBJECT.search(subject):
                batch.errors.append(f"source {src.name}: alert email {subject[:60]!r} gave 0 offers "
                                    f"(layout changed?)")
            kept = [o for o in offers if keep(o)]
            batch.per_source[src.name] = batch.per_source.get(src.name, 0) + len(kept)
            rows += kept
        if left:
            log.info("email alerts: %d more emails wait for the next run (max %d per run)", left, max_emails)

        def commit() -> None:
            if todo:
                state_file.parent.mkdir(exist_ok=True)
                state_file.write_text(json.dumps({"uidvalidity": uidvalidity, "last_uid": max(todo),
                                                  "updated": datetime.now().isoformat(timespec="seconds")}),
                                      encoding="utf-8")
        batch.commit = commit
        batch.offers = _frame(rows)
        return batch
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _quote(folder: str) -> str:
    return folder if folder.upper() == "INBOX" else '"' + folder.replace('"', '\\"') + '"'


def _uidvalidity(imap) -> str:
    _, data = imap.response("UIDVALIDITY")   # (code, [value]) from the SELECT's untagged responses
    value = data[0] if data else None
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["search_location"] = SEARCH_LOCATION
    df = df.drop_duplicates(subset="job_url")
    posted = pd.to_datetime(df["date_posted"], errors="coerce")
    df["age_days"] = (pd.Timestamp(date.today()) - posted).dt.days
    return df.reset_index(drop=True)
