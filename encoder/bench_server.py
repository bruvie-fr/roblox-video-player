#!/usr/bin/env python3
"""Benchmark/probe server for the Roblox video streamer.

Exists to answer one undocumented question before any real code is written:
does HttpService preserve arbitrary bytes in a response body, or does the
transport mangle anything that isn't valid UTF-8?

Every payload here is deterministic, so the Luau side can verify it
byte-for-byte without shipping a checksum it might implement differently.

    python encoder/bench_server.py            # serves on 127.0.0.1:8080
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Stdlib only, on purpose: the probe must not be able to fail for reasons
# unrelated to what it is probing.

HOST = "127.0.0.1"
PORT = 8080

# byte[i] = (i * 167 + 13) % 256. 167 is coprime with 256, so a 256-byte run
# hits all 256 values exactly once -- the Luau side recomputes this and
# compares directly, no checksum needed.
STRIDE = 167
OFFSET = 13
MAX_PATTERN = 4 * 1024 * 1024


def pattern(n: int) -> bytes:
    return bytes(((i * STRIDE + OFFSET) & 0xFF) for i in range(n))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (stdlib-mandated name)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/health":
            self._send(b'{"ok":true}', "application/json")
            return

        # All 256 byte values, each exactly once. The critical test: if any
        # byte comes back altered, the whole binary-transport design is dead
        # and everything has to move to base64.
        if path == "/bytes256":
            self._send(pattern(256))
            return

        # Same generator at arbitrary length, for measuring throughput and
        # confirming large bodies survive intact.
        if path.startswith("/pattern/"):
            try:
                n = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._fail(400, "pattern size must be an integer")
                return
            if not 0 < n <= MAX_PATTERN:
                self._fail(400, f"pattern size must be in 1..{MAX_PATTERN}")
                return
            self._send(pattern(n))
            return

        self._fail(404, "not found")

    def log_message(self, fmt: str, *args) -> None:
        # Default handler logs every request to stderr; too noisy under the
        # rapid polling the throughput probe does.
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[bench] serving on http://{args.host}:{args.port}", flush=True)
    print("[bench]   /health  /bytes256  /pattern/<n>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bench] shutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
