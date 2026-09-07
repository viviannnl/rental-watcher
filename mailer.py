"""Sending mail: one digest of new listings to you, and intro messages to landlords.

A poll sends at most one email, listing the places it found. It used to send one per
listing, which turned a normal evening into six messages that each said less than the
dashboard already showed. The digest is truncated (DIGEST_MAX), because past the first
handful you are going to open the dashboard anyway - so the email's job is to tell you
there is something worth looking at, not to be the thing you look at.
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import config
from craigslist import walk_minutes

log = logging.getLogger(__name__)


def configured():
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS)


def smtp_send(to_addr, subject, body, reply_to=None, even_in_dry_run=False):
    """Send one plain-text message. Returns the Message-ID, or None in dry run.

    DRY_RUN exists so you can run this without contacting a stranger, so mail to your
    own address is exempt when the caller says so - otherwise the only way to receive
    your own digest would be to also arm real replies to landlords.
    """
    if config.DRY_RUN and not even_in_dry_run:
        log.info("[dry-run email to %s]\nSubject: %s\n\n%s\n", to_addr, subject, body)
        return None
    if not configured():
        raise RuntimeError("SMTP is not configured (set SMTP_HOST/SMTP_USER/SMTP_PASS)")

    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)
    log.info("emailed %r to %s", subject, to_addr)
    return msg["Message-ID"]


def describe(listing):
    """The one-line gist of a listing: what it costs, how big, how far."""
    beds = listing.get("bedrooms")
    parts = [listing.get("price_display") or "no price"]
    if beds is not None:
        parts.append("studio" if beds == 0 else f"{beds}br")
    if listing.get("distance_m") is not None:
        parts.append(f"{walk_minutes(round(listing['distance_m']))} min walk")
    return " · ".join(parts)


def digest_subject(items):
    """One listing gets its details in the subject; several get counted.

    With one find, the subject can say the whole thing and you never open anything.
    With six, no subject can, so it shouldn't pretend by naming an arbitrary one.
    """
    if len(items) == 1:
        _num, listing = items[0]
        return f"New rental: {describe(listing)} - {listing['title'][:60]}"
    return f"{len(items)} new rentals near the office"


def digest_body(items, waiting=0, limit=None):
    """The digest, newest first and truncated. `waiting` is what's unseen beyond these."""
    limit = config.DIGEST_MAX if limit is None else limit
    lines = []
    for num, listing in items[:limit]:
        lines.append(f"#{num}  {describe(listing)}")
        lines.append(f"    {(listing.get('title') or '').strip()[:70]}")
        if listing.get("address"):
            extra = listing["address"]
            if listing.get("year_built"):
                extra += f", built {listing['year_built']}"
            lines.append(f"    {extra}")
        lines.append(f"    {listing.get('url') or ''}")
        lines.append("")

    hidden = len(items) - len(items[:limit])
    if hidden:
        lines.append(f"...and {hidden} more found in this check.")
    if waiting > 0:
        lines.append(f"{waiting} other listing{'' if waiting == 1 else 's'} still unopened.")

    return "\n".join(lines).rstrip() + (
        f"\n\n---\n{config.DASHBOARD_URL}\n"
        "Hit Send on a row there to fire off your intro message. Replying to this email\n"
        "does nothing - a digest has no single poster to forward your words to."
    )


def send_digest(items, waiting=0, to=None):
    """Mail yourself one summary of what a poll turned up. `items` is [(num, listing)]."""
    if not items:
        return None
    return smtp_send(
        to or config.ALERT_EMAIL,
        digest_subject(items),
        digest_body(items, waiting),
        even_in_dry_run=True,
    )


def send_notice(subject, body, to=None):
    """A plain message to yourself that isn't about one listing."""
    return smtp_send(to or config.ALERT_EMAIL, subject, body)
