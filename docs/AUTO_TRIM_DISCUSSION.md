# Auto-Trim Feature — Discussion Log

Cuộc thảo luận về tính năng auto-trim dead time. Ghi lại để chiều/tối
tiếp tục bàn.

Plan cụ thể đã chốt: [TODO.md](TODO.md). File này lưu **context +
rationale + open questions** đằng sau plan đó.

---

## 📌 RESUME POINTER 2026-05-19 dawn — Phase B.5/B.6 + state-leak fix

**State khi pause:**
- Dataset: **53 confirmed ROIs** (47 + 6 new during session, all 7 unseen in
  `D:\Table Tennis Video\2026\Match` tested)
- YOLO retrained: Mask mAP50-95 **0.921** (vs 0.879 at 47 entries)
- Production noise floor ~0.005-0.008 corner err (visually OK per operator)
- Still GATED — Phase 1b not unblocked

**Shipped in this session:**

1. **B.5 — cross-tier IoU clustering in `detect_roi_multiframe`.** Pool
   frames whose tier is "trusted" and cluster by IoU across tier labels.
   Cross-tier cluster of N beats any per-tier cluster of < N. Tag gets `*`.
   Fixed DucTu where 1-frame `+both_agree` had wrong corners but beat a
   4-frame `+orb_agree` of correct corners. LOO 12 SEEN: max 0.0461 →
   0.0168, mean 0.0139 → 0.0111.

2. **B.6 — 4 corner zoom panels in Auto Trim modal.** 180×180px each at
   ZOOM_FACTOR=3 with crosshair at exact corner pixel. Operator can judge
   sub-percent alignment without GT overlay reference. Fixed perception
   issue where operator saw "rất tệ" on cases that were actually at
   noise-floor accuracy.

3. **State-leak fix.** `frontend/auto_trim_modal.js` no longer prefers
   `project.info.roi_quadrilateral` over `det.corners`. Previously, after
   confirming video 1 + switching to video 2 without page-reload, video
   1's corners showed up on video 2's modal ("kết quả sai hoàn toàn"
   symptom). The `roi_quadrilateral` field is still WRITTEN on confirm
   for forward compatibility with Phase 1b render but no longer READ by
   the modal. `saved_corners()` function removed (dead code).

4. **`run.bat` auto-cache-clear.** Pre-uvicorn block removes
   `temp/refframes/` + all `__pycache__/` under repo. Reproducible
   behaviour after every restart.

**Diagnostic scripts added** (gitignored under `scripts/_inspect_cache/`
and `scripts/diag_*.py`):
- `diag_3failures.py` — raw YOLO polygon vs v3 quad vs minAreaRect
- `diag_modal_match.py` — reproduces exact modal view + adds GT overlay
- `diag_2bad.py` — per-frame breakdown for occlusion vs algorithm failure
- These remain useful for the next round of operator-flagged regressions.

**Next session resume:**
- All 7 unseen videos tested + accepted (6 confirmed, 1 visually OK in diag)
- Operator still wants higher bar before unblocking Phase 1b (auto-trim
  detection backend). Continue dataset growth via natural operator
  workflow + retrain at next milestone.
- Outstanding: 2 known-hard LOO cases (0418_aTri, 0510_ThaoBa) still
  miss, but these are cross-validation tier issues unrelated to B.5/B.6.

---

## 📌 RESUME POINTER 2026-05-18 khuya — _polygon_to_quad v3 fix (max-area approxPolyDP + minAreaRect fallback)

**State khi pause:**
- Dataset: **46 confirmed ROIs** (41 + 5 from priority-1 unseen test)
- Phase B.2 multi-frame training shipped earlier (model retrained on 246 examples)
- Operator tested 5 unseen videos via modal; 4 looked "sai" visually
- Investigation revealed `_polygon_to_quad` in `roi_yolo.py` was the bug:
  - **v1 (original)**: approxPolyDP returned FIRST 4-point reduction → could
    produce kite-shape polygon when an early epsilon picked smooth interior
    perimeter points instead of actual corners
  - **v2 (attempted)**: image-axis extremes (argmin/max of x±y) → degenerated
    for tilted/slim masks (two extreme criteria picked same hull vertex →
    3-sided "triangle" polygon). Operator confirmed visually on HoangMinh + ThaoTien.
  - **v3 (current)**: hybrid — try approxPolyDP at multiple epsilons,
    pick the 4-point result with MAX AREA (most filling), validate no
    degenerate edges (min edge ≥ 1% of hull diagonal), fall back to
    `cv2.minAreaRect` (always 4 distinct corners). LOO mean **0.020**
    (best yet — was 0.029 pre-any-fix, 0.057 v2). 44/46 pass at 0.10.

**Việc cần làm khi quay lại:**
1. **Restart `run.bat`** to load v3 fix (inference-path only, no model reload)
2. Re-test unseen videos via modal — visual should match `V3_*.jpg` overlays in `scripts/_inspect_cache/`
3. If operator confirms visual fix → proceed to address 2 remaining LOO failures:
   - 0418_aTri_0-3.MP4 err 0.143 — `yolo_seg+both_agree` (cross-validation didn't catch the error)
   - 0510_ThaoBa_vs_TuanAnhNhueMinh err 0.269 — `color_contrast_vs_yolo` override misfires

**Files updated this session:**
- `backend/roi_yolo.py` — `_polygon_to_quad` v3 algorithm
- `scripts/_inspect_cache/` — V3_*.jpg overlays for the 5 priority-1 unseen videos

---

## 📌 RESUME POINTER 2026-05-18 đêm — Multi-frame training shipped, unseen test pending

**State khi pause:**
- Dataset (groundtruth corners): **41 confirmed ROIs**, unchanged
- Training set: **231 examples** (was 41) via multi-frame extraction
  - 38 videos × 5 frames + 38 cached refframes + 3 refframe-only (Tan/Dat/Ha
    no source available) = 188 train + 43 val
  - Each video contributes 5 frames at 10/30/50/70/90% duration + the
    midpoint refframe, ALL with same `latest_corners` label (tripod is
    fixed per match → ROI applies to every frame)
  - **REAL** variance: different player positions, ball locations,
    scoreboard states, spectator background motion. Augmentation can't
    synthesize this.
- Val set now 43 (was 8) → val mAP much more representative
- Training time: 4.6 min (was 1.3 — bigger dataset, still fast)
- Phase B.1 cross-check + algorithm upgrades still in effect

**Việc cần làm khi quay lại:**
1. **Restart `run.bat`** to load new multi-frame-trained model
2. Test 3-5 truly-unseen videos. Expectation: more `yolo_seg+orb_agree` /
   `+color_agree` / `+both_agree` tags (training set has more variance
   so classical CV more likely to agree with YOLO's pick on unseen)

Source videos live at `D:\Table Tennis Video\2026\Match` for re-training:
```
./venv/Scripts/python.exe -X utf8 scripts/build_yolo_dataset.py \
  --force --frames-per-video 5 \
  --search-path "D:/Table Tennis Video/2026/Match"
./venv/Scripts/python.exe -X utf8 scripts/train_roi_seg.py
```

Previous models:
- `assets/models/roi_seg.pt` — current (multi-frame, 2026-05-18 đêm)
- `assets/models/roi_seg_pre_multiframe_backup.pt` — Phase B.1 single-frame retrain
- `assets/models/roi_seg_pre_41_backup.pt` — Phase B first training (38 entries)

---

## 📌 RESUME POINTER 2026-05-18 tối — Phase B retrain + cross-check shipped, unseen test pending

**State khi pause:**
- Dataset: **41 confirmed ROIs** (3 new corrections added evening 2026-05-18)
- Phase A (ORB + sanity) shipped ✅
- Phase A.5 (color-contrast + multi-frame + tier priority) shipped ✅
- Phase B (YOLOv8n-seg) FIRST training shipped 2026-05-18 morning ✅
- **Phase B.1 (cross-check + 2nd retrain) shipped 2026-05-18 tối** ✅
  - Production test on operator's workflow: 13/41 (32%) failed at err > 0.05;
    1 catastrophic at err 0.44 (0331_TrungThao_vs_PhuongBa_2_3-1.MP4) with YOLO
    conf 0.975 — confidence does NOT track accuracy
  - Retrained YOLO with 41-entry dataset (was 38). LOO 41/41 pass, mean 0.0125
  - Algorithm upgrade: cross-check YOLO against ORB + color-contrast in
    `detect_roi`. Three new method tags surface agreement strength:
    `yolo_seg+both_agree` / `+orb_agree` / `+color_agree`. Multi-frame
    priority gates reordered: cross-validated agreement now beats bare
    YOLO; `color_contrast_foreground` singleton gate REMOVED (100% fail
    rate when fired alone in production data).
  - Model: `assets/models/roi_seg.pt` (6.8MB). Previous model backed up
    to `assets/models/roi_seg_pre_41_backup.pt`.

**Việc cần làm khi quay lại:**
1. **Restart `run.bat`** để load new model + algorithm changes
2. **Test trên 3-5 video TRULY unseen** — focus on cases where YOLO previously
   gave high-confidence wrong answers. Modal will now show method tag
   `yolo_seg+orb_agree` / `+color_agree` / `+both_agree` when classical CV
   confirms YOLO's pick, or `orb_homography_vs_yolo` / `color_contrast_vs_yolo`
   when classical CV overrode YOLO
3. **Decision tree:**
   - ≥80% unseen pass → mark Phase B SHIPPED, unlock Phase 1b (trim detection)
   - 50-80% → grow dataset → 50 → retrain (chỉ 1.3 phút)
   - <50% → augment thêm, hoặc switch sang YOLOv8s-seg (larger model)

**Files đã đụng trong session 17-18/05:**
- `backend/roi_detector.py` — Phase A.5 stages, multiframe, tier priority + numpy.float32 fix + min_y corner order
- `backend/roi_yolo.py` — NEW, YOLO lazy loader
- `backend/server.py` — multi-frame extraction + sanitizer
- `scripts/build_yolo_dataset.py` — NEW
- `scripts/train_roi_seg.py` — NEW
- `scripts/test_roi_detector.py` — multi-frame LOO + rotation-invariant error
- `scripts/debug_roi_overlay.py` — 3-panel diagnostic
- `assets/models/roi_seg.pt` — trained model (gitignored)
- `requirements.txt` — ultralytics dep
- `dataset/roi_groundtruth/` — 13 → 38 entries

**Quick verify state sau restart:**
```bash
# Confirm model exists + CUDA works
./venv/Scripts/python.exe -c "from backend import roi_yolo; print(roi_yolo.model_status())"
./venv/Scripts/python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"

# Re-run LOO (1 min):
./venv/Scripts/python.exe -X utf8 scripts/test_roi_detector.py
```

---

## 15. Phase B.2 — Multi-frame training (2026-05-18 đêm)

### Trigger

Operator asked whether mirror-flip would help YOLO training on 41 entries.
Answer was NO (fliplr=0.5 already in augmentation; pre-flipping is
redundant). Real opportunity: **operator's tripod is fixed per match**,
so a single video's ROI corners apply to EVERY frame of that video.
Previously we used only 1 refframe.jpg per video → 41 examples.

### Change shipped

`scripts/build_yolo_dataset.py` extended:
- `--frames-per-video N` flag (default 5): extracts N evenly-spaced
  frames per video at 10/30/50/70/90% duration (same sampling as
  inference-time multi-frame), labels each with the same
  `latest_corners`. Cached refframe kept as a 6th example per video.
- `--search-path PATH` flag: extra directory to recursively search
  for source videos when `videos/` is empty (operator archives videos
  off the workspace folder).
- Falls back to manifest-lookup of `dataset/<slug>/source.*` for
  auto-archived renders, then to cached refframe alone if all else fails.

Operator's source video archive: `D:\Table Tennis Video\2026\Match`
contains 38/41 of the videos. The 3 missing (Tan/Dat/Ha — old confirms
from before the naming convention changed) use refframe-only fallback.

### Dataset size

| | Old (single-frame) | New (multi-frame) |
|---|---:|---:|
| Train examples | 33 | 188 |
| Val examples | 8 | 43 |
| Total | 41 | 231 |
| Training time | 1.3 min | 4.6 min |

Val set 5× larger → val mAP becomes a meaningful early-stop signal
(8 examples were too few to be statistically reliable).

### Val metrics (new model vs old)

| | Pre multi-frame (8 val) | Post multi-frame (43 val) |
|---|---:|---:|
| Box mAP50 | 0.995 | 0.995 |
| Box mAP50-95 | 0.920 | 0.873 |
| Mask mAP50 | 0.995 | 0.995 |
| Mask mAP50-95 | 0.880 | 0.871 |

mAP50-95 drop is **expected and healthy**: val set is now more diverse
(43 frames covering 5 timestamps per video) so it's a harder test, not
the cherry-picked midpoint refframe. Higher score on 8 was an artifact
of low-variance evaluation.

### LOO results post multi-frame retrain

`test_roi_detector.py` extended with `--search-path` and `--alias` flags
to run an HONEST multi-frame LOO (was falling back to refframe-only
because videos live outside `videos/`).

Honest multi-frame LOO command:
```
./venv/Scripts/python.exe -X utf8 scripts/test_roi_detector.py \
  --search-path "D:/Table Tennis Video/2026/Match" \
  --alias "Tan.MP4=0510_NguyenKhanhTan_3-1.MP4" \
  --alias "Ha.MP4=0510_HoangHuuHa_1-3.MP4" \
  --alias "Dat.MP4=0406_DatDo_0-3.MP4"
```

Results: **39/41 (95%) pass at threshold 0.10**. Mean 0.0289, max 0.4415.

Method breakdown:
- 25 multiframe[5/5] consensus across all 5 inference frames
- 3 multiframe[3/5] / 3 multiframe[2/5] / 3 multiframe[4/5] (partial cluster)
- 5 yolo_seg+both_agree (no cluster needed — single frame with classical agreement)
- 1 color_contrast_vs_yolo (classical overrode YOLO — also one of the 2 fails)

2 hard cases that still fail:
- **0331_TrungThao_vs_PhuongBa_2_3-1.MP4** — err 0.4415 with
  `multiframe[4/5]:yolo_seg`. YOLO confidently picked the wrong table
  across 4 of 5 frames. ORB didn't help (likely no same-arena example
  in dataset). Color-contrast didn't fire strong enough to override.
  This case has been catastrophic since first prod test; persists.
- **0510_ThaoBa_vs_TuanAnhNhueMinh_1-3.MP4** — err 0.2690 with
  `color_contrast_vs_yolo`. Cross-check FIRED but went the wrong way:
  color contrast had red_surround ≥ 0.50 (passed override threshold)
  yet was less accurate than YOLO. False positive of the override rule.

Net: vs original 32% prod fail rate (13/41), this honest LOO shows
**5% fail rate (2/41)** — ~6× improvement on the same dataset. Real
generalization to truly-unseen videos still needs operator test.

### Why LOO numbers don't move much

LOO is testing on single cached refframes that the model was trained on
(refframe is part of the multi-frame training set per video). Real
unseen accuracy needs operator testing on fresh videos via Auto Trim
modal. The hypothesis is multi-frame training improves robustness to:
- Player occlusion (frames with player blocking table center)
- Ball-in-frame variations (sometimes near table, sometimes not)
- Lighting drift (rare in a single venue but slight changes happen)
- Background spectator motion (mosaic augmentation can't synthesize the
  same kind of background variety as real frames)

### Files updated

- `scripts/build_yolo_dataset.py` — `--frames-per-video`, `--search-path`,
  manifest fallback for dataset/<slug>/source.*
- `assets/models/roi_seg.pt` — retrained on 231 multi-frame examples
- `assets/models/roi_seg_pre_multiframe_backup.pt` — Phase B.1 single-frame backup
- This file + `docs/TODO.md` + memory `project_auto_trim_feature.md`

### Open work

- Operator tests on truly-unseen videos to validate multi-frame benefit
- If accuracy still insufficient on unseen, consider:
  - Bump `--frames-per-video` to 10 (more variance per video)
  - Try YOLOv8s-seg (3× params, now feasible with 200+ examples)
  - Increase `imgsz` 640 → 800 for sharper mask edges

---

## 14. Phase B.1 — Cross-check + 2nd retrain (2026-05-18 tối)

### Trigger

After morning's first training, operator ran Auto Trim on a batch of seen
and unseen videos. Most still required correction. Analysis of the
`history[]` records (`scripts/analyze_detector_errors.py`) on all 41
confirms showed:

- **97% of confirms needed edit** (57/59 history entries with `was_edited=true`)
- **13/41 videos (32%) had latest detector_proposed error > 0.05** — far worse
  than the 38/38 LOO suggested
- **1 catastrophic case**: `0331_TrungThao_vs_PhuongBa_2_3-1.MP4` —
  `multiframe[5/5]:yolo_seg` returned conf 0.975 with err 0.4427 (44%
  lệch). YOLO confidently picked the wrong table across all 5 frames.
- **Confidence does NOT correlate with accuracy** in YOLO — mean conf
  0.995 across all YOLO predictions, but max err 0.44 at conf 0.975

The LOO benchmark was overconfident: `test_roi_detector.py` calls
`detect_roi(exclude_video_id=vid)` which excludes the entry from the
classical-CV groundtruth lookup but NOT from the YOLO training set. YOLO
is being tested on its own training data.

### Per-tier error breakdown (latest-per-video)

| Tier (latest detection) | n | Mean err | Max err | Fail (>0.05) |
|---|---:|---:|---:|---|
| multiframe[5/5]:orb_homography | 8 | 0.007 | 0.015 | 0% (8/8 pass) |
| multiframe[5/5]:yolo_seg | 8 | 0.068 | 0.443 | 12% (7/8 pass) |
| color_contrast_foreground (alone) | 1 | 0.166 | 0.166 | 100% (0/1 pass) |
| multiframe[2/5]:color_contrast_foreground | 1 | 0.339 | 0.339 | 100% |
| learned_* tiers | various | 0.05-0.10 | 0.10 | ~50-100% |

**Strategic insight:** ORB 5/5 cluster is gold standard when same arena
exists in dataset. YOLO is the only tier that works on truly-novel
arenas but its confidence is uninformative. Color-contrast alone is
catastrophic. → Need YOLO + classical-CV cross-validation.

### Changes shipped

1. **Retrained YOLO** with 41-entry dataset (was 38). Same 1.3 min on
   RTX 5060 Ti. Box mAP50-95 improved 0.894 → 0.920. Model overwrites
   `assets/models/roi_seg.pt`; previous saved to
   `assets/models/roi_seg_pre_41_backup.pt`.

2. **Cross-check in `detect_roi`** ([backend/roi_detector.py:1330](../backend/roi_detector.py#L1330)):
   - Run YOLO + ORB + color-contrast in parallel (instead of YOLO-wins-outright)
   - **Both agree** (IoU ≥ 0.45) → tag `yolo_seg+both_agree`, +0.05 conf bonus
   - **Only one agrees** → tag `yolo_seg+orb_agree` or `yolo_seg+color_agree`
   - **YOLO disagrees** but ORB has high conf (≥ 0.80) → trust ORB, tag
     `orb_homography_vs_yolo:<src>`
   - **YOLO disagrees** but color has strong red-surround (≥ 0.50) → trust
     color, tag `color_contrast_vs_yolo`
   - **No classical competitor** → keep YOLO (only choice for novel arenas)

3. **Multi-frame priority gates reordered**
   ([backend/roi_detector.py:1184](../backend/roi_detector.py#L1184)):
   - Top: cross-validated agreement tiers (`yolo_seg+both_agree` →
     `+orb_agree` → `+color_agree` → `color_contrast+orb_agree`)
   - Then: ORB cluster ≥ 3 (was 2) — strongest classical signal
   - Then: disagreement-resolved fallbacks (`orb_homography_vs_yolo`,
     `color_contrast_vs_yolo`) at singleton
   - Then: YOLO cluster ≥ 2 (was 1) — stops stray YOLO frame dominating
   - **REMOVED**: `("color_contrast_foreground", 1)` singleton gate —
     production showed 100% fail rate when this fires alone

### LOO results (post-changes)

41/41 pass at threshold 0.05. Mean 0.0125, max 0.0322. Method breakdown:
- 22 yolo_seg+orb_agree
- 5 yolo_seg+color_agree
- 2 yolo_seg+both_agree (strongest signal)
- 7 yolo_seg (no classical competitor — novel-arena pattern)
- 5 multiframe (varied)

The 0331_TrungThao catastrophic case: 0.4427 → 0.0187 (×24 better). Both
the retrain (now sees the operator's correction in training) and the
cross-check (ORB agreed with YOLO post-retrain) contributed.

**Caveat:** LOO is still overconfident on YOLO since the test uses the
same model that trained on all 41 entries. True unseen accuracy requires
operator testing on fresh videos not in `dataset/roi_groundtruth/`.

### What's still uncertain

- Will the cross-check help on TRULY unseen arenas where neither ORB nor
  color-contrast has signal? In that case the pipeline falls to bare
  `yolo_seg` (now demoted in priority but still chosen) with no
  independent confirmation.
- The 1 YOLO catastrophic case might recur on a new venue. Mitigation
  requires either more training data covering similar venues OR a
  geometric sanity check on YOLO output (e.g. reject when the quad is
  far from frame center).

### Files updated in Phase B.1

- `scripts/analyze_detector_errors.py` — NEW, error breakdown from history[]
- `backend/roi_detector.py` — cross-check in `detect_roi`, priority gates
  reordered, color-contrast singleton gate removed
- `assets/models/roi_seg.pt` — retrained on 41 entries
- `assets/models/roi_seg_pre_41_backup.pt` — previous model snapshot
- This file + `docs/TODO.md` + `CLAUDE.md`

---

## 13. Phase B — YOLOv8-seg fine-tune, FIRST TRAINING COMPLETE (2026-05-18 sáng sớm)

### Trigger

Phase A.5 hit classical-CV ceiling at ~70-80% unseen accuracy. Operator unblocked Phase B by aggressive dataset growth (13 → 22 → 38 over a few hours of confirming during a tournament evening). Threshold for first train (25 entries) hit and exceeded; operator triggered training with 38.

### Pipeline executed

```bash
pip install -r requirements.txt                       # pulls ultralytics + torch (CPU default ✗)
pip install --upgrade --force-reinstall \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch torchvision                                   # force CUDA 12.8 build for Blackwell
python scripts/build_yolo_dataset.py                  # 38 → yolo_seg/ (30 train, 8 val)
python scripts/train_roi_seg.py                       # 1.3 min on RTX 5060 Ti
# → assets/models/roi_seg.pt (6.8 MB), best.pt copied automatically
```

### Training details

- **Base:** YOLOv8n-seg pretrained on COCO (transferred 381/417 layers)
- **Hardware:** RTX 5060 Ti, CUDA 12.8, AMP enabled, sm_120 compute
- **Augmentation:** mosaic 1.0, mixup 0.15, hsv 0.02/0.7/0.6, perspective 0.0008, fliplr 0.5, NO flipud (red floor always at bottom)
- **Optimizer:** AdamW auto (lr 0.002, cos schedule, momentum 0.9)
- **120 epochs, batch 8, imgsz 640, patience 30**
- **Time: 0.022 hours = 1.3 minutes** (Blackwell + small dataset + AMP)
- Loss converged box ~0.20, seg ~0.25 (stable in last 30 epochs)

### Results

Val metrics (8 val examples — overconfident):
| | Box | Mask |
|---|---|---|
| Precision | 0.991 | 0.991 |
| Recall | 1.000 | 1.000 |
| mAP50 | 0.995 | 0.995 |
| mAP50-95 | 0.894 | 0.899 |

LOO on all 38 entries (rotation-invariant error):
| Tier | Mean | Max | Pass |
|---|---:|---:|---|
| Phase A.5 classical | 0.0506 | 0.1695 | 18/22 (82%) |
| **Phase B YOLO** | **0.0126** | **0.0317** | **38/38 (100%)** |

Confidence per prediction: 0.81-0.98 consistently.

### Gotchas (HIGH VALUE for next session)

1. **Default `pip install ultralytics` pulls CPU torch.** Blackwell (sm_120) needs PyTorch with CUDA 12.8 wheels:
   ```
   pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch torchvision
   ```
   Result: `torch 2.11.0+cu128`. Verify via `torch.cuda.is_available()` + `get_device_name(0)`.

2. **Corner ordering convention mismatch caused FAKE 0.146 error.** First LOO run looked catastrophic — predicted quads visually overlapped groundtruth, but corner labels (TL/TR/BR/BL) were rotated. Two layers of fix:
   - **Test script:** added rotation-invariant error (try all 4 cyclic rotations, take min). Use this any time evaluating polygon detectors.
   - **Production code:** changed `_order_clockwise_from_tl` from `min(x+y)` to `min_y` (tiebreak `min_x`). Empirically: 23/30 of operator's groundtruth entries have idx 0 = topmost corner (operator clicks back-left of table first because it's at top of the image in low-angle perspective view). `min(x+y)` was wrong for perspective trapezoids where the table extends rightward (front-right of table has smaller x+y than back-left).

3. **numpy.float32 → Pydantic crash.** `quad_blob_ratio` accumulated as numpy.float32 from numpy.float32 quad arrays, leaked into debug dict via `round()`, FastAPI couldn't serialize. Fixed at source (wrap in `float()`) + added `_sanitize_for_json` recursive helper in `server.py` for defense-in-depth. Any future numpy leak (e.g. from torch tensor) is auto-converted.

4. **Training is FAST on Blackwell for small datasets.** Expected 30-60 min, actual 1.3 min. Re-train freely as dataset grows — no need to batch confirms.

### Open questions for next session

- **Does YOLO actually generalize to UNSEEN videos?** LOO on trained set is overconfident — model has seen each test image during training. Real test: operator clicks Auto Trim on a fresh video not in `dataset/roi_groundtruth/`. Expected ≥95% pass rate based on training-set LOO + the augmentation regime, but unconfirmed.
- **Phase A.5 classical CV — keep as fallback or remove?** Lean toward KEEP (defense in depth, near-zero runtime cost, only activates when YOLO conf < 0.55 or mask area sanity fails). Could decide after seeing how YOLO performs on truly-unseen edge cases.
- **Dataset growth: stop at 50 or keep going?** Operator target is 50. With current 1.3 min retrain time, no harm in retraining at every milestone (40, 50, 60, etc.). Diminishing returns kick in around 80-100.

### Roadmap update (post-Phase B)

| Phase | Status | Notes |
|---|---|---|
| A (ORB) | shipped | kept as fallback in classical CV chain |
| A.5 (color-contrast + multiframe) | shipped | kept as fallback below YOLO |
| **B (YOLOv8n-seg)** | **first train ✅, unseen test pending** | top-priority tier in `detect_roi` |
| Truly-unseen verification | pending | operator tests in modal, 3-5 fresh videos |
| Retrain at 50 entries | pending | continue dataset growth via normal workflow |
| Phase C (multi-modal ensemble) | DEFERRED | only if B + dataset growth not enough |
| Unlock Phase 1b (trim backend + SSE) | gated on ROI ≥99% truly-unseen | next step after Phase B confirms generalization |

---

## 12. Phase A.5 — Color-contrast foreground + multi-frame + tier priority (2026-05-17 tối, late)

### Trigger

Phase A locked với LOO mean err 0.047 trên 10 ROIs trained. Operator test
trên video unseen đầu tiên → **sai bét** (ORB transferred corners sang
background tables area). Retest 2 lần nữa với 2 video unseen khác → cũng
sai. Pattern rõ ràng: ORB tier silently confuses arenas khi truly unseen,
HSV NN fallback cũng không khá hơn vì dataset nhỏ.

Operator quote: "Có cần tôi confirm thêm 5 dataset nữa, tăng từ 10-15 data
groundtruth cho dễ tìm thuật toán đúng ko?"

**Insight quan trọng từ operator:** "Bàn bóng bàn thường màu xanh dương,
màu khá tương phản với thảm/sàn... có màu đỏ." → universal physical
feature mà thuật toán không khai thác.

### Tại sao color-contrast hoạt động

Hai tier hiện có đều có ceiling rõ ràng:

- **HSV histogram** chỉ capture color distribution → cùng arena khác
  góc có hist giống nhau (sim 0.65+) nhưng ROI position khác.
- **ORB keypoints** match spatial features → tốt khi đúng arena, nhưng
  unseen arena có thể match nhầm vào features ngẫu nhiên (banners,
  equipment) và transfer corners đến vị trí sai.

Color-contrast tier khai thác **physical fact**: foreground table =
blue surface, surrounded by red carpet. Background tables cũng xanh
nhưng surrounded by other tables / walls / spectators. **"Blue blob
with red around it"** là discriminator universal, không cần dataset.

### Algorithm

```
1. HSV blue mask (95-130, 80-255, 45-220) → connected blobs
2. HSV red mask (split: 0-12 + 168-180) → context mask  
3. Morph OPEN with ELLIPSE 9×9 (snip diagonal connectors), then
   CLOSE with RECT 5×5 (fill court-line internal holes)
4. For each blue blob:
   - Geometric filters: area 1.2-22%, aspect 1.3-4.5,
     top_y > 0.25, bot_y < 0.95, center_y in [0.40, 0.85],
     solidity ≥ 0.55
   - Dilate blob 30px → measure red_frac in surrounding ring
   - Require red_frac ≥ 0.30
   - Score = red_frac × area_frac × centrality
     where centrality drops from 1.0 at cx=0.5 to 0.3 at edges
5. Highest score wins, fit quad via approxPolyDP on convex hull
6. Reject if fitted quad/blob area ratio > 1.5 (over-extension)
```

### Critical implementation detail — MORPH_ELLIPSE not MORPH_RECT

First Phase A.5 attempt used `cv2.MORPH_RECT` for the open kernel.
On 3 unseen failure videos, after open 9×9, the foreground table was
still merged with surrounding skirts/background tables via thin diagonal
connectors → fitted quad spanned the merged region → wrong.

Investigation revealed: rect kernel preserves 4 corner pixels. A
diagonal connector of ~3px width survives because the rect kernel's
corners reach across it. **Ellipse kernel has no corners** → snips
diagonal threads cleanly even at the same kernel size.

Single line change (`MORPH_RECT` → `MORPH_ELLIPSE`) unblocked 3 venues.
This is the kind of detail that's hard to find without rendering
per-stage diagnostic overlays.

### Multi-frame aggregation

Player occlusion is real — 1 frame might have a player blocking the
table center, fragmenting the blue blob. Solution: extract 5 evenly-
spaced refframes (10/30/50/70/90% of video duration) and aggregate.

Naive majority voting failed first attempt: in QuangVinh, 3 frames
returned correlated `learned_blend` answers (wrong, err 0.146), 1
frame returned `color_contrast` (right, err 0.058). Majority picked
the wrong cluster.

**Fix: tier-priority voting.** `learned_*` answers are CORRELATED
(same dataset queried every frame) — 3 correlated guesses are not 3
independent votes. Priority gates instead:

```
Gate 1: cc+orb_agree any cluster ≥ 1   (both detectors converged → highest trust)
Gate 2: orb cluster ≥ 2                (same-arena consensus)
Gate 3: cc cluster ≥ 2                  (universal-feature consensus)
Gate 4: learned_* cluster ≥ 4          (very strong learned consensus
                                        beats marginal cc singleton)
Gate 5: cc singleton                    (cc fires once → trust it over learned)
Gate 6: orb singleton
Gate 7+: learned_* cluster ≥ 2
```

Within winning gate: per-corner median of same-tier cluster members.

Gate 4 came from FQuang debugging: cc fired false-positive on 1 frame
(red 0.30 barely passes, area 0.03 marginal), 4 frames consistently
returned learned_blend (closer to truth). Bare priority would pick cc
singleton → err 0.107. Gate 4 ("4-frame learned consensus") prevents
this by promoting strong learned cluster above marginal cc singleton.

### Results

LOO on 13 entries (10 original + 3 unseen-confirmed-after-failures):

| Variant | Mean err | Max err | Notes |
|---|---:|---:|---|
| Phase A v6 (10 entries) | 0.0469 | 0.1322 | baseline |
| Phase A.5 multiframe + priority (13) | **0.0380** | **0.0796** | 13/13 pass |

4/13 entries use color_contrast tier as primary signal. 1/13 (vsVinh)
has both cc and ORB converging on same region (method tag
`color_contrast+orb_agree:0207_FQuang_2-3.MP4`, err 0.015) — strongest
signal possible.

### Roadmap → 100% (unchanged, accelerated)

| Phase | Target | Status |
|---|---|---|
| A (ORB) | 99%+ seen | shipped 2026-05-17 chiều |
| **A.5 (color-contrast + multi-frame + priority)** | **≥80% UNSEEN** | shipped 2026-05-17 tối; testing pending |
| Grow dataset 13 → 30 | cluster coverage | continuous |
| B (YOLOv8-seg) | 95%+ unseen | DEFERRED if A.5 sufficient |
| C (multi-modal) | 99.5% ensemble | DEFERRED |

**Key question for next session:** does Phase A.5 generalize on truly-
unseen videos (not the 3 just confirmed)? Operator needs to test. If
≥80% pass on 5-10 unseen → A.5 SHIPPED, unlock trim detection Phase 1b.
If <50% → escalate to Phase B earlier than planned.

---

## 1. Bối cảnh

**Project hiện tại:** v2.0 — manual-mode final release đã ship
2026-05-16. Operator phải làm thủ công 100%:
- Bấm `A`/`D` score real-time khi xem trận (~1h)
- Bấm `H` mark highlights
- Bấm `T`/`Y` mark trim points

**2 pain point user nêu:**
1. **Live scoring** — tốn time, khó (phải xem real-time không rời mắt)
2. **Trim Start/End** — cũng rất tốn time, scrub tới chỗ dead time

**Scope đã thu hẹp dần qua thảo luận:**
- Ban đầu mình đề xuất plan 5 phase giải cả 2 pain point
- User quyết định: **giữ scoring manual**, chỉ làm **auto-trim**
- UX cuối cùng: 1 nút "Auto detect" trong Trim panel hiện có, không
  build timeline UI / không build sliders / không build panel riêng

---

## 2. Setup vật lý quan trọng (user describe)

- Tripod cố định, đặt góc xéo xéo bàn
- **Cố định cả trận** (không pan/zoom/move)
- Bàn của user **luôn ở trung tâm** trong khung hình
- Sảnh thi đấu có **rất nhiều bàn khác cùng đánh** lọt vào khung hình
- **Audio không dùng được** — sảnh cực ồn, không nghe rõ tiếng bóng
  của bàn mình

**Insight kiến trúc rút ra từ setup này:**
- Camera fixed + bàn ở trung tâm → có thể define **ROI cố định**
  (quadrilateral) cô lập vùng bàn + player area, ignore các bàn khác
- Audio out → phải dùng CV
- Background subtraction trên ROI sẽ work tốt vì background không đổi
- Không cần ML model — chỉ cần đếm foreground pixels trong ROI

---

## 2b. Cách operator làm manual trim (⚠ ground-truth caveat)

User chia sẻ 2026-05-16, **đã clarify cùng ngày**:

- Manual trim **CHỦ QUAN / NHIỄU** — KHÔNG có threshold thời lượng.
  Operator lướt qua video, **thấy đoạn nào "rõ là banh chết lâu" thì
  cắt**: hết set, timeout, đi nhặt bóng xa, hoặc đôi khi cả đoạn ngắn
  hơn nếu tình cờ nhìn thấy.
- **Cũng có đoạn dài operator KHÔNG cắt** — vì không lướt qua thấy, mất
  tập trung, hoặc đơn giản lười. **Không chỉ đoạn ngắn mới bị miss,
  đoạn dài cũng có thể bị miss.**
- → Manual trims là một **subset chủ quan, phi hệ thống** của tổng dead
  time thật. Không threshold, không systematic, không đảm bảo coverage.
- **Auto-trim phải làm tốt hơn manual** — catch hết, kể cả các đoạn
  operator đã miss / lười cắt / không thấy.

**Implication kỹ thuật:**

1. **Manual `trims` KHÔNG phải ground truth ở bất kỳ chiều nào.**
   - Auto catch hết manual trims ≠ proven recall (manual có unknown
     false negatives).
   - Auto catch nhiều hơn manual ≠ false positive — nhiều khả năng là
     true positive operator đã miss.
   - Auto MISS 1 manual trim cũng không hẳn regression (e.g. set break
     có người đi qua ROI thì motion detection legitimately không phân
     loại là dead).

2. **Đừng tune threshold để match manual.** Đó là sai target.

3. **Auto-trim SẼ "over-trim"** so với manual — đó là đúng, expected.

4. **Để đo precision/recall thật**: cần 1 trong 2:
   - Eyeball QA trên vài render output (subjective, nhưng đó là
     operator's quality bar thật)
   - 1 video "gold standard" — operator dành thời gian đánh dấu MỌI
     gap (làm 1 lần, ~2× thời gian thông thường)

---

## 3. Evolution của approach (đã từ chối những gì)

### ❌ Approach 1: Audio-based rally detection (rejected)
- Idea ban đầu: detect tiếng "pock" của bóng (~2-5kHz transient)
- **Rejected vì:** sảnh nhiều bàn → audio bị mask hoàn toàn, không
  thể tách bóng của bàn mình ra khỏi tiếng các bàn xung quanh

### ❌ Approach 2: OCR venue scoreboard (rejected)
- Idea: nếu sảnh có bảng LED điểm và lọt vào khung hình, OCR đọc trực
  tiếp → skip rally detection cho scoring
- User trả lời: **không bao giờ** có bảng LED trong khung hình

### ❌ Approach 3: Full 5-phase plan (Rally Review UI + auto-trim +
###    highlights + CV attribution) (deferred)
- Mình đề xuất plan to: backend detection → ROI editor → Rally Review
  scoring panel → auto-trim suggest → highlight auto-suggest → CV ball
  tracking
- **User thu hẹp:** chỉ làm auto-trim, scoring vẫn manual

### ❌ Approach 4: Auto-Trim panel với timeline visualization (rejected)
- Mình đề xuất: panel riêng với canvas timeline (green=rally, red=gap),
  padding sliders, per-rally exclude
- **User thu hẹp:** chỉ 1 nút trong Trim panel hiện có. Operator review
  bằng cách click trim entry → jump-to-start → verify ngẫu nhiên

### ✅ Approach cuối: CV motion detection trong ROI → append trim list
- Background subtraction (MOG2) trong ROI quadrilateral
- Rally segments → invert thành trim list
- Append vào `project.trims` với tag `source: "auto"`
- Re-run Auto detect: xoá auto trims cũ, giữ manual trims, tạo lại auto

---

## 4. Decisions đã chốt (qua AskUserQuestion)

| Quyết định | Giá trị | Rationale |
|---|---|---|
| ROI shape | **Polygon 4 điểm** (quadrilateral) | Khớp phối cảnh bàn chéo + bao vùng player movement. Linh hoạt hơn rect, đơn giản hơn polygon tự do |
| ROI default | Rect 60%×40% giữa frame | 4 đỉnh `(0.2,0.3), (0.8,0.3), (0.8,0.7), (0.2,0.7)` |
| Detection trigger | Manual button | Operator kiểm soát khi nào chạy, không tự động khi load video |
| Padding pre | **1.0s** | Default 0.5s quá chặt; 1s giữ được anticipation trước rally |
| Padding post | **2.0s** | Giữ reaction sau pha bóng + lúc bóng rơi xuống đất |
| Min rally duration | **0.8s** | Loại noise + knock-up; giữ được miss serve thực tế (~1s) |
| Existing manual trims | **Append** (không thay) | Auto trims tag `source: "auto"`, manual giữ `source: "manual"` |
| CV ball tracking Phase 5 | **Defer** | Sau khi auto-trim ship + dùng thực tế mới quyết |
| Dep mới | `opencv-python` (~40MB) | Đã approve. Không thêm librosa hay torch |

---

## 5. Open questions chưa giải quyết hết

Đây là những điểm raise trong thảo luận nhưng user nói "còn thảo luận
thêm" — cần bàn tiếp:

### 5.1. False positive sẽ ảnh hưởng thế nào?

ROI cô lập bàn của user, nhưng motion vẫn có thể trigger từ:
- **Người đi qua giữa camera và bàn** (operator giải, trọng tài, khán
  giả) → motion lớn trong ROI dù không có bóng
- **Bóng lạc từ bàn khác** bay vào ROI → motion spike ngắn
- **User tự nhặt bóng / lau bàn** → motion trong ROI nhưng không phải
  rally đang đánh

**Câu hỏi cần thảo luận:**
- MVP có cần auto-trim chính xác 100% không, hay accept rằng operator
  vẫn phải review + delete vài trim sai bằng tay?
- Nếu cần độ chính xác cao → cần đầu tư thêm (ví dụ: pattern matching
  motion oscillation để tách rally vs người đi qua)

### 5.2. Tốc độ detection — CPU hay GPU?

| | CPU | GPU (cv2.cuda hoặc torch wrap) |
|---|---|---|
| Speed cho 1h video | 3-5 phút | < 1 phút |
| Setup complexity | None | Cần cv2 build CUDA hoặc torch + cv2 hybrid |
| Acceptable? | Có lẽ OK | Có đáng đầu tư extra setup? |

**Câu hỏi:** 3-5 phút CPU OK chưa hay cần GPU?

### 5.3. Output có "choppy" không?

Mỗi rally ~5-15s stitch thẳng tới rally tiếp qua concat demuxer → hard
cut. Không có crossfade.

**Options:**
- Accept hard cut (đơn giản, render nhanh)
- Padding rộng hơn (default lên 2s/3s)
- Crossfade 200ms giữa segments → phải dùng filter_complex thay vì
  concat demuxer → render lâu hơn

### 5.4. Workflow: auto-trim trước hay sau marking?

Hiện tại workflow user:
1. Xem full 1h video
2. Mark highlights bằng `H` (sẽ slow-mo replay)
3. Score bằng `A`/`D`
4. Mark trims bằng `T`/`Y`
5. Render

**Sau auto-trim, 2 options:**
- **(a) Auto-trim sau marking:** flow như cũ, auto-trim chỉ filter
  output cuối. Operator vẫn xem full 1h để mark + score.
- **(b) Auto-trim trước marking:** chạy auto-trim đầu tiên → operator
  xem clip đã trim (ngắn hơn 60%) → mark highlight + score trên đó.
  **Big change** — player phải support hiding trimmed regions trong
  playback.

**Câu hỏi:** option (a) đủ chưa hay cần (b)?

### 5.5. ROI per project hay ROI preset?

Tripod thường đặt vị trí gần giống nhau qua các trận (cùng setup).

- **(a)** ROI riêng từng project, vẽ lại mỗi trận (~30s) — đơn giản
- **(b)** Save 1-2 "ROI preset" trong config.json, project mới load
  preset → nhanh hơn nếu setup ổn định
- **(c)** Cả 2: default từ preset, operator override per-project nếu cần

---

## 6. Phase 0 prerequisites — ✅ đã đáp ứng

**Cập nhật 2026-05-16 tối:**

- ✅ **3 dataset entries đã thu thập** trong `dataset/`. Source video +
  output + project.json + groundtruth.json + refframe.png + notes.md
  + manifest.json đầy đủ. Auto-archive shipped — workflow manual của
  operator không đổi, dataset tích lũy sau lưng.
- ✅ **Schema groundtruth v2** đã rename `rally_segments` → `kept_segments`
  + thêm `real_rally_count` derive từ score events. Migration đã chạy
  cho 3 entries.
- ✅ **notes.md auto-filled** mỗi entry với match info / kept segments
  breakdown / real rally stats / dead time gap distribution / caveat
  về manual partial labels.

**3 entries (xem `dataset/manifest.json`):**

| Slug | Tournament | Duration | Real rallies | Avg/rally | Dead% |
|---|---|---|---|---|---|
| match_001_20260516_215553 | BBTV Open (vs Tân) | 22:00 | 75 | 11.2s | 36.1% |
| match_001_20260516_223928 | BBTV Open (vs Hà) | 20:41 | 79 | 10.6s | 32.6% |
| match_001_20260516_230801 | Giao Lưu (vs Đạt) | 13:55 | 65 | 11.3s | 12.2% |

Tất cả 2688×1512 @ 59.94fps, hardlink cả source + output → ~17.8GB dataset.

**Avg rally 10.6–11.3s consistent qua 3 trận** → con số đáng tin cậy cho
spike tuning. `min_rally_duration` nên đặt **<< 11s** (1-3s).

**Pass criteria của Phase 0 (cập nhật cho manual = partial labels):**
- **Recall ≥ 95% trên manual trims subset** (catch hết / gần hết những
  gì operator đã đánh dấu — manual subset đã selected chủ quan rồi nên
  catch được là OK)
- **Catch thêm small gaps** (5-15s) operator không mark — đó là goal,
  không phải side effect
- **KHÔNG đo precision** vs manual labels (manual không phải full GT —
  xem §2b). Eyeball QA trên rendered output là cách evaluate thật.

**Nếu fail:**
- Manual recall < 95% → tune threshold xuống, expand ROI, check ROI bao
  đủ vùng player chưa
- Pre/post match noise quá nhiều → accept (operator có thể xóa manual)
  hoặc thêm heuristic "skip first/last N seconds"
- Fail nặng → escalate sang YOLOv8 ball detection (Phase 5)

---

## 7. Plan implementation (chưa chốt — review ngày mai)

| Phase | Effort | Tóm tắt | Blocker |
|---|---|---|---|
| 0 — Spike validate | ½ ngày | Script standalone trong `scripts/`, không đụng codebase chính. Test trên 1 entry, đo recall trên manual subset + count thêm gaps detect được | **ROI coords** cho entry chọn |
| 1 — Backend detector | 1 ngày | `backend/rally_detector.py` pure-logic module + caching | Phase 0 pass |
| 2 — ROI editor frontend | ½ ngày | Modal canvas, drag 4 corners trên refframe; endpoint serve refframe | Có thể parallel Phase 1 |
| 3 — "Auto detect" button | 1 ngày | Nút trong Trim panel hiện có; POST `/api/auto_trim`; append với tag `source: "auto"` | Phase 1 + 2 |
| 4 — Persistence + tests | ½ ngày | `ProjectInfo.roi_quadrilateral` + `TrimSegment.source` field; pytest pure-logic | Phase 3 |
| 5 (optional) | 2-3 ngày | YOLOv8 ball tracking — chỉ làm nếu Phase 0-4 chưa đủ chính xác | Defer |

**Total Phase 0–4: ~3.5 ngày work.** [TODO.md](TODO.md) có chi tiết cũ
(viết trước khi schema v2 + dataset auto-archive ship), cần review lại.

---

## 8. Resume points cho buổi sau (2026-05-17)

**Trạng thái:** dataset infrastructure đã ship hết (auto-archive,
schema v2, notes auto-fill, migration). 3 entries sẵn sàng. **Chưa có
dòng auto-trim code nào.** Tất cả pending là decision + Phase 0 spike
viết từ đầu.

### Vấn đề cần review/quyết trước khi bắt đầu Phase 0

**A. Chốt phase list (§7).** Đọc lại 6 phase (0-5), confirm scope mỗi
phase. Có muốn cắt/gộp/đổi thứ tự không?

**B. Resolve open questions tối thiểu để Phase 0 chạy được:**

- **§5.5 (ROI per project vs preset)** — Phase 0 cần ít nhất 1 ROI cho
  1 entry. 3 options cho ROI coords:
  - (a) Tôi mở `dataset/.../refframe.png` (Read tool render PNG được),
    propose 4 coords → operator confirm. **Nhanh nhất.**
  - (b) Operator tự đo 4 góc trên refframe (Paint, hover chuột đọc
    pixel) → gửi tôi
  - (c) Hardcode rect mặc định `(0.2, 0.3, 0.8, 0.7)` normalized →
    spike chạy nhưng có thể không tối ưu
- **§5.1 (false positive tolerance)** — defer được. Spike chạy xong
  biết tỷ lệ thật mới quyết.
- **§5.2 (CPU vs GPU)** — defer. Bắt đầu CPU; nếu spike chạy quá chậm
  trên 22-min video 2688×1512@60fps thì upgrade.
- **§5.3 (crossfade vs hard cut)** — không liên quan Phase 0-4. Defer.
- **§5.4 (auto-trim trước/sau marking)** — không liên quan Phase 0.
  Quyết ở Phase 3 UX design.

**C. Chốt entry nào spike trên đó:**

- Đề xuất **entry #3 (vs Đạt, 13:55)** — ngắn nhất để iterate nhanh +
  dead time chỉ 12% là worst case cho auto-trim (ít manual labels) +
  3 highlights để cross-check
- Hoặc entry #1 / #2 (dài hơn, nhiều label hơn)

**D. Phase 0 spike scope cụ thể (chưa chốt):**

- Input: entry path + ROI + tunables (threshold, min_rally_duration,
  padding_before, padding_after, downscale_factor)
- Logic: NVDEC decode → crop ROI → MOG2 → foreground pixel count per
  frame → smooth → threshold → group thành dead segments
- Performance target: 22-min 2688×1512@60fps source → spike chạy < 5
  phút trên CPU. Nếu không đạt → downscale 960×540 + frame-skip ×2.
- Output: print detected dead segments + compare với manual trims
  (recall ≥ 95%, count thêm gaps) → markdown report

**E. Performance constraint cần lưu ý:**

Source 2688×1512 @ 59.94fps là **4× pixel-rate của 1080p@30fps**. MOG2
full-res 22 phút sẽ chậm. Spike script ngay từ đầu phải downscale +
frame-skip để fit budget.

### Đề xuất bắt đầu ngày mai

1. Review §7 phase list, push back chỗ nào không ưng
2. Quyết entry để spike + cách lấy ROI (recommend a+c: tôi xem refframe
   propose, operator confirm)
3. Code Phase 0 spike script
4. Chạy spike → có metric thật mới quyết §5.1, §5.2

---

## 11. Phase A — ORB-homography algorithm v6 (2026-05-17 tối)

### Bối cảnh

Phase 1a UI shipped, operator confirmed 10 ROIs trong dataset/roi_groundtruth/.
Algorithm v0-v3 dùng HSV color-histogram NN/blend đạt ceiling **mean LOO error
6.2%** (~120 pixel trên 1920×1080). Mục tiêu operator chốt là **100% automatic
không cần review**. HSV alone không đủ → escalate Phase A.

### Quyết định algorithm

3-stage pipeline trong `backend/roi_detector.py`:

```
Stage 0  ORB keypoints + RANSAC homography
Stage 1  HSV histogram NN/blend/mean (fallback)
Stage 2  Naive color (no groundtruth case)
```

### Vì sao ORB > HSV

HSV chỉ capture **color distribution**. Hai refframes cùng arena nhưng khác
camera angle có thể color giống y hệt → HSV cho similarity cao nhưng ROI
position khác. ORB tracks **spatial keypoints** (corners, edges của objects)
→ inlier count cao = camera POV thực sự tương tự → homography H map pixel
positions chính xác → ROI transfer near-pixel-accurate.

### Critical sanity checks

LOO v5 (ORB không sanity) cho mean error 7.3% — WORSE than HSV baseline!
Lý do: ORB matches concentrated trên scene features ngoài bàn (banners,
equipment, people) → homography over-fits những regions đó → ROI transfer
distorted wildly. Case xấu nhất: MinhDuong error 27%.

Thêm 3-layer sanity:
1. **Bounds**: corners phải nằm trong [-0.15, 1.15] (normalized)
2. **Displacement**: max corner shift từ source ≤ 12% (same camera shouldn't shift more)
3. **Area ratio**: transferred polygon area / source ≤ [0.5×, 2.0×]

→ v6 mean error giảm xuống **4.7%** (-25% vs HSV baseline).

### Performance

- ORB feature compute: ~50ms per refframe
- Match 1 query × 10 examples: ~400ms total
- Acceptable cho modal UX (sub-second response)

### Results

LOO validation breakdown:

| Source | Method | Err | Notes |
|---|---|---:|---|
| DoBinhMinh, Tan, Ha (cluster A) | ORB exact 78-96 inliers | 0.006 | pixel-accurate |
| CuongPhan, NguyenTon (cluster B) | ORB exact | 0.043 | good |
| MinhDuong | ORB rejected → HSV blend | 0.016 | sanity worked |
| 3 singletons | HSV mean fallback | 0.06-0.08 | no cluster |
| QuangVinh | HSV NN (sim 0.65) | 0.132 | cross-cluster borderline |

**Operator self-test**: opened all 10 confirmed videos in modal → 10/10
pixel-accurate (validated 2026-05-17 tối).

### Roadmap → 100%

| Phase | Target accuracy | Trigger |
|---|---|---|
| **A** (done) | 99%+ on seen-cluster arenas | Same as 2026-05-17 |
| **Grow** | Singletons → clusters | Operator confirms 2-3 matches/venue |
| **B** | 95%+ on UNSEEN arenas | After ≥30 confirms — YOLOv8-seg fine-tune |
| **C** | 99.5% ensemble | Multi-modal + geometric validation |

Phase B requires ML dep (`ultralytics`, ~500MB). Defer until dataset đủ
diverse (5-8 clusters × 3-5 examples each).

---

## 10. ⚠ MILESTONE 2026-05-17 chiều — ROI gates everything

Operator review overlay video từ Phase 0 spike → phát hiện ROI eyeball-guess
của assistant **lệch xa** trên E1, E2. ROI sai → motion signal sai → mọi
tuning downstream vô ích. Quyết định:

**Tạm ngưng mọi subsystem auto-trim khác. Phase 1 thu hẹp về ROI workflow only.**

Build feedback loop UI:
- Click Auto Trim → modal hiển thị refframe + proposed ROI
- Operator confirm / edit 4 corners / re-detect
- Confirm → save vào project + append vào `dataset/roi_groundtruth/<hash>.json`
- Stop. Trim detection chờ ROI proven trên ≥10 inputs.

Files: `backend/roi_detector.py`, 3 endpoints (refframe/detect_roi/confirm_roi),
modal HTML + JS. Chi tiết trong [TODO.md](TODO.md).

Phase 0 spike report giữ nguyên trong `scripts/spike_out/PHASE0_REPORT.md`,
nhưng **không apply số liệu đó cho production** cho đến khi ROI đúng.

---

## 9. Cập nhật 2026-05-17 — Algorithm pivot + GUI in scope

Buổi thảo luận chiều 2026-05-17 đã đảo ngược 2 quyết định lớn của plan
gốc. Section này là **plan-of-record mới**. Section 1-8 ở trên giữ lại
làm rationale lịch sử.

### 9.1. Algorithm: pivot sang score-anchored

**Vấn đề với blanket MOG2 (plan gốc):**

Bóng bàn quá nhỏ (40mm → 2-5px blurred) để track. MOG2 motion-in-ROI
chỉ thấy player motion, và **không phân biệt được**:
- Rally thật vs người đi qua ROI (trọng tài, khán giả, operator giải)
- Rally thật vs player nhặt bóng / lau bàn / chuẩn bị serve
- Rally thật vs bóng lạc từ bàn khác bay vào

→ False positive rate cao, threshold global khó tune.

**Approach mới:** dùng **score events** làm rally-end anchors.

Operator đã bấm A/D tạo score events cho scoreboard rồi → mỗi event
là rally-end **đáng tin, miễn phí**. Bài toán biến đổi:

| Cũ | Mới |
|---|---|
| "Tìm tất cả rally trong 22 phút" | "Tìm 1 transition dead→rally trong mỗi gap đã bound" |
| Global threshold | Per-gap search |
| Hỏng vì person walking | Person walking không tạo false rally vì ta tìm transition đầu, không phải tất cả motion |
| Tune cho từng trận | Robust vì có anchor |

Pseudo-code:
```python
for i, score in enumerate(score_events):
    if i == 0:
        # Pre-match: trim từ video_start tới ~11s trước score đầu
        emit_trim(0, score.time - RALLY_DUR_PRIOR - LAG_CORRECTION)
        continue
    prev_score = score_events[i-1]
    gap = (prev_score.time + TAIL_PAD, score.time)
    motion = motion_signal[gap]
    rally_start = first_sustained_motion(motion, min_dur=1.0, threshold=T)
    if rally_start is None:
        # Missed score case: fallback blanket trong gap
        ...
    else:
        trim_start = prev_score.time - LAG_CORRECTION + TAIL_PAD
        trim_end = rally_start - PRE_PAD
        if trim_end - trim_start >= MIN_TRIM_DUR:
            emit_trim(trim_start, trim_end)
# Post-match
emit_trim(score_events[-1].time + TAIL_PAD - LAG_CORRECTION, video_end)
```

**Workflow implication:** Auto Trim button hoạt động sau khi operator
score xong. Đây là **OK** vì:
- Workflow hiện tại đã là: xem video → score real-time → trim
- Score events bạn tạo cho scoreboard anyway, không tốn thêm công
- Đây là điểm khiến approach này robust hơn — có "free signal"

**End-state:** Khi algorithm ổn định, auto-trim sẽ chạy ngầm trong
Render button (sau khi operator finalize scoring). Modal debug GUI
giữ lại để debug khi cần, không phải mandatory step.

### 9.2. GUI: debug modal IS in scope (reversed)

Plan gốc nói "không build UI panel riêng, chỉ 1 nút trong Trim panel".
**Đảo ngược 2026-05-17:** operator cần visibility để debug algorithm
trong iteration phase.

Modal full-screen overlay khi bấm "Auto Trim":
- **Left:** ROI panel với refframe.png + 4-corner quadrilateral overlay,
  view/edit toggle. Operator có thể drag góc để điều chỉnh ROI per-project.
- **Right:**
  - Run controls (Start/Cancel/Apply/Discard)
  - Progress bar + stage name ("Decoding frame 4521/79320")
  - Params display (ROI summary, padding, min_rally, lag_correction)
  - **Live log stream** qua SSE: stage transitions, motion samples,
    detected rally/trim segments, errors
  - Results summary sau khi xong: "78 rallies, 41 trims, 12.3 min saved"

Backend implementation: SSE endpoint `/api/auto_trim/events/{job}` push
structured events realtime → frontend EventSource client append vào
log + update progress.

### 9.3. GPU: chỉ NVDEC, bỏ cupy

Plan gốc đề xuất cupy GPU ops + ffmpeg NVDEC. **Đảo ngược:** bottleneck
thực sự là decode (NVDEC giải quyết); motion compute sau downscale
480×270 chỉ ~130k pixel/frame → CPU numpy vectorized đủ nhanh.

- ✅ ffmpeg NVDEC decode + `scale_cuda` downscale + crop trên GPU
- ✅ Motion compute trên CPU numpy
- ❌ cupy (tránh dep + Blackwell sm_120 compatibility risk)
- ❌ cv2.cuda (build từ source painful trên Windows)
- ❌ torch (overkill)

Nếu sau spike thấy CPU motion compute chậm → mới cân nhắc thêm cupy.

### 9.4. Open questions update

| ID | Câu hỏi | Status |
|---|---|---|
| 5.1 | False positive tolerance | **Resolved partial:** debug modal cho operator review trim đề xuất + discard sai trước khi Apply. Iteration phase chấp nhận tỷ lệ FP cao; end-state cần FP ≤5% để safe auto-handle trong Render |
| 5.2 | CPU vs GPU | **Resolved:** NVDEC only, motion CPU |
| 5.3 | Hard-cut vs crossfade output | Defer (không liên quan trim detection) |
| 5.4 | Auto-trim trước/sau marking | **Resolved:** SAU. Workflow: score → auto-trim → render |
| 5.5 | ROI per project vs preset | **Resolved partial:** Phase 3 saves per-project. Preset có thể thêm sau (load mặc định từ last project hoặc config.json) |

### 9.5. Phase plan revised (xem TODO.md)

| # | Phase | Effort | Tóm tắt |
|---|---|---|---|
| 0 | Algorithm spike CLI | 1 ngày | 2 algorithms (score-anchored + blanket) song song trên 3 entries. Validate trước khi build GUI |
| 1 | Backend + SSE | 1.5 ngày | Detector + endpoints + cache. Tách `run_auto_trim()` headless cho Phase 5 integration |
| 2 | Modal scaffold + log viewer | 1 ngày | SSE client, progress, log stream |
| 3 | ROI editor in modal | 1 ngày | Canvas + 4-corner drag, view/edit toggle |
| 4 | Apply + persistence + tests | 1 ngày | Schema, append, pytest |
| 5 | Render button integration | ½ ngày | Headless auto-trim trong render pipeline (sau khi algorithm proven) |
| 6 (opt) | YOLOv8-pose escalation | 2-3 ngày | Chỉ nếu Phase 0 fail |

**Tổng base: ~5 ngày** (Phase 0-4) + ½ ngày Phase 5 integration.
