"""Tests for the saved-search row and the migration onto it.

conftest points DB_PATH at a temporary file, so importing db here is safe. The
migration is the risky part - it runs against a database with real listings in it and
gets exactly one chance to carry the filters over.
"""

import config
import db
import settings


def test_migration_created_the_search():
    """Importing settings is what runs the migration, so it has already happened."""
    row = db.get_search(settings.SEARCH_ID)
    assert row is not None and row["active"] == 1
    assert settings.SEARCH_ID in {s["id"] for s in db.active_searches()}


def test_search_is_owned_by_the_user():
    row = db.get_search(settings.SEARCH_ID)
    assert row["user_id"] == settings.USER_ID


def test_filters_default_to_the_env_values():
    assert settings.all_settings() == settings.DEFAULTS


def test_all_settings_returns_every_spec_field_typed():
    values = settings.all_settings()
    assert set(values) == set(settings.SPEC)
    for name, (caster, *_rest) in settings.SPEC.items():
        assert isinstance(values[name], caster)


def test_migration_is_idempotent():
    """It runs at import, so a restart must not overwrite saved filters with .env."""
    settings.update({"max_price": "2400"})
    settings._migrate_to_searches()
    assert settings.get("max_price") == 2400
    settings.reset()


def test_migration_carries_over_a_filter_saved_the_old_way():
    """Filters used to live in meta under 'setting:<name>'. A user upgrading has a
    dashboard-saved value there, and losing it would silently widen their search."""
    db.set_meta(settings.META_PREFIX + "max_price", "1999")
    db.set_meta(settings.META_PREFIX + "walk_minutes", "17")
    legacy_id = 99
    try:
        original, settings.SEARCH_ID = settings.SEARCH_ID, legacy_id
        settings._migrate_to_searches()
        assert settings.get("max_price") == 1999
        assert settings.get("walk_minutes") == 17
        # Untouched filters still come from .env.
        assert settings.get("min_bedrooms") == config.MIN_BEDROOMS
        # And the old keys are gone, so there is only one source of truth left.
        assert db.get_meta(settings.META_PREFIX + "max_price") is None
    finally:
        settings.SEARCH_ID = original


def test_update_persists_and_reports_what_changed():
    changed, errors = settings.update({"walk_minutes": "15"})
    assert errors == []
    assert changed == {"walk_minutes": 15}
    assert settings.get("walk_minutes") == 15
    settings.reset()


def test_update_ignores_a_value_that_did_not_change():
    changed, errors = settings.update({"walk_minutes": str(settings.get("walk_minutes"))})
    assert changed == {} and errors == []


def test_update_writes_nothing_when_any_field_is_invalid():
    """Atomicity matters here: a typo in one box shouldn't half-apply the form."""
    before = settings.all_settings()
    changed, errors = settings.update({"walk_minutes": "12", "max_price": "not a number"})
    assert errors and changed == {}
    assert settings.all_settings() == before


def test_update_rejects_out_of_range_values():
    _changed, errors = settings.update({"walk_minutes": "999"})
    assert errors and "between" in errors[0]


def test_update_rejects_a_max_below_its_min():
    _changed, errors = settings.update({"min_price": "3000", "max_price": "1000"})
    assert errors and "Max price" in errors[0]


def test_reset_restores_the_env_values():
    settings.update({"walk_minutes": "42", "max_price": "1234"})
    settings.reset()
    assert settings.all_settings() == settings.DEFAULTS


def test_radius_follows_the_walking_time():
    settings.update({"walk_minutes": "10"})
    assert settings.radius_m() == 667
    settings.reset()


# --- alerts ---------------------------------------------------------------

def test_record_alert_is_recorded_once_per_search_and_listing():
    num = db.add({"cl_id": "alert-test", "url": "u", "title": "t"}, notified=True)
    assert db.record_alert(settings.SEARCH_ID, num, "email") is True
    # A second attempt is refused by the UNIQUE constraint, so overlapping polls
    # can't double-email.
    assert db.record_alert(settings.SEARCH_ID, num, "email") is False
    assert num in db.alerted_nums(settings.SEARCH_ID)


def test_alerts_are_tracked_per_search_not_per_listing():
    """The reason the column on listings has to go: two searches can legitimately
    both need telling about the same place."""
    num = db.add({"cl_id": "shared-listing", "url": "u", "title": "t"}, notified=True)
    other = db.create_search(settings.USER_ID, settings.DEFAULTS, name="second")
    assert db.record_alert(settings.SEARCH_ID, num) is True
    assert db.record_alert(other, num) is True
    assert db.alerted_nums(other) == {num}


# --- new / seen -----------------------------------------------------------

def test_a_surfaced_listing_starts_unseen():
    """Unseen is what makes it show as new, so it has to be the default state."""
    num = db.add({"cl_id": "unseen-1", "url": "u", "title": "t"}, notified=True)
    db.record_alert(settings.SEARCH_ID, num)
    assert num in db.unseen_nums(settings.SEARCH_ID)


def test_mark_seen_clears_only_the_listings_named():
    """The dashboard shows at most 100 rows and hides non-matching ones, so marking
    everything seen would silently retire places you were never shown."""
    shown = db.add({"cl_id": "seen-shown", "url": "u", "title": "t"}, notified=True)
    hidden = db.add({"cl_id": "seen-hidden", "url": "u", "title": "t"}, notified=True)
    db.record_alert(settings.SEARCH_ID, shown)
    db.record_alert(settings.SEARCH_ID, hidden)

    assert db.mark_seen(settings.SEARCH_ID, [shown]) == 1
    unseen = db.unseen_nums(settings.SEARCH_ID)
    assert shown not in unseen and hidden in unseen


def test_mark_seen_is_not_undone_by_a_later_sweep():
    num = db.add({"cl_id": "seen-twice", "url": "u", "title": "t"}, notified=True)
    db.record_alert(settings.SEARCH_ID, num)
    db.mark_seen(settings.SEARCH_ID, [num])
    # Already seen, so there is nothing left for a second call to do.
    assert db.mark_seen(settings.SEARCH_ID, [num]) == 0
    assert num not in db.unseen_nums(settings.SEARCH_ID)


def test_mark_seen_with_no_listings_does_nothing():
    """An empty list must not be read as "all of them"."""
    num = db.add({"cl_id": "seen-none", "url": "u", "title": "t"}, notified=True)
    db.record_alert(settings.SEARCH_ID, num)
    assert db.mark_seen(settings.SEARCH_ID, []) == 0
    assert num in db.unseen_nums(settings.SEARCH_ID)


def test_unseen_is_per_search():
    num = db.add({"cl_id": "unseen-shared", "url": "u", "title": "t"}, notified=True)
    other = db.create_search(settings.USER_ID, settings.DEFAULTS, name="third")
    db.record_alert(settings.SEARCH_ID, num)
    db.record_alert(other, num)
    db.mark_seen(settings.SEARCH_ID, [num])
    # One person having looked says nothing about the other.
    assert num not in db.unseen_nums(settings.SEARCH_ID)
    assert num in db.unseen_nums(other)
