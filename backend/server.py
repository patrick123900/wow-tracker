#!/usr/bin/env python3
"""Forever Tracker backend.

- POST /api/ingest   companion uploads (LAN only, bearer token)
- GET  /api/characters   every tracked character, most recently played first
- GET  /api/state    latest snapshot + online status          (?c=<Name-Realm>, default: last played)
- GET  /api/history  position trail, XP samples, event log    (?c=...)
- GET  /api/stream   Server-Sent Events: "live" state each second, "update" when history changed (?c=...)
- everything else    static frontend

Standard library only. Configure with environment variables (see README).
"""

import ipaddress
import json
import mimetypes
import os
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("FT_TOKEN", "")
DB_PATH = os.environ.get("FT_DB", os.path.join(HERE, "tracker.sqlite3"))
HOST = os.environ.get("FT_HOST", "0.0.0.0")
PORT = int(os.environ.get("FT_PORT", "8787"))
STATIC = os.path.realpath(os.environ.get("FT_STATIC", os.path.join(HERE, "..", "frontend")))
INGEST_NETS = [ipaddress.ip_network(n.strip()) for n in os.environ.get(
    "FT_INGEST_NETS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8").split(",")]
PRESENCE_TIMEOUT = 120   # seconds without a heartbeat before showing offline
MAX_BODY = 2 * 1024 * 1024

# Only these snapshot fields are stored and published.
SNAPSHOT_FIELDS = {
    "t", "status", "name", "surname", "realm", "faction", "race", "class", "classToken", "sex",
    "level", "maxLevel", "xp", "xpMax", "rested", "health", "healthMax", "healthPct", "power",
    "money", "guild", "dead", "ghost", "resting", "combat", "zone", "subzone",
    "mapID", "map", "x", "y", "vitalsAt", "vitalsAssumed", "enemy",
}
EVENT_FIELDS = {"t", "type", "level", "zone", "subzone", "x", "y", "mapID", "skill", "rank", "max", "learned",
                "questID", "title", "xp", "money"}
IDENTITY_FIELDS = {"name", "surname", "realm", "faction", "race", "class", "classToken", "sex", "guild", "maxLevel"}
ZONE_FIELDS = {"zone", "subzone", "map"}
LIVE_FIELDS = {"level", "xp", "xpMax", "rested", "health", "healthMax", "healthPct", "money", "mapID", "x", "y"}
LIVE_STALE = 10            # seconds; a live frame newer than this beats a file snapshot
FULL_AFTER = int(os.environ.get("FT_FULL_AFTER", "30"))  # seconds out of combat before vitals count as full
POSITION_EVERY = 5         # seconds between stored trail points from live data
XP_EVERY = 30
MAX_QUESTS = 60


def clean_quests(quests):
    """Keeps only the expected quest fields and sane sizes."""
    out = []
    for q in (quests if isinstance(quests, list) else [])[:MAX_QUESTS]:
        if not isinstance(q, dict) or not isinstance(q.get("title"), str):
            continue
        objectives = []
        for o in (q.get("objectives") if isinstance(q.get("objectives"), list) else [])[:12]:
            if isinstance(o, dict) and isinstance(o.get("text"), str):
                objectives.append({"text": o["text"][:200], "done": bool(o.get("done")),
                                   "have": o.get("have") if isinstance(o.get("have"), int) else None,
                                   "need": o.get("need") if isinstance(o.get("need"), int) else None})
        out.append({
            "id": q.get("id") if isinstance(q.get("id"), int) else None,
            "title": q["title"][:200],
            "level": q.get("level") if isinstance(q.get("level"), int) else None,
            "header": q.get("header") if isinstance(q.get("header"), str) else None,
            "complete": bool(q.get("complete")),
            "failed": bool(q.get("failed")),
            "objectives": objectives,
        })
    return out


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _text(v, limit=100):
    return v[:limit] if isinstance(v, str) and v else None


def clean_gear(items):
    """One item per equipment slot (1-19), expected fields only."""
    out = {}
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and _int(it.get("slot")) in range(1, 20):
            out[it["slot"]] = {"slot": it["slot"], "id": _int(it.get("id")), "name": _text(it.get("name")),
                               "quality": _int(it.get("quality")), "ilvl": _int(it.get("ilvl")),
                               "enchant": _int(it.get("enchant")), "suffix": _int(it.get("suffix"))}
    return [out[k] for k in sorted(out)]


ENEMY_RANKS = {"elite", "rare", "rareelite", "worldboss"}


def clean_enemy(e):
    """What the player is fighting (a creature; never another player)."""
    if not isinstance(e, dict) or not _text(e.get("name")):
        return None
    return {"name": _text(e["name"], 60), "level": _int(e.get("level")),
            "rank": e.get("rank") if e.get("rank") in ENEMY_RANKS else None,
            "targeted": bool(e.get("targeted")), "more": min(_int(e.get("more")) or 0, 40)}


def clean_professions(items):
    out = []
    for p in (items if isinstance(items, list) else [])[:12]:
        if isinstance(p, dict) and _text(p.get("name")):
            out.append({"name": _text(p["name"], 60), "rank": _int(p.get("rank")), "max": _int(p.get("max")),
                        "bonus": _int(p.get("bonus")), "secondary": bool(p.get("secondary"))})
    return out


# --- storage -----------------------------------------------------------------
#
# Each character ("Name-Realm") has its own history rows and its own kv
# entries ("char:<key>:snapshot" etc). Global kv: "presence" (online, t, and
# which character) and "current" (the character played most recently).

CHAR_KV = ("snapshot", "quests", "gear", "professions", "liveAt", "updatedAt")
CHARACTER_FIELDS = ("name", "surname", "realm", "faction", "race", "class", "classToken", "sex", "level")


def char_key(name, realm):
    return f"{name}-{realm}" if isinstance(name, str) and name and isinstance(realm, str) and realm else None


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.migrate()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                char TEXT NOT NULL, t INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (char, t, type, data));
            CREATE TABLE IF NOT EXISTS positions (
                char TEXT NOT NULL, t INTEGER NOT NULL, mapID INTEGER, map TEXT, x REAL, y REAL,
                PRIMARY KEY (char, t));
            CREATE TABLE IF NOT EXISTS xp (
                char TEXT NOT NULL, t INTEGER NOT NULL, level INTEGER, xp INTEGER, xpMax INTEGER,
                PRIMARY KEY (char, t));
        """)
        self.live_version = 0
        self.history_version = 0
        self.changed = threading.Condition()
        self.last_position = {}     # character -> (t, mapID, x, y)
        self.last_xp = {}           # character -> (t, (level, xp))
        self.combat_ended_at = {}   # character -> time its last fight ended

    def migrate(self):
        """Before per-character storage, one character's data had no key. It
        all goes to that character (named in its snapshot), in one transaction."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(events)")]
        if not cols or "char" in cols:
            return
        snap = self.get("snapshot") or {}
        owner = char_key(snap.get("name"), snap.get("realm"))
        if owner is None:
            raise SystemExit("old database without a character snapshot; can't tell whose history it is")
        with self.conn:
            self.conn.executescript("""
                ALTER TABLE events RENAME TO events_v1;
                ALTER TABLE positions RENAME TO positions_v1;
                ALTER TABLE xp RENAME TO xp_v1;
                CREATE TABLE events (
                    char TEXT NOT NULL, t INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (char, t, type, data));
                CREATE TABLE positions (
                    char TEXT NOT NULL, t INTEGER NOT NULL, mapID INTEGER, map TEXT, x REAL, y REAL,
                    PRIMARY KEY (char, t));
                CREATE TABLE xp (
                    char TEXT NOT NULL, t INTEGER NOT NULL, level INTEGER, xp INTEGER, xpMax INTEGER,
                    PRIMARY KEY (char, t));
            """)
            self.conn.execute("INSERT INTO events SELECT ?, t, type, data FROM events_v1", (owner,))
            self.conn.execute("INSERT INTO positions SELECT ?, t, mapID, map, x, y FROM positions_v1", (owner,))
            self.conn.execute("INSERT INTO xp SELECT ?, t, level, xp, xpMax FROM xp_v1", (owner,))
            for table in ("events", "positions", "xp"):
                old = self.conn.execute(f"SELECT count(*) FROM {table}_v1").fetchone()[0]
                new = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if old != new:
                    raise RuntimeError(f"migration lost rows in {table}: {old} -> {new}")
            self.conn.executescript("DROP TABLE events_v1; DROP TABLE positions_v1; DROP TABLE xp_v1;")
            for k in CHAR_KV:
                value = self.get(k)
                if value is not None:
                    self.cput(owner, k, value)
                    self.conn.execute("DELETE FROM kv WHERE key=?", (k,))
            self.put("current", owner)
            presence = self.get("presence") or {"online": False, "t": 0}
            self.put("presence", {**presence, "character": owner})
        print(f"migrated existing history to {owner}", flush=True)

    def get(self, key):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def cget(self, char, key):
        return self.get(f"char:{char}:{key}")

    def cput(self, char, key, value):
        self.put(f"char:{char}:{key}", value)

    def characters(self):
        return [k[len("char:"):-len(":snapshot")] for (k,) in self.conn.execute(
            "SELECT key FROM kv WHERE key LIKE 'char:%:snapshot'")]

    def resolve(self, char):
        """The character asked for, or the one played most recently."""
        with self.lock:
            if char and self.cget(char, "snapshot") is not None:
                return char
            return self.get("current")

    def notify(self, history=False):
        with self.changed:
            self.live_version += 1
            if history:
                self.history_version += 1
            self.changed.notify_all()

    def add_events(self, char, events):
        added = 0
        for e in events or []:
            if isinstance(e, dict) and isinstance(e.get("t"), int) and e.get("type"):
                data = {k: v for k, v in e.items() if k in EVENT_FIELDS}
                cur = self.conn.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                                        (char, e["t"], str(e["type"]), json.dumps(data, sort_keys=True)))
                added += cur.rowcount
        return added

    def ingest_live(self, char, payload, now):
        frame = payload.get("frame") or {}
        flags = frame.get("flags") or {}
        snap = self.cget(char, "snapshot") or {}
        for part, fields in ((payload.get("identity"), IDENTITY_FIELDS),
                             (payload.get("zone"), ZONE_FIELDS)):
            if isinstance(part, dict):
                snap.update({k: v for k, v in part.items() if k in fields})
        # Missing (secret) values become None so the page shows them as hidden.
        for k in LIVE_FIELDS:
            snap[k] = frame.get(k)
        power = frame.get("power")
        if isinstance(power, dict):
            snap["power"] = {k: power.get(k) for k in ("type", "cur", "max", "pct")}
        # The combat log fills in values the addon can't read (secret).
        vitals = payload.get("vitals")
        snap["vitalsAt"] = None
        if isinstance(vitals, dict) and isinstance(vitals.get("t"), (int, float)):
            if snap.get("health") is None and isinstance(vitals.get("health"), int):
                snap["health"], snap["healthMax"] = vitals["health"], vitals.get("healthMax")
                snap["vitalsAt"] = vitals["t"]
            vp = vitals.get("power") if isinstance(vitals.get("power"), dict) else {}
            power = snap.get("power") or {}
            if power.get("cur") is None and isinstance(vp.get("cur"), int) and vp.get("type") == power.get("type"):
                snap["power"] = {**power, "cur": vp["cur"], "max": vp.get("max") or power.get("max")}
                snap["vitalsAt"] = vitals["t"]
        for k in ("dead", "ghost", "combat", "resting"):
            snap[k] = bool(flags.get(k))
        snap["enemy"] = clean_enemy(payload.get("enemy")) if snap["combat"] else None
        self.assume_full(char, snap, now, frame)
        snap["status"] = "offline" if flags.get("leaving") else "online"
        snap["t"] = frame.get("t") or now

        history = self.add_events(char, payload.get("events")) > 0
        if "quests" in payload:
            self.cput(char, "quests", {"t": now, "list": clean_quests(payload["quests"])})
            history = True
        history |= self.put_sheet(char, payload, now)
        if snap.get("name"):
            if self.cget(char, "snapshot") is None:
                history = True  # a new character: refresh the character list
            self.cput(char, "snapshot", snap)
            self.cput(char, "updatedAt", now)
            self.cput(char, "liveAt", now)
            self.put("current", char)
            history |= self.sample_live(char, snap, now)
        self.put("presence", {"online": snap["status"] == "online", "t": now, "character": char})
        return history

    def assume_full(self, char, snap, now, frame):
        """After FULL_AFTER seconds out of combat with no newer combat-log
        reading, health and power have regenerated: show them as full (rage
        as empty)."""
        snap["vitalsAssumed"] = False
        ended = self.combat_ended_at.get(char)
        if snap["combat"] or ended is None:
            self.combat_ended_at[char] = None if snap["combat"] else now
            return
        if snap["dead"] or snap["ghost"] or now - ended < FULL_AFTER:
            return
        if snap["vitalsAt"] and now - snap["vitalsAt"] < FULL_AFTER:
            return
        power = snap.get("power") or {}
        if power.get("max") and (frame.get("power") or {}).get("cur") is None:
            snap["power"] = {**power, "cur": 0 if power.get("type") == "RAGE" else power["max"]}
            snap["vitalsAssumed"] = True
        if snap.get("healthMax") and frame.get("health") is None:
            snap["health"] = snap["healthMax"]
            snap["vitalsAssumed"] = True
        if snap["vitalsAssumed"]:
            snap["vitalsAt"] = None

    def sample_live(self, char, s, now):
        """Adds trail and XP points from live data at a modest rate."""
        added = False
        if s.get("x") is not None and s.get("mapID"):
            last = self.last_position.get(char)
            moved = (not last or last[1] != s["mapID"]
                     or abs(last[2] - s["x"]) > 0.002 or abs(last[3] - s["y"]) > 0.002)
            if moved and (not last or now - last[0] >= POSITION_EVERY):
                self.conn.execute("INSERT OR IGNORE INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                                  (char, now, s["mapID"], s.get("map"), s["x"], s["y"]))
                self.last_position[char] = (now, s["mapID"], s["x"], s["y"])
                added = True
        if s.get("xp") is not None and s.get("xpMax"):
            last = self.last_xp.get(char)
            key = (s["level"], s["xp"])
            if not last or (last[1] != key and (key[0] != last[1][0] or now - last[0] >= XP_EVERY)):
                self.conn.execute("INSERT OR IGNORE INTO xp VALUES (?, ?, ?, ?, ?)",
                                  (char, now, s["level"], s["xp"], s["xpMax"]))
                self.last_xp[char] = (now, key)
                added = True
        return added

    def payload_character(self, payload):
        """Which character an upload is about: named by the companion, else
        from its identity or snapshot, else the one played most recently."""
        char = payload.get("character")
        if isinstance(char, str) and 0 < len(char) <= 100:
            return char
        for part in (payload.get("identity"), payload.get("snapshot")):
            if isinstance(part, dict) and char_key(part.get("name"), part.get("realm")):
                return char_key(part["name"], part["realm"])
        return self.get("current")

    def ingest(self, payload):
        kind = payload.get("kind")
        now = int(time.time())
        history = False
        with self.lock, self.conn:
            if kind == "presence":
                presence = self.get("presence") or {}
                self.put("presence", {"online": bool(payload.get("online")), "t": now,
                                      "character": presence.get("character")})
            elif kind == "live":
                char = self.payload_character(payload)
                if not char:
                    raise ValueError("live data without a character")
                history = self.ingest_live(char, payload, now)
            elif kind == "snapshot":
                snap = {k: v for k, v in (payload.get("snapshot") or {}).items()
                        if k in SNAPSHOT_FIELDS}
                if not snap.get("name"):
                    raise ValueError("snapshot without character name")
                char = self.payload_character(payload)
                # The saved file has no surname; keep the one live data gave us.
                old = self.cget(char, "snapshot") or {}
                if old.get("name") == snap["name"] and old.get("surname"):
                    snap["surname"] = old["surname"]
                # While live data flows it is fresher than any saved file.
                live = now - (self.cget(char, "liveAt") or 0) < LIVE_STALE
                if not live:
                    self.cput(char, "snapshot", snap)
                    self.cput(char, "updatedAt", now)
                    self.put("current", char)
                    online = snap.get("status") == "online" and payload.get("gameRunning") is not False
                    self.put("presence", {"online": online, "t": now, "character": char})
                    if payload.get("quests") is not None:
                        self.cput(char, "quests", {"t": now, "list": clean_quests(payload["quests"])})
                    self.put_sheet(char, payload, now)
                history = True
                self.add_events(char, payload.get("events"))
                for p in payload.get("positions") or []:
                    if isinstance(p, dict) and isinstance(p.get("t"), int) and "x" in p:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                            (char, p["t"], p.get("mapID"), p.get("map"), p.get("x"), p.get("y")))
                for s in payload.get("xp") or []:
                    if isinstance(s, dict) and isinstance(s.get("t"), int):
                        self.conn.execute("INSERT OR IGNORE INTO xp VALUES (?, ?, ?, ?, ?)",
                                          (char, s["t"], s.get("level"), s.get("xp"), s.get("xpMax")))
            else:
                raise ValueError(f"unknown kind {kind!r}")
        self.notify(history)

    def put_sheet(self, char, payload, now):
        """Stores gear and professions; returns whether either changed."""
        changed = False
        for key, clean in (("gear", clean_gear), ("professions", clean_professions)):
            if payload.get(key) is None:
                continue
            items = clean(payload[key])
            old = self.cget(char, key) or {}
            if old.get("items") != items:
                changed = True
            self.cput(char, key, {"t": now if old.get("items") != items else old.get("t", now), "items": items})
        return changed

    def online(self, presence, now):
        return bool(presence.get("online")) and now - presence.get("t", 0) < PRESENCE_TIMEOUT

    def character_list(self):
        """Every character with saved data, most recently played first."""
        now = int(time.time())
        with self.lock:
            presence = self.get("presence") or {}
            out = []
            for char in self.characters():
                snap = self.cget(char, "snapshot") or {}
                out.append({"id": char, **{k: snap.get(k) for k in CHARACTER_FIELDS},
                            "online": self.online(presence, now) and presence.get("character") == char,
                            "updatedAt": self.cget(char, "updatedAt")})
        return sorted(out, key=lambda c: c["updatedAt"] or 0, reverse=True)

    def state(self, char, quests=True):
        with self.lock:
            presence = self.get("presence") or {"online": False, "t": 0}
            snapshot = self.cget(char, "snapshot")
            updated = self.cget(char, "updatedAt")
            live_at = self.cget(char, "liveAt") or 0
            extra = {k: self.cget(char, k) for k in ("quests", "gear", "professions")} if quests else {}
        now = int(time.time())
        playing = presence.get("character") == char
        online = playing and self.online(presence, now)
        state = {
            "serverTime": now,
            "character": char,
            "online": online,
            "live": online and now - live_at < LIVE_STALE,
            "presenceAt": presence["t"] if playing else updated,
            "updatedAt": updated,
            "snapshot": snapshot,
        }
        state.update(extra)
        return state

    def history(self, char, limit_positions=500, limit_events=150):
        with self.lock:
            positions = self.conn.execute(
                "SELECT t, mapID, map, x, y FROM positions WHERE char=? ORDER BY t DESC LIMIT ?",
                (char, limit_positions)).fetchall()
            xp = self.conn.execute("SELECT t, level, xp, xpMax FROM xp WHERE char=? ORDER BY t",
                                   (char,)).fetchall()
            events = self.conn.execute(
                "SELECT data FROM events WHERE char=? ORDER BY t DESC LIMIT ?", (char, limit_events)).fetchall()
        return {
            "positions": [dict(zip(("t", "mapID", "map", "x", "y"), r)) for r in reversed(positions)],
            "xp": [dict(zip(("t", "level", "xp", "xpMax"), r)) for r in xp],
            "events": [json.loads(r[0]) for r in events],
        }


STORE = None


# --- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ForeverTracker/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FT_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, msg):
        self.send_json({"error": msg}, status)

    def ingest_allowed(self):
        # Uploads come straight from the companion. Anything relayed by a
        # reverse proxy (i.e. from the internet) is refused.
        if self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"):
            return False
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in INGEST_NETS)

    def do_POST(self):
        if urlparse(self.path).path != "/api/ingest":
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        if not self.ingest_allowed():
            return self.error(HTTPStatus.FORBIDDEN, "ingest is LAN-only")
        if not TOKEN or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.error(HTTPStatus.UNAUTHORIZED, "bad token")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "bad body size")
        try:
            payload = json.loads(self.rfile.read(length))
            STORE.ingest(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        self.send_json({"ok": True})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/characters":
            return self.send_json(STORE.character_list())
        if url.path in ("/api/state", "/api/history", "/api/stream"):
            char = STORE.resolve((parse_qs(url.query).get("c") or [None])[0])
            if url.path == "/api/state":
                return self.send_json(STORE.state(char) if char else {"serverTime": int(time.time())})
            if url.path == "/api/history":
                return self.send_json(STORE.history(char) if char else {"positions": [], "xp": [], "events": []})
            return self.stream(char)
        if url.path.startswith("/api/"):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        return self.static(url.path)

    def stream(self, char):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        seen_live, seen_history = STORE.live_version, STORE.history_version
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                with STORE.changed:
                    STORE.changed.wait_for(lambda: STORE.live_version != seen_live, timeout=25)
                if STORE.history_version != seen_history:
                    # Journal, trail or quests changed: the page refetches everything.
                    seen_live, seen_history = STORE.live_version, STORE.history_version
                    self.wfile.write(f"event: update\ndata: {seen_history}\n\n".encode())
                elif STORE.live_version != seen_live:
                    seen_live = STORE.live_version
                    state = json.dumps(STORE.state(char, quests=False) if char else {},
                                       separators=(",", ":"))
                    self.wfile.write(f"event: live\ndata: {state}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global STORE
    if not TOKEN:
        raise SystemExit("set FT_TOKEN (shared secret for the companion)")
    STORE = Store(DB_PATH)
    print(f"Forever Tracker backend on http://{HOST}:{PORT} (static: {STATIC}, db: {DB_PATH})",
          flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
