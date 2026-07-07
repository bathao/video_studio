# TODO

## RESUME POINTER 2026-07-07 — improvement plan COMPLETE (Phase 0–2, 4–6 done; 3 skipped)

`v3-dev` is pushed to `origin/v3-dev` (tracking set up 2026-07-07 as
part of improvement-plan Phase 0). Working state after the 2026-06-05
feature freeze is described in [HISTORY.md](HISTORY.md).

**Next big feature (IN PROGRESS since 2026-07-07):** Live Score
automation — full plan in [AUTO_SCORE_PLAN.md](AUTO_SCORE_PLAN.md).
GUI splits the Live Score panel into Manual | Auto tabs (manual path
untouched); vision-only winner detection (local VLM + trained
classifier + score-grammar solver; audio ruled out by operator).
Offline Phase 0 feasibility spike first — no GUI work until Gate G0
passes. Phase 0 step status:

- ✅ **Step 1 — corpus builder** (2026-07-07, uncommitted):
  `scripts/auto_score_spike/build_corpus.py` →
  `dataset/auto_score_corpus/corpus.jsonl` (gitignored, regenerable).
  622 records = 551 unique manual score events (GOLD winner labels)
  + 71 attempt-1 reviewed rallies (near/far winner + taxonomy).
  Dedup by source video found the 9 manifest slugs are only **7
  unique matches** (0331_Trung archived 3x — 5-event partial + one
  93-event duplicate dropped); the "649 labeled points" figure in
  the plan is 551 unique. Held-out PINNED: 0510_HoangHuuHa_1-3
  (singles) + 0402_ThoiThao_vs_LoiPhuong_3-2 (doubles) = 173
  eval-only records; 449 train. v1 is index-only (no clip cutting —
  frames extracted on demand downstream; deliberate deviation from
  the plan text). Verified: all 622 video paths resolve; set_index
  0-4 cross-checks against every filename final score; winner
  balance P1=260/P2=291 + a=42/b=29.
- ⬜ Step 2 — unanchored rally segmentation eval (vs the 34-point
  start truth in dataset/attempt1 + 551 rally-end labels).
- ⬜ Step 3 — VLM bake-off (shortlist in plan §4.2; needs Ollama
  approval, plan §9).
- ⬜ Step 4 — trained-classifier spike (parallel with step 3).
- ⬜ Step 5 — serve-side detector spike.
- ⬜ Step 6 — solver simulation at both anchor rungs (§4.1).

**Operator-driven open item:** (A) run Auto Trim on a fresh match
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
- ✅ **Phase 2 — frontend correctness/UX** (2026-07-07): info edits
  (names / tournament / best-of) snapshot per 1.5 s burst so a later
  Ctrl+Z no longer clobbers typed names; match-type + video-source
  changes are undoable; undo restores `pendingTrimStart` + both HUD
  badges; boot survives a down backend (`loadVideoList` failure no
  longer aborts `syncAllUI`); try/catch + toasts on save / load /
  render-start / reveal / open-folder fetches; scoreboard preview
  marks its signature clean only after a successful fetch (+3 s
  failure backoff); `scorePoint` blocks once the match is decided per
  `best_of` and announces match win; manual highlight/trim add is an
  inline row at the playhead (`prompt()` removed). Verified
  end-to-end in headless Edge via Playwright — 18/18 checks.
- ⏭️ **Phase 3 — expose backend features in UI**: SKIPPED by operator
  decision 2026-07-07 — all four items (output library, render-job
  re-attach after reload, delete-project button, probe metadata) have
  Explorer/folder workarounds and none prevents real work. Revisit
  render-job re-attach only if a mid-render page reload actually bites.
- ✅ **Phase 4 — performance plumbing** (2026-07-07): scoreboard
  preview refresh moved OFF the timeupdate path (the .ass depends only
  on info + events — mutation sites call it explicitly; was a full
  project JSON.stringify 4×/s); Preview Cut kept-segments cached,
  invalidated via syncTrims + durationchange; all 3 list panels
  (events / highlights / trims) render as one HTML string with
  delegated listeners instead of per-row createElement + rebind;
  auto-trim cache-hit no longer replays thousands of per-frame
  progress events through the SSE queue (trims/stage/log/done only —
  frontend jumps the bar to 100% on close). Verified in headless Edge:
  0 preview fetches during playback, all delegated interactions work,
  Preview Cut skips a freshly-edited trim. Dropped as not-worth-it:
  `concat_parts` re-probe (3 ffprobe spawns once per render) and the
  rally_detector `cv2.mean` swap (touches detection code — barred by
  the don't-touch-ROI rule).
- ✅ **Phase 5 — tests + debt** (2026-07-07). Tests: 34 new pure-logic
  tests (`tests/test_pure_helpers.py`) covering `_quad_iou`,
  `_order_clockwise_from_tl` (incl. a drift-pin against roi_yolo's
  deliberate local copy), `_default_roi`, `_polygon_to_quad`,
  `_fmt_mmss`, `escape_ffmpeg_filter_path`, `_ass_escape` (apostrophe
  backlog item ✔), `_ffmpeg_color`, config fallbacks — suite now
  **209 pass**. Dedup: `videoIdentBody` (detection.js now imports from
  api.js), `_append_msg` (dataset.py imports from groundtruth.py),
  refframe ffmpeg cmd (`_refframe_cmd` shared), detection-state reset
  (`resetDetection` exported; index.js's drifted copy removed).
  Refactor: `onRunDetectionClick` split — 7 SSE handlers extracted as
  named functions + `attachSse`; modal verified live in headless Edge
  (refframe + multi-frame detect ran clean).
  **Deferred to their own sessions** (need reference-output
  verification): splitting `detect_roi_multiframe` (~340 lines — also
  where `_PRIORITY_GATES` tests become possible after extraction;
  barred-by-default under the don't-touch-ROI rule) and
  `render_main_with_scoreboard` (~290 lines — needs byte-identical
  render capture/verify per CLAUDE.md).
- ✅ **Phase 6 — operator backlog picks** (2026-07-07):
  - Slow-mo audio ducking: **already resolved by design, backlog item
    was stale** — `REPLAY_VOLUME = 0.0` mutes the original audio in
    every replay and the `slow_motion.mp3` music bed plays instead
    (since v1.4). The "atempo robot voice" can never reach the output.
  - Renderer dry-run: `scripts/dry_run_render.py <project>` prints
    source metadata, trim/kept breakdown, replay inserts, playlist
    composition + NVDEC pre-concat trigger, and the estimated final
    duration — one read-only ffprobe, zero encoding. Tested against a
    real 69-auto-trim project.
  - CI: `.github/workflows/ci.yml` (windows-latest, Python 3.13) —
    ruff critical rules (E9,F63,F7,F82; verified passing locally) +
    the 209-test pytest suite. Dev deps in `requirements-dev.txt`.
    Run #1 failed — fresh checkouts lack `temp/`, and pytest doesn't
    create its pinned basetemp's parent; fixed in `38bbc5b` with
    `tests/conftest.py` (verified on a fresh clone: 60 errors → 209
    pass). Run #2 on `38bbc5b`: **green**.
  - Python version pinned: `requires-python = ">=3.13"` in
    `pyproject.toml` (matches the operator venv).

---

## Shipped history

All completed phase reports, SHIPPED/COMMITTED notes, and superseded
plan drafts moved to [HISTORY.md](HISTORY.md) on 2026-06-05 to keep
this file actionable. The lessons / "what NOT to do" notes live there.

---

## Remaining backlog (cosmetic / needs operator input)

Pruned 2026-07-07 — resolved items moved into the improvement-plan
notes above (audio ducking: stale, resolved by design since v1.4;
Python pin, dry-run, CI lint: Phase 6; apostrophe .ass test: Phase 5;
NVDEC session limit: investigated + fixed by `pre_concat_slices`,
shipped 1f2b577).

- 🟡 Country/club flags next to player names in the scoreboard
- 🟡 Tournament logo on the intro card
- 🟡 Per-tournament scoreboard theme presets
- 🟢 Bundle a font under assets/ for the text-intro drawtext fallback
