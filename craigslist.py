"""Craigslist rental search.

Craigslist has no public API. The web UI is a JS app that talks to an internal
JSON endpoint (sapi.craigslist.org), which is what we use: one request per poll
returns every result with coordinates already attached, and the server honours a
radius filter. That is far gentler on Craigslist than scraping N detail pages.

Response items are compact positional arrays. Decoded by inspection:

    [0] internal id (stable per posting -> our dedupe key)
    [1] monotonically increasing sequence number (descending when sort=date)
    [2] category id
    [3] price, integer
    [4] "<precision>:<n>~<lat>~<lon>" geocode, or absent/empty if ungeocoded
    [5] image prefix
    [-1] title (last bare string)
    tagged sublists, [tag, *values]:
      6  -> url slug
      10 -> display price ("$2,650")
      13 -> url id
      5  -> [bedrooms, bathrooms]

The tag numbers are not documented and can change without notice; parse_item()
is written to degrade rather than raise when a field is missing.
"""

import logging
import math

import requests

import config

log = logging.getLogger(__name__)

SAPI = "https://sapi.craigslist.org/web/v8/postings/search/full"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BATCH_SIZE = 360

TAG_SLUG = 6
TAG_PRICE_DISPLAY = 10
TAG_URL_ID = 13
TAG_BEDS_BATHS = 5


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _tag(item, tag):
    """Values of a tagged sublist, or None."""
    for field in item:
        if isinstance(field, list) and field and field[0] == tag:
            return field[1:]
    return None


def _tag1(item, tag):
    vals = _tag(item, tag)
    return vals[0] if vals else None


def _geo(item):
    raw = item[4] if len(item) > 4 else None
    if not isinstance(raw, str) or "~" not in raw:
        return None, None
    parts = raw.split("~")
    try:
        return float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None, None


def _title(item):
    # The title is the only bare string that isn't the geocode or image prefix.
    for field in reversed(item):
        if isinstance(field, str) and "~" not in field:
            return field
    return "(untitled)"


def parse_item(item):
    """One raw API item -> dict, or None if it lacks the fields we need."""
    try:
        cl_id = str(item[0])
    except (IndexError, TypeError):
        return None

    slug, url_id = _tag1(item, TAG_SLUG), _tag1(item, TAG_URL_ID)
    if not url_id:
        return None
    url = f"https://www.craigslist.org/view/d/{slug or 'listing'}/{url_id}"

    lat, lon = _geo(item)
    beds = _tag(item, TAG_BEDS_BATHS)
    price = item[3] if len(item) > 3 and isinstance(item[3], (int, float)) else None

    return {
        "cl_id": cl_id,
        "url": url,
        "title": _title(item),
        "price": int(price) if price else None,
        "price_display": _tag1(item, TAG_PRICE_DISPLAY) or (f"${price}" if price else "?"),
        "bedrooms": beds[0] if beds else None,
        "lat": lat,
        "lon": lon,
    }


def search():
    """Fetch current matching listings, each with distance_m, newest first.

    Filters come from settings (dashboard-adjustable) rather than config, so they
    are read fresh on every poll.
    """
    import settings  # deferred: settings imports db, which shouldn't load on import

    s = settings.all_settings()
    radius_m, office_lat, office_lon = s["radius_m"], s["office_lat"], s["office_lon"]

    params = {
        "areaId": config.CL_AREA_ID,
        "subAreaId": config.CL_SUBAREA_ID,
        "batch": f"{config.CL_AREA_ID}-0-{BATCH_SIZE}-1-0",
        "cc": "CA",
        "lang": "en",
        "searchPath": config.CL_CATEGORY,
        "sort": "date",
        "min_bedrooms": s["min_bedrooms"],
        "max_bedrooms": s["max_bedrooms"],
        "lat": office_lat,
        "lon": office_lon,
        # Ask for a wider radius than we want (API unit is km) and filter precisely
        # below, so listings sitting just outside aren't silently dropped by
        # Craigslist's own rounding.
        "search_distance": max(1, math.ceil(radius_m / 1000) + 1),
    }
    if s["min_price"]:
        params["min_price"] = s["min_price"]
    if s["max_price"]:
        params["max_price"] = s["max_price"]

    resp = requests.get(
        SAPI, params=params, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30
    )
    resp.raise_for_status()
    items = resp.json().get("data", {}).get("items", [])

    out = []
    skipped_nogeo = skipped_kw = 0
    for raw in items:
        item = parse_item(raw)
        if not item:
            continue
        if item["lat"] is None:
            # No coordinates means we can't honour the walking-distance rule.
            skipped_nogeo += 1
            continue
        title = item["title"].lower()
        if any(kw in title for kw in config.EXCLUDE_KEYWORDS):
            skipped_kw += 1
            continue
        d = haversine_m(office_lat, office_lon, item["lat"], item["lon"])
        if d > radius_m:
            continue
        item["distance_m"] = round(d)
        out.append(item)

    log.info(
        "craigslist: %d returned, %d within %dm (%d no coords, %d excluded by keyword)",
        len(items), len(out), radius_m, skipped_nogeo, skipped_kw,
    )
    # Left in the API's sort=date order (newest first) so callers can prioritise
    # fresh listings; sort by distance at display time instead.
    return out


def walk_minutes(distance_m):
    """Rough walking time: pavement route ~1.25x crow-flies, at 5 km/h."""
    return max(1, round(distance_m * 1.25 / (5000 / 60)))
