--!strict

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local Stats = game:GetService("Stats")

local Shared = ReplicatedStorage:WaitForChild("Shared")
local Config = require(Shared:WaitForChild("Config"))
local Protocol = require(Shared:WaitForChild("Protocol"))
local WavPlayer = require(script.Parent:WaitForChild("WavPlayer"))
local ControlPanel = require(script.Parent:WaitForChild("ControlPanel"))
local Decoder = require(script.Parent:WaitForChild("Decoder"))
local Renderer = require(script.Parent:WaitForChild("Renderer"))

local SHOW_STATS = Config.SHOW_STATS
local BUFFER_TARGET_SECONDS = Config.BUFFER_TARGET_SECONDS
local BUFFER_MAX_SECONDS = Config.BUFFER_MAX_SECONDS
local MAX_CATCHUP_FRAMES = Config.MAX_CATCHUP_FRAMES

local remote = ReplicatedStorage:WaitForChild(Protocol.REMOTE_NAME) :: RemoteEvent
local control = ReplicatedStorage:WaitForChild(Protocol.CONTROL_NAME) :: RemoteEvent

local decoder: Decoder.Decoder? = nil
local renderer: Renderer.Renderer? = nil
local videoFps = 30

local queue: { { buf: buffer, offset: number, remaining: number } } = {}
local queuedFrames = 0

local playing = false
local nextFrameClock = 0
local framesPresented, framesDropped, chunksDropped, bytesReceived = 0, 0, 0, 0

local presentFailed = false

local buildingRenderer = false

local wav = WavPlayer.new()
wav:setVolume(Config.AUDIO_VOLUME)
wav:attachStatus(workspace)

local audioParts: { string } = {}
local audioExpected = 0

local function resetAudio()
	table.clear(audioParts)
	audioExpected = 0
	wav:stop()
end

local WINDOW_SECONDS = 2.0
local windowClock, windowBytes, windowPresented = 0, 0, 0
local measuredKBps, measuredFps = 0, 0

local panel = ControlPanel.new(function(url)
	control:FireServer({ action = "open", url = url })
end, function()
	control:FireServer({ action = "stop" })
end)

local function bufferedSeconds(): number
	return queuedFrames / math.max(videoFps, 1)
end

local function clearStream()
	table.clear(queue)
	queuedFrames = 0
	playing = false
end

local function teardown()
	clearStream()
	resetAudio()
	if renderer then
		(renderer :: Renderer.Renderer):destroy()
		renderer = nil
	end
	decoder = nil
	framesPresented, framesDropped, bytesReceived = 0, 0, 0
	windowBytes, windowPresented = 0, 0
	measuredKBps, measuredFps = 0, 0

	presentFailed = false
end

control.OnClientEvent:Connect(function(msg)
	if type(msg) ~= "table" then
		return
	end

	if msg.action == "audioReset" then
		resetAudio()
		audioExpected = tonumber(msg.total) or 0
		return
	end

	if msg.action == "audio" then
		local index = tonumber(msg.index)
		if index and type(msg.data) == "string" then
			audioParts[index] = msg.data
		end

		if audioExpected > 0 and #audioParts == audioExpected then
			local ok, err = wav:load(table.concat(audioParts))
			if ok then
				wav:play()
				print(`[VideoPlayer] audio ready, {math.floor(wav:duration())}s`)
			else
				warn(`[VideoPlayer] audio rejected: {err}`)
			end
		end
		return
	end

	if msg.action == "reset" then
		clearStream()
		return
	end

	if msg.action == "ended" then

		teardown()
		panel:setBusy(false)
		panel:setStatus("finished -- paste a link to play something else")
		return
	end

	if msg.action == "stop" then
		teardown()
		panel:setBusy(false)
		panel:setStatus("stopped -- paste a link to play something else")
		return
	end

	if msg.action ~= "status" then
		return
	end

	if presentFailed then
		return
	end

	local state = msg.state
	if state == "idle" then
		panel:setBusy(false)
		panel:setStatus("paste a video or GIF link to start")
	elseif state == "fetching" then
		panel:setBusy(true)
		panel:setStatus("fetching link...")
	elseif state == "probing" then
		panel:setBusy(true)
		panel:setStatus("choosing best resolution...")
	elseif state == "encoding" then
		panel:setBusy(true)
		local ready, total = msg.chunksReady or 0, msg.estimatedChunks or 0
		panel:setStatus(if total > 0
			then string.format("encoding %d/%d chunks", ready, total)
			else string.format("encoding (%d chunks)", ready))
	elseif state == "ready" then
		panel:setBusy(false)
		panel:setStatus(`playing: {msg.title or "video"}`)
	elseif state == "error" then
		panel:setBusy(false)
		panel:setStatus(msg.message or "failed", true)
	end
end)

remote.OnClientEvent:Connect(function(payload: buffer)
	if typeof(payload) ~= "buffer" then
		return
	end
	bytesReceived += buffer.len(payload)

	local header, err = Protocol.readHeader(payload)
	if not header then
		chunksDropped += 1
		warn(`[VideoPlayer] dropping chunk: {err}`)
		return
	end

	local d = decoder
	if not d or d.width ~= header.width or d.height ~= header.height then

		if buildingRenderer then
			return
		end
		buildingRenderer = true

		local previous = renderer
		renderer = nil
		decoder = Decoder.new(header.width, header.height)
		videoFps = math.max(header.fps, 1)
		clearStream()
		if previous then
			previous:destroy()
		end

		local ok, result = pcall(function()
			return Renderer.new(header.width, header.height, {
				showStats = SHOW_STATS,
				pixelated = Config.PIXELATED,
			})
		end)
		buildingRenderer = false

		if not ok then
			warn(`[VideoPlayer] renderer failed: {result}`)
			panel:setStatus("renderer failed -- see console", true)
			decoder = nil
			return
		end
		renderer = result :: any
	end

	table.insert(queue, {
		buf = payload,
		offset = header.bodyOffset,
		remaining = header.frameCount,
	})
	queuedFrames += header.frameCount

	if bufferedSeconds() > BUFFER_MAX_SECONDS then
		local newest = queue[#queue]
		table.clear(queue)
		table.insert(queue, newest)
		framesDropped += queuedFrames - newest.remaining
		queuedFrames = newest.remaining
		playing = false
	end
end)

local function decodeOne(): boolean
	local head = queue[1]
	if not head then
		return false
	end

	local d = decoder :: Decoder.Decoder
	local ok, result = pcall(function()
		return d:decodeFrame(head.buf, head.offset)
	end)

	if not ok then

		warn(`[VideoPlayer] decode error, dropping chunk: {result}`)
		queuedFrames -= head.remaining
		chunksDropped += 1
		table.remove(queue, 1)
		return false
	end

	head.offset = result :: number
	head.remaining -= 1
	queuedFrames -= 1
	if head.remaining <= 0 then
		table.remove(queue, 1)
	end
	return true
end

RunService.Heartbeat:Connect(function(dt)
	if not decoder or not renderer then
		return
	end
	local r = renderer :: Renderer.Renderer
	local d = decoder :: Decoder.Decoder

	local now = os.clock()
	local frameInterval = 1 / videoFps

	if not playing and bufferedSeconds() >= BUFFER_TARGET_SECONDS then
		playing = true
		nextFrameClock = now
	end

	if playing and now >= nextFrameClock then

		local decodedAny, guard = false, 0
		while now >= nextFrameClock and guard < MAX_CATCHUP_FRAMES do
			if not decodeOne() then
				break
			end
			decodedAny = true
			nextFrameClock += frameInterval
			guard += 1
			if guard > 1 then
				framesDropped += 1
			end
		end

		if now > nextFrameClock then
			nextFrameClock = now
		end

		if decodedAny then
			local ok, err = r:present(d.frame)
			if ok then
				framesPresented += 1
			elseif not presentFailed then
				presentFailed = true
				panel:setStatus(`render failed: {err}`, true)
				warn(`[VideoPlayer] render failed: {err}`)
			end
		else
			playing = false
			nextFrameClock = now
		end
	end

	if SHOW_STATS then
		local windowElapsed = now - windowClock
		if windowElapsed >= WINDOW_SECONDS then
			measuredKBps = (bytesReceived - windowBytes) / 1024 / windowElapsed
			measuredFps = (framesPresented - windowPresented) / windowElapsed
			windowClock = now
			windowBytes = bytesReceived
			windowPresented = framesPresented
		end

		local s = d.stats
		local skipPct = 100 * s.blocksSkipped / math.max(1, s.blocksSkipped + s.blocksCoded)
		r:setStats(string.format(
			"%dx%d  %s  [%s]\ndecode  %.2f ms/frame   skip %.1f%%\nshown   %.1f fps  (%d shown, %d dropped)\nnet     %.1f KB/s measured\nengine  %.1f KB/s (0 in play-solo)\nbuffer  %.1f s (%d frames)\ndata    %.0f KB recv, %d bad chunks",
			d.width, d.height, playing and "PLAYING" or "BUFFERING", r.backend,
			s.lastDecodeMs, skipPct,
			measuredFps, framesPresented, framesDropped,
			measuredKBps,
			Stats.DataReceiveKbps,
			bufferedSeconds(), queuedFrames,
			bytesReceived / 1024, chunksDropped
		))
	end
end)
