"""Where does the visible error come from?

Two candidates, and they need opposite fixes:

  quantization -- the 256-entry codebook cannot represent the picture, so even a
                  freshly coded block lands on a poor match. Fix: bigger/better
                  codebook.
  staleness    -- the block was skipped to hold the bitrate, so what is on
                  screen is an old frame. Fix: rate control / bitrate.

Also measures how much a larger codebook would buy, by fitting one and scoring
its best-case match without changing the bitstream.
"""
import sys

import numpy as np

from codec import VQEncoder, _kmeans, frame_to_blocks
from source import iter_frames

CLIP = sys.argv[1] if len(sys.argv) > 1 else "_testdata/user.mp4"
FPS = 30


def analyse(width: int, height: int, budget_kbps: float = 100.0) -> None:
    frames = list(iter_frames(CLIP, width, height, FPS, max_frames=150))
    enc = VQEncoder(width, height, FPS, target_bytes_per_frame=int(budget_kbps * 1024 / FPS))

    quant_sse = 0.0     # error of coded blocks vs their source
    stale_sse = 0.0     # error of skipped blocks vs their source
    coded_n = stale_n = 0
    total_bytes = 0

    for frame in frames:
        blocks = frame_to_blocks(frame).astype(np.float32)
        before = enc.recon.astype(np.float32).copy()
        total_bytes += len(enc.encode_frame(frame))
        after = enc.recon.astype(np.float32)

        changed = np.any(after != before, axis=1)
        err = ((after - blocks) ** 2).sum(axis=1)
        quant_sse += float(err[changed].sum())
        stale_sse += float(err[~changed].sum())
        coded_n += int(changed.sum())
        stale_n += int((~changed).sum())

    total = quant_sse + stale_sse
    kbps = (total_bytes / 1024) / (len(frames) / FPS)
    print(f"\n{width}x{height} @ {budget_kbps:.0f} KB/s target -> actual {kbps:.1f} KB/s")
    print(f"  error from quantization : {100 * quant_sse / total:5.1f}%  ({coded_n} coded blocks)")
    print(f"  error from staleness    : {100 * stale_sse / total:5.1f}%  ({stale_n} skipped blocks)")
    print(f"  final skip_threshold    : {enc.skip_threshold:.0f} / 8000")

    # Best case for a given codebook size: fit on this content and score the
    # nearest match, ignoring bitrate entirely. Shows the ceiling a bigger
    # codebook could reach.
    sample = frame_to_blocks(frames[len(frames) // 2]).astype(np.float32)
    print("  codebook ceiling (best-case match, no bitrate limit):")
    for k in (256, 512, 1024, 4096):
        centers = _kmeans(sample, k, iters=6)
        d = (sample @ centers.T) * -2.0 + (centers * centers).sum(1)[None, :]
        best = d.min(axis=1) + (sample * sample).sum(1)
        mse = float(np.maximum(best, 0).sum() / sample.size)
        psnr = 10 * np.log10(255.0**2 / mse) if mse > 0 else float("inf")
        print(f"    {k:5d} entries -> {psnr:5.2f} dB")


for w, h in [(848, 480), (640, 360), (480, 272)]:
    analyse(w, h)
