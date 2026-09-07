"""Tests for the two halves of craigslist.py: decoding, and matching.

match() is pure, so these run with no network and no database. The raw items in
RAW_* are real responses captured from the sapi endpoint, trimmed only of extra
image entries - decoding tests are worth nothing if the input is invented, since the
whole risk is that Craigslist's undocumented format isn't what we think it is.
"""

import craigslist

# A real 1-bedroom in Yaletown, $2,500.
RAW_ONE_BED = [
    12992965, 152881, 1, 2500, "1:1~49.2749~-123.116", "0ak07J",
    [13, "pJzDaA2Dhxoi2T7KYqkvQs"],
    [4, "3:00606_gHIYQTtmiEb_0ak07J"],
    [6, "vancouver-beautiful-bedroom-den-condo"],
    [10, "$2,500"],
    "*** BEAUTIFUL 1 BEDROOM + DEN CONDO FOR RENT AT THE MAX *** (Yaletown)",
    [5, 1, 615],
]

# A real 2-bedroom, $4,200. Note tag 5 reports 1103 - floor area, not bathrooms,
# despite the posting advertising 2 bathrooms.
RAW_TWO_BED = [
    2155352, 152786, 1, 4200, "1:1~49.2761~-123.1177", "0CI0t2",
    [13, "8GSjr34FtNmLAmW1q2Hqd1"],
    [6, "vancouver-furnished-bedroom-bathroom"],
    [10, "$4,200"],
    "Furnished 2 Bedroom / 2 Bathroom - YaleTown",
    [5, 2, 1103],
]

# Filters shaped like settings.all_settings(): studio or 1br, 10 min walk of the
# office, no price limits.
FILTERS = {
    "walk_minutes": 10,
    "min_price": 0,
    "max_price": 0,
    "min_bedrooms": 0,
    "max_bedrooms": 1,
    "office_lat": 49.2806,
    "office_lon": -123.1116,
}


def filters(**overrides):
    return {**FILTERS, **overrides}


def listing(**overrides):
    """A listing 200 m from the office that passes FILTERS."""
    base = {
        "cl_id": "1",
        "url": "https://example.com/1",
        "title": "Bright studio downtown",
        "price": 2000,
        "bedrooms": 0,
        "lat": 49.2824,
        "lon": -123.1116,
    }
    return {**base, **overrides}


# --- decoding -------------------------------------------------------------

def test_parse_item_decodes_a_real_posting():
    item = craigslist.parse_item(RAW_ONE_BED)
    assert item["cl_id"] == "12992965"
    assert item["price"] == 2500
    assert item["price_display"] == "$2,500"
    assert item["bedrooms"] == 1
    assert item["lat"] == 49.2749
    assert item["lon"] == -123.116
    assert item["title"].startswith("*** BEAUTIFUL 1 BEDROOM")
    assert item["url"].endswith("/vancouver-beautiful-bedroom-den-condo/pJzDaA2Dhxoi2T7KYqkvQs")


def test_parse_item_reads_the_title_not_the_image_prefix():
    """The image prefix ("0CI0t2") is a bare string too, and sits earlier in the
    array. Scanning from the end is what keeps it from being mistaken for the title."""
    assert craigslist.parse_item(RAW_TWO_BED)["title"] == (
        "Furnished 2 Bedroom / 2 Bathroom - YaleTown"
    )


def test_parse_item_returns_none_without_a_url_id():
    """No url id means no link to send, which makes the row useless to us."""
    assert craigslist.parse_item([123, 1, 1, 2000, "1:1~49.28~-123.11", "abc"]) is None


def test_parse_item_survives_missing_fields():
    """The tag numbers are undocumented and can change; degrading beats raising."""
    item = craigslist.parse_item([123, 1, 1, None, None, [13, "xyz"], "A place"])
    assert item["price"] is None
    assert item["price_display"] == "?"
    assert item["bedrooms"] is None
    assert item["lat"] is None and item["lon"] is None


def test_parse_item_handles_a_malformed_geocode():
    item = craigslist.parse_item(
        [123, 1, 1, 2000, "1:1~notanumber~-123.11", [13, "xyz"], "A place"]
    )
    assert item["lat"] is None and item["lon"] is None


# --- matching -------------------------------------------------------------

def test_match_accepts_a_listing_that_fits():
    ok, reason = craigslist.match(listing(), filters())
    assert ok and reason is None


def test_match_rejects_ungeocoded_listings():
    ok, reason = craigslist.match(listing(lat=None, lon=None), filters())
    assert not ok and reason.startswith("no coordinates")


def test_match_rejects_listings_past_the_walking_radius():
    # Roughly 2 km north of the office, well past a 10 minute walk.
    ok, reason = craigslist.match(listing(lat=49.2986), filters())
    assert not ok and reason.startswith("distance")


def test_match_respects_a_wider_walking_radius():
    """The same listing that fails at 10 minutes passes at 40. (It sits ~2 km out,
    which is almost exactly a 30 minute walk, so 30 is too close to call.)"""
    far = listing(lat=49.2986)
    assert not craigslist.match(far, filters())[0]
    assert craigslist.match(far, filters(walk_minutes=40))[0]


def test_match_rejects_too_many_bedrooms():
    ok, reason = craigslist.match(listing(bedrooms=3), filters())
    assert not ok and reason.startswith("bedrooms")


def test_match_accepts_a_studio_when_min_bedrooms_is_zero():
    assert craigslist.match(listing(bedrooms=0), filters())[0]


def test_match_rejects_a_studio_when_a_bedroom_is_required():
    ok, reason = craigslist.match(listing(bedrooms=0), filters(min_bedrooms=1))
    assert not ok and reason.startswith("bedrooms")


def test_match_rejects_prices_over_the_cap():
    ok, reason = craigslist.match(listing(price=3000), filters(max_price=2500))
    assert not ok and reason.startswith("price")


def test_match_rejects_prices_under_the_floor():
    ok, reason = craigslist.match(listing(price=900), filters(min_price=1500))
    assert not ok and reason.startswith("price")


def test_zero_max_price_means_no_cap():
    """0 is the "no limit" sentinel, not a $0 ceiling that rejects everything."""
    assert craigslist.match(listing(price=99000), filters(max_price=0))[0]


def test_match_keeps_listings_with_no_price():
    """Plenty of real postings omit the price. "Ask me" isn't "too expensive"."""
    assert craigslist.match(listing(price=None), filters(max_price=2500))[0]


def test_match_rejects_excluded_keywords_case_insensitively():
    ok, reason = craigslist.match(
        listing(title="ROOM FOR RENT in shared house"), filters(), ["room for rent"]
    )
    assert not ok and "room for rent" in reason


def test_match_ignores_keywords_when_none_are_given():
    """The dashboard re-checks stored rows without keywords, because they already
    passed that gate when they were crawled."""
    assert craigslist.match(listing(title="Room for rent"), filters())[0]


def test_match_does_not_mutate_the_listing():
    """Purity is the point: one crawled row is matched against many saved searches,
    so leaving one search's distance behind would corrupt the next one's answer."""
    item = listing()
    before = dict(item)
    craigslist.match(item, filters())
    assert item == before


# --- matching in bulk -----------------------------------------------------

def test_matching_filters_and_attaches_distance():
    near, far = listing(cl_id="near"), listing(cl_id="far", lat=49.2986)
    out = craigslist.matching([near, far], filters())
    assert [item["cl_id"] for item in out] == ["near"]
    assert 150 < out[0]["distance_m"] < 250


def test_matching_preserves_input_order():
    """The crawl arrives newest first and the per-poll alert cap trims the tail, so
    reordering here would silently drop the newest listings instead of the oldest."""
    items = [listing(cl_id=str(i)) for i in range(5)]
    assert [item["cl_id"] for item in craigslist.matching(items, filters())] == [
        "0", "1", "2", "3", "4"
    ]


def test_matching_returns_nothing_when_everything_is_rejected():
    assert craigslist.matching([listing(bedrooms=4)], filters()) == []


# --- distance helpers -----------------------------------------------------

def test_haversine_matches_a_known_distance():
    """Office to Waterfront Station, about 700 m on the map."""
    d = craigslist.haversine_m(49.2806, -123.1116, 49.2859, -123.1116)
    assert 570 < d < 620


def test_walk_minutes_and_metres_for_walk_round_trip():
    for minutes in (5, 10, 20, 45):
        assert craigslist.walk_minutes(craigslist.metres_for_walk(minutes)) == minutes


def test_distance_from_is_none_without_coordinates():
    assert craigslist.distance_from(listing(lat=None), 49.28, -123.11) is None
