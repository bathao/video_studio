# TODO

Actionable items, ordered roughly by priority. Each entry has a small
indicator: 🟢 quick (<1h), 🟡 medium (~half-day), 🔴 large (1+ day).

## Now (P0 — needed for daily use)

- [ ] 🟢 **End-to-end test on real 2K match (`0418_Tu_3-1.MP4`)**
      Verify NVENC + NVDEC happy path on a 10–15 GB file. Measure render
      wall-clock time for 90-min footage.
- [x] 🟢 **Cancel render mid-flight** — done.
      `run_ffmpeg_with_progress` accepts a `cancel_check` predicate that
      trips `proc.terminate()` on the next progress line; the renderer
      threads it through every stage and raises `FFmpegCancelled`, which
      flags the job as `cancelled`. UI exposes a Cancel button in the
      render-status panel; `POST /api/render/{job_id}/cancel` flips the
      flag.
- [x] 🟢 **Cleanup `temp/<job_id>` after success** — done.
      `run_render` now `shutil.rmtree`s the job dir once the final mp4
      lands in `output/`. Errors still leave temp untouched so the
      operator can inspect the failed ffmpeg inputs.
- [x] 🟡 **Scoreboard preview in the UI** — done.
      Auto-attached as soon as the source video reports `loadedmetadata`;
      feeds the exact `.ass` the render burns in to JASSUB (libass-WASM)
      which composites a canvas over the `<video>` element. Single
      source: `build_scoreboard_ass_text` in `backend/ass/scoreboard.py`
      powers both the render and the preview, so they're byte-identical.
      Updates live as the operator edits tournament / player names or
      scores.

## Next (P1 — meaningful UX wins)

- [ ] 🟡 **Timeline markers on the seek bar**
      Render coloured ticks for each highlight (green) and trim segment
      (red) directly on the scrubber.
- [ ] 🟡 **Serve indicator**
      Table-tennis serves alternate every 2 points (every 1 in deuce).
      Track `serving_player` in live state and surface a `🏓` next to the
      server's name in both UI and scoreboard.
- [ ] 🟡 **Deuce / match-point flag**
      Show `DEUCE` / `MATCH POINT` overlays on the scoreboard when the
      conditions are met.
- [ ] 🟡 **Best-of-N match config**
      User picks "best of 3 / 5 / 7" and the renderer stops the
      scoreboard updates after match completion. Currently we treat each
      set independently with no match-end concept.
- [ ] 🟡 **Multiple slow-mo factors**
      Right now slow-mo is hard-coded to 2× on the last 2.5 s. Add UI to
      pick `1.5× / 2× / 4×` and tail length per highlight.
- [ ] 🟡 **Inline rename / reorder highlights and trims**
      Drag-to-reorder is nice; rename via the existing inline inputs.
- [ ] 🔴 **Resume from last completed stage**
      If render fails halfway, allow re-render that skips already-built
      `intro.mp4` / `highlight.mp4` / `main.mp4` in `temp/<job>`.

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
- [ ] 🔴 **Animated set transition card** (1-second card between sets in
      the main render).

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
