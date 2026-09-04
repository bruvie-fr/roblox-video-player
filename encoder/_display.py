"""Compare tiers at DISPLAY resolution, which is the only fair comparison.

PSNR at native size flatters low resolutions: a 480x272 encode is scored against
a 480x272 reference it was never asked to beat. What the player actually sees is
that frame bilinearly upscaled to fill the screen, so every candidate is scored
against the same full-size source after the same upscale the ImageLabel does.
"""
import sys

import numpy as np
from PIL import Image

from codec import RefDecoder, VQEncoder
from source import iter_frames

CLIP = sys.argv[1] if len(sys.argv) > 1 else "_testdata/user.mp4"
FPS = 30
SECONDS = 5
DISPLAY = (1280, 720)   # what it is stretched to on screen
BUDGET_KBPS = 100.0

TIERS = [(384, 216), (480, 272), (568, 320), (640, 360), (768, 432), (848, 480)]


def upscale(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(Image.fromarray(frame).resize(size, Image.BILINEAR))


reference = [
    upscale(f, DISPLAY)
    for f in iter_frames(CLIP, DISPLAY[0], DISPLAY[1], FPS, max_frames=FPS * SECONDS)
]
print(f"{CLIP}: {len(reference)} frames, scored at {DISPLAY[0]}x{DISPLAY[1]}\n")
print(f"{'tier':>12} {'KB/s':>8} {'display PSNR':>13}")
print("-" * 36)

results = []
for width, height in TIERS:
    frames = list(iter_frames(CLIP, width, height, FPS, max_frames=FPS * SECONDS))
    enc = VQEncoder(width, height, FPS, target_bytes_per_frame=int(BUDGET_KBPS * 1024 / FPS))
    dec = RefDecoder(width, height)
    chunk = enc.encode_chunk(frames, 0)
    decoded = dec.decode_chunk(chunk)

    total, count = 0.0, 0
    for out, ref in zip(decoded, reference):
        diff = upscale(out, DISPLAY).astype(np.float64) - ref.astype(np.float64)
        total += float((diff * diff).sum())
        count += diff.size
    mse = total / max(count, 1)
    psnr = 10 * np.log10(255.0**2 / mse) if mse > 0 else float("inf")
    kbps = (len(chunk) / 1024) / (len(frames) / FPS)
    results.append((psnr, kbps, width, height))
    print(f"{width:>5}x{height:<4} {kbps:>8.1f} {psnr:>12.2f} dB")

best = max(results)
print(f"\nbest on screen: {best[2]}x{best[3]} at {best[1]:.1f} KB/s ({best[0]:.2f} dB)")
