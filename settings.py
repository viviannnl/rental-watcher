"""Search filters you can change from the dashboard without a restart.

`.env` supplies the initial value of each one; anything you change in the UI is
written to the `meta` table and wins from then on, so the two don't fight and your
tweaks survive a restart. Reset a field to fall back to the `.env` value.

Everything is validated here rather than in the template, so the API and the form
can't disagree about what's allowed.
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
    "walk_minutes": (int, 1, 60, "Max walk from office (min)"),
    "min_price": (int, 0, 100000, "Min price ($)"),
    "max_price": (int, 0, 100000, "Max price ($, 0 = no cap)"),
    "min_bedrooms": (int, 0, 5, "Min bedrooms (0 = studio)"),
    "max_bedrooms": (int, 0, 5, "Max bedrooms"),
    "office_lat": (float, 48.0, 50.5, "Office latitude"),
    "office_lon": (float, -124.5, -122.0, "Office longitude"),
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


def radius_m():
    """The search radius in metres implied by the chosen walking time."""
    return craigslist.metres_for_walk(get("walk_minutes"))


def get(name):
    caster = SPEC[name][0]
    stored = db.get_meta(META_PREFIX + name)
    if stored is None:
        return DEFAULTS[name]
    try:
        return caster(stored)
    except (TypeError, ValueError):
        log.warning("stored setting %s=%r is unreadable; using the .env value", name, stored)
        return DEFAULTS[name]


def all_settings():
    return {name: get(name) for name in SPEC}


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
    for name, value in changed.items():
        db.set_meta(META_PREFIX + name, value)
    if changed:
        log.info("settings changed: %s", changed)
    return changed, []


def reset():
    """Drop all overrides so the .env values apply again."""
    for name in SPEC:
        db.del_meta(META_PREFIX + name)
