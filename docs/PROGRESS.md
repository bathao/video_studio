# Progress Status

Last update: 2026-07-07 (improvement plan COMPLETE: Phase 0 housekeeping `a20d7dc`+`ffd7d54`, Phase 1 backend robustness `715d0c1`, Phase 2 frontend correctness/UX `7508d91`, Phase 3 skipped, Phase 4 perf plumbing, Phase 5 tests + debt (209 tests), Phase 6 backlog picks — dry-run script, GitHub Actions CI, Python pin, audio-ducking item found stale — see [TODO.md](TODO.md))

## Module map

Several packages here used to be single-file modules. After the
refactor pass shipped in commits fb4d2aa / 6993a37 / 3c4f167 /
0072cab, each public name is a re-export from the package
`__init__.py` so existing callers keep working. See
[CLAUDE.md](../CLAUDE.md) for per-module file layout.

| Module | Status | File |
|---|---|---|
| Backend skeleton (FastAPI) | ✅ done | [backend/server/](../backend/server/) (package) |
| Config loader | ✅ done | [backend/config.py](../backend/config.py) |
| Pydantic models | ✅ done | [backend/models.py](../backend/models.py) |
| FFmpeg / FFprobe wrapper | ✅ done | [backend/ffmpeg_runner.py](../backend/ffmpeg_runner.py) |
| ASS overlay builders | ✅ done | [backend/ass/](../backend/ass/) |
| Render orchestrator | ✅ done | [backend/renderer/](../backend/renderer/) (package) |
| Cinematic intro builder | ✅ done | [backend/intro_builder.py](../backend/intro_builder.py) |
| Auto-stinger builder | ✅ done | [backend/stinger_builder.py](../backend/stinger_builder.py) |
| Post-render groundtruth sidecar | ✅ done | [backend/groundtruth.py](../backend/groundtruth.py) |
| Post-render dataset archive | ✅ done | [backend/dataset.py](../backend/dataset.py) |
| Auto Trim ROI auto-detect | ✅ done (gate unblocked 2026-05-20) | [backend/roi/](../backend/roi/) + [backend/roi_yolo.py](../backend/roi_yolo.py) |
| Auto Trim rally detector | ✅ done | [backend/rally_detector.py](../backend/rally_detector.py) |
| Auto Trim SSE orchestration (job state + cache + endpoints) | ✅ done | [backend/server/routes_auto_trim.py](../backend/server/routes_auto_trim.py) + [state.py](../backend/server/state.py) |
| Frontend HTML + Tailwind | ✅ done | [frontend/index.html](../frontend/index.html) |
| Frontend logic (player + state) | ✅ done | [frontend/app.js](../frontend/app.js) (boot) + 12 ES6 modules |
| Auto Trim modal (Phase A ROI + Phase B detection) | ✅ done | [frontend/auto_trim/](../frontend/auto_trim/) (8 modules) |
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
- ✅ **Preview Cut** — "▶ Preview cut" toggle plays only the kept
      segments, auto-jumping past every trim (manual + auto) on playback
      and manual seeks (the same set the render keeps; scoreboard overlay
      stays correct because score events live in kept segments). A
      dedicated transport bar appears under the main controls only while
      preview is on (accent border + pulsing label, clear of the
      bottom-right scoreboard): play/pause, ⏪/⏩ and the position readout
      all operate on KEPT time via source↔kept mapping; speed pills
      (0.5/1/1.5/2×) sync with the main speed selector. Frontend-only;
      no intro / replay / stinger / outro (those need a real render).

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
- ✅ Match-over detection per `best_of`: first to ceil(best_of/2) sets
      ends the match — the closing set gets a "🏆 Match won" toast and
      further scoring at that playback position is blocked (seek back
      before the final set to re-enable). (2026-07-07)
- ✅ Undo restores prior state including pending highlight start,
      pending trim start + both HUD badges. Info edits (names /
      tournament / best-of), match-type toggles and video-source
      changes are undoable too — snapshots are taken per 1.5 s typing
      burst, so Ctrl+Z can no longer clobber names typed after the
      last scoring action. (2026-07-07)

### Highlight & trim lists
- ✅ Add highlight by `H` key (start / end)
- ✅ Add highlight manually — inserts an inline-editable row at the
      playhead (blocking `prompt()` pair removed 2026-07-07; trims same)
- ✅ Edit start/end inline; jump-to-start; delete
- ✅ Per-highlight clip export — ⤓ button on each row cuts the raw
      source segment for that highlight to
      `output/<project>_hl<NN>_<start>-<end>.mp4` via
      `POST /api/highlights/export` (NVENC re-encode for a frame-accurate
      cut, audio mapped iff present, camera timecode track dropped).
      Source resolved the renderer's way (token → else `video_file` as
      absolute path or `videos/` basename), so it works on reloaded
      projects after a server restart.
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
      slow-mo replay in main. IN clip (config default 1.5 s) carries the full
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
- ✅ NVDEC session-budget guard for high slice counts. When the main
      playlist has > 6 "slice" entries (Auto Trim with many trims —
      one consumer GPU run hit 91 slices), the renderer first
      consolidates every kept segment into a single intermediate via
      the concat demuxer (`pre_concat_slices` in `renderer/stages.py`),
      then the main filter graph consumes it through `split` + `trim`
      per slice entry. One decoder context for every slice instead of
      N parallel `-i src` opens — escapes both CUDA_ERROR_OUT_OF_MEMORY
      on `cuvidCreate` (NVDEC session cap) and the software-decode RAM
      blowup from 90+ HEVC ref-frame buffers. Replays + stingers keep
      their own inputs (replays need precise frame-accurate seek for
      slow-mo). Pre-concat consumes ~25 % of the "main" stage weight;
      filter-graph render gets ~75 %.
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
- ✅ Inter-set recap: same bottom-right scoreboard panel as the final
      summary, expanded by one set column each time. After set 1 the
      panel shows 1 set column; after set 2, 2 columns; etc. Each
      recap holds for 4 s (clipped to 0.3 s before the next score
      event). Match-ending set falls through to the dedicated final
      scoreboard. Replaces the previous centred 140 px / 200 px
      "SET N" + "SET N+1" cards which were hard to read.
- ✅ End-of-match final scoreboard anchored at the same bottom-right
      corner with identical fonts/colours/opacities; just adds one
      column per played set. Shares `_emit_scoreboard_panel` with the
      inter-set recap path — single layout, two call sites.
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
- ✅ Performance plumbing pass (2026-07-07, improvement-plan Phase 4):
  - Scoreboard preview refresh no longer runs on every `timeupdate`
    tick (was a full-project `JSON.stringify` 4×/s); the .ass depends
    only on info + score events, so the mutation sites (`scorePoint`,
    `deleteScoreEvent`, `syncInfoFromInputs`) refresh it explicitly.
  - Preview Cut's kept-segments are cached; `syncTrims` +
    `durationchange` invalidate.
  - Events / highlights / trims panels render as one HTML string with
    click/change listeners delegated to each `<ul>` — a long match no
    longer rebuilds + rebinds hundreds of per-row listeners per point.
  - Auto-trim cache-hit replays only stage/log/trim/done events; the
    thousands of cached per-frame progress events are skipped (the
    frontend jumps the bar to 100% on `close`), making cache hits
    actually instant.
- ✅ Frontend robustness pass (2026-07-07, improvement-plan Phase 2):
  - Boot survives a down/starting backend — a failed `/api/videos`
    list no longer aborts init, so `syncAllUI` always runs and the
    operator sees an initialized UI + a toast instead of a dead page.
  - Network-failure try/catch + toast on every user-triggered fetch:
    save / list / load project, render start, reveal output, open
    output folder. Save button disabled while the PUT is in flight.
  - Scoreboard preview refresh marks its state signature clean only
    AFTER a successful `.ass` fetch (was: before the fetch, so a
    failed refresh looked up-to-date and the overlay stayed stale
    until the next unrelated edit). 3 s backoff between retries so a
    down backend isn't hammered every debounce tick.
- ✅ Backend robustness pass (2026-07-07, improvement-plan Phase 1):
  - Auto-trim cache-hit replay publishes `job.trims` atomically
    (local list + single assignment) instead of appending while the
    `/api/auto_trim/job/{id}` status endpoint iterates — closes a
    "list changed size during iteration" race.
  - Job registries (`_jobs` render + `_auto_trim_jobs`) evict the
    oldest finished entries beyond 20 via `prune_finished_jobs` at
    insert time — previously both grew unbounded for the life of the
    uvicorn process (auto-trim entries own an event queue each).
  - Missing `videos/` basename now returns a clean 404 from
    `_resolve_video_for_auto_trim` instead of an unhandled
    `FileNotFoundError` 500 from `_video_identity`'s `stat()`.
  - Swallowed failures surfaced: refframe ffmpeg extracts log
    per-frame failures and raise if ALL frames fail; probe failure
    raises 500 instead of silently assuming a 60 s duration (which
    sampled all 5 detect frames from the first minute of long
    videos); malformed score events dropped at `/api/auto_trim/start`
    are counted, logged, and reported as `dropped_events` in the
    response.
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
      stinger cache + dataset archive + groundtruth sidecar + rally
      detector gaps-to-trims + auto-trim cache key + TrimSegment.source).
      **209 tests**, runs in <0.8 s. Configured in `pyproject.toml`,
      basetemp pinned to `temp/pytest/` to dodge sandbox-denied
      access on the user-temp dir; `tests/conftest.py` creates the
      `temp/` parent so fresh checkouts (CI) work too.
- ✅ GitHub Actions CI (2026-07-07): windows-latest + Python 3.13,
      ruff critical rules + full pytest suite on every push/PR to
      main / v3-dev. Dev-only deps in `requirements-dev.txt`.

### Auto Trim (Phase 1a + ROI gate)
- ✅ Auto Trim modal opens via "⚡ Auto Trim" button on the Trim panel.
      Modal extracts a midpoint refframe, runs `detect_roi_multiframe`
      against 5 evenly-spaced frames, renders the proposed quad on a
      canvas with 4 draggable corners + always-on 3×-zoom inset panels.
      Confirm saves to `project.info.roi_quadrilateral` AND appends to
      `dataset/roi_groundtruth/<video_id>.json` (latest_corners + full
      history of detector-proposed-vs-confirmed for drift measurement).
- ✅ Multi-tier ROI detector (`backend/roi/` package): YOLOv8-seg
      + ORB-keypoint homography + HSV color-contrast foreground + HSV
      learned-NN + naive color-blue. 5 frames per detect, cross-tier
      IoU consensus prefers geometric agreement across tier tags over
      single-tier vote. Mask mAP50-95 = 0.921 on 53-entry dataset.
- ✅ Perf bundle (commit 50b8058): YOLO model warmup at uvicorn
      startup, process-wide ORB feature cache + groundtruth example
      cache invalidated by dir mtime, batched YOLO inference, parallel
      multi-frame worker pool, parallel ffmpeg refframe extracts.
      First-click ~3× faster; steady-state click ~2.7s for 5 frames.
- ✅ **ROI gate unblocked 2026-05-20.** Operator confirmed accuracy
      acceptable for production.

### Auto Trim (Phase 1b — rally detection end-to-end, shipped 2026-05-26)
- ✅ Score-event-anchored rally detector (`backend/rally_detector.py`).
      NVDEC decode → 480×270 RGB → ROI motion → 0.5 s moving avg →
      adaptive p70 threshold → backward-scan gap detection → 5 s rally
      minimum gate. Pure `gaps_to_trims` core split out for unit-test
      coverage without ffmpeg. Hardcoded Balanced preset
      (`J_fg_p70_rmin5`) — same params validated in PHASE0_REPORT.
      Verify on 3 spike entries: recall 0.957-0.993 (≥ PHASE0_REPORT
      Balanced 0.922-0.976), extras 303.7-352.7s within tolerance.
      Detect speed ~6.2× realtime since 2026-06-05 (`-hwaccel cuda`
      GPU-decode fix; was ~2.1× — the old `scale_cuda` NVDEC command
      silently failed and fell back to CPU decode). See TODO.md.
- ✅ Backend SSE orchestration (`backend/server/routes_auto_trim.py` +
      `state.py`). New endpoints:
      `POST /api/auto_trim/start` → spawns worker thread, returns job_id;
      `GET /api/auto_trim/events/{job_id}` → SSE stream of stage /
      progress / trim / done / error events;
      `POST /api/auto_trim/cancel/{job_id}` → flip cancel flag;
      `GET /api/auto_trim/job/{job_id}` → snapshot for debug.
      Cache layer at `temp/auto_trim_cache/<sha1>.json` keyed by
      (video_id + roi + score events + params); cache hit replays
      events in ms. Worker mirrors the render-job pattern (one dict +
      one lock in `state.py`) but with a `queue.Queue` per job that
      the SSE handler drains.
- ✅ Frontend Phase B in the Auto Trim modal. After confirming ROI,
      "Rally detection" section unlocks: shows score event count, Run
      button (disabled until ≥10 score events), progress bar that
      tracks decode frames, live log of stage events, results panel
      with trim count + total dead time + cache hit/miss, Apply /
      Discard buttons. Re-running auto-trim filters out existing
      `source=="auto"` trims first so manual trims are preserved.
      New module `frontend/auto_trim/detection.js` (state machine +
      EventSource client + Apply/Discard handlers).
- ✅ `TrimSegment.source: Literal["manual", "auto"]` field. Legacy
      project JSONs without the field default to `"manual"` via
      Pydantic. Trim panel renders an `[AUTO]` badge next to
      `source=="auto"` rows.
- 9 new pytest tests (`tests/test_auto_trim_routes.py`): cache key
      determinism (same inputs / event-order invariance / float jitter /
      different video / different roi / different params) + TrimSegment
      backwards compat. Total suite 166 pass.

### Code organisation
- ✅ ASS overlay generators live in the `backend/ass/` package —
      `common` / `scoreboard` (sub-package) / `intro` / `outro` /
      `badges` / `stinger`. Public re-exports in `__init__.py`.
- ✅ Scoreboard subdivided into `backend/ass/scoreboard/` package —
      `geometry` / `events` / `emit_live` / `emit_cards` / `emit_final` /
      `builder`. Public function is a thin dispatcher.
- ✅ `run_render` lives in the `backend/renderer/` package (split
      across `state` / `segments` / `replays` / `stages` /
      `orchestrator` / `__init__`). Public API re-exported from
      `__init__.py` so historical `from backend.renderer import ...`
      calls still work.
- ✅ HTTP endpoints split across the `backend/server/` package —
      `app` (composition root + main) / `state` (job registry + path
      constants) / `utils` (shared helpers) / four `routes_*.py`
      routers (videos / projects / render / auto_trim).
- ✅ NVENC / AAC / hwaccel helpers + audio rate constants centralised
      in `ffmpeg_runner.py` (was duplicated between renderer.py and
      intro_builder.py).
- ✅ Frontend `app.js` (915 lines) split into 12 ES6 modules under
      `frontend/*.js` — state, dom, timecode, toast, avatars, score,
      player, highlights, trims, project_io, render + the boot file.
- ✅ Auto Trim modal split into `frontend/auto_trim/` package —
      `index` (public entry + listeners) / `state` (DOM refs +
      session state) / `modal` (close helper) / `log` / `canvas` /
      `info_panel` / `api`. ROI confirmation is wired up; Phase B
      (rally detection UI) lands with Step 3 of Phase 1b.

## Known gaps

See [TODO.md](TODO.md) and [ROADMAP.md](ROADMAP.md) for what's left.
