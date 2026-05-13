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

## Repo layout

```
backend/
  server.py          FastAPI endpoints (videos, projects, render jobs,
                     avatars). One file. ~545 lines.
  renderer.py        Render orchestrator (RenderPlan + RenderContext +
                     stage helpers). Owns the public `run_render(plan)`.
  intro_builder.py   Cinematic intro ffmpeg filter graph (avatars +
                     bg blur + Ken-Burns zoom + libass text).
  ffmpeg_runner.py   ffprobe + run_ffmpeg_with_progress + shared
                     NVENC / AAC / hwaccel arg helpers and the
                     TARGET_AUDIO_RATE / TARGET_AUDIO_CHANNELS
                     constants. Anything ffmpeg-related lives here.
  avatars.py         find_avatar / find_default_avatar / find_avatar_or_default
                     against assets/avatars/<Name>.<ext>.
  config.py          Reads config.json into typed properties.
  models.py          Pydantic ProjectInfo / ProjectData / RenderRequest.
  ass/
    __init__.py      Re-exports the public builders.
    common.py        Palette (C_*) + drawing primitives (_rect) +
                     text helpers (_trim_*, _ass_escape, _ass_rgb,
                     _bgr) + _fmt_time. Imported by every other ass/*.
    scoreboard.py    Live + final scoreboard, recap/transition cards,
                     GP/MP/DEUCE flag. Internally split into
                     _Geometry + _AssetText + _emit_* helpers. Exports
                     `resolve_row_names` so doubles' combined-name rule
                     (last 2 words of each partner) is shared by every
                     consumer.
    intro.py         Text-only fallback intro + libass companion for
                     the cinematic intro. Same builder serves singles
                     (2 avatars) and doubles (4 avatars laid out as 2
                     pairs); caller pre-combines names for doubles.
    badges.py        Top-left HIGHLIGHT, FULL MATCH and SLOW MOTION
                     badges. SLOW MOTION takes a list of (start, end)
                     ranges so one .ass covers every spliced replay.
    intermission.py  3-second typography bridge between highlight reel
                     and main match (when `intermission_enabled` is
                     true). Headline + tournament + players over a
                     dim bg image (optional, falls back to lavfi color).
    outro.py         5-second closing card after main match. libass
                     overlay over the blurred + dimmed last frame of
                     main.mp4; fades to black in the final second.
                     Silent audio for concat-demuxer compatibility.
    transition.py    Gold-sweep bridge — fallback when intermission is
                     disabled in config.

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
                     browseForVideo / external token registration).
  score.js           Score logic (recompute, sync from time, score,
                     delete) + score-panel UI + events list.
  highlights.js      All highlight ops + list UI.
  trims.js           All trim ops + list UI.
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
assets/backgrounds/  Optional `intermission_bg.jpg` for the intermission
                     card. Missing → renderer falls back to lavfi color.
assets/sounds/       Optional `intermission_boom.mp3` impact sound for
                     the intermission card. Missing → silent audio.
                     (Outro is always silent — no sound asset.)
assets/backgrounds/  Also accepts an optional `outro_bg.jpg` used when
                     `_outro_stage` can't extract main's last frame
                     (or when the operator wants a fixed bg).
videos/              Source MP4s (gitignored).
projects/            Saved project JSON files (gitignored).
output/              Final rendered MP4s.
temp/                Per-job intermediates; cleaned up on success only.

config.json          Encoder + paths + intro tuning. See ConfigClass
                     in backend/config.py for accepted keys.

docs/
  PROGRESS.md, TODO.md, ROADMAP.md, CINEMATIC_INTRO_PLAN.md
```

## Render pipeline at a glance

`backend.server.start_render` (POST /api/render) builds a `RenderPlan`,
spawns a thread, and returns a job id. The thread runs:

```
run_render(plan):
  ctx = _prepare_context(plan)        # probe src, kept_segments,
                                       # remap score events, alloc job_dir,
                                       # build stage weight table
  _intro_stage(ctx)                   # cinematic OR text intro → intro.mp4
                                       # 4-avatar layout when doubles
  hl = _highlight_stage(ctx)          # render highlight reel → highlight.mp4
                                       # (no per-clip slow-mo — straight encode)
  _bridge_stage(ctx, hl)              # intermission card (3 s typography
                                       # over dim bg) when enabled; gold-sweep
                                       # 0.8 s fallback otherwise
  _main_stage(ctx)                    # scoreboard + (optional FULL MATCH
                                       # badge) + per-highlight 50%-speed
                                       # replay spliced after each real-time
                                       # occurrence, with SLOW MOTION badge
                                       # → main.mp4
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
  `backend/ass/scoreboard.py`. The render pipeline writes it to a file
  for ffmpeg's `ass=` filter; the preview endpoint streams it to
  JASSUB-in-browser. If you change how the scoreboard looks, change
  `scoreboard.py` and both paths update — never duplicate the layout
  logic on the frontend.

- **Doubles row-name rule is one function.** `resolve_row_names` in
  `backend/ass/scoreboard.py` is the *only* place that decides how the
  4 raw names (p1/p2/p3/p4) collapse to the 2 scoreboard row labels.
  Both the scoreboard renderer AND `_intro_stage` (for the cinematic
  intro's name line) call into it. The frontend mirrors the rule for
  Live Score panel labels only — actual rendered output always goes
  through the backend.

- **Main render splices slow-mo replays inline.** Every highlight
  becomes a 50%-speed replay inserted into main *right after* its
  real-time occurrence. `build_replay_plan` + `build_main_playlist` in
  `renderer.py` produce a chronological list of (slice | replay)
  entries; `remap_events_with_replays` shifts score events past each
  insert point by the replay's final duration. Total main duration
  changes — the scoreboard `total_duration` must use the post-replay
  value, not the trimmed-source sum.

- **Intermission card suppresses the FULL MATCH top-left badge.** When
  `config.intermission_enabled` is true, `_main_stage` passes
  `full_match_badge_ass=None`, since the intermission card already
  signals "we're entering the main match". Keeping both would read as
  duplicate for ~18 s.

- **Temp survives errors.** `_finalize` deletes `temp/<job_id>/` ONLY
  on success. Failed renders leave the .ass / .mp4 / .concat.txt
  intermediates around so the operator (or you) can inspect the
  ffmpeg inputs that broke.

- **Vietnamese diacritics are NFC-normalised on lookup.** `find_avatar`
  case-insensitive + NFC compare. Project schema is UTF-8 throughout.

## Things that surprise people

- **NVENC/AAC/hwaccel helpers live in `ffmpeg_runner.py`**, not
  renderer.py. Both renderer.py and intro_builder.py import them.
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
  main filter graph runs. We could decode live source for 6 s but
  zoompan + gblur per frame would be wasteful. Two-pass approach is
  ~10× faster.

## When making changes

- Don't add new ASS builders to `ass_builder.py` — that file is gone.
  Pick one of `ass/scoreboard.py`, `ass/intro.py`, `ass/badges.py`,
  `ass/transition.py`, or create a new module under `ass/` and
  re-export from `ass/__init__.py`.

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
  `tests/` directory. There are currently none.

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
| Intro duration / avatar size / blur  | [config.json](config.json) (`intro_*` keys) |
| Intermission on/off, text, bg, sound | [config.json](config.json) (`intermission_*` keys) |
| Outro on/off, text, duration, bg     | [config.json](config.json) (`outro_*` keys) |
| Scoreboard layout / colours          | [backend/ass/scoreboard.py](backend/ass/scoreboard.py) |
| Doubles combined-name rule           | `resolve_row_names` / `_combine_doubles_name` in [backend/ass/scoreboard.py](backend/ass/scoreboard.py) + [backend/ass/common.py](backend/ass/common.py) |
| Cinematic intro filter graph         | [backend/intro_builder.py](backend/intro_builder.py) — branches on `is_doubles` for the 4-avatar layout |
| Cinematic intro text overlays        | `build_cinematic_intro_ass` in [backend/ass/intro.py](backend/ass/intro.py) |
| Intermission card layout             | `build_intermission_card_ass` in [backend/ass/intermission.py](backend/ass/intermission.py); ffmpeg side in `render_intermission_card` in [backend/renderer.py](backend/renderer.py) |
| Outro card layout                    | `build_outro_card_ass` in [backend/ass/outro.py](backend/ass/outro.py); ffmpeg side in `render_outro_card` in [backend/renderer.py](backend/renderer.py) |
| Highlight reel rendering             | `render_highlight_clip` in [backend/renderer.py](backend/renderer.py) |
| Slow-mo replay plan / playlist       | `build_replay_plan` / `build_main_playlist` / `remap_events_with_replays` in [backend/renderer.py](backend/renderer.py) |
| Slow-mo / HIGHLIGHT / FULL MATCH badge | [backend/ass/badges.py](backend/ass/badges.py) |
| Score logic (replay, set wins)       | [frontend/score.js](frontend/score.js) — `recomputeAllEvents`, `scorePoint` |
| Avatar lookup rules                  | [backend/avatars.py](backend/avatars.py) |
| Render-time pipeline orchestration   | `_intro_stage` / `_highlight_stage` / `_bridge_stage` / `_main_stage` / `_outro_stage` / `_finalize` in [backend/renderer.py](backend/renderer.py) |
| Add a new HTTP endpoint              | [backend/server.py](backend/server.py) |
| Add new project field                | [backend/models.py](backend/models.py) `ProjectInfo`, then frontend `project.info` schema in [frontend/state.js](frontend/state.js), then UI input in [frontend/index.html](frontend/index.html) |
