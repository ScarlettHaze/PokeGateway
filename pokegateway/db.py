"""SQLite storage for accounts, friends, GTS and uploaded content."""

import json
import os
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    profileid   INTEGER PRIMARY KEY AUTOINCREMENT,
    userid      TEXT NOT NULL,
    gsbrcd      TEXT NOT NULL,
    gameid      TEXT,
    uniquenick  TEXT,
    email       TEXT,
    macadr      TEXT,
    ingamesn    TEXT,
    firstname   TEXT DEFAULT '',
    lastname    TEXT DEFAULT '',
    banned      INTEGER DEFAULT 0,
    created     INTEGER,
    last_login  INTEGER,
    UNIQUE(userid, gsbrcd)
);
CREATE TABLE IF NOT EXISTS nas_logins (
    authtoken TEXT PRIMARY KEY,
    userid    TEXT,
    data      TEXT,
    created   INTEGER
);
CREATE TABLE IF NOT EXISTS buddies (
    owner     INTEGER,
    buddy     INTEGER,
    status    INTEGER DEFAULT 0,
    notified  INTEGER DEFAULT 0,
    time      INTEGER,
    PRIMARY KEY(owner, buddy)
);
CREATE TABLE IF NOT EXISTS pending_messages (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    src    INTEGER,
    dst    INTEGER,
    msg    BLOB
);
CREATE TABLE IF NOT EXISTS gts (
    gen           INTEGER,
    pid           INTEGER,
    data          BLOB,
    species       INTEGER,
    gender        INTEGER,
    level         INTEGER,
    req_species   INTEGER,
    req_gender    INTEGER,
    req_min       INTEGER,
    req_max       INTEGER,
    country       INTEGER,
    is_exchanged  INTEGER DEFAULT 0,
    deposited     REAL,
    locked_until  REAL DEFAULT 0,
    locked_by     INTEGER,
    PRIMARY KEY(gen, pid)
);
CREATE TABLE IF NOT EXISTS gts_last_search (
    gen  INTEGER,
    pid  INTEGER,
    time REAL,
    PRIMARY KEY(gen, pid)
);
CREATE TABLE IF NOT EXISTS gts_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    gen     INTEGER,
    pid     INTEGER,
    partner INTEGER,
    species INTEGER,
    time    INTEGER
);
CREATE TABLE IF NOT EXISTS gamestats_profiles (
    gen     INTEGER,
    pid     INTEGER,
    data    BLOB,
    ip      TEXT,
    updated INTEGER,
    PRIMARY KEY(gen, pid)
);
CREATE TABLE IF NOT EXISTS uploads (
    serial   INTEGER PRIMARY KEY,
    kind     TEXT,      -- bv4, bv5, dressup4, box4, musical5
    pid      INTEGER,
    search1  INTEGER,   -- species / box label
    country  INTEGER,
    region   INTEGER,
    meta     INTEGER,
    header   BLOB,
    data     BLOB,
    saved    INTEGER DEFAULT 0,
    created  INTEGER
);
CREATE INDEX IF NOT EXISTS uploads_kind ON uploads(kind, created);
CREATE TABLE IF NOT EXISTS pgl_players (
    gsid    TEXT PRIMARY KEY,
    rom     INTEGER,
    lang    INTEGER,
    status  INTEGER DEFAULT 0,   -- 0 awake, 1 sleeping
    name    TEXT DEFAULT '',
    data    TEXT DEFAULT '{}',   -- JSON dream data
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS tower (
    gen     INTEGER,
    pid     INTEGER,
    rank    INTEGER,
    room    INTEGER,
    won     INTEGER,
    data    BLOB,
    leader  INTEGER DEFAULT 0,
    updated INTEGER,
    PRIMARY KEY(gen, pid)
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        return self.conn.execute(sql, args)

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- NAS
    def next_userid(self) -> str:
        row = self.one("SELECT max(CAST(userid AS INTEGER)) AS m FROM users")
        base = 2
        if row and row["m"]:
            base = int(row["m"]) + 1
        # Also don't hand out an id that only exists as an unfinished login.
        return "%013d" % base

    def is_banned(self, userid: str, macadr: str = "") -> bool:
        row = self.one("SELECT 1 FROM users WHERE banned = 1 AND "
                       "(userid = ? OR (macadr = ? AND macadr != ''))",
                       (userid, macadr))
        return row is not None

    def create_authtoken(self, data: dict) -> str:
        while True:
            token = "NDS" + secrets.token_urlsafe(60)[:80]
            if not self.one("SELECT 1 FROM nas_logins WHERE authtoken = ?", (token,)):
                break
        self.q("INSERT INTO nas_logins VALUES (?, ?, ?, ?)",
               (token, data.get("userid", ""), json.dumps(data), int(time.time())))
        return token

    def get_authtoken(self, token: str):
        row = self.one("SELECT data FROM nas_logins WHERE authtoken = ?", (token,))
        return json.loads(row["data"]) if row else None

    # ------------------------------------------------------------- users
    def get_or_create_profile(self, userid, gsbrcd, macadr="", ingamesn=""):
        row = self.one("SELECT * FROM users WHERE userid = ? AND gsbrcd = ?",
                       (userid, gsbrcd))
        now = int(time.time())
        if row:
            self.q("UPDATE users SET last_login = ?, ingamesn = COALESCE(NULLIF(?, ''), ingamesn) "
                   "WHERE profileid = ?", (now, ingamesn, row["profileid"]))
            return self.get_profile(row["profileid"])
        uniquenick = _base32(int(userid or 0)) + gsbrcd
        cur = self.q(
            "INSERT INTO users (userid, gsbrcd, gameid, uniquenick, email, macadr, "
            "ingamesn, created, last_login) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (userid, gsbrcd, gsbrcd[:4], uniquenick, uniquenick + "@nds",
             macadr, ingamesn, now, now))
        return self.get_profile(cur.lastrowid)

    def get_profile(self, profileid):
        row = self.one("SELECT * FROM users WHERE profileid = ?", (int(profileid),))
        return dict(row) if row else None

    def update_profile(self, profileid, field, value):
        if field in ("firstname", "lastname"):
            self.q("UPDATE users SET %s = ? WHERE profileid = ?" % field,
                   (value, profileid))

    def list_users(self):
        return [dict(r) for r in self.all("SELECT * FROM users ORDER BY profileid")]

    def set_banned(self, profileid, banned=True):
        self.q("UPDATE users SET banned = ? WHERE profileid = ?",
               (1 if banned else 0, profileid))

    # ------------------------------------------------------------- buddies
    def buddy_list(self, owner):
        return [dict(r) for r in self.all("SELECT * FROM buddies WHERE owner = ?", (owner,))]

    def add_buddy(self, owner, buddy):
        self.q("INSERT OR IGNORE INTO buddies (owner, buddy, status, time) "
               "VALUES (?, ?, 0, ?)", (owner, buddy, int(time.time())))

    def auth_buddy(self, owner, buddy):
        """`buddy` accepted `owner`'s request (owner -> buddy becomes 1)."""
        self.q("INSERT OR IGNORE INTO buddies (owner, buddy, status, time) "
               "VALUES (?, ?, 0, ?)", (owner, buddy, int(time.time())))
        self.q("UPDATE buddies SET status = 1 WHERE owner = ? AND buddy = ?",
               (owner, buddy))

    def del_buddy(self, owner, buddy):
        self.q("DELETE FROM buddies WHERE owner = ? AND buddy = ?", (owner, buddy))

    def has_buddy(self, owner, buddy):
        return self.one("SELECT 1 FROM buddies WHERE owner = ? AND buddy = ?",
                        (owner, buddy)) is not None

    def pending_requests_for(self, profileid):
        """People who added `profileid` but are not yet authorized."""
        return [dict(r) for r in self.all(
            "SELECT * FROM buddies WHERE buddy = ? AND status = 0", (profileid,))]

    def unnotified_auths(self, owner):
        rows = [dict(r) for r in self.all(
            "SELECT * FROM buddies WHERE owner = ? AND status = 1 AND notified = 0",
            (owner,))]
        self.q("UPDATE buddies SET notified = 1 WHERE owner = ? AND status = 1", (owner,))
        return rows

    def save_message(self, src, dst, msg: bytes):
        self.q("INSERT INTO pending_messages (src, dst, msg) VALUES (?, ?, ?)",
               (src, dst, msg))

    def pop_messages(self, dst):
        rows = [dict(r) for r in self.all(
            "SELECT * FROM pending_messages WHERE dst = ? ORDER BY id", (dst,))]
        self.q("DELETE FROM pending_messages WHERE dst = ?", (dst,))
        return rows

    # ------------------------------------------------------------- gamestats
    def set_gamestats_profile(self, gen, pid, data, ip):
        self.q("INSERT OR REPLACE INTO gamestats_profiles VALUES (?, ?, ?, ?, ?)",
               (gen, pid, data, ip, int(time.time())))

    # ------------------------------------------------------------- GTS
    def gts_get(self, gen, pid):
        row = self.one("SELECT * FROM gts WHERE gen = ? AND pid = ?", (gen, pid))
        return dict(row) if row else None

    def gts_put(self, gen, pid, rec):
        self.q("INSERT OR REPLACE INTO gts (gen, pid, data, species, gender, level, "
               "req_species, req_gender, req_min, req_max, country, is_exchanged, "
               "deposited, locked_until, locked_by) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
               (gen, pid, rec.raw, rec.species, rec.gender, rec.level,
                rec.req_species, rec.req_gender, rec.req_min, rec.req_max,
                rec.country, rec.is_exchanged, time.time()))

    def gts_delete(self, gen, pid):
        return self.q("DELETE FROM gts WHERE gen = ? AND pid = ?", (gen, pid)).rowcount > 0

    def gts_search(self, gen, pid, species, gender, min_level, max_level, country, count):
        where = ["gen = ?", "pid != ?", "is_exchanged = 0", "locked_until < ?",
                 "species = ?"]
        args = [gen, pid, time.time(), species]
        if gender in (1, 2):
            where.append("gender IN (?, 3)")
            args.append(gender)
        if min_level:
            where.append("level >= ?")
            args.append(min_level)
        if max_level:
            where.append("level <= ?")
            args.append(max_level)
        if country:
            where.append("country = ?")
            args.append(country)
        sql = ("SELECT * FROM gts WHERE " + " AND ".join(where) +
               " ORDER BY deposited DESC LIMIT ?")
        args.append(count)
        return [dict(r) for r in self.all(sql, args)]

    def gts_set_last_search(self, gen, pid):
        self.q("INSERT OR REPLACE INTO gts_last_search VALUES (?, ?, ?)",
               (gen, pid, time.time()))

    def gts_get_last_search(self, gen, pid):
        row = self.one("SELECT time FROM gts_last_search WHERE gen = ? AND pid = ?",
                       (gen, pid))
        return row["time"] if row else None

    def gts_lock(self, gen, pid, partner, seconds=60):
        now = time.time()
        cur = self.q("UPDATE gts SET locked_until = ?, locked_by = ? WHERE gen = ? AND "
                     "pid = ? AND (locked_until < ? OR locked_by = ?)",
                     (now + seconds, partner, gen, pid, now, partner))
        return cur.rowcount > 0

    def gts_lock_ok(self, gen, pid, partner):
        row = self.one("SELECT 1 FROM gts WHERE gen = ? AND pid = ? AND "
                       "(locked_until < ? OR locked_by = ?)",
                       (gen, pid, time.time(), partner))
        return row is not None

    def gts_log_trade(self, gen, pid, partner, species):
        self.q("INSERT INTO gts_log (gen, pid, partner, species, time) VALUES (?, ?, ?, ?, ?)",
               (gen, pid, partner, species, int(time.time())))

    def gts_list(self):
        return [dict(r) for r in self.all(
            "SELECT gen, pid, species, gender, level, req_species, req_min, req_max, "
            "is_exchanged, deposited FROM gts ORDER BY deposited DESC")]

    # ------------------------------------------------------------- uploads
    def new_serial(self) -> int:
        while True:
            serial = secrets.randbelow(900000000000) + 100000000000
            if not self.one("SELECT 1 FROM uploads WHERE serial = ?", (serial,)):
                return serial

    def upload_add(self, kind, pid, data, header=b"", search1=0, country=0,
                   region=0, meta=0):
        # Refuse exact duplicates (the game re-sends after timeouts).
        dup = self.one("SELECT serial FROM uploads WHERE kind = ? AND pid = ? AND data = ?",
                       (kind, pid, data))
        if dup:
            return 0
        serial = self.new_serial()
        self.q("INSERT INTO uploads (serial, kind, pid, search1, country, region, meta, "
               "header, data, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (serial, kind, pid, search1, country, region, meta, header, data,
                int(time.time())))
        return serial

    def upload_get(self, kind, serial):
        row = self.one("SELECT * FROM uploads WHERE kind = ? AND serial = ?", (kind, serial))
        return dict(row) if row else None

    def upload_search(self, kind, search1=None, country=None, region=None,
                      meta=None, limit=30, order="created DESC"):
        where, args = ["kind = ?"], [kind]
        for col, val in (("search1", search1), ("country", country),
                         ("region", region), ("meta", meta)):
            if val is not None:
                where.append("%s = ?" % col)
                args.append(val)
        args.append(limit)
        return [dict(r) for r in self.all(
            "SELECT * FROM uploads WHERE %s ORDER BY %s LIMIT ?" % (" AND ".join(where), order),
            args)]

    def upload_search_species(self, kind, species, limit=30):
        """Battle videos: species anywhere in the party list (stored in header)."""
        rows = self.upload_search(kind, limit=1000)
        out = []
        for r in rows:
            hdr = r["header"]
            party = [int.from_bytes(hdr[0x80 + i * 2:0x82 + i * 2], "little") for i in range(12)]
            if species in party:
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def upload_flag_saved(self, kind, serial):
        return self.q("UPDATE uploads SET saved = saved + 1 WHERE kind = ? AND serial = ?",
                      (kind, serial)).rowcount > 0

    # ------------------------------------------------------------- battle tower
    def tower_update(self, gen, pid, rank, room, won, data, leader):
        self.q("INSERT OR REPLACE INTO tower VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (gen, pid, rank, room, won, data, 1 if leader else 0, int(time.time())))

    def tower_opponents(self, gen, pid, rank, room, count):
        return [dict(r) for r in self.all(
            "SELECT * FROM tower WHERE gen = ? AND rank = ? AND room = ? AND pid != ? "
            "ORDER BY won DESC, updated DESC LIMIT ?", (gen, rank, room, pid, count))]

    def tower_leaders(self, gen, rank, room, count=30):
        return [dict(r) for r in self.all(
            "SELECT * FROM tower WHERE gen = ? AND rank = ? AND room = ? AND leader = 1 "
            "ORDER BY updated DESC LIMIT ?", (gen, rank, room, count))]

    # ------------------------------------------------------------- Global Link
    def pgl_get(self, gsid):
        row = self.one("SELECT * FROM pgl_players WHERE gsid = ?", (gsid,))
        return dict(row) if row else None

    def pgl_exists(self, gsid):
        return self.one("SELECT 1 FROM pgl_players WHERE gsid = ?", (gsid,)) is not None

    def pgl_create(self, gsid, rom=None, lang=None):
        self.q("INSERT OR IGNORE INTO pgl_players (gsid, rom, lang, status, updated) "
               "VALUES (?, ?, ?, 0, ?)", (gsid, rom, lang, int(time.time())))
        return self.pgl_get(gsid)

    def pgl_update(self, gsid, **fields):
        if not fields:
            return
        cols = ", ".join("%s = ?" % k for k in fields)
        args = list(fields.values()) + [int(time.time()), gsid]
        self.q("UPDATE pgl_players SET %s, updated = ? WHERE gsid = ?" % cols, args)

    # ------------------------------------------------------------- stats
    def counts(self):
        def c(sql, args=()):
            return self.one(sql, args)[0]
        return {
            "users": c("SELECT count(*) FROM users"),
            "gts4": c("SELECT count(*) FROM gts WHERE gen = 4 AND is_exchanged = 0"),
            "gts5": c("SELECT count(*) FROM gts WHERE gen = 5 AND is_exchanged = 0"),
            "trades": c("SELECT count(*) FROM gts_log"),
            "battle_videos": c("SELECT count(*) FROM uploads WHERE kind IN ('bv4', 'bv5')"),
            "dressups": c("SELECT count(*) FROM uploads WHERE kind = 'dressup4'"),
            "boxes": c("SELECT count(*) FROM uploads WHERE kind = 'box4'"),
            "musicals": c("SELECT count(*) FROM uploads WHERE kind = 'musical5'"),
        }


def _base32(num: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuv"
    out = ""
    while num:
        num, r = divmod(num, 32)
        out = alphabet[r] + out
    return out.rjust(9, "0")
