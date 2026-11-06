#!/usr/bin/env python3
"""Forever Tracker companion.

Watches the ForeverTracker SavedVariables file that WoW writes on reload,
logout and exit, and forwards it to the tracker backend on the LAN. The
backend address comes from the addon's in-game setting; the shared token comes
from config.json next to this script. Standard library only.
"""

import argparse
import glob
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import combatlog
import strip

HERE = os.path.dirname(os.path.abspath(__file__))
SV_NAME = "ForeverTracker.lua"
GAME_PROCESS = re.compile(r"^wow(classic|forever|t|b)?(-64)?\.exe$|^world of warcraft", re.I)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --- SavedVariables (Lua table literal) parser ------------------------------

class LuaParser:
    """Parses the subset of Lua that WoW uses to serialise SavedVariables."""

    TOKEN = re.compile(r"""
        (?P<ws>\s+|--[^\n]*)
      | (?P<str>"(?:\\.|[^"\\])*")
      | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|-?0[xX][0-9a-fA-F]+|-?(?:inf|nan))
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<sym>[{}\[\]=,;])
    """, re.X)

    ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'",
               "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\n": "\n"}

    def __init__(self, text):
        self.tokens = []
        pos = 0
        while pos < len(text):
            m = self.TOKEN.match(text, pos)
            if not m:
                raise ValueError(f"unexpected character at {pos}: {text[pos:pos+20]!r}")
            pos = m.end()
            if m.lastgroup != "ws":
                self.tokens.append((m.lastgroup, m.group()))
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ValueError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def unescape(self, s):
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c != "\\":
                out.append(c)
                i += 1
                continue
            n = s[i + 1]
            if n.isdigit():
                m = re.match(r"\d{1,3}", s[i + 1:])
                out.append(chr(int(m.group())))
                i += 1 + len(m.group())
            else:
                out.append(self.ESCAPES.get(n, n))
                i += 2
        # The file is read as latin-1 so every char is one byte; WoW writes
        # UTF-8, and decimal escapes are bytes too.
        return "".join(out).encode("latin-1").decode("utf-8", "replace")

    def value(self):
        kind, text = self.take()
        if kind == "str":
            return self.unescape(text[1:-1])
        if kind == "num":
            if "x" in text.lower():
                return int(text, 16)
            f = float(text)
            return int(f) if f.is_integer() and re.fullmatch(r"-?\d+", text) else f
        if kind == "name":
            return {"true": True, "false": False, "nil": None}.get(text, text)
        if text == "{":
            return self.table()
        raise ValueError(f"unexpected token {text!r}")

    def table(self):
        array, mapping = [], {}
        while self.peek()[1] != "}":
            kind, text = self.peek()
            if text == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                mapping[key] = self.value()
            elif kind == "name" and self.tokens[self.i + 1][1] == "=":
                self.take()
                self.take("=")
                mapping[text] = self.value()
            else:
                array.append(self.value())
            if self.peek()[1] in (",", ";"):
                self.take()
        self.take("}")
        # Tables keyed 1..n are lists.
        if not array and mapping and all(isinstance(k, int) for k in mapping):
            keys = sorted(mapping)
            if keys == list(range(1, len(keys) + 1)):
                return [mapping[k] for k in keys]
        if array and not mapping:
            return array
        if array:
            for idx, v in enumerate(array, 1):
                mapping.setdefault(idx, v)
        return mapping if mapping or not array else array

    def assignments(self):
        result = {}
        while self.i < len(self.tokens):
            _, name = self.take()
            self.take("=")
            result[name] = self.value()
        return result


def parse_saved_variables(path):
    with open(path, "rb") as f:
        text = f.read().decode("latin-1")
    return LuaParser(text).assignments()


# --- helpers ----------------------------------------------------------------

def find_saved_variables(cfg):
    explicit = cfg.get("saved_variables")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    root = cfg.get("wow_path", "")
    pattern = os.path.join(root, "_*_", "WTF", "Account", "*", "SavedVariables", SV_NAME)
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def game_running():
    """True/False if the WoW client process is running, None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
                except OSError:
                    continue
                if GAME_PROCESS.match(re.split(r"[\\/]", argv0)[-1]):
                    return True
            return False
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10).stdout
            names = [line.split('","')[0].strip('"') for line in out.splitlines()]
        else:
            out = subprocess.run(["ps", "-axo", "comm"], capture_output=True,
                                 text=True, timeout=10).stdout
            names = [os.path.basename(line.strip()) for line in out.splitlines()]
        return any(GAME_PROCESS.match(n) for n in names)
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        log(f"process check failed: {exc}")
        return None


def endpoint_url(endpoint):
    """Builds the ingest URL, refusing anything that isn't a private IPv4."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)(?::(\d+))?", str(endpoint or ""))
    if not m:
        return None
    try:
        ip = ipaddress.IPv4Address(m.group(1))
    except ValueError:
        return None
    if not (ip.is_private or ip.is_loopback):
        return None
    return f"http://{ip}:{int(m.group(2) or 8787)}/api/ingest"


def rows(payload):
    """The strip's compact lists; an empty one arrives as {}."""
    return [r for r in payload if isinstance(r, list)] if isinstance(payload, list) else []


def expand_gear(payload):
    """[slot, id, quality, ilvl, enchant, suffix, name] -> item dicts."""
    items = []
    for r in rows(payload):
        if len(r) >= 7:
            slot, item_id, quality, ilvl, enchant, suffix, name = r[:7]
            items.append({"slot": slot, "id": item_id or None, "quality": None if quality == -1 else quality,
                          "ilvl": ilvl or None, "enchant": enchant or None, "suffix": suffix or None,
                          "name": name or None})
    return items


def expand_professions(payload):
    """[name, rank, max, bonus, secondary] -> profession dicts."""
    return [{"name": r[0], "rank": r[1], "max": r[2], "bonus": r[3] or None, "secondary": bool(r[4])}
            for r in rows(payload) if len(r) >= 5]


def post(url, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


# --- main loop ---------------------------------------------------------------

class Companion:
    LIVE_EVERY = 1.0      # seconds between live uploads
    CAPTURE_EVERY = 0.05  # seconds between strip reads
    LIVE_STALE = 3.0      # no valid strip for this long = not live

    def __init__(self, cfg):
        self.cfg = cfg
        self.token = cfg["token"]
        self.poll = cfg.get("poll_seconds", 2)
        self.heartbeat_every = cfg.get("heartbeat_seconds", 30)
        self.path = None
        self.mtime = None
        self.db = None
        self.pending = None
        self.last_heartbeat = 0
        self.was_running = None
        self.last_file_check = 0

        self.live = cfg.get("live", True)
        self.capture = None
        self.capture_failures = 0
        self.capture_retry_at = 0
        self.reasm = strip.Reassembler()
        self.frame = None
        self.frame_at = 0
        self.identity = None
        self.zone = None
        self.quests = None
        self.quests_dirty = False
        self.gear = None
        self.professions = None
        self.sheet_dirty = False   # gear or professions changed
        self.target = None         # attackable creature targeted in game, from the strip
        self.events = []
        self.last_live_post = 0
        self.last_hidden_post = 0
        self.live_logged = False
        self.combat_log = combatlog.CombatLogTail(cfg["wow_path"]) if cfg.get("wow_path") else None

    def settings(self):
        return (self.db or {}).get("settings") or {}

    def sharing(self):
        s = self.settings()
        return bool(s.get("enabled")) and not s.get("appearOffline")

    def build_payload(self, running):
        s = self.settings()
        if not s.get("enabled"):
            return None  # tracking off: nothing leaves this machine
        if s.get("appearOffline"):
            return {"kind": "presence", "online": False}
        snap = self.db.get("snapshot")
        if not snap:
            return None
        return {
            "kind": "snapshot",
            "gameRunning": running,
            "snapshot": snap,
            "events": self.db.get("events") or [],
            "positions": self.db.get("positions") or [],
            "xp": self.db.get("xp") or [],
            "quests": self.db.get("quests"),
            "gear": self.db.get("gear"),
            "professions": self.db.get("professions"),
        }

    def send(self, payload):
        endpoint = self.settings().get("endpoint") or self.cfg.get("endpoint")
        url = endpoint_url(endpoint)
        if not url:
            log(f"endpoint {endpoint!r} is not a LAN address; not sending")
            return True  # don't retry an invalid target
        try:
            post(url, self.token, payload)
            return True
        except (urllib.error.URLError, OSError) as exc:
            log(f"send to {url} failed: {exc}")
            return False

    def check_file(self):
        path = find_saved_variables(self.cfg)
        if not path:
            return
        mtime = os.path.getmtime(path)
        if path == self.path and mtime == self.mtime:
            return
        try:
            self.db = parse_saved_variables(path).get("ForeverTrackerDB") or {}
        except (ValueError, OSError) as exc:
            # The game may still be writing the file; retry next poll.
            log(f"could not parse {path}: {exc}")
            return
        self.path, self.mtime = path, mtime
        running = game_running()
        self.pending = self.build_payload(running)
        log(f"saved variables changed ({path}); "
            + ("sending update" if self.pending else "tracking off, not sending"))

    # --- live strip ---------------------------------------------------------

    def read_strip(self, now):
        if self.capture is None:
            self.capture = strip.make_capture()
        data = self.capture.grab()
        frame = strip.decode(data) if data else None
        if not frame:
            return
        self.frame, self.frame_at = frame, now
        if not self.live_logged:
            log("reading live data from the in-game strip")
            self.live_logged = True
        chunk = frame.pop("chunk", None)
        msg = chunk and self.reasm.add(chunk, now)
        if not msg:
            return
        kind, payload = msg
        if kind == "I":
            self.identity = payload
        elif kind == "Z":
            self.zone = payload
        elif kind == "Q":
            if payload != self.quests:
                self.quests, self.quests_dirty = payload, True
        elif kind == "T":
            self.target = payload if isinstance(payload, dict) and payload.get("name") else None
        elif kind == "G":
            gear = expand_gear(payload)
            if gear != self.gear:
                self.gear, self.sheet_dirty = gear, True
        elif kind == "P":
            professions = expand_professions(payload)
            if professions != self.professions:
                self.professions, self.sheet_dirty = professions, True
        elif kind == "E":
            self.events.append(payload)

    def is_live(self, now):
        return self.frame is not None and now - self.frame_at < self.LIVE_STALE

    def enemy(self, flags):
        """What you're fighting, while in combat: health from the combat log;
        level and rank when it's your target."""
        if not flags.get("combat"):
            return None
        target = self.target
        foe = self.combat_log.enemy(target)
        if foe is None and target and not target.get("dead"):
            # Targeted but not in the log yet (no blows traded): name only.
            foe = {"name": target["name"], "health": None, "healthMax": None, "targeted": True, "more": 0}
        if foe and foe["targeted"]:
            foe.update(level=target.get("level"), rank=target.get("rank"))
            if foe["health"] is None and target.get("pct") is not None:
                foe["pct"] = target["pct"]
        return foe

    def post_live(self, now):
        if now - self.last_live_post < self.LIVE_EVERY:
            return
        if not endpoint_url(self.settings().get("endpoint") or self.cfg.get("endpoint")):
            return  # keep queued events until we know where to send them
        self.last_live_post = now
        flags = self.frame["flags"]
        if flags["hidden"]:
            # Appear offline: only ever say so, nothing else.
            if now - self.last_hidden_post > self.heartbeat_every:
                if self.send({"kind": "presence", "online": False}):
                    self.last_hidden_post = now
            return
        if not flags["sharing"]:
            return
        self.last_hidden_post = 0
        payload = {"kind": "live", "frame": self.frame, "identity": self.identity,
                   "zone": self.zone, "events": self.events}
        # Health/power from the game's combat log, only while the in-game
        # setting is on (the strip says so).
        if self.combat_log and flags.get("combatLog"):
            try:
                self.combat_log.poll()
            except OSError as exc:
                log(f"combat log read failed: {exc}")
            if self.combat_log.vitals:
                payload["vitals"] = self.combat_log.vitals
            payload["enemy"] = self.enemy(flags)
            surname = self.identity and self.combat_log.surname_for(self.identity.get("name"))
            if surname:
                payload["identity"] = {**self.identity, "surname": surname}
        if self.quests_dirty:
            payload["quests"] = self.quests
        if self.sheet_dirty:
            if self.gear is not None:
                payload["gear"] = self.gear
            if self.professions is not None:
                payload["professions"] = self.professions
        if self.send(payload):
            self.events = []
            self.quests_dirty = False
            self.sheet_dirty = False
            self.last_heartbeat = now

    # --- main loop -----------------------------------------------------------

    def tick(self):
        now = time.time()
        # The settings file goes first: it holds the backend address.
        if now - self.last_file_check >= self.poll:
            self.last_file_check = now
            self.check_file()
            if self.pending and self.send(self.pending):
                self.pending = None
                self.last_heartbeat = now

        if self.live and now >= self.capture_retry_at:
            try:
                self.read_strip(now)
                self.capture_failures = 0
            except Exception as exc:  # noqa: BLE001 - capture is best effort
                # Back off (up to 30 s) instead of retrying 20 times a second.
                self.capture_failures += 1
                self.capture_retry_at = now + min(30, 2 ** self.capture_failures)
                if self.capture_failures <= 3 or self.capture_failures % 20 == 0:
                    log(f"strip capture failed: {exc}")
                self.capture = None
            if self.is_live(now):
                self.post_live(now)

        if self.db is None or not self.sharing() or self.is_live(now):
            return
        if now - self.last_heartbeat < self.heartbeat_every:
            return
        running = game_running()
        snap = self.db.get("snapshot") or {}
        online = running is not False and snap.get("status") == "online"
        if online or self.was_running:
            if self.send({"kind": "presence", "online": online}):
                self.last_heartbeat = now
                self.was_running = online

    def run(self):
        log("Forever Tracker companion started")
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                log(f"error: {exc}")
            time.sleep(self.CAPTURE_EVERY if self.live else self.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dump", action="store_true",
                    help="parse the SavedVariables file once, print the payload and exit")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    if args.dump:
        path = find_saved_variables(cfg)
        if not path:
            sys.exit("ForeverTracker.lua not found; check wow_path / saved_variables")
        c = Companion(cfg)
        c.db = parse_saved_variables(path).get("ForeverTrackerDB") or {}
        print(json.dumps({"file": path, "settings": c.settings(),
                          "payload": c.build_payload(game_running())}, indent=2))
        return

    Companion(cfg).run()


if __name__ == "__main__":
    main()
