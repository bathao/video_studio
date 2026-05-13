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
- [ ] 🟢 Add `pytest` smoke tests for `kept_segments_from_trims`,
      `remap_score_event_to_trimmed`, and `_detect_active`.
- [ ] 🟡 Add a `--dry-run` mode to the renderer that prints the full
      ffmpeg command instead of executing.
- [ ] 🟡 GitHub Actions: lint (ruff) + import-check on push.

## Bugs / questions

- [ ] 🟢 `drawtext` on the intro card uses no `fontfile=` — relies on
      ffmpeg's default font lookup which can fail on some Windows builds.
      Add a bundled font in `assets/` and reference it explicitly.
- [ ] 🟢 If user names contain `'` (apostrophe), the .ass escape may
      double-escape inside `Dialogue:` lines. Add a unit test.
- [ ] 🟡 NVDEC session limit on consumer GPUs is ~8. The main render
      opens N inputs (one per kept segment). Investigate whether ffmpeg
      keeps all decoders open simultaneously or pools them. If many trims
      → many sessions, fall back to filter-trim path.
