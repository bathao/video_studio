# TODO

## ✅ Scoreboard inter-set recap VERIFIED + COMMITTED 2026-06-05

Operator render-tested a multi-set match and confirmed the recap panel
renders correctly at the bottom-right corner with no flicker. Committed
as `14c5db7` on `v3-dev`.

### What shipped

- Refactor: extracted `_emit_scoreboard_panel` shared between the
  final scoreboard and the inter-set recap. Final output byte-
  identical to pre-refactor (wrapper passes fade defaults 500/300).
- UX: each non-match-ending set expands the bottom-right panel by
  one column, holds 4 s (clipped 0.3 s before the next score event).
  Dropped the two large centred cards (140 px score + 200 px
  "SET N+1") entirely.
- Dead-code cleanup: 3 font fields (`fs_recap_lbl / recap_score /
  transition`) + 3 ASS styles (`SetLabel / SetRecap / SetTransition`)
  removed.
- +2 smoke tests for recap behaviour. 170 pytest pass.

Follow-up commit `22b5f08`: 9 new player avatars + updated Nguyễn Minh
Quân photo.

---

## ✅ Phase 1b VERIFIED + COMMITTED 2026-05-29

Operator tested end-to-end in browser and confirmed Auto Trim works
at a basic level. Bundled commit covers:

- Phase 1b Auto Trim end-to-end (see section below for the 5-step
  breakdown).
- **NVDEC session-budget fix** (`pre_concat_slices` in
  [renderer/stages.py](../backend/renderer/stages.py) +
  [renderer/orchestrator.py](../backend/renderer/orchestrator.py)).
  Auto Trim's 60+ trims / kept segments previously OOM'd the GPU's
  NVDEC session cap (~6 concurrent on RTX 5060 Ti) when each segment
  was opened as its own `-hwaccel cuda -i src`; the software-decode
  fallback then exhausted RAM holding 90+ HEVC ref-frame buffers.
  Fix: when slice count > 6, pre-concat all slices into a single
  intermediate via the concat demuxer (one decoder context), then
  the main render consumes it through `split` + `trim` per slice
  entry. Replay + stinger entries keep their own inputs (replays need
  precise frame-accurate seek for slow-mo; stingers come from cached
  files). Pre-concat takes ~25% of "main" weight; filter-graph render
  gets the remaining ~75%.
- New player avatar `assets/avatars/Nguyễn Văn Trung.jpg`.

### Follow-ups discussed (operator picks when ready)

- (A) Test on a fresh match outside the 3 PHASE0_REPORT spike entries
  to measure real-world recall on truly-unseen audio + venue.
- (B) Optimise detect speed 2.2× → 6-8× realtime (plumbing only —
  greyscale decode from NV12 luma instead of per-frame cvtColor;
  numpy mask sum hot loop). Algorithm-preserving fixes per memory
  `feedback_dont_touch_roi_algorithm_when_results_good.md` apply
  equally to the rally detector.

---

## ✅ Phase 1b SHIPPED 2026-05-26 — Auto Trim end-to-end

Operator workflow now works end-to-end:

```
⚡ Auto Trim  →  Confirm ROI  →  Run detection  →  Apply  →  trims appear
   modal         Phase A           Phase B          with [AUTO] badge
```

### What landed (all 5 steps in one bundle per operator preference)

| Step | What | Files |
|---|---|---|
| 1 | Backend rally detector (NVDEC decode → motion → adaptive p70 → score-anchored gap detection → trims). Pure `gaps_to_trims` core split out. Hardcoded Balanced preset. | [backend/rally_detector.py](../backend/rally_detector.py), [tests/test_rally_detector.py](../tests/test_rally_detector.py) (22 tests), [scripts/verify_rally_detector.py](../scripts/verify_rally_detector.py) |
| 2 | SSE orchestration: 4 new endpoints (`/start`, `/events/{id}`, `/cancel/{id}`, `/job/{id}`) + `AutoTrimJobState` + `queue.Queue` per job + `temp/auto_trim_cache/<sha1>.json` cache layer keyed by (video_id + roi + score events + params). Cache hit replays events in ms. | [backend/server/routes_auto_trim.py](../backend/server/routes_auto_trim.py), [backend/server/state.py](../backend/server/state.py) |
| 3 | Modal Phase B: detection section in right column (status / stage / progress bar / live log / results / Run / Cancel / Apply / Discard buttons). EventSource client + state machine in new module. | [frontend/index.html](../frontend/index.html), [frontend/auto_trim/detection.js](../frontend/auto_trim/detection.js) (NEW), [state.js](../frontend/auto_trim/state.js), [index.js](../frontend/auto_trim/index.js), [api.js](../frontend/auto_trim/api.js), [modal.js](../frontend/auto_trim/modal.js) |
| 4 | `TrimSegment.source: Literal["manual", "auto"] = "manual"`. Apply filters existing `source=="auto"` trims first → re-run replaces only auto, preserves manual. Trim panel renders `[AUTO]` badge. | [backend/models.py](../backend/models.py), [frontend/trims.js](../frontend/trims.js) |
| 5 | Cache key determinism + TrimSegment BC tests (9 new). Total suite 166 pass. Validation in `/start`: ≥10 score events required; `duration < 60 s` rejected. | [tests/test_auto_trim_routes.py](../tests/test_auto_trim_routes.py) (NEW) |

### Verify results — all 3 PHASE0_REPORT spike entries PASS

| Entry | Source | Recall | Extras | Trims | Detect speed | PHASE0_REPORT Balanced |
|---|---|---:|---:|---:|---|---|
| E1 `match_001_20260516_215553` | 22:00 | **0.993** | **352.7s** | 71 | 2.2× realtime | 97.6% / 323s |
| E2 `match_001_20260516_223928` | 20:41 | **0.968** | **319.6s** | 72 | 2.2× realtime | 92.2% / 289s |
| E3 `match_001_20260516_230801` | 13:54 | **0.957** | **303.7s** | 62 | 2.1× realtime | 94.9% / 276s |

Recall exceeds PHASE0_REPORT on every entry (better ROI fit from
`detect_roi_multiframe` vs spike's hand-eyeballed corners). Extras
slightly higher than spike target (~10–30 s per entry) but well within
tolerance. All 3 used `multiframe[5/5]:yolo_seg+orb_agree` for ROI
(high-trust cross-validated tag).

### Known concern: detect speed

~2.2× realtime, so a 22-min source ≈ 10 min wall-clock. Below
PHASE0_REPORT's 8× decode claim. Hypotheses: per-frame `cv2.cvtColor`
on CPU (could decode greyscale directly from NV12 luma plane), Python
fancy-indexing on `diff[mask_bool].sum()` per frame, or NVDEC not
actually engaging on this driver/build. **Not blocking** — algorithm
correctness verified; perf optimisation is a plumbing-only follow-up.

### Operator end-to-end test checklist

Restart `run.bat` and test in browser at http://127.0.0.1:8765/ :

1. Load a video, score ≥10 points with A/D throughout the match
2. Click ⚡ Auto Trim → ROI canvas loads → Confirm ROI (Phase A unchanged)
3. After Confirm, "Run detection" button enables; score event count shown
4. Click Run → progress bar streams; log shows `stage` and `progress` events
5. Done → results panel shows `Trims found / Dead time / Cache miss`,
   Apply + Discard buttons appear
6. Click Apply → trims appended to Trim panel with `[AUTO]` badge
7. Re-run on same project → `Cache hit ⚡` → done in milliseconds
8. Cancel mid-run → status flips to `cancelled` within ~1 s
9. Open modal again with a different video → no stale state leaks

If all 9 pass, the only remaining task is the single commit covering
Phase 1b end-to-end.

---

## Phase 1b plan (draft, 2026-05-20)

Operator clicks ⚡ Auto Trim → confirm ROI (already shipped) → click
"Run detection" → backend extracts motion signal + runs detector +
streams progress + per-rally / per-trim events via SSE → operator
reviews + Apply → trims append to `project.trim_segments` with
`source="auto"`.

**Estimate:** ~2 days focused work (down from 4.5 days in the older
2026-05-17 plan, because the refactor + perf bundle already shipped
much of the supporting infra).

### Step 1 — Backend rally detector  (~6-8h)

New file `backend/rally_detector.py` (single file, package only if it
overflows). Rebuilt from PHASE0_REPORT.md spec because the spike was
deleted; report is the authoritative source for parameters.

- ffmpeg NVDEC decode pipeline: `-hwaccel cuda -hwaccel_output_format
  cuda -vf "scale_cuda=480:270,hwdownload,format=nv12,format=rgb24"`.
  Spike measured 240-263 fps decode (8× realtime) on RTX 5060 Ti.
- ROI mask via `cv2.fillPoly` on the 4-corner quad.
- Motion signal: `mean(abs(curr - prev) * roi_mask)` per frame.
- Smooth: moving average window 0.5s.
- Adaptive threshold: per-match p70 percentile of smoothed signal
  (Balanced default; spike showed adaptive p65/p70 added ~7-8% recall
  over fixed threshold).
- Score-event-anchored gap detection:
  - For each consecutive pair `(t_i, t_{i+1})` in score events.
  - Backward-scan from `t_{i+1} - 0.5s` looking for 1.5s of sustained
    idle (motion below threshold).
  - Apply `rally_dur_min ≥ 5s` gate (Balanced).
  - Emit trim `(t_i - 0.7s lag + 0.5s tail, rally_start - 1.0s pre_pad)`.
- Edges: pre-first-score (treat as gap from 0 to first event), post-
  last-score (treat as gap from last event to video end).
- Public API:
  ```
  def run_rally_detection(
      video_path, roi_corners, score_events, params,
      cancel_check: Callable[[], bool] = lambda: False,
      emit: Callable[[str, dict], None] = lambda *a: None,
  ) -> list[TrimSegment]
  ```
- Pure-logic split: `gaps_to_trims(score_events, motion_signal,
  threshold, params) -> list[TrimSegment]` unit-testable without ffmpeg.

### Step 2 — Backend SSE orchestration  (~3-4h)

Extend `backend/server/routes_auto_trim.py` + `backend/server/state.py`.

- Add `_auto_trim_jobs: dict[str, AutoTrimJobState]` + `_auto_trim_lock`
  to state.py — mirror the existing render-job pattern.
- Endpoints:
  - `POST /api/auto_trim/start` body `{video_token|name, roi,
    score_events, params}` → returns `{job_id}`.
  - `GET /api/auto_trim/events/{job_id}` → SSE stream
    (`text/event-stream`). Events: `stage`, `progress`, `log`,
    `motion_sample` (subsampled to ~10/s), `rally`, `trim`, `done`,
    `error`.
  - `POST /api/auto_trim/cancel/{job_id}` → flip cancel flag; detector
    polls `cancel_check()` each frame.
- Cache layer at `temp/auto_trim_cache/<sha1>.json`. Key = sha1(video_id
  + roi + score_events + params). Cache hit replays all stored events
  + emits `done` in milliseconds.
- Thread spawns a worker that runs `run_rally_detection(...)` with an
  `emit` callback that pushes events into a queue the SSE handler
  drains.

### Step 3 — Frontend modal extension  (~3-4h)

Extend `frontend/auto_trim/` package — modal becomes two-phase:

- **Phase A (existing):** confirm ROI.
- **Phase B (new, unlocked after confirm):**
  - "Run detection" button (disabled until score_events ≥ 10).
  - Stage indicator ("Decoding frame 4521/79320").
  - Progress bar.
  - Live log (scroll-to-bottom, color-coded levels).
  - Optional motion mini-plot canvas (subsamples `motion_sample`
    events).
  - Results panel after `done`: "78 rallies, 41 trims, 12.3 min
    trimmed" + Apply / Discard buttons.
- New modules under `frontend/auto_trim/`:
  - `detection.js` — SSE client (`EventSource`) + state machine
    (idle / running / done / error).
  - Possibly `motion_plot.js` for the mini-plot (defer if running tight
    on time).

### Step 4 — Apply / persist  (~2h)

- `backend/models.py`: `TrimSegment.source: Literal["manual", "auto"]
  = "manual"`. Update `ProjectData` save/load tolerance for legacy
  projects (default to "manual" when field absent).
- `frontend/state.js`: trim shape includes `source`.
- Apply button: filter out existing `source=="auto"` trims, append new
  ones from job result, then trigger `syncAllUI()`.
- `frontend/trims.js`: render small `[AUTO]` badge next to trims with
  `source=="auto"`.
- Re-run replaces only auto trims; manual ones are preserved.

### Step 5 — Tests + edge cases  (~2h)

Pytest pure-logic suite (under `tests/`):

- `gaps_to_trims_score_anchored` — gap edge cases (pre-first-score,
  post-last-score, missed score with very long gap, score events spaced
  closer than `rally_dur_min`).
- `lag_correction` — operator-press-lag math correctness.
- `cache_key_determinism` — same inputs produce same sha1 across runs;
  different score-event order doesn't reorder cache key.

Edge cases in code:

- `score_events < 10` → log warning, disable Run button (per
  PHASE0_REPORT: Balanced requires score events). Future: optional
  blanket-MOG2 fallback (defer to Phase 6 if ever needed).
- Cancel mid-run → cleanup partial state (motion signal half-computed
  cache file deleted).
- Source video < 60s → button stays disabled (no auto-trim use case).
- Score event lands inside an auto trim → frontend toast warning.

### Phase 1b acceptance

- Operator clicks ⚡ Auto Trim → ROI confirm → Run → ~2-3 minutes for a
  22-minute source (per PHASE0_REPORT) → results panel shows trims →
  Apply → trim list updates with `[AUTO]` badges.
- Re-running auto-trim on the same project (no input change) hits cache
  and completes in < 1s.
- Cancel during decode actually stops within ~1s.
- ≥ 95% recall vs. operator-marked manual trims on the 3 spike entries
  in `dataset/<slug>/`.

### Phases 2-5 (post Phase 1b)

Phase 5 from the original sketch (headless auto-trim inside Render
button) and Phase 6 (YOLOv8-pose escalation) remain deferred — revisit
only after operator runs Phase 1b on enough fresh matches to decide
whether algorithm reliability is high enough to skip the modal.

---

## Auto Trim perf phase ✅ SHIPPED 2026-05-20

Algorithm-preserving plumbing pass on the Auto Trim modal. 6 fixes
across 2 days; detector output byte-identical (verified deterministic
re-run + pytest 135/135). Operator confirmed *"tốc độ cải thiện khá ok
rồi, đạt yêu cầu"*.

### What shipped

| # | Fix | Date | File(s) |
|---|---|---|---|
| 1 | Confirm reuses frontend detector result (avoids re-running `detect_roi_multiframe` server-side just to log it) | 2026-05-19 | [routes_auto_trim.py](../backend/server/routes_auto_trim.py), [frontend/auto_trim/api.js](../frontend/auto_trim/api.js) |
| 2 | Parallelize the 4 `_extract_multi_refframes` ffmpeg fast-seek calls | 2026-05-19 | [routes_auto_trim.py](../backend/server/routes_auto_trim.py) |
| 3 | Warm YOLO model + groundtruth cache in a daemon thread at uvicorn `lifespan` startup — moves the first-click cold-load tax (~2-3s) off the operator's critical path | 2026-05-20 | [app.py](../backend/server/app.py), [roi_yolo.py](../backend/roi_yolo.py), [roi/__init__.py](../backend/roi/__init__.py) |
| 6 | **Process-wide cache of groundtruth examples** + ORB features. Invalidated by an mtime+size signature over `dataset/roi_groundtruth/`. Previously every detect call re-`imread`'d 53 jpgs and recomputed ORB features from scratch; now done once per process. | 2026-05-20 | [roi/learned_nn_tier.py](../backend/roi/learned_nn_tier.py) |
| 7 | **Batch YOLO inference** across the 5 multiframe sample frames in one `model.predict()` call — single CUDA launch + single NMS instead of 5 sequential. | 2026-05-20 | [roi_yolo.py](../backend/roi_yolo.py), [roi/yolo_tier.py](../backend/roi/yolo_tier.py), [roi/detector.py](../backend/roi/detector.py) |
| 8 | **ThreadPoolExecutor parallelization** of the 5 frames in `detect_roi_multiframe`. After Fix 7 each per-frame worker only runs the CPU tiers (ORB query + color-contrast); both release the GIL inside numpy/opencv hot loops, so the i9's 16 cores actually do something. | 2026-05-20 | [roi/detector.py](../backend/roi/detector.py) |

### Smoke-test numbers

| Phase | Measured |
|---|---|
| `warm_up_groundtruth_cache` (one-time, in lifespan thread) | 0.71s for 53 examples (HSV feature + ORB features eager) |
| Multiframe ×5 frames, warm | ~2.7s end-to-end |
| Multiframe ×5 frames, re-run | ~2.7s (steady state — no drift) |
| Corner output, two re-runs | byte-identical |

Operator-perceived: first click of session no longer slow; subsequent
clicks comfortable.

### Things deliberately NOT done

- **Fix 4 (precache refframes on video load)** — became premature
  optimization once Fix 3+6 made first-click fast. Design notes are in
  prior commit history if ever needed.
- **Fix 5 (skip 1 of 5 frames in multiframe)** — operator vetoed:
  *"không muốn chạy lại gì để ảnh hưởng tới kết quả ROI table
  detection, vì kết quả đang tốt"*. Re-running LOO to validate would
  risk invalidating accepted accuracy. ROI milestone gate >
  hypothetical 40% speedup. See memory
  `feedback_dont_touch_roi_algorithm_when_results_good.md`.

### Lessons logged for future perf work in this area

- Wall-clock for ⚡ Auto Trim was dominated by **detector compute** (YOLO
  cold-load + 5× ORB tier rebuild), NOT by ffmpeg I/O — which is why
  Fix 2 alone showed no perceptible change. Always profile the dominant
  cost before optimizing the convenient one.
- The pre-existing `ex["orb_features"]` cache only worked WITHIN a
  single `_try_orb_match` call because the examples list was rebuilt
  from disk every time. Persisting the dicts process-wide was the
  single biggest win — same algorithm, ~5× ORB tier speedup.
- ultralytics' `model.predict([img1, img2, ...])` correctly returns a
  positionally-aligned list of Results identical to per-image calls.
  Letterboxing per image keeps each frame's coordinate system
  independent. Safe to batch.

---

## 🔓 ROI MILESTONE UNBLOCKED 2026-05-20

Operator confirmed ROI detect quality acceptable on truly-unseen videos
across the recent batch (53-entry dataset, multi-tier pipeline with
cross-tier IoU consensus + B.5 + B.6 polish + perf bundle 50b8058
shipping the modal latency reduction). The `[ ] Algorithm reach ≥99%
on unseen arenas with no operator edit` gate is considered satisfied
for practical purposes — accuracy is "đạt yêu cầu" per operator.

**Phase 1b (backend trim detector + SSE) is now unblocked.** See the
new "Phase 1b plan" section toward the bottom of this file. The earlier
"Phase 1-5" sketch from 2026-05-17 still applies in spirit; Phase 1b
updates it for the post-refactor layout (server / renderer / auto_trim
are now packages, not single files) and replaces "port the spike script"
with "rebuild from PHASE0_REPORT.md spec" because the spike script was
deleted in 37b474a's disk cleanup (only the report survived).

---

## ⚠ HISTORICAL MILESTONE — ROI auto-detect gates everything (updated 2026-05-19 dawn)

**Update 2026-05-19 dawn (latest):** Phase B.5 (cross-tier IoU clustering) +
Phase B.6 (modal UX with 4 corner zoom panels at 3× + state-leak fix) shipped.

- **B.5 — cross-tier IoU clustering** in `detect_roi_multiframe`. Pool all
  frames whose tier is "trusted" (yolo_seg+*_agree, plain yolo_seg, orb_homography,
  color_contrast_foreground, *_vs_yolo) and cluster by IoU ≥ 0.5 across tier
  labels. A cross-tier cluster of N beats any per-tier cluster of < N. Method
  tag gets `*` suffix when cross-tier consensus won. Fixed DucTu case where
  a 1-frame `+both_agree` beat a 4-frame `+orb_agree` cluster of correct
  corners — random LOO went from mean 0.0139 / max 0.0461 → mean 0.0111 /
  max 0.0168.

- **B.6 — 4-corner zoom panels.** Row of 4 always-on inset canvases below
  the main canvas in the Auto Trim modal, each 180×180px at 3× magnification
  with a green crosshair at the exact corner pixel. Solves "operator can't
  judge sub-percent accuracy without a reference overlay" — they scan the
  4 zooms and verify alignment with the table edge at near-pixel precision.

- **State-leak fix** in `frontend/auto_trim_modal.js`. Modal previously
  preferred `project.info.roi_quadrilateral` (project-level field, written
  on Confirm) over the fresh detector output. When operator confirmed
  video 1 + switched to video 2 without reloading, video 1's saved corners
  showed up on video 2 → "kết quả sai hoàn toàn" symptom. Modal now always
  trusts `det.corners` from the latest detector run.

- **Dataset grew 47 → 53** (operator tested 7 unseen, accepted all 7
  visually after diag overlay verification). Retrained YOLO: Box mAP50-95
  0.880, Mask mAP50-95 0.921 (vs 0.879 at 47 entries). Model backup at
  `assets/models/roi_seg_pre_53entries_backup.pt`.

- **`run.bat` auto-clears caches on startup.** Pre-uvicorn block removes
  `temp/refframes/` + all `__pycache__/` under the repo. Solves "stale
  refframe shows old YOLO model's polygon" + "stale .pyc shadows fresh
  .py" symptoms that confused per-call results.

Production noise floor now ~0.005-0.008 corner err (5-15px on 1080p =
visually OK per operator). Still gated — Phase 1b NOT unblocked.

**Update đêm 2026-05-18:** Multi-frame training shipped.
`build_yolo_dataset.py` now extracts 5 frames per video at 10/30/50/70/90%
duration → 41 entries × 5 frames + 41 refframes = 231 training examples
(was 41). Operator's tripod is fixed per match so all frames of a video
share the same `latest_corners` label — this is REAL variance (player
position, ball, scoreboard, spectator background) that augmentation
can't synthesize.

YOLO retrained 4.6 min on 231 examples. Val set 8 → 43 (more
statistically meaningful early-stop signal). Cross-validated wins
post-retrain: 33/41 (was 29/41 with 41-entry training).

**Note:** Mirror-flip option (82 dataset via horizontal flip) was rejected
— `fliplr=0.5` already in train augmentation → pre-flipping is redundant.

Source videos for re-training live at `D:\Table Tennis Video\2026\Match`.
Tan.MP4 / Dat.MP4 / Ha.MP4 sources no longer exist; those 3 entries use
refframe-only fallback.

**Earlier (tối same day):** Phase B retrained on 41 + cross-check shipped.



**Tạm ngưng mọi thứ ở ROI table. Khi nào detect ROI đúng với nhiều input
khác nhau thì mới phát triển tiếp.**

**Update tối 2026-05-18 (latest):** Phase B retrained on 41-entry dataset
+ algorithm upgrade with YOLO-vs-classical-CV cross-check shipped.

Production analysis (`scripts/analyze_detector_errors.py`) on 41 confirms
showed YOLO confidence is uninformative (mean 0.995, max err 0.44 at
conf 0.975). 32% of latest detector_proposed errors exceeded 0.05.
1 catastrophic case: 0331_TrungThao_vs_PhuongBa_2_3-1.MP4 with
multiframe[5/5]:yolo_seg confidently picked wrong table across all 5
frames.

Fix shipped 2026-05-18 tối:
1. Retrained YOLO with 41-entry data — Box mAP50-95 0.894 → 0.920
2. `detect_roi` now runs YOLO + ORB + color-contrast in parallel,
   cross-validates. New method tags: `yolo_seg+both_agree` /
   `+orb_agree` / `+color_agree` (high trust) and
   `orb_homography_vs_yolo` / `color_contrast_vs_yolo` (YOLO rejected)
3. Multi-frame priority gates reordered: cross-validated agreement at
   top; ORB cluster ≥ 3 high; YOLO singleton demoted to mid-priority;
   `color_contrast_foreground` singleton gate REMOVED (100% prod fail)

Post-fix LOO 41/41 pass, mean 0.0125, max 0.0322. The catastrophic
0331_TrungThao case dropped 0.443 → 0.019 (×24 better). **LOO is still
overconfident on YOLO** since test uses model trained on all 41
entries; true unseen accuracy requires operator testing on fresh videos.

**Earlier history (kept for context):**

**Update tối 2026-05-17:** Phase A.5 shipped (color-contrast foreground +
multi-frame + priority gating). LOO trên 13 entries mean 0.0409 — all 13/13
pass 0.10 threshold. Truly-unseen tests showed ~70-80% accuracy with ~20%
catastrophic failures (fence merge / off-center table / multi-table merge).
Classical-CV ceiling reached.

**Phase B infrastructure shipped same evening:** YOLOv8-seg pipeline ready
(`scripts/build_yolo_dataset.py`, `scripts/train_roi_seg.py`,
`backend/roi_yolo.py`, integration into `roi_detector.py` as Stage -1).
Operator committed to growing dataset 13 → 50 entries; first training run
will validate pipeline at ~25 entries.

### Vì sao milestone này

Phase 0 spike chứng minh trim-detection algorithm đã tối ưu hết mức không-ML.
Quality gap còn lại từ **ROI sai**, không phải logic detection. Assistant
eyeball ROI từ 1 refframe → sai bét cho E1 và E2. Cần feedback loop với
operator để collect ground-truth ROIs trước khi tune tiếp.

### Workflow milestone

1. Operator click **Auto Trim** trong Trim panel
2. Backend extract refframe at video midpoint
3. Backend chạy `roi_detector.detect()` → propose 4 corners
4. Modal hiển thị refframe + ROI overlay
5. Operator:
   - **Confirm** (ROI đúng) → save vào `project.info.roi_quadrilateral`
     + append vào `dataset/roi_groundtruth/<hash>.json` (training data)
   - **Edit** → drag 4 corners trên canvas → confirm → same save
   - **Reject / re-detect**
6. **STOP HERE.** Trim detection NOT yet wired up.

Mọi backend khác (rally detector, SSE log/progress, trim apply…)
**defer** đến khi ROI auto-detect đạt quality bar operator chấp nhận
trên ≥10 inputs khác nhau.

### Unlock criteria

- [x] Build modal UI cho ROI confirmation/edit                                     (2026-05-17)
- [x] Build `roi_detector.py` (v0 naive color-based)                               (2026-05-17)
- [x] Build endpoints (refframe, detect_roi, confirm_roi, groundtruth_count)       (2026-05-17)
- [x] Collect ≥10 confirmed ROIs từ different videos                               (2026-05-17, dataset/roi_groundtruth/)
- [x] Iterate `roi_detector` algorithm — v6 with ORB + sanity + HSV fallback       (2026-05-17)
- [x] Algorithm đạt ≥80% correctness on seen data — 100% on 10 self-tests          (2026-05-17, user-validated in modal)
- [x] First unseen-video test: 3 fails caught → led to Phase A.5 algorithm rewrite  (2026-05-17 tối)
- [x] Phase A.5: color-contrast foreground + multi-frame cluster + priority gates  (2026-05-17 tối)
- [x] Dataset 10 → 13 (3 unseen failures confirmed, used to validate Phase A.5)    (2026-05-17 tối)
- [x] LOO err on 13 entries: mean 0.0380, max 0.0796 — all pass 0.10               (2026-05-17 tối)
- [x] Phase B first training (38 entries) — LOO 38/38 mean 0.0126                  (2026-05-18 sáng)
- [x] Grow dataset 38 → 41 via operator workflow                                   (2026-05-18 tối)
- [x] Production error analysis on history[] — 13/41 (32%) fail rate identified    (2026-05-18 tối)
- [x] Phase B retrain (41 entries) — Box mAP50-95 0.894 → 0.920                    (2026-05-18 tối)
- [x] Algorithm upgrade: YOLO-vs-classical cross-check + tier reorder              (2026-05-18 tối)
- [x] Post-upgrade LOO 41/41 pass mean 0.0125 max 0.0322                           (2026-05-18 tối)
- [x] Multi-frame training: 41 → 246 examples (198 train / 48 val), 41/41 multi-frame  (2026-05-18 đêm)
- [x] Aliases for renamed sources (Tan/Ha/Dat → long names) + leak-free split       (2026-05-18 đêm)
- [x] Honest multi-frame LOO via --search-path: **39/41 (95%) pass**, 2 hard cases   (2026-05-18 đêm)
- [x] Priority-1 unseen test: 5 videos. 1/5 perfect, 4/5 visually wrong              (2026-05-18 khuya)
- [x] Root-cause: `_polygon_to_quad` returned kite-shape from approxPolyDP. v2 extreme-points fix degenerated on tilted masks. v3 max-area approxPolyDP + minAreaRect fallback. LOO mean **0.020** (best), 44/46 pass                                                          (2026-05-18 khuya)
- [ ] **Re-test priority-1 unseen videos with v3 fix — NEXT STEP** (restart run.bat first)
- [ ] Algorithm reach ≥99% on unseen arenas with no operator edit
- [ ] → Unlock Phase 1b (backend trim detector + SSE) + Phase 2-5

**End-state goal (operator stated 2026-05-17):** ROI detection must be 100%
automatic. NO operator review in long-term workflow. Manual confirmation now
is only to seed the training set for iterative improvement. Path forward:
ORB-homography (Phase A, done) → grow dataset → YOLOv8-seg (Phase B, after
≥30 confirms) → multi-modal ensemble (Phase C). See PHASE_A_REPORT below.

---

## Phase 1a — ROI workflow modal ✅ SHIPPED 2026-05-17

**Files mới/sửa:**
- `backend/roi_detector.py` (mới) — `detect_roi(refframe_path) -> RoiDetection`
- `backend/server.py` — endpoints:
  - `GET /api/auto_trim/refframe?name=X|token=X` — serve JPG midpoint frame
  - `POST /api/auto_trim/detect_roi` body `{name|token}` → `{corners, confidence, method, debug}`
  - `POST /api/auto_trim/confirm_roi` body `{name|token, corners, was_edited}` → saves project + groundtruth (+ copies refframe alongside)
  - `GET /api/auto_trim/groundtruth_count` — milestone progress
- `backend/models.py` — `ProjectInfo.roi_quadrilateral: Optional[List[List[float]]]`
- `frontend/auto_trim_modal.js` (mới) — modal logic, canvas, 4-corner drag, edit toggle, SSE-style log
- `frontend/index.html` — full-screen modal HTML + tailwind palette extension (success/warn shades)
- `frontend/trims.js` — "⚡ Auto Trim" button in Trim panel header
- `frontend/state.js` — `roi_quadrilateral: null` default
- `requirements.txt` — opencv-python>=4.13
- `dataset/roi_groundtruth/<video_id>.{json,jpg}` (auto-created on each confirm)

**Modal scope:** ROI confirmation/editing ONLY. Trim detection stays deferred until ROI auto-detect proven on more inputs.

---

## Phase A — ORB keypoint + homography (algorithm v6) ✅ SHIPPED 2026-05-17

**3-stage detector** in `backend/roi_detector.py`:

```
Stage 0  ORB keypoints + RANSAC homography transfer
         ├─ Compute ORB features per groundtruth refframe (1000 kp, scale 1.2, 8 levels)
         ├─ BFMatcher Hamming + findHomography RANSAC (reproj 4 px)
         ├─ Sanity checks (3 layers):
         │    1. corners stay in [-0.15, 1.15] bounds
         │    2. max corner displacement ≤ 0.12 (rejects over-fit H)
         │    3. area ratio in [0.5, 2.0] (rejects degenerate quads)
         └─ confidence = 0.7 + (inliers - 15)/200, scaled by displacement penalty

Stage 1  HSV histogram NN (fallback when ORB fails)
         ├─ Tier exact   (sim ≥ 0.65)  → reuse top-1 corners directly
         ├─ Tier blend   (sim ≥ 0.40)  → top-K=3 similarity-weighted average
         └─ Tier mean    (else, n≥2)   → simple mean of all examples

Stage 2  Naive color-blue / color-green (fallback when no groundtruth)
         └─ Search band Y∈[0.45, 0.95] X∈[0.10, 0.90], shape+aspect filters
```

### Validation (leave-one-out on 10 confirmed ROIs)

| Variant | Mean err | Median | Max | Notes |
|---|---:|---:|---:|---|
| v1 (HSV all-blend) | 0.0626 | 0.0636 | 0.1322 | baseline |
| v3 (HSV top-K) | 0.0624 | 0.0636 | 0.1322 | marginal |
| v5 (ORB no sanity) | 0.0727 | 0.0495 | 0.2746 | over-fits |
| **v6 (ORB+sanity+HSV)** | **0.0469** | **0.0432** | 0.1322 | locked |

### v6 per-input breakdown

| Source | Method | Err | Notes |
|---|---|---:|---|
| DoBinhMinh, Tan, Ha | ORB exact (96/94/78 inliers) | 0.006 | pixel-accurate |
| CuongPhan, NguyenTon | ORB exact (cluster B pair) | 0.043 | good |
| MinhDuong | ORB rejected → HSV blend | 0.016 | sanity check fired |
| 0504_Vinh, VanKhanh, Dat | HSV mean fallback | 0.06-0.08 | singletons |
| QuangVinh | HSV NN | 0.132 | borderline cluster A |

**Production self-test:** user opened all 10 confirmed videos in modal → 10/10 pixel-accurate (without operator edit).

### Lessons learned

- **HSV similarity alone is a ceiling at ~6% error** — color histograms can't distinguish same-arena/different-angle from different-arena/similar-lighting.
- **ORB+homography is pixel-accurate WITHIN a cluster** but can over-fit when matches concentrate on busy background features (banners, equipment). The 3-layer sanity check is essential.
- **Singletons (1 confirm per arena) can't use ORB** — need ≥2 inputs/cluster for at least one match candidate. Currently 4/10 are singletons.

### Open work — toward 100% accuracy goal

| Target | Status | Action |
|---|---|---|
| 100% on seen arenas (cluster has ≥1 match) | ~99% achieved | Done via Phase A |
| 100% on **unseen** arenas | NOT YET | Cần Phase B |
| Grow dataset 10 → 20 (cluster expansion) | 10/20 | Operator continues workflow |
| Grow dataset 20 → 30 (cluster diversity) | 0/30 | Operator continues workflow |
| Phase B: YOLOv8-seg fine-tune | DEFERRED | Triggered at ≥30 confirms |
| Phase C: Multi-modal (court lines + net) | DEFERRED | After Phase B if needed |

**Current ask of operator:** continue using Auto Trim modal in normal workflow. Each match adds an example. Priorities:
1. **Quay thêm matches từ arenas ĐÃ có trong groundtruth** (biến singletons thành clusters) — high value, ORB will hit on next match
2. **Quay matches từ arenas mới** — needed for Phase B training diversity
3. **Avoid re-confirming the same video** unless adjustments wanted

### Files updated in Phase A

- `backend/roi_detector.py` — full rewrite, 3-stage detector + sanity checks
- `frontend/auto_trim_modal.js` — top-3 similar list shown in modal info panel
- `frontend/index.html` — modal banner clarifying "trim detection deferred"
- This file (docs/TODO.md)
- `docs/AUTO_TRIM_DISCUSSION.md`
- Memory `project_auto_trim_feature.md`

### Remaining diagnostic capabilities

- `scripts/spike_show_roi.py` — render current confirmed ROI on each refframe.png for visual audit
- `scripts/spike_out/<slug>_autodetect_v4_orb.png` — overlays showing ORB result on 3 dataset entries
- `scripts/test_roi_detector.py` — LOO regression (multi-frame, finds videos in subfolders)
- `scripts/debug_roi_overlay.py` — render 3-panel (overlay + blue/red masks) per dataset entry

---

## Phase A.5 — Color-contrast + multi-frame + tier priority ✅ SHIPPED 2026-05-17 tối

**Trigger:** First unseen-video tests (3 cases) all failed. ORB tier
silently transferred corners from wrong-arena examples (background tables,
walls). Operator observation: foreground table is consistently **blue
top + red carpet around it**, and has **larger white court lines** than
background tables. Phase A.5 exploits these universal physical features
to escape dataset dependence on unseen arenas.

### What shipped

**4-stage detector** in `backend/roi_detector.py` (new tier 0):

```
Stage 0  Foreground table by colour contrast (universal physics)
         ├─ HSV blue mask (95-130, 80-255, 45-220)
         ├─ HSV red mask (split at hue wrap: 0-12 ∪ 168-180)
         ├─ Morph open ELLIPSE 9×9 (critical: rect-kernel keeps diagonal
         │  connectors that bridge fg-table to skirts/walls; ellipse snips)
         ├─ Morph close RECT 5×5 (fill court-line gaps within blob)
         ├─ For each blue blob:
         │    - Filters: area [1.2%, 22%], aspect [1.3, 4.5],
         │      top_y > 0.25, bot_y < 0.95, center_y ∈ [0.40, 0.85],
         │      solidity ≥ 0.55 (relaxed from 0.82 — bitten blobs ok)
         │    - Dilate 30px → ring sample → red_frac in ring ≥ 0.30
         │    - score = red_frac × area_frac × centrality
         │      (centrality: 1.0 at cx=0.5 → 0.3 at edges, multiplicative)
         ├─ Best blob → fit quad via approxPolyDP-on-hull (4 corners
         │  preferred, minAreaRect fallback)
         └─ Reject if quad_area / blob_area > 1.5 (over-extension catch)

Stage 1  ORB keypoints + RANSAC homography (Phase A, unchanged)

Stage 2  HSV NN/blend/mean (Phase A fallback)

Stage 3  Naive colour-blue/green
```

**Hybrid disagreement logic** between Stage 0 + Stage 1 (single frame):
- Both succeed + IoU ≥ 0.45 → use ORB (sub-pixel via homography)
- Both succeed + disagree + color_contrast red_surround ≥ 0.40 → color_contrast
  wins regardless (catches wrong-arena ORB transfer)
- Both succeed + disagree + weak signals → higher-confidence wins

### Multi-frame aggregation

`detect_roi_multiframe()` runs full pipeline on **5 evenly-spaced refframes**
(10/30/50/70/90%) and aggregates via **tier-priority gating**:

```
Gate 1: color_contrast+orb_agree any cluster ≥ 1    (both detectors converged)
Gate 2: orb_homography cluster ≥ 2                  (same-arena consensus)
Gate 3: color_contrast cluster ≥ 2                  (universal-feature consensus)
Gate 4: learned_* cluster ≥ 4                       (strong learned consensus beats
                                                     marginal cc singleton)
Gate 5: color_contrast singleton                    (cc fires once → still trust it)
Gate 6: orb_homography singleton
Gate 7+: learned_* cluster ≥ 2 → fall through
```

Within winning gate: per-corner median across same-tier cluster members.

**Why tier-priority instead of raw majority:** learned_nn/blend return
CORRELATED predictions across frames (same dataset queried every time) →
3 correlated wrong answers should not out-vote 1 grounded right answer.
Tier-priority prevents `learned_*` plurality from dominating
`color_contrast` singleton when the latter is the only frame-grounded
result.

Server endpoints `POST /api/auto_trim/detect_roi` + `confirm_roi` rewired
to use `_extract_multi_refframes` + `detect_roi_multiframe`. Midpoint
frame shared with the legacy single-frame cache so the operator-facing
refframe is identical.

### Validation (leave-one-out on 13 confirmed ROIs)

| Variant | Mean err | Max err | Pass rate | Notes |
|---|---:|---:|---|---|
| v6 (Phase A only, 10 entries) | 0.0469 | 0.1322 | 10/10 | baseline |
| Phase A.5 single-frame (13 entries) | ~0.045 | ~0.10 | 12/13 | intermediate |
| **Phase A.5 multiframe + priority (13)** | **0.0380** | **0.0796** | **13/13** | locked |

### Phase A.5 per-input breakdown (13 entries LOO)

| # | Source | Winning gate | Err |
|---|---|---|---:|
| 1 | TuanPhu (was unseen, err 0.408 pre-A.5) | cc 5/5 cluster | 0.040 |
| 2 | ThienQ7 (was unseen) | cc 4/5 cluster | 0.060 |
| 3 | DoBinhMinh | ORB 5/5 cluster | 0.006 |
| 4 | Tan | ORB 5/5 cluster | 0.006 |
| 5 | CuongPhan | ORB 5/5 cluster | 0.040 |
| 6 | FQuang (was unseen) | learned_blend 4-cluster | 0.061 |
| 7 | VanKhanh | learned_mean 5/5 | 0.079 |
| 8 | NguyenTon | ORB 5/5 cluster | 0.042 |
| 9 | Dat | learned_mean 5/5 | 0.080 |
| 10 | vsVinh | cc+orb_agree (both converged!) | **0.015** |
| 11 | MinhDuong | learned_blend 2/5 | 0.035 |
| 12 | Ha | ORB 5/5 cluster | 0.004 |
| 13 | QuangVinh | cc 3/5 cluster | 0.025 |

**4/13 entries (31%)** use color_contrast tier — universal physics
engaging on multiple venues. **8/13** still on ORB (trained venues).
**1/13** falls to learned_blend (FQuang — cc fires false-positive on 1
frame but 4-frame learned cluster overrides).

### Lessons learned in Phase A.5

1. **MORPH_RECT vs MORPH_ELLIPSE matters more than kernel size.** Rect
   leaves the 4 corner pixels intact, preserving diagonal connectors.
   Ellipse has no corners → cleanly snips ~3px diagonal threads that
   would otherwise bridge separate blobs. Single line change unblocked
   3 venues.
2. **Centrality bias is essential** when multiple tables visible. Side
   background tables pass red_surround + area filters but lose on
   centrality (cx ≈ 0.05 → score × 0.30). Foreground table at cx ≈ 0.50
   wins via × 1.0 multiplier.
3. **Solidity ≥ 0.82 was too tight** — player standing on table bites
   the blob → solidity 0.66 → rejected. Relaxed to 0.55; the
   quad-vs-blob area-ratio check at 1.5 catches actual over-extension.
4. **Multi-frame raw majority is unreliable** when failure mode is
   `learned_*` returning correlated guesses. 3 correlated wrong answers
   != 3 independent votes. Tier-priority fixes this without disabling
   `learned_*` consensus when it's the only available signal.
5. **3 unseen-test failures provided more algorithm signal than 10
   trained ROIs.** The previously-trained 10 all worked via ORB; the
   3 failures revealed every weakness of color_contrast tier. Confirm
   failures **immediately** when they happen — each one is a high-
   value labeled negative.

### Open work (Phase A.5 → 100%)

| Target | Status | Action |
|---|---|---|
| Truly-unseen test pass rate measurement | NEXT | Operator tests on videos not in dataset (0201_BaoNgoc, 0207_QuangVinh, etc.) |
| If ≥80% unseen pass → Phase A.5 SHIPPED, unlock Phase 1b | gated | Test results pending |
| If 50-80% unseen pass → tune filters or add white-line discriminator | contingent | Implementation queued |
| If <50% unseen pass → escalate to Phase B (YOLOv8-seg) earlier | contingent | Dataset still small for fine-tune |
| Grow dataset 13 → 20 (cluster expansion) | 13/20 | Operator continues workflow |
| Grow dataset 20 → 30 (cluster diversity) | 13/30 | Operator continues workflow |
| Phase B: YOLOv8-seg fine-tune | DEFERRED | Triggered at ≥30 confirms OR if A.5 misses |
| Phase C: Multi-modal (court lines + net detect) | DEFERRED | Plan B if A.5 + dataset growth not enough |

### Files updated in Phase A.5

- `backend/roi_detector.py` — added Stage 0 `_try_foreground_by_color_contrast()` + helpers (`_fit_quad_to_blob`, `_quad_iou`) + `detect_roi_multiframe()` + tier-priority gates
- `backend/server.py` — added `_extract_multi_refframes()`, rewired 2 endpoints to multi-frame
- `scripts/test_roi_detector.py` — rewritten for multi-frame LOO, locates videos via rglob (paths in groundtruth.json may be stale)
- `scripts/debug_roi_overlay.py` — 3-panel diagnostic
- `dataset/roi_groundtruth/` — 10 → 13 entries
- This file
- `docs/AUTO_TRIM_DISCUSSION.md`
- Memory `project_auto_trim_feature.md`

---

## Phase B.2 — Multi-frame training 🟢 SHIPPED 2026-05-18 đêm

**Trigger:** operator asked if mirror-flipping the dataset to 82 would
help YOLO training. Answer: NO (already covered by `fliplr=0.5` aug).
Real opportunity: tripod is fixed → every frame of a video shares the
same ROI corners → can extract N frames per video at training time for
REAL data variance (player positions, ball, lighting, background).

### Changes

**`scripts/build_yolo_dataset.py`** ([scripts/build_yolo_dataset.py](../scripts/build_yolo_dataset.py)):
- New flag `--frames-per-video N` (default 5) — extract N evenly-spaced
  frames at 10/30/50/70/90% duration. Matches inference-time sampling.
- New flag `--search-path PATH` — additional directory to recursively
  search for source videos (operator archives outside `videos/`).
- Falls back to `dataset/manifest.json` → `dataset/<slug>/source.*` for
  auto-archived render entries.
- Falls back to cached refframe alone when source is missing.

### Pipeline run

```bash
./venv/Scripts/python.exe -X utf8 scripts/build_yolo_dataset.py \
  --force --frames-per-video 5 \
  --search-path "D:/Table Tennis Video/2026/Match" \
  --alias "Tan.MP4=0510_NguyenKhanhTan_3-1.MP4" \
  --alias "Ha.MP4=0510_HoangHuuHa_1-3.MP4" \
  --alias "Dat.MP4=0406_DatDo_0-3.MP4"
# → 41 videos × (5 frames + refframe) = 246 examples
# → train 198, val 48 (leak-free split: aliased entries land same split as target)

./venv/Scripts/python.exe -X utf8 scripts/train_roi_seg.py
# ~5 min on RTX 5060 Ti
```

Aliases needed because operator renamed 3 source files (Tan/Ha/Dat → long
descriptive names) after originally confirming the ROI. Without aliases,
those 3 entries fall back to refframe-only (no multi-frame).

### Val metrics post multi-frame

| | Old (8-val single-frame) | New (48-val multi-frame) |
|---|---:|---:|
| Box mAP50 | 0.995 | 0.995 |
| Box mAP50-95 | 0.920 | 0.923 |
| Mask mAP50 | 0.995 | 0.995 |
| Mask mAP50-95 | 0.880 | 0.913 |

Both mAP50-95 metrics improved on the larger (48-example) val set —
the multi-frame model generalizes better AND is being tested on a
harder/more representative val pool.

### LOO post multi-frame (honest, multi-frame inference)

After extending `test_roi_detector.py` with `--search-path` + `--alias`
flags (was falling back to refframe-only because videos live outside
`videos/`), honest multi-frame LOO yields:

**39/41 (95%) pass at threshold 0.10**. Mean 0.0289. Max 0.4415.

Method breakdown:
- 25 `multiframe[5/5]` consensus
- 3+3+3 partial clusters (`multiframe[2/5]`, `[3/5]`, `[4/5]`)
- 5 `yolo_seg+both_agree`
- 1 `color_contrast_vs_yolo` (also one of the failures — bad override)

2 hard cases (same throughout iteration):
- 0331_TrungThao_vs_PhuongBa_2_3-1.MP4 err 0.44 — YOLO confidently picked
  wrong table across 4/5 frames; no classical signal strong enough to override
- 0510_ThaoBa_vs_TuanAnhNhueMinh err 0.27 — `color_contrast_vs_yolo`
  override fired but went the wrong way (color was wrong, YOLO was right)

Net: from original 32% (13/41) prod failure rate → **5% (2/41) on
honest LOO** — 6× improvement on the same dataset. Real generalization
to truly-unseen videos still pending operator's restart + test.

### What we expect to improve in production

Multi-frame training teaches the model:
- Player-occlusion robustness (mid-rally frames have players moving)
- Ball-position invariance (ball appears in random spots, model
  shouldn't lock onto it as a "feature")
- Slight lighting drift within a match
- Spectator/background motion patterns

These are precisely the cases where the previous single-frame-trained
model failed in production (32% prod fail rate from history analysis).

### Files updated in Phase B.2

- `scripts/build_yolo_dataset.py` — multi-frame extraction
- `assets/models/roi_seg.pt` — retrained on 231 examples
- `assets/models/roi_seg_pre_multiframe_backup.pt` — Phase B.1 snapshot
- This file + `docs/AUTO_TRIM_DISCUSSION.md`
- Memory `project_auto_trim_feature.md`

### Open work (toward Phase 1b unlock)

| Step | Status |
|---|---|
| Restart `run.bat` to load multi-frame model | pending (operator) |
| Test 3-5 truly-unseen videos | pending (operator) |
| If still insufficient: bump `--frames-per-video` 5 → 10 | contingent |
| If 200+ examples + still need more capacity: try YOLOv8s-seg | contingent |
| If working ≥80% unseen: unlock Phase 1b trim detection backend | gating |

---

## Phase B.1 — Cross-check + retrain 🟢 SHIPPED 2026-05-18 tối

**Trigger:** operator tested first-train model on seen + unseen videos →
most needed edit. `scripts/analyze_detector_errors.py` over 41 confirms
showed 13/41 (32%) with detector_proposed error > 0.05; worst case
0.4427 on `0331_TrungThao_vs_PhuongBa_2_3-1.MP4`. YOLO confidence
uncorrelated with accuracy (mean 0.995, max err 0.44 at 0.975).

### Algorithm changes

**1. Cross-check YOLO against classical CV in `detect_roi`**
([backend/roi_detector.py:1330](../backend/roi_detector.py#L1330))

Previously YOLO won outright when mask area sanity passed. Now we always
run color-contrast + ORB in parallel and compute IoU agreement:

```
yolo + orb_iou ≥ 0.45 + color_iou ≥ 0.45  → "yolo_seg+both_agree"   (+0.05 conf bonus)
yolo + orb_iou ≥ 0.45                       → "yolo_seg+orb_agree"
yolo + color_iou ≥ 0.45                     → "yolo_seg+color_agree"
yolo disagrees + orb_conf ≥ 0.80            → trust ORB, tag "orb_homography_vs_yolo:<src>"
yolo disagrees + color red_surround ≥ 0.50  → trust color, tag "color_contrast_vs_yolo"
yolo with no classical competitor           → keep yolo_seg (novel-arena case)
```

**2. Multi-frame priority gates reordered**
([backend/roi_detector.py:1184](../backend/roi_detector.py#L1184))

```
Top tier: cross-validated agreement (yolo_seg+both_agree / +orb_agree / +color_agree)
Next:     color_contrast+orb_agree (no YOLO but both classicals agree)
Next:     orb_homography cluster ≥ 3 (was 2) — strongest classical signal
Next:     orb_homography_vs_yolo singleton (YOLO already rejected)
Next:     color_contrast_vs_yolo singleton
Then:     yolo_seg cluster ≥ 2 (was 1) — needs multi-frame consensus
Then:     orb_homography / color_contrast_foreground cluster ≥ 2
Then:     yolo_seg singleton (no classical competitor, last resort for novel arena)
Then:     learned_* cluster ≥ 4, orb singleton, learned_* cluster ≥ 2, etc.

REMOVED: color_contrast_foreground singleton (was rank 7) — 100% prod fail
```

**3. Retrained YOLO** with 41-entry dataset. Time 1.3 min on RTX 5060 Ti.
Val Box mAP50-95: 0.894 → 0.920. Best model copied to `assets/models/roi_seg.pt`,
prev saved as `assets/models/roi_seg_pre_41_backup.pt`.

### Results (LOO post-changes, 41 entries)

| Metric | Pre Phase B.1 | Post Phase B.1 |
|---|---:|---:|
| Mean err | 0.0126 | **0.0125** |
| Max err | 0.0317 | **0.0322** |
| Pass rate (0.05) | 38/38 (LOO) but 28/41 prod | **41/41 LOO** (prod test pending) |
| 0331_TrungThao | 0.4427 (prod) | **0.0187** (LOO) |
| Cross-validated wins | 0 | **29/41** entries |

Method breakdown post-upgrade: 22 yolo+orb_agree, 5 yolo+color_agree,
2 yolo+both_agree, 7 yolo plain (novel-arena), 5 multiframe.

**LOO is still overconfident** — YOLO is tested on training data. True
production accuracy unknown until operator retests Auto Trim on fresh videos.

### Files updated

- `scripts/analyze_detector_errors.py` — NEW, error breakdown from history[]
- `backend/roi_detector.py` — cross-check in `detect_roi`, priority gates
- `assets/models/roi_seg.pt` — retrained
- `assets/models/roi_seg_pre_41_backup.pt` — previous model
- This file + `docs/AUTO_TRIM_DISCUSSION.md`
- Memory `project_auto_trim_feature.md`

### Open work (Phase B.1 → unlock Phase 1b)

| Target | Status |
|---|---|
| Restart `run.bat` to load new model + algorithm | pending (operator-triggered) |
| Test 3-5 truly-unseen videos via Auto Trim modal | pending |
| If method tag shows `+orb_agree` / `+color_agree` / `+both_agree` | strongest possible signal |
| If method tag shows `orb_homography_vs_yolo` / `color_contrast_vs_yolo` | YOLO was overridden — note for future training |
| If method tag still `yolo_seg` (no agreement) on a wrong result | needs more data covering similar venues |
| Acceptance: ≥80% unseen pass without edit | gates Phase 1b unlock |

---

## Phase B — YOLOv8-seg fine-tune 🟢 FIRST TRAIN COMPLETE 2026-05-18

### First training run (38 confirmed ROIs)

Operator grew dataset 13 → 38 in 1 day. Trained YOLOv8n-seg on
2026-05-18, 30 train / 8 val split, 120 epochs, RTX 5060 Ti.

**Training time: 1.3 minutes** (NOT 30-60 min as estimated — Blackwell sm_120 + AMP + small dataset is very fast).

**Val metrics:**
| Metric | Box | Mask |
|---|---|---|
| Precision | 0.991 | 0.991 |
| Recall | 1.000 | 1.000 |
| mAP50 | 0.995 | 0.995 |
| mAP50-95 | 0.894 | 0.899 |

(Val set only 8 examples → overconfident metrics. Real test on unseen videos needed.)

**LOO error across all 38 entries (rotation-invariant, see below):**
| Tier | Mean err | Max err | Pass rate |
|---|---:|---:|---|
| Phase A.5 classical CV | 0.0506 | 0.1695 | 18/22 (82%) |
| **Phase B YOLOv8n-seg** | **0.0126** | **0.0317** | **38/38 (100%)** |

YOLOv8 is ~4× more accurate on the trained set. Confidence 0.81-0.98 consistently.

### Critical gotchas hit during first run

1. **PyTorch CPU install by default.** `pip install ultralytics` pulls CPU-only torch from PyPI. For RTX 5060 Ti (Blackwell sm_120) needed force-reinstall from CUDA 12.8 index: `pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch torchvision`. Result: torch 2.11.0+cu128, all working.

2. **Corner ordering mismatch.** Initial LOO showed YOLO err 0.146 (worse than classical CV). Visual inspection of overlays: predicted quad **overlapped groundtruth perfectly**, but corner labels (TL/TR/BR/BL) were rotated by 1-2 positions vs groundtruth's storage order. Root cause: `_order_clockwise_from_tl` used `min(x+y)` to pick TL, but operator's convention is `min(y)` (topmost first). Validated on 30 groundtruth entries: 23/30 match `min_y` vs 6/30 for `min(x+y)`. Fixed both `_order_clockwise_from_tl` (in roi_detector.py) and `_order_clockwise_from_tl_local` (in roi_yolo.py). After fix: LOO mean 0.013.

3. **numpy.float32 serialization crash.** Earlier Phase A.5 issue — debug dict contained `quad_blob_ratio` as numpy.float32 from operations on `quad_px` arrays; Pydantic couldn't serialize. Fixed in `roi_detector.py` (wrap in `float()` at the leak point) + added recursive `_sanitize_for_json` in `server.py` as defense-in-depth.

### Phase B status

- ✅ Infrastructure shipped 2026-05-17 (scripts + roi_yolo + integration)
- ✅ First training successful 2026-05-18 with 38 entries
- ✅ LOO 38/38 pass on trained-set with rotation-invariant error
- ⏳ TRULY-unseen video test (not yet performed)
- ⏳ Continue dataset growth → 50 entries → retrain → final validation
- ⏳ Decide: keep classical CV (Phase A.5) as fallback OR remove

### Files added/changed in Phase B activation

- `requirements.txt` — `ultralytics>=8.3.0` (pulls torch CUDA build separately)
- `scripts/build_yolo_dataset.py` — converted dataset to YOLO seg format
- `scripts/train_roi_seg.py` — trained model, copied to `assets/models/roi_seg.pt`
- `scripts/test_roi_detector.py` — rotation-invariant error
- `backend/roi_yolo.py` — fixed `_order_clockwise_from_tl_local` to use min_y
- `backend/roi_detector.py` — fixed `_order_clockwise_from_tl` to use min_y; numpy float32 leak
- `backend/server.py` — `_sanitize_for_json` helper applied to detect/confirm endpoints
- `assets/models/roi_seg.pt` (gitignored, 6.8MB) — trained checkpoint
- `runs/segment/roi_seg/` (gitignored) — training logs + plots
- `dataset/yolo_seg/` (gitignored) — converted YOLO-format dataset

---

## Phase B — YOLOv8-seg fine-tune 🟡 INFRASTRUCTURE READY 2026-05-17 tối

**Trigger:** Phase A.5 unseen tests showed 70-80% accuracy with ~20% catastrophic failures (quad lan ra fence / multiple tables merged / table off-center missed). Operator confirmed classical-CV ceiling reached and committed to growing dataset → 50 confirms while infrastructure is staged.

### What ships now (code only — no model training yet)

- `requirements.txt` — added `ultralytics>=8.3.0` (pulls torch ~700MB CUDA). Lazy at runtime; backend imports cleanly even before pip-install completes.
- `scripts/build_yolo_dataset.py` — converts `dataset/roi_groundtruth/*.json` (+ sibling .jpg refframes) into YOLOv8-seg format under `dataset/yolo_seg/` with deterministic 80/20 train/val split by hash. Re-runnable; idempotent.
- `scripts/train_roi_seg.py` — fine-tune YOLOv8n-seg on the converted dataset. Augmentation tuned for small-dataset / single-class scenario (mosaic 1.0, mixup 0.15, hsv_h 0.02 / hsv_s 0.7 / hsv_v 0.6, perspective 0.0008, fliplr 0.5, flipud 0 — vertical flip would put red floor at top which never happens). Outputs `assets/models/roi_seg.pt`.
- `backend/roi_yolo.py` — lazy model loader + single-image inference. Returns 4-corner quad from highest-confidence mask. Caches `False` on missing model so subsequent calls cost one bool check.
- `backend/roi_detector.py` — new Stage -1 `_try_yolo_seg()` as top of pipeline. Wins outright when YOLO confidence ≥ 0.55 and mask area in [1%, 40%]. Multi-frame priority gate `("yolo_seg", 1)` ahead of all classical-CV gates.
- `.gitignore` — `assets/models/` + `runs/` excluded (operator-local model + training logs, regenerable from dataset).

### Operator workflow when dataset reaches threshold

```
# Step 0 — install heavy deps (one-time, ~5 min download)
./venv/Scripts/python.exe -m pip install -r requirements.txt

# Step 1 — convert groundtruth into YOLO format
./venv/Scripts/python.exe -X utf8 scripts/build_yolo_dataset.py

# Step 2 — train (30-60 min on RTX 5060 Ti)
./venv/Scripts/python.exe -X utf8 scripts/train_roi_seg.py

# Step 3 — restart run.bat → next Auto Trim uses YOLO automatically
```

### Dataset size targets

| Size | Expected unseen accuracy | Recommendation |
|---|---|---|
| 13 (now) | ~75-90% | Pipeline-validation training only; not production |
| 25-30 | 85-92% | Minimum viable production train |
| 50 | 95%+ | **operator's stated target** |
| 80-100 | 99%+ | Production-grade |

Diversity > total: 30 examples from 10 venues (3 per) beats 50 from 3 venues. Currently 13 from ~6 venues, 4-5 of which are singletons → priority is cluster expansion (confirm 2-3 more matches per existing venue) rather than fresh venues.

### Open work (Phase B)

| Step | Status |
|---|---|
| Pip-install heavy deps in operator's venv | pending (operator-triggered) |
| First training run on 13 entries (pipeline validation) | pending |
| Grow dataset 13 → 25 via normal workflow | in progress |
| Re-train at 25 entries, evaluate on held-out unseen | pending |
| Grow dataset to 50 | pending |
| Production train + acceptance test | pending |
| Phase A.5 deprecation? | NO — kept as fallback. YOLO is top-priority but classical CV is still good defense when YOLO confidence is low. |

---

Plan tiếp theo (DEFERRED until ROI milestone): **Auto-trim dead time** dùng **score-event-anchored
detection** + debug GUI modal cho iteration phase.

**Scope:**
- Thêm nút "Auto Trim" trong Trim panel → mở **debug modal full-screen**
- Modal: ROI viewer/editor (left), progress + log + results (right)
- Backend chạy detection bằng score events làm anchor (operator đã
  bấm A/D trước khi chạy auto-trim)
- Append trim entries vào `project.trims` với tag `source: "auto"`
- End-state: khi algorithm đủ tin cậy → auto-trim chạy ngầm trong
  Render button, modal chỉ để debug khi cần

**Workflow mới:**
```
1. Operator bấm A/D scoring real-time          (giữ nguyên)
2. Optional: bấm H mark highlights              (giữ nguyên)
3. → Bấm "Auto Trim" → mở debug modal           (MỚI)
4. Operator review trim đề xuất, Apply / Discard
5. Optional: bấm T/Y mark trim thủ công thêm    (giữ nguyên)
6. Render                                       (giữ nguyên)
```

## Decisions đã chốt (2026-05-17)

| Quyết định | Giá trị | Lý do |
|---|---|---|
| **Algorithm** | **Score-event-anchored** | Operator đã tạo score events miễn phí; mỗi event là rally-end anchor đáng tin. Bài toán giảm xuống "tìm 1 transition trong mỗi gap đã bound" → robust hơn blanket MOG2 nhiều |
| Fallback | Blanket MOG2 trong window | Khi gap quá dài (missed score) hoặc `<10` score events tổng cộng |
| Press-lag correction | 0.7s subtract từ score_event.time | Operator bấm A/D sau khi rally kết thúc ~0.7s |
| Padding | pre 1.0s / post 0.5s (tail từ score) | Tunable trong config.json |
| Min rally duration | 0.8s | Loại noise + serve toss |
| Min sustained motion | 1.0s | Tránh false transition do người đi qua |
| **GUI** | **Debug modal full-screen** | Iteration phase cần visibility. End-state sẽ chạy headless trong Render |
| ROI shape | Polygon 4 điểm | Khớp phối cảnh bàn chéo |
| ROI default | `(0.2,0.3) (0.8,0.3) (0.8,0.7) (0.2,0.7)` | |
| ROI persist | `project.info.roi_quadrilateral` | Per-project |
| **GPU** | **ffmpeg NVDEC only** | Decode là bottleneck thực sự. Motion compute trên CPU numpy đủ nhanh sau downscale 480×270. Skip cupy + cv2.cuda → tránh Blackwell sm_120 risk + giảm 1 dep |
| Spectral check | Optional, deferred | Nếu base algorithm fail mới thêm FFT periodicity check |
| Re-run | Xoá auto trims cũ, giữ manual | Tag `source: "auto"` vs `"manual"` |
| Dep mới | `opencv-python` (~40MB) | Approved. KHÔNG thêm cupy, torch |

## Phase 0 — Algorithm spike ✅ HOÀN THÀNH 2026-05-17

**Kết quả:** 31-variant sweep + tight ROI + per-match adaptive threshold +
rally-duration prior + backward-scan = approach validated.

Default variant **`J_fg_p65_rmin4`**: recall 95.3-98.7%, extras 344-362s
trên 3 entries. NVDEC pipeline 240-250 fps decode (8× realtime).

3 presets cho modal:
- Aggressive (`J_fg_p65_rmin4`): max recall, ~60-71 trims/trận
- Balanced (`J_fg_p70_rmin5`): middle, ~57-67 trims
- Conservative (`I_prior_only_r11`): few trims, ~22-27

Full report: `scripts/spike_out/PHASE0_REPORT.md`.
Files: variants JSON, motion CSV, plot PNG, overlay MP4 cho từng entry.

## Phase 0 (gốc) — Algorithm spike — KEPT FOR REFERENCE 🔴

Script standalone trong `scripts/`, **không động codebase chính**.
Mục đích: validate cả 2 algorithms trước khi đầu tư build GUI.

**File:** `scripts/spike_rally_detector.py`

Tasks:
- [ ] Argparse: `--entry <slug>`, `--algorithm {score_anchored,blanket,both}`,
      `--roi <x1,y1,x2,y2,x3,y3,x4,y4>` (normalized 0-1), `--out <dir>`
- [ ] Đọc entry từ `dataset/<slug>/`: source.mp4, groundtruth.json,
      refframe.png, notes.md
- [ ] ffmpeg NVDEC pipeline:
      `ffmpeg -hwaccel cuda -hwaccel_output_format cuda -i src -vf
       "scale_cuda=480:270,hwdownload,format=nv12,format=rgb24"
       -f rawvideo -pix_fmt rgb24 -`
- [ ] Decode pipe → numpy frame iterator
- [ ] ROI mask via `cv2.fillPoly`
- [ ] Motion signal: frame-diff `mean(abs(curr-prev) * mask)`
- [ ] Smooth: moving avg window 0.5s
- [ ] Algorithm A (score_anchored):
  - Load score events từ groundtruth.json
  - For each gap `(t_i, t_{i+1})`: scan forward từ `t_i + 0.5s`, find
    first sustained motion ≥1s above threshold → rally_start
  - Trim = `(t_i - 0.7s_lag + 0.5s_tail, rally_start - 1.0s_pre_pad)`
  - Edge: pre-first-score, post-last-score, missed-score detection
- [ ] Algorithm B (blanket): MOG2 toàn video → group rally → invert
- [ ] Output cho mỗi algorithm:
  - `<out>/<slug>_<algo>_trims.json` — detected trim list
  - `<out>/<slug>_<algo>_motion.csv` — motion signal per frame (cho plot)
  - `<out>/<slug>_<algo>_overlay.mp4` — debug video: ROI viz + motion bar
    + detected segments highlighted (1 phút sample)
  - `<out>/<slug>_<algo>_metrics.txt` — recall vs manual trim subset,
    count "extra" trims detected vs manual (likely true positives), avg
    rally duration of detected rallies vs 10.6-11.3s prior
- [ ] Console log: stage + frame progress + per-rally summary
- [ ] Validate trên 3 entries (cả 3, không phải 1)

**Pass criteria:**
- Score-anchored: recall ≥95% manual trims subset, không miss rally
  >2s, rally duration distribution khớp prior (~10s avg)
- Blanket MOG2: recall ≥85% (làm baseline để so sánh)
- Score-anchored phải **rõ ràng tốt hơn** blanket trên: false positive
  từ người đi qua, từ pre-serve bouncing

**Nếu fail:** thêm spectral periodicity check (FFT 2-4Hz peak). Nếu
vẫn fail, escalate Phase 5 (YOLOv8-pose).

## Phase 1 — Backend detector + SSE stream (~1.5 ngày) 🔴

**Files mới/sửa:**
- `backend/rally_detector.py` (mới) — port algorithm winner từ spike,
  có `cancel_check` predicate
- `backend/models.py` — `RoiQuad`, `Rally`, `AutoTrimJob`,
  `AutoTrimEvent` (SSE event types)
- `backend/server.py` — endpoints mới, SSE handler
- `backend/config.py` — auto_trim defaults

**Endpoints:**
- `POST /api/auto_trim/start` body `{ video_token, roi, params? }` → `{ job_id }`
- `GET /api/auto_trim/events/{job_id}` — SSE stream (text/event-stream):
  - `event: stage` data: `{ name, started_at }`
  - `event: progress` data: `{ stage, fraction, frame_n, frame_total }`
  - `event: log` data: `{ level, msg, ts }`
  - `event: motion_sample` data: `{ t, value }` (subsampled, ~10/s)
  - `event: rally` data: `{ start, end, duration, peak_motion }`
  - `event: trim` data: `{ start, end, source: "auto" }`
  - `event: done` data: `{ summary }`
  - `event: error` data: `{ msg, traceback }`
- `POST /api/auto_trim/cancel/{job_id}`
- `GET /api/refframe/{video_token}` — extract 1 frame qua ffmpeg
  cho ROI editor

**Caching:**
- `temp/auto_trim_cache/<sha1(video+mtime+roi+score_events+params)>.json`
- Cache hit → emit log "cache hit" + tất cả events từ cache + done

**Headless mode (cho future Render integration):**
- Tách `run_auto_trim(plan) -> AutoTrimResult` pure function, không
  phụ thuộc SSE → Render button gọi trực tiếp sau này

## Phase 2 — Modal GUI scaffold + log viewer (~1 ngày) 🟡

**Files:**
- `frontend/index.html` — thêm `<div id="auto-trim-modal">` ẩn
- `frontend/auto_trim_modal.js` (mới) — open/close, SSE client,
  state machine (idle / running / done / error)

UI sections (layout đã sketch trong AUTO_TRIM_DISCUSSION.md):
- Header: title + close (X)
- Left column: ROI panel placeholder (Phase 3 fill in)
- Right column:
  - Run controls: Start / Cancel
  - Stage indicator: "Decoding frame 4521/79320"
  - Progress bar
  - Params display (read-only): ROI summary, padding, min_rally, etc.
  - Live log: scroll-to-bottom textarea, monospace, color-coded levels
  - Results panel (hidden until done):
    - Counts: "78 rallies detected, 41 trims, 12.3 min trimmed"
    - Apply / Discard buttons

SSE client:
- `EventSource('/api/auto_trim/events/{job_id}')`
- Append log lines as they stream
- Update progress bar
- Subsample motion_sample events → mini plot (canvas, optional Phase 2.5)

## Phase 3 — ROI editor trong modal (~1 ngày) 🟡

**File:** `frontend/roi_editor.js` (mới, dùng trong modal)

- Canvas overlay refframe.png (từ `/api/refframe/{video_token}`)
- Default 4-corner quad lấy từ `project.info.roi_quadrilateral` hoặc
  default `(0.2,0.3) (0.8,0.3) (0.8,0.7) (0.2,0.7)`
- View mode (default): vẽ polygon + corner dots, không drag được
- Edit mode toggle: corner dots draggable, validation convex polygon
- Save → `project.info.roi_quadrilateral` qua existing project save
- Modal mở: load saved ROI; chưa có ROI → dùng default, prompt edit

## Phase 4 — Apply + persistence + tests (~1 ngày) 🟡

- [ ] `ProjectInfo.roi_quadrilateral: Optional[RoiQuad]`
- [ ] `TrimSegment.source: Literal["manual", "auto"] = "manual"`
- [ ] Apply button: append auto trims, re-run xoá auto cũ, manual
      giữ nguyên
- [ ] Modal close → sync trim panel UI
- [ ] Badge `[AUTO]` trong trim list
- [ ] Edge cases:
  - Score events < 10 → tự fallback blanket, modal log warning
  - Score event rơi vào auto trim → toast warning
  - Cancel mid-run → cleanup partial state
  - Source video < 60s → button disable
- [ ] Pytest pure-logic:
  - `gaps_to_trims_score_anchored` với edge cases
  - `merge_overlapping_trims`
  - `rerun_replaces_only_auto`
  - `lag_correction`, `boundary_gap_handling`

## Phase 5 — End-state integration (~½ ngày) 🟢 (sau khi 1-4 stable)

- [ ] Setting trong config.json: `auto_trim_on_render: bool` (default false ban đầu, true sau khi tin cậy)
- [ ] Render button: nếu setting bật + có ROI + ≥10 score events → gọi
      `run_auto_trim()` headless trước khi build RenderPlan, append
      results vào `project.trims`
- [ ] Show summary trong render progress: "Auto-trim: 41 segments, 12.3 min saved"
- [ ] Modal vẫn available qua "Auto Trim" button cho debug

## Phase 6 (optional escalation) — YOLOv8-pose 🟢 (defer)

Chỉ chạy nếu Phase 0 cả 2 algorithm fail trên dataset.
- Detect 2 player skeleton trong ROI
- Wrist velocity oscillation 2-4Hz → rally signature
- ~2-3 ngày work, thêm dep `ultralytics`

## Backlog cũ chưa làm

- 🟡 Country/club flags cạnh tên player trong scoreboard
- 🟡 Tournament logo trên intro card
- 🟡 Theme presets scoreboard per-tournament
- 🔴 Audio leveling/ducking trong slow-mo (atempo=0.5 hơi robot)
- 🔴 Per-highlight export riêng từng MP4
- 🟢 Pin Python version trong pyproject.toml
- 🟡 `--dry-run` mode cho renderer
- 🟡 GitHub Actions lint
- 🟢 Bundle font trong assets/ cho intro drawtext fallback
- 🟢 Unit test apostrophe trong .ass escape
- 🟡 NVDEC session limit investigation
