-- Loads the addon against stubbed WoW APIs, simulates a few seconds of play
-- and writes every pixel-strip frame (171 bytes each) to a binary file.
-- Usage: lua tests/strip_sim.lua <frames.bin> [saved-variables.lua [name [realm [guid [race class level]]]]]
-- With a SavedVariables file, starts from that saved data (as the game would)
-- and prints how it's stored per character afterwards.

local out, savedFile = arg[1], arg[2]
local simName, simRealm = arg[3] or "Pattes", arg[4] or "Test Realm"
local simGUID = arg[5] or "Player-1-00000001"
local simRace, simClass = arg[6] or "Dwarf", arg[7] or "SHAMAN"
local simLevel = tonumber(arg[8]) or 3
if savedFile then dofile(savedFile) end -- defines ForeverTrackerDB
local clock, gtime = 1793890000, 0
local SECRET = setmetatable({}, { __tostring = function() return "<secret>" end })

local S = { level = simLevel, xp = 120, xpMax = 1100, x = 0.2971, y = 0.7166, zone = "Dun Morogh",
	subzone = "Coldridge Valley", mapID = 1426, hp = SECRET, hpMax = 92, mana = 70, manaMax = 110 }

function issecretvalue(v) return v == SECRET end
function GetServerTime() return clock end
function GetTime() return gtime end
time = function() return clock end
function wipe(t) for k in pairs(t) do t[k] = nil end return t end
function hooksecurefunc() end
function GetPhysicalScreenSize() return 2560, 1440 end

local function texture()
	local t = {}
	setmetatable(t, { __index = function() return function() end end })
	return t
end
local frames = {}
function CreateFrame()
	local f = { scripts = {} }
	f.RegisterEvent = function() end
	f.UnregisterEvent = function() end
	f.SetScript = function(self, name, fn) self.scripts[name] = fn end
	f.CreateTexture = function() return texture() end
	setmetatable(f, { __index = function() return function() end end })
	frames[#frames + 1] = f
	return f
end
local tickers = {}
C_Timer = { NewTicker = function(_, fn) tickers[#tickers + 1] = fn end, After = function(_, fn) fn() end }
C_Map = {
	GetBestMapForUnit = function() return S.mapID end,
	GetPlayerMapPosition = function() return { GetXY = function() return S.x, S.y end } end,
	GetMapInfo = function() return { name = S.zone } end,
}
local quests = {
	{ title = "Dun Morogh", isHeader = true },
	{ title = "Dwarven Outfitters", questID = 179, level = 1 },
	{ title = "Coldridge Valley Mail Delivery", questID = 233, level = 2 },
	{ title = "A \"Tough\" Wolf Meat — Ünïcode", questID = 3361, level = 3 },
}
C_QuestLog = {
	GetNumQuestLogEntries = function() return #quests end,
	GetInfo = function(i) return quests[i] end,
	IsComplete = function(id) return id == 233 end,
	IsFailed = function() return false end,
	GetQuestObjectives = function(id)
		if id == 179 then return { { text = "Tough Wolf Meat: 6/8", finished = false, numFulfilled = 6, numRequired = 8 } } end
		if id == 3361 then return { { text = "Wolf slain", finished = true, numFulfilled = 1, numRequired = 1 } } end
		return {}
	end,
	GetTitleForQuestID = function(id) return "Quest " .. id end,
}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, m) print("[chat] " .. m) end }
SlashCmdList = {}
UISpecialFrames = {}
function UnitClass() return simClass:sub(1, 1) .. simClass:sub(2):lower(), simClass end
function UnitRace() return simRace end
function UnitGUID() return simGUID end
function UnitLevel() return S.level end
function GetMaxPlayerLevel() return 60 end
function GetGuildInfo() return nil end
function UnitName() return simName end
function GetRealmName() return simRealm end
function UnitFactionGroup() return "Alliance" end
function UnitSex() return 2 end
function UnitXP() return S.xp end
function UnitXPMax() return S.xpMax end
function GetXPExhaustion() return 300 end
function UnitHealth() return S.hp end
function UnitHealthMax() return S.hpMax end
CurveConstants = { ScaleTo100 = {} }
function UnitHealthPercent() return 73.4 end
function UnitPowerPercent() return SECRET end
function UnitPowerType() return 0, "MANA" end
function UnitPower() return S.mana end
function UnitPowerMax() return S.manaMax end
function GetMoney() return 1234 end
function UnitIsDeadOrGhost() return false end
function UnitIsGhost() return false end
function IsResting() return false end
function UnitAffectingCombat() return true end
function GetRealZoneText() return S.zone end
function GetSubZoneText() return S.subzone end

-- Equipped items by slot: { id, name, quality, ilvl, enchant, suffix }.
-- Slot 16's item level isn't cached until GET_ITEM_INFO_RECEIVED.
local gear = {
	[1] = { 10019, "Dreadmist Mask", 3, 30 }, [5] = { 2105, "Thug Shirt — Ünïcode", 1, 5, 41 },
	[7] = { 6585, "Ridge Cleaver of the Bear", 2, 22, nil, 1182 }, [16] = { 2361, "Battleworn Hammer", 1, nil },
}
local function link(slot)
	local g = gear[slot]
	return g and ("|cff1eff00|Hitem:" .. g[1] .. ":" .. (g[5] or "") .. ":::::" .. (g[6] or "") .. ":::3:::::|h[" .. g[2] .. "]|h|r")
end
function GetInventoryItemLink(_, slot) return link(slot) end
function GetInventoryItemID(_, slot) return gear[slot] and gear[slot][1] end
function GetInventoryItemQuality(_, slot) return gear[slot] and gear[slot][3] end
C_Item = { GetDetailedItemLevelInfo = function(l)
	for _, g in pairs(gear) do if l:find("item:" .. g[1] .. ":", 1, true) then return g[4] end end
end }
function GetItemInfo() return nil end

-- Skills pane: headers and lines { name, isHeader, isExpanded, rank, temp, modifier, max }.
local skills = {
	{ "Class Skills", true, true }, { "Holy", false, false, 15, 0, 0, 15 },
	{ "Professions", true, true }, { "Alchemy", false, false, 48, 0, 0, 75 }, { "Herbalism", false, false, 61, 0, 0, 75 },
	{ "Secondary Skills", true, true }, { "Cooking", false, false, 1, 0, 0, 75 },
}
function GetNumSkillLines() return #skills end
function GetSkillLineInfo(i) return unpack(skills[i]) end
unpack = unpack or table.unpack

local function load(file) assert(loadfile("addon/ForeverTracker/" .. file))("ForeverTracker") end
load("Core.lua")
load("Quests.lua")
load("Character.lua")
load("Strip.lua")
local FT = ForeverTracker
local eventFrame = frames[1]
local function fire(e, ...) eventFrame.scripts.OnEvent(eventFrame, e, ...) end

fire("ADDON_LOADED", "ForeverTracker")
fire("PLAYER_ENTERING_WORLD", true, false)
for _, t in ipairs(tickers) do t() end

local f = assert(io.open(out, "wb"))
for step = 1, 450 do -- 45 seconds at 10 Hz
	gtime = gtime + 0.1
	if step % 10 == 0 then
		clock = clock + 1
		S.x = S.x + 0.001
		for _, t in ipairs(tickers) do t() end
	end
	if step == 50 then fire("QUEST_TURNED_IN", 233, 450, 120) end
	if step == 80 then gear[16][4] = 3; fire("GET_ITEM_INFO_RECEIVED", 2361, true) end
	if step == 120 then gear[8] = { 2309, "Handstitched Leather Boots", 1, 8 }; fire("PLAYER_EQUIPMENT_CHANGED", 8, false) end
	if step == 150 then skills[4][4] = 50; fire("SKILL_LINES_CHANGED") end
	local bytes = FT.StripEncode()
	for i = 0, 170 do f:write(string.char(bytes[i])) end
end
f:close()
print("frames written")
if savedFile then
	local db = ForeverTrackerDB
	print("current: " .. tostring(db.current))
	for _, k in ipairs({ "events", "positions", "xp", "snapshot", "quests", "gear", "professions" }) do
		if db[k] ~= nil then print("left at top level: " .. k) end
	end
	local keys = {}
	for key in pairs(db.chars) do keys[#keys + 1] = key end
	table.sort(keys)
	for _, key in ipairs(keys) do
		local c = db.chars[key]
		print(string.format("%s: %d events, %d positions, %d xp, snapshot %s (level %s), %d quests, %d gear",
			key, #c.events, #c.positions, #c.xp, c.snapshot and c.snapshot.name or "-",
			c.snapshot and tostring(c.snapshot.level) or "-", #(c.quests or {}), #(c.gear or {})))
	end
end
