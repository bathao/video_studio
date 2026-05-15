# Release History

Where the project actually is, version by version. Anything beyond the
latest tag is "not decided yet" — when an operator-facing feature gets
planned, it gets a new section here.

Currently shipped: **v1.6** (manifest-cached stingers + pipeline lock-in).

## v1.6 — Manifest cache + pipeline lock-in (2026-05-15)

Polish + simplification on top of the v1.5 ship; no new operator
feature, no scope expansion.

- Stinger pair persists at fixed paths
  (`assets/branding/stinger_{in,out}.mp4`) with a sibling
  `stinger.manifest.json` snapshot of every input that affects pixels
  (W/H/fps, brand colour, channel name, replay label, in/out durations,
  logo + sound paths + mtimes). Re-renders with identical inputs are
  cache hits at zero ffmpeg overhead. Editing the logo or sound in
  place invalidates via mtime.
- Pipeline locked: removed the operator-facing "Main match + scoreboard"
  and "Slow-mo replays of highlights" checkboxes from the Render panel.
  The pipeline always runs intro (cinematic or text) → main with
  inline stinger-bracketed replays → outro; only the intro style is
  user-toggleable now.
- Dead config plumbing removed: `stinger_text` config key + builder
  param + manifest field. `channel_name` + `stinger_replay_label`
  already cover the on-screen text role.
- `outro_bg_path` cleared from `config.json` (was pointing at a
  shipped-but-missing file); the renderer's freeze-frame fallback is
  the supported path.
- `intro_duration_seconds` property default synced to `4.0` to match
  `config.json` (was `6.0`, masked by the runtime read).
- Retired `docs/CINEMATIC_INTRO_PLAN.md` (feature shipped in v1.0).

Tests: 109 passing — added `test_intro_fallback.py` (doubles photo-gate)
and `test_stinger_cache.py` (cache-hit + invalidation matrix).

## v1.5 — Branded stinger transitions (2026-05-15)

Each slow-mo replay in main is bracketed by a branded sting clip
([backend/stinger_builder.py](../backend/stinger_builder.py),
[backend/ass/stinger.py](../backend/ass/stinger.py)).

- IN (default 2 s) — blurred source-frame background + brand-colour
   wipe + diagonal light streak + circular logo + REPLAY label.
- OUT (default 0.6 s) — same visual reversed, no text. Quick wipe
   back to live action.
- Cached per `(source × W × H × fps)` in `assets/branding/`;
   regenerates automatically when any spec changes, or when the
   operator deletes the cached mp4s after editing branding config.
- Pipeline simplified at the same time: removed the upfront highlight
   reel, the intermission/transition bridge, and the FULL MATCH badge.
   Output is now strictly intro → main (with inline slow-mo replays
   bracketed by stingers) → outro.

Config: `brand_color`, `brand_logo_path`, `channel_name`,
`stinger_replay_label`, `stinger_duration_seconds`,
`stinger_out_duration_seconds`, `stinger_sound_path`.

Tests: 109 passing (post-v1.5 cleanup added the intro photo-gate
+ stinger cache-hit suites).

## v1.4 — Audio beds (2026-05-13)

Optional music tracks wired through a shared `music_input_args` +
`music_filter_chain` helper pair in
[backend/ffmpeg_runner.py](../backend/ffmpeg_runner.py): looped, capped
to clip duration, volume scaled, with afade in/out.

- `intro_sound_path` + `outro_sound_path` (defaults
   `assets/sounds/intro.mp3`, different fade windows so the outro
   sinks into the fade-to-black tail).
- `replay_sound_path` (default `assets/sounds/slow_motion.mp3`) reused
   for every slow-mo replay spliced into main.
- Intermission audio (still part of the pipeline at this point)
   upgraded from one-shot `-i` to the shared loop+fade pattern.

## v1.3 — Cinematic outro card (2026-05-12)

Closing card appended after main.mp4:

- Extracts the last frame of main.mp4, applies `gblur=sigma=30` +
   `eq=brightness=-0.3` → static backdrop.
- libass overlay: configurable headline (default "THANK YOU FOR
   WATCHING"), centred, 1 s fade-in.
- Final 1 s fades a full-frame black box on top so the video sinks to
   black before EOF — no xfade boundary because the first frame of
   outro matches the last frame of main, hiding the cut.
- Silent audio (`anullsrc`) so the concat-demuxer's stream layout
   check passes.

Config: `outro_enabled`, `outro_text`, `outro_duration_seconds`,
`outro_bg_path`.

## v1.2 — Live scoreboard preview + cancellable renders (2026-05-08)

- **Live scoreboard preview** — JASSUB (libass-WASM) attaches a canvas
   to the `<video>` element as soon as the source reports
   `loadedmetadata`. Browser overlay uses the SAME `.ass` text the
   render pipeline burns in (fetched via `POST /api/preview/scoreboard.ass`),
   so what you see while editing is byte-identical to what the render
   produces.
- **Cancellable renders** — `run_ffmpeg_with_progress` reads a
   `cancel_check` predicate per progress line and `proc.terminate()`s
   the child; `FFmpegCancelled` propagates up to `run_render` which
   marks the job `cancelled` (distinct from `error`). UI exposes a
   Cancel button while a render is in flight.

## v1.1 — Layout overhaul (2026-04-26)

Operator-facing UI rework — wider video panel, panels relaid out
around it so scoring + highlight marking can happen while watching at
a larger size.

## v1.0 — First release in regular use (2026-04-20)

End-to-end working tool. Single operator on Windows + RTX 5060 Ti can
drop a tripod recording in, watch the match with `A`/`D` scoring and
`H` highlight marking, and render a recap MP4 with intro, slow-mo
highlight reel, transition bridge, main match with burned-in
scoreboard, and final-score card.

The "highlight reel + transition + FULL MATCH badge" sequence was the
default pipeline through v1.4 — see v1.5 above for the simplification.

## Pre-1.0 milestones

Early commits before tagging discipline. Notable ones:

- Initial MVP with FastAPI backend + browser UI + scoreboard.
- Scoreboard redesigned to broadcast style (gold accents, set-tint
   columns, GP/MP/DEUCE flag).
- Intro ported from `drawtext` to libass for Vietnamese diacritics.
- Action-based scoring (`who: 1 | 2` per event; cache re-derived on
   every change).
- BO3 / 5 / 7 support with match-point awareness.
- Cinematic avatar intro (circular-masked player photos + Ken-Burns
   blur background) replacing the text-only title card.
- Doubles support (P3 + P4 inputs; combined-name rule
   `resolve_row_names`).
- Inline 50%-speed slow-mo replay of each highlight spliced into
   main.
- ASS-builder package split (`backend/ass/`) + frontend split into ES6
   modules (`frontend/*.js`).
- pytest suite added (currently 109 tests across 8 files).

## Non-goals (deliberately out of scope)

- Multi-user / multi-tenant editing — single-operator tool by design.
- General-purpose video editor — optimised for one workflow:
   table-tennis match recap.
- Mobile app — browser UI is enough; the heavy lifting is on the
   operator's desktop GPU.
- Cloud upload of source video — files stay local; uploads are the
   user's choice via the OS share sheet.

## What's next

Nothing planned yet. When a new feature is decided, it gets a new
section above with a target date or "shipped" stamp.
