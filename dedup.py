"""Fuzzy cross-site duplicate rules (company names and title tags differ between sites).

classify(a, b) -> "merge" | "twin" | None, for offers given as dicts with
title, company, location, site and date (datetime.date or None):

- merge: same normalized title + same city + dates <= MAX_DAYS apart + related companies
         (a shared distinctive token, or the same group in COMPANY_ALIASES);
         or same company + same city + same date + title similarity >= MIN_SIMILARITY on
         different sites (French/English versions of one offer; on a single site, close titles
         from one company are usually distinct roles, e.g. "R&D Data Scientist").
- twin:  same normalized title + same city + dates <= MAX_DAYS apart, unrelated companies
         (e.g. a consulting firm reposting a client's offer): flagged, never merged.
         Only for titles of MIN_TWIN_WORDS+ words: generic titles are posted by many companies.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

MAX_DAYS = 2
MIN_SIMILARITY = 0.8
MIN_TWIN_WORDS = 3  # "data scientist" alone is too generic to call two companies' offers twins

TITLE_TAGS = {"stage", "stagiaire", "intern", "internship", "trainee"}
GENDER_TAGS = {"h", "f", "x", "m"}  # what remains of H/F, F/H/X, M/F once punctuation is gone
# Words that don't identify a company: legal forms, "group", geography, generic sector words.
COMPANY_STOP = {
    "groupe", "group", "france", "sa", "sas", "sasu", "sarl", "se", "en", "de", "du", "des", "la", "le",
    "les", "et", "the", "and", "of", "ile", "paris", "lille", "europe", "international", "holding",
    "consulting", "conseil", "solutions", "services", "service", "partners", "technologies",
    "technology", "tech", "digital", "data", "financial", "finance", "informatiques", "informatique",
    "bank", "banque", "assurance", "assurances", "insurance", "capital", "management", "systems",
}
# Subsidiaries/brands posting the same offer as their parent. Keys are normalized names.
COMPANY_ALIASES = {
    "mobilize": "renault",
    "boursorama": "societe generale",
}


def _ascii(text) -> str:
    if text is None or text != text:  # None / NaN
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _words(text) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", _ascii(text)).split()


def norm_title(title) -> str:
    return " ".join(w for w in _words(title) if w not in TITLE_TAGS and w not in GENDER_TAGS)


def norm_city(location) -> str:
    return " ".join(_words(str(location or "").split(",")[0]))


def company_tokens(company) -> set[str]:
    words = _words(company)
    tokens = {w for w in words if w not in COMPANY_STOP and len(w) > 2}
    name = " ".join(words)
    for marker, group in COMPANY_ALIASES.items():
        if marker in name or group in name:
            tokens.add("@" + group)  # group marker, can't collide with a real word
    return tokens


def related(a, b) -> bool:
    return bool(company_tokens(a) & company_tokens(b))


def same_company(a, b) -> bool:
    ta, tb = company_tokens(a), company_tokens(b)
    return bool(ta) and ta == tb


def title_similarity(a, b) -> float:
    return SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def _days(a: dict, b: dict) -> int | None:
    da, db = a.get("date"), b.get("date")
    return abs((da - db).days) if isinstance(da, date) and isinstance(db, date) else None


def classify(a: dict, b: dict) -> str | None:
    if not norm_city(a["location"]) or norm_city(a["location"]) != norm_city(b["location"]):
        return None
    days = _days(a, b)
    if days is None:
        return None
    same_title = bool(norm_title(a["title"])) and norm_title(a["title"]) == norm_title(b["title"])
    if same_title and days <= MAX_DAYS:
        if related(a["company"], b["company"]):
            return "merge"
        return "twin" if len(norm_title(a["title"]).split()) >= MIN_TWIN_WORDS else None
    cross_site = bool(a.get("site")) and bool(b.get("site")) and a["site"] != b["site"]
    if days == 0 and cross_site and same_company(a["company"], b["company"]) \
            and title_similarity(a["title"], b["title"]) >= MIN_SIMILARITY:
        return "merge"
    return None


def near_miss(a: dict, b: dict) -> str | None:
    """Why two offers that look alike were NOT merged (shown in the dry run only)."""
    if norm_city(a["location"]) != norm_city(b["location"]) or norm_title(a["title"]) != norm_title(b["title"]):
        return None
    days = _days(a, b)
    if days is None:
        return "date de publication manquante"
    if days > MAX_DAYS and related(a["company"], b["company"]):
        return f"{days} jours d'écart"
    return None
