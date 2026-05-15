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
      109 tests in `tests/`, includes segment math + scoreboard event
      walk + main playlist + stinger bracket + event remap + intro
      photo gate + stinger cache.
- [ ] 🟡 Add a `--dry-run` mode to the renderer that prints the full
      ffmpeg command instead of executing.
- [ ] 🟡 GitHub Actions: lint (ruff) + import-check on push.

## Bugs / questions

- [ ] 🟢 `drawtext` on the intro card uses no `fontfile=` — relies on
      ffmpeg's default font lookup which can fail on some Windows
      builds. Add a bundled font in `assets/` and reference it
      explicitly. (Stinger text is libass-rendered, already safe.)
- [ ] 🟢 If user names contain `'` (apostrophe), the .ass escape may
      double-escape inside `Dialogue:` lines. Add a unit test.
- [ ] 🟡 NVDEC session limit on consumer GPUs is ~8. The main render
      opens N inputs (one per kept segment). Investigate whether ffmpeg
      keeps all decoders open simultaneously or pools them. If many trims
      → many sessions, fall back to filter-trim path.

## Post-v1.5 cleanup (from 2026-05-15 audit)

Surfaced when reviewing the codebase after the auto-stinger ship. None
are urgent — just consolidating before the next feature.

- [x] 🟢 **Drop dead `stinger_text` plumbing.** Removed config key,
      property, builder param, manifest field, README/CLAUDE mentions.
      Manifest version bumped 1→2 so the prior cached snapshot is
      invalidated cleanly.
- [x] 🟢 **Retire `docs/CINEMATIC_INTRO_PLAN.md`.** Deleted (history
      preserved in git). Removed references in `docs/README.md` and
      `CLAUDE.md`.
- [x] 🟢 **Sync `intro_duration_seconds` defaults.** Property default
      now matches `config.json` at `4.0`.
- [x] 🟢 **`outro_bg_path` points at a missing file.** Cleared to
      `""` in config.json so it matches the `channel_name`
      "empty knob" convention. Renders still get the freeze-frame
      fallback via `_outro_stage`. CLAUDE.md reworded to call out
      that `assets/backgrounds/` is operator-created on demand.
- [x] 🟡 **Test gaps** identified during the audit:
      - Doubles-intro fallback: gate extracted as
        `all_intro_photos_present` + 8 cases in `test_intro_fallback.py`.
      - Stinger cache-hit: 8 cases in `test_stinger_cache.py` covering
        first-call render, cache hit, brand/resolution/duration/channel
        change invalidation, logo-mtime invalidation, and stale-cache
        recovery. Renderers stubbed via monkeypatch so the suite stays
        pure-Python (no ffmpeg execution).
