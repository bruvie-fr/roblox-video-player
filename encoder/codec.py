#!/usr/bin/env python3
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"RVS5"

PITCH_SCALE = 10.0
FLAG_CHUNK_AUDIO = 0x01
MAX_VOICES = 16
BLOCK = 4
BLOCK_PIXELS = BLOCK * BLOCK
BLOCK_RGB_BYTES = BLOCK_PIXELS * 3
BLOCK_RGBA_BYTES = BLOCK_PIXELS * 4

MODE_SKIP = 0
MODE_VQ = 1
MODE_SKIP_LONG = 2
MODE_VQ_LONG = 3

MAX_SHORT_RUN = 64
MAX_LONG_RUN = 0xFFFF

FLAG_KEYFRAME = 0x01

DEFAULT_CODEBOOK_SIZE = 256
DEFAULT_TARGET_BYTES = 3400

DEFAULT_KEYFRAME_INTERVAL = 45

def frame_to_blocks(rgb: np.ndarray) -> np.ndarray:
    h, w, _ = rgb.shape
    bh, bw = h // BLOCK, w // BLOCK
    return (
        rgb.reshape(bh, BLOCK, bw, BLOCK, 3)
        .transpose(0, 2, 1, 3, 4)
        .reshape(bh * bw, BLOCK_RGB_BYTES)
    )

def _kmeans(samples: np.ndarray, k: int, iters: int = 6) -> np.ndarray:
    distinct = np.unique(samples, axis=0)
    k = min(k, len(distinct))
    order = np.argsort(distinct.mean(axis=1))
    centers = distinct[order[np.linspace(0, len(distinct) - 1, k).astype(np.int64)]].copy()

    dims = samples.shape[1]
    for _ in range(iters):
        d = (samples @ centers.T) * -2.0
        d += (centers * centers).sum(1)[None, :]
        labels = np.argmin(d, axis=1)

        counts = np.bincount(labels, minlength=k)
        sums = np.empty((k, dims), dtype=np.float32)

        for j in range(dims):
            sums[:, j] = np.bincount(labels, weights=samples[:, j], minlength=k)

        alive = counts > 0
        centers[alive] = sums[alive] / counts[alive, None]

    return centers

def blocks_to_frame(blocks: np.ndarray, width: int, height: int) -> np.ndarray:
    bh, bw = height // BLOCK, width // BLOCK
    return (
        blocks.reshape(bh, bw, BLOCK, BLOCK, 3)
        .transpose(0, 2, 1, 3, 4)
        .reshape(height, width, 3)
    )

DEFAULT_REFRESH_INTERVAL = 4

COHERENCE_STRONG = 3.0
COHERENCE_NEED = 2

MIN_REFRESH_ENTRIES = 8
MAX_REFRESH_ENTRIES = 48

REFRESH_URGENT_FRACTION = 0.25

KEYFRAME_FIT_SAMPLES = 40000

@dataclass
class EncoderStats:
    frames: int = 0
    total_bytes: int = 0
    keyframes: int = 0
    skipped_blocks: int = 0
    coded_blocks: int = 0
    codebook_updates: int = 0
    per_frame_bytes: list[int] = field(default_factory=list)

    index_bytes: int = 0
    run_bytes: int = 0
    codebook_bytes: int = 0
    runs: int = 0

class VQEncoder:

    def __init__(
        self,
        width: int,
        height: int,
        fps: int,
        codebook_size: int = DEFAULT_CODEBOOK_SIZE,
        target_bytes_per_frame: int = DEFAULT_TARGET_BYTES,
        keyframe_interval: int = DEFAULT_KEYFRAME_INTERVAL,
    ) -> None:
        if width % BLOCK or height % BLOCK:
            raise ValueError(f"dimensions must be multiples of {BLOCK}: got {width}x{height}")
        if not 1 <= codebook_size <= 256:
            raise ValueError("codebook_size must be 1..256 (index is one byte)")

        self.width, self.height, self.fps = width, height, fps
        self.bw, self.bh = width // BLOCK, height // BLOCK
        self.nblocks = self.bw * self.bh
        self.codebook_size = codebook_size
        self.target_bytes = target_bytes_per_frame
        self.keyframe_interval = keyframe_interval

        self.codebook = np.zeros((codebook_size, BLOCK_RGB_BYTES), dtype=np.uint8)
        self.codebook_ready = False

        self.usage = np.zeros(codebook_size, dtype=np.float32)
        self.recon = np.zeros((self.nblocks, BLOCK_RGB_BYTES), dtype=np.uint8)
        self.frame_index = 0
        self.refresh_interval = DEFAULT_REFRESH_INTERVAL

        self._cb_cache: tuple[np.ndarray, np.ndarray] | None = None

        self.skip_threshold = 40.0
        self.avg_bytes = float(target_bytes_per_frame)
        self.stats = EncoderStats()

    def _invalidate_codebook(self) -> None:
        self._cb_cache = None

    def _codebook_f32(self) -> tuple[np.ndarray, np.ndarray]:
        if self._cb_cache is None:
            c = self.codebook.astype(np.float32)
            self._cb_cache = (c, (c * c).sum(1))
        return self._cb_cache

    def _nearest(self, blocks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        b = np.ascontiguousarray(blocks, dtype=np.float32)
        c, c2 = self._codebook_f32()
        d = (b @ c.T) * -2.0
        d += c2[None, :]
        idx = np.argmin(d, axis=1)
        err = d[np.arange(len(b)), idx] + (b * b).sum(1)
        return idx, err

    def _fit_codebook(self, blocks: np.ndarray) -> None:
        sample = blocks
        if len(blocks) > KEYFRAME_FIT_SAMPLES:
            pick = np.linspace(0, len(blocks) - 1, KEYFRAME_FIT_SAMPLES).astype(np.int64)
            sample = blocks[pick]

        centers = _kmeans(sample.astype(np.float32), self.codebook_size)
        k = len(centers)
        self.codebook[:k] = np.clip(centers, 0, 255).astype(np.uint8)
        if k < self.codebook_size:
            self.codebook[k:] = centers[-1]
        self.codebook_ready = True
        self.usage[:] = 1.0
        self._invalidate_codebook()

    def _refresh_entries(self, blocks: np.ndarray, errors: np.ndarray, budget: int) -> list[int]:
        if budget <= 0 or len(blocks) == 0:
            return []

        k = min(budget, len(blocks))
        worst = np.argpartition(errors, -k)[-k:] if k < len(errors) else np.arange(len(errors))
        picked = blocks[worst]

        victims = np.argsort(self.usage)[:len(picked)]
        for slot, entry in zip(victims, picked):
            self.codebook[slot] = entry
            self.usage[slot] = 1.0
        self._invalidate_codebook()
        return [int(v) for v in victims]

    def _emit_tokens(self, modes: np.ndarray, indices: np.ndarray) -> bytearray:
        out = bytearray()
        n = len(modes)
        if n == 0:
            return out

        idx8 = indices.astype(np.uint8)
        edges = np.flatnonzero(np.diff(modes.view(np.int8))) + 1
        bounds = np.concatenate(([0], edges, [n]))

        for start, stop in zip(bounds[:-1], bounds[1:]):
            coded = bool(modes[start])
            self.stats.runs += 1
            pos, remaining = int(start), int(stop - start)

            while remaining > 0:
                take = min(remaining, MAX_LONG_RUN)
                if take <= MAX_SHORT_RUN:
                    mode = MODE_VQ if coded else MODE_SKIP
                    out.append((mode << 6) | (take - 1))
                else:
                    mode = MODE_VQ_LONG if coded else MODE_SKIP_LONG
                    out.append(mode << 6)
                    out += struct.pack("<H", take)
                if coded:
                    out += idx8[pos:pos + take].tobytes()
                pos += take
                remaining -= take
        return out

    def encode_frame(
        self,
        rgb: np.ndarray,
        voices: list[tuple[int, int]] | None = None,
        percussion: tuple[int, int, int] = (0, 0, 0),
        onsets: int = 0,
    ) -> bytes:
        if rgb.shape != (self.height, self.width, 3):
            raise ValueError(f"expected {(self.height, self.width, 3)}, got {rgb.shape}")

        blocks = frame_to_blocks(rgb)
        keyframe = (not self.codebook_ready) or (self.frame_index % self.keyframe_interval == 0)

        cb_updates: list[int] = []
        if keyframe:
            self._fit_codebook(blocks)
            cb_updates = list(range(self.codebook_size))
            code_mask = np.ones(self.nblocks, dtype=bool)
        else:
            diff = np.subtract(blocks, self.recon, dtype=np.float32)
            drift = np.einsum("ij,ij->i", diff, diff) * (1.0 / BLOCK_RGB_BYTES)

            weak = drift > self.skip_threshold

            pad = np.zeros((self.bh + 2, self.bw + 2), dtype=np.int8)
            pad[1:-1, 1:-1] = weak.reshape(self.bh, self.bw)
            company = (
                pad[:-2, 1:-1] + pad[2:, 1:-1] + pad[1:-1, :-2] + pad[1:-1, 2:]
            ).reshape(-1)

            code_mask = (drift > self.skip_threshold * COHERENCE_STRONG) | (
                weak & (company >= COHERENCE_NEED)
            )

        idx = np.zeros(self.nblocks, dtype=np.int64)
        coded = np.flatnonzero(code_mask)

        self.usage *= 0.9

        if len(coded) > 0:

            sub = blocks[coded]
            sub_idx, sub_err = self._nearest(sub)

            if not keyframe and self.frame_index % self.refresh_interval == 0:
                poor = sub_err > (self.skip_threshold * BLOCK_RGB_BYTES * 4)
                poor_count = int(poor.count_nonzero()) if hasattr(poor, "count_nonzero") else int(poor.sum())

                if poor_count:
                    spare = max(0, self.target_bytes - len(coded))
                    budget = spare // (BLOCK_RGB_BYTES + 1)
                    if poor_count >= max(1, int(len(coded) * REFRESH_URGENT_FRACTION)):
                        budget = MAX_REFRESH_ENTRIES
                    budget = min(MAX_REFRESH_ENTRIES, max(MIN_REFRESH_ENTRIES, budget))

                    cb_updates = self._refresh_entries(sub[poor], sub_err[poor], budget)
                    sub_idx, sub_err = self._nearest(sub)

            idx[coded] = sub_idx
            self.usage += np.bincount(sub_idx, minlength=self.codebook_size).astype(np.float32)

            self.recon[coded] = self.codebook[sub_idx]

        payload = bytearray()
        payload.append(FLAG_KEYFRAME if keyframe else 0)

        picked = (voices or [])[:MAX_VOICES]
        payload.append(len(picked))
        for hz, level in picked:
            ticks = int(round(float(hz) * PITCH_SCALE))
            payload += struct.pack("<HB", max(0, min(65535, ticks)),
                                   max(0, min(255, int(level))))

        for level in percussion:
            payload.append(max(0, min(255, int(level))))

        payload += struct.pack("<H", max(0, min(0xFFFF, int(onsets))))

        payload += struct.pack("<H", len(cb_updates))
        for slot in cb_updates:
            payload.append(slot)
            payload += self.codebook[slot].tobytes()

        tokens = self._emit_tokens(code_mask, idx)
        payload += tokens

        self.stats.index_bytes += len(coded)
        self.stats.run_bytes += len(tokens) - len(coded)
        self.stats.codebook_bytes += len(cb_updates) * (BLOCK_RGB_BYTES + 1)

        frame_bytes = struct.pack("<I", len(payload)) + bytes(payload)

        self.avg_bytes = 0.9 * self.avg_bytes + 0.1 * len(frame_bytes)
        ratio = self.avg_bytes / max(self.target_bytes, 1)

        if ratio > 1.02:

            self.skip_threshold = min(self.skip_threshold * min(ratio, 1.20), 8000.0)
        elif ratio < 0.92:
            self.skip_threshold = max(self.skip_threshold * max(ratio, 0.88), 4.0)

        self.stats.frames += 1
        self.stats.total_bytes += len(frame_bytes)
        self.stats.keyframes += int(keyframe)
        self.stats.coded_blocks += int(code_mask.sum())
        self.stats.skipped_blocks += int(self.nblocks - code_mask.sum())
        self.stats.codebook_updates += len(cb_updates)
        self.stats.per_frame_bytes.append(len(frame_bytes))

        self.frame_index += 1
        return frame_bytes

    def encode_chunk(
        self,
        frames: list[np.ndarray],
        chunk_index: int,
        voices_per_frame: list[list[tuple[int, int]]] | None = None,
        percussion_per_frame: list[tuple[int, int, int]] | None = None,
        onsets_per_frame: list[int] | None = None,
    ) -> bytes:
        if voices_per_frame is None:
            voices_per_frame = [[] for _ in frames]
        if percussion_per_frame is None:
            percussion_per_frame = [(0, 0, 0) for _ in frames]
        if onsets_per_frame is None:
            onsets_per_frame = [0] * len(frames)
        body = b"".join(
            self.encode_frame(f, v, p, o)
            for f, v, p, o in zip(frames, voices_per_frame, percussion_per_frame,
                                  onsets_per_frame)
        )
        flags = FLAG_CHUNK_AUDIO if (any(voices_per_frame) or any(any(p) for p in percussion_per_frame)) else 0
        header = (
            MAGIC
            + struct.pack("<I", chunk_index)
            + struct.pack("<H", len(frames))
            + struct.pack("<H", self.width)
            + struct.pack("<H", self.height)
            + struct.pack("<B", self.fps)
            + struct.pack("<B", flags)
        )
        return header + body

class RefDecoder:
    def __init__(self, width: int, height: int, codebook_size: int = DEFAULT_CODEBOOK_SIZE) -> None:
        self.width, self.height = width, height
        self.bw, self.bh = width // BLOCK, height // BLOCK
        self.nblocks = self.bw * self.bh
        self.codebook = np.zeros((codebook_size, BLOCK_RGB_BYTES), dtype=np.uint8)
        self.blocks = np.zeros((self.nblocks, BLOCK_RGB_BYTES), dtype=np.uint8)
        self.voices: list[tuple[int, int]] = []
        self.percussion: tuple[int, int, int] = (0, 0, 0)
        self.onsets: int = 0

    def decode_frame(self, data: bytes, offset: int = 0) -> tuple[np.ndarray, int]:
        (payload_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        end = offset + payload_len

        offset += 1

        voice_count = data[offset]
        offset += 1
        self.voices = [
            (lambda t, a: (t / PITCH_SCALE, a))(
                *struct.unpack_from("<HB", data, offset + i * 3))
            for i in range(voice_count)
        ]
        offset += voice_count * 3
        self.percussion = (data[offset], data[offset + 1], data[offset + 2])
        offset += 3
        (self.onsets,) = struct.unpack_from("<H", data, offset)
        offset += 2

        (cb_count,) = struct.unpack_from("<H", data, offset)
        offset += 2
        for _ in range(cb_count):
            slot = data[offset]
            offset += 1
            self.codebook[slot] = np.frombuffer(data, np.uint8, BLOCK_RGB_BYTES, offset)
            offset += BLOCK_RGB_BYTES

        block = 0
        while offset < end and block < self.nblocks:
            token = data[offset]
            offset += 1
            mode, short_run = token >> 6, (token & 0x3F) + 1

            if mode in (MODE_SKIP_LONG, MODE_VQ_LONG):
                (run,) = struct.unpack_from("<H", data, offset)
                offset += 2
            else:
                run = short_run

            if mode in (MODE_VQ, MODE_VQ_LONG):
                indices = np.frombuffer(data, np.uint8, run, offset)
                offset += run
                self.blocks[block:block + run] = self.codebook[indices]
            block += run

        return blocks_to_frame(self.blocks, self.width, self.height), end

    def decode_chunk(self, data: bytes) -> list[np.ndarray]:
        if data[:4] != MAGIC:
            raise ValueError(f"bad magic: {data[:4]!r}")
        _, count, width, height, _fps, _ = struct.unpack_from("<IHHHBB", data, 4)
        if (width, height) != (self.width, self.height):
            raise ValueError(f"chunk is {width}x{height}, decoder is {self.width}x{self.height}")
        offset = 16
        frames = []
        for _ in range(count):
            frame, offset = self.decode_frame(data, offset)
            frames.append(frame.copy())
        return frames
