#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

def is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise SystemExit(f"{tool} is not installed or not on PATH")

def probe(video_path: str) -> dict:
    require("ffprobe")
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(Path(video_path).resolve())],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})

    fps = 0.0
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        pass

    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "fps": fps,
        "duration_seconds": float(fmt.get("duration") or stream.get("duration") or 0),
    }

DIRECT_EXTS = {".mp4", ".gif", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".webp", ".apng", ".png", ".jpg", ".jpeg"}
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

def _clear(out_dir: Path) -> None:
    for stale in out_dir.glob("video.*"):
        try:
            stale.unlink()
        except OSError:
            pass

def download_direct(url: str, out_dir: Path) -> str:
    suffix = Path(urlparse(url).path).suffix.lower() or ".mp4"
    dest = out_dir / f"video{suffix}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (roblox-video-encoder)"})

    total = 0
    with urlopen(request, timeout=30) as response, dest.open("wb") as handle:
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"download exceeded {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB cap")
            handle.write(block)

    if total == 0:
        raise RuntimeError("downloaded 0 bytes")
    return str(dest)

BOT_WALL_MARKERS = (
    "unexpected response from webpage",
    "captcha",
    "sign in to confirm",
    "login required",
    "this video is unavailable",
)

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

def netscape_from_json(json_path: Path, out_path: Path | None = None) -> Path:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise RuntimeError(f"{json_path.name} is not a JSON list of cookies")

    out_path = out_path or json_path.with_suffix(".txt")
    lines = [
        "# Netscape HTTP Cookie File",
        f"# converted from {json_path.name}",
    ]
    written = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        domain = entry.get("domain")
        if not name or not domain:
            continue

        domain = _MD_LINK.sub(r"\1", str(domain)).strip()
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(entry.get("path") or "/")
        secure = "TRUE" if entry.get("secure") else "FALSE"

        expiry = int(float(entry.get("expirationDate") or 0))
        value = str(entry.get("value") or "")

        prefix = "#HttpOnly_" if entry.get("httpOnly") else ""
        lines.append(f"{prefix}{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
        written += 1

    if written == 0:
        raise RuntimeError(f"no usable cookies found in {json_path.name}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    return out_path

def prepare_cookies(path: str | Path) -> Path:
    source_path = Path(path).expanduser()
    if not source_path.exists():
        raise RuntimeError(f"cookies file not found: {source_path}")
    if source_path.suffix.lower() == ".json":
        converted = netscape_from_json(source_path)
        print(f"[source] converted {source_path.name} -> {converted.name}", file=sys.stderr)
        return converted
    return source_path

def _cookie_args(cookies: str | None, cookies_from_browser: str | None) -> list[str]:
    if cookies:
        return ["--cookies", str(prepare_cookies(cookies))]
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser]
    return []

def download_ytdlp(
    url: str,
    out_dir: Path,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    extra_args: list[str] | None = None,
) -> str:
    require("yt-dlp")
    template = str(out_dir / "video.%(ext)s")
    cmd = ["yt-dlp", "-N", "8",

           "-f", "bv*[height<=1280][width<=1280]+ba/b[height<=1280][width<=1280]"
                 "/bv*+ba/b",
           "--merge-output-format", "mp4", "--no-playlist", "--no-part"]
    cmd += _cookie_args(cookies, cookies_from_browser)
    if proxy:
        cmd += ["--proxy", proxy]
    if extra_args:
        cmd += list(extra_args)
    cmd += ["-o", template, "--", url]

    result = subprocess.run(cmd, capture_output=True, text=True)
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".gif"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return str(candidate)

    stderr = (result.stderr or "").strip()
    if any(marker in stderr.lower() for marker in BOT_WALL_MARKERS) and not (
        cookies or cookies_from_browser
    ):

        raise RuntimeError(
            "this site blocked the download (bot check). It needs sign-in cookies: "
            "export cookies.txt and restart the encoder with --cookies cookies.txt"
        )

    tail = stderr.splitlines()
    raise RuntimeError(tail[-1] if tail else "yt-dlp produced no video file")

def resolve(
    source: str,
    download_dir: Path | None = None,
    cookies: str | None = None,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    extra_args: list[str] | None = None,
) -> str:
    if not is_url(source):
        p = Path(source).expanduser().resolve()
        if not p.exists():
            raise RuntimeError(f"File not found: {p}")
        return str(p)

    out_dir = download_dir or Path("./_downloads")
    out_dir.mkdir(parents=True, exist_ok=True)
    _clear(out_dir)

    suffix = Path(urlparse(source).path).suffix.lower()
    if suffix in DIRECT_EXTS:
        try:
            return download_direct(source, out_dir)
        except Exception as exc:
            print(f"[source] direct download failed ({exc}), trying yt-dlp", file=sys.stderr)
            _clear(out_dir)
    return download_ytdlp(source, out_dir, cookies, cookies_from_browser, proxy, extra_args)

def fit_dimensions(
    src_width: int | None,
    src_height: int | None,
    budget_pixels: int,
    max_dim: int = 1024,
) -> tuple[int, int]:
    if not src_width or not src_height:

        src_width, src_height = 16, 9

    aspect = src_width / src_height
    height = (budget_pixels / aspect) ** 0.5
    width = height * aspect

    scale = min(1.0, max_dim / max(width, height))
    width, height = width * scale, height * scale

    def to_block(value: float) -> int:
        return max(16, min(max_dim, int(round(value / 4.0)) * 4))

    return to_block(width), to_block(height)

def scale_filter(width: int, height: int) -> str:
    return (
        f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

def iter_frames(
    video_path: str,
    width: int,
    height: int,
    fps: int,
    start_seconds: float | None = None,
    max_frames: int | None = None,
) -> Iterator[np.ndarray]:
    require("ffmpeg")
    if width % 4 or height % 4:
        raise ValueError(f"dimensions must be multiples of 4: got {width}x{height}")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_seconds:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", f"fps={fps},{scale_filter(width, height)}",
        "-pix_fmt", "rgb24", "-f", "rawvideo",
    ]
    if max_frames:
        cmd += ["-frames:v", str(max_frames)]
    cmd += ["-"]

    frame_bytes = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 4)
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
        if proc.returncode not in (0, None) and proc.stderr:
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            if err:
                print(f"[source] ffmpeg: {err}", file=sys.stderr)

def make_test_clip(path: Path, seconds: int = 10, fps: int = 30) -> Path:
    require("ffmpeg")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}:duration={seconds}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", str(path)],
        check=True,
    )
    return path
