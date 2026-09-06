import logging

import config
from craigslist import walk_minutes

log = logging.getLogger(__name__)

_client = None


def client():
    global _client
    if _client is None:
        from twilio.rest import Client

        _client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def configured():
    return all(
        [config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN, config.TWILIO_FROM, config.MY_PHONE]
    )


def send(body, to=None):
    to = to or config.MY_PHONE
    if config.DRY_RUN or not configured():
        log.info("[dry-run sms to %s]\n%s", to, body)
        return None
    msg = client().messages.create(body=body, from_=config.TWILIO_FROM, to=to)
    log.info("sent sms %s to %s", msg.sid, to)
    return msg.sid


def listing_alert(num, listing):
    """The new-listing text. Kept under ~320 chars so it stays 1-2 SMS segments."""
    beds = "studio" if listing.get("bedrooms") == 0 else f"{listing.get('bedrooms')}br"
    metres = round(listing["distance_m"])
    return (
        f"[#{num}] {listing['price_display']} {beds} - {listing['title'][:60]}\n"
        f"{metres}m from YVR14 (~{walk_minutes(metres)} min walk)\n"
        f"{listing['url']}\n"
        f"Reply {num} to send your intro message."
    )
