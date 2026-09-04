--!strict

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Shared")
local Config = require(Shared:WaitForChild("Config"))
local Protocol = require(Shared:WaitForChild("Protocol"))

local BASE_URL = Config.ENCODER_URL
local LEAD_SECONDS = Config.LEAD_SECONDS
local REPLAY_CHUNKS = Config.REPLAY_CHUNKS

local OPEN_COOLDOWN = 3

local remote = Instance.new("RemoteEvent")
remote.Name = Protocol.REMOTE_NAME
remote.Parent = ReplicatedStorage

local control = Instance.new("RemoteEvent")
control.Name = Protocol.CONTROL_NAME
control.Parent = ReplicatedStorage

type Info = {
	generation: number,
	state: string,
	message: string,
	title: string?,
	width: number?,
	height: number?,
	fps: number?,
	framesPerChunk: number?,
	keyframeEveryChunks: number?,
	chunksReady: number,
	estimatedChunks: number?,
	complete: boolean,
	playable: boolean,
}

local info: Info? = nil
local replay: { { data: buffer, keyframe: boolean } } = {}
local lastOpenAt: { [Player]: number } = {}

local function httpRequest(method: string, path: string, body: string?): (boolean, string)
	local ok, result = pcall(function()
		return HttpService:RequestAsync({
			Url = BASE_URL .. path,
			Method = method,
			Headers = { ["Content-Type"] = "application/json" },
			Body = body,
		})
	end)
	if not ok then
		return false, tostring(result)
	end
	local res = result :: any
	if not res.Success then
		return false, `HTTP {res.StatusCode} {res.StatusMessage}`
	end
	return true, res.Body
end

local function httpGet(path: string): (boolean, string)
	return httpRequest("GET", path)
end

local function broadcastStatus()
	local current = info
	if not current then
		return
	end
	control:FireAllClients({
		action = "status",
		state = current.state,
		message = current.message,
		title = current.title,
		chunksReady = current.chunksReady,
		estimatedChunks = current.estimatedChunks,
	})
end

local function pushReplay(data: buffer, keyframe: boolean)
	table.insert(replay, { data = data, keyframe = keyframe })
	while #replay > REPLAY_CHUNKS do
		table.remove(replay, 1)
	end
end

local AUDIO_CHUNK_BYTES = 40000

local audioChunks: { string } = {}
local audioGeneration = -1
local audioFetching = false
local endedGeneration = -1

local function sendAudioChunk(target: Player?, index: number)
	local msg = {
		action = "audio",
		index = index,
		total = #audioChunks,
		data = audioChunks[index],
	}
	if target then
		control:FireClient(target, msg)
	else
		control:FireAllClients(msg)
	end
end

local function fetchAudio(generation: number)
	if audioFetching or audioGeneration == generation then
		return
	end
	audioFetching = true
	task.spawn(function()
		local ok, body = httpGet(`/stream/audio.wav?g={generation}`)
		audioFetching = false
		if not ok or type(body) ~= "string" or #body < 64 then
			warn("[StreamServer] no audio for this clip")
			return
		end
		local current = info
		if current and current.generation ~= generation then
			return
		end

		table.clear(audioChunks)
		local offset = 1
		while offset <= #body do
			table.insert(audioChunks, string.sub(body, offset, offset + AUDIO_CHUNK_BYTES - 1))
			offset += AUDIO_CHUNK_BYTES
		end
		audioGeneration = generation

		control:FireAllClients({ action = "audioReset", total = #audioChunks })
		for i = 1, #audioChunks do
			sendAudioChunk(nil, i)

			if i % 4 == 0 then
				task.wait()
			end
		end
		print(string.format("[StreamServer] gen %d: audio sent, %d KB in %d chunks",
			generation, math.floor(#body / 1024), #audioChunks))
	end)
end

local function primePlayer(player: Player)
	local current = info
	if current then
		control:FireClient(player, {
			action = "status",
			state = current.state,
			message = current.message,
			title = current.title,
			chunksReady = current.chunksReady,
			estimatedChunks = current.estimatedChunks,
		})
	end

	if #audioChunks > 0 then
		control:FireClient(player, { action = "audioReset", total = #audioChunks })
		task.spawn(function()
			for i = 1, #audioChunks do
				sendAudioChunk(player, i)
				if i % 4 == 0 then
					task.wait()
				end
			end
		end)
	end

	local start: number? = nil
	for i = #replay, 1, -1 do
		if replay[i].keyframe then
			start = i
			break
		end
	end
	if not start then
		return
	end
	for i = start, #replay do
		remote:FireClient(player, replay[i].data)
	end
end

Players.PlayerAdded:Connect(primePlayer)
Players.PlayerRemoving:Connect(function(player)
	lastOpenAt[player] = nil
end)

control.OnServerEvent:Connect(function(player, payload)
	if type(payload) ~= "table" then
		return
	end

	local operator = Config.OPERATOR
	if operator ~= "" and player.Name ~= operator then
		return
	end

	if payload.action == "stop" then
		print(`[StreamServer] {player.Name} stopped playback`)
		local ok, err = httpRequest("POST", "/stream/stop", "{}")
		if not ok then
			warn(`[StreamServer] stop failed: {err}`)
		end

		table.clear(replay)
		table.clear(audioChunks)
		control:FireAllClients({ action = "stop" })
		return
	end

	if payload.action ~= "open" then
		return
	end
	local url = payload.url
	if type(url) ~= "string" or #url == 0 or #url > 2048 then
		control:FireClient(player, { action = "status", state = "error", message = "invalid link" })
		return
	end

	local now = os.clock()
	local last = lastOpenAt[player]
	if last and now - last < OPEN_COOLDOWN then
		control:FireClient(player, {
			action = "status", state = "error",
			message = `wait {math.ceil(OPEN_COOLDOWN - (now - last))}s before loading another`,
		})
		return
	end
	lastOpenAt[player] = now

	print(`[StreamServer] {player.Name} requested: {url}`)
	control:FireAllClients({ action = "status", state = "fetching", message = "fetching " .. url })

	local ok, response = httpRequest("POST", "/stream/open", HttpService:JSONEncode({ url = url }))

	if not ok then
		warn(`[StreamServer] open failed: {response}`)
		control:FireAllClients({
			action = "status", state = "error",
			message = "encoder unreachable -- is server.py running?",
		})
	end
end)

task.spawn(function()
	while true do
		local ok, body = httpGet("/stream/info")
		if ok then
			local decoded
			if pcall(function() decoded = HttpService:JSONDecode(body) end) then
				local previous = info
				info = decoded
				local current = info :: Info

				if previous and previous.state ~= "idle" and current.state == "idle" then
					table.clear(replay)
					control:FireAllClients({ action = "stop" })
				end

				if not previous
					or previous.state ~= current.state
					or previous.generation ~= current.generation
					or (current.state == "encoding" and previous.chunksReady ~= current.chunksReady)
				then
					broadcastStatus()
				end
			end
		end
		task.wait(0.75)
	end
end)

task.spawn(function()
	if not HttpService.HttpEnabled then
		warn("[StreamServer] HttpService.HttpEnabled is false -- enable it in Game Settings.")
		return
	end

	local generation = -1
	local chunkIndex = 0
	local keyframeEvery = 4
	local chunkSeconds = 0.5
	local startClock = os.clock()
	local sentSeconds = 0.0

	while true do
		local current = info

		if not current or current.state == "idle" or not current.playable then
			task.wait(0.5)
			continue
		end

		if current.generation ~= generation then
			generation = current.generation
			chunkIndex = 0
			table.clear(replay)
			startClock = os.clock()
			sentSeconds = 0.0
			keyframeEvery = math.max(current.keyframeEveryChunks or 4, 1)
			chunkSeconds = (current.framesPerChunk or 15) / math.max(current.fps or 30, 1)
			control:FireAllClients({ action = "reset" })
			table.clear(audioChunks)
			audioGeneration = -1
			endedGeneration = -1
			fetchAudio(generation)
			print(string.format("[StreamServer] gen %d: %dx%d @%dfps -- %s",
				generation, current.width or 0, current.height or 0, current.fps or 0,
				current.title or "?"))
		end

		if chunkIndex >= current.chunksReady then
			if current.complete then

				if endedGeneration ~= generation then
					endedGeneration = generation
					control:FireAllClients({ action = "ended" })

					table.clear(replay)
					table.clear(audioChunks)
					print(string.format("[StreamServer] gen %d: finished, %d chunks sent",
						generation, chunkIndex))
				end
				task.wait(0.5)
			else
				task.wait(0.25)
			end
			continue
		end

		local ok, body = httpGet(`/stream/chunk/{chunkIndex}?g={generation}`)
		if not ok then
			task.wait(0.25)
			continue
		end

		local data = buffer.fromstring(body)
		if Protocol.readHeader(data) then
			local keyframe = chunkIndex % keyframeEvery == 0
			pushReplay(data, keyframe)
			remote:FireAllClients(data)
		else
			warn(`[StreamServer] chunk {chunkIndex} had a bad header, skipping`)
		end

		chunkIndex += 1
		sentSeconds += chunkSeconds

		local sleep = sentSeconds - (os.clock() - startClock) - LEAD_SECONDS
		if sleep > 0 then
			task.wait(sleep)
		end
	end
end)
