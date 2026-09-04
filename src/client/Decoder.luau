--!strict
--!native

local Protocol = require(game:GetService("ReplicatedStorage").Shared.Protocol)

local MODE_SKIP_LONG = Protocol.MODE_SKIP_LONG
local MODE_VQ = Protocol.MODE_VQ
local MODE_VQ_LONG = Protocol.MODE_VQ_LONG
local CODEBOOK_SIZE = Protocol.CODEBOOK_SIZE

local Decoder = {}
Decoder.__index = Decoder

export type Stats = {
	lastDecodeMs: number,
	framesDecoded: number,
	blocksCoded: number,
	blocksSkipped: number,
}

export type Decoder = typeof(setmetatable(
	{} :: {
		width: number,
		height: number,
		bw: number,
		bh: number,
		nblocks: number,
		rowStride: number,
		frame: buffer,
		codebook: { buffer },
		stats: Stats,
		voices: { number },
		voiceCount: number,
		percussion: { number },
		onsets: number,
	},
	Decoder
))

function Decoder.new(width: number, height: number): Decoder
	assert(width % 4 == 0 and height % 4 == 0, `dimensions must be multiples of 4, got {width}x{height}`)

	local codebook = table.create(CODEBOOK_SIZE)
	for i = 1, CODEBOOK_SIZE do

		codebook[i] = buffer.create(Protocol.BLOCK_RGBA_BYTES)
	end

	return setmetatable({
		width = width,
		height = height,
		bw = width // 4,
		bh = height // 4,
		nblocks = (width // 4) * (height // 4),
		rowStride = width * 4,
		frame = buffer.create(width * height * 4),
		codebook = codebook,
		stats = {
			lastDecodeMs = 0,
			framesDecoded = 0,
			blocksCoded = 0,
			blocksSkipped = 0,
		},

		voices = table.create(Protocol.MAX_VOICES * 2, 0),
		voiceCount = 0,
		percussion = { 0, 0, 0 },
		onsets = 0,
	}, Decoder) :: any
end

function Decoder.decodeFrame(self: Decoder, data: buffer, offset: number): number
	local t0 = os.clock()

	local payloadLen = buffer.readu32(data, offset)
	offset += 4
	local frameEnd = offset + payloadLen

	if frameEnd > buffer.len(data) then
		error(`frame overruns chunk: needs {frameEnd}, have {buffer.len(data)}`)
	end

	offset += 1

	local voiceCount = buffer.readu8(data, offset)
	offset += 1
	local voices = self.voices
	for i = 1, voiceCount do
		local o = offset + (i - 1) * 3
		voices[(i - 1) * 2 + 1] = buffer.readu16(data, o) / Protocol.PITCH_SCALE
		voices[(i - 1) * 2 + 2] = buffer.readu8(data, o + 2)
	end
	self.voiceCount = voiceCount
	offset += voiceCount * 3

	local perc = self.percussion
	perc[1] = buffer.readu8(data, offset)
	perc[2] = buffer.readu8(data, offset + 1)
	perc[3] = buffer.readu8(data, offset + 2)
	offset += 3

	self.onsets = buffer.readu16(data, offset)
	offset += 2

	local cbCount = buffer.readu16(data, offset)
	offset += 2

	local codebook = self.codebook
	for _ = 1, cbCount do
		local slot = buffer.readu8(data, offset)
		offset += 1
		local dst = codebook[slot + 1]
		for k = 0, 15 do
			local s = offset + k * 3
			buffer.writeu32(
				dst,
				k * 4,
				buffer.readu8(data, s)
					+ buffer.readu8(data, s + 1) * 0x100
					+ buffer.readu8(data, s + 2) * 0x10000
					+ 0xFF000000
			)
		end
		offset += Protocol.BLOCK_RGB_BYTES
	end

	local frame = self.frame
	local rowStride = self.rowStride
	local bw = self.bw
	local nblocks = self.nblocks
	local blockRow2 = rowStride * 2
	local blockRow3 = rowStride * 3
	local blockAdvance = rowStride * 4

	local bi = 0
	local bx = 0
	local rowBase = 0
	local coded = 0

	while bi < nblocks and offset < frameEnd do
		local token = buffer.readu8(data, offset)
		offset += 1
		local mode = token // 64

		local run
		if mode == MODE_SKIP_LONG or mode == MODE_VQ_LONG then
			run = buffer.readu16(data, offset)
			offset += 2
		else
			run = (token % 64) + 1
		end

		if bi + run > nblocks then
			error(`token run overruns block plane: {bi + run} > {nblocks}`)
		end

		if mode == MODE_VQ or mode == MODE_VQ_LONG then
			coded += run
			for k = 0, run - 1 do
				local src = codebook[buffer.readu8(data, offset + k) + 1]
				local o = rowBase + bx * 16

				buffer.copy(frame, o, src, 0, 16)
				buffer.copy(frame, o + rowStride, src, 16, 16)
				buffer.copy(frame, o + blockRow2, src, 32, 16)
				buffer.copy(frame, o + blockRow3, src, 48, 16)

				bx += 1
				if bx == bw then
					bx = 0
					rowBase += blockAdvance
				end
			end
			offset += run
		else

			bx += run
			if bx >= bw then
				local rows = bx // bw
				rowBase += rows * blockAdvance
				bx -= rows * bw
			end
		end

		bi += run
	end

	local stats = self.stats
	stats.lastDecodeMs = (os.clock() - t0) * 1000
	stats.framesDecoded += 1
	stats.blocksCoded += coded
	stats.blocksSkipped += nblocks - coded

	return frameEnd
end

return Decoder
