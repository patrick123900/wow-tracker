-- Forever Tracker
-- Collects character data through the regular addon API. It is kept in the
-- ForeverTrackerDB SavedVariable (written on /reload, logout and exit) and
-- also drawn as a pixel strip in the top-left corner (see Strip.lua) that the
-- companion app reads for live updates.
--
-- Values the client marks as secret (issecretvalue) are never read, stored or
-- encoded; they are reported as unavailable instead.

local ADDON = ...
local FT = {}
_G.ForeverTracker = FT

local SCHEMA = 2
local MAX_POSITIONS = 400
local MAX_XP = 600
local MAX_EVENTS = 200
local TICK = 1               -- seconds between snapshot refreshes
local POSITION_EVERY = 5     -- seconds between trail samples
local XP_THROTTLE = 30       -- min seconds between XP samples within a level

local DEFAULTS = {
	enabled = true,
	appearOffline = false,
	endpoint = "192.168.1.221:8787",
	combatLog = true,
}

local db
local leaving = false
local lastZone
local lastXPSample = 0
local lastPositionSample = 0

BINDING_HEADER_FOREVERTRACKER = "Forever Tracker"
BINDING_NAME_FOREVERTRACKER_SYNC = "Sync now (reloads UI)"
BINDING_NAME_FOREVERTRACKER_OPTIONS = "Toggle options"

local function now()
	return GetServerTime and GetServerTime() or time()
end
FT.Now = now

local function round(v, places)
	local m = 10 ^ (places or 0)
	return math.floor(v * m + 0.5) / m
end

local function push(list, item, cap)
	list[#list + 1] = item
	while #list > cap do
		table.remove(list, 1)
	end
end

-- Returns v, or nil when the client has made it a secret value.
local isSecret = issecretvalue or function() return false end
FT.secretSeen = {}
function FT.Safe(v, name)
	if isSecret(v) then
		if name then FT.secretSeen[name] = true end
		return nil
	end
	return v
end
local safe = FT.Safe

-- Calls fn and returns its first result only if it's readable.
local function read(name, fn, ...)
	if not fn then return nil end
	local ok, v = pcall(fn, ...)
	if not ok then return nil end
	return safe(v, name)
end
FT.Read = read

function FT.Print(msg)
	DEFAULT_CHAT_FRAME:AddMessage("|cff66c2ffForever Tracker|r: " .. msg)
end

-- Tracking is active only when enabled and not hidden. While hidden nothing
-- is recorded at all, so it can't leak out later either.
function FT.IsRecording()
	return db and db.settings.enabled and not db.settings.appearOffline
end

-- Accepts "a.b.c.d" or "a.b.c.d:port" on a private/loopback IPv4 range.
-- Returns the normalised "ip:port" or nil.
function FT.NormaliseEndpoint(text)
	if type(text) ~= "string" then return nil end
	text = text:gsub("^%s+", ""):gsub("%s+$", ""):gsub("^https?://", ""):gsub("/+$", "")
	local a, b, c, d, port = text:match("^(%d+)%.(%d+)%.(%d+)%.(%d+):?(%d*)$")
	if not a then return nil end
	a, b, c, d = tonumber(a), tonumber(b), tonumber(c), tonumber(d)
	for _, n in ipairs({ a, b, c, d }) do
		if n > 255 then return nil end
	end
	local private = a == 10 or a == 127
		or (a == 172 and b >= 16 and b <= 31)
		or (a == 192 and b == 168)
	if not private then return nil end
	port = tonumber(port ~= "" and port or "8787")
	if port < 1 or port > 65535 then return nil end
	return string.format("%d.%d.%d.%d:%d", a, b, c, d, port)
end

function FT.PlayerPosition()
	if not (C_Map and C_Map.GetBestMapForUnit) then return nil end
	local mapID = read("mapID", C_Map.GetBestMapForUnit, "player")
	if not mapID then return nil end
	local info = C_Map.GetMapInfo and C_Map.GetMapInfo(mapID)
	local mapName = info and safe(info.name, "map")
	local ok, pos = pcall(C_Map.GetPlayerMapPosition, mapID, "player")
	pos = ok and safe(pos, "position")
	if not pos then
		-- Instances return no coordinates.
		return { mapID = mapID, map = mapName }
	end
	local x, y = pos:GetXY()
	x, y = safe(x, "position"), safe(y, "position")
	if not (x and y) then return { mapID = mapID, map = mapName } end
	return { mapID = mapID, map = mapName, x = round(x, 4), y = round(y, 4) }
end

-- Blizzard's own 0-100 percentage APIs. They're sometimes readable when the
-- raw values are secret; when they're secret too, we leave them out.
local scaleTo100 = CurveConstants and CurveConstants.ScaleTo100
local function percent(name, fn, ...)
	if not (fn and scaleTo100) then return nil end
	local v = read(name, fn, ...)
	if type(v) ~= "number" then return nil end
	return math.max(0, math.min(100, round(v)))
end

local function powerInfo()
	local ok, id, token = pcall(UnitPowerType, "player")
	id, token = ok and safe(id, "powerType"), ok and safe(token, "powerType")
	if not id then return nil end
	return {
		id = id,
		type = token,
		cur = read("power", UnitPower, "player", id),
		max = read("powerMax", UnitPowerMax, "player", id),
		pct = percent("powerPct", UnitPowerPercent, "player", id, false, scaleTo100),
	}
end

local function flag(name, fn, ...)
	local v = read(name, fn, ...)
	if v == nil then return nil end
	return v and true or false
end

function FT.BuildSnapshot()
	local _, classToken = UnitClass("player")
	local pos = FT.PlayerPosition() or {}
	return {
		t = now(),
		status = leaving and "offline" or "online",
		name = read("name", UnitName, "player"),
		realm = read("realm", GetRealmName),
		faction = read("faction", UnitFactionGroup, "player"),
		race = read("race", UnitRace, "player"),
		class = read("class", UnitClass, "player"),
		classToken = safe(classToken, "class"),
		sex = read("sex", UnitSex, "player"),
		level = read("level", UnitLevel, "player"),
		maxLevel = read("maxLevel", GetMaxPlayerLevel) or 60,
		xp = read("xp", UnitXP, "player"),
		xpMax = read("xpMax", UnitXPMax, "player"),
		rested = read("rested", GetXPExhaustion) or 0,
		health = read("health", UnitHealth, "player"),
		healthMax = read("healthMax", UnitHealthMax, "player"),
		healthPct = percent("healthPct", UnitHealthPercent, "player", false, scaleTo100),
		power = powerInfo(),
		money = read("money", GetMoney),
		guild = read("guild", GetGuildInfo, "player"),
		dead = flag("dead", UnitIsDeadOrGhost, "player"),
		ghost = flag("ghost", UnitIsGhost, "player"),
		resting = flag("resting", IsResting),
		combat = flag("combat", UnitAffectingCombat, "player"),
		zone = read("zone", GetRealZoneText),
		subzone = read("subzone", GetSubZoneText),
		mapID = pos.mapID,
		map = pos.map,
		x = pos.x,
		y = pos.y,
	}
end

-- A snapshot taken while the character is still loading (or already gone,
-- as during PLAYER_LOGOUT) has zeroed values; never keep one of those.
local function plausible(s)
	return s.name and s.level and s.level > 0
		and (s.xpMax == nil or s.xpMax > 0 or s.level >= s.maxLevel)
end

local eventListeners = {}
function FT.OnEvent(fn) eventListeners[#eventListeners + 1] = fn end

function FT.AddEvent(kind, data)
	if not FT.IsRecording() then return end
	data = data or {}
	data.t = now()
	data.type = kind
	push(db.events, data, MAX_EVENTS)
	for _, fn in ipairs(eventListeners) do fn(data) end
end
local addEvent = FT.AddEvent

local function sampleXP(force)
	if not FT.IsRecording() then return end
	local t = now()
	if not force and t - lastXPSample < XP_THROTTLE then return end
	local level = read("level", UnitLevel, "player")
	local xp, xpMax = read("xp", UnitXP, "player"), read("xpMax", UnitXPMax, "player")
	if not (level and xp and xpMax and xpMax > 0) then return end
	lastXPSample = t
	push(db.xp, { t = t, level = level, xp = xp, xpMax = xpMax }, MAX_XP)
end

local function samplePosition()
	if not FT.IsRecording() then return end
	local pos = FT.PlayerPosition()
	if not (pos and pos.x) then return end
	local last = db.positions[#db.positions]
	if last and last.mapID == pos.mapID
		and math.abs(last.x - pos.x) < 0.002 and math.abs(last.y - pos.y) < 0.002 then
		return
	end
	pos.t = now()
	push(db.positions, pos, MAX_POSITIONS)
end

local function initDB()
	ForeverTrackerDB = ForeverTrackerDB or {}
	db = ForeverTrackerDB
	db.schema = SCHEMA
	db.settings = db.settings or {}
	for k, v in pairs(DEFAULTS) do
		if db.settings[k] == nil then db.settings[k] = v end
	end
	db.events = db.events or {}
	db.positions = db.positions or {}
	db.xp = db.xp or {}
	FT.db = db
end

-- Other files hook into each refresh with FT.OnRefresh(fn(recording)).
local refreshers = {}
function FT.OnRefresh(fn) refreshers[#refreshers + 1] = fn end

-- Refreshes the cached snapshot. Runs every TICK seconds, so the file holds
-- the last good state whenever the client decides to write it.
local function refresh()
	if not db then return end
	local recording = FT.IsRecording()
	for _, fn in ipairs(refreshers) do fn(recording) end
	if not recording then
		db.snapshot = nil
		db.quests = nil
		FT.current = nil
		return
	end
	local snap = FT.BuildSnapshot()
	if plausible(snap) then
		FT.current = snap
		db.snapshot = snap
	end
	if FT.ReadQuestLog and FT.questsDirty then
		FT.questsDirty = false
		db.quests = FT.ReadQuestLog()
		FT.questsVersion = (FT.questsVersion or 0) + 1
	end
	local t = now()
	if t - lastPositionSample >= POSITION_EVERY then
		lastPositionSample = t
		samplePosition()
	end
end
FT.Refresh = refresh

-- Reloading is the only way to make the client write SavedVariables mid-
-- session. It also pushes setting changes (e.g. appear offline) right away.
function ForeverTracker_Sync()
	ReloadUI()
end

local frame = CreateFrame("Frame")
local handlers = {}
FT.handlers = handlers

-- Other files add their handlers and init functions while loading; they're
-- wired up once our ADDON_LOADED arrives.
FT.onLoaded = {}
function FT.OnLoaded(fn) FT.onLoaded[#FT.onLoaded + 1] = fn end

function handlers.ADDON_LOADED(name)
	if name ~= ADDON then return end
	initDB()
	for event in pairs(handlers) do
		-- Not every event exists on every client flavour.
		if event ~= "ADDON_LOADED" then pcall(frame.RegisterEvent, frame, event) end
	end
	frame:UnregisterEvent("ADDON_LOADED")
	for _, fn in ipairs(FT.onLoaded) do fn() end
end

function handlers.PLAYER_ENTERING_WORLD(isInitialLogin, isReload)
	leaving = false
	FT.questsDirty = true
	if FT.MarkCharacterDirty then FT.MarkCharacterDirty() end
	-- Zone text isn't ready on the first frame of a login.
	C_Timer.After(2, function()
		lastZone = lastZone or read("zone", GetRealZoneText)
		if isInitialLogin then
			addEvent("login", { zone = read("zone", GetRealZoneText), level = read("level", UnitLevel, "player") })
		end
		sampleXP(true)
		refresh()
	end)
end

function handlers.PLAYER_LEVEL_UP(level)
	addEvent("level", { level = safe(level), zone = read("zone", GetRealZoneText) })
	-- Record the new level once UnitXPMax has updated.
	C_Timer.After(1, function() sampleXP(true) end)
end

function handlers.PLAYER_XP_UPDATE()
	sampleXP(false)
end

function handlers.PLAYER_DEAD()
	local pos = FT.PlayerPosition() or {}
	addEvent("death", {
		level = read("level", UnitLevel, "player"),
		zone = read("zone", GetRealZoneText),
		subzone = read("subzone", GetSubZoneText),
		x = pos.x, y = pos.y, mapID = pos.mapID,
	})
end

function handlers.ZONE_CHANGED_NEW_AREA()
	local zone = read("zone", GetRealZoneText)
	if zone and zone ~= "" and zone ~= lastZone then
		lastZone = zone
		addEvent("zone", { zone = zone, level = read("level", UnitLevel, "player") })
	end
end

-- Logging out or quitting fires these first; a plain /reload does not, which
-- is how the snapshot tells "went offline" from "synced".
function handlers.PLAYER_CAMPING() leaving = true end
function handlers.PLAYER_QUITING() leaving = true end
function handlers.LOGOUT_CANCEL() leaving = false end
function handlers.QUIT_CANCEL() leaving = false end

function handlers.PLAYER_LOGOUT()
	-- Unit data is already gone here, so keep the last cached snapshot and
	-- only update its status.
	if db.snapshot then
		db.snapshot.status = leaving and "offline" or "online"
	end
	db.savedAt = now()
end

frame:SetScript("OnEvent", function(_, event, ...)
	handlers[event](...)
end)
frame:RegisterEvent("ADDON_LOADED")

C_Timer.NewTicker(TICK, refresh)

SLASH_FOREVERTRACKER1 = "/ft"
SLASH_FOREVERTRACKER2 = "/forevertracker"
SlashCmdList.FOREVERTRACKER = function(msg)
	local cmd, rest = (msg or ""):match("^%s*(%S*)%s*(.-)%s*$")
	cmd = cmd:lower()
	if cmd == "sync" then
		ForeverTracker_Sync()
	elseif cmd == "on" then
		FT.SetSetting("enabled", true)
	elseif cmd == "off" then
		FT.SetSetting("enabled", false)
	elseif cmd == "hide" or cmd == "offline" then
		FT.SetSetting("appearOffline", true)
	elseif cmd == "show" or cmd == "online" then
		FT.SetSetting("appearOffline", false)
	elseif cmd == "ip" or cmd == "endpoint" then
		if not FT.SetEndpoint(rest) then
			FT.Print("use a LAN address, e.g. /ft ip 192.168.1.221:8787")
		end
	elseif cmd == "status" then
		FT.Print(FT.StatusText())
	elseif cmd == "debug" then
		FT.Debug()
	elseif cmd == "" or cmd == "config" or cmd == "options" then
		ForeverTracker_ToggleOptions()
	else
		FT.Print("/ft [options|sync|on|off|hide|show|ip <addr>|status|debug]")
	end
end

-- Lists which values the client currently hides from addons.
function FT.Debug()
	wipe(FT.secretSeen)
	local s = FT.BuildSnapshot()
	local hidden = {}
	for name in pairs(FT.secretSeen) do hidden[#hidden + 1] = name end
	table.sort(hidden)
	FT.Print(string.format("combat log: %s, advanced: %s", tostring(LoggingCombat and LoggingCombat()),
		tostring(GetCVar and GetCVar("advancedCombatLogging"))))
	local p = s.power or {}
	FT.Print(string.format("level %s, xp %s/%s, hp %s/%s (%s%%), power %s/%s (%s%%), map %s (%s, %s), combat %s",
		tostring(s.level), tostring(s.xp), tostring(s.xpMax), tostring(s.health), tostring(s.healthMax),
		tostring(s.healthPct), tostring(p.cur), tostring(p.max), tostring(p.pct),
		tostring(s.mapID), tostring(s.x), tostring(s.y), tostring(s.combat)))
	FT.Print("secret right now: " .. (#hidden > 0 and table.concat(hidden, ", ") or "nothing"))
	if FT.ReadQuestLog then
		FT.Print(string.format("%d quests in log", #FT.ReadQuestLog()))
	end
	if FT.CharacterDebug then FT.CharacterDebug() end
end

function FT.StatusText()
	local s = db.settings
	local state
	if not s.enabled then
		state = "|cffff6060paused|r"
	elseif s.appearOffline then
		state = "|cffffb347appearing offline|r"
	else
		state = "|cff60ff60sharing live|r"
	end
	return string.format("%s, backend %s", state, s.endpoint)
end

local listeners = {}
function FT.OnSettingsChanged(fn) listeners[#listeners + 1] = fn end

local function notify()
	for _, fn in ipairs(listeners) do fn() end
end

function FT.SetSetting(key, value)
	db.settings[key] = value
	FT.Print(FT.StatusText())
	refresh()
	notify()
end

function FT.SetEndpoint(text)
	local ep = FT.NormaliseEndpoint(text)
	if not ep then return false end
	db.settings.endpoint = ep
	FT.Print("backend set to " .. ep .. " (the companion picks this up on the next sync)")
	notify()
	return true
end

-- Turns on the game's own combat log file (Logs/WoWCombatLog*.txt) with
-- advanced logging while tracking. The client writes your current health and
-- power into that file on each combat action; the companion reads it there,
-- since addons can't read those values themselves.
--
-- Logging is only switched off again if this addon switched it on, so it
-- doesn't interfere with logging you started yourself (e.g. for Warcraft Logs).


local startedByUs = false

local function wanted()
	return FT.IsRecording() and FT.db.settings.combatLog
end

local function applyCombatLog()
	if not LoggingCombat then return end
	if wanted() then
		if GetCVar("advancedCombatLogging") ~= "1" then
			SetCVar("advancedCombatLogging", "1")
		end
		if not LoggingCombat() then
			LoggingCombat(true)
			startedByUs = true
		end
	elseif startedByUs and LoggingCombat() then
		LoggingCombat(false)
		startedByUs = false
	end
end

-- Logging resets on logout; turn it back on after every loading screen.
local origEnteringWorld = handlers.PLAYER_ENTERING_WORLD
handlers.PLAYER_ENTERING_WORLD = function(...)
	origEnteringWorld(...)
	C_Timer.After(3, applyCombatLog)
end
FT.OnSettingsChanged(applyCombatLog)
