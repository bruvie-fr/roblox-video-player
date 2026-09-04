#!/usr/bin/env python3
"""Does a tiled scan order actually shorten the run-length plane?

On real footage 42% of the bitstream is RLE token overhead. The suspicion is
that changed blocks cluster into 2D blobs, and raster scanning slices every blob
into one run per row. A tiled order should keep blobs contiguous.

This measures the run count both ways on real frames before committing to a
bitstream change -- the format is duplicated in Luau, so changing it is not free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from codec import VQEncoder, frame_to_blocks
from source import iter_frames

FPS = 30


def raster_order(bw: int, bh: int) -> np.ndarray:
    return np.arange(bw * bh, dtype=np.int64)


def tile_order(bw: int, bh: int, tile: int) -> np.ndarray:
    """Scan tile by tile, raster within each tile.

    Chosen over Morton order because it handles non-power-of-two block grids
    without padding. Padding would insert out-of-frame positions that split runs
    right back apart, which is the thing being fixed.
    """
    order = []
    for ty in range(0, bh, tile):
        for tx in range(0, bw, tile):
            for y in range(ty, min(ty + tile, bh)):
                base = y * bw
                order.extend(range(base + tx, base + min(tx + tile, bw)))
    return np.asarray(order, dtype=np.int64)


def count_runs(mask: np.ndarray) -> int:
    if len(mask) == 0:
        return 0
    return int(np.count_nonzero(np.diff(mask.view(np.int8)))) + 1


def main() -> None:
    clip = sys.argv[1] if len(sys.argv) > 1 else "_downloads/video.mp4"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 848
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 480
    if not Path(clip).exists():
        raise SystemExit(f"no such clip: {clip}")

    frames = list(iter_frames(clip, width, height, FPS, max_frames=150))
    bw, bh = width // 4, height // 4
    print(f"{clip}  {width}x{height}  {bw}x{bh} = {bw * bh} blocks, {len(frames)} frames\n")

    # Re-run the encoder to capture each frame's drift field and the threshold
    # in force at the time. Both are needed: the rules below re-derive the mask.
    enc = VQEncoder(width, height, FPS, target_bytes_per_frame=int(100 * 1024 / FPS))
    masks: list[np.ndarray] = []
    drifts: list[np.ndarray] = []
    thresholds: list[float] = []
    for frame in frames:
        blocks = frame_to_blocks(frame)
        diff = np.subtract(blocks, enc.recon, dtype=np.float32)
        drift = np.einsum("ij,ij->i", diff, diff) * (1.0 / 48)
        drifts.append(drift)
        thresholds.append(enc.skip_threshold)
        masks.append(drift > enc.skip_threshold)
        enc.encode_frame(frame)

    orders = {
        "raster": raster_order(bw, bh),
        "tile 4x4": tile_order(bw, bh, 4),
        "tile 8x8": tile_order(bw, bh, 8),
        "tile 16x16": tile_order(bw, bh, 16),
    }

    print(f"{'order':>12} {'runs/frame':>12} {'vs raster':>11} {'coded/frame':>12}")
    print("-" * 50)
    baseline = None
    for name, order in orders.items():
        runs = float(np.mean([count_runs(m[order]) for m in masks]))
        coded = float(np.mean([int(m.sum()) for m in masks]))
        if baseline is None:
            baseline = runs
        print(f"{name:>12} {runs:>12.0f} {runs / baseline:>10.2f}x {coded:>12.0f}")

    best = min(float(np.mean([count_runs(m[o]) for m in masks])) for o in orders.values())
    saved = (baseline - best) * FPS / 1024
    print(f"\nbest order saves ~{baseline - best:.0f} tokens/frame = {saved:.1f} KB/s at {FPS}fps")

    # --- coherence filter -------------------------------------------------
    # If the coded set is scattered rather than clustered, reordering cannot
    # help. Suppressing the scatter can: a lone block barely over threshold is
    # usually source noise, and coding it costs an index *and* two run tokens
    # while changing almost nothing on screen.
    print(f"\n{'coherence rule':>22} {'runs':>8} {'coded':>8} {'bytes/frame':>13} {'vs base':>9}")
    print("-" * 64)

    def neighbours(mask2d: np.ndarray) -> np.ndarray:
        """Count of 4-connected coded neighbours, without scipy."""
        pad = np.zeros((mask2d.shape[0] + 2, mask2d.shape[1] + 2), dtype=np.int8)
        pad[1:-1, 1:-1] = mask2d
        return (pad[:-2, 1:-1] + pad[2:, 1:-1] + pad[1:-1, :-2] + pad[1:-1, 2:])

    base_bytes = None
    for label, strong_mult, need in [
        ("none (baseline)", 1.0, 0),
        ("strong 2x, need 1", 2.0, 1),
        ("strong 3x, need 1", 3.0, 1),
        ("strong 3x, need 2", 3.0, 2),
        ("strong 6x, need 2", 6.0, 2),
    ]:
        runs_acc, coded_acc = [], []
        for drift, thr in zip(drifts, thresholds):
            weak = drift > thr
            if need == 0:
                kept = weak
            else:
                n = neighbours(weak.reshape(bh, bw).astype(np.int8)).reshape(-1)
                # Code a block if it changed a lot on its own, or if it changed
                # a little and its neighbours changed too -- the latter is real
                # motion, an isolated small change is almost always noise.
                kept = (drift > thr * strong_mult) | (weak & (n >= need))
            runs_acc.append(count_runs(kept))
            coded_acc.append(int(kept.sum()))
        runs = float(np.mean(runs_acc))
        coded = float(np.mean(coded_acc))
        total = runs + coded
        if base_bytes is None:
            base_bytes = total
        print(f"{label:>22} {runs:>8.0f} {coded:>8.0f} {total:>13.0f} {total / base_bytes:>8.2f}x")


if __name__ == "__main__":
    main()
