#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 22050

FFT_SIZE = 2048
MAX_VOICES = 16
MIN_HZ = 60.0
MAX_HZ = 8000.0

REL_FLOOR = 0.10
LOUDNESS_EXPONENT = 0.55

BASS_LIFT = 3.0
BASS_LIFT_BANDS = 6

MAX_SOUNDING = 4

PERCUSSION_FLOOR = 0.42
PERCUSSION_MIN_GAP = 0.14

NOTE_HOLD_FACTOR = 0.45

def extract_pcm(path: str, start_seconds: float = 0.0, max_seconds: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_seconds:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += ["-i", str(Path(path).resolve())]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm

def has_audio(path: str) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(Path(path).resolve())],
        capture_output=True, text=True,
    )
    return "audio" in (result.stdout or "")

def analyse(
    pcm: np.ndarray,
    fps: int,
    frame_count: int,
    max_voices: int = MAX_VOICES,
) -> tuple[list[list[tuple[int, int]]], list[tuple[int, int, int]]]:
    if pcm.size == 0:
        return [[] for _ in range(frame_count)], [(0, 0, 0) for _ in range(frame_count)]

    hop = max(1, SAMPLE_RATE // max(fps, 1))
    window = np.hanning(FFT_SIZE).astype(np.float32)
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
    usable = (freqs >= MIN_HZ) & (freqs <= MAX_HZ)
    usable_freqs = freqs[usable]

    peak_ref = float(np.abs(pcm).max()) or 1.0

    band_masks = [
        (freqs >= 40) & (freqs < 250),
        (freqs >= 250) & (freqs < 2000),
        (freqs >= 2000) & (freqs < 10000),
    ]
    previous_bands = np.zeros(3)
    raw_flux: list[np.ndarray] = []

    out: list[list[tuple[int, int]]] = []
    for index in range(frame_count):
        start = index * hop
        block = pcm[start:start + FFT_SIZE]
        if block.size < FFT_SIZE:
            block = np.pad(block, (0, FFT_SIZE - block.size))

        full = np.abs(np.fft.rfft(block * window))

        bands = np.array([full[m].sum() for m in band_masks])
        raw_flux.append(np.maximum(0.0, bands - previous_bands))
        previous_bands = bands

        spectrum = full[usable]
        if spectrum.size == 0 or spectrum.max() <= 0:

            out.append([(0, 0.0)] * max_voices)
            continue

        interior = spectrum[1:-1]
        peaks = np.flatnonzero(
            (interior > spectrum[:-2]) & (interior >= spectrum[2:])
        ) + 1
        if peaks.size == 0:

            out.append([(0, 0.0)] * max_voices)
            continue

        block_gain = float(np.abs(block).max()) / peak_ref
        bin_width = float(usable_freqs[1] - usable_freqs[0])

        voices: list[tuple[int, int]] = []
        for band in range(max_voices):
            lo_hz = MIN_HZ * (MAX_HZ / MIN_HZ) ** (band / max_voices)
            hi_hz = MIN_HZ * (MAX_HZ / MIN_HZ) ** ((band + 1) / max_voices)

            in_band = peaks[(usable_freqs[peaks] >= lo_hz) & (usable_freqs[peaks] < hi_hz)]
            if in_band.size == 0:
                voices.append((0, 0))
                continue

            bin_index = int(in_band[np.argmax(spectrum[in_band])])
            magnitude = float(spectrum[bin_index])

            a, b, c = spectrum[bin_index - 1], magnitude, spectrum[bin_index + 1]
            denom = a - 2 * b + c
            shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
            hz = float(usable_freqs[bin_index] + shift * bin_width)
            hz = min(max(hz, lo_hz), hi_hz)

            voices.append((int(round(hz)), magnitude * block_gain))

        out.append(voices)

    if out:
        magnitudes = np.array([[v[1] for v in frame] for frame in out])

        reference = float(np.percentile(magnitudes, 96)) or 1.0

        levels = np.clip((magnitudes / reference) ** LOUDNESS_EXPONENT, 0.0, 1.0)

        bands = np.arange(levels.shape[1])
        tilt = 1.0 + (BASS_LIFT - 1.0) * np.clip(1.0 - bands / BASS_LIFT_BANDS, 0.0, 1.0)
        levels = np.clip(levels * tilt[None, :], 0.0, 1.0)

        if levels.shape[1] > MAX_SOUNDING:

            held = np.zeros(levels.shape[1], dtype=bool)
            for frame in range(levels.shape[0]):
                row = levels[frame]
                order = np.argsort(row)[::-1]
                cutoff = row[order[MAX_SOUNDING - 1]]
                keep_threshold = cutoff * NOTE_HOLD_FACTOR

                chosen = np.zeros(levels.shape[1], dtype=bool)

                for band in np.flatnonzero(held):
                    if chosen.sum() < MAX_SOUNDING and row[band] >= keep_threshold and row[band] > 0:
                        chosen[band] = True
                for band in order:
                    if chosen.sum() >= MAX_SOUNDING:
                        break
                    if row[band] > 0:
                        chosen[band] = True

                levels[frame] = np.where(chosen, row, 0.0)
                held = chosen

        levels[levels < REL_FLOOR] = 0.0
        out = [
            [
                (frame[b][0] if levels[i, b] > 0 else 0, int(round(levels[i, b] * 255)))
                for b in range(len(frame))
            ]
            for i, frame in enumerate(out)
        ]

    flux = np.asarray(raw_flux) if raw_flux else np.zeros((frame_count, 3))
    reference = np.percentile(flux, 97, axis=0)
    reference[reference <= 0] = 1.0
    scaled = np.clip(flux / reference, 0.0, 1.0)
    scaled[scaled < PERCUSSION_FLOOR] = 0.0

    gap = max(1, int(round(PERCUSSION_MIN_GAP * fps)))
    for band in range(scaled.shape[1]):
        last = -gap
        for frame in range(scaled.shape[0]):
            if scaled[frame, band] > 0:
                if frame - last < gap:
                    scaled[frame, band] = 0.0
                else:
                    last = frame

    percussion = [
        (int(row[0] * 255), int(row[1] * 255), int(row[2] * 255))
        for row in scaled
    ]
    return out, percussion

def analyse_file(
    path: str, fps: int, frame_count: int, start_seconds: float = 0.0
) -> tuple[list[list[tuple[int, int]]], list[tuple[int, int, int]]]:
    seconds = frame_count / max(fps, 1) + FFT_SIZE / SAMPLE_RATE
    pcm = extract_pcm(path, start_seconds, seconds)
    return analyse(pcm, fps, frame_count)

def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0, rate

def _write_wav(path: Path, samples: np.ndarray, rate: int) -> Path:
    import wave

    pcm = (np.clip(samples, -1.0, 1.0) * 32767 * 0.9).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return path

SUPERSAW_OFFSETS = (-0.11002313, -0.06288439, -0.01952356,
                    0.01991221, 0.06216538, 0.10745242)
SUPERSAW_SIDE_GAIN = 0.55

def _partials(shape: str, n_harmonics: int, duty: float) -> tuple[np.ndarray, np.ndarray]:
    n = np.arange(1, n_harmonics + 1, dtype=np.float64)
    if shape == "sine":
        amp = np.zeros_like(n)
        amp[0] = 1.0
    elif shape == "saw":
        amp = 1.0 / n
    elif shape == "square":
        amp = np.where(n % 2 == 1, 1.0 / n, 0.0)
    elif shape == "triangle":
        amp = np.where(n % 2 == 1, 1.0 / (n ** 2), 0.0)
    elif shape == "pulse":
        amp = np.abs(np.sin(np.pi * n * duty)) / n
    else:
        raise ValueError(f"unknown shape: {shape}")

    phase = np.where((shape == "triangle") & (((n - 1) // 2) % 2 == 1), np.pi, 0.0)
    return amp, phase

def make_tone_wav(
    path: Path,
    hz: float = 440.0,
    seconds: float = 1.0,
    rate: int = 44100,
    shape: str = "sine",
    band_limit: float | None = None,
    duty: float = 0.25,
    detune_cents: float = 16.0,
) -> Path:
    if shape == "noise":
        return make_noise_wav(path, seconds=seconds, rate=rate)

    nyquist = rate / 2.0
    limit = min(band_limit or nyquist * 0.9, nyquist * 0.98)

    if shape == "supersaw":

        samples = int(round(rate * seconds))
        t = np.arange(samples, dtype=np.float64) / rate
        data = np.zeros(samples)

        span = max(abs(o) for o in SUPERSAW_OFFSETS)
        spread_hz = hz * (2 ** (detune_cents / 1200.0) - 1.0)
        freqs = [hz] + [round(hz + o / span * spread_hz) for o in SUPERSAW_OFFSETS]
        gains = [1.0] + [SUPERSAW_SIDE_GAIN] * len(SUPERSAW_OFFSETS)
        rng = np.random.default_rng(20260816)
        for f, g in zip(freqs, gains):
            f = max(1.0, float(round(f)))
            n_harm = max(1, int(limit // f))
            amp, _ = _partials("saw", n_harm, duty)

            offset = rng.random() * 2 * np.pi
            k = np.arange(1, n_harm + 1)[:, None]
            data += g * (amp[:, None] * np.sin(2 * np.pi * f * k * t + offset)).sum(0)
    else:
        cycles = max(1, round(hz * seconds))
        samples = int(round(cycles * rate / hz))
        t = np.arange(samples, dtype=np.float64) / rate
        n_harm = max(1, int(limit // hz))
        amp, phase = _partials(shape, n_harm, duty)
        k = np.arange(1, n_harm + 1)[:, None]
        data = (amp[:, None] * np.sin(2 * np.pi * hz * k * t + phase[:, None])).sum(0)

    peak = np.abs(data).max()
    if peak > 0:
        data = data / peak

    data -= data.mean()
    return _write_wav(path, data, rate)

def make_noise_wav(path: Path, seconds: float = 2.0, rate: int = 44100, pink: bool = True) -> Path:
    samples = int(seconds * rate)
    bins = samples // 2 + 1
    freqs = np.fft.rfftfreq(samples, 1.0 / rate)

    magnitude = np.ones(bins)
    if pink:
        magnitude[1:] = 1.0 / np.sqrt(freqs[1:])
    magnitude[0] = 0.0

    rng = np.random.default_rng(0)
    spectrum = magnitude * np.exp(2j * np.pi * rng.random(bins))
    data = np.fft.irfft(spectrum, n=samples)
    data /= np.abs(data).max() or 1.0
    return _write_wav(path, data, rate)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tone":
        target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_testdata/tone440.wav")
        make_tone_wav(target)
        print(f"wrote {target} -- upload this to Roblox and put its asset id in Config.luau")
        raise SystemExit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "waves":
        out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_testdata/waves")
        made = []
        for shape in ("sine", "saw", "square", "triangle"):
            made.append(make_tone_wav(out_dir / f"{shape}440.wav", shape=shape))
        made.append(make_noise_wav(out_dir / "noise_pink.wav"))
        for f in made:
            print(f"  {f}  ({f.stat().st_size / 1024:.0f} KB)")
        raise SystemExit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "banks":

        out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_testdata/banks")
        specs = [
            ("lead_supersaw330", dict(hz=330, shape="supersaw", band_limit=8000)),
            ("bass_saw110", dict(hz=110, shape="saw", band_limit=4400)),
            ("pad_pulse880", dict(hz=880, shape="pulse", band_limit=10600, duty=0.25)),
        ]
        for name, kw in specs:
            path = make_tone_wav(out_dir / f"{name}.wav", seconds=1.0, **kw)
            data, rate = _read_wav(path)
            dc = float(np.mean(data))
            seam = float(abs(data[0] - data[-1]))
            print(f"  {path.name:22s} {path.stat().st_size / 1024:5.0f} KB  "
                  f"{len(data) / rate:.3f}s  DC {dc:+.5f}  seam {seam:.4f}")
        print("\nUpload these three to Roblox, then put the asset ids in "
              "Config.luau as LEAD_ASSET_ID / BASS_ASSET_ID / TONE_ASSET_ID.")
        raise SystemExit(0)

    clip = sys.argv[1] if len(sys.argv) > 1 else "_testdata/user.mp4"
    fps, count = 30, 150
    print(f"{clip}: audio track present = {has_audio(clip)}")
    ticks, perc = analyse_file(clip, fps, count)
    used = [len(t) for t in ticks]
    print(f"{len(ticks)} ticks, voices per tick: min {min(used)} max {max(used)} "
          f"avg {sum(used)/len(used):.1f}")
    print(f"wire cost: {(sum(used) * 3 + len(perc) * 3) / (count / fps) / 1024:.2f} KB/s\n")

    hits = [sum(1 for p in perc if p[b] > 0) for b in range(3)]
    print(f"percussion onsets over {count/fps:.1f}s -- "
          f"kick {hits[0]}, snare {hits[1]}, hat {hits[2]}")
    print(f"  (~{hits[0]/(count/fps)*60:.0f}, {hits[1]/(count/fps)*60:.0f}, "
          f"{hits[2]/(count/fps)*60:.0f} per minute)\n")

    print("  frame | kick snare  hat | top tonal voices")
    for i in range(0, min(40, len(ticks))):
        k, s, h = perc[i]
        if k or s or h:
            shown = ", ".join(f"{hz}Hz" for hz, _ in ticks[i][:3])
            print(f"  {i:5d} | {k:4d} {s:5d} {h:4d} | {shown}")
