import os

from dotenv import load_dotenv

load_dotenv()


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


# --- Search area ---
# Amazon YVR14, 402 Dunsmuir St, Vancouver BC. Verify with scripts/check_office.py;
# OSM has no exact rooftop for this address, so this is a street-level estimate.
OFFICE_LAT = _f("OFFICE_LAT", "49.2806")
OFFICE_LON = _f("OFFICE_LON", "-123.1116")

# How far you're willing to walk, in minutes. This is the number you actually care
# about; the search radius in metres is derived from it (see craigslist.py, which
# accounts for real routes being longer than straight lines).
# RADIUS_M is still read for older .env files that set metres directly.
RADIUS_M = _f("RADIUS_M", "700")
WALK_MINUTES = _i("WALK_MINUTES", 0) or max(1, round(RADIUS_M * 1.25 / (5000 / 60)))

MIN_BEDROOMS = _i("MIN_BEDROOMS", 0)  # 0 == studio
MAX_BEDROOMS = _i("MAX_BEDROOMS", 1)
MIN_PRICE = _i("MIN_PRICE", 0)
MAX_PRICE = _i("MAX_PRICE", 0)  # 0 == no cap

# Craigslist counts a shared room as "1br", so room/roommate posts leak into an
# apartment search. Titles containing any of these are dropped.
EXCLUDE_KEYWORDS = [
    w.strip().lower()
    for w in os.getenv(
        "EXCLUDE_KEYWORDS",
        "room for rent,roommate,shared room,room available,private bedroom,"
        "private room,room in,bedroom for rent,sublet",
    ).split(",")
    if w.strip()
]

POLL_MINUTES = _i("POLL_MINUTES", 10)

CL_AREA_ID = _i("CL_AREA_ID", 16)  # vancouver, BC
CL_SUBAREA_ID = _i("CL_SUBAREA_ID", 1)  # city of vancouver
CL_CATEGORY = os.getenv("CL_CATEGORY", "apa")

# The area one crawl covers, independent of any one person's commute. Craigslist
# returns at most 360 postings per request with no way to page further, and every
# radius saturates that, so a wider crawl doesn't see more places - it sees the same
# 360 spread over more ground, reaching less far back in time. Measured within a
# 1 km circle the crawl sees 360 of 471 current postings; at 3 km, 360 of 1751. So
# keep this just wide enough to contain every saved search: 2 km covers a 30 minute
# walk, which is the widest anyone here is likely to want.
#
# Centred on the office by default. With watchers spread across the city this wants
# to become the centroid of their searches, or several crawls.
CRAWL_LAT = _f("CRAWL_LAT", OFFICE_LAT)
CRAWL_LON = _f("CRAWL_LON", OFFICE_LON)
CRAWL_RADIUS_KM = _i("CRAWL_RADIUS_KM", 2)

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
# Needed regardless of whether you use an API key: Twilio signs inbound webhooks
# with the Auth Token, so it is the only thing that can verify them.
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# Optional. If set, outbound sends use this restricted key instead of the Auth
# Token, so the credential doing the sending can't do anything else.
TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID", "")
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
MY_PHONE = os.getenv("MY_PHONE", "")

# --- How you get alerted ---
# "email" needs only a Gmail App Password and is the default. "sms" needs a paid
# Twilio account (trial accounts can only send canned templates, so alerts fail)
# plus a public webhook URL for replies. "both" does each.
NOTIFY_CHANNEL = os.getenv("NOTIFY_CHANNEL", "email").strip().lower()
NOTIFY_EMAIL = NOTIFY_CHANNEL in ("email", "both")
NOTIFY_SMS = NOTIFY_CHANNEL in ("sms", "both")

# --- Email (alerts to you, replies to landlords, and reading your responses) ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _i("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
# Where alerts go. Defaults to the sending account, i.e. you email yourself.
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "") or SMTP_USER

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = _i("IMAP_PORT", 993)
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
# How often to check for your replies. Cheap, so it can be much tighter than the
# Craigslist poll.
INBOX_POLL_SECONDS = _i("INBOX_POLL_SECONDS", 60)
WATCH_INBOX = os.getenv("WATCH_INBOX", "1") == "1"
REPLY_SUBJECT = os.getenv("REPLY_SUBJECT", "Interested in your rental listing")

# When you can view a place. Pulled out of the message body because it's the part
# most likely to change week to week, and it reads better as one editable phrase
# than as prose you have to surgically rewrite.
AVAILABILITY = os.getenv(
    "AVAILABILITY", "on weekday evenings after 6pm, and most of the weekend"
)

REPLY_TEMPLATE = os.getenv(
    "REPLY_MESSAGE",
    "Hi! I saw your listing on Craigslist and I'm very interested. "
    "Is it still available? I'm free {availability}.",
)


def reply_body():
    """The message to send, with {availability} filled in.

    Uses str.replace rather than str.format so a stray brace in someone's message
    can't raise, and so unknown tokens are left visible instead of vanishing.
    """
    return REPLY_TEMPLATE.replace("{availability}", AVAILABILITY)


# Look up how old the building is. Costs one page fetch per alerted listing plus
# a City of Vancouver open-data query, so it is only done for listings you're
# actually told about, never the whole search.
LOOKUP_YEAR_BUILT = os.getenv("LOOKUP_YEAR_BUILT", "1") == "1"

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
