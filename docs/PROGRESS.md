# Progress Status

Last update: 2026-05-12

## Module map

| Module | Status | File |
|---|---|---|
| Backend skeleton (FastAPI) | ✅ done | [backend/server.py](../backend/server.py) |
| Config loader | ✅ done | [backend/config.py](../backend/config.py) |
| Pydantic models | ✅ done | [backend/models.py](../backend/models.py) |
| FFmpeg / FFprobe wrapper | ✅ done | [backend/ffmpeg_runner.py](../backend/ffmpeg_runner.py) |
| ASS scoreboard generator | ✅ done | [backend/ass_builder.py](../backend/ass_builder.py) |
| Render orchestrator | ✅ done | [backend/renderer.py](../backend/renderer.py) |
| Frontend HTML + Tailwind | ✅ done | [frontend/index.html](../frontend/index.html) |
| Frontend logic (player + state) | ✅ done | [frontend/app.js](../frontend/app.js) |
| Frontend styles | ✅ done | [frontend/styles.css](../frontend/styles.css) |
| Run launcher (Windows) | ✅ done | [run.bat](../run.bat) |
| Virtual environment | ✅ done | `venv/` |

## Feature status

### Setup & project management
- ✅ Tournament + player names
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
| A / D | P1 / P2 score | ✅ |
| H | mark highlight start/end | ✅ |
| S | toggle slow-mo on most-recent highlight | ✅ |
| T / Y | mark trim start / end | ✅ |
| Ctrl+Z | undo (100-deep stack) | ✅ |

### Referee logic
- ✅ Point-by-point scoring
- ✅ Auto set win at 11 + 2-point lead
- ✅ Auto reset of point counter on set win
- ✅ Score event timestamped to source video time
- ✅ Undo restores prior state including pending highlight start

### Highlight & trim lists
- ✅ Add highlight by `H` key (start / end)
- ✅ Add highlight manually (start / end via prompt)
- ✅ Per-highlight slow-mo checkbox
- ✅ Edit start/end inline; jump-to-start; delete
- ✅ Trim segments (T/Y or manual)

### Render pipeline
- ✅ Intro (4 s default; cinematic avatar card with blurred-source bg,
      circular-masked player photos sliding in from both sides, gold
      tournament line, slow Ken-Burns bg zoom, avatar bobbing, "VS"
      pulse, slow name fade-out — falls back to text-only intro when
      no avatar/placeholder is on disk)
- ✅ Highlight reel (per-clip ffmpeg with input seeking + 2× slow-mo on tail)
- ✅ Main match (multi-input ffmpeg with input seeking, scoreboard burned via `ass=`)
- ✅ Final concat (concat demuxer, no re-encode)
- ✅ NVENC h264 (configurable to hevc / av1)
- ✅ NVDEC via `-hwaccel cuda`
- ✅ Per-stage progress reporting via `-progress pipe:1`
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
      avatar lookup, scoreboard event walk, builder smoke tests).
      78 tests, runs in <0.5 s. Configured in `pyproject.toml`,
      basetemp pinned to `temp/pytest/` to dodge sandbox-denied
      access on the user-temp dir.

### Code organisation
- ✅ ASS overlay generators split from a 1208-line `ass_builder.py`
      monolith into the `backend/ass/` package — common / scoreboard
      / intro / badges / transition. Public re-exports in
      `__init__.py`; every emitted .ass file is byte-identical to
      pre-split output (verified across 8 + 11 cases).
- ✅ `build_scoreboard_ass` decomposed into `_Geometry` dataclass +
      `_AssetText` + 5 emit helpers (`_emit_live_panel`,
      `_emit_dynamic_numbers`, `_emit_recap_cards`,
      `_emit_flag_overlays`, `_emit_final_scoreboard`). Public function
      now a 66-line dispatcher.
- ✅ `run_render` decomposed into `RenderContext` + 7 stage helpers
      (`_resolve_source`, `_prepare_context`, `_intro_stage`,
      `_highlight_stage`, `_bridge_stage`, `_main_stage`, `_finalize`).
      Public function now a 12-line dispatcher.
- ✅ NVENC / AAC / hwaccel helpers + audio rate constants centralised
      in `ffmpeg_runner.py` (was duplicated between renderer.py and
      intro_builder.py).
- ✅ Frontend `app.js` (915 lines) split into 12 ES6 modules under
      `frontend/*.js` — state, dom, timecode, toast, avatars, score,
      player, highlights, trims, project_io, render + the boot file.

## Known gaps

See [TODO.md](TODO.md) and [ROADMAP.md](ROADMAP.md) for what's left.
