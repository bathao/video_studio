# TODO

## RESUME POINTER 2026-07-10 — Auto Score Phase 0 measured + Phase 1 semi-auto GUI shipped

All Auto Score work below is committed on `v3-dev` (`b516687` step 1
corpus, `8f4d02c` steps 2+6, `10708ba` G0a + Phase 1 GUI + steps 3+5).
**These 3 commits are NOT pushed yet** — `origin/v3-dev` is 3 behind.
Working state after the 2026-06-05 feature freeze is described in
[HISTORY.md](HISTORY.md); the 2026-07-07 improvement plan (complete)
is in the next section.

**Next big feature (IN PROGRESS since 2026-07-07):** Live Score
automation — full plan in [AUTO_SCORE_PLAN.md](AUTO_SCORE_PLAN.md).
GUI splits the Live Score panel into Manual | Auto tabs (manual path
untouched); vision-only winner detection (local VLM + trained
classifier + score-grammar solver; audio ruled out by operator).
Offline Phase 0 feasibility spike first — no GUI work until Gate G0
passes. Phase 0 step status:

- ✅ **Step 1 — corpus builder** (2026-07-07, committed `b516687`):
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
- 🔴 **Step 2 — unanchored segmentation eval** (2026-07-08,
  MEASURED on all 7 matches): 1-D ROI-motion signal totals **78.8%
  association recall (434/551), per-match range 65-94%**, ~45% junk
  proposals; ladder rungs v0-v5 (hysteresis / Otsu / near-far
  alternation / duration priors / periodicity / semi-Markov) ALL
  measured, none breaks the ceiling. 2_sets clean-clip start-F1
  78.8% @2s. Full verdict + escalation options in plan §6 step 2.
  Scripts: `motion_cache.py`, `eval_segmentation.py` under
  scripts/auto_score_spike/; caches + plots in out/ (gitignored).
  **Escalation MEASURED (2026-07-08 session 2)**, singles-only per
  operator directive (doubles excluded from train+eval everywhere;
  data kept on disk):
  - Frame classifier: 4 variants ALL failed held-out (static 49.7%,
    temporal stack 60.0%, stack+aug-fix 61.1%, playzone-masked
    51.7%) — pixel CNNs learn the venue, not the play state, with
    only 4 training venues. Path closed; pose features
    (`pose_features.py`, written, unmeasured) are the venue-invariant
    successor if needed.
  - **Miss audit changed the game**: 25/26 missed events on the worst
    match sat ABOVE the motion threshold inside merged blobs — rapid
    point series (~6 s apart) fuse into one interval. Fix is
    algorithmic, not ML: **v7 recursive valley split** → singles
    assoc-recall **76.1% → 85.5% total** (worst match 65.3→82.7%,
    held-out 68.4→77.2%, ~1.7x proposals). Sweep tuned on train
    singles only.
  - Operator principle recorded: table ROI is the anchor of every
    mask; GUI must gate on operator ROI confirm (plan §5.2); playzone
    = ROI quad extended along its own edge vectors (perspective-true).
  - VLM bake-off: MiniCPM-V 4.5 = 65% mapping-acc, ~chance pairwise,
    FLAT confidence 0.9 (useless for the solver), boilerplate
    reasons. Zero-shot VLM looks weak, consistent with the attempt-1
    prior — strategy shifted: Phase 1 gates on segmentation only
    (G0a); winner path (G0b) deferred until the flywheel grows the
    corpus. RESUMED 2026-07-08 pm: partial dirs wiped; remaining 4
    models (qwen3.5:9b / qwen3-vl:8b / gemma4:12b / qwen2.5vl:7b)
    ran on the EXACT 60 clips MiniCPM saw (`--ids-file` added to
    `vlm_bakeoff.py` — corpus rebuilds no longer change the sample).
    Completed — full results in step 3 below.
  - **Flywheel turn 1 (2026-07-08 pm)**: first new production match
    archived (match_001_20260708_143451, source 0611_Tim_2-3.MP4,
    singles, 5 sets, 90 events, P1 = near per the new convention).
    Corpus rebuilt: 622 → 712 records (641 dataset events, 9 unique
    matches). **Truly-unseen v7 test on it: 85.6% (77/90)** with the
    frozen tuned config — matches the 85.5% train average; no
    overfit. ROI auto-detect hit `yolo_seg+orb_agree` on the new
    venue. Assoc metric + tuned config persisted in
    `eval_unseen.py` (was ad-hoc).
  - 🟢 **G0a BAR REACHED (2026-07-08 pm)**: miss audits showed the
    residual misses were (a) blobs that CONTAIN the event with a
    late fetch-tail end (fine for a review GUI → coverage metric)
    and (b) short spikes >= p77 killed by `min_rally_s=1.5` (not
    quiet rallies). **v7-tuned2** (`min_rally_s=1.0, pct_lo=55`):
    coverage recall **train 98.0% / held-out 94.9% / truly-unseen
    97.8% / total 97.5%**, proposals ~2x true events (~50%
    precision → GUI review burden ~155-230 cards/match; pose junk
    filter demoted to click-count optimization). Details + caveat
    in plan §6 step 2. Segmentation no longer blocks Phase 1
    semi-auto GUI.
  - 🟢 **Phase 1 semi-auto GUI BUILT (2026-07-08 pm, committed `10708ba`)**:
    Live Score panel split into Manual|Auto tabs (manual DOM moved
    verbatim, score.js untouched). Auto tab flow: mandatory ROI
    confirm gate (reuses the Auto Trim modal) → "Detect rallies"
    (new `backend/auto_score/` package: v7-tuned2 port +
    `/api/auto_score/*` SSE job pipeline mirroring auto-trim, cache
    at temp/auto_score_cache/) → keyboard-first review list
    (1/2=winner, Space=accept, X=delete, row click seeks) → Apply
    writes score_events with `source:"auto"` + recompute. Review
    draft persists in `project.auto_score_draft` (new ProjectData
    field; ScoreEvent gained `source`). Frontend package
    `frontend/auto_score/` (4 ES modules). Tests 209 → 216 (7 new
    pure-logic segmenter tests incl. frozen-config guard). Verified
    E2E in headless Edge (Playwright, 19/19 real checks: live
    decode on a 6-min clip → 48 proposals, keyboard verdicts,
    hotkey interception, apply, draft save/load round-trip, 131 ms
    cache-hit rerun; the one flagged console 404 is the pre-existing
    by-design avatar probe). Operator must restart run.bat to get
    the new routes.
- 🔴 **Step 3 — VLM bake-off** (2026-07-08/09, MEASURED): 4 models ×
  same 60 clips; zero-shot = CHANCE on the honest derived-mapping
  metric (MiniCPM 53.3%, gemma4 50.9%, Qwen3-VL 45.3%, Qwen3.5
  41.3%); the fitted metric everyone showed 65-78% on is inflated.
  Qwen3.5 alone hid a ~59% flip-inverted signal (pairwise 58.8%);
  **prompt-B probe (geometry-defined near/far) recovered it: 60.4%**
  — best zero-shot number; MiniCPM+B control stayed at chance
  (46.7%), confirming prompt B can't conjure signal (gemma4 variant
  skipped on that basis). Operator backfilled P1-side bits
  (`side_truth.json`, artifact page) → `rescore_vlm.py` scores TRUE
  accuracy with no mapping fit. Full table in plan §6 step 3. G0b:
  **Qwen3.5-9B + geometry prompt is the fine-tune base**; zero-shot
  tops out ~60%, so fine-tune-on-flywheel remains the only path to
  >=90%.
- 🔴 Step 4 — trained-classifier spike: the 4-variant pixel-CNN
  failure in the step-2 escalation block IS this step's measurement
  (learned venue, not play state, at 4 venues). Revisit only after
  the flywheel adds venues; pose features are the venue-invariant
  successor.
- 🟠 Step 5 — side-identity tracker MEASURED 2026-07-09
  (`side_identity.py` v2, near-player lower-body re-ID): boundary
  swaps 14/18, controls 19/22, set-5 verdicts 2/2 with clean
  margins — viable SOFT set-boundary/mapping evidence + set-5 bit
  resolver (details plan §6 step 5). Serve-side detector MEASURED
  (`serve_side_spike.py`, pose aggregates over serve/pre-serve
  windows, derived labels incl. the free set-1-server bit →
  baseline ~53%): best held-out **~61%** (near-far table-distance
  contrast in the first 2 s) — real but too weak to locate set
  boundaries alone; deprioritized in favor of the side-identity
  tracker. Upgrade path if ever needed: higher-fps serve-window
  sampling + wrist-height (ball-toss) cues.
- ✅ **Step 6 — solver simulation** (2026-07-08):
  `solver_sim.py`. Grammar multiplier for winner-only observations
  is ~1.0 (NOT the rescuer the plan assumed — §4 revised); per-set
  anchors cut flags 78→27/match at raw 90%; calibrated per-point
  confidence is the real lever (raw 87.7% → post 89.1%, 20
  flags/match, but ~2 UNFLAGGED errors/match remain → vision must
  reach >=90-95% raw). Bonus: all 7 real corpora sequences replay
  legally under the grammar; set structure matches every filename.

- 🟢 **Side-info training labels in the GUI (2026-07-10, uncommitted)**:
  three new `ProjectInfo` fields captured at production time so the
  flywheel corpus carries side/swap truth without the post-hoc
  `side_truth.json` backfill — `p1_side_set1` ("near"/"far"/null,
  camera view, set 1), `swap_sides_each_set` (default true; untick for
  special no-swap matches), `set5_mid_swap` (true/false/null unknown).
  Setup panel gets a "Side info (auto-score training)" block, hidden
  in Double mode. **Doubles exclusion is now explicit in data**:
  manifest entries carry `match_type` + `auto_score_train_eligible`
  (false for doubles), notes.md doubles entries carry an
  "AUTO-SCORE TRAINING: EXCLUDED" marker, and every corpus record
  carries `train_eligible` + the three side fields (`.get` defaults on
  pre-field groundtruths). Bonus fix: project load now spreads
  `data.info` over defaults instead of `Object.assign` onto the live
  info, so loading a legacy JSON no longer carries the PREVIOUS
  project's ROI / side labels into it. Also `camera_angle`
  ("standard" behind-player family default | "side" ~90° | "other",
  2026-07-10 pm): the P1-side select's AXIS follows the angle —
  near/far for standard, LEFT/RIGHT of frame for side-on (operator
  design: same control, options swap), disabled for "other";
  `p1_side_set1` literal extended to near|far|left|right. Non-standard
  matches are marked EVAL-ONLY in notes.md / manifest / corpus for
  angle-locked training; operator holds a few rare side-on
  recordings → out-of-family eval for G0b. Defaults follow the
  operator's
  production conventions (2026-07-10 pm): NEW projects default to
  `p1_side_set1="near"`, `swap_sides_each_set=true`,
  `set5_mid_swap=true` — operator flips only when a match deviates
  (rare venue-quirk no-swap matches use the checkbox). Legacy project
  loads normalise missing keys to null/unknown, never the defaults —
  no fabricated labels for old data. Corpus rebuild verified (712
  records, 166 doubles flagged ineligible); GUI verified E2E in
  headless Edge (19/19: visibility toggle, defaults, save/load
  round-trip, legacy reset, undo).

- 🟢 **Handicap (điểm chấp) support (2026-07-10, uncommitted)**: new
  `ProjectInfo.handicap_receiver` (0|1|2) + `handicap_pattern` (digit
  string, one digit per set, CYCLING — "232" → set4 wraps to 2; digit
  n = receiver starts the set leading n–0, sets still play to 11
  win-by-2). Setup panel gets a "Handicap" block (receiver select +
  pattern input with datalist presets 020/202/222/232/323/333/444),
  visible in BOTH singles and doubles (production display; doubles
  stays train-excluded). Score recompute starts each set from its
  handicap score and RETRO-recomputes existing events on handicap
  edits; the scoreboard (burned-in + preview, single source) shows a
  gold "+<pattern>" badge after the receiver's name and the correct
  pre-first-event start score. **Training-metadata guarantee: score
  events remain REAL rallies only** (handicap baked into set-start
  scores, never fake key presses) → handicap matches stay fully
  train-eligible for segmentation + winner labels; notes.md gets a
  prominent HANDICAP declaration, manifest + corpus records carry
  `handicap_receiver`/`handicap_pattern` so score-grammar/solver
  consumers can adjust or skip. Shared rule lives in
  `handicap_set_start` (backend/ass/scoreboard/events.py) mirrored by
  `handicapStart` (frontend/score.js). Tests 222 → 233 (incl. a
  no-handicap byte-identity guard); E2E in headless Edge 19/19 (real
  scoring on a live video: 11×A wins the set at 11–2, next set cycles
  to 0–2, retro-recompute 222→020 verified).

- 🟢 **YOLO retrain flywheel closed (2026-07-10, uncommitted)**:
  (1) `/api/auto_trim/groundtruth_count` reports
  `confirms_since_yolo_train` (confirm events newer than
  `assets/models/roi_seg.pt` mtime) + `yolo_model_exists`. (2) NEW
  module `backend/server/retrain.py` owns an in-server retrain job
  (`POST /api/auto_trim/retrain_yolo` + status endpoint): runs
  `build_yolo_dataset.py` → `train_roi_seg.py` on a worker thread,
  refuses to overlap itself or any GPU job (render / auto-trim /
  auto-score), and on success calls the new
  `roi_yolo.invalidate_model_cache()` so the running server picks up
  the new weights with NO restart. (3) NEW frontend module
  `frontend/auto_trim/retrain.js` owns the whole retrain UI: staleness
  line + "Retrain now" button in the Groundtruth panel, a popup offer
  right after Confirm when ≥5 confirms are pending (once per modal
  session), and 5 s status polling with toast on done/error. Rationale:
  classical tiers (ORB + learned-NN) learn from confirms instantly;
  YOLO used to lag silently (13 confirms pending when found). Same day
  the backlog was cleared with a manual retrain on 56 videos / 126
  images: mask mAP50-95 **0.908**, mAP50 0.995 (val = 26 images incl.
  new venues; old 0.921 was on the smaller 53-entry val — not directly
  comparable). (4) **Auto-comparison after every retrain**
  (`scripts/compare_roi_models.py`, operator request): the retrain job
  backs up the previous weights to `assets/models/roi_seg.prev.pt`,
  and after training A/Bs old-vs-new through the exact production
  inference path on every confirmed refframe (truth = operator
  corners) — SUMMARY verdict (IMPROVED / TAIL IMPROVED / EQUIVALENT /
  REGRESSED) lands in the modal's status toast/log. Measured verdict
  for today's retrain: **TAIL IMPROVED** — within-2% 50→53/56, worst
  2.81→2.28%, mean ~equal (1.01→1.07%); the recorded production
  proposals the operator corrected averaged only 0.85% error (max
  1.94%) — the remaining offsets are fine-precision edge adjustments,
  not detection failures.
  Tests 238 → 251; smoke E2E 4/4.

- 🟢 **Training-status dashboard (2026-07-10, uncommitted)**: top-bar
  "📊 Training" button (next to Save/Load/Render) opens a popup that
  answers "is my manual production paying off, and is any training
  action due?" without leaving the web UI. (1) NEW
  `GET /api/training/status` (`backend/server/routes_training.py`,
  thin aggregator) = corpus readiness + ROI staleness + live retrain
  snapshot. (2) `dataset.training_corpus_stats()` counts MATCHES
  (unique source videos, newest render wins) toward the G0b fine-tune
  target (15): labeled (singles + score events + side info in the
  archived project snapshot) / unlabeled legacy / doubles-excluded /
  no-events; per-match table in the popup. The G0b fine-tune itself is
  a milestone DECISION — the popup reports readiness only; the real
  Start button is ROI retrain (same backend job as the Auto Trim
  modal's). (3) Retrain job now reports **epoch-level progress**:
  `retrain.py` streams `train_roi_seg.py` stdout via Popen, parses
  ultralytics `epoch/total` lines into a 0..1 fraction (build 0→0.10,
  train 0.10→0.90 by epoch, compare →1.0) exposed in
  `retrain_status()`; progress bar in the popup + a "⟳ 42%" chip on
  the top-bar button while a retrain runs in background (whichever UI
  started it — Auto Trim modal dispatches `retrain-active`). (4)
  `groundtruth_summary()` moved to `retrain.py` (shared by the modal
  endpoint + dashboard). Frontend: `frontend/training_status.js`
  (single-writer for `#modal-training` + `#btn-training-prog`).
  Tests 251 → 260; E2E in headless Edge 10/10 on real data
  (1/15 labeled, 56 ROI videos, retrain button disabled at staleness 0).

- 🟢 **Retro-labeling of archived entries (2026-07-10, uncommitted)**:
  `dataset.apply_retro_labels(slug, labels)` fills side-info into an
  archived entry WITHOUT re-render — the expensive label (score
  events) already sits in `dataset/<slug>/project.json`; only the
  cheap geometry labels were missing. Whitelisted fields only
  (`match_type` / `p1_side_set1` / `swap_sides_each_set` /
  `set5_mid_swap` / `camera_angle`); snapshot updated in place,
  "## Retro labels" provenance section appended to notes.md (original
  auto-filled sections untouched), manifest `camera_angle` /
  `match_type` + `auto_score_train_eligible` synced. Applied the
  operator's answers to all 8 pre-GUI entries the same evening: 6
  singles labeled (5 near + 1 far, all standard angle, swap yes,
  set5 mid-swap yes where reached), and 2 entries turned out to be
  **doubles mis-recorded as singles** (pre-match_type archives) — now
  corrected + excluded. Corpus: **7/15 labeled, 0 unlabeled left**.
  No GUI (operator preference — answers gathered in chat, applied by
  the assistant); `resolve_entry_file` is traversal-safe for any
  future dashboard use. Tests 260 → 265.

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
- ✅ **Phase 1 — backend robustness** (2026-07-07, committed `715d0c1`):
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
