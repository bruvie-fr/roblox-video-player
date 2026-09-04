# Roblox Video Player

Streams video **and audio** from an external source into a Roblox experience in
real time. Video is rendered in-engine to an `EditableImage` through a
purpose-built codec; audio is rebuilt from the real waveform by a 384-voice FFT
oscillator bank, because Roblox has no raw-sample audio API.

Typical stream: **768x432 at 30 fps, ~100 KB/s**, with sound.

---

## How it works

A small Python service downloads the video, encodes it into a compact block
format, and serves it over HTTP. The Roblox server polls that service and fans
the bytes out to every player, so bandwidth is the same whether one person is
watching or fifty. The client decodes each frame and draws it.

```
  Python encoder  --HTTP-->  Roblox server  --RemoteEvent-->  Roblox client
   yt-dlp/ffmpeg              poll + fanout                    decode + draw
```

## Two ways of drawing it

The renderer picks a backend at runtime, per player:

- **`EditableImage`** — one image, pixels written straight into it. Sharp and
  cheap: around 0.1–3 ms to decode a frame. This is the good path.
- **Frame grid** — a grid of `Frame` objects, one per pixel block, capped at
  192x108 (20,736 Frames). Coarse and much heavier, but it works.

The fallback exists because enabling **Allow Mesh / Image APIs** requires a 13+
ID-verified account, and plenty of places can't. Without it, those players would
otherwise see nothing at all.

Detection deliberately *writes* a pixel rather than just creating the image:
`CreateEditableImage` succeeds even when the API is disabled, and only the write
fails. Checking creation alone gives you a renderer that looks fine and then
throws once per frame forever.

The stats overlay shows which one is live — `[editableimage]` or `[framegrid]`.

## Requirements

- `ffmpeg` and `ffprobe` on PATH
- Python 3.11+ : `pip install numpy pillow fastapi uvicorn pydantic yt-dlp`
- In Studio, **Game Settings -> Security**: enable **Allow HTTP Requests**, and
  **Allow Mesh / Image APIs** for sharp rendering (needs a 13+ ID-verified
  account; without it the player falls back to a coarser mode automatically)

## Quick start

**1. Run the encoder**

```bash
cd encoder
python server.py --port 8090
```

**2. Sync the code into Studio**

```bash
rojo serve
```

Then **Plugins -> Rojo -> Connect** in Studio and press Play. Paste a link into
the bar at the bottom of the screen.

That's it for local testing. Studio can reach `127.0.0.1`; a published game
cannot.

## Publishing to a real game

A published Roblox server can't see your machine, so the encoder needs a public
address:

```bash
cloudflared tunnel --url http://localhost:8090
```

Put the URL it prints into `Config.ENCODER_URL`, or pass it to `bruvo()` below.

### Distributing as one module

```bash
rojo build bundle.project.json -o MainModule.rbxm
```

Insert that into a place, right-click the `MainModule`, **Save to Roblox** as a
public Model, then from a server Script in any game:

```lua
require(ASSET_ID).bruvo("USERNAME", "https://your-tunnel.trycloudflare.com")
```

That installs everything and gives `USERNAME` control of playback; everyone else
watches. Both arguments are optional — omit the username and anyone can control
it.

## Notes and limits

- **1080p60 is not possible in Roblox.** `EditableImage` caps at 1024x1024 and
  only one may update per frame. This targets 768x432 at 30 fps.
- **Audio tops out around 8.3 kHz** and is capped at 4 minutes per clip. The
  engine only mixes about 400 simultaneous voices, which sets that ceiling.
- **YouTube blocks datacenter IPs.** Running the encoder from a home connection
  works; running it on a VPS usually hits a bot check. Direct `.mp4` links and
  local files work anywhere.
- Anyone can paste a link unless you set an operator, which is what the username
  argument to `bruvo()` is for.

## Layout

```
encoder/    Python: download, encode, serve
src/        Luau: server fan-out, client decode and render
assets/     source waveforms for the audio oscillators
```

## Licence

MIT with the [Commons Clause](https://commonsclause.com/): use it, modify it,
ship it inside your own game — including one that makes money. You may not sell
the software itself. Keep the copyright notice. See [LICENSE](LICENSE).
