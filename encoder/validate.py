#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from codec import RefDecoder, VQEncoder, blocks_to_frame
from source import iter_frames, make_test_clip, probe

TIERS = [
    (480, 272, 30),
    (640, 360, 30),
    (768, 432, 30),
    (848, 480, 30),
    (1024, 576, 30),
]

BUDGET_KBPS = 100.0
BURST_KBPS = 200.0

def psnr_over(pairs: list[tuple[np.ndarray, np.ndarray]]) -> float:
    total, count = 0.0, 0
    for a, b in pairs:
        diff = a.astype(np.float64) - b.astype(np.float64)
        total += float((diff * diff).sum())
        count += diff.size
    if count == 0:
        return 0.0
    mse = total / count
    return float("inf") if mse == 0 else 10.0 * np.log10((255.0**2) / mse)

def run_tier(video: str, width: int, height: int, fps: int, seconds: int) -> dict:
    frames = list(iter_frames(video, width, height, fps, max_frames=fps * seconds))
    if not frames:
        raise SystemExit("ffmpeg produced no frames")

    target = int(BUDGET_KBPS * 1024 / fps)
    enc = VQEncoder(width, height, fps, target_bytes_per_frame=target)
    dec = RefDecoder(width, height)

    chunk = enc.encode_chunk(frames, chunk_index=0)
    decoded = dec.decode_chunk(chunk)

    enc_recon = blocks_to_frame(enc.recon, width, height)
    exact = np.array_equal(decoded[-1], enc_recon)

    quality = psnr_over(list(zip(frames, decoded)))
    total = len(chunk)
    kbps = (total / len(frames)) * fps / 1024.0
    per_frame = enc.stats.per_frame_bytes
    inter = [b for i, b in enumerate(per_frame) if i % enc.keyframe_interval != 0]

    s = enc.stats
    split = max(1, s.index_bytes + s.run_bytes + s.codebook_bytes)
    return {
        "width": width, "height": height, "fps": fps,
        "frames": len(frames), "total_bytes": total, "kbps": kbps,
        "psnr": quality, "exact": exact,
        "skip_pct": 100.0 * s.skipped_blocks / max(1, s.skipped_blocks + s.coded_blocks),
        "peak_frame": max(per_frame), "median_inter": int(np.median(inter)) if inter else 0,
        "idx_pct": 100.0 * s.index_bytes / split,
        "run_pct": 100.0 * s.run_bytes / split,
        "cb_pct": 100.0 * s.codebook_bytes / split,
    }

def main() -> None:
    seconds = 10
    if len(sys.argv) > 1:
        video = sys.argv[1]
    else:
        clip = Path("_testdata/testclip.mp4")
        if not clip.exists():
            print("[validate] synthesizing test clip ...", flush=True)
            make_test_clip(clip, seconds=seconds)
        video = str(clip)

    meta = probe(video)
    print(f"[validate] source: {video}")
    print(f"[validate]   {meta['width']}x{meta['height']} {meta['codec']} "
          f"{meta['fps']:.2f}fps {meta['duration_seconds']:.1f}s\n")

    header = (f"{'tier':>14} {'KB/s':>8} {'PSNR':>7} {'skip%':>7} "
              f"{'idx%':>6} {'run%':>6} {'cb%':>6} {'peak':>8}  {'exact':>6}  verdict")
    print(header)
    print("-" * len(header))

    for width, height, fps in TIERS:
        r = run_tier(video, width, height, fps, seconds)
        if r["kbps"] <= BUDGET_KBPS:
            verdict = "FITS"
        elif r["kbps"] <= BURST_KBPS:
            verdict = "tight"
        else:
            verdict = "OVER"
        print(f"{r['width']:>5}x{r['height']:<4}@{r['fps']:<2} "
              f"{r['kbps']:>8.1f} {r['psnr']:>7.2f} {r['skip_pct']:>6.1f}% "
              f"{r['idx_pct']:>5.1f}% {r['run_pct']:>5.1f}% {r['cb_pct']:>5.1f}% "
              f"{r['peak_frame']:>8}  {'yes' if r['exact'] else 'NO':>6}  {verdict}")

    print("\n[validate] idx% = codebook indices, run% = RLE token overhead, "
          "cb% = codebook updates")

    print(f"\n[validate] budget {BUDGET_KBPS:.0f} KB/s sustained, {BURST_KBPS:.0f} KB/s burst")
    print("[validate] 'exact' = reference decoder matches encoder reconstruction byte-for-byte")
    print("[validate] 'peak' is the keyframe; buffering absorbs it")

if __name__ == "__main__":
    main()
