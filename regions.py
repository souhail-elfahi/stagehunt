"""Region (2016 regions) of an offer's location string.

Locations look like "City, X, France" where X is a full region name (LinkedIn), an Indeed
code, or missing; LinkedIn also uses metro labels ("Greater Paris Metropolitan Region").
region_of() returns the region name, NO_REGION for a France-wide offer, or UNKNOWN ("?")
when nothing matches. Unknown codes are collected in UNMAPPED so they can be added here.
"""

from __future__ import annotations

import re
import unicodedata

UNKNOWN = "?"
NO_REGION = "France (non précisée)"

IDF, HDF, ARA, OCC, NAQ, PAC = ("Île-de-France", "Hauts-de-France", "Auvergne-Rhône-Alpes", "Occitanie",
                                "Nouvelle-Aquitaine", "Provence-Alpes-Côte d'Azur")
GES, BRE, PDL, NOR, CVL, BFC, COR = ("Grand Est", "Bretagne", "Pays de la Loire", "Normandie",
                                     "Centre-Val de Loire", "Bourgogne-Franche-Comté", "Corse")
REGIONS = [IDF, HDF, ARA, OCC, NAQ, PAC, GES, BRE, PDL, NOR, CVL, BFC, COR,
           "Guadeloupe", "Martinique", "Guyane", "La Réunion", "Mayotte"]

# Indeed codes, as seen on fr.indeed.com. Indeed mixes two systems.
INDEED_CODES = {
    # Newer abbreviations. Verified on real offers: ARA, BFC, HDF, N, NA, O.
    "ARA": ARA, "BFC": BFC, "HDF": HDF, "N": NOR, "NA": NAQ, "O": OCC,
    "IDF": IDF, "GE": GES, "GES": GES, "BRE": BRE, "PDL": PDL, "CVL": CVL, "COR": COR, "PAC": PAC,
    "PACA": PAC, "OCC": OCC, "NAQ": NAQ, "NOR": NOR,
    # Old (pre-2016) GeoNames codes, numbered alphabetically. Verified: A2, A8, B5, B8.
    "A1": BFC, "A2": BRE, "A3": CVL, "A4": GES, "A5": COR, "A6": BFC, "A7": NOR, "A8": IDF,
    "A9": OCC, "B1": NAQ, "B2": GES, "B3": OCC, "B4": HDF, "B5": PDL, "B6": HDF, "B7": NAQ,
    "B8": PAC, "B9": ARA, "C1": GES, "97": NAQ, "98": ARA, "99": NOR,
}
# Pre-2016 region names (LinkedIn sometimes still uses them) -> current region.
OLD_NAMES = {
    "alsace": GES, "lorraine": GES, "champagne-ardenne": GES, "aquitaine": NAQ, "limousin": NAQ,
    "poitou-charentes": NAQ, "auvergne": ARA, "rhone-alpes": ARA, "bourgogne": BFC,
    "franche-comte": BFC, "centre": CVL, "basse-normandie": NOR, "haute-normandie": NOR,
    "languedoc-roussillon": OCC, "midi-pyrenees": OCC, "nord-pas-de-calais": HDF, "picardie": HDF,
    "provence-alpes-cote d'azur": PAC, "ile de france": IDF, "reunion": "La Réunion",
    # English names
    "brittany": BRE, "normandy": NOR, "corsica": COR, "grand-est": GES, "centre-val-de-loire": CVL,
    "burgundy": BFC, "french guiana": "Guyane",
}
# Large cities, for metro labels and locations without a region part.
CITIES = {
    "paris": IDF, "la defense": IDF, "lyon": ARA, "grenoble": ARA, "saint-etienne": ARA,
    "clermont-ferrand": ARA, "annecy": ARA, "marseille": PAC, "nice": PAC, "toulon": PAC,
    "aix-en-provence": PAC, "sophia antipolis": PAC, "toulouse": OCC, "montpellier": OCC, "nimes": OCC,
    "bordeaux": NAQ, "limoges": NAQ, "poitiers": NAQ, "la rochelle": NAQ, "lille": HDF, "amiens": HDF,
    "nantes": PDL, "angers": PDL, "le mans": PDL, "rennes": BRE, "brest": BRE, "strasbourg": GES,
    "metz": GES, "nancy": GES, "reims": GES, "mulhouse": GES, "rouen": NOR, "caen": NOR,
    "le havre": NOR, "tours": CVL, "orleans": CVL, "dijon": BFC, "besancon": BFC, "ajaccio": COR,
}

UNMAPPED: set[str] = set()  # unknown codes/labels met during this run


REMOTE = {"teletravail", "remote", "full remote", "a distance"}  # compared with _key()


def _key(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s.replace("’", "'")).strip()


_BY_NAME = {_key(r): r for r in REGIONS} | {_key(k): v for k, v in OLD_NAMES.items()}


def city_of(location) -> str:
    """Display city: first part, "Paris 11e" -> "Paris", metro labels -> "(métropole) Paris"."""
    if location is None or location != location:
        return UNKNOWN
    first = str(location).split(",")[0].strip()
    if _key(first) == "france":
        return "(non précisée)"
    metro = re.match(r"(?:greater )?(.+?)(?: metropolitan (?:region|area)| area)$", first, re.I)
    if metro and len(str(location).split(",")) == 1:
        return f"(métropole) {metro.group(1).strip()}"
    first = re.sub(r"\s+\d{1,2}(?:e|er|ème)$", "", first)  # Paris 11e, Lyon 2e, Marseille 1er
    return first.replace("’", "'") or UNKNOWN


_PARTICLES = {"de", "du", "des", "la", "le", "les", "en", "sur", "sous", "lès", "lez", "et", "aux", "au"}


def _recase(name: str) -> str:
    """'VILLENEUVE-D'ASCQ' / 'villeneuve d'ascq' -> "Villeneuve-d'Ascq" (only all-caps or all-lower)."""
    if name.startswith("(") or not (name.isupper() or name.islower()):
        return name   # "(non précisée)" is a label, not a city
    parts = re.split(r"([\s\-']+)", name.lower())
    out = []
    for i, p in enumerate(parts):
        if i % 2:                     # a separator
            out.append(p)
        elif i and (p in _PARTICLES or p in ("d", "l")):
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:])
    return "".join(out)


def ville_of(location) -> str:
    """The Ville column: the city before the first comma, cleaned. "Paris 11e" -> "Paris",
    apostrophes unified, a metro label gives its city, a bare "France" gives "(non précisée)",
    "Télétravail" is kept. Empty when there is no location."""
    city = city_of(location)
    if city == UNKNOWN:
        return ""
    city = re.sub(r"\s+", " ", city.replace("(métropole) ", "")).strip()
    return _recase(city)


def ville_key(name) -> str:
    """Spelling-insensitive key: accents, case, hyphens, spaces and apostrophes don't matter."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\s\-'’]+", " ", s).strip()


def canonical_villes(names) -> dict[str, str]:
    """key -> the spelling to use: the most "typographic" one (hyphens, accents, apostrophes:
    official names have them), then the most frequent. "Villeneuve d'Ascq" -> "Villeneuve-d'Ascq"."""
    counts: dict[str, dict[str, int]] = {}
    for n in names:
        if n:
            counts.setdefault(ville_key(n), {}).setdefault(n, 0)
            counts[ville_key(n)][n] += 1
    def rank(n, c):
        return (sum(ch in "-'" or ord(ch) > 127 for ch in n), c, len(n))
    return {k: max(v, key=lambda n: rank(n, v[n])) for k, v in counts.items()}


def region_of(location) -> str:
    if location is None or location != location or not str(location).strip():
        return UNKNOWN
    parts = [p.strip() for p in str(location).split(",") if p.strip()]
    if parts and _key(parts[-1]) == "france":
        parts = parts[:-1]
    if not parts or all(_key(p) in REMOTE for p in parts):  # France-wide or remote: no region
        return NO_REGION
    for part in reversed(parts[1:]):  # region part(s) after the city
        if part.upper() in INDEED_CODES:
            return INDEED_CODES[part.upper()]
        if region := _BY_NAME.get(_key(part)):
            return region
    city = _key(city_of(", ".join(parts)).replace("(métropole) ", ""))
    if city in CITIES:
        return CITIES[city]
    if region := _BY_NAME.get(_key(parts[0])):  # "Île-de-France, France", "Hauts-de-France"
        return region
    UNMAPPED.add(", ".join(parts[1:]) or parts[0])
    return UNKNOWN
