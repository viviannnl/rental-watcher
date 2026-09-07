"""Tests for the digest email.

The point of the digest is that one poll produces one message, so the things worth
pinning down are the truncation and the fact that nothing is sent when there is
nothing to say - both of which are how this stops being spam.
"""

import config
import mailer


def listing(**overrides):
    base = {
        "title": "Bright studio in Yaletown",
        "url": "https://vancouver.craigslist.org/van/apa/d/x/1.html",
        "price": 1850,
        "price_display": "$1,850",
        "bedrooms": 0,
        "distance_m": 500.0,
    }
    return {**base, **overrides}


def items(n, start=1):
    return [(start + i, listing(title=f"Place {i}")) for i in range(n)]


def test_describe_names_a_studio_rather_than_zero_bedrooms():
    assert mailer.describe(listing()) == "$1,850 · studio · 8 min walk"


def test_describe_survives_a_listing_with_no_price_or_location():
    """Craigslist posts turn up with either missing, and a crash here would lose the
    whole digest over one bad row."""
    text = mailer.describe(listing(price=None, price_display=None, distance_m=None))
    assert text == "no price · studio"


def test_one_listing_puts_the_details_in_the_subject():
    subject = mailer.digest_subject(items(1))
    assert "$1,850" in subject and "studio" in subject and "Place 0" in subject


def test_several_listings_are_counted_not_named():
    assert mailer.digest_subject(items(3)) == "3 new rentals near the office"


def test_body_lists_up_to_the_cap_and_counts_the_rest():
    body = mailer.digest_body(items(10), limit=3)
    assert body.count("min walk") == 3
    assert "and 7 more found in this check" in body


def test_body_does_not_claim_extras_when_everything_fits():
    body = mailer.digest_body(items(2), limit=8)
    assert "more found in this check" not in body


def test_body_mentions_what_is_still_unopened():
    assert "4 other listings still unopened" in mailer.digest_body(items(1), waiting=4)
    assert "1 other listing still unopened" in mailer.digest_body(items(1), waiting=1)


def test_body_says_nothing_about_unopened_when_there_is_nothing():
    assert "unopened" not in mailer.digest_body(items(1), waiting=0)


def test_body_links_the_dashboard_and_disowns_replies():
    """Replying used to trigger the intro message. It can't for a digest, and quietly
    swallowing a reply would be worse than saying so."""
    body = " ".join(mailer.digest_body(items(2)).split())
    assert config.DASHBOARD_URL in body
    assert "Replying to this email does nothing" in body


def test_body_includes_the_address_and_year_when_enrichment_found_them():
    body = mailer.digest_body([(7, listing(address="1075 COMOX ST", year_built=1906))])
    assert "1075 COMOX ST, built 1906" in body


def test_send_digest_does_nothing_with_an_empty_batch(monkeypatch):
    """A poll that found nothing must not mail you to say so."""
    monkeypatch.setattr(mailer, "smtp_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert mailer.send_digest([]) is None


def test_send_digest_is_exempt_from_dry_run(monkeypatch):
    """Dry run is about not contacting strangers; this only goes to your own inbox."""
    sent = {}
    monkeypatch.setattr(mailer, "smtp_send", lambda *a, **k: sent.update(k) or "<id>")
    mailer.send_digest(items(2))
    assert sent["even_in_dry_run"] is True


def test_send_digest_uses_the_cap_from_config(monkeypatch):
    monkeypatch.setattr(config, "DIGEST_MAX", 2)
    body = mailer.digest_body(items(5))
    assert body.count("min walk") == 2
