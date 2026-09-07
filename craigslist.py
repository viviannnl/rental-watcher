"""Craigslist rental search.

Craigslist has no public API. The web UI is a JS app that talks to an internal
JSON endpoint (sapi.craigslist.org), which is what we use: one request per poll
returns every result with coordinates already attached, and the server honours a
radius filter. That is far gentler on Craigslist than scraping N detail pages.

Split in two halves on purpose:

    crawl()  talks to Craigslist and knows nothing about who is watching.
    match()  decides whether one listing suits one saved search, and talks to
             nothing at all.

The split is what keeps upstream load flat as watchers are added - a second saved
search costs zero extra requests - and it is what makes the filter rules testable
without a network, which is where the interesting bugs were hiding.

On the 360 cap: the API returns at most 360 items per request and there is no way
to page past them. Measured, not assumed - a larger batch size returns nothing, the
batch parameter's second field is not an offset, and s/offset/start return a
different result set rather than the next page. Every radius saturates (a 2 km
circle already reports ~1300 current postings), so the crawl always sees the newest
360 and nothing older. Fine for alerting, which only cares what appeared since last
time, but it means a narrower CRAWL_RADIUS_KM buys real coverage: the same 360 slots
spent on a smaller area reach further back in time.

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
      5  -> [bedrooms, square feet] - the second value is floor area, not
            bathrooms: a live sample ran from 0 to 3000, and a "2 Bedroom /
            2 Bathroom" posting reported 1103.

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


def crawl(lat=None, lon=None, radius_km=None):
    """Every current posting in the covered area, newest first, unfiltered.

    Deliberately carries no per-watcher filters: price, bedrooms and walking
    distance are decided later by match(), against stored rows. One request serves
    every saved search, so watchers are free and Craigslist sees the same traffic
    whether one person is looking or fifty.

    The radius here is the service's coverage area, not anyone's commute. It has to
    be wide enough to contain every saved search, and no wider - see the 360 cap in
    the module docstring.

    Returns rows without distance_m; distance is a property of a search, not a
    listing, so whoever matches fills it in.
    """
    params = {
        "areaId": config.CL_AREA_ID,
        "subAreaId": config.CL_SUBAREA_ID,
        "batch": f"{config.CL_AREA_ID}-0-{BATCH_SIZE}-1-0",
        "cc": "CA",
        "lang": "en",
        "searchPath": config.CL_CATEGORY,
        "sort": "date",
        "lat": config.CRAWL_LAT if lat is None else lat,
        "lon": config.CRAWL_LON if lon is None else lon,
        "search_distance": config.CRAWL_RADIUS_KM if radius_km is None else radius_km,
    }

    resp = requests.get(
        SAPI, params=params, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    items = data.get("items", [])

    parsed = [item for item in (parse_item(raw) for raw in items) if item]
    log.info(
        "crawl: %d postings within %s km (%d unparseable, %d currently listed there)",
        len(parsed), params["search_distance"], len(items) - len(parsed),
        data.get("totalResultCount", 0),
    )
    # Left in the API's sort=date order (newest first) so callers can prioritise
    # fresh listings; sort by distance at display time instead.
    return parsed


def distance_from(listing, lat, lon):
    """Straight-line metres from a listing to a point, or None if ungeocoded."""
    if listing.get("lat") is None or listing.get("lon") is None:
        return None
    return haversine_m(lat, lon, listing["lat"], listing["lon"])


def match(listing, f, exclude=()):
    """Does one listing suit one saved search? Returns (ok, reason).

    Pure: no network, no database, no settings lookup, so it can be tested directly
    and run against a thousand stored rows without cost. `f` is a filter dict shaped
    like settings.all_settings(); `exclude` is title keywords to reject.

    reason is None when ok, otherwise "<code>: <detail>" - the code before the colon
    is stable enough to tally on, the detail is for humans.
    """
    d = distance_from(listing, f["office_lat"], f["office_lon"])
    if d is None:
        # No coordinates means we can't honour the walking-distance rule at all.
        return False, "no coordinates: nothing to measure from"

    title = (listing.get("title") or "").lower()
    hit = next((kw for kw in exclude if kw in title), None)
    if hit:
        return False, f"keyword: title contains {hit!r}"

    beds = listing.get("bedrooms")
    if beds is not None and not f["min_bedrooms"] <= beds <= f["max_bedrooms"]:
        return False, f"bedrooms: {beds} outside {f['min_bedrooms']}-{f['max_bedrooms']}"

    price = listing.get("price")
    # A missing price is not a reason to reject: plenty of real listings omit it,
    # and "ask me" shouldn't be treated as "too expensive".
    if price is not None:
        if f["min_price"] and price < f["min_price"]:
            return False, f"price: ${price} under ${f['min_price']}"
        if f["max_price"] and price > f["max_price"]:
            return False, f"price: ${price} over ${f['max_price']}"

    radius = metres_for_walk(f["walk_minutes"])
    if d > radius:
        return False, f"distance: {round(d)}m past {radius}m"
    return True, None


def matching(listings, f, exclude=()):
    """The subset of listings that suit this search, each with distance_m filled in.

    Tallies rejections by reason code and logs the breakdown, which is the line worth
    reading when a poll finds nothing and you want to know whether that means "no new
    places" or "your price cap is doing it".
    """
    out, rejected = [], {}
    for listing in listings:
        ok, reason = match(listing, f, exclude)
        if not ok:
            rejected[reason.split(":")[0]] = rejected.get(reason.split(":")[0], 0) + 1
            continue
        listing["distance_m"] = round(distance_from(listing, f["office_lat"], f["office_lon"]))
        out.append(listing)

    radius = metres_for_walk(f["walk_minutes"])
    log.info(
        "match: %d of %d within a %d min walk / %dm%s",
        len(out), len(listings), f["walk_minutes"], radius,
        " (rejected: " + ", ".join(f"{k} {v}" for k, v in sorted(rejected.items())) + ")"
        if rejected else "",
    )
    return out


def search():
    """Current listings for the one saved search, newest first, with distance_m.

    Convenience for the single-user case: crawl, then match against whatever the
    dashboard currently says. Filters are read fresh so a change takes effect on the
    next poll without a restart.
    """
    import settings  # deferred: settings imports this module, so avoid a cycle

    return matching(crawl(), settings.all_settings(), config.EXCLUDE_KEYWORDS)


# Walking pace, and how much longer a real pavement route is than a straight line.
# 1.25 suits downtown Vancouver's grid; a tangle of cul-de-sacs would be higher.
WALK_SPEED_M_PER_MIN = 5000 / 60
WALK_DETOUR = 1.25


def walk_minutes(distance_m):
    """Straight-line metres -> roughly how long that is to walk."""
    return max(1, round(distance_m * WALK_DETOUR / WALK_SPEED_M_PER_MIN))


def metres_for_walk(minutes):
    """Inverse of walk_minutes: the search radius that matches a walking time."""
    return round(minutes * WALK_SPEED_M_PER_MIN / WALK_DETOUR)
