"""Sending mail: one digest of new listings to you, and intro messages to landlords.

A poll sends at most one email, listing the places it found. It used to send one per
listing, which turned a normal evening into six messages that each said less than the
dashboard already showed. The digest is truncated (DIGEST_MAX), because past the first
handful you are going to open the dashboard anyway - so the email's job is to tell you
there is something worth looking at, not to be the thing you look at.
"""

import html
import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import config
from craigslist import walk_minutes

log = logging.getLogger(__name__)


def configured():
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS)


def smtp_send(to_addr, subject, body, reply_to=None, even_in_dry_run=False, html_body=None):
    """Send one message. Returns the Message-ID, or None in dry run.

    `body` is the plain-text part and stays the source of truth; `html_body`, when
    given, rides along as a prettier alternative that clients show when they can and
    silently ignore when they can't. Adding it changes nothing about who is mailed or
    when - it's presentation only.

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
    if html_body:
        msg.add_alternative(html_body, subtype="html")

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


# A warm, light palette that mirrors the dashboard, spelled out here because email
# has no shared stylesheet - every colour has to be inlined on the element.
_C = {
    "bg": "#fdf6ec", "card": "#fffaf3", "ink": "#3a2c22", "ink2": "#7a6a5c",
    "ink3": "#a08a76", "line": "#ecdcc6", "accent": "#e0743b", "accent_deep": "#c15a24",
    "accent_soft": "#fdeadd",
}


def _row_html(num, listing):
    """One listing as an email-safe card. Inline styles only - no <style>, no classes,
    since many mail clients strip both."""
    c = _C
    title = html.escape((listing.get("title") or "").strip())
    url = html.escape(listing.get("url") or "", quote=True)
    price = listing.get("price_display") or "No price"
    # Price is already the headline of the card, so the fact line drops it and keeps
    # just the size and distance.
    bits = []
    beds = listing.get("bedrooms")
    if beds is not None:
        bits.append("studio" if beds == 0 else f"{beds}br")
    if listing.get("distance_m") is not None:
        bits.append(f"{walk_minutes(round(listing['distance_m']))} min walk")
    facts = html.escape(" · ".join(bits))
    addr = listing.get("address") or ""
    if listing.get("year_built"):
        addr = (addr + ", " if addr else "") + f"built {listing['year_built']}"
    addr = html.escape(addr)

    new_badge = (
        f'<span style="background:{c["accent"]};color:#fff;font-size:11px;font-weight:700;'
        f'padding:2px 8px;border-radius:999px;">NEW</span>'
    )
    title_html = (
        f'<a href="{url}" style="color:{c["ink"]};text-decoration:none;font-weight:600;'
        f'font-size:15px;">{title}</a>' if url else
        f'<span style="color:{c["ink"]};font-weight:600;font-size:15px;">{title}</span>'
    )
    addr_html = (
        f'<div style="color:{c["ink3"]};font-size:12px;padding-top:4px;">{addr}</div>'
        if addr else ""
    )
    return f"""
    <tr><td style="padding:0 0 14px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{c['card']};border:1px solid {c['line']};border-radius:14px;
                    border-top:3px solid {c['accent']};">
        <tr><td style="padding:16px 18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:Georgia,'Times New Roman',serif;font-size:24px;
                       font-weight:400;color:{c['ink']};">{html.escape(price)}</td>
            <td align="right">{new_badge}</td>
          </tr></table>
          <div style="color:{c['accent_deep']};font-size:13px;font-weight:600;padding:8px 0 6px;">{facts}</div>
          {title_html}
          {addr_html}
          <div style="padding-top:12px;">
            <a href="{url}" style="background:{c['accent']};color:#fff;text-decoration:none;
               font-size:13px;font-weight:700;padding:9px 16px;border-radius:10px;
               display:inline-block;">View listing &rarr;</a>
          </div>
        </td></tr>
      </table>
    </td></tr>"""


def digest_html(items, waiting=0, limit=None):
    """The digest as warm HTML - same content and truncation as the text version."""
    limit = config.DIGEST_MAX if limit is None else limit
    c = _C
    shown = items[:limit]
    rows = "".join(_row_html(num, listing) for num, listing in shown)

    hidden = len(items) - len(shown)
    tail = []
    if hidden:
        tail.append(f"&hellip;and {hidden} more found in this check.")
    if waiting > 0:
        tail.append(f"{waiting} other listing{'' if waiting == 1 else 's'} still unopened.")
    tail_html = (
        f'<div style="color:{c["ink3"]};font-size:13px;padding:4px 0 18px;text-align:center;">'
        + "<br>".join(tail) + "</div>"
    ) if tail else ""

    n = len(items)
    heading = "1 new place near the office" if n == 1 else f"{n} new places near the office"
    dash = html.escape(config.DASHBOARD_URL, quote=True)

    return f"""\
<!doctype html><html><body style="margin:0;padding:0;background:{c['bg']};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{c['bg']};">
<tr><td align="center" style="padding:28px 16px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
    <tr><td style="padding:0 4px 20px;">
      <div style="font-family:Georgia,'Times New Roman',serif;font-size:26px;font-style:italic;
                  color:{c['ink']};">Rental Watcher</div>
      <div style="color:{c['ink2']};font-size:14px;padding-top:6px;">{heading} &mdash; the moment they posted.</div>
    </td></tr>
    {rows}
    <tr><td>{tail_html}</td></tr>
    <tr><td align="center" style="padding:6px 0 22px;">
      <a href="{dash}" style="background:{c['card']};border:1px solid {c['accent']};
         color:{c['accent_deep']};text-decoration:none;font-size:14px;font-weight:700;
         padding:12px 22px;border-radius:12px;display:inline-block;">Open the dashboard &rarr;</a>
    </td></tr>
    <tr><td style="border-top:1px solid {c['line']};padding:16px 4px 0;">
      <div style="color:{c['ink3']};font-size:12px;line-height:1.5;">
        Hit <b style="color:{c['ink2']};">Send</b> on a card in the dashboard to fire off your intro message.
        Replying to this email does nothing &mdash; a digest has no single poster to forward your words to.
      </div>
    </td></tr>
  </table>
</td></tr></table></body></html>"""


def send_digest(items, waiting=0, to=None):
    """Mail yourself one summary of what a poll turned up. `items` is [(num, listing)]."""
    if not items:
        return None
    return smtp_send(
        to or config.ALERT_EMAIL,
        digest_subject(items),
        digest_body(items, waiting),
        even_in_dry_run=True,
        html_body=digest_html(items, waiting),
    )


def send_notice(subject, body, to=None):
    """A plain message to yourself that isn't about one listing."""
    return smtp_send(to or config.ALERT_EMAIL, subject, body)
