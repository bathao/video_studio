# TODO

## RESUME POINTER 2026-07-07 — improvement plan in progress (Phase 0–1 done)

`v3-dev` is pushed to `origin/v3-dev` (tracking set up 2026-07-07 as
part of improvement-plan Phase 0). Working state after the 2026-06-05
feature freeze is described in [HISTORY.md](HISTORY.md).

**Only operator-driven open item:** (A) run Auto Trim on a fresh match
outside the 3 PHASE0_REPORT spike entries to measure real recall on
truly-unseen venue + audio. Needs a new recording; the assistant can
only analyse the result, not produce the input.

Lower-priority / deferred: headless auto-trim inside the Render button
and YOLOv8-pose escalation — both gated on (A) producing enough
confidence in detector reliability first.

## Improvement plan 2026-07-07 (source-sweep driven)

Full findings came from a docs + backend + frontend source sweep.
Status per phase:

- ✅ **Phase 0 — housekeeping** (committed `a20d7dc` + `ffd7d54`,
  pushed): avatar roster bulk update; gitignored `assets/Jun_named/` +
  `assets/No Name/` staging dumps; pending feature docs committed;
  5 doc-drift fixes (intro 6s→4s docstrings, stinger 2s→1.5s default,
  `models.py` detect_roi name, test count, STALE warning on
  `verify_rally_detector.py` EXPECTED targets).
- ✅ **Phase 1 — backend robustness** (2026-07-07, uncommitted):
  see PROGRESS.md "Polish & robustness" for the 4 fixes (job.trims
  race, registry eviction, 404 on missing video, swallowed-failure
  surfacing). 175 tests pass (+5 new for registry eviction).
- ⬜ **Phase 2 — frontend correctness/UX**: undo clobbers player-name
  edits (`app.js` Object.assign restores stale `project.info`);
  `pendingTrimStart` missing from snapshot; boot dies when backend is
  down (`loadVideoList` outside boot try/catch); scoreboard preview
  sticks stale after a failed fetch (`lastSignature` advanced before
  fetch); `best_of` collected but match-over never detected; replace
  `prompt()` dialogs; loading states for save/load.
- ⬜ **Phase 3 — expose backend features in UI**: output library
  (`GET /api/outputs`), render-job re-attach after page reload
  (`GET /api/render`), delete-project button
  (`DELETE /api/projects/{name}`), probe metadata display.
- ⬜ **Phase 4 — performance plumbing**: frontend `stateSignature()`
  JSON.stringify on every timeupdate → dirty flag; keptSegments cache;
  list-render event delegation; auto-trim cache-hit skip per-progress
  queue replay; `concat_parts` re-probe removal. (rally_detector
  `cv2.mean` swap ONLY if byte-identical on existing dataset.)
- ⬜ **Phase 5 — tests + debt**: pure-logic tests for `_quad_iou` /
  `_order_clockwise_from_tl` / `_polygon_to_quad` / `_PRIORITY_GATES`,
  `escape_ffmpeg_filter_path`, .ass apostrophe escape; dedup
  `videoIdentBody` ×2, `_append_msg` ×2, refframe-extract cmd ×2,
  `_resetDetection` drift; then split the 3 giant functions
  (`detect_roi_multiframe`, `render_main_with_scoreboard`,
  `onRunDetectionClick`) — tests first.
- ⬜ **Phase 6 — operator backlog picks**: slow-mo audio ducking (🔴),
  renderer `--dry-run`, GitHub Actions lint, pin Python version.

---

## Shipped history

All completed phase reports, SHIPPED/COMMITTED notes, and superseded
plan drafts moved to [HISTORY.md](HISTORY.md) on 2026-06-05 to keep
this file actionable. The lessons / "what NOT to do" notes live there.

---

## Backlog cũ chưa làm

- 🟡 Country/club flags cạnh tên player trong scoreboard
- 🟡 Tournament logo trên intro card
- 🟡 Theme presets scoreboard per-tournament
- 🔴 Audio leveling/ducking trong slow-mo (atempo=0.5 hơi robot)
- 🟢 Pin Python version trong pyproject.toml
- 🟡 `--dry-run` mode cho renderer
- 🟡 GitHub Actions lint
- 🟢 Bundle font trong assets/ cho intro drawtext fallback
- 🟢 Unit test apostrophe trong .ass escape
- 🟡 NVDEC session limit investigation
