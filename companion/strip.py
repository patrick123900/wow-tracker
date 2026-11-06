"""Reads and decodes the ForeverTracker pixel strip (see addon Strip.lua)."""

import json
import os
import subprocess
import sys
import time

CELL_PX = 4
NUM_BYTES = 171
NUM_CELLS = NUM_BYTES // 3
WIDTH = NUM_CELLS * CELL_PX
VERSIONS = (1, 2)  # v2 adds health/power percent and shrinks chunks to 118 bytes

FLAGS = {"sharing": 1, "dead": 2, "ghost": 4, "combat": 8, "resting": 16, "hidden": 32, "leaving": 64,
         "combatLog": 128}
VALID = {"level": 1, "xp": 2, "health": 4, "healthMax": 8, "power": 16, "powerMax": 32, "pos": 64,
         "money": 128, "map": 256, "healthPct": 512, "powerPct": 1024}
POWER_TYPES = {0: "MANA", 1: "RAGE", 2: "FOCUS", 3: "ENERGY"}


def cells_to_bytes(pixels):
    """pixels: list of (r, g, b) sampled at each cell centre."""
    out = bytearray()
    for r, g, b in pixels:
        out += bytes((r, g, b))
    return bytes(out)


def checksum_ok(data):
    a = b = 0
    for byte in data[:NUM_BYTES - 2]:
        a = (a + byte) % 255
        b = (b + a) % 255
    return data[NUM_BYTES - 2] == b and data[NUM_BYTES - 1] == a


def num(data, offset, width):
    return int.from_bytes(data[offset:offset + width], "big")


def decode(data):
    """Returns the decoded frame dict, or None if this isn't a valid strip."""
    if len(data) < NUM_BYTES or data[0:2] != b"FT" or data[2] not in VERSIONS or not checksum_ok(data):
        return None
    flags = data[4]
    valid = num(data, 5, 2)
    has = lambda k: bool(valid & VALID[k])
    v2 = data[2] >= 2
    frame = {
        "counter": data[3],
        "flags": {k: bool(flags & v) for k, v in FLAGS.items()},
        "t": num(data, 41, 4),
        "level": data[7] if has("level") else None,
        "xp": num(data, 9, 3) if has("xp") else None,
        "xpMax": num(data, 12, 3) if has("xp") else None,
        "rested": num(data, 15, 3) if has("xp") else None,
        "health": num(data, 18, 3) if has("health") else None,
        "healthMax": num(data, 21, 3) if has("healthMax") else None,
        "healthPct": data[167] if v2 and has("healthPct") else None,
        "power": {
            "id": data[8],
            "type": POWER_TYPES.get(data[8]),
            "cur": num(data, 24, 3) if has("power") else None,
            "max": num(data, 27, 3) if has("powerMax") else None,
            "pct": data[168] if v2 and has("powerPct") else None,
        },
        "money": num(data, 37, 4) if has("money") else None,
        "mapID": num(data, 30, 3) if has("map") else None,
        "x": round(num(data, 33, 2) / 65535, 4) if has("pos") else None,
        "y": round(num(data, 35, 2) / 65535, 4) if has("pos") else None,
    }
    length = data[48]
    if data[45] and 0 < length <= (118 if v2 else 120):
        frame["chunk"] = (data[45], data[46], data[47], bytes(data[49:49 + length]))
    return frame


class Reassembler:
    """Collects message chunks; returns (kind, payload) when one completes.

    Each chunk stays on screen for several frames, so chunks of a message that
    just completed keep arriving; ids completed in the last DONE_WINDOW
    seconds are ignored (ids wrap at 255, far slower than that)."""

    DONE_WINDOW = 10

    def __init__(self):
        self.parts = {}
        self.done = {}

    def add(self, chunk, now):
        msg_id, index, count, data = chunk
        if not (1 <= index <= count):
            return None
        if now - self.done.get(msg_id, -1e9) < self.DONE_WINDOW:
            return None
        entry = self.parts.get(msg_id)
        if entry is None or entry["count"] != count:
            entry = self.parts[msg_id] = {"count": count, "chunks": {}}
        entry["chunks"][index] = data
        if len(entry["chunks"]) < count:
            return None
        raw = b"".join(entry["chunks"][i] for i in range(1, count + 1))
        del self.parts[msg_id]
        self.done[msg_id] = now
        try:
            text = raw.decode("utf-8")
            return text[0], json.loads(text[1:])
        except (UnicodeDecodeError, ValueError, IndexError):
            return None


# --- capture -------------------------------------------------------------------

class X11Capture:
    """Reads the strip straight from the WoW X11 window (works under XWayland,
    and while the window is covered)."""

    def __init__(self):
        from Xlib import X, display
        self.X = X
        self.display = display.Display()
        self.window = None
        self.next_search = 0

    def find_window(self):
        root = self.display.screen().root
        stack = [root]
        while stack:
            win = stack.pop()
            try:
                name = win.get_wm_name()
                if name == "World of Warcraft":
                    geom = win.get_geometry()
                    if geom.width > WIDTH:
                        return win
                stack.extend(win.query_tree().children)
            except Exception:  # noqa: BLE001 - windows vanish mid-walk
                continue
        return None

    def grab(self):
        if self.window is None:
            if time.monotonic() < self.next_search:
                return None
            self.window = self.find_window()
            if self.window is None:
                self.next_search = time.monotonic() + 5
                return None
        try:
            img = self.window.get_image(0, 0, WIDTH, CELL_PX, self.X.ZPixmap, 0xFFFFFFFF)
        except Exception:  # noqa: BLE001 - window closed
            self.window = None
            return None
        raw = img.data
        row = CELL_PX // 2
        stride = WIDTH * 4
        pixels = []
        for i in range(NUM_CELLS):
            o = row * stride + (i * CELL_PX + CELL_PX // 2) * 4
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            pixels.append((r, g, b))
        return cells_to_bytes(pixels)


class ScreenCapture:
    """Fallback: grabs the top-left of the primary screen (game must be
    fullscreen or its window at the screen origin)."""

    def __init__(self):
        from PIL import ImageGrab
        self.grab_image = ImageGrab.grab

    def grab(self):
        img = self.grab_image(bbox=(0, 0, WIDTH, CELL_PX)).convert("RGB")
        y = CELL_PX // 2
        return cells_to_bytes([img.getpixel((i * CELL_PX + CELL_PX // 2, y)) for i in range(NUM_CELLS)])


SESSION_VARS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY")


def adopt_session_env():
    """A user service started at boot can come up before the desktop session
    has published DISPLAY. Take the session's values from systemd once they
    exist, so capture works without restarting the companion."""
    if os.environ.get("DISPLAY"):
        return
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in SESSION_VARS and value and not os.environ.get(key):
            os.environ[key] = value


def make_capture():
    if sys.platform.startswith("linux"):
        adopt_session_env()
        if not os.environ.get("DISPLAY"):
            # No desktop session yet; a screen grab can't work either.
            raise RuntimeError("no display yet (desktop session not started)")
        try:
            return X11Capture()
        except Exception as exc:  # noqa: BLE001
            print(f"X11 capture unavailable ({exc}); falling back to screen grab", flush=True)
    return ScreenCapture()
