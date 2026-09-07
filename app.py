import datetime
import logging
import os
import re
import threading
import time

from flask import Flask, request, render_template, abort

import config
import craigslist
import db
import enrich
import inbox
import mailer
import replier
import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app")

app = Flask(__name__)

# A poll can find any number of listings: they become rows on a page and at most one
# email, so volume costs nothing. What is still worth capping is enrichment, because
# each lookup costs a page fetch plus an open-data query. The rest fill in on demand
# when you click. The newest are done first, so the digest is the enriched end.
ENRICH_PER_POLL = 5


def add_building_info(num, listing):
    """Attach address and year built, and remember them. Never fatal.

    Only called for listings we're about to alert on, which the per-poll cap keeps
    to a handful, since each one costs a page fetch plus maybe an open-data query.
    """
    if not config.LOOKUP_YEAR_BUILT:
        return
    try:
        year, source, address = enrich.year_built(listing)
    except Exception:
        log.exception("year-built lookup failed for #%s", num)
        return
    listing["year_built"], listing["year_source"], listing["address"] = year, source, address
    db.save_building_info(num, address, year, source)


def poll_once():
    """Fetch listings, store new ones, and mark the matching ones as new to look at.

    Everything found is announced on the dashboard and stays marked new until you've
    seen it. On top of that a poll sends at most one email summarising the batch -
    where it used to send one per listing plus an overflow notice, which meant six
    messages saying what one could.

    The very first poll seeds the database silently: everything on Craigslist right
    now is already old news, and flagging 300 of them as new would be useless.
    """
    s = settings.all_settings()

    # A search wider than the crawl can never be answered fully, and it would fail
    # quietly: the table would just stop gaining places past the crawl edge.
    crawl_m = config.CRAWL_RADIUS_KM * 1000
    if settings.radius_m() > crawl_m:
        log.warning(
            "your %d min walk reaches %dm but the crawl only covers %dm - listings "
            "beyond that are invisible. Raise CRAWL_RADIUS_KM.",
            s["walk_minutes"], settings.radius_m(), crawl_m,
        )

    crawled = craigslist.crawl()
    listings = craigslist.matching(crawled, s, config.EXCLUDE_KEYWORDS)
    seeded = db.get_meta("seeded") == "1"
    known = db.known_ids()
    fresh = [item for item in listings if item["cl_id"] not in known]

    # Craigslist caps a crawl at 360 postings with no way to page back further. If a
    # whole crawl is both full and entirely unseen, the window turned over between
    # polls and something in the gap was missed - poll more often or crawl a smaller
    # area. Only meaningful once seeded, since the first crawl is new by definition.
    if seeded and len(crawled) >= craigslist.BATCH_SIZE:
        if all(item["cl_id"] not in known for item in crawled):
            log.warning(
                "crawl returned a full %d postings and none were known: listings may "
                "have been missed. Lower POLL_MINUTES (now %d) or CRAWL_RADIUS_KM (now %d).",
                craigslist.BATCH_SIZE, config.POLL_MINUTES, config.CRAWL_RADIUS_KM,
            )

    if not seeded:
        for item in fresh:
            db.add(item, notified=False)
        db.set_meta("seeded", "1")
        log.info("first run: seeded %d existing listings without flagging them", len(fresh))
        return 0

    # The crawl comes back newest first, so the enrichment budget goes to the newest
    # listings rather than whichever ones happen to be at the end of the list.
    new_items, enriched = [], 0
    for item in fresh:
        num = db.add(item, notified=True)
        if num is None:
            continue
        # Recorded against the search, not the listing: two searches can both need
        # telling about the same place, which a column on the listing can't express.
        # seen_at stays NULL, which is what makes the row show as new.
        db.record_alert(settings.SEARCH_ID, num)
        if enriched < ENRICH_PER_POLL:
            try:
                add_building_info(num, item)
                enriched += 1
            except Exception:
                log.exception("failed to enrich listing %s", num)
        # After enrichment, so the digest can quote the address and year.
        new_items.append((num, item))

    unseen = len(db.unseen_nums(settings.SEARCH_ID))
    log.info(
        "poll: %d new (%d enriched), %d waiting to be looked at",
        len(new_items), enriched, unseen,
    )
    send_digest(new_items, waiting=unseen - len(new_items))
    return len(new_items)


def send_digest(new_items, waiting=0):
    """One email for the whole poll, or none at all. Never fatal.

    A failed send must not fail the poll: the listings are already stored and on the
    dashboard, so losing the email costs a nudge, not data.
    """
    if not new_items or not config.EMAIL_DIGEST:
        return
    if not mailer.configured():
        log.info("%d new listings, but SMTP isn't set up - see the dashboard", len(new_items))
        return
    try:
        mailer.send_digest(new_items, waiting=max(waiting, 0))
    except Exception:
        log.exception("couldn't send the digest; the listings are on the dashboard anyway")


def _record_poll():
    """Stamp the time of a completed poll so the dashboard can prove it's alive."""
    db.set_meta("last_poll", datetime.datetime.now().isoformat(timespec="seconds"))


def poller():
    while True:
        try:
            poll_once()
            _record_poll()
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


# Column -> sort key. Rows missing the value sort last either way, so an
# unpriced listing never masquerades as the cheapest.
SORTS = {
    "new": lambda l: -l["num"],
    "walk": lambda l: (l["distance_m"] is None, l["distance_m"] or 0),
    "price": lambda l: (l["price"] is None, l["price"] or 0),
}

# How far back the dashboard looks. Bounded so the page can't get slower forever,
# but wide enough that the counts on the Matching/All toggle are the real totals.
WINDOW = 500


def mark_reposts(listings):
    """Flag rows that are the same unit relisted, keeping the newest as canonical.

    Landlords repost the same place every few days, and Craigslist mints a new id
    each time, so the dedupe on cl_id can't see it. Sorting by walking distance made
    this obvious: identical title, price and distance, three rows apart. Rather than
    guess and hide, the newest row says how many times it has appeared.

    Expects newest-first input, which is what db.recent gives.
    """
    canonical = {}
    for item in listings:
        title = re.sub(r"[^a-z0-9]+", "", (item["title"] or "").lower())[:40]
        # Coordinates wobble slightly between postings, so bucket to 25 m.
        key = (item["price"], round((item["distance_m"] or 0) / 25), title)
        first = canonical.get(key)
        if first is None:
            canonical[key] = item
            item["repost"], item["repeats"] = False, 0
        else:
            item["repost"], item["repeats"] = True, 0
            first["repeats"] += 1


def _ago(iso):
    """'4 min ago' for a stored local timestamp, or None if there isn't one."""
    if not iso:
        return None
    try:
        then = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    seconds = (datetime.datetime.now() - then).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def _render(errors=(), saved=None, clear_new=False):
    s = settings.all_settings()
    radius = settings.radius_m()

    # Whole-table window: the counts on the toggle have to be true totals, not
    # "true within the first page", or they mislead more than they inform.
    listings = db.recent(WINDOW)
    # Rows outlive the filters that found them - widen the radius, look around, narrow
    # it again, and the table keeps the strays. Marking them beats silently dropping
    # them, because a place you already replied to shouldn't vanish. Same matcher the
    # poll uses, so the table can't disagree with the poll; keywords are left off
    # because these rows already passed that gate when they were crawled.
    unseen = db.unseen_nums(settings.SEARCH_ID)
    for item in listings:
        item["matches"] = craigslist.match(item, s)[0]
        item["is_new"] = item["num"] in unseen
    mark_reposts(listings)
    total = len(listings)
    matching = sum(1 for item in listings if item["matches"] and not item["repost"])

    show = request.args.get("show", "match")
    sort = request.args.get("sort", "new")
    if show == "match":
        listings = [item for item in listings if item["matches"] and not item["repost"]]
    listings.sort(key=SORTS.get(sort, SORTS["new"]))
    shown = listings[:100]
    new_count = sum(1 for item in shown if item["is_new"])

    last_poll = db.get_meta("last_poll")
    # Twice the interval is the grace period: one skipped cycle is a blip, two
    # means something is wrong and the dot should stop claiming otherwise.
    stale_after = config.POLL_MINUTES * 120
    healthy = False
    if last_poll:
        try:
            age = (datetime.datetime.now() - datetime.datetime.fromisoformat(last_poll))
            healthy = age.total_seconds() < stale_after
        except ValueError:
            pass

    html = render_template(
        "dashboard.html",
        listings=shown,
        new_count=new_count,
        total=total,
        matching=matching,
        show=show,
        sort=sort,
        qs=request.query_string.decode(),
        last_poll=_ago(last_poll),
        healthy=healthy,
        config=config,
        settings=s,
        spec=settings.SPEC,
        notes=settings.NOTES,
        defaults=settings.DEFAULTS,
        radius_m=radius,
        errors=list(errors),
        saved=saved,
        walk=craigslist.walk_minutes,
        playwright=replier.playwright_available(),
        placeholders=replier.unfilled_placeholders(),
    )

    # Cleared after rendering, so the badges appear on the page that clears them and
    # are gone by the next load. Only the rows actually on the page count as looked
    # at: anything past the 100-row cut, or hidden by the Matching toggle, wasn't
    # shown to you and shouldn't quietly stop being new.
    if clear_new and new_count:
        db.mark_seen(settings.SEARCH_ID, [item["num"] for item in shown if item["is_new"]])
    return html


@app.get("/")
def dashboard():
    return _render(clear_new=True)


@app.post("/settings")
def save_settings():
    if request.form.get("reset"):
        before = settings.all_settings()
        settings.reset()
        after = settings.all_settings()
        dropped = {k: before[k] for k in before if before[k] != after[k]}
        log.info("filters reset to .env, discarding %s", dropped or "nothing")
        return _render(
            saved="Reset to the values in .env."
            + (f" Discarded {', '.join(k.replace('_', ' ') for k in dropped)}." if dropped else "")
        )

    changed, errors = settings.update(request.form.to_dict())
    if errors:
        return _render(errors=errors), 400
    if not changed:
        return _render(saved="Already saved - nothing to change.")

    # A new office location makes every stored distance wrong, so redo them.
    if "office_lat" in changed or "office_lon" in changed:
        s = settings.all_settings()
        n = db.recompute_distances(s["office_lat"], s["office_lon"], craigslist.haversine_m)
        log.info("recomputed distances for %d listings", n)

    summary = ", ".join(f"{k.replace('_', ' ')} to {v}" for k, v in changed.items())
    return _render(saved=f"Saved {summary}. This sticks until you change it again.")


@app.post("/check-inbox")
def check_inbox():
    return {"handled": handle_email_replies()}


@app.post("/poll")
def manual_poll():
    found = poll_once()
    _record_poll()
    return _render(
        saved=f"Checked Craigslist - {found} new listing{'' if found == 1 else 's'} found."
    )


@app.post("/reply/<int:num>")
def manual_reply(num):
    """Try unattended first, then fall back to opening a browser to finish by hand.

    Answers JSON when asked, so the row can report back where you clicked instead of
    re-rendering and throwing you to the top of a hundred-row table.
    """
    listing = db.get(num)
    if not listing:
        abort(404)
    outcome = act_on_listing(listing)
    if request.accept_mimetypes.best == "application/json":
        fresh = db.get(num)
        return {"num": num, "note": outcome, "replied": bool(fresh["replied_at"])}
    return _render(saved=outcome)


@app.post("/building/<int:num>")
def building(num):
    """Look up one listing's age on demand, for rows the poll didn't cover.

    Answers JSON when the page asks for it, so the dashboard can update the one
    cell in place. Re-rendering the whole page would scroll you back to the top,
    away from the row you were looking at. The form still works without JS.
    """
    listing = db.get(num)
    if not listing:
        abort(404)
    add_building_info(num, dict(listing))
    fresh = db.get(num)

    if fresh["year_built"]:
        note = f"#{num} was built in {fresh['year_built']} (per {fresh['year_source']})."
    elif fresh["address"]:
        note = f"#{num}: no record found for {fresh['address']}."
    else:
        note = f"#{num}: the posting gives no street address, so there's nothing to look up."

    if request.accept_mimetypes.best == "application/json":
        return {
            "num": num,
            "year_built": fresh["year_built"],
            "year_source": fresh["year_source"],
            "address": fresh["address"],
            "note": note,
        }
    return _render(saved=note)


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
    if config.EMAIL_DIGEST and mailer.configured():
        log.info(
            "new listings appear in the dashboard, plus one digest email per check to %s "
            "(at most %d listed)%s",
            config.ALERT_EMAIL, config.DIGEST_MAX,
            " - sent for real even in dry run, since it only goes to you" if config.DRY_RUN else "",
        )
    else:
        log.info("new listings appear in the dashboard; no email is sent")
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

    # Off by default now. It exists to let you reply to an alert email, and there are
    # no alert emails - leaving it on meant every reply you sent triggered a captcha
    # attempt and mailed you back about it, which is exactly the noise this removed.
    if config.WATCH_INBOX and inbox.configured() and not config.DRY_RUN:
        log.info("watching %s for replies every %ds", config.IMAP_FOLDER, config.INBOX_POLL_SECONDS)
        threading.Thread(target=inbox_watcher, daemon=True).start()

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))


if __name__ == "__main__":
    main()
