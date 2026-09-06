import logging
import os
import re
import threading
import time

from flask import Flask, request, render_template, abort

import config
import craigslist
import db
import notifier
import replier

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app")

app = Flask(__name__)

# Cap how many texts one poll can fire. Without this, a first run against a
# tweaked filter (wider radius, higher price) would dump a hundred texts at once.
MAX_ALERTS_PER_POLL = 5


def poll_once():
    """Fetch listings, store new ones, text about the ones worth knowing.

    The very first poll seeds the database silently: everything on Craigslist
    right now is already old news, and texting 100 of them would be useless.
    """
    listings = craigslist.search()
    seeded = db.get_meta("seeded") == "1"
    known = db.known_ids()
    fresh = [item for item in listings if item["cl_id"] not in known]

    if not seeded:
        for item in fresh:
            db.add(item, notified=False)
        db.set_meta("seeded", "1")
        log.info("first run: seeded %d existing listings without alerting", len(fresh))
        return 0

    # craigslist.search() returns newest first, so when we hit the per-poll cap
    # the listings we drop are the older ones.
    alerted = 0
    for item in fresh:
        should_alert = alerted < MAX_ALERTS_PER_POLL
        num = db.add(item, notified=should_alert)
        if num is None or not should_alert:
            continue
        try:
            notifier.send(notifier.listing_alert(num, item))
            alerted += 1
        except Exception:
            log.exception("failed to text about listing %s", num)

    skipped = len(fresh) - alerted
    if skipped > 0:
        log.warning("%d new listings recorded but not texted (per-poll cap)", skipped)
        try:
            notifier.send(
                f"+{skipped} more new listings matched but weren't texted "
                f"(cap {MAX_ALERTS_PER_POLL}/poll). See the dashboard."
            )
        except Exception:
            log.exception("failed to send overflow notice")

    log.info("poll: %d new, %d texted", len(fresh), alerted)
    return alerted


def poller():
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("poll failed; will retry next interval")
        time.sleep(config.POLL_MINUTES * 60)


def _validate_twilio(req):
    """Reject forged webhook calls. The endpoint is public, so this matters."""
    if config.DRY_RUN or not config.TWILIO_AUTH_TOKEN:
        return True
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    # Twilio signs the URL it called; behind a tunnel that is the forwarded scheme.
    url = req.url.replace("http://", "https://", 1)
    return validator.validate(url, req.form.to_dict(), req.headers.get("X-Twilio-Signature", ""))


def _twiml(text):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
        200,
        {"Content-Type": "application/xml"},
    )


def handle_inbound(body):
    """Interpret an inbound text and act. Returns the reply text."""
    text = (body or "").strip()

    match = re.search(r"\d+", text)
    if match:
        listing = db.get(int(match.group(0)))
        if not listing:
            return f"No listing #{match.group(0)}. Text the number from an alert."
    elif re.match(r"^(y|yes|ok|send|reply)\b", text, re.I):
        listing = db.latest_notified()
        if not listing:
            return "Nothing to reply to yet."
    else:
        return "Text a listing number (e.g. 12) to send your intro, or Y for the latest."

    if listing["replied_at"]:
        return f"Already replied to #{listing['num']} at {listing['replied_at']}."

    ok, note = replier.auto_reply(listing)
    if ok:
        db.mark_replied(listing["num"], note)
        return f"Sent your message for #{listing['num']} ({note})."

    # Craigslist blocks unattended replies behind a captcha, so hand it off to a
    # visible browser on the machine running this and tell the user to finish it.
    if replier.playwright_available():
        threading.Thread(
            target=_assisted, args=(listing,), daemon=True, name=f"assist-{listing['num']}"
        ).start()
        return (
            f"#{listing['num']}: couldn't send unattended ({note}). Opening the reply "
            f"form with your message filled in - finish it on your laptop."
        )
    return (
        f"Couldn't auto-reply to #{listing['num']}: {note}. "
        f"Reply by hand: {listing['url']}"
    )


def _assisted(listing):
    """Run assisted reply off the request thread; it waits on a human."""
    try:
        ok, note = replier.assisted_reply(listing)
        if ok:
            db.mark_replied(listing["num"], f"assisted: {note}")
    except Exception:
        log.exception("assisted reply for #%s failed", listing["num"])


@app.post("/sms")
def sms():
    if not _validate_twilio(request):
        log.warning("rejected webhook with bad Twilio signature")
        abort(403)
    try:
        return _twiml(handle_inbound(request.form.get("Body", "")))
    except Exception:
        log.exception("inbound sms handling failed")
        return _twiml("Something broke handling that. Check the server logs.")


@app.get("/")
def dashboard():
    return render_template(
        "dashboard.html",
        listings=db.recent(100),
        config=config,
        walk=craigslist.walk_minutes,
        playwright=replier.playwright_available(),
        twilio=notifier.configured(),
        placeholders=replier.unfilled_placeholders(),
    )


@app.post("/poll")
def manual_poll():
    return {"alerted": poll_once()}


@app.post("/reply/<int:num>")
def manual_reply(num):
    """Try unattended first, then fall back to opening a browser to finish by hand."""
    listing = db.get(num)
    if not listing:
        abort(404)
    ok, note = replier.auto_reply(listing)
    if ok:
        db.mark_replied(num, note)
        return {"ok": True, "note": note}
    if replier.playwright_available():
        threading.Thread(target=_assisted, args=(listing,), daemon=True).start()
        return {"ok": False, "note": f"{note}; opening a browser to finish by hand"}
    return {"ok": False, "note": note}


@app.post("/assist/<int:num>")
def assist(num):
    listing = db.get(num)
    if not listing:
        abort(404)
    threading.Thread(target=_assisted, args=(listing,), daemon=True).start()
    return {"ok": True, "note": "opening a browser window"}


def main():
    log.info(
        "watching %sbr-%sbr within %dm of (%.4f, %.4f), every %d min",
        config.MIN_BEDROOMS, config.MAX_BEDROOMS, config.RADIUS_M,
        config.OFFICE_LAT, config.OFFICE_LON, config.POLL_MINUTES,
    )
    if not notifier.configured():
        log.warning("Twilio not configured - alerts will only be logged")
    if "{availability}" not in config.REPLY_TEMPLATE:
        log.warning(
            "REPLY_MESSAGE has no {availability} placeholder, so AVAILABILITY (%r) "
            "will not appear in your replies",
            config.AVAILABILITY,
        )
    missing = replier.unfilled_placeholders()
    if missing:
        log.warning(
            "REPLY_MESSAGE still contains %s - unattended replies are blocked until "
            "you fill these in",
            ", ".join(missing),
        )
    threading.Thread(target=poller, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))


if __name__ == "__main__":
    main()
