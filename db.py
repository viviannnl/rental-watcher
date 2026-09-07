import os
import sqlite3
import threading

# Overridable so tests and experiments don't scribble on the real database - the
# settings you save from the dashboard live in here too, not just listings.
DB_PATH = os.getenv("DB_PATH", "listings.db")

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
    CREATE TABLE IF NOT EXISTS listings (
        num        INTEGER PRIMARY KEY AUTOINCREMENT,
        cl_id      TEXT UNIQUE NOT NULL,
        url        TEXT NOT NULL,
        title      TEXT,
        price      INTEGER,
        bedrooms   INTEGER,
        lat        REAL,
        lon        REAL,
        distance_m REAL,
        seen_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        notified   INTEGER NOT NULL DEFAULT 0,
        replied_at TEXT,
        reply_note TEXT
    );
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
""")
_conn.commit()


def _add_missing_columns():
    """Bring an older database up to date without discarding what's in it."""
    have = {r["name"] for r in _conn.execute("PRAGMA table_info(listings)")}
    for name, decl in (("address", "TEXT"), ("year_built", "INTEGER"), ("year_source", "TEXT")):
        if name not in have:
            _conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    _conn.commit()


def _localize_timestamps():
    """One-off: rows written before this stored UTC, so times displayed hours off.

    Guarded by a flag because applying 'localtime' to an already-local value would
    shift it a second time.
    """
    if _conn.execute("SELECT 1 FROM meta WHERE key = 'tz_localized'").fetchone():
        return
    _conn.execute("UPDATE listings SET seen_at = datetime(seen_at, 'localtime')")
    _conn.execute(
        "UPDATE listings SET replied_at = datetime(replied_at, 'localtime')"
        " WHERE replied_at IS NOT NULL"
    )
    _conn.execute("INSERT INTO meta (key, value) VALUES ('tz_localized', '1')")
    _conn.commit()


_add_missing_columns()
_localize_timestamps()


def _q(sql, args=(), fetch=None):
    with _lock:
        cur = _conn.execute(sql, args)
        if fetch == "one":
            row = cur.fetchone()
            out = dict(row) if row else None
        elif fetch == "all":
            out = [dict(r) for r in cur.fetchall()]
        else:
            out = cur.lastrowid
        _conn.commit()
    return out


def known_ids():
    """All Craigslist ids we've ever recorded, for cheap in-memory dedupe."""
    return {r["cl_id"] for r in _q("SELECT cl_id FROM listings", fetch="all")}


def add(listing, notified):
    """Insert a listing, returning its short number, or None if already present."""
    cols = ("cl_id", "url", "title", "price", "bedrooms", "lat", "lon", "distance_m")
    args = {k: listing.get(k) for k in cols}
    args["notified"] = int(notified)
    with _lock:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO listings"
            " (cl_id, url, title, price, bedrooms, lat, lon, distance_m, notified)"
            " VALUES (:cl_id, :url, :title, :price, :bedrooms, :lat, :lon, :distance_m, :notified)",
            args,
        )
        # rowcount is 0 when the UNIQUE constraint made this a no-op, in which
        # case lastrowid still points at some earlier insert.
        num = cur.lastrowid if cur.rowcount else None
        _conn.commit()
    return num


def get(num):
    return _q("SELECT * FROM listings WHERE num = ?", (num,), fetch="one")


def latest_notified():
    return _q(
        "SELECT * FROM listings WHERE notified = 1 ORDER BY num DESC LIMIT 1", fetch="one"
    )


def recent(limit=200):
    return _q(
        "SELECT * FROM listings ORDER BY num DESC LIMIT ?", (limit,), fetch="all"
    )


def save_building_info(num, address, year_built, year_source):
    _q(
        "UPDATE listings SET address = ?, year_built = ?, year_source = ? WHERE num = ?",
        (address, year_built, year_source, num),
    )


def mark_replied(num, note):
    _q(
        "UPDATE listings SET replied_at = datetime('now', 'localtime'), reply_note = ? WHERE num = ?",
        (note, num),
    )


def get_meta(key, default=None):
    row = _q("SELECT value FROM meta WHERE key = ?", (key,), fetch="one")
    return row["value"] if row else default


def set_meta(key, value):
    _q(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def del_meta(key):
    _q("DELETE FROM meta WHERE key = ?", (key,))


def recompute_distances(lat, lon, haversine):
    """Redo every stored distance against a new office location.

    Without this, moving the office would leave old rows showing how far they were
    from the previous one, which reads as a bug rather than stale data.
    """
    rows = _q("SELECT num, lat, lon FROM listings WHERE lat IS NOT NULL", fetch="all")
    with _lock:
        for row in rows:
            _conn.execute(
                "UPDATE listings SET distance_m = ? WHERE num = ?",
                (round(haversine(lat, lon, row["lat"], row["lon"])), row["num"]),
            )
        _conn.commit()
    return len(rows)
