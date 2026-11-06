#!/usr/bin/env python3
"""Forever Tracker backend.

- POST /api/ingest   companion uploads (LAN only, bearer token)
- GET  /api/state    latest snapshot + online status
- GET  /api/history  position trail, XP samples, event log
- GET  /api/stream   Server-Sent Events; fires "update" after each ingest
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

class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                t INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (t, type, data));
            CREATE TABLE IF NOT EXISTS positions (
                t INTEGER PRIMARY KEY, mapID INTEGER, map TEXT, x REAL, y REAL);
            CREATE TABLE IF NOT EXISTS xp (
                t INTEGER PRIMARY KEY, level INTEGER, xp INTEGER, xpMax INTEGER);
        """)
        self.live_version = 0
        self.history_version = 0
        self.changed = threading.Condition()
        self.last_position = None
        self.last_xp = None
        self.combat_ended_at = None

    def get(self, key):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def notify(self, history=False):
        with self.changed:
            self.live_version += 1
            if history:
                self.history_version += 1
            self.changed.notify_all()

    def add_events(self, events):
        added = 0
        for e in events or []:
            if isinstance(e, dict) and isinstance(e.get("t"), int) and e.get("type"):
                data = {k: v for k, v in e.items() if k in EVENT_FIELDS}
                cur = self.conn.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?)",
                                        (e["t"], str(e["type"]), json.dumps(data, sort_keys=True)))
                added += cur.rowcount
        return added

    def ingest_live(self, payload, now):
        frame = payload.get("frame") or {}
        flags = frame.get("flags") or {}
        snap = self.get("snapshot") or {}
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
        self.assume_full(snap, now, frame)
        snap["status"] = "offline" if flags.get("leaving") else "online"
        snap["t"] = frame.get("t") or now

        history = self.add_events(payload.get("events")) > 0
        if "quests" in payload:
            self.put("quests", {"t": now, "list": clean_quests(payload["quests"])})
            history = True
        history |= self.put_sheet(payload, now)
        if snap.get("name"):
            self.put("snapshot", snap)
            self.put("updatedAt", now)
            self.put("liveAt", now)
            history |= self.sample_live(snap, now)
        self.put("presence", {"online": snap["status"] == "online", "t": now})
        return history

    def assume_full(self, snap, now, frame):
        """After FULL_AFTER seconds out of combat with no newer combat-log
        reading, health and power have regenerated: show them as full (rage
        as empty)."""
        snap["vitalsAssumed"] = False
        if snap["combat"] or self.combat_ended_at is None:
            self.combat_ended_at = None if snap["combat"] else now
            return
        if snap["dead"] or snap["ghost"] or now - self.combat_ended_at < FULL_AFTER:
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

    def sample_live(self, s, now):
        """Adds trail and XP points from live data at a modest rate."""
        added = False
        if s.get("x") is not None and s.get("mapID"):
            last = self.last_position
            moved = (not last or last[1] != s["mapID"]
                     or abs(last[2] - s["x"]) > 0.002 or abs(last[3] - s["y"]) > 0.002)
            if moved and (not last or now - last[0] >= POSITION_EVERY):
                self.conn.execute("INSERT OR IGNORE INTO positions VALUES (?, ?, ?, ?, ?)",
                                  (now, s["mapID"], s.get("map"), s["x"], s["y"]))
                self.last_position = (now, s["mapID"], s["x"], s["y"])
                added = True
        if s.get("xp") is not None and s.get("xpMax"):
            last = self.last_xp
            key = (s["level"], s["xp"])
            if not last or (last[1] != key and (key[0] != last[1][0] or now - last[0] >= XP_EVERY)):
                self.conn.execute("INSERT OR IGNORE INTO xp VALUES (?, ?, ?, ?)",
                                  (now, s["level"], s["xp"], s["xpMax"]))
                self.last_xp = (now, key)
                added = True
        return added

    def ingest(self, payload):
        kind = payload.get("kind")
        now = int(time.time())
        history = False
        with self.lock, self.conn:
            if kind == "presence":
                self.put("presence", {"online": bool(payload.get("online")), "t": now})
            elif kind == "live":
                history = self.ingest_live(payload, now)
            elif kind == "snapshot":
                snap = {k: v for k, v in (payload.get("snapshot") or {}).items()
                        if k in SNAPSHOT_FIELDS}
                if not snap.get("name"):
                    raise ValueError("snapshot without character name")
                # The saved file has no surname; keep the one live data gave us.
                old = self.get("snapshot") or {}
                if old.get("name") == snap["name"] and old.get("surname"):
                    snap["surname"] = old["surname"]
                # While live data flows it is fresher than any saved file.
                live = now - (self.get("liveAt") or 0) < LIVE_STALE
                if not live:
                    self.put("snapshot", snap)
                    self.put("updatedAt", now)
                    online = snap.get("status") == "online" and payload.get("gameRunning") is not False
                    self.put("presence", {"online": online, "t": now})
                    if payload.get("quests") is not None:
                        self.put("quests", {"t": now, "list": clean_quests(payload["quests"])})
                    self.put_sheet(payload, now)
                history = True
                self.add_events(payload.get("events"))
                for p in payload.get("positions") or []:
                    if isinstance(p, dict) and isinstance(p.get("t"), int) and "x" in p:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO positions VALUES (?, ?, ?, ?, ?)",
                            (p["t"], p.get("mapID"), p.get("map"), p.get("x"), p.get("y")))
                for s in payload.get("xp") or []:
                    if isinstance(s, dict) and isinstance(s.get("t"), int):
                        self.conn.execute("INSERT OR IGNORE INTO xp VALUES (?, ?, ?, ?)",
                                          (s["t"], s.get("level"), s.get("xp"), s.get("xpMax")))
            else:
                raise ValueError(f"unknown kind {kind!r}")
        self.notify(history)

    def put_sheet(self, payload, now):
        """Stores gear and professions; returns whether either changed."""
        changed = False
        for key, clean in (("gear", clean_gear), ("professions", clean_professions)):
            if payload.get(key) is None:
                continue
            items = clean(payload[key])
            old = self.get(key) or {}
            if old.get("items") != items:
                changed = True
            self.put(key, {"t": now if old.get("items") != items else old.get("t", now), "items": items})
        return changed

    def state(self, quests=True):
        with self.lock:
            presence = self.get("presence") or {"online": False, "t": 0}
            snapshot = self.get("snapshot")
            updated = self.get("updatedAt")
            live_at = self.get("liveAt") or 0
            quest_state = self.get("quests") if quests else None
            gear = self.get("gear") if quests else None
            professions = self.get("professions") if quests else None
        now = int(time.time())
        online = presence["online"] and now - presence["t"] < PRESENCE_TIMEOUT
        state = {
            "serverTime": now,
            "online": online,
            "live": online and now - live_at < LIVE_STALE,
            "presenceAt": presence["t"],
            "updatedAt": updated,
            "snapshot": snapshot,
        }
        if quests:
            state["quests"] = quest_state
            state["gear"] = gear
            state["professions"] = professions
        return state

    def history(self, limit_positions=500, limit_events=150):
        with self.lock:
            positions = self.conn.execute(
                "SELECT t, mapID, map, x, y FROM positions ORDER BY t DESC LIMIT ?",
                (limit_positions,)).fetchall()
            xp = self.conn.execute("SELECT t, level, xp, xpMax FROM xp ORDER BY t").fetchall()
            events = self.conn.execute(
                "SELECT data FROM events ORDER BY t DESC LIMIT ?", (limit_events,)).fetchall()
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
        if url.path == "/api/state":
            return self.send_json(STORE.state())
        if url.path == "/api/history":
            return self.send_json(STORE.history())
        if url.path == "/api/stream":
            return self.stream()
        if url.path.startswith("/api/"):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        return self.static(url.path)

    def stream(self):
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
                    state = json.dumps(STORE.state(quests=False), separators=(",", ":"))
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
