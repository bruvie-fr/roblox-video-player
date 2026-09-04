#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from audio import has_audio
from autotier import choose_tier
from codec import VQEncoder
from source import iter_frames, probe, resolve

FRAMES_PER_CHUNK = 15

KEYFRAME_EVERY_CHUNKS = 3
BUDGET_KBPS = 100.0

MIN_CHUNKS_TO_START = 6

DEFAULT_MAX_SECONDS = 300.0

@dataclass
class Job:
    generation: int
    source: str
    width: int
    height: int
    fps: int
    state: str = "fetching"
    message: str = ""
    title: str = ""
    chunks: list[bytes] = field(default_factory=list)
    estimated_chunks: int = 0
    cancelled: bool = False
    encode_started: float = 0.0
    encode_rate: float = 0.0

    auto_size: bool = True
    budget_pixels: int = 0
    has_audio: bool = False

    media_path: str = ""
    wav_bytes: bytes | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.budget_pixels:
            self.budget_pixels = self.width * self.height

    def snapshot(self) -> dict:
        with self.lock:
            ready = len(self.chunks)
            total_bytes = sum(len(c) for c in self.chunks)
        seconds = ready * FRAMES_PER_CHUNK / max(self.fps, 1)
        return {
            "generation": self.generation,
            "state": self.state,
            "message": self.message,
            "title": self.title,
            "source": self.source,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "framesPerChunk": FRAMES_PER_CHUNK,
            "keyframeEveryChunks": KEYFRAME_EVERY_CHUNKS,
            "minChunksToStart": MIN_CHUNKS_TO_START,
            "chunksReady": ready,
            "estimatedChunks": self.estimated_chunks,
            "complete": self.state in ("ready", "error"),
            "encodeRate": round(self.encode_rate, 2),
            "hasAudio": self.has_audio,

            "playable": (
                self.state == "ready"
                or (ready >= min(MIN_CHUNKS_TO_START, max(self.estimated_chunks, 1))
                    and (self.encode_rate == 0.0 or self.encode_rate >= 1.1))
            ),
            "seconds": round(seconds, 2),
            "kbps": round((total_bytes / 1024) / seconds, 1) if seconds > 0 else 0.0,
        }

STATE_LOCK = threading.Lock()
CURRENT: Job | None = None
NEXT_GENERATION = 1

DEFAULTS: dict = {
    "width": 848, "height": 480, "fps": 30,
    "max_seconds": DEFAULT_MAX_SECONDS, "auto_size": True,

    "cookies": os.environ.get("ENCODER_COOKIES") or None,
    "cookies_from_browser": os.environ.get("ENCODER_COOKIES_FROM_BROWSER") or None,
    "proxy": os.environ.get("ENCODER_PROXY") or None,
    "ytdlp_args": shlex.split(os.environ.get("ENCODER_YTDLP_ARGS", "")),
}

def _encode_worker(job: Job, max_seconds: float) -> None:
    try:
        path = resolve(
            job.source,
            cookies=DEFAULTS.get("cookies"),
            cookies_from_browser=DEFAULTS.get("cookies_from_browser"),
            proxy=DEFAULTS.get("proxy"),
            extra_args=DEFAULTS.get("ytdlp_args"),
        )
        if job.cancelled:
            return
        job.media_path = str(path)

        meta = probe(path)
        job.title = Path(path).name
        duration = min(meta.get("duration_seconds") or 0.0, max_seconds)
        if duration > 0:
            job.estimated_chunks = max(1, int(duration * job.fps / FRAMES_PER_CHUNK))

        if job.auto_size:
            job.state = "probing"
            job.message = "choosing resolution"
            job.width, job.height = choose_tier(
                path,
                meta.get("width"), meta.get("height"),
                meta.get("duration_seconds") or 0.0,
                job.fps,
                int(BUDGET_KBPS * 1024 / job.fps),
                max_pixels=job.budget_pixels,
                log=lambda m: print(m, flush=True),
            )
            if job.cancelled:
                return

        print(f"[encoder] gen {job.generation}: {job.title} "
              f"{meta.get('width')}x{meta.get('height')} {meta.get('codec')} "
              f"{meta.get('duration_seconds', 0):.1f}s -> {job.width}x{job.height}@{job.fps}", flush=True)

        job.state = "encoding"
        job.message = "encoding"
        job.encode_started = time.time()

        encoder = VQEncoder(
            job.width, job.height, job.fps,
            target_bytes_per_frame=int(BUDGET_KBPS * 1024 / job.fps),
            keyframe_interval=FRAMES_PER_CHUNK * KEYFRAME_EVERY_CHUNKS,
        )

        max_frames = int(max_seconds * job.fps) if max_seconds else None

        audio_ticks: list[list[tuple[int, int]]] = []
        audio_perc: list[tuple[int, int, int]] = []
        audio_onsets: list[int] = []

        if has_audio(path):
            job.has_audio = True
            print(f"[encoder] gen {job.generation}: audio track -> streamed as wav",
                  flush=True)
        else:
            print(f"[encoder] gen {job.generation}: no audio track", flush=True)
        job.message = "encoding"

        pending = []
        frame_index = 0
        for frame in iter_frames(path, job.width, job.height, job.fps, max_frames=max_frames):
            if job.cancelled:
                return
            pending.append(frame)
            frame_index += 1
            if len(pending) == FRAMES_PER_CHUNK:
                with job.lock:
                    index = len(job.chunks)
                start = frame_index - FRAMES_PER_CHUNK
                voices = [
                    audio_ticks[i] if i < len(audio_ticks) else []
                    for i in range(start, frame_index)
                ]
                perc = [
                    audio_perc[i] if i < len(audio_perc) else (0, 0, 0)
                    for i in range(start, frame_index)
                ]
                onsets = [
                    audio_onsets[i] if i < len(audio_onsets) else 0
                    for i in range(start, frame_index)
                ]
                encoded = encoder.encode_chunk(pending, index, voices, perc, onsets)
                pending = []
                with job.lock:
                    job.chunks.append(encoded)
                    produced = len(job.chunks) * FRAMES_PER_CHUNK / job.fps
                elapsed = time.time() - job.encode_started
                if elapsed > 0.5:
                    job.encode_rate = produced / elapsed

        with job.lock:
            count = len(job.chunks)
        if count == 0:

            job.state = "error"
            job.message = "no frames could be decoded (clip too short, or not a video)"
            print(f"[encoder] gen {job.generation}: {job.message}", flush=True)
            return

        job.state = "ready"
        job.message = f"{count} chunks"
        print(f"[encoder] gen {job.generation}: ready, {count} chunks "
              f"({count * FRAMES_PER_CHUNK / job.fps:.1f}s) "
              f"at {job.encode_rate:.2f}x realtime", flush=True)
        if job.encode_rate and job.encode_rate < 1.1:
            print(f"[encoder] WARNING: {job.width}x{job.height} encodes slower than it plays. "
                  f"Short clips still work (they finish, then loop), but a long one will "
                  f"stall partway. Drop the resolution to fix it.", flush=True)

    except Exception as exc:  # noqa: BLE001 -- the game gets told, not lied to
        if not job.cancelled:
            job.state = "error"
            job.message = str(exc)[:200]
            print(f"[encoder] gen {job.generation} failed: {exc}", flush=True)

def start_job(source: str, width: int, height: int, fps: int, max_seconds: float) -> Job:
    global CURRENT, NEXT_GENERATION
    with STATE_LOCK:
        if CURRENT is not None:
            CURRENT.cancelled = True
        job = Job(
            generation=NEXT_GENERATION, source=source,
            width=width, height=height, fps=fps,
            auto_size=DEFAULTS["auto_size"],
        )
        NEXT_GENERATION += 1
        CURRENT = job
    threading.Thread(target=_encode_worker, args=(job, max_seconds), daemon=True).start()
    return job

def stop_current() -> None:
    global CURRENT
    with STATE_LOCK:
        if CURRENT is None:
            return
        CURRENT.cancelled = True
        CURRENT = None
    print("[encoder] stopped -- idle", flush=True)

app = FastAPI(title="RVS5 encoder")

class OpenRequest(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None
    fps: int | None = None

@app.get("/health")
def health() -> dict:
    return {"ok": True}

@app.post("/stream/open")
def stream_open(req: OpenRequest) -> dict:
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    lowered = url.lower()
    if "://" in lowered and not lowered.startswith(("http://", "https://")):
        raise HTTPException(400, "only http:// and https:// URLs are accepted")

    job = start_job(
        url,
        req.width or DEFAULTS["width"],
        req.height or DEFAULTS["height"],
        req.fps or DEFAULTS["fps"],
        DEFAULTS["max_seconds"],
    )
    return job.snapshot()

@app.post("/stream/stop")
def stream_stop() -> dict:
    stop_current()
    return {"ok": True, "state": "idle"}

@app.get("/stream/info")
def stream_info() -> dict:
    job = CURRENT
    if job is None:
        return {"generation": 0, "state": "idle", "message": "nothing loaded",
                "chunksReady": 0, "complete": False, "playable": False}
    return job.snapshot()

@app.get("/stream/chunk/{index}")
def stream_chunk(index: int, g: int = 0) -> Response:
    job = CURRENT
    if job is None:
        raise HTTPException(503, "no stream loaded")

    if g and g != job.generation:
        raise HTTPException(409, f"generation {g} is stale, current is {job.generation}")
    with job.lock:
        if not 0 <= index < len(job.chunks):
            raise HTTPException(404, f"chunk {index} not ready ({len(job.chunks)} available)")
        payload = job.chunks[index]
    return Response(content=payload, media_type="application/octet-stream")

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None,
                    help="optional video path or URL to preload; omit to start idle")
    ap.add_argument("--width", type=int, default=848)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    ap.add_argument("--exact-size", action="store_true",
                    help="use --width/--height literally instead of matching the "
                         "source aspect within that pixel budget")
    ap.add_argument("--cookies", default=None,
                    help="Netscape cookies.txt, required by TikTok and other "
                         "sites with a bot check")
    ap.add_argument("--cookies-from-browser", default=None,
                    help="read cookies from a browser instead (chrome, edge, firefox); "
                         "the browser must be closed")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    cookies = args.cookies
    if cookies is None:
        for name in ("cookies.txt", "cookies.json"):
            candidate = Path(__file__).with_name(name)
            if candidate.exists():
                cookies = str(candidate)
                print(f"[encoder] using {candidate.name}", flush=True)
                break

    DEFAULTS.update(width=args.width, height=args.height, fps=args.fps,
                    max_seconds=args.max_seconds, auto_size=not args.exact_size,
                    cookies=cookies, cookies_from_browser=args.cookies_from_browser)

    if args.source:
        start_job(args.source, args.width, args.height, args.fps, args.max_seconds)
    else:
        print("[encoder] idle -- paste a link in-game, or pass --source to preload", flush=True)

    print(f"[encoder] serving on http://{args.host}:{args.port}", flush=True)
    print("[encoder]   POST /stream/open {\"url\": ...}   GET /stream/info   GET /stream/chunk/{n}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

AUDIO_RATE = 22050
AUDIO_MAX_SECONDS = 240.0

def _build_wav(path: str, seconds: float) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", path, "-vn",
         "-t", str(seconds),

         "-af", "highpass=f=55",
         "-ac", "1",
         "-ar", str(AUDIO_RATE),
         "-c:a", "pcm_s16le",
         "-f", "wav", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else "ffmpeg produced no audio")

    return proc.stdout

@app.get("/stream/audio.wav")
def stream_audio(g: int = 0) -> Response:
    job = CURRENT
    if job is None or not job.media_path:
        raise HTTPException(status_code=404, detail="no stream open")
    if g and g != job.generation:
        raise HTTPException(status_code=409, detail="stale generation")

    with job.lock:
        cached = job.wav_bytes
    if cached is None:
        try:
            cached = _build_wav(job.media_path, AUDIO_MAX_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- ffmpeg's opinion, forwarded
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        with job.lock:
            job.wav_bytes = cached
        print(f"[encoder] gen {job.generation}: audio.wav ready, "
              f"{len(cached) / 1024:.0f} KB at {AUDIO_RATE} Hz mono", flush=True)

    return Response(content=cached, media_type="audio/wav")

if __name__ == "__main__":
    main()
