import sqlite3
import threading

DB_PATH = "listings.db"

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
        seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
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


def mark_replied(num, note):
    _q(
        "UPDATE listings SET replied_at = datetime('now'), reply_note = ? WHERE num = ?",
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
