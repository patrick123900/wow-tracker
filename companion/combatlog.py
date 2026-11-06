"""Follows the game's own combat log (Logs/WoWCombatLog*.txt, advanced
logging on) for the player's current health and power.

The client writes an "advanced" block on most lines describing one unit:
    infoGUID, ownerGUID, currentHP, maxHP, attackPower, spellPower, armor,
    3 more fields, powerType, currentPower, maxPower, powerCost, x, y, ...
Only lines about the player's own character are used, plus which creatures
are fighting the player (never other players), for "what you're fighting";
the log also contains everyone nearby, and none of that is passed on.
"""

import csv
import glob
import os
import re
import time
from datetime import datetime

# Unit flags: COMBATLOG_OBJECT_AFFILIATION_MINE | COMBATLOG_OBJECT_TYPE_PLAYER
MINE, PLAYER = 0x1, 0x400
GUID = re.compile(r"^[A-Za-z]+-[0-9A-F]+(-[0-9A-F]+)*$")
NO_OWNER = "0000000000000000"
POWER_TYPES = {"0": "MANA", "1": "RAGE", "2": "FOCUS", "3": "ENERGY"}
FOE_KINDS = ("Creature-", "Vehicle-")
MAX_FOES = 20
FOE_ACTIVE = 15     # seconds since its last line for a creature to count as still fighting
BACKFILL = 64 * 1024  # bytes read from the end of a log when first opened
IDENTITY_SCAN = 8 * 1024 * 1024  # bytes searched from the start for our own name


def find_log(wow_path):
    logs = glob.glob(os.path.join(wow_path, "_*_", "Logs", "WoWCombatLog*.txt"))
    return max(logs, key=os.path.getmtime) if logs else None


def parse_time(stamp):
    """'11/5/2026 15:48:06.5351' (local time) -> epoch seconds."""
    try:
        date, clock = stamp.split(" ")
        dt = datetime.strptime(f"{date} {clock[:15]}", "%m/%d/%Y %H:%M:%S.%f")
        return dt.timestamp()
    except ValueError:
        return None


class CombatLogTail:
    def __init__(self, wow_path):
        self.wow_path = wow_path
        self.path = None
        self.offset = 0
        self.partial = b""
        self.me = None          # player GUID, learned from "mine + player" source flags
        self.full_name = None   # e.g. "Pattes-Heals-PvP-EU": first name, surname, realm
        self.surnames = {}      # first name -> surname, kept across log files
        self.vitals = None      # latest {"t", "health", "healthMax", "power": {...}}
        # creature GUID -> {"name", "t" (last health), "fought" (last blow with us), "health", "healthMax", "dead"}
        self.foes = {}
        self.next_check = 0

    def poll(self):
        """Reads whatever the game appended since the last call."""
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 0.5
        path = find_log(self.wow_path)
        if not path:
            return
        size = os.path.getsize(path)
        if path != self.path or size < self.offset:
            self.path, self.partial, self.me = path, b"", None
            self.offset = max(0, size - BACKFILL)
            skip_first = self.offset > 0
        else:
            skip_first = False
        if size == self.offset:
            return
        with open(path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()  # last piece may be an incomplete line
        if skip_first and lines:
            lines.pop(0)            # we started mid-line
        for raw in lines:
            self.parse(raw.decode("utf-8", "replace").rstrip("\r"))
        if self.me is None and skip_first:
            self.learn_identity(path)

    def learn_identity(self, path):
        """Finds our GUID and full name earlier in the log when the recent
        backfill had no line of ours (e.g. after standing idle)."""
        with open(path, "rb") as f:
            for raw in f:
                if f.tell() > IDENTITY_SCAN:
                    return
                fields = raw.decode("utf-8", "replace").partition("  ")[2].split(",", 4)
                if len(fields) < 4 or not fields[1].startswith("Player-"):
                    continue
                try:
                    flags = int(fields[3], 16)
                except ValueError:
                    continue
                if flags & MINE and flags & PLAYER:
                    self.me, self.full_name = fields[1], fields[2].strip('"')
                    return

    def surname_for(self, first):
        """The surname from the log's "First-Surname-Realm-Region" name, if the
        first name matches the character the addon reports."""
        parts = (self.full_name or "").split("-")
        if first and len(parts) >= 4 and parts[0] == first:
            self.surnames[first] = parts[1]
        return self.surnames.get(first)

    def enemy(self, target=None):
        """The creature to show as what you're fighting: the target when it's
        in the log (Forever scrambles the GUIDs addons see, so it's matched by
        name), else the one you most recently traded blows with. `more` counts
        the other creatures still fighting you."""
        now = time.time()
        active = [f for f in self.foes.values() if not f["dead"] and now - f["fought"] <= FOE_ACTIVE]
        pick = None
        if target and target.get("name"):
            named = [f for f in self.foes.values() if f["name"] == target["name"] and now - f["t"] <= 60]
            # A dead match only counts when the target is dead too (not the previous kill).
            named = [f for f in named if not f["dead"] or target.get("dead")]
            pick = max(named, key=lambda f: f["t"], default=None)
        if pick is None and active:
            pick = max(active, key=lambda f: f["fought"])
        if pick is None:
            return None
        return {"name": pick["name"], "t": pick["t"],
                "targeted": bool(target and target.get("name") == pick["name"]),
                "more": len([f for f in active if f is not pick])}

    def note_foe(self, guid, name, t, hp=None, hp_max=None, dead=False, fought=False):
        foe = self.foes.pop(guid, None) or {"name": name, "t": t, "fought": t, "health": None,
                                            "healthMax": None, "dead": False}
        if fought:
            foe["fought"] = max(foe["fought"], t)
        if t >= foe["t"]:
            foe["t"] = t
            if hp_max:
                foe["health"], foe["healthMax"] = hp, hp_max
            foe["dead"] = dead or (foe["dead"] and hp is None)
        self.foes[guid] = foe  # re-inserted: most recently seen last
        while len(self.foes) > MAX_FOES:
            self.foes.pop(next(iter(self.foes)))

    def parse(self, line):
        stamp, sep, body = line.partition("  ")
        if not sep:
            return
        try:
            fields = next(csv.reader([body]))
        except (csv.Error, StopIteration):
            return
        if len(fields) < 4:
            return
        # Learn our GUID from a line where we're the source.
        if self.me is None and fields[1].startswith("Player-"):
            try:
                flags = int(fields[3], 16)
            except ValueError:
                flags = 0
            if flags & MINE and flags & PLAYER:
                self.me, self.full_name = fields[1], fields[2]
        if self.me is None:
            return
        # A creature that died, or one on a line where it and we are source
        # and target: something fighting us.
        foe = None
        if len(fields) > 6:
            if fields[0] == "UNIT_DIED" and fields[5] in self.foes:
                t = parse_time(stamp)
                if t is not None:
                    self.note_foe(fields[5], fields[6], t, dead=True)
                return
            if self.me in (fields[1], fields[5]):
                other, name = (fields[5], fields[6]) if fields[1] == self.me else (fields[1], fields[2])
                if other.startswith(FOE_KINDS):
                    foe = other
                    t = parse_time(stamp)
                    if t is not None:
                        self.note_foe(foe, name, t, fought=True)
        for i in range(1, len(fields) - 13):
            # Our own lines, and any line describing a creature already fighting
            # us (others' blows on it update its health too).
            unit = fields[i]
            if not (unit == self.me or unit == foe or unit in self.foes):
                continue
            if not (fields[i + 1] == NO_OWNER or GUID.match(fields[i + 1])):
                continue
            try:
                hp, hp_max = int(fields[i + 2]), int(fields[i + 3])
                ptype, power, power_max = fields[i + 10], int(fields[i + 11]), int(fields[i + 12])
            except ValueError:
                continue
            t = parse_time(stamp)
            if t is None or hp_max <= 0:
                return
            if unit != self.me:
                self.note_foe(unit, self.foes[unit]["name"], t, hp, hp_max, dead=hp == 0)
                return
            if self.vitals is None or t >= self.vitals["t"]:
                self.vitals = {
                    "t": round(t, 2), "health": hp, "healthMax": hp_max,
                    "power": {"type": POWER_TYPES.get(ptype), "cur": power, "max": power_max},
                }
            return
