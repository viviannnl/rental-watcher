"""Replying to a Craigslist post.

Read this before expecting the reply feature to be fully automatic — it can't be,
and here is exactly why (verified against the live site, Sept 2026):

  * The "reply" button on a post carries a placeholder URL,
    .../reply/van/apa/<postingId>/__SERVICE_ID__, and the real path is
    .../reply/van/apa/<postingId>/init.
  * Requesting that URL over plain HTTP returns 404, even with the post's cookies.
    It only resolves inside a session their JS has established.
  * Driving it in a browser does resolve, and then loads hCaptcha with an
    image-selection challenge before it will reveal the anonymised relay address.

So there is no way to post a reply unattended without defeating a captcha, which
this project will not do. What it does instead:

  ASSISTED MODE (assisted_reply)
    Opens a real, visible browser window using a persistent profile, navigates to
    the post, opens the reply form and pre-fills your message. You solve the
    captcha if it appears and press send. Two taps instead of typing it all out.

  AUTO ATTEMPT (auto_reply)
    Retries headlessly against that same persistent profile. hCaptcha often stops
    challenging a profile it has already seen pass, so once you have used assisted
    mode a few times this can start succeeding on its own. Treat any success as a
    bonus; the code always falls back to telling you to reply by hand.

Enable both with:  pip install playwright && playwright install chromium
"""

import logging
import os
import re
import smtplib
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)

# Reusing one profile directory is what makes the auto attempt worth trying at
# all: cookies hCaptcha set during an assisted reply carry over to later runs.
PROFILE_DIR = os.path.abspath(os.getenv("BROWSER_PROFILE_DIR", ".browser-profile"))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
RELAY_DOMAINS = ("craigslist.org", "reply.craigslist.org")

# The template ships with [MOVE-IN DATE], [YOUR PHONE] and so on. Sending those
# verbatim to a landlord is worse than not replying, so every path checks first.
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,40}\]")


def unfilled_placeholders(message=None):
    return PLACEHOLDER_RE.findall(message if message is not None else config.REPLY_MESSAGE)


def playwright_available():
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def _context(pw, headless):
    return pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        user_agent=UA,
        locale="en-CA",
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _open_reply_form(page, url, timeout_ms):
    """Load a post and click through to its reply form."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.click("button.reply-button", timeout=timeout_ms)


def _saw_captcha(page):
    return "hcaptcha" in page.content().lower()


def auto_reply(listing, timeout_ms=25000):
    """Try headlessly to read the relay address and email it. Returns (ok, note)."""
    if not playwright_available():
        return False, "playwright not installed"
    missing = unfilled_placeholders()
    if missing:
        return False, f"REPLY_MESSAGE still has {', '.join(missing)} to fill in"

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=True)
        try:
            page = ctx.new_page()
            try:
                _open_reply_form(page, listing["url"], timeout_ms)
            except PWTimeout:
                return False, "reply button never became clickable"

            try:
                page.wait_for_selector("a[href^='mailto:']", timeout=timeout_ms)
            except PWTimeout:
                if _saw_captcha(page):
                    return False, "craigslist demanded a captcha"
                return False, "reply form never revealed an address"

            href = page.get_attribute("a[href^='mailto:']", "href") or ""
            addr = href[len("mailto:"):].split("?")[0].strip()
            if not addr:
                found = EMAIL_RE.search(page.content())
                addr = found.group(0) if found else ""
            if not addr:
                return False, "no address in reply form"
            if not addr.endswith(RELAY_DOMAINS):
                return False, f"{addr} is not a craigslist relay address"
        finally:
            ctx.close()

    try:
        send_email(addr, config.REPLY_SUBJECT, config.REPLY_MESSAGE)
    except Exception as exc:
        log.exception("smtp send failed")
        return False, f"found {addr} but sending failed: {exc}"
    return True, f"emailed {addr}"


def assisted_reply(listing, timeout_ms=25000):
    """Open a visible browser at the post with the message pre-filled.

    Blocks until you close the window, so callers should run it off the request
    thread. Returns (ok, note) where ok means we got as far as pre-filling.
    """
    if not playwright_available():
        return False, "playwright not installed"

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _context(pw, headless=False)
        try:
            page = ctx.new_page()
            try:
                _open_reply_form(page, listing["url"], timeout_ms)
            except PWTimeout:
                return False, "could not open the reply form"

            filled = False
            for sel in ("textarea", "[contenteditable='true']"):
                try:
                    page.fill(sel, config.REPLY_MESSAGE, timeout=8000)
                    filled = True
                    break
                except PWTimeout:
                    continue

            if not filled:
                note = "opened the reply form, but found no message box to pre-fill"
            elif unfilled_placeholders():
                # Still worth opening: she can edit it in the window before sending.
                note = (
                    "pre-filled, but EDIT IT FIRST - still contains "
                    + ", ".join(unfilled_placeholders())
                )
            else:
                note = "message pre-filled - solve the captcha and press send"
            log.info("assisted reply for #%s: %s", listing["num"], note)
            # Give the human time to finish; the window closing ends the wait.
            try:
                page.wait_for_event("close", timeout=10 * 60 * 1000)
            except PWTimeout:
                pass
            return filled, note
        finally:
            ctx.close()


def send_email(to_addr, subject, body):
    if config.DRY_RUN:
        log.info("[dry-run email to %s] %s\n%s", to_addr, subject, body)
        return
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS):
        raise RuntimeError("SMTP is not configured")

    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)
    log.info("emailed reply to %s", to_addr)
