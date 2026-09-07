"""Search filters you can change from the dashboard without a restart.

`.env` supplies the initial value of each one; anything you change in the UI is
written to the database and wins from then on, so the two don't fight and your tweaks
survive a restart. Reset puts the `.env` values back.

Everything is validated here rather than in the template, so the API and the form
can't disagree about what's allowed.

The filters live in a row of the `searches` table, owned by a row of `users`. There
is one of each and no way to make a second, so nothing about this is visible in the
UI - but it means a saved search is a record with typed columns rather than seven
loose keys in `meta`, where a mistyped key reads as "use the default" and says
nothing. The rest of the module is unchanged around it: callers still ask for
get("max_price") and don't know a search id exists.
"""

import logging

import config
import craigslist
import db

log = logging.getLogger(__name__)

META_PREFIX = "setting:"

# name -> (caster, low, high, label). Bounds are sanity rails, not preferences:
# an hour's walk or a negative price is a mistake, not a choice.
SPEC = {
    "walk_minutes": (int, 1, 60, "Max walk (min)"),
    "min_price": (int, 0, 100000, "Min price ($)"),
    "max_price": (int, 0, 100000, "Max price ($)"),
    "min_bedrooms": (int, 0, 5, "Min bedrooms"),
    "max_bedrooms": (int, 0, 5, "Max bedrooms"),
    "office_lat": (float, 48.0, 50.5, "Office latitude"),
    "office_lon": (float, -124.5, -122.0, "Office longitude"),
}

# Shown on hover. Kept out of the labels so they stay short enough not to wrap,
# which was knocking the inputs out of alignment with each other.
NOTES = {
    "walk_minutes": "Straight-line radius is derived from this, allowing for detours",
    "max_price": "0 means no upper limit",
    "min_bedrooms": "0 includes studios",
}

DEFAULTS = {
    "walk_minutes": config.WALK_MINUTES,
    "min_price": config.MIN_PRICE,
    "max_price": config.MAX_PRICE,
    "min_bedrooms": config.MIN_BEDROOMS,
    "max_bedrooms": config.MAX_BEDROOMS,
    "office_lat": config.OFFICE_LAT,
    "office_lon": config.OFFICE_LON,
}


def _migrate_radius():
    """Carry a saved radius in metres over to the equivalent walking time.

    The filter used to be expressed in metres. Without this, switching units would
    silently discard whatever radius you had chosen and snap back to the .env value.
    """
    legacy = db.get_meta(META_PREFIX + "radius_m")
    if legacy is None:
        return
    if db.get_meta(META_PREFIX + "walk_minutes") is None:
        try:
            minutes = craigslist.walk_minutes(float(legacy))
        except (TypeError, ValueError):
            log.warning("could not migrate saved radius %r", legacy)
            minutes = None
        if minutes:
            db.set_meta(META_PREFIX + "walk_minutes", minutes)
            log.info("migrated saved radius of %sm to a %s min walk", legacy, minutes)
    db.del_meta(META_PREFIX + "radius_m")


_migrate_radius()

# The one user and the one search, until there is a way to make more. Pinned to 1 so
# that rows written before and after this change agree about who owns them.
USER_ID = 1
SEARCH_ID = 1


def _migrate_to_searches():
    """Move the filters out of `meta` and into a real row. Runs once, idempotently.

    Any value previously saved from the dashboard is carried over; anything never
    changed falls back to `.env`, which is what it was already doing. The old
    `setting:` keys are deleted afterwards so there is exactly one place a filter can
    come from - leaving both would mean two sources of truth and a coin toss about
    which one a future reader trusts.
    """
    if db.get_search(SEARCH_ID):
        return

    saved = {}
    for name, (caster, *_rest) in SPEC.items():
        stored = db.get_meta(META_PREFIX + name)
        if stored is None:
            continue
        try:
            saved[name] = caster(stored)
        except (TypeError, ValueError):
            log.warning("saved filter %s=%r is unreadable; using the .env value", name, stored)

    db.ensure_user(config.ALERT_EMAIL or "owner@localhost", user_id=USER_ID)
    db.create_search(USER_ID, {**DEFAULTS, **saved}, search_id=SEARCH_ID, name="My search")
    for name in SPEC:
        db.del_meta(META_PREFIX + name)
    log.info(
        "moved filters into searches row %d (%s came from the dashboard, the rest "
        "from .env)", SEARCH_ID, ", ".join(sorted(saved)) or "nothing",
    )


_migrate_to_searches()


def radius_m():
    """The search radius in metres implied by the chosen walking time."""
    return craigslist.metres_for_walk(get("walk_minutes"))


def all_settings():
    """Every filter for the current search, as a dict craigslist.match understands."""
    row = db.get_search(SEARCH_ID)
    if row is None:
        # Only reachable if the row were deleted underneath us. Falling back to .env
        # beats raising on every page load.
        log.warning("search %d is missing; using the .env values", SEARCH_ID)
        return dict(DEFAULTS)
    return {name: SPEC[name][0](row[name]) for name in SPEC}


def get(name):
    return all_settings()[name]


def _validate(name, raw):
    """Returns (value, error). error is None when the value is usable."""
    caster, low, high, label = SPEC[name]
    try:
        value = caster(str(raw).strip())
    except (TypeError, ValueError):
        return None, f"{label}: {raw!r} is not a number"
    if not low <= value <= high:
        return None, f"{label}: must be between {low} and {high}"
    return value, None


def update(form):
    """Apply a dict of {name: raw}. Returns (changed, errors).

    Nothing is written unless every field validates, so a single typo can't leave
    the filters half-updated.
    """
    proposed, errors = dict(all_settings()), []
    for name, raw in form.items():
        if name not in SPEC or raw is None or str(raw).strip() == "":
            continue
        value, error = _validate(name, raw)
        if error:
            errors.append(error)
        else:
            proposed[name] = value

    if proposed["max_price"] and proposed["max_price"] < proposed["min_price"]:
        errors.append("Max price must be at least the min price (or 0 for no cap)")
    if proposed["max_bedrooms"] < proposed["min_bedrooms"]:
        errors.append("Max bedrooms must be at least the min")
    if errors:
        return {}, errors

    current = all_settings()
    changed = {n: v for n, v in proposed.items() if v != current[n]}
    if changed:
        db.update_search(SEARCH_ID, changed)
        log.info("settings changed: %s", changed)
    return changed, []


def reset():
    """Put the .env values back."""
    db.update_search(SEARCH_ID, DEFAULTS)
