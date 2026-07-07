# CLAUDE.md

Reference for AI assistants working on this repo. Read this first;
it'll save you ~10 minutes of grep-style discovery on every session.

## What this is

A local web app that turns a tripod recording of a table-tennis match
into a broadcast-style recap with intro, slow-mo highlights, and a
burned-in live scoreboard. Single operator on Windows + RTX 5060 Ti.
Runs entirely offline; no external services.

## Run / dev loop

```bash
run.bat                           # activates venv/, starts uvicorn on :8765
# UI at http://127.0.0.1:8765/
```

The dev loop is **edit → save → reload browser**. The frontend is
served as static files with cache-busting headers, so a hard refresh
isn't needed. The backend reloads only when you restart `run.bat`
(no `--reload` flag — uvicorn restart is fast enough manually).

`requirements.txt` is just FastAPI + uvicorn + python-multipart +
pydantic. ffmpeg + ffprobe live on PATH. tkinter ships with Python.
Dev/CI-only extras (pytest, numpy, opencv-python-headless, ruff) live
in `requirements-dev.txt`; `.github/workflows/ci.yml` runs ruff
critical rules + the pytest suite on windows-latest per push/PR.
Python is pinned `>=3.13` in `pyproject.toml`.

## Repo layout

```
backend/
  server/            FastAPI endpoints (videos, projects, render jobs,
                     avatars, auto-trim). 9 modules under the package:
    __init__.py        Re-exports `app` + `main` so historical entry
                       points keep working (`uvicorn backend.server:app`,
                       `python -m backend.server`).
    __main__.py        Entry for `python -m backend.server` (run.bat).
    app.py             Composition root — FastAPI instance + CORS +
                       no-cache middleware + StaticFiles mount + `/`
                       index + `/api/health` + `main()` uvicorn launcher.
                       Includes every routes_*.py router.
    state.py           Module-level state shared across routers:
                       `_jobs` + `_jobs_lock` (render job registry),
                       `_external_videos` + `_external_lock` (token →
                       absolute path registry), `_REFFRAME_CACHE`,
                       `_ROI_GROUNDTRUTH_DIR`, plus ROOT_DIR /
                       FRONTEND_DIR / VIDEO_EXTS / SAFE_NAME_RE
                       constants.
    utils.py           Pure helpers — no app instance, no router:
                       `_validate_video_path`, `_register_*` /
                       `_resolve_external_video`, `_safe_name`,
                       `_resolve_inside`, `_sanitize_for_json`, and
                       the `_open_or_focus_explorer` PowerShell shim.
    routes_videos.py   `/api/videos/*` (local list + probe + range-
                       aware stream), `/api/videos/browse` (native
                       picker), `/api/videos/external/*` (token-keyed
                       arbitrary-path videos), `/api/avatars/*`.
                       Hosts the shared `_stream_file` helper.
    routes_projects.py `/api/projects/*` (list/get/save/delete).
    routes_render.py   `/api/render/*` (start/status/cancel/list),
                       `/api/output*`, `/api/output-folder/open`,
                       `/api/outputs`, `/api/preview/scoreboard.ass`
                       (+ ScoreboardPreviewRequest model),
                       `/api/highlights/export` (cut one highlight's raw
                       source segment to output/ via NVENC re-encode;
                       `_resolve_export_source` mirrors the renderer's
                       token-or-path source resolution).
    routes_auto_trim.py `/api/auto_trim/*` (refframe / detect_roi /
                       confirm_roi / groundtruth_count) + sidecar
                       helpers (`_resolve_video_for_auto_trim`,
                       `_video_identity`, `_extract_refframe`,
                       `_extract_multi_refframes`,
                       `_validate_roi_corners`).
  renderer/          Render orchestrator package. Owns the public
                     `run_render(plan)`. 6 modules:
    __init__.py        Re-exports public API (RenderPlan, RenderContext,
                       RenderState, run_render) + segment / replay /
                       stage helpers used by tests and the groundtruth
                       sidecar.
    state.py           RenderState dataclass (per-job progress + status
                       snapshot). Lives in its own module so
                       backend/server/state.py can import it without
                       pulling the full ffmpeg dep graph.
    segments.py        Segment math — `kept_segments_from_trims` +
                       `remap_score_event_to_trimmed`. Pure logic, no
                       I/O.
    replays.py         Slow-mo replay plumbing — REPLAY_* constants,
                       `ReplayInsert`, `build_replay_plan`,
                       `remap_events_with_replays`, `_PlaylistEntry`,
                       `build_main_playlist`. Pure logic.
    stages.py          Stage helpers (kwargs-only, no ctx): `render_intro`,
                       `pre_concat_slices`, `render_main_with_scoreboard`,
                       `render_outro_card`, `concat_parts`. These are
                       the ffmpeg builders. `pre_concat_slices` runs
                       optionally before the main render when the playlist
                       has more "slice" entries than the GPU's NVDEC
                       session budget (~6), consolidating kept segments
                       into one intermediate via the concat demuxer so
                       the main stage can consume them through `split`
                       + `trim` instead of N parallel `-i src` inputs.
    orchestrator.py    Top-level: `RenderPlan`, `RenderContext`,
                       `_prepare_context`, `_resolve_source`,
                       `all_intro_photos_present`, `_intro_stage`,
                       `_main_stage`, `_outro_stage`, `_finalize`,
                       `run_render`. Knows the pipeline shape.
  intro_builder.py   Cinematic intro ffmpeg filter graph (avatars +
                     bg blur + Ken-Burns zoom + libass text).
  stinger_builder.py Auto-stinger transition clip. Renders a 1s branded
                     wipe (brand bar + optional logo + text) once per
                     (W, H, fps) into assets/branding/stinger_in_*.mp4,
                     then reverses it into stinger_out_*.mp4. Used to
                     bracket every slow-mo replay in main.
  ffmpeg_runner.py   ffprobe + run_ffmpeg_with_progress + shared
                     NVENC / AAC / hwaccel arg helpers and the
                     TARGET_AUDIO_RATE / TARGET_AUDIO_CHANNELS
                     constants. Anything ffmpeg-related lives here.
  avatars.py         find_avatar / find_default_avatar / find_avatar_or_default
                     against assets/avatars/<Name>.<ext>.
  config.py          Reads config.json into typed properties.
  models.py          Pydantic ProjectInfo / ProjectData / RenderRequest.
  groundtruth.py     `export_groundtruth(ctx, output_mp4)` — writes
                     `<name>.groundtruth.json` + `<name>.refframe.png`
                     next to the rendered mp4. Project snapshot + derived
                     `kept_segments` + source video metadata + stats
                     (schema v2: `real_rally_count`, `avg_real_rally_seconds`
                     derived from score events). Called from `_finalize`
                     after every successful render.
  dataset.py         `archive_to_dataset(ctx, output_mp4)` — mirrors the
                     just-finished render into `dataset/<slug>/` via
                     hardlinks (copy fallback cross-volume), copies the
                     sidecars, snapshots project.json, auto-fills
                     notes.md from project + groundtruth, appends to
                     `dataset/manifest.json`. Runs AFTER `export_groundtruth`
                     in `_finalize`. Best-effort: failures land in
                     `RenderState.message` but never abort the render.
  roi/               Multi-stage ROI quadrilateral detector package for the
                     Auto Trim modal. `detect_roi` runs YOLOv8-seg + ORB +
                     color-contrast in parallel and cross-validates. Result
                     tags: `yolo_seg+both_agree` / `+orb_agree` / `+color_agree`
                     when classical CV confirms YOLO's pick (strongest);
                     `orb_homography_vs_yolo` / `color_contrast_vs_yolo`
                     when classical CV overrides YOLO (medium-strong); plain
                     `yolo_seg` when no classical competitor (novel-arena
                     fallback); `color_contrast+orb_agree` / `orb_homography`
                     / `color_contrast_foreground` / `learned_nn/blend/mean`
                     / `color_blue` for non-YOLO paths. `detect_roi_multiframe`
                     aggregates 5 evenly-spaced refframes via tier-priority
                     gates (not raw majority — `learned_*` votes correlated;
                     `color_contrast_foreground` singleton REMOVED, 100%
                     prod fail rate). YOLO confidence is uninformative
                     (mean 0.99, fails at high conf possible) — agreement
                     tags are the real trust signal. Split across 8 modules:
    __init__.py        Re-exports public API (`detect_roi`,
                       `detect_roi_multiframe`, `RoiDetection`,
                       `render_debug_overlay`).
    detector.py        Public entry points + cross-tier disagreement
                       resolution in `detect_roi`, multi-frame priority
                       gates + cross-tier IoU consensus in
                       `detect_roi_multiframe`.
    quad.py            `RoiDetection` dataclass + shared geometry helpers
                       (`_default_roi`, `_fit_quad_to_blob`, `_quad_iou`,
                       `_expand_rect`, `_order_clockwise_from_tl`,
                       `render_debug_overlay`, `_GROUNDTRUTH_DIR`).
                       Imports nothing from sibling tier modules — keeps
                       the dependency graph acyclic.
    yolo_tier.py       Stage -1: `_try_yolo_seg` thin wrapper over
                       `backend.roi_yolo` with mask-area sanity bounds.
    color_contrast_tier.py
                       Stage 0: foreground table by blue/red colour
                       contrast. Hosts `_try_foreground_by_color_contrast`
                       + `_refine_quad_via_white_court_lines` + all
                       `_TT_BLUE_*` / `_FLOOR_RED_*` / `_FG_*` / `_WHITE_*`
                       constants.
    orb_tier.py        Stage 1: ORB keypoint match + RANSAC homography
                       transfer. Reads dataset via
                       `learned_nn_tier._load_groundtruth_examples`.
    learned_nn_tier.py Stage 1 fallback: HSV color-histogram nearest-
                       neighbour with 3-tier prediction (exact / weighted
                       blend / mean-of-all). Hosts the shared
                       `_load_groundtruth_examples` loader.
    naive_tier.py      Stage 2: HSV-color fallback when no groundtruth
                       exists at all (cold start).
  roi_yolo.py        Lazy loader for `assets/models/roi_seg.pt`. Single-
                     image inference returns the 4-corner quad from the
                     highest-confidence mask. Caches `False` when the
                     model file is missing so subsequent calls are a
                     one-bool no-op.
  ass/
    __init__.py      Re-exports the public builders.
    common.py        Palette (C_*) + drawing primitives (_rect) +
                     text helpers (_trim_*, _ass_escape, _ass_rgb,
                     _bgr) + _fmt_time. Imported by every other ass/*.
    scoreboard/      Live + final scoreboard, recap/transition cards,
                     GP/MP/DEUCE flag. Exports `resolve_row_names` so
                     doubles' combined-name rule (last 2 words of each
                     partner) is shared by every consumer. Package
                     layout:
      __init__.py      Public re-exports (ScoreFrame, resolve_row_names,
                       build_scoreboard_ass, build_scoreboard_ass_text)
                       + the `_set_final_score` / `_walk_events`
                       helpers used by the pure-logic test suite.
      geometry.py      `_Geometry` + `_AssetText` dataclasses,
                       `_compute_geometry`, `_scoreboard_header` style
                       block, `_title_fscx_tag` width-fitter.
      events.py        `ScoreFrame` dataclass, `_walk_events`,
                       `_set_final_score`, `resolve_row_names`.
                       Pure logic, tier-agnostic.
      emit_live.py     Static live panel + per-event sets/pts numbers.
      emit_cards.py    Inter-set recap (expand the scoreboard panel
                       with one column per set played so far, ~4 s
                       after each non-match-ending set) + GP/MP/DEUCE
                       pulsing flag.
      emit_final.py    `_emit_scoreboard_panel` (shared bottom-right
                       layout — header + 2 rows + totals col + one
                       col per set in `set_history_so_far`). Used by
                       both the inter-set recap (history sliced to
                       the just-ended set) and the end-of-match
                       summary (full history); `_emit_final_scoreboard`
                       is a thin wrapper around it.
      builder.py       `build_scoreboard_ass_text` orchestrator +
                       `build_scoreboard_ass` file-write wrapper.
    intro.py         Text-only fallback intro + libass companion for
                     the cinematic intro. Same builder serves singles
                     (2 avatars) and doubles (4 avatars laid out as 2
                     pairs); caller pre-combines names for doubles.
    badges.py        Top-left SLOW MOTION badge. Takes a list of
                     (start, end) ranges so one .ass covers every
                     spliced replay.
    outro.py         5-second closing card after main match. libass
                     overlay over the blurred + dimmed last frame of
                     main.mp4; fades to black in the final second.
                     Silent audio for concat-demuxer compatibility.
    stinger.py       libass overlay burned onto the auto-stinger IN
                     clip: diagonal light streak (\\move + \\frz +
                     \\blur), channel name (Vietnamese-safe via
                     libass shaping), gold "▶ REPLAY" label.

frontend/
  index.html         Tailwind via CDN, single-page UI. Entry script
                     `<script type="module" src="app.js">`.
  app.js             Boot module: imports each feature module (which
                     wires its own DOM events at load time), defines
                     the cross-cutting `syncAllUI` / `syncInfoFromInputs`
                     / `undo`, and runs the initial health ping.
  state.js           project / live / undoStack + the `mut` object for
                     scalar shared state (pendingHighlightStart,
                     pendingTrimStart, externalToken, lastSourcedFile,
                     pollTimer). Plus `snapshot()`.
  dom.js             `$` helper.
  timecode.js        `fmt(s)` and `parseTimecode("m:ss.xx")`.
  toast.js           Floating toast.
  avatars.js         `refreshAvatarThumb(slot)` debounced lookup.
  player.js          <video> element + HUD + seek/scrub + speed +
                     source switching (loadVideoList / setVideoSource /
                     browseForVideo / external token registration). Also
                     hosts Preview Cut: playback + manual seeks jump past
                     every trim_segment (jumpPastTrims) and the dedicated
                     "preview-bar" transport seeks on KEPT time via
                     source↔kept mapping (keptSegments / sourceToKept /
                     keptToSource).
  score.js           Score logic (recompute, sync from time, score,
                     delete) + score-panel UI + events list.
  highlights.js      All highlight ops + list UI, incl. the per-row ⤓
                     clip-export button (POST /api/highlights/export).
  trims.js           All trim ops + list UI. Hosts the "⚡ Auto Trim"
                     button that opens `auto_trim/index.js`.
  auto_trim/         Auto Trim modal package: fetches refframe + calls
                     `/api/auto_trim/detect_roi`; renders proposed
                     quadrilateral on canvas with 4 draggable corners
                     (view/edit toggle); Confirm POSTs to
                     `/api/auto_trim/confirm_roi` → saves to
                     `project.info.roi_quadrilateral` AND appends to
                     `dataset/roi_groundtruth/<video_id>.json`. Trim
                     detection itself is gated — modal scope is ROI
                     confirmation only until detector hits ≥99% on
                     truly-unseen videos. See `docs/TODO.md`.
                     Split into 7 ES modules:
    index.js           Public entry — exports `openAutoTrimModal`,
                       wires button bindings + window resize/Escape
                       listeners. Importing this module arms the
                       interaction layer.
    state.js           `els` DOM refs + `state` session object +
                       DEFAULT_CORNERS + CORNER_LABELS constants.
                       No side effects.
    modal.js           `closeModal()` — extracted so `api.js` can
                       call it without an import cycle through
                       `index.js`.
    log.js             `log` + `clearLog` + `setLoading` UI helpers.
    canvas.js          `redraw` + `drawCornerZooms` + `loadImage` +
                       corner-drag handlers. Mousedown / window
                       mousemove / mouseup listeners wired at import
                       time so dragging works the moment the module
                       loads.
    info_panel.js      `updateInfoPanel` — video name, method tag,
                       confidence, source kind, edit badge, top-3
                       NN list. Read-only side: consumes
                       `state.detectorResult`, writes the DOM.
    api.js             Backend HTTP calls: `loadRefframeAndDetect`,
                       `refreshGroundtruthCount`, `onConfirmClick`,
                       plus the small `buildQuery` / `videoIdentBody`
                       helpers.
  project_io.js      Save / Load Project + load modal.
  render.js          startRender + pollRender + cancel + intro-style
                     mutual exclusion + Open output folder.
  scoreboard_preview.js
                     Auto-attaches JASSUB (libass-WASM) to the `<video>`
                     element as soon as the source reports
                     `loadedmetadata`. Feeds it the *same* .ass file the
                     render pipeline burns in (fetched from
                     `POST /api/preview/scoreboard.ass`), so the preview
                     overlay is byte-identical to the final output.
                     Refresh is debounced 250 ms on info / score changes
                     via `syncScoreboardPreview` — name keystrokes
                     collapse to a single fetch.
  vendor/jassub/     Vendored JASSUB build (libass via WebAssembly,
                     ~4.4 MB) — loader, worker, two WASM variants,
                     default.woff2 (Liberation Sans, Arial metric clone
                     w/ Vietnamese coverage). Served as static files.
  styles.css         A few @apply shorthands plus plain-CSS fallbacks
                     for the Tailwind classes (the CDN's @apply support
                     is patchy on older builds).

assets/avatars/      Player photos as flat files: <Name>.{png,jpg,jpeg,webp}
                     `_default.jpg` ships as the placeholder silhouette.
assets/backgrounds/  Operator-created on demand. Drop an image here
                     and point `config.outro_bg_path` at it to override
                     the freeze-frame outro bg. Empty config → freeze
                     frame; freeze-frame extraction fails → solid dark.
assets/branding/     `logo.png` / `logo.jpg` (operator-supplied) +
                     auto-generated `stinger_in.mp4` / `stinger_out.mp4`
                     + `stinger.manifest.json` cache files. The manifest
                     captures every input that affects pixels (resolution,
                     fps, brand_color, channel_name, replay_label, logo +
                     sound mtimes, in/out durations); cache is reused iff
                     the manifest snapshot matches.
assets/sounds/       Optional music beds, all wired through the shared
                     `music_input_args` + `music_filter_chain` helpers
                     (loop + cap + volume + afade in/out). Defaults:
                     `intro.mp3` for the cinematic intro AND the outro
                     (0.3/0.5 s and 0.5/1.0 s fades respectively);
                     `slow_motion.mp3` reused for every slow-mo replay
                     spliced into main; `stinger_swoosh.wav` for the
                     stinger transition clips. Any missing file →
                     silent fallback for that slot.
assets/models/       Trained ML models (gitignored, regenerable). Holds
                     `roi_seg.pt` — YOLOv8-seg fine-tuned on
                     `dataset/yolo_seg/`. Re-train via
                     `scripts/train_roi_seg.py` (~1.3 min on RTX 5060 Ti).
                     Lazy-loaded by `backend/roi_yolo.py`; missing file
                     → tier 0 silently skips and pipeline falls to ORB.
videos/              Source MP4s (gitignored).
projects/            Saved project JSON files (gitignored).
output/              Final rendered MP4s + per-render sidecars:
                     `<name>.groundtruth.json` + `<name>.refframe.png`
                     written by `export_groundtruth` in `_finalize`.

temp/                Runtime caches and per-job intermediates (gitignored,
                     fully regenerable). `run.bat` clears the volatile
                     parts on every startup so behaviour is reproducible.
  refframes/           Cache for `_extract_refframe` +
                       `_extract_multi_refframes` (single midpoint + 5
                       evenly-spaced frames per video, JPEG @ max_w=960).
                       Keyed by `_video_identity = sha1(path|size|mtime)
                       [:16]`. Wiped on every `run.bat` startup.
  <job_id>/            Per-render scratch (intro.mp4, main.mp4,
                       outro.mp4, .ass, concat.txt). Deleted on
                       successful `_finalize`; preserved on failure so
                       ffmpeg inputs are inspectable.

runs/                 YOLO training output history (gitignored). Each
                     `runs/segment/roi_seg-N/` is one full training run
                     written by ultralytics — checkpoints, validation
                     plots, args.yaml, results.csv. Operator only needs
                     the latest; prune older runs after `train_roi_seg.py`
                     copies `best.pt` to `assets/models/roi_seg.pt`. None
                     of this is consumed at runtime.

dataset/             Auto-accumulated training corpus (gitignored). TWO
                     parallel datasets serving TWO different ML tasks —
                     don't conflate. See "Dataset accumulation" section
                     below for the auto-archive flows that fill these.
  manifest.json        Index of `<slug>/` entries appended per render
                       (schema v1). Downstream eval scripts enumerate
                       via this rather than scanning folders.
  <slug>/              One per successful render; `slug =
                       <sanitised_project>_<YYYYMMDD_HHMMSS>`. Contains
                       hardlinked source<.ext> + output.mp4, copied
                       groundtruth.json + refframe.png, verbatim
                       project.json snapshot, auto-filled notes.md.
                       Used for auto-trim algorithm development
                       (rally detection, segment timing).
  roi_groundtruth/     `<video_id>.json` + sibling `<video_id>.jpg`
                       per ROI confirmation. `video_id = sha1(absolute
                       video path)`. Each json keeps `latest_corners`
                       (canonical truth for that video) + `history[]`
                       of every confirm with the detector's proposal
                       at that time → algorithm-drift measurable from
                       this log alone. Read directly by ORB / HSV tiers
                       on every detect; YOLO consumes via the build
                       script below.
  yolo_seg/            YOLO segmentation format dataset (regenerable).
                       Built from roi_groundtruth/ via
                       `scripts/build_yolo_dataset.py`; deterministic
                       80/20 train/val split by hash.

config.json          Encoder + paths + intro tuning. See ConfigClass
                     in backend/config.py for accepted keys.

scripts/             Operator-triggered tooling, organized by purpose.
                     None of this is imported by the running backend —
                     it's all standalone CLI driven by the operator
                     (retrain, regression test, diagnostic viz).
  build_yolo_dataset.py    Convert dataset/roi_groundtruth/ → YOLO
                           segmentation format under dataset/yolo_seg/.
                           Multi-frame extraction (5 frames/video at
                           10/30/50/70/90% duration). Flags:
                           --search-path / --alias / --frames-per-video.
  train_roi_seg.py         Fine-tune YOLOv8n-seg on dataset/yolo_seg/.
                           Copies best.pt → assets/models/roi_seg.pt.
                           ~1.3 min on RTX 5060 Ti for ~50-entry dataset.
  test_roi_detector.py     LOO regression: runs detect_roi_multiframe
                           on each groundtruth entry with that entry
                           excluded from classical-CV NN lookup. Prints
                           per-entry corner error + method tag. Honest
                           for classical tiers; YOLO still trained on
                           all entries (separate retrain to fully
                           leave-one-out).
  analyze_detector_errors.py
                           Reads history[] from every groundtruth entry,
                           summarizes per-method-tier error breakdown.
                           Useful for "where is the detector failing
                           in production" diagnostic between retrain
                           milestones.
  debug_roi_overlay.py     Render 3-panel diagnostic (overlay + blue
                           mask + red mask) for one entry. CLI takes a
                           video_id prefix.
  dry_run_render.py        Print a render plan without encoding: source
                           metadata, trim/kept breakdown, replay
                           inserts, playlist composition + NVDEC
                           pre-concat trigger, estimated final
                           duration. Takes a project name or JSON path;
                           one read-only ffprobe is the only external
                           call.
  spike/                   Historical Phase-0 (rally detection) spike
                           scripts. Not part of any current code path.
                           Kept for reference when Phase 1b (trim
                           detection backend) is eventually unblocked.
                           Outputs went to scripts/spike_out/ (deleted);
                           PHASE0_REPORT.md preserved under
                           docs/spike_archive/.

docs/
  PROGRESS.md, TODO.md, ROADMAP.md, README.md, AUTO_TRIM_DISCUSSION.md
  spike_archive/
    PHASE0_REPORT.md   Locked recipe + parameter sweep from the Phase 0
                       rally-detector spike. Restore reading material
                       when Phase 1b is unblocked.
```

## Render pipeline at a glance

`backend.server.routes_render.start_render` (POST /api/render) builds a `RenderPlan`,
spawns a thread, and returns a job id. The thread runs:

```
run_render(plan):
  ctx = _prepare_context(plan)        # probe src, kept_segments,
                                       # remap score events, alloc job_dir,
                                       # build stage weight table
  _intro_stage(ctx)                   # cinematic OR text intro → intro.mp4
                                       # 4-avatar layout when doubles
  _main_stage(ctx)                    # scoreboard + per-highlight 50%-speed
                                       # replay spliced after each real-time
                                       # occurrence, bracketed by branded
                                       # stinger-in / stinger-out clips,
                                       # with SLOW MOTION badge → main.mp4
  _outro_stage(ctx)                   # extract last frame of main.mp4,
                                       # blur + dim → bg; THANK YOU card
                                       # over it; fade-to-black tail → outro.mp4
                                       # (skipped when main is disabled)
  _finalize(ctx)                      # concat → output/<name>.mp4
                                       # then shutil.rmtree(job_dir) on success
```

All stages encode to NVENC h264 + AAC at TARGET_AUDIO_RATE so the
final concat demuxer stitches them without re-encoding.

`RenderContext.make_progress(stage_name)` returns a callback that
reads the live `completed_weight` at call time, so the running 0..1
progress fraction stays correct as stages advance.

## Dataset accumulation

Every operator action that produces labelled output is auto-archived
for ML training without changing the workflow. **TWO independent
datasets serving TWO different tasks — do NOT conflate them.**

### Post-render archive → `dataset/<slug>/` (fed by RENDER)

Trigger: every successful render. Two-step pipeline at the tail of
`_finalize` in `renderer/orchestrator.py`:

```
1. export_groundtruth(ctx, output_mp4)    # backend/groundtruth.py
   → output/<name>.groundtruth.json       (project snapshot + kept_segments
                                            + source video metadata + stats)
   → output/<name>.refframe.png           (midpoint of first kept segment —
                                            guaranteed to have a player)

2. archive_to_dataset(ctx, output_mp4)    # backend/dataset.py
   → dataset/<slug>/source.<ext>          (hardlink; copy fallback)
   → dataset/<slug>/output.mp4            (hardlink; copy fallback)
   → dataset/<slug>/project.json          (verbatim, reloadable in GUI)
   → dataset/<slug>/groundtruth.json      (copy of output/ sidecar)
   → dataset/<slug>/refframe.png          (copy of output/ sidecar)
   → dataset/<slug>/notes.md              (auto-filled match info +
                                            manual labels + rally stats +
                                            dead-gap distribution +
                                            highlights + permanent caveat
                                            about noisy manual trims)
   → dataset/manifest.json                (append entry with stats)
```

Used by the auto-trim algorithm spike (rally detection, segment
timing). Best-effort — failures append warnings to `RenderState.message`
but never abort the render; the mp4 in `output/` is canonical
regardless of archive success. Hardlinks make this near-zero disk cost
on same-volume NTFS (the existing ~17.8 GB dataset takes only marginal
extra space).

**Manual trims are NOT reliable ground truth.** `notes.md` auto-includes
a permanent caveat: operator-marked `trim_segments` are a subjective
partial label set — no duration threshold, can include short gaps,
can miss long ones depending on what the operator happened to scrub
past. Auto-trim is expected to be strictly more thorough; do NOT tune
it to match this label set. Real precision/recall needs eyeball QA on
rendered output, or one exhaustively-marked reference video.

### ROI confirmation → `dataset/roi_groundtruth/` (fed by Auto Trim modal)

Trigger: every Confirm click in the "⚡ Auto Trim" modal. Single endpoint
`POST /api/auto_trim/confirm_roi`:

```
→ dataset/roi_groundtruth/<video_id>.json   (latest_corners + history[])
→ dataset/roi_groundtruth/<video_id>.jpg    (refframe copy)
```

`video_id = sha1(absolute_source_path)` — re-opening the same file
always lands on the same record. `history[]` appends every confirm
along with the detector's proposal at that time → algorithm accuracy
drift is measurable from this log alone. Re-confirming the same
video **overwrites** `latest_corners` (use carefully — a bad re-confirm
poisons the label) and appends to history.

Three downstream consumers of this directory:

- **ORB + HSV fallback tiers** in `backend/roi/` query this dir
  directly on every detect call (via
  `learned_nn_tier._load_groundtruth_examples`). Confirms take effect
  on the **next Auto Trim click** — no retrain needed.
- **YOLOv8-seg (top-priority tier)** consumes via
  `scripts/build_yolo_dataset.py` → `dataset/yolo_seg/` →
  `scripts/train_roi_seg.py` → `assets/models/roi_seg.pt`. **Manual
  trigger only** — confirming in the modal does NOT retrain the model.
  Operator re-runs the two scripts at every milestone (~1.3 min on
  RTX 5060 Ti for ~40 entries).
- **`GET /api/auto_trim/groundtruth_count`** for milestone progress
  tracking (number of unique videos confirmed).

This dataset is for **ROI auto-detection only**, not trim detection.
The ROI milestone gates everything downstream — see `docs/TODO.md`
for unlock criteria (≥99% on truly-unseen videos unlocks Phase 1b
trim detection backend).

## Key invariants

- **Source video can be anywhere.** `project.info.video_file` is either
  a bare filename inside `videos/` or an absolute path picked via the
  native file dialog. `_resolve_source` handles both. The frontend
  uses session sha1 tokens to stream files outside videos/ without
  exposing the path.

- **Score events are ACTIONS, not states.** Each event records who
  scored (`who: 1 | 2`) at a video timestamp. The p1/p2/set fields are
  a derived cache that the frontend recomputes on every change by
  replaying all actions in chronological order. Don't trust the cache
  blindly — re-derive when in doubt.

- **`_default.jpg` is the avatar fallback.** When a player photo is
  missing the cinematic intro substitutes the shipped silhouette and
  stamps a hint into `RenderState.message`. Only when EVEN the
  default is missing do we fall back to the text intro.

- **Scoreboard has one source of truth.** Both the burned-in render
  AND the live preview overlay call `build_scoreboard_ass_text` in
  `backend/ass/scoreboard/builder.py`. The render pipeline writes it
  to a file for ffmpeg's `ass=` filter; the preview endpoint streams
  it to JASSUB-in-browser. If you change how the scoreboard looks,
  change one of the `backend/ass/scoreboard/emit_*.py` modules and
  both paths update — never duplicate the layout logic on the frontend.

- **Doubles row-name rule is one function.** `resolve_row_names` in
  `backend/ass/scoreboard/events.py` is the *only* place that decides
  how the 4 raw names (p1/p2/p3/p4) collapse to the 2 scoreboard row
  labels.
  Both the scoreboard renderer AND `_intro_stage` (for the cinematic
  intro's name line) call into it. The frontend mirrors the rule for
  Live Score panel labels only — actual rendered output always goes
  through the backend.

- **Main render splices slow-mo replays inline.** Every highlight
  becomes a 50%-speed replay inserted into main *right after* its
  real-time occurrence. `build_replay_plan` + `build_main_playlist` in
  `renderer/replays.py` produce a chronological list of (slice | stinger_in |
  replay | stinger_out) entries; `remap_events_with_replays` shifts
  score events past each insert point by `replay_dur + 2 *
  stinger_dur`. Total main duration changes — the scoreboard
  `total_duration` must use the post-replay, post-stinger value, not
  the trimmed-source sum.

- **Stinger pair is manifest-cached.** `get_or_build_stinger_pair`
  in `backend/stinger_builder.py` writes `assets/branding/stinger_in.mp4`
  + `stinger_out.mp4` + a sibling `stinger.manifest.json`. The manifest
  records every input that affects pixels (W/H/fps, brand_color,
  channel_name, replay_label, in/out durations, logo + sound paths +
  mtimes). On the next render the manifest is rebuilt and compared by
  exact equality — cache hit returns the existing mp4s untouched; any
  drift triggers a rebuild. Editing the logo or sound file in place is
  detected via mtime. All three cache files are gitignored.

- **Stinger logo auto-detects.** `find_brand_logo()` in
  `backend/stinger_builder.py` prefers `config.brand_logo_path` when
  it points to an existing file, but falls back to scanning
  `assets/branding/` for the first image (sorted by name, skipping
  `_*` and `stinger_*.mp4`). Means the operator can drop any image
  named anything (e.g. `Nguyễn Bá Thảo.jpg`) in `assets/branding/`
  and it'll be picked up as the logo. The logo is centre-cropped to
  a square and circular-alpha-masked at render time — same idiom as
  the cinematic intro's avatar processing.

- **Temp survives errors.** `_finalize` deletes `temp/<job_id>/` ONLY
  on success. Failed renders leave the .ass / .mp4 / .concat.txt
  intermediates around so the operator (or you) can inspect the
  ffmpeg inputs that broke.

- **Vietnamese diacritics are NFC-normalised on lookup.** `find_avatar`
  case-insensitive + NFC compare. Project schema is UTF-8 throughout.

## Things that surprise people

- **NVENC/AAC/hwaccel helpers live in `ffmpeg_runner.py`**, not
  renderer/stages.py. Both renderer/stages.py and intro_builder.py import them.
  Same with `TARGET_AUDIO_RATE`.

- **`zoompan` doesn't support `t` in expressions** — only `on` (output
  frame index). The cinematic intro pre-computes `0.012 / fps` so the
  bg ends at ~1.08x zoom regardless of source fps.

- **ASS alpha is inverted**: `&H00&` = fully opaque, `&HFF&` = fully
  transparent. Most overlays use `0C` (≈95 % opaque).

- **`crop=min(iw\\,ih)` needs the comma escaped** when used inside
  `filter_complex`. `r(X\\,Y)` likewise inside `geq` expressions.

- **Vietnamese filenames** in `assets/avatars/` are stored NFC. Windows
  defaults to NFC, so copy-paste from File Explorer works. macOS NFD
  would mismatch — `_norm` in `avatars.py` normalises to NFC on lookup
  to defend against this.

- **The cinematic intro extracts a single frame as a PNG** before the
  main filter graph runs. We could decode live source for 4 s but
  zoompan + gblur per frame would be wasteful. Two-pass approach is
  ~10× faster.

## When making changes

- Don't add new ASS builders to `ass_builder.py` — that file is gone.
  Pick one of `ass/scoreboard/`, `ass/intro.py`, `ass/outro.py`,
  `ass/badges.py`, or create a new module under `ass/` and re-export
  from `ass/__init__.py`.

- Don't duplicate `nvenc_args()` / `aac_args()` / `hwaccel_input_args()`
  — import from `ffmpeg_runner`.

- The frontend is split into ES6 modules under `frontend/*.js`. Each
  module wires its own DOM event listeners at load time. Cross-cutting
  state lives in `state.js`; cross-cutting orchestration (`syncAllUI`,
  `undo`) lives in `app.js`. When you need a dependency that would
  introduce a circular import, register a callback (see
  `setSyncAllUI` / `setSyncInfoFromInputs` in `project_io.js` and
  `render.js`).

- When changing render output (scoreboard layout, intro animation,
  etc.), capture reference outputs first and verify byte-identical
  output for unaffected cases before/after — see
  `_capture_ref.py` / `_verify_ref.py` patterns from prior commits.

## Don't

- Don't add tests next to the modules; if you add tests put them in a
  `tests/` directory. ~209 tests live there; pure-logic only (segment
  math, builder smoke, playlist + remap, quad geometry, job-registry
  eviction, helper formatters), no ffmpeg execution. `tests/conftest.py`
  creates `temp/` so the pinned basetemp works on fresh checkouts (CI).

- Don't commit videos, project JSONs, output mp4s, or temp/. They're
  in `.gitignore` already; if a `git status` shows them as new files
  the gitignore is broken and that's a bug.

- Don't run destructive git commands (`reset --hard`, `push --force`,
  `checkout --`) without asking the user first. The repo is on
  `main` only; there is no recovery branch.

- Don't auto-commit. The user has a memory rule "only commit when
  explicitly asked". Stage changes and wait for approval.

- Don't introduce new dependencies without approval. The deps list is
  intentionally minimal (FastAPI + uvicorn + python-multipart +
  pydantic). ffmpeg / ffprobe / tkinter are external.

## Where things live (cheat sheet)

| You want to change…                  | File |
|---|---|
| Encoder, preset, quality             | [config.json](config.json) |
| ROI warmup at server start (CPU/RAM vs first-click speed) | [config.json](config.json) `roi_warmup_enabled` (operator keeps it `false`) |
| Intro duration / avatar size / blur / sound | [config.json](config.json) (`intro_*` keys) |
| Outro on/off, text, duration, bg, sound | [config.json](config.json) (`outro_*` keys) |
| Music bed shared helper (loop + fade + volume) | `music_input_args` / `music_filter_chain` in [backend/ffmpeg_runner.py](backend/ffmpeg_runner.py) |
| Scoreboard layout / colours          | [backend/ass/scoreboard/](backend/ass/scoreboard/) — `emit_*.py` for Dialogue emit, `geometry.py` for layout + style block |
| Doubles combined-name rule           | `resolve_row_names` in [backend/ass/scoreboard/events.py](backend/ass/scoreboard/events.py) + `_combine_doubles_name` in [backend/ass/common.py](backend/ass/common.py) |
| Cinematic intro filter graph         | [backend/intro_builder.py](backend/intro_builder.py) — branches on `is_doubles` for the 4-avatar layout |
| Cinematic intro text overlays        | `build_cinematic_intro_ass` in [backend/ass/intro.py](backend/ass/intro.py) |
| Outro card layout                    | `build_outro_card_ass` in [backend/ass/outro.py](backend/ass/outro.py); ffmpeg side in `render_outro_card` in [backend/renderer/](backend/renderer/) |
| Slow-mo replay plan / playlist       | `build_replay_plan` / `build_main_playlist` / `remap_events_with_replays` in [backend/renderer/](backend/renderer/) |
| Slow-mo replay music                 | `replay_sound_path` / `replay_sound_volume` in [config.json](config.json); resolved via `config.replay_sound_path` and consumed by `render_main_with_scoreboard` in [backend/renderer/](backend/renderer/) |
| SLOW MOTION badge                    | `build_slow_motion_badge_ass` in [backend/ass/badges.py](backend/ass/badges.py) |
| Auto-stinger generation              | `get_or_build_stinger_pair` in [backend/stinger_builder.py](backend/stinger_builder.py) |
| Brand identity (color / logo / channel name) | [config.json](config.json) `brand_color` / `brand_logo_path` / `channel_name` / `stinger_replay_label` / `stinger_duration_seconds` / `stinger_sound_path` |
| Score logic (replay, set wins)       | [frontend/score.js](frontend/score.js) — `recomputeAllEvents`, `scorePoint` |
| Avatar lookup rules                  | [backend/avatars.py](backend/avatars.py) |
| Render-time pipeline orchestration   | `_intro_stage` / `_main_stage` / `_outro_stage` / `_finalize` in [backend/renderer/](backend/renderer/) |
| Post-render groundtruth sidecar      | `export_groundtruth` in [backend/groundtruth.py](backend/groundtruth.py) — called from `_finalize` |
| Post-render dataset archive          | `archive_to_dataset` in [backend/dataset.py](backend/dataset.py) — called from `_finalize` after `export_groundtruth` |
| Auto-filled `notes.md` template      | `build_notes_md` in [backend/dataset.py](backend/dataset.py) |
| ROI auto-detect (multi-tier pipeline)| `detect_roi_multiframe` in [backend/roi/detector.py](backend/roi/detector.py); tier modules under [backend/roi/](backend/roi/); YOLO loader in [backend/roi_yolo.py](backend/roi_yolo.py) |
| ROI confirm → groundtruth append     | `/api/auto_trim/confirm_roi` in [backend/server/routes_auto_trim.py](backend/server/routes_auto_trim.py); files land in `dataset/roi_groundtruth/<video_id>.{json,jpg}` |
| YOLO ROI training                    | `scripts/build_yolo_dataset.py` then `scripts/train_roi_seg.py` → `assets/models/roi_seg.pt` |
| Auto Trim modal (frontend)           | [frontend/auto_trim/](frontend/auto_trim/) — public entry [frontend/auto_trim/index.js](frontend/auto_trim/index.js); button hosted in [frontend/trims.js](frontend/trims.js) |
| Add a new HTTP endpoint              | pick the matching `backend/server/routes_*.py` (videos / projects / render / auto_trim), or [backend/server/app.py](backend/server/app.py) for cross-cutting endpoints |
| Add new project field                | [backend/models.py](backend/models.py) `ProjectInfo`, then frontend `project.info` schema in [frontend/state.js](frontend/state.js), then UI input in [frontend/index.html](frontend/index.html) |
