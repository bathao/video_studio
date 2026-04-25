# Pingpong Studio

Local web app to edit fixed-tripod table-tennis footage: mark points with
keyboard shortcuts, capture highlights, and render a final video that
contains an intro, a slow-motion highlight reel, and the main match with
a burned-in scoreboard. Hardware-accelerated via NVIDIA NVENC.

## Requirements

- Windows 10/11
- Python 3.10+
- FFmpeg with NVENC (`h264_nvenc`) on PATH
- An NVIDIA GPU (RTX 5060 Ti or similar)

Verify FFmpeg sees your GPU:

```
ffmpeg -hide_banner -encoders | findstr nvenc
```

## Install

Use a virtual environment so the project's Python deps don't collide
with anything else on the machine:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

(One-time. After this, `run.bat` auto-detects `venv\` and uses it.)

## Run

```
run.bat
```

This launches the server at `http://127.0.0.1:8765` and opens it in your
browser.

## Workflow

1. Drop video files into `videos/`.
2. Open the app, fill in tournament + player names, pick the source video.
3. Watch the match. Use shortcuts to keep your hands off the mouse:

   | Key | Action |
   | --- | --- |
   | `Space` | Play / pause |
   | `←` / `→` | Seek 5s (hold `Shift` for 1s) |
   | `A` | Player 1 scores a point |
   | `D` | Player 2 scores a point |
   | `Ctrl+Z` | Undo last action |
   | `H` | Toggle highlight start/end |
   | `S` | Toggle slow-mo on most-recent highlight |
   | `T` / `Y` | Mark trim start / end |

   The referee logic is automatic: 11 points + 2-point lead wins a set,
   then the point score resets to 0-0 and the set counter increments.

4. Save the project (right side `Save Project` button). It writes
   `projects/<name>.json` so you can resume later.
5. Hit `Render`. The progress bar shows each stage (intro → highlight →
   main → concat). Output is written to `output/<name>.mp4`.

## Folder layout

```
video_studio/
├── videos/            # source MP4s go here
├── projects/          # saved project JSONs
├── output/            # final rendered MP4s
├── temp/              # per-job intermediate files (safe to delete)
├── assets/            # for fonts/logos if you customise the .ass overlay
├── config.json        # encoder / paths / port
├── backend/           # FastAPI + ffmpeg pipeline
└── frontend/          # HTML + JS + CSS
```

## Render pipeline

```
intro.mp4   ── 3s title card built with lavfi color + drawtext
highlight.mp4 ── per-clip ffmpeg with input seeking + slow-mo, then concat
main.mp4    ── one ffmpeg with one `-i` per kept segment (input seeking)
              + concat + scoreboard.ass burn, all NVDEC → CPU → NVENC
final.mp4   ── concat-demuxer of the three (no re-encode)
```

All stages share the source video's resolution, fps, pixel format, sample
rate, and codec, so the final concat is a fast stream copy.

### Why input seeking matters for big files

A typical match recording at 2K@60fps is 10–15 GB. The pipeline uses
**input-side `-ss` / `-t`** (placed BEFORE every `-i`), so ffmpeg seeks
straight to each kept range with NVDEC and never demuxes the trimmed-out
gaps. Without this, a single `filter_complex` on the whole file would walk
the entire 15 GB even if you only kept a few minutes.

The scoreboard is generated from `score_events` as an `.ass` file. Source
timestamps are remapped into trimmed-output time before the file is
written. Events that fall inside a trimmed-out gap are snapped forward to
the start of the next kept segment so the score change still appears.

## Tuning

Edit `config.json`:

- `encoder` — `h264_nvenc`, `hevc_nvenc`, or `av1_nvenc`
- `preset` — NVENC presets `p1`..`p7` (p1 = fastest, p7 = best quality)
- `cq` — constant quality (lower = better, default 21)
- `use_hwaccel` — set false to disable CUDA decode if your driver chokes

## Project docs

- [docs/PROGRESS.md](docs/PROGRESS.md) — current build status, module by module
- [docs/TODO.md](docs/TODO.md) — prioritised next tasks
- [docs/ROADMAP.md](docs/ROADMAP.md) — version plan toward production

## Troubleshooting

- **`ffmpeg` not found** — add the FFmpeg `bin` directory to PATH or set
  `ffmpeg_path` / `ffprobe_path` in `config.json`.
- **`No NVENC capable devices found`** — driver too old, or another app is
  using the encoder. Update GPU driver.
- **Source video has no audio** — handled automatically; the highlight
  and main stages get a silent track so the final concat works.
- **Trim removes everything** — render aborts with an explicit error.
