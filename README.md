# Table Tennis Studio

Local web app to edit fixed-tripod table-tennis footage: mark points with
keyboard shortcuts, capture highlights, and render a final video that
contains an intro and the main match with a burned-in scoreboard plus
50%-speed slow-mo replays of every highlight. Hardware-accelerated via
NVIDIA NVENC.

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
2. Open the app. In the **Setup** panel choose **Single** or **Double**:
   - **Single**: fill Player 1 (left, key `A`) and Player 2 (right, key `D`).
   - **Double**: also fill Player 3 (team 1 partner) and Player 4
     (team 2 partner). The scoreboard combines names per team using
     the last two words of each partner (e.g. "Văn An + Hoàng Nam").
3. Watch the match. Use shortcuts to keep your hands off the mouse:

   | Key | Action |
   | --- | --- |
   | `Space` | Play / pause |
   | `←` / `→` | Seek 5s (hold `Shift` for 1s) |
   | `A` | Player 1 (team 1 in doubles) scores a point |
   | `D` | Player 2 (team 2 in doubles) scores a point |
   | `Ctrl+Z` | Undo last action |
   | `H` | Toggle highlight start/end |
   | `T` / `Y` | Mark trim start / end |

   The referee logic is automatic: 11 points + 2-point lead wins a set,
   then the point score resets to 0-0 and the set counter increments.

   Once a source video is loaded, a **scoreboard preview** overlays the
   player area in real-time — same `.ass` file the renderer burns in,
   composited in the browser via libass-WASM. Edit tournament / player
   names or score points and the overlay updates instantly so you can
   sanity-check the burned-in look before rendering.

4. Save the project (right side `Save Project` button). It writes
   `projects/<name>.json` so you can resume later.
5. Hit `Render`. The progress bar shows each stage (intro → main →
   outro → concat). The `Cancel render` button below the bar terminates
   the in-flight ffmpeg process if you spot a problem; the partial
   intermediates stay in `temp/<job>/` for inspection. Output is written
   to `output/<name>.mp4`.

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
└── frontend/          # HTML + JS + CSS + vendor/jassub (libass-WASM)
```

## Render pipeline

```
intro.mp4          ── cinematic title card (4 avatars in doubles, 2 in
                      singles); falls back to a 3 s libass-only card
                      when player photos are missing
main.mp4           ── one ffmpeg, one `-i` per kept slice AND one `-i`
                      per slow-mo replay (replays use setpts*2 +
                      atempo=0.5). Each replay is bracketed by an
                      asymmetric branded pair: long IN clip (default
                      2 s, full reveal with channel name + REPLAY
                      label) BEFORE the replay, short OUT clip
                      (default 0.6 s, no text) AFTER. Concat +
                      scoreboard.ass + SLOW MOTION badge burn, all
                      NVDEC → CPU → NVENC. Score events shift forward
                      by cumulative (replay + IN + OUT) duration
                      before hitting the scoreboard.
                      Stingers themselves come from
                      `assets/branding/stinger_{in,out}_{src_key}_{W}x{H}_{fps}.mp4`
                      — auto-generated on first render at each source
                      × resolution × fps combo and cached. Delete cache
                      files to regenerate after editing brand colour /
                      logo / channel name / REPLAY label in config.
outro.mp4          ── 5 s closing card: extract `main.mp4`'s last
                      frame, gblur + dim it into a static bg, libass
                      overlay the "THANK YOU FOR WATCHING" headline
                      with a 1 s fade-in. Final 1 s fades a full-frame
                      black box on top so the video sinks to black.
                      Silent audio (anullsrc) for concat compatibility
final.mp4          ── concat-demuxer of the three (no re-encode)
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
- `outro_enabled` — `true` to append the 5 s closing card after the
  main render (silent, ends in fade-to-black).
- `outro_text` — big white centred headline (default
  `"THANK YOU FOR WATCHING"`).
- `outro_duration_seconds` — total card length; the last 1 s is the
  fade-to-black tail (default `5.0`).
- `outro_bg_path` — fallback / override bg image used when the last
  frame of `main.mp4` can't be extracted (e.g. main was disabled, or
  the operator wants a fixed shot instead). Empty / missing →
  pipeline falls back to a solid dark colour.
- `intro_sound_path` / `outro_sound_path` — optional mp3 music beds
  for the intro and outro. Looped + capped at the clip duration, with
  volume + afade in/out. Missing file → silent fallback. Default both
  point at `assets/sounds/intro.mp3` so a single track covers both
  bookends; set different paths to differentiate.
- `intro_sound_volume` / `outro_sound_volume` — 0..1, default `0.7`.
  Lower if music drowns out the "VS" pulse / fade-to-black moment.
- `replay_sound_path` — optional mp3 used for every slow-mo replay
  spliced into the main render. Each replay's clip is
  `-stream_loop`-looped and `-t`-capped to the replay's final
  duration, so a short mp3 loops to fill and a long mp3 gets trimmed.
  Defaults to `assets/sounds/slow_motion.mp3`. Missing file → muted
  replay fallback (the original behaviour). `replay_sound_volume`
  (0..1, default `0.7`) scales the music against the surrounding
  real-time slice audio.
- `stinger_enabled` — `true` to bracket every slow-mo replay with a
  branded transition; `false` skips the brackets entirely.
- `stinger_duration_seconds` — IN-clip duration in seconds. The IN
  clip carries the full reveal animation + channel name + REPLAY
  hold. Default `2.0` — increase to give viewers longer to read.
- `stinger_out_duration_seconds` — OUT-clip duration. Always shorter
  than IN; rendered as a separate no-text variant then time-reversed,
  so the wipe back to live action is snappy. Default `0.6`.
- `brand_color` — hex (`#FF5722`), `0xRRGGBB`, or named colour for
  the sliding bar + accent. Default deep orange (`#FF5722`); contrasts
  the cyan SLOW MOTION badge so they read as two distinct mode
  signals.
- `channel_name` — line of text below the logo (Vietnamese-safe via
  libass shaping). Empty → no line. Only renders on IN.
- `stinger_replay_label` — accent-gold line below the channel name,
  default `"REPLAY"`. Renders as `▶ <label>`. Empty → no line.
- `stinger_text` — legacy config key, currently unused in the renderer
  (channel_name + stinger_replay_label replaced it). Kept in config
  for backwards compatibility; safe to ignore.
- `brand_logo_path` — image overlaid centred on the brand bar
  (circular alpha mask + fade-in applied at render time). Accepts
  `.png` / `.jpg` / `.jpeg` / `.webp`. Default
  `assets/branding/logo.jpg`. Missing → brand bar + text only.
- `stinger_sound_path` — short swoosh mp3/wav layered into the IN
  clip; the reverse comes for free in the OUT clip via `areverse`.
  Default `assets/sounds/stinger_swoosh.wav`. Missing → silent.

Stinger clips are rendered ONCE per `(source × W × H × fps)` and
cached in `assets/branding/stinger_{in,out}_{src_key}_{W}x{H}_{fps}.mp4`,
where `src_key` is an 8-char hash of the source video's path (so the
blurred-frame background tracks the actual match being recapped, not
some other match's frame). Subsequent renders of the same match at
the same spec reuse the cache with zero overhead; switching to a
different source video auto-regenerates. To swap the branding
identity (colour, logo, channel name, REPLAY label), edit the config
keys and **delete the cached mp4s** — the next render will rebuild
from current config.

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
