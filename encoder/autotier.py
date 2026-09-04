#!/usr/bin/env python3
from __future__ import annotations

import numpy as np
from PIL import Image

from codec import RefDecoder, VQEncoder
from source import fit_dimensions, iter_frames

CANDIDATE_PIXELS = (
    384 * 216,
    480 * 272,
    568 * 320,
    640 * 360,
    768 * 432,
)

DISPLAY_PIXELS = 960 * 540

PROBE_SECONDS = 1.5

TIE_MARGIN = 0.15
TIE_MAX_EXTRA_BITRATE = 1.10
PROBE_START_FRACTION = 0.25

def _resize(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(Image.fromarray(frame).resize(size, Image.BILINEAR))

def choose_tier(
    path: str,
    src_width: int | None,
    src_height: int | None,
    duration: float,
    fps: int,
    target_bytes_per_frame: int,
    max_pixels: int,
    probe_seconds: float = PROBE_SECONDS,
    log=print,
) -> tuple[int, int]:
    aspect_w = src_width or 16
    aspect_h = src_height or 9

    candidates: list[tuple[int, int]] = []
    for pixels in CANDIDATE_PIXELS:
        if pixels > max_pixels:
            continue
        dims = fit_dimensions(aspect_w, aspect_h, pixels)
        if dims not in candidates:
            candidates.append(dims)

    if not candidates:
        return fit_dimensions(aspect_w, aspect_h, min(CANDIDATE_PIXELS[0], max_pixels))
    if len(candidates) == 1:
        return candidates[0]

    display = fit_dimensions(aspect_w, aspect_h, DISPLAY_PIXELS, max_dim=4096)
    frame_count = max(int(probe_seconds * fps), fps)
    start = max(0.0, duration * PROBE_START_FRACTION) if duration else 0.0

    reference = list(iter_frames(path, display[0], display[1], fps,
                                 start_seconds=start, max_frames=frame_count))
    if not reference:
        return candidates[len(candidates) // 2]

    best_score = -1.0
    best_dims = candidates[0]
    rates: dict[tuple[int, int], float] = {}
    scored: list[str] = []

    for width, height in candidates:
        frames = [_resize(f, (width, height)) for f in reference]
        if not frames:
            continue

        encoder = VQEncoder(width, height, fps, target_bytes_per_frame=target_bytes_per_frame)
        decoder = RefDecoder(width, height)
        chunk = encoder.encode_chunk(frames, 0)
        decoded = decoder.decode_chunk(chunk)

        total, count = 0.0, 0
        for out, ref in zip(decoded, reference):
            diff = _resize(out, display).astype(np.float64) - ref.astype(np.float64)
            total += float((diff * diff).sum())
            count += diff.size
        if count == 0:
            continue

        mse = total / count

        score = 10 * np.log10(255.0**2 / mse) if mse > 0 else 99.0
        kbps = (len(chunk) / 1024) / (len(frames) / fps)
        scored.append(f"{width}x{height} {score:.1f}dB/{kbps:.0f}KBps")
        rates[(width, height)] = kbps

        bigger = width * height > best_dims[0] * best_dims[1]
        affordable = kbps <= rates.get(best_dims, kbps) * TIE_MAX_EXTRA_BITRATE
        if score > best_score + TIE_MARGIN or (
            bigger and affordable and score > best_score - TIE_MARGIN
        ):
            best_score, best_dims = max(score, best_score), (width, height)

    if scored:
        log(f"[autotier] {' | '.join(scored)} -> {best_dims[0]}x{best_dims[1]}")
    return best_dims
