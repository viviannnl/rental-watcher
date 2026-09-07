"""Watching your inbox for replies to listing alerts.

This is the email equivalent of an inbound SMS webhook, and the reason the email
setup needs no tunnel or public URL: instead of Twilio pushing a request to you,
we connect out to IMAP and look for unread replies. Same Gmail App Password as
sending.

A reply is matched to a listing by the "[#12]" tag the alert put in the subject,
which survives the "Re: " your mail client prepends.
"""

import email
import imaplib
import logging
import re

import config

log = logging.getLogger(__name__)

SUBJECT_NUM_RE = re.compile(r"\[#(\d+)\]")
# Typing one of these instead of just replying means "leave it alone".
DECLINE_WORDS = ("no", "skip", "nope", "stop", "ignore", "don't", "dont")


def configured():
    return bool(config.IMAP_HOST and config.SMTP_USER and config.SMTP_PASS)


def _body_text(msg):
    """First text/plain part, decoded as forgivingly as possible."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def _quoted_stripped(text):
    """Drop quoted history so we only read what was actually typed."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("On ") and stripped.endswith("wrote:"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def declined(body):
    """True if the reply looks like an explicit 'don't send'."""
    first = _quoted_stripped(body).lower().strip()
    if not first:
        return False
    opening = re.split(r"[\s,.!]+", first)[0]
    return opening in DECLINE_WORDS


def fetch_replies():
    """Unread replies to alerts, as [{num, from, body, uid}]. Marks them read.

    Only messages from your own address count, so a stranger who somehow learns
    the subject format can't make the app email landlords on your behalf.
    """
    if not configured():
        return []

    out = []
    try:
        conn = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    except OSError as exc:
        log.error("could not reach %s: %s", config.IMAP_HOST, exc)
        return []

    try:
        try:
            conn.login(config.SMTP_USER, config.SMTP_PASS)
        except imaplib.IMAP4.error as exc:
            # Nearly always a bad App Password, or IMAP disabled on the account.
            log.error(
                "IMAP login failed for %s (%s). Check SMTP_USER and that SMTP_PASS is "
                "a Gmail App Password, not your account password.",
                config.SMTP_USER, exc,
            )
            return []
        conn.select(config.IMAP_FOLDER)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            log.warning("imap search failed: %s", typ)
            return []

        for uid in data[0].split():
            typ, raw = conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])

            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
            match = SUBJECT_NUM_RE.search(subject)
            if not match:
                continue  # not one of our alerts; leave it unread

            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            allowed = {config.ALERT_EMAIL.lower(), config.SMTP_USER.lower()}
            if sender not in allowed:
                log.warning("ignoring reply to %s from unexpected sender %s", subject, sender)
                continue

            out.append(
                {
                    "num": int(match.group(1)),
                    "from": sender,
                    "subject": subject,
                    "body": _body_text(msg),
                }
            )
            conn.store(uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out
