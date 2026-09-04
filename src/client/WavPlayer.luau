--!strict
--!native
--!optimize 2

local RunService = game:GetService("RunService")

local WavPlayer = {}
WavPlayer.__index = WavPlayer

local WINDOW = 1024
local BINS = WINDOW // 2 + 1

local VOICES = 384

local LOW = { id = "rbxassetid://9040512197", base = 440.0, cross = 0.812, half = 1 / 440.0 / 2 }
local HIGH = { id = "rbxassetid://198099744", base = 1050.0, cross = 2.531, half = 1 / 1050.0 / 2 }
local MAX_SPEED = 20.0

local GAIN = 2 / WINDOW

local TAU = math.pi * 2
local QUARTER = math.pi * 0.5

export type WavPlayer = typeof(setmetatable(
	{} :: {
		container: Folder,
		waves: { AudioPlayer },
		fader: AudioFader,
		analyzer: AudioAnalyzer,
		kinds: { number },
		data: buffer?,
		sampleRate: number,
		bitsPerSample: number,
		sampleCount: number,
		dataOffset: number,
		scale: number,
		position: number,
		playing: boolean,
		looped: boolean,
		volume: number,
		speed: number,
		reals: { number },
		imags: { number },
		bitRev: { number },
		cosT: { number },
		sinT: { number },
		conn: RBXScriptConnection?,
		status: Folder?,
	},
	WavPlayer
))

local function buildTables(size: number)
	local bitRev, cosT, sinT = {}, {}, {}
	local bits = math.floor(math.log(size, 2) + 0.5)
	for i = 0, size - 1 do
		local rev, tmp = 0, i
		for _ = 1, bits do
			rev = bit32.bor(bit32.lshift(rev, 1), bit32.band(tmp, 1))
			tmp = bit32.rshift(tmp, 1)
		end
		bitRev[i] = rev
	end
	for k = 0, size // 2 - 1 do
		local angle = -2 * math.pi * k / size
		cosT[k] = math.cos(angle)
		sinT[k] = math.sin(angle)
	end
	return bitRev, cosT, sinT
end

local function fft(
	re: { number },
	im: { number },
	n: number,
	bitRev: { number },
	cosT: { number },
	sinT: { number }
)
	for i = 0, n - 1 do
		local j = bitRev[i]
		if j > i then
			re[i + 1], re[j + 1] = re[j + 1], re[i + 1]
			im[i + 1], im[j + 1] = im[j + 1], im[i + 1]
		end
	end
	local m = 1
	while m < n do
		local m2 = m * 2
		local step = n // m2
		for k = 0, m - 1 do
			local c, s = cosT[k * step], sinT[k * step]
			for j = k, n - 1, m2 do
				local a, b = j + 1, j + m + 1
				local tr = c * re[b] - s * im[b]
				local ti = s * re[b] + c * im[b]
				re[b] = re[a] - tr
				im[b] = im[a] - ti
				re[a] = re[a] + tr
				im[a] = im[a] + ti
			end
		end
		m = m2
	end
end

local function makeWave(parent: Instance, bus: AudioFader, index: number): AudioPlayer
	local p = Instance.new("AudioPlayer")
	p.Name = `bin{index}`
	p.Asset = LOW.id
	p.Looping = true
	p.Volume = 0
	p.Parent = parent

	local w = Instance.new("Wire")
	w.SourceInstance = p
	w.TargetInstance = bus
	w.Parent = p
	return p
end

function WavPlayer.new(parent: Instance?): WavPlayer
	local container = Instance.new("Folder")
	container.Name = "WavPlayer"
	container.Parent = parent or game:GetService("SoundService")

	local fader = Instance.new("AudioFader")
	fader.Name = "Bus"
	fader.Volume = 1
	fader.Parent = container

	local analyzer = Instance.new("AudioAnalyzer")
	analyzer.Name = "Analyzer"
	analyzer.Parent = container

	local out = Instance.new("AudioDeviceOutput")
	out.Name = "Output"
	out.Player = game:GetService("Players").LocalPlayer
	out.Parent = container

	for _, target in ipairs({ analyzer :: Instance, out :: Instance }) do
		local w = Instance.new("Wire")
		w.SourceInstance = fader
		w.TargetInstance = target
		w.Parent = fader
	end

	local waves = table.create(VOICES)
	local kinds = table.create(VOICES)
	for i = 1, VOICES do
		waves[i] = makeWave(container, fader, i - 1)
		kinds[i] = 0
	end

	local bitRev, cosT, sinT = buildTables(WINDOW)
	local self = setmetatable({
		container = container,
		waves = waves,
		fader = fader,
		analyzer = analyzer,
		kinds = kinds,
		data = nil,
		sampleRate = 22050,
		bitsPerSample = 16,
		sampleCount = 0,
		dataOffset = 0,
		scale = 1 / 32768,
		position = 0,
		playing = false,
		looped = false,
		volume = 1,
		speed = 1,
		reals = table.create(WINDOW, 0),
		imags = table.create(WINDOW, 0),
		bitRev = bitRev,
		cosT = cosT,
		sinT = sinT,
		conn = nil,
		status = nil,
	}, WavPlayer) :: any

	return self
end

local function readTag(buf: buffer, offset: number): string
	local out = table.create(4)
	for i = 0, 3 do
		out[i + 1] = string.char(buffer.readu8(buf, offset + i))
	end
	return table.concat(out)
end

function WavPlayer.load(self: WavPlayer, bytes: string): (boolean, string?)
	if #bytes < 64 then
		return false, `too short: {#bytes} bytes`
	end
	local buf = buffer.fromstring(bytes)
	if readTag(buf, 0) ~= "RIFF" or readTag(buf, 8) ~= "WAVE" then
		return false, "not a RIFF/WAVE file"
	end

	local fmtSize = buffer.readu32(buf, 16)
	local channels = buffer.readu16(buf, 22)
	local rate = buffer.readu32(buf, 24)
	local bits = buffer.readu16(buf, 34)
	if channels ~= 1 then
		return false, `must be mono, got {channels} channels`
	end
	if bits ~= 8 and bits ~= 16 and bits ~= 32 then
		return false, `unsupported bit depth {bits}`
	end

	local offset = 20 + fmtSize
	local dataSize = 0
	while offset + 8 <= #bytes do
		local tag = readTag(buf, offset)
		local size = buffer.readu32(buf, offset + 4)
		if tag == "data" then
			offset += 8
			dataSize = size
			break
		end
		offset += 8 + size + (size % 2)
	end
	if dataSize == 0 then
		return false, "no data chunk"
	end

	local available = #bytes - offset
	if dataSize > available then
		dataSize = available
	end

	self.data = buf
	self.sampleRate = rate
	self.bitsPerSample = bits
	self.dataOffset = offset
	self.sampleCount = dataSize // (bits // 8)
	self.scale = 1 / (2 ^ (bits - 1))
	self.position = 0
	self:retune()
	return true, nil
end

function WavPlayer.retune(self: WavPlayer)
	local spacing = self.sampleRate / WINDOW * math.abs(self.speed)
	for i = 1, VOICES do
		local freq = (i - 1) * spacing
		local wave = self.waves[i]
		local wantHigh = (freq / LOW.base) > MAX_SPEED
		local spec = if wantHigh then HIGH else LOW
		local kind = if wantHigh then 1 else 0
		if self.kinds[i] ~= kind then
			wave.Asset = spec.id
			self.kinds[i] = kind
		end
		wave.PlaybackSpeed = math.clamp(freq / spec.base, 0, MAX_SPEED)
	end
end

function WavPlayer.play(self: WavPlayer)
	if not self.data then
		return
	end
	self.playing = true

	for i = 2, VOICES do
		local wave = self.waves[i]
		if not wave.IsPlaying then
			wave:Play()
		end
	end
	if not self.conn then

		self.conn = RunService.PreRender:Connect(function(dt: number)
			self:step(dt)
		end)
	end
end

function WavPlayer.stop(self: WavPlayer)
	self.playing = false
	for i = 1, VOICES do
		local w = self.waves[i]
		w.Volume = 0
		if w.IsPlaying then
			w:Stop()
		end
	end
end

function WavPlayer.attachStatus(self: WavPlayer, parent: Instance): Folder
	local folder = Instance.new("Folder")
	folder.Name = "WavStatus"
	for _, n in ipairs({ "Position", "Length", "Loudness" }) do
		local v = Instance.new("NumberValue")
		v.Name = n
		v.Parent = folder
	end
	folder.Parent = parent
	self.status = folder
	return folder
end

function WavPlayer.setVolume(self: WavPlayer, v: number)
	self.volume = math.max(v, 0)
end

function WavPlayer.seek(self: WavPlayer, seconds: number)
	self.position = math.max(seconds, 0)
end

function WavPlayer.duration(self: WavPlayer): number
	if self.sampleRate <= 0 then
		return 0
	end
	return self.sampleCount / self.sampleRate
end

function WavPlayer.step(self: WavPlayer, dt: number)
	local data = self.data
	if not self.playing or not data then
		return
	end

	local duration = self.sampleCount / self.sampleRate
	if duration <= 0 then
		return
	end
	self.position += dt * self.speed
	if self.looped then
		self.position %= duration
	elseif self.position >= duration then
		self:stop()
		return
	end

	local at = self.position * self.sampleRate
	local index = math.floor(at)
	local frac = at - index
	index = math.clamp(index, 0, math.max(self.sampleCount - WINDOW, 0))

	local re, im = self.reals, self.imags
	local bits, base, scale = self.bitsPerSample, self.dataOffset, self.scale
	if bits == 16 then
		local o = base + index * 2
		for i = 1, WINDOW do
			re[i] = buffer.readi16(data, o + (i - 1) * 2) * scale
			im[i] = 0
		end
	elseif bits == 8 then
		local o = base + index
		for i = 1, WINDOW do
			re[i] = buffer.readi8(data, o + (i - 1)) * scale
			im[i] = 0
		end
	else
		local o = base + index * 4
		for i = 1, WINDOW do
			re[i] = buffer.readi32(data, o + (i - 1) * 4) * scale
			im[i] = 0
		end
	end

	fft(re, im, WINDOW, self.bitRev, self.cosT, self.sinT)

	local vol = self.volume * GAIN
	local fracPhase = TAU * frac / WINDOW
	local loudness = 0.0

	for i = 2, VOICES do
		local wave = self.waves[i]
		local r, m = re[i], im[i]
		local magnitude = math.sqrt(r * r + m * m) * vol

		local spec = if self.kinds[i] == 1 then HIGH else LOW

		local phase = (math.atan2(m, r) + QUARTER + fracPhase * (i - 1)) % TAU
		if phase > math.pi then
			phase -= TAU
		end
		wave.TimePosition = spec.cross + (phase / math.pi) * spec.half
		wave.Volume = magnitude
		loudness += magnitude
	end

	local status = self.status
	if status then
		(status.Position :: NumberValue).Value = self.position
		;(status.Length :: NumberValue).Value = duration
		;(status.Loudness :: NumberValue).Value = loudness / VOICES
	end
end

function WavPlayer.destroy(self: WavPlayer)
	if self.conn then
		self.conn:Disconnect()
		self.conn = nil
	end
	self.playing = false
	self.data = nil
	self.container:Destroy()
end

return WavPlayer
