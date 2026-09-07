import logging
import os
import re
import threading
import time

from flask import Flask, request, render_template, abort

import config
import craigslist
import db
import inbox
import mailer
import notifier
import replier
import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app")

app = Flask(__name__)

# Cap how many alerts one poll can fire. Without this, a first run against a
# tweaked filter (wider radius, higher price) would dump a hundred messages at once.
MAX_ALERTS_PER_POLL = 5


def alert(num, listing):
    """Send one listing alert over whichever channels are enabled."""
    sent = []
    if config.NOTIFY_EMAIL:
        mailer.send_alert(num, listing)
        sent.append("email")
    if config.NOTIFY_SMS:
        notifier.send(notifier.listing_alert(num, listing))
        sent.append("sms")
    return sent


def notice(subject, body):
    """Tell yourself something that isn't about a specific listing."""
    if config.NOTIFY_EMAIL:
        mailer.send_notice(subject, body)
    if config.NOTIFY_SMS:
        notifier.send(body)


def poll_once():
    """Fetch listings, store new ones, alert about the ones worth knowing.

    The very first poll seeds the database silently: everything on Craigslist
    right now is already old news, and alerting on 100 of them would be useless.
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
            alert(num, item)
            alerted += 1
        except Exception:
            log.exception("failed to alert about listing %s", num)

    skipped = len(fresh) - alerted
    if skipped > 0:
        log.warning("%d new listings recorded but not alerted (per-poll cap)", skipped)
        try:
            notice(
                f"{skipped} more listings matched",
                f"{skipped} more new listings matched but weren't sent individually "
                f"(cap is {MAX_ALERTS_PER_POLL} per poll). See the dashboard.",
            )
        except Exception:
            log.exception("failed to send overflow notice")

    log.info("poll: %d new, %d alerted", len(fresh), alerted)
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


def act_on_listing(listing):
    """Try to reply to a listing. Returns a sentence describing what happened.

    Shared by both inbound paths, so a text and an email reply behave identically.
    """
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

    return act_on_listing(listing)


def handle_email_replies():
    """Act on any unread replies to alert emails. Returns how many were handled."""
    replies = inbox.fetch_replies()
    for reply in replies:
        listing = db.get(reply["num"])
        if not listing:
            log.warning("email reply references unknown listing #%s", reply["num"])
            continue
        if inbox.declined(reply["body"]):
            log.info("#%s declined by email reply", reply["num"])
            continue

        log.info("acting on email reply for #%s", reply["num"])
        outcome = act_on_listing(listing)
        try:
            mailer.send_notice(f"Re: [#{reply['num']}] {outcome[:60]}", outcome)
        except Exception:
            log.exception("could not confirm outcome for #%s", reply["num"])
    return len(replies)


def inbox_watcher():
    while True:
        try:
            handle_email_replies()
        except Exception:
            log.exception("inbox check failed; will retry")
        time.sleep(config.INBOX_POLL_SECONDS)


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


def _render(errors=(), saved=None):
    return render_template(
        "dashboard.html",
        listings=db.recent(100),
        config=config,
        settings=settings.all_settings(),
        spec=settings.SPEC,
        defaults=settings.DEFAULTS,
        radius_m=settings.radius_m(),
        errors=list(errors),
        saved=saved,
        walk=craigslist.walk_minutes,
        playwright=replier.playwright_available(),
        twilio=notifier.configured(),
        email_ready=mailer.configured(),
        inbox_ready=config.WATCH_INBOX and inbox.configured(),
        placeholders=replier.unfilled_placeholders(),
    )


@app.get("/")
def dashboard():
    return _render()


@app.post("/settings")
def save_settings():
    if request.form.get("reset"):
        settings.reset()
        return _render(saved="Filters reset to the values in .env.")

    changed, errors = settings.update(request.form.to_dict())
    if errors:
        return _render(errors=errors), 400
    if not changed:
        return _render(saved="No changes.")

    # A new office location makes every stored distance wrong, so redo them.
    if "office_lat" in changed or "office_lon" in changed:
        s = settings.all_settings()
        n = db.recompute_distances(s["office_lat"], s["office_lon"], craigslist.haversine_m)
        log.info("recomputed distances for %d listings", n)

    summary = ", ".join(f"{k.replace('_', ' ')} to {v}" for k, v in changed.items())
    return _render(saved=f"Updated {summary}. Applies from the next check onward.")


@app.post("/check-inbox")
def check_inbox():
    return {"handled": handle_email_replies()}


@app.post("/poll")
def manual_poll():
    poll_once()
    return _render(saved="Checked Craigslist.")


@app.post("/reply/<int:num>")
def manual_reply(num):
    """Try unattended first, then fall back to opening a browser to finish by hand."""
    listing = db.get(num)
    if not listing:
        abort(404)
    return _render(saved=act_on_listing(listing))


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
    log.info("alert channel: %s", config.NOTIFY_CHANNEL)
    if config.NOTIFY_EMAIL and not mailer.configured():
        log.warning("email alerts enabled but SMTP is not configured - alerts will only be logged")
    if config.NOTIFY_SMS and not notifier.configured():
        log.warning("sms alerts enabled but Twilio is not configured - alerts will only be logged")
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

    if config.WATCH_INBOX and inbox.configured() and not config.DRY_RUN:
        log.info("watching %s for replies every %ds", config.IMAP_FOLDER, config.INBOX_POLL_SECONDS)
        threading.Thread(target=inbox_watcher, daemon=True).start()
    elif config.WATCH_INBOX and config.DRY_RUN:
        log.info("dry run: not watching the inbox (nothing was emailed to reply to)")

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))


if __name__ == "__main__":
    main()
