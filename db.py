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

    -- Everything below is the multi-tenant shape, currently holding exactly one
    -- user and one search. Nothing here is exposed yet: there is no sign-up, no
    -- login and no second row. It exists so that adding those later is a feature
    -- rather than a rewrite, and so the filters stop living in a key-value table
    -- where a typo'd key silently means "default".
    CREATE TABLE IF NOT EXISTS users (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        email      TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );

    -- One saved search. Deliberately not one-per-user: the whole point of splitting
    -- crawl from match is that a person can watch two neighbourhoods at different
    -- prices, and each is a row here.
    CREATE TABLE IF NOT EXISTS searches (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        name         TEXT,
        walk_minutes INTEGER NOT NULL,
        min_price    INTEGER NOT NULL DEFAULT 0,
        max_price    INTEGER NOT NULL DEFAULT 0,
        min_bedrooms INTEGER NOT NULL DEFAULT 0,
        max_bedrooms INTEGER NOT NULL DEFAULT 1,
        office_lat   REAL NOT NULL,
        office_lon   REAL NOT NULL,
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );

    -- Who has been told about what. listings stays shared and un-owned, because a
    -- posting is a fact about the world, not about a subscriber - two people watching
    -- the same block should cost one crawl and one row, not two of each. This is the
    -- table that makes that safe: it moves "already alerted" off the listing, where
    -- it can only ever be true for one person, and onto the pair.
    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id   INTEGER NOT NULL REFERENCES searches(id),
        listing_num INTEGER NOT NULL REFERENCES listings(num),
        channel     TEXT,
        sent_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (search_id, listing_num)
    );
    CREATE INDEX IF NOT EXISTS alerts_by_search ON alerts (search_id, listing_num);
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


# The filter columns of a search, in the order the dashboard shows them. Kept here
# so the INSERT and UPDATE below can't drift apart from each other.
SEARCH_FILTERS = (
    "walk_minutes", "min_price", "max_price",
    "min_bedrooms", "max_bedrooms", "office_lat", "office_lon",
)


def ensure_user(email, user_id=None):
    """Return the id of the user with this email, creating the row if needed.

    user_id forces a specific id, which is only used to pin the existing installation
    to 1 so that older data and new rows agree about who they belong to.
    """
    row = _q("SELECT id FROM users WHERE email = ?", (email,), fetch="one")
    if row:
        return row["id"]
    if user_id is None:
        return _q("INSERT INTO users (email) VALUES (?)", (email,))
    return _q("INSERT INTO users (id, email) VALUES (?, ?)", (user_id, email))


def get_search(search_id):
    return _q("SELECT * FROM searches WHERE id = ?", (search_id,), fetch="one")


def active_searches():
    """Every search a crawl should be matched against, cheapest query in the app.

    One row today. The signature is the point: the poll loops over searches instead
    of assuming there is only ever one.
    """
    return _q(
        "SELECT * FROM searches WHERE active = 1 ORDER BY id", fetch="all"
    )


def create_search(user_id, filters, search_id=None, name=None):
    """Insert a saved search from a {name: value} dict of filters."""
    names = ["user_id", "name", *SEARCH_FILTERS]
    args = [user_id, name, *(filters[name] for name in SEARCH_FILTERS)]
    if search_id is not None:
        names.insert(0, "id")
        args.insert(0, search_id)
    return _q(
        f"INSERT INTO searches ({', '.join(names)})"
        f" VALUES ({', '.join('?' * len(names))})",
        tuple(args),
    )


def update_search(search_id, filters):
    """Write only the named filters, leaving the rest of the row alone."""
    fields = [name for name in filters if name in SEARCH_FILTERS]
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _q(
        f"UPDATE searches SET {assignments} WHERE id = ?",
        tuple([filters[name] for name in fields] + [search_id]),
    )


def record_alert(search_id, listing_num, channel=None):
    """Note that this search has been told about this listing.

    Returns False if it already had been. The UNIQUE constraint is doing the real
    work: it makes a duplicate alert impossible even if two polls overlap, rather
    than relying on the caller to check first.
    """
    with _lock:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO alerts (search_id, listing_num, channel)"
            " VALUES (?, ?, ?)",
            (search_id, listing_num, channel),
        )
        inserted = bool(cur.rowcount)
        _conn.commit()
    return inserted


def alerted_nums(search_id):
    """Listing numbers this search has already been told about."""
    return {
        r["listing_num"]
        for r in _q(
            "SELECT listing_num FROM alerts WHERE search_id = ?", (search_id,), fetch="all"
        )
    }


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
