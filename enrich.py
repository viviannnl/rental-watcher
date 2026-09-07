"""Finding out how old a building is.

Craigslist has no year-built attribute, so this uses two sources in order:

1. **The posting itself.** Some landlords write "built in 1998" in the body. Free
   and exact when present, which is maybe one listing in ten.
2. **City of Vancouver open data.** The property-tax-report dataset carries
   `year_built` for every assessed property, keyed by civic number and street.
   Craigslist exposes a street address for roughly half of listings, in the
   `mapaddress` element, which is enough to look it up.

Deliberately NOT used: reverse-geocoding the listing's coordinates to guess an
address. Craigslist rounds coordinates to anonymise them, so that would
confidently attach the wrong building's age to a listing, which is worse than
admitting we don't know.

Quirks of the dataset, learned the hard way:
  * `to_civic_number` is the street number. `from_civic_number` is the unit for
    strata properties, or null - it is not a range as the names suggest.
  * Street names put the direction last: "GEORGIA ST W", not "W GEORGIA ST".
  * A strata building returns one row per unit, all with the same year, so take
    the most common value rather than assuming a single row.
"""

import collections
import json
import logging
import re
import urllib.parse

import requests

import db

log = logging.getLogger(__name__)

DETAIL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ODS = (
    "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/"
    "property-tax-report/records"
)

SUFFIX = {
    "STREET": "ST", "ST": "ST", "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "ROAD": "RD", "RD": "RD", "DRIVE": "DR", "DR": "DR", "PLACE": "PL", "PL": "PL",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "SQUARE": "SQ", "SQ": "SQ", "WAY": "WAY",
    "CRESCENT": "CRES", "CRES": "CRES", "LANE": "LANE", "COURT": "CRT", "CRT": "CRT",
    "MEWS": "MEWS", "WALK": "WALK", "TERRACE": "TERR", "HIGHWAY": "HWY",
}
DIRECTIONS = {"WEST": "W", "W": "W", "EAST": "E", "E": "E",
              "NORTH": "N", "N": "N", "SOUTH": "S", "S": "S"}

MAPADDRESS_RE = re.compile(r'class="mapaddress"[^>]*>([^<]+)', re.I)
# "built in 1998", "built: 1998", "year built 1998". Bounded to plausible years so
# a floor area or a phone number can't be read as a date.
YEAR_IN_TEXT_RE = re.compile(
    r"(?:year\s*built|built\s*in|built|constructed)\D{0,10}((?:18|19|20)\d{2})", re.I
)


def normalize_address(raw):
    """'1203-822 Seymour Street' -> ('822', 'SEYMOUR ST'), or (None, None)."""
    if not raw:
        return None, None
    text = re.sub(r"\s+", " ", re.sub(r"[,.]", " ", raw.upper())).strip()
    # Drop a leading unit number: "1203-822 ...", "#2103 - 610 ...".
    text = re.sub(r"^(?:#|UNIT|APT|SUITE)?\s*\w+\s*-\s*", "", text)
    match = re.match(r"^(\d+)\s+(.+)$", text)
    if not match:
        return None, None

    civic, words = match.group(1), match.group(2).split()
    direction = ""
    if words and words[0] in DIRECTIONS:
        direction = DIRECTIONS[words.pop(0)]
    elif len(words) > 1 and words[-1] in DIRECTIONS:
        direction = DIRECTIONS[words.pop()]
    words = [SUFFIX.get(w, w) for w in words]
    if not words:
        return None, None
    return civic, " ".join(words + ([direction] if direction else []))


def _ods_year(where):
    resp = requests.get(
        ODS, params={"where": where, "limit": 40, "select": "year_built"}, timeout=20
    )
    resp.raise_for_status()
    rows = resp.json().get("results", [])
    years = collections.Counter(r["year_built"] for r in rows if r.get("year_built"))
    if not years:
        return None
    return int(years.most_common(1)[0][0])


def year_from_open_data(civic, street):
    """Look up year built, trying an exact street match then a looser one."""
    if not (civic and street):
        return None
    try:
        year = _ods_year(f'street_name="{street}" and to_civic_number="{civic}"')
        if year:
            return year
        # Our street-type mapping can be wrong (Homer Mews is really Homer St), so
        # retry on the distinctive first word.
        return _ods_year(
            f'street_name like "{street.split()[0]}" and to_civic_number="{civic}"'
        )
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        log.warning("open data lookup failed for %s %s: %s", civic, street, exc)
        return None


def year_from_text(text):
    match = YEAR_IN_TEXT_RE.search(text or "")
    if not match:
        return None
    year = int(match.group(1))
    return year if 1850 <= year <= 2100 else None


def fetch_details(url):
    """Scrape the posting page for its address and body text."""
    try:
        resp = requests.get(url, headers={"User-Agent": DETAIL_UA}, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("could not fetch %s: %s", url, exc)
        return {}
    html = resp.text
    match = MAPADDRESS_RE.search(html)
    address = match.group(1).strip() if match else None
    body = re.sub(r"<[^>]+>", " ", html)
    return {"address": address or None, "body": body}


def year_built(listing):
    """Best effort year built. Returns (year, source, address).

    source is one of "posting", "city data", or None when we couldn't tell.
    """
    details = fetch_details(listing["url"])
    address = details.get("address")

    from_text = year_from_text(details.get("body"))
    if from_text:
        return from_text, "posting", address

    civic, street = normalize_address(address)
    if not civic:
        return None, None, address

    cache_key = f"year:{civic} {street}"
    cached = db.get_meta(cache_key)
    if cached is not None:
        # "-" records a previous miss, so we don't re-query a hopeless address.
        return (int(cached) if cached != "-" else None), "city data", address

    year = year_from_open_data(civic, street)
    db.set_meta(cache_key, year if year else "-")
    return year, ("city data" if year else None), address
