#!/usr/bin/env python3
"""Where does encode time actually go?

Encoding must outrun playback or a live stream starves once the initial buffer
drains. This measures the real rate and attributes the cost, rather than
guessing at which numpy call is slow.
"""
from __future__ import annotations

import cProfile
import pstats
import sys
import time
from io import StringIO
from pathlib import Path

from codec import VQEncoder
from source import iter_frames, make_test_clip

FPS = 30


def main() -> None:
    """usage: profile_encode.py [clip] [width] [height]"""
    clip = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_testdata/testclip.mp4")
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 480
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 272
    if not clip.exists():
        make_test_clip(clip, seconds=10)

    frames = list(iter_frames(str(clip), width, height, FPS, max_frames=150))
    print(f"loaded {len(frames)} frames at {width}x{height}")

    enc = VQEncoder(width, height, FPS, target_bytes_per_frame=int(100 * 1024 / FPS))

    profiler = cProfile.Profile()
    profiler.enable()
    started = time.perf_counter()
    for frame in frames:
        enc.encode_frame(frame)
    elapsed = time.perf_counter() - started
    profiler.disable()

    rate = len(frames) / elapsed
    print(f"\nencoded {len(frames)} frames in {elapsed:.2f}s")
    print(f"rate: {rate:.1f} fps  ({rate / FPS:.2f}x realtime at {FPS}fps)")
    print(f"{'ABOVE' if rate > FPS else 'BELOW'} realtime -- "
          f"{'ok' if rate > FPS else 'a live stream would starve'}\n")

    stream = StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(18)
    # Trim the header noise; only the table matters here.
    for line in stream.getvalue().splitlines():
        if line.strip() and not line.startswith(("   Ordered", "   List reduced")):
            print(line)


if __name__ == "__main__":
    main()
