--!strict

local Protocol = {}

Protocol.MAGIC = 0x35535652

Protocol.PITCH_SCALE = 10
Protocol.FLAG_CHUNK_AUDIO = 0x01
Protocol.MAX_VOICES = 16
Protocol.PERCUSSION_BANDS = 3

Protocol.BLOCK = 4
Protocol.BLOCK_PIXELS = 16
Protocol.BLOCK_RGB_BYTES = 48
Protocol.BLOCK_RGBA_BYTES = 64

Protocol.MODE_SKIP = 0
Protocol.MODE_VQ = 1
Protocol.MODE_SKIP_LONG = 2
Protocol.MODE_VQ_LONG = 3

Protocol.FLAG_KEYFRAME = 0x01

Protocol.CODEBOOK_SIZE = 256
Protocol.CHUNK_HEADER_BYTES = 16

Protocol.REMOTE_NAME = "VideoStream"
Protocol.CONTROL_NAME = "VideoControl"

export type ChunkHeader = {
	chunkIndex: number,
	frameCount: number,
	width: number,
	height: number,
	fps: number,
	bodyOffset: number,
}

function Protocol.readHeader(buf: buffer): (ChunkHeader?, string?)
	if buffer.len(buf) < Protocol.CHUNK_HEADER_BYTES then
		return nil, `chunk too short: {buffer.len(buf)} bytes`
	end
	if buffer.readu32(buf, 0) ~= Protocol.MAGIC then
		return nil, `bad magic: {string.format("0x%08X", buffer.readu32(buf, 0))}`
	end
	return {
		chunkIndex = buffer.readu32(buf, 4),
		frameCount = buffer.readu16(buf, 8),
		width = buffer.readu16(buf, 10),
		height = buffer.readu16(buf, 12),
		fps = buffer.readu8(buf, 14),
		bodyOffset = Protocol.CHUNK_HEADER_BYTES,
	}, nil
end

return Protocol
