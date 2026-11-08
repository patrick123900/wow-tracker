-- Quest log: reads the player's current quests and their objective progress,
-- and records accepted / abandoned / turned-in quests in the journal.

local FT = ForeverTracker
local safe, read = FT.Safe, FT.Read
local handlers = FT.handlers

local function objectivesModern(questID)
	local list = {}
	local ok, objectives = pcall(C_QuestLog.GetQuestObjectives, questID)
	if not ok or not safe(objectives) then return list end
	for _, o in ipairs(objectives) do
		local text = safe(o.text)
		if text and text ~= "" then
			list[#list + 1] = {
				text = text,
				done = safe(o.finished) and true or false,
				have = safe(o.numFulfilled),
				need = safe(o.numRequired),
			}
		end
	end
	return list
end

local function readModern()
	local quests, header = {}, nil
	local n = read(nil, C_QuestLog.GetNumQuestLogEntries) or 0
	for i = 1, n do
		local ok, info = pcall(C_QuestLog.GetInfo, i)
		info = ok and safe(info)
		if info then
			local title = safe(info.title)
			if safe(info.isHeader) then
				header = title
			elseif title and not safe(info.isHidden) then
				local id = safe(info.questID)
				local complete = id and read(nil, C_QuestLog.IsComplete, id)
				local failed = id and C_QuestLog.IsFailed and read(nil, C_QuestLog.IsFailed, id)
				quests[#quests + 1] = {
					id = id,
					title = title,
					level = safe(info.level),
					header = header,
					complete = complete and true or false,
					failed = failed and true or false,
					objectives = id and objectivesModern(id) or {},
				}
			end
		end
	end
	return quests
end

local function readLegacy()
	local quests, header = {}, nil
	local n = GetNumQuestLogEntries() or 0
	for i = 1, n do
		local title, level, _, isHeader, _, isComplete, _, questID = GetQuestLogTitle(i)
		title, level, isHeader = safe(title), safe(level), safe(isHeader)
		if isHeader then
			header = title
		elseif title then
			isComplete = safe(isComplete)
			local objectives = {}
			for j = 1, (GetNumQuestLeaderBoards(i) or 0) do
				local text, _, finished = GetQuestLogLeaderBoard(j, i)
				text = safe(text)
				if text then
					-- "Kobold Vermin slain: 4/10"
					local have, need = text:match("(%d+)/(%d+)%s*$")
					objectives[#objectives + 1] = {
						text = text, done = safe(finished) and true or false,
						have = tonumber(have), need = tonumber(need),
					}
				end
			end
			quests[#quests + 1] = {
				id = safe(questID), title = title, level = level, header = header,
				complete = isComplete == 1, failed = isComplete == -1, objectives = objectives,
			}
		end
	end
	return quests
end

function FT.ReadQuestLog()
	local ok, quests
	if C_QuestLog and C_QuestLog.GetInfo and C_QuestLog.GetNumQuestLogEntries then
		ok, quests = pcall(readModern)
	elseif GetQuestLogTitle then
		ok, quests = pcall(readLegacy)
	end
	return ok and quests or {}
end

local function questTitle(questID)
	if not (C_QuestLog and questID) then return nil end
	if C_QuestLog.GetTitleForQuestID then
		local title = read(nil, C_QuestLog.GetTitleForQuestID, questID)
		if title then return title end
	end
	if C_QuestLog.GetQuestInfo then
		return read(nil, C_QuestLog.GetQuestInfo, questID)
	end
end

-- The last saved quest log still holds quests that were just removed.
local function knownTitle(questID)
	for _, q in ipairs(FT.char and FT.char.quests or {}) do
		if q.id == questID then return q.title end
	end
	return questTitle(questID)
end

local function lastArg(...)
	return safe((select(select("#", ...), ...)))
end

function handlers.QUEST_LOG_UPDATE()
	FT.questsDirty = true
end

-- Classic passes (logIndex, questID), modern clients just (questID).
function handlers.QUEST_ACCEPTED(...)
	local questID = lastArg(...)
	FT.questsDirty = true
	C_Timer.After(0.5, function()
		FT.AddEvent("accept", { questID = questID, title = questTitle(questID) })
	end)
end

function handlers.QUEST_TURNED_IN(questID, xpReward, moneyReward)
	questID = safe(questID)
	FT.questsDirty = true
	FT.AddEvent("quest", {
		questID = questID,
		title = knownTitle(questID),
		xp = safe(xpReward),
		money = safe(moneyReward),
	})
end

-- QUEST_REMOVED also fires on turn-in; an abandon is the one preceded by an
-- AbandonQuest call.
local abandoning
local function markAbandon() abandoning = GetTime() end
if C_QuestLog and C_QuestLog.AbandonQuest then
	hooksecurefunc(C_QuestLog, "AbandonQuest", markAbandon)
elseif AbandonQuest then
	hooksecurefunc("AbandonQuest", markAbandon)
end

function handlers.QUEST_REMOVED(questID)
	questID = safe(questID)
	FT.questsDirty = true
	if abandoning and GetTime() - abandoning < 2 then
		abandoning = nil
		FT.AddEvent("abandon", { questID = questID, title = knownTitle(questID) })
	end
end
