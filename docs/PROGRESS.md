# Progress Status

Last update: 2026-05-15 (auto-stinger)

## Module map

| Module | Status | File |
|---|---|---|
| Backend skeleton (FastAPI) | ✅ done | [backend/server.py](../backend/server.py) |
| Config loader | ✅ done | [backend/config.py](../backend/config.py) |
| Pydantic models | ✅ done | [backend/models.py](../backend/models.py) |
| FFmpeg / FFprobe wrapper | ✅ done | [backend/ffmpeg_runner.py](../backend/ffmpeg_runner.py) |
| ASS overlay builders | ✅ done | [backend/ass/](../backend/ass/) |
| Render orchestrator | ✅ done | [backend/renderer.py](../backend/renderer.py) |
| Cinematic intro builder | ✅ done | [backend/intro_builder.py](../backend/intro_builder.py) |
| Auto-stinger builder | ✅ done | [backend/stinger_builder.py](../backend/stinger_builder.py) |
| Frontend HTML + Tailwind | ✅ done | [frontend/index.html](../frontend/index.html) |
| Frontend logic (player + state) | ✅ done | [frontend/app.js](../frontend/app.js) |
| Frontend styles | ✅ done | [frontend/styles.css](../frontend/styles.css) |
| Run launcher (Windows) | ✅ done | [run.bat](../run.bat) |
| Virtual environment | ✅ done | `venv/` |

## Feature status

### Setup & project management
- ✅ Tournament + player names
- ✅ Match type tabs (**Single** / **Double**). Doubles adds P3 + P4
      inputs (team 1 partner = P3, team 2 partner = P4) and switches the
      scoreboard rows to combined "lastTwo(P1) + lastTwo(P3)" labels.
      Score hotkeys A/D map to top team / bottom team in doubles. Shared
      rule lives in `resolve_row_names` (backend) — same combine logic
      drives the scoreboard, the cinematic intro's name line, and the
      Live Score panel mirror in the frontend.
- ✅ Source video dropdown (auto-scan `videos/`)
- ✅ Native "Browse…" picker — pick any file from disk. Files inside
      `videos/` merge with the dropdown; files elsewhere are streamed
      under a session-scoped sha1 token (`/api/videos/external/{token}/stream`).
      Saved projects store the absolute path; reload re-registers the
      token automatically.
- ✅ Save / Load project to `projects/<name>.json`
- ✅ Project list modal

### Video playback
- ✅ Range-aware streaming with 32 MiB chunk cap (works on 10–15 GB files)
- ✅ Play / pause, skip ±5/10/20 s, scrubber
- ✅ Speed presets 0.25× / 0.5× / 1× / 1.5× / 2×
- ✅ HUD time + duration

### Keyboard shortcuts
| Key | Action | Status |
|---|---|---|
| Space | play / pause | ✅ |
| ←/→ | seek 5s (Shift = 1s) | ✅ |
| A / D | P1 / P2 score (top / bottom team in doubles) | ✅ |
| H | mark highlight start/end | ✅ |
| T / Y | mark trim start / end | ✅ |
| Ctrl+Z | undo (100-deep stack) | ✅ |

(S used to toggle per-highlight tail slow-mo; removed once the main
render started splicing a full 50% replay after every highlight.)

### Referee logic
- ✅ Point-by-point scoring
- ✅ Auto set win at 11 + 2-point lead
- ✅ Auto reset of point counter on set win
- ✅ Score event timestamped to source video time
- ✅ Undo restores prior state including pending highlight start

### Highlight & trim lists
- ✅ Add highlight by `H` key (start / end)
- ✅ Add highlight manually (start / end via prompt)
- ✅ Edit start/end inline; jump-to-start; delete
- ✅ Trim segments (T/Y or manual)

### Render pipeline
- ✅ Cancel a running render: backend's `run_ffmpeg_with_progress`
      reads a `cancel_check` predicate on each progress line and
      `proc.terminate()`s the child; `FFmpegCancelled` propagates to
      `run_render` which flags the job `cancelled` (distinct from `error`).
      `POST /api/render/{job_id}/cancel` flips the flag; the UI exposes
      a Cancel button while a render is in flight.
- ✅ Intro (4 s default; cinematic avatar card with blurred-source bg,
      circular-masked player photos sliding in from both sides, gold
      tournament line, slow Ken-Burns bg zoom, avatar bobbing, "VS"
      pulse, slow name fade-out — falls back to text-only intro when
      no avatar/placeholder is on disk). Doubles uses a 4-avatar layout
      (P1+P3 left pair, P2+P4 right pair, each ~60% of the singles
      avatar size) so all four players appear without re-rendering or
      a separate pipeline.
- ✅ Main match (multi-input ffmpeg with input seeking, scoreboard
      burned via `ass=`). Every highlight gets a 50%-speed replay
      spliced in **right after its real-time occurrence** (setpts*2 +
      atempo=0.5), with a pulsing SLOW MOTION badge top-left for the
      duration of each replay. Replay plan + playlist builder + event
      remap pass live in `renderer.py`; total main duration grows by
      `sum(highlight_dur) × (1 / 0.5)` and the scoreboard's
      `total_duration` is recomputed from the playlist's final
      timeline so libass doesn't expire mid-clip. Pipeline simplified
      (2026-05-15): no separate highlight reel section at the top, no
      intermission/transition bridge, no FULL MATCH badge — highlights
      now contribute only their inline slow-mo replays.
- ✅ Auto-stinger transition: asymmetric branded bracket around every
      slow-mo replay in main. IN clip (default 2 s) carries the full
      reveal — blurred source-frame bg, brand-colour wipe (alpha 0.5),
      diagonal light streak, circular-masked logo, channel name +
      "▶ REPLAY" label, vignette. OUT clip (default 0.6 s) is a
      no-text variant rendered separately at the OUT duration then
      time-reversed → quick wipe back to live action. Cached as
      `assets/branding/stinger_{in,out}.mp4` + `stinger.manifest.json`;
      manifest captures every input that affects pixels (W/H/fps,
      brand_color, channel_name, replay_label, in/out durations,
      logo + sound paths + mtimes), so editing the logo or sound in
      place auto-invalidates the cache. No manual delete needed.
      Score events shifted by `replay_dur + in_dur + out_dur` past
      each replay so the scoreboard stays in sync.
- ✅ Slow-mo replay music: single mp3 (`replay_sound_path`, defaults
      to `assets/sounds/slow_motion.mp3`) reused for every spliced-in
      replay. Each replay gets its own `-stream_loop -1 -t r_dur -i
      <file>` input so a short mp3 loops to fill the replay and a long
      mp3 gets trimmed. Volume + afade in/out match the surrounding
      slice transitions; missing file → original muted-replay path
      (REPLAY_VOLUME=0) as fallback.
- ✅ Optional music beds for intro + outro (`intro_sound_path` /
      `outro_sound_path` in config — defaults to shared
      `assets/sounds/intro.mp3`). `music_input_args` +
      `music_filter_chain` in `ffmpeg_runner.py` loop the file, cap to
      the clip duration, and apply `volume` + `afade` in/out. Intro
      uses a 0.3 s in / 0.5 s out; outro uses 0.5 s in / 1.0 s out so
      it sinks together with the fade-to-black tail. Missing file →
      silent anullsrc fallback.
- ✅ Cinematic outro card at the very end of the final cut: extract
      the last frame of `main.mp4`, blur (`gblur=sigma=30`) and dim
      (`eq=brightness=-0.3`) it into a static background, then libass
      overlay the configured headline (default
      "THANK YOU FOR WATCHING") centred with a 1 s fade-in. The last
      1 s of the 5 s clip fades a full-frame black box in on top so
      the video sinks to black before EOF — no xfade at the concat
      boundary because the outro's first frame matches main's last
      frame visually, so the cut is invisible. Audio stream is
      `anullsrc` (silent) — concat demuxer needs matching stream
      layout, so `-an` is the wrong tool here. Optional
      `outro_bg_path` in config acts as a fallback / override when
      frame extraction fails (e.g. main didn't render) or the
      operator wants a fixed bg.
- ✅ Final concat (concat demuxer, no re-encode)
- ✅ NVENC h264 (configurable to hevc / av1)
- ✅ NVDEC via `-hwaccel cuda`
- ✅ Per-stage progress reporting via `-progress pipe:1` — progress
      messages format the elapsed / expected time as `m:ss` (or
      `h:mm:ss` past an hour) instead of raw seconds so operator can
      glance and know how far in / left at a glance.
- ✅ Score-event timestamp remap (source-time → trimmed-output-time)
- ✅ Silent-audio injection when source has no audio track

### Scoreboard graphics
- ✅ Bottom-right corner, two-row layout
- ✅ Tournament tag above the panel (right-aligned, single-line)
- ✅ Player names + set count + points per row
- ✅ ▶ marker on the player who most recently scored
- ✅ Gold highlight on the active player's points; white on the other
- ✅ Set-point (sets) column tinted gold to read distinctly from the
      points column at a glance
- ✅ Compact cell sizing — fonts one step smaller, padding tightened so
      borders sit close to the digits
- ✅ Set-transition card ("SET 2/3/4/5") holds for 4.5 s after each set
      (was 1.5 s) so the viewer has time to read it
- ✅ End-of-match final scoreboard anchored at the same bottom-right
      corner with identical fonts/colours/opacities; just adds one
      column per played set
- ✅ Vietnamese diacritics (UTF-8 .ass + Arial fallback via libass + DirectWrite)
- ✅ Pre-render scoreboard preview: same `build_scoreboard_ass_text` the
      render pipeline burns in is streamed to JASSUB (libass-WASM) in the
      browser, which auto-attaches a canvas to the `<video>` element as
      soon as the source video loads its metadata. Overlay is
      byte-identical to the final render — single source of truth,
      animation states (recap cards, GP/MP/DEUCE flag pulse, set
      transitions) all preview correctly. Refresh debounced 250 ms on
      info / score edits so the overlay tracks every keystroke.

### Output & file management
- ✅ Output saved to `output/<name>.mp4`
- ✅ Inline browser playback at `/api/output/<name>`
- ✅ "Show in folder" → `explorer /select,<path>`
- ✅ "Open output folder" → opens `output/`
- ✅ List of past outputs at `/api/outputs`

### Polish & robustness
- ✅ Path-traversal protection (`_resolve_inside`)
- ✅ Safe project name regex
- ✅ FFmpeg error captured and surfaced to UI (last 2KB of stderr)
- ✅ Toast notifications
- ✅ `.gitignore` for venv / temp / outputs
- ✅ README in English with full workflow
- ✅ Docs index (this folder)
- ✅ Pytest suite for pure logic (segment math, text helpers,
      avatar lookup, scoreboard event walk, builder smoke tests, main
      playlist + stinger bracket + event remap + intro photo gate +
      stinger cache). 109 tests, runs in
      <0.2 s. Configured in `pyproject.toml`,
      basetemp pinned to `temp/pytest/` to dodge sandbox-denied
      access on the user-temp dir.

### Code organisation
- ✅ ASS overlay generators live in the `backend/ass/` package —
      `common` / `scoreboard` / `intro` / `outro` / `badges` /
      `stinger`. Public re-exports in `__init__.py`. (Originally
      split from a 1208-line `ass_builder.py` monolith; the v1.5
      pipeline simplification later retired the `intermission` and
      `transition` submodules.)
- ✅ `build_scoreboard_ass` decomposed into `_Geometry` dataclass +
      `_AssetText` + 5 emit helpers (`_emit_live_panel`,
      `_emit_dynamic_numbers`, `_emit_recap_cards`,
      `_emit_flag_overlays`, `_emit_final_scoreboard`). Public function
      now a thin dispatcher.
- ✅ `run_render` decomposed into `RenderContext` + 4 stage helpers
      (`_resolve_source` + `_prepare_context` + `_intro_stage` +
      `_main_stage` + `_outro_stage` + `_finalize`). Public function
      is a short orchestrator.
- ✅ NVENC / AAC / hwaccel helpers + audio rate constants centralised
      in `ffmpeg_runner.py` (was duplicated between renderer.py and
      intro_builder.py).
- ✅ Frontend `app.js` (915 lines) split into 12 ES6 modules under
      `frontend/*.js` — state, dom, timecode, toast, avatars, score,
      player, highlights, trims, project_io, render + the boot file.

## Known gaps

See [TODO.md](TODO.md) and [ROADMAP.md](ROADMAP.md) for what's left.
