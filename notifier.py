import logging

import config
from craigslist import walk_minutes

log = logging.getLogger(__name__)

_client = None


def client():
    """Twilio client, preferring a restricted API key over the account Auth Token.

    An API key can be scoped to just "send a message" and revoked on its own,
    whereas the Auth Token is full account access. Note this only narrows the
    sending path: validating inbound webhook signatures still needs the Auth
    Token, because that is what Twilio signs its requests with.
    """
    global _client
    if _client is None:
        from twilio.rest import Client

        if config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET:
            _client = Client(
                config.TWILIO_API_KEY_SID,
                config.TWILIO_API_KEY_SECRET,
                config.TWILIO_ACCOUNT_SID,
            )
        else:
            _client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def configured():
    has_credential = bool(config.TWILIO_AUTH_TOKEN) or bool(
        config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET
    )
    return bool(
        config.TWILIO_ACCOUNT_SID and config.TWILIO_FROM and config.MY_PHONE and has_credential
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
