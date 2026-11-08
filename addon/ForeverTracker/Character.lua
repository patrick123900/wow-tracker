-- Equipped gear and professions, for the website's equipment and profession
-- cards. Both are re-read on their game events and every RECHECK seconds as a
-- safety net, and only sent on when something actually changed. Also the
-- current target, for "what you're fighting".

local FT = ForeverTracker
local safe, read = FT.Safe, FT.Read
local handlers = FT.handlers

local RECHECK = 20 -- seconds

-- Inventory slot ids, in character-pane order. 4 = shirt, 19 = tabard.
local SLOTS = { 1, 2, 3, 15, 5, 4, 19, 9, 10, 6, 7, 8, 11, 12, 13, 14, 16, 17, 18 }

local gearDirty, profDirty = true, true
local lastCheck = 0
FT.gearVersion, FT.professionsVersion = 0, 0
local gearKey, profKey

function FT.MarkCharacterDirty()
	gearDirty, profDirty = true, true
end

-- --- gear ----------------------------------------------------------------------

local function itemLevel(link)
	for _, fn in ipairs({ C_Item and C_Item.GetDetailedItemLevelInfo, GetDetailedItemLevelInfo }) do
		local v = read(nil, fn, link)
		if type(v) == "number" and v > 0 then return v end
	end
	local ok, _, _, _, level = pcall(GetItemInfo, link)
	if ok and type(safe(level)) == "number" and level > 0 then return level end
end

-- "item:id:enchant:gem1:gem2:gem3:gem4:suffix:..." (fields may be empty).
local function linkFields(link)
	local body = link:match("item:([%-%d:]*)")
	local f, i = {}, 0
	if body then
		for v in (body .. ":"):gmatch("([^:]*):") do
			i = i + 1
			f[i] = tonumber(v) -- empty fields stay nil but keep their position
		end
	end
	return f
end

-- Returns the item list and whether some item's details weren't cached yet.
local function readGear()
	local items, missing = {}, false
	for _, slot in ipairs(SLOTS) do
		local link = read(nil, GetInventoryItemLink, "player", slot)
		if type(link) == "string" then
			local f = linkFields(link)
			local ilvl = itemLevel(link)
			if not ilvl then missing = true end
			local enchant, suffix = f[2], f[7]
			items[#items + 1] = {
				slot = slot,
				id = read(nil, GetInventoryItemID, "player", slot) or f[1],
				name = link:match("%[(.-)%]"),
				quality = read(nil, GetInventoryItemQuality, "player", slot),
				ilvl = ilvl,
				enchant = enchant ~= 0 and enchant or nil,
				suffix = suffix ~= 0 and suffix or nil,
			}
		end
	end
	return items, missing
end

-- --- professions ---------------------------------------------------------------

local PRIMARY_HEADERS = { [TRADE_SKILLS or "Professions"] = true, Professions = true }
local SECONDARY_HEADERS = { [(SECONDARY_SKILLS or "Secondary Skills"):gsub(":$", "")] = true, ["Secondary Skills"] = true }
-- Recognised by name too, in case a client names the sections differently.
local KNOWN = {
	Alchemy = "primary", Blacksmithing = "primary", Enchanting = "primary", Engineering = "primary",
	Herbalism = "primary", Leatherworking = "primary", Mining = "primary", Skinning = "primary",
	Tailoring = "primary", Jewelcrafting = "primary", Inscription = "primary",
	Cooking = "secondary", ["First Aid"] = "secondary", Fishing = "secondary",
}

local function entry(name, rank, max, bonus, kind)
	bonus = safe(bonus)
	return { name = name, rank = safe(rank), max = safe(max), bonus = bonus and bonus ~= 0 and bonus or nil,
		secondary = kind == "secondary" or nil }
end

-- Modern clients: GetProfessions() gives indices for GetProfessionInfo.
local function fromProfessionAPI()
	if not (GetProfessions and GetProfessionInfo) then return nil end
	local ok, p1, p2, arch, fish, cook, aid = pcall(GetProfessions)
	if not ok then return nil end
	local list = {}
	for i, index in pairs({ p1, p2, arch, fish, cook, aid }) do
		index = safe(index)
		if index then
			local ok2, name, _, rank, max, _, _, _, modifier = pcall(GetProfessionInfo, index)
			name = ok2 and safe(name)
			if name then list[#list + 1] = entry(name, rank, max, modifier, i <= 2 and "primary" or "secondary") end
		end
	end
	return list
end

-- Classic clients: the skills pane. Returns nil when it can't be read
-- completely right now (a collapsed section hides its entries).
local function fromSkillLines()
	if not (GetNumSkillLines and GetSkillLineInfo) then return nil end
	local n = read(nil, GetNumSkillLines)
	if not n or n == 0 then return nil end
	local list, kind = {}, nil
	for i = 1, n do
		local ok, name, isHeader, isExpanded, rank, _, modifier, maxRank = pcall(GetSkillLineInfo, i)
		name = ok and safe(name)
		if name then
			if isHeader then
				kind = PRIMARY_HEADERS[name] and "primary" or SECONDARY_HEADERS[name] and "secondary" or nil
				if kind and not isExpanded then return nil end
			elseif kind or KNOWN[name] then
				list[#list + 1] = entry(name, rank, maxRank, modifier, kind or KNOWN[name])
			end
		end
	end
	return list
end

local function readProfessions()
	local list = fromProfessionAPI()
	if list and #list > 0 then return list end
	return fromSkillLines() or list
end

function FT.CharacterDebug()
	local n = GetNumSkillLines and read(nil, GetNumSkillLines)
	local headers = {}
	for i = 1, n or 0 do
		local ok, name, isHeader, isExpanded = pcall(GetSkillLineInfo, i)
		if ok and isHeader then headers[#headers + 1] = tostring(safe(name)) .. (isExpanded and "" or " (collapsed)") end
	end
	local list = readProfessions()
	FT.Print(string.format("skills: GetProfessions %s, %s skill lines, sections: %s",
		GetProfessions and "yes" or "no", tostring(n), table.concat(headers, ", ")))
	FT.Print(string.format("professions read: %s; gear: %d items", list and #list or "unreadable",
		#(FT.char and FT.char.gear or {})))
end

-- Journal entries for professions: when one is learned, every 5 points and
-- at the current cap (an entry per point would flood the journal).
local function recordSkillUps(old, new)
	local before = {}
	for _, p in ipairs(old or {}) do before[p.name] = p.rank end
	for _, p in ipairs(new) do
		local was = before[p.name]
		if was and p.rank and p.rank > was then
			local milestone = math.floor(p.rank / 5) > math.floor(was / 5) or p.rank == p.max
			if milestone then FT.AddEvent("skill", { skill = p.name, rank = p.rank, max = p.max }) end
		elseif old and not was then
			FT.AddEvent("skill", { skill = p.name, rank = p.rank, max = p.max, learned = true })
		end
	end
end

-- --- target --------------------------------------------------------------------

local RANKS = { elite = true, rare = true, rareelite = true, worldboss = true }

-- The current target when it's a creature you can fight, else nil: the
-- website shows what you're fighting (Forever keeps its health secret).
function FT.ReadTarget()
	if not read(nil, UnitExists, "target") or not read(nil, UnitCanAttack, "player", "target") then return nil end
	if read(nil, UnitIsPlayer, "target") then return nil end -- other players stay private
	local name = read(nil, UnitName, "target")
	if not name then return nil end
	local rank = read(nil, UnitClassification, "target")
	return {
		name = name,
		level = read(nil, UnitLevel, "target"), -- -1 for "??"
		rank = RANKS[rank] and rank or nil,
		dead = read(nil, UnitIsDead, "target") and true or nil,
	}
end

-- --- refresh -------------------------------------------------------------------

local function key(list)
	local parts = {}
	for _, it in ipairs(list) do
		for _, k in ipairs({ "slot", "id", "name", "quality", "ilvl", "enchant", "suffix", "rank", "bonus", "max" }) do
			parts[#parts + 1] = tostring(it[k])
		end
	end
	return table.concat(parts, "|")
end

FT.OnRefresh(function(recording)
	local db = FT.char
	if not recording then
		db.gear, db.professions = nil, nil
		gearKey, profKey = nil, nil
		return
	end
	local t = GetTime()
	if t - lastCheck >= RECHECK then
		lastCheck = t
		gearDirty, profDirty = true, true
	end
	if gearDirty then
		gearDirty = false
		local items, missing = readGear()
		FT.gearIncomplete = missing
		local k = key(items)
		if k ~= gearKey then
			gearKey, db.gear = k, items
			FT.gearVersion = FT.gearVersion + 1
		end
	end
	if profDirty then
		profDirty = false
		local list = readProfessions()
		local k = list and key(list)
		if list and k ~= profKey then
			if profKey then recordSkillUps(db.professions, list) end
			profKey, db.professions = k, list
			FT.professionsVersion = FT.professionsVersion + 1
		end
	end
end)

-- Equipment changes land a frame later in the inventory API; re-read then
-- and once more shortly after, so a swap is never missed.
local function gearChanged()
	gearDirty = true
	C_Timer.After(0.2, function() gearDirty = true; FT.Refresh() end)
end

function handlers.PLAYER_EQUIPMENT_CHANGED() gearChanged() end

function handlers.UNIT_INVENTORY_CHANGED(unit)
	if safe(unit) == "player" then gearChanged() end
end

-- Item levels of uncached items arrive later.
function handlers.GET_ITEM_INFO_RECEIVED()
	if FT.gearIncomplete then gearDirty = true end
end

function handlers.SKILL_LINES_CHANGED() profDirty = true end
function handlers.CHAT_MSG_SKILL() profDirty = true end
