# Forever Tracker

A progress page your friends can follow while you level in *World of Warcraft: Forever*.

```
 WoW client                   your PC                       Proxmox CT                 internet
┌───────────────┐ pixel strip ┌──────────────┐  HTTP (LAN)  ┌──────────────┐  Caddy   ┌──────────┐
│ ForeverTracker │ ──(1 s)───▶ │  companion   │ ───────────▶ │   backend    │ ───────▶ │ friends  │
│    addon       │ SavedVars   │ ft_companion │  /api/ingest │ server.py +  │  (GET    │ browser  │
└───────────────┘ ──(reload)─▶ └──────────────┘   + token     │ SQLite + UI  │   only)  └──────────┘
                                                             └──────────────┘
```

## How data gets out of the game

Blizzard's addon API has **no network or file access**. The addon reads everything through the normal addon API
(`UnitXP`, `C_Map.GetPlayerMapPosition`, `C_QuestLog.GetInfo`, events like `PLAYER_LEVEL_UP`). It has two ways out:

1. **Live pixel strip (every second).** The addon draws a 228×4 px row of coloured cells in the top-left corner of
   the screen. The companion reads those pixels from the WoW window ~20 times a second (X11/XWayland on Linux,
   screen grab elsewhere), decodes them and uploads once a second. The strip carries numbers (level, XP, health,
   power, gold, map position) in every frame, plus a chunked message channel for text: identity, zone, the quest
   log, equipped gear, professions and journal events. Format: see the header of `addon/ForeverTracker/Strip.lua`.
2. **SavedVariables (on /reload, logout, exit).** It holds the full history (trail, XP samples, journal, quest
   log) and backfills anything the live channel missed, e.g. while the companion wasn't running.

> **ToS note:** reading the screen from an outside program is against the letter of Blizzard's EULA (1.C.vi,
> "intercepts, collects, reads, or 'mines' information"). The strip only reads data and never sends input, and it
> doesn't touch the game process or its files. That keeps it away from what gets enforced in practice
> (automation, memory reading), but the risk isn't zero. Untick *Enable tracking* to remove the strip completely.

**Secret values.** Forever's client keeps current health and power (and their percentages) secret from addons, in
and out of combat. The addon never reads or encodes those. Instead, with *Share health from the combat log* on, the
addon switches on the game's own combat log (`/combatlog` with Advanced Combat Logging, the file Warcraft Logs
reads). The client writes your current health and power there on every combat action, and `companion/combatlog.py`
follows the file and keeps only lines for your own character (no data about anyone nearby leaves your PC). The page
labels these values with their age, since nothing is logged out of combat. After 30 seconds out of combat with no
newer reading (`FT_FULL_AFTER` on the backend), health and mana are shown as full, since they will have regenerated.
The surname (Forever characters have first and last names) also comes from the combat log, where your own lines name
you as `First-Surname-Realm`; the addon API only returns the first name. The log file grows while you play;
delete old `Logs/WoWCombatLog-*.txt` files now and then. `/ft debug` lists what's secret right now.

## In-game

Open the options with `/ft` (also under *Options → AddOns → Forever Tracker*, plus a keybinding).

| Setting | Effect |
|---|---|
| Enable tracking | Off: nothing is recorded, the strip disappears and the companion sends nothing. |
| Appear offline | Takes effect immediately. Friends see *Offline*. Nothing is recorded while it's on, so those moments never show up later. |
| Share health from the combat log | Turns on the game's combat log so the companion can read your health and mana from it (see above). |
| Backend address | LAN IP:port of the backend, e.g. `192.168.1.221:8787`. Only private IPv4 ranges are accepted, both in game and in the companion. |
| Sync now | Reloads the UI so the game writes the full history file. |

Slash commands: `/ft sync | on | off | hide | show | ip <addr> | status | debug`.

Recorded: name, realm, race, class, level, XP, rested XP, health, power (mana/rage/energy), gold, guild,
zone/subzone, map coordinates, and combat/resting/dead flags. It also keeps a position trail (sampled every 5 s
when you move), XP samples, and a journal of level-ups, deaths, zone changes, quest turn-ins, profession
milestones and logins. Each list is a capped ring buffer, so the file stays small.

**Gear** is read from every equipment slot (`GetInventoryItemLink`, item level, quality, enchant and random
suffix) on `PLAYER_EQUIPMENT_CHANGED` / `UNIT_INVENTORY_CHANGED`, again a moment later, when late item info
arrives, and every 20 s as a safety net. It's only sent when something changed (and re-sent every minute), ahead
of the quest log in the strip's queue, so a swap shows on the site within about 2 seconds. Icons and the hover
tooltips come from Wowhead's public item data; Forever-only items get a generic icon for their slot.

**Professions** come from the skills pane (`GetSkillLineInfo`, the *Professions* and *Secondary Skills*
sections), on `SKILL_LINES_CHANGED` / `CHAT_MSG_SKILL` and every 20 s. The journal gets an entry when you learn
one, every 5 points and at the current cap. If a section is collapsed in the skills pane its entries can't be
read; the last known list is kept until it's expanded again.

The site's look (carved frames, parchment pages, the character pane for equipment) comes from Skyttehehe's
fork, [Skyttehehe/wow-tracker](https://github.com/Skyttehehe/wow-tracker).

## Setup

### 1. Addon

Copy (or symlink) `addon/ForeverTracker` into
`World of Warcraft/_forever_/Interface/AddOns/`. Interface version `16001` (client 1.60.1).

### 2. Backend (Proxmox CT)

A Debian 12 CT with `python3` is all it needs. There are no pip dependencies.

```bash
useradd --system --home /var/lib/forever-tracker forevertracker
mkdir -p /opt/forever-tracker /var/lib/forever-tracker && chown forevertracker /var/lib/forever-tracker
# copy backend/ and frontend/ to /opt/forever-tracker/
cp deploy/forever-tracker.env.example /etc/forever-tracker.env   # set FT_TOKEN
cp deploy/forever-tracker.service /etc/systemd/system/
systemctl enable --now forever-tracker
```

Expose it with the snippet in `deploy/Caddyfile.snippet` on the Caddy CT. The backend also refuses any upload
that arrives through a proxy (`X-Forwarded-For`) or from outside `FT_INGEST_NETS`, so uploads stay LAN-only
even if the Caddy rule is missing.

### 3. Companion (gaming PC)

```bash
cp companion/config.example.json companion/config.json   # set wow_path and the same token
python3 companion/ft_companion.py --dump                  # sanity check: prints what would be sent
python3 companion/ft_companion.py                         # run it
```

On Linux, `deploy/ft-companion.service` runs it as a systemd user service. On Windows, run it with `pythonw`
from a scheduled task at logon.

### Zone maps

`python3 tools/fetch_maps.py` builds `frontend/maps/<uiMapID>.jpg` (plus `index.json`) from the game's own world map
tiles for the exact client build, via wago.tools. Rerun it with `--build <version> --force` after a patch that
changes maps. Map coordinates from the game line up 1:1 with these images.

## Development

```bash
lua tests/strip_sim.lua /tmp/frames.bin               # runs the addon on stubbed WoW APIs, dumps strip frames
FT_TOKEN=dev FT_PORT=8799 python3 backend/server.py  # http://localhost:8799
```

## Known limits

- Live updates need the strip visible to the companion: the game must be running in the world (not on a loading
  screen). Minimised windows may stop rendering, so the page falls back to "Online" with the last known values.
- Values the game makes secret stay hidden; that's by design.
- Logging out at an inn may skip `PLAYER_CAMPING`. If that happens you show as online at character select
  until the game closes.
- A crash or disconnect doesn't save SavedVariables. Live data still arrives, but the trail and journal from that
  session only come from what the live channel carried.
