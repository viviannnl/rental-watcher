"""Sending mail: listing alerts to you, and intro messages to landlords.

Email is the default alert channel because it needs no paid account, no phone
number and no public webhook. Replies come back through IMAP (see inbox.py), so
the whole loop runs over one Gmail App Password.
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


def smtp_send(to_addr, subject, body, reply_to=None):
    """Send one plain-text message. Returns the Message-ID, or None in dry run."""
    if config.DRY_RUN:
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


def alert_subject(num, listing):
    """Subject line carrying the listing number.

    inbox.py finds the number in the "Re: [#12] ..." subject of your reply, which
    is how a reply gets matched back to a listing. Keep [#N] at the front.
    """
    beds = "studio" if listing.get("bedrooms") == 0 else f"{listing.get('bedrooms')}br"
    return f"[#{num}] {listing['price_display']} {beds} - {listing['title'][:70]}"


def alert_body(num, listing):
    metres = round(listing["distance_m"])
    return (
        f"{listing['title']}\n\n"
        f"  Price:    {listing['price_display']}\n"
        f"  Distance: {metres} m from the office (~{walk_minutes(metres)} min walk)\n\n"
        f"{listing['url']}\n\n"
        f"---\n"
        f"Reply to this email to send your intro message to the poster.\n"
        f"Anything you type is ignored; replying at all is the go-ahead.\n"
        f"Reply with \"no\" or \"skip\" to do nothing."
    )


def send_alert(num, listing, to=None):
    return smtp_send(to or config.ALERT_EMAIL, alert_subject(num, listing), alert_body(num, listing))


def send_notice(subject, body, to=None):
    """A plain message to yourself that isn't about one listing."""
    return smtp_send(to or config.ALERT_EMAIL, subject, body)
