# TODO

Actionable items, ordered roughly by priority. Each entry has a small
indicator: 🟢 quick (<1h), 🟡 medium (~half-day), 🔴 large (1+ day).

## Later (P2 — broadcast-quality polish)

- [ ] 🟡 **Country / club flags** next to player names in the scoreboard.
      Source: small PNGs in `assets/flags/`. Burn via `overlay` filter at
      fixed positions inside the panel.
- [ ] 🟡 **Tournament logo** on the intro card and bottom corner.
      Add `info.logo_path` to the project schema.
- [ ] 🟡 **Theme presets** for the scoreboard (colour palette per
      tournament). Persist in `projects/<name>.json`.
- [ ] 🔴 **Audio leveling / ducking** during slow-mo (currently hard
      atempo=0.5 sounds robotic). Try crossfade or use `rubberband`.
- [ ] 🔴 **Per-highlight export** as separate MP4 files (in addition to
      the combined reel).
- [x] 🔴 **Animated set transition card** — done.
      `_emit_recap_cards` in `backend/ass/scoreboard.py` shows
      `SET N` + final score (4 s) immediately followed by a `SET N+1`
      transition (4.5 s) for every non-match-ending set boundary.

## Dev infra

- [ ] 🟢 Pin Python version in `pyproject.toml` (currently bare requirements.txt).
- [x] 🟢 Add `pytest` smoke tests for `kept_segments_from_trims`,
      `remap_score_event_to_trimmed`, and `_detect_active`. Done —
      93 tests in `tests/`, includes segment math + scoreboard event
      walk + main playlist + stinger bracket + event remap.
- [ ] 🟡 Add a `--dry-run` mode to the renderer that prints the full
      ffmpeg command instead of executing.
- [ ] 🟡 GitHub Actions: lint (ruff) + import-check on push.

## Bugs / questions

- [ ] 🟢 `drawtext` on the intro card AND the auto-stinger clip uses
      no `fontfile=` — relies on ffmpeg's default font lookup which can
      fail on some Windows builds, and likely won't render Vietnamese
      diacritics in `stinger_text`. Add a bundled font in `assets/` and
      reference it explicitly from both call sites.
- [ ] 🟢 If user names contain `'` (apostrophe), the .ass escape may
      double-escape inside `Dialogue:` lines. Add a unit test.
- [ ] 🟡 NVDEC session limit on consumer GPUs is ~8. The main render
      opens N inputs (one per kept segment). Investigate whether ffmpeg
      keeps all decoders open simultaneously or pools them. If many trims
      → many sessions, fall back to filter-trim path.

## Post-v1.5 cleanup (from 2026-05-15 audit)

Surfaced when reviewing the codebase after the auto-stinger ship. None
are urgent — just consolidating before the next feature.

- [ ] 🟢 **Drop dead `stinger_text` plumbing.** Config key
      (`config.json:31`), property (`backend/config.py:180-181`), and
      param threaded through `get_or_build_stinger_pair` are never
      rendered — `channel_name` + `stinger_replay_label` replaced this
      role. Either delete the key + param + README mention (preferred,
      reduces config surface), or wire it into `ass/stinger.py` as a
      third text line. README currently flags it as "legacy, unused".
- [ ] 🟢 **Retire `docs/CINEMATIC_INTRO_PLAN.md`.** The cinematic intro
      shipped long ago; the doc still describes a 5–8 s clip
      "transitioning smoothly into the highlight reel" — both stale.
      Move to `docs/archive/` (keep history) or delete.
- [ ] 🟢 **Sync `intro_duration_seconds` defaults.** `backend/config.py:79`
      falls back to `6.0`, `config.json:15` sets `4.0`. config.json
      wins at runtime but a dev reading `config.py` gets confused.
      Make the property default `4.0`.
- [ ] 🟢 **`outro_bg_path` points at a missing file.** `config.json:23`
      = `assets/backgrounds/outro_bg.jpg`; that directory doesn't
      exist. `_optional_asset` returns None gracefully so the
      `gblur(last frame of main)` fallback runs — but config implies
      an asset that isn't shipped. Either drop the key (rely on the
      frame-extraction fallback only) or ship a real bg jpg.
- [ ] 🟡 **Test gaps** identified during the audit:
      - No test covers the doubles-intro fallback (< 4 photos →
        text intro) in `_intro_stage` (`backend/renderer.py:959-963`).
      - No test covers `get_or_build_stinger_pair` cache-hit path
        (re-call with same args returns existing paths without
        re-rendering). Pure playlist + remap math is well covered.
