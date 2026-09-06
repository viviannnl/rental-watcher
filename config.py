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

# Straight-line metres. A 10 min walk is ~830 m of pavement, but real routes detour
# around blocks; in downtown Vancouver's grid the ratio is ~1.2-1.3, so ~700 m
# crow-flies is a closer match to "10 min walk" than 830 m.
RADIUS_M = _f("RADIUS_M", "700")

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

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
MY_PHONE = os.getenv("MY_PHONE", "")

# --- Outbound reply email ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _i("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
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


DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
