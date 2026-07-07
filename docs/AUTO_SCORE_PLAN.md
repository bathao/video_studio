# Live Score Automation — Plan (drafted 2026-07-07)

Automate the Live Score workflow: detect each rally's end and WHO won
the point, producing `score_events` automatically instead of the
operator pressing A/D ~70-100 times per match.

Status: **PLAN — Phase 0 spike not started.** Nothing here is committed
scope until the Phase 0 gate passes. Companion discussion doc for the
Auto Trim precedent: [AUTO_TRIM_DISCUSSION.md](AUTO_TRIM_DISCUSSION.md).

---

## 1. Hard constraints (operator-stated)

- **Production safety.** The app is in production. Manual Live Score
  must keep working untouched at every moment of this feature's
  development. Automation is additive, never a replacement, until it
  earns it.
- **Full isolation.** Auto lives in its own tab, own backend package,
  own endpoints, own state. A broken/half-built Auto path can never
  corrupt a manual scoring session.
- **Gradual delivery.** Ship in stages that are each independently
  useful (semi-auto before full-auto), mirroring how Auto Trim went
  ROI-gate → Phase 1a → Phase 1b.
- **Reuse the ROI work.** The table region for winner detection is the
  same region Auto Trim detects; reuse `detect_roi_multiframe` + the
  `roi_groundtruth` dataset as-is. Per the standing rule, the ROI
  algorithm itself is not to be touched.
- **Local models only.** No cloud calls; the LLM/vision model runs on
  the operator's machine (RTX 5060 Ti, 16 GB VRAM).

## 1.5 Lessons from attempt #1 — `C:\Users\MSI\scoreboard_tool` (Feb–May 2026, failed)

The operator already attempted this exact feature as a standalone tool.
Full post-mortem was read on 2026-07-07. What matters for attempt #2:

**How it failed.** A 6-step waterfall (trim → face-ID players → rally
detection → winner VLM → review → render) died permanently at step 3.1
(rally-START detection via YOLOv8x-pose wrist velocity). Eleven
detector versions never passed row-level truth on ONE 6-minute test
clip; the only "pass" was a cached re-merge the project's own risk
register flagged as false confidence. Everything downstream (side
state, set boundaries, winner prediction) stayed blocked forever.
Root cause in their own notes: **the pose-motion signal was not
duration-invariant** — whole-clip normalization broke on long videos
(side swaps, walking, adjacent tables, spectators).

**The deeper design flaw: error accumulation without resync anchors.**
Reconstructing score from play means ONE missed rally or wrong winner
desynchronizes everything after it. They had no anchor to resync on.

**Winner VLM reality check.** Qwen3-VL-4B + LoRA (trained on 71
labeled rallies from one match) scored **67% winner accuracy** on a
9-sample eval — barely above coin flip on a binary task. Treat ~65-75%
raw as the realistic prior for G0 planning, not 90%.

**Process failures to not repeat:** Web UI + rendering + LoRA training
stack + 12-phase refactor all built before the core detector worked;
no eval harness for the first 2 months; all tuning on one short clip.

**Why attempt #2 has materially better odds:**
1. Rally detection here uses a DIFFERENT, production-proven signal:
   ROI motion energy with per-match adaptive threshold, validated on
   full 14-22 min matches (Auto Trim, recall 95-99% anchored). The
   thing that killed attempt #1 is the thing we already have working
   at full-video length. Unanchored mode still must be measured, but
   we start from a detector that survives long video.
2. Resync anchors are designed in: score-grammar solver + filename
   final score + set boundaries + human review checkpoints.
3. Eval harness + 649 labeled points exist BEFORE any detector code
   (their truth files arrived 2 months in; ours are already on disk).
4. Human-in-the-loop is the product (semi-auto Phase 1), not an
   admission of defeat — their own post-mortem recommends exactly this.
5. No face-ID needed: P1/P2 by table side + per-set mapping in the
   solver (their ArcFace layer broke on back-to-camera + same-color
   jerseys; we simply don't need identity).

**Salvage from attempt #1 — COPIED 2026-07-07 into
`dataset/attempt1/`** (see its README_ATTEMPT1.md; verified parsing):
- **71 human-reviewed rally clips + labels** `{winner, loser,
  taxonomy, last_hitter}` (match_vinh_001, winner split a=42/b=29) —
  merged into our eval corpus.
- **34-point rally-START truth incl. 1 LET** for `2_sets.mp4`
  (match_2_sets_debug_001) — the only rally-start/LET truth we have
  (our 649 points mark key-presses near rally END only); primary
  metric for the unanchored-segmentation eval.
- Source videos hardlinked into `dataset/attempt1/source_videos/`
  (2_sets.mp4 1.6 GB + match_vinh_001_full.mp4 4.5 GB, zero extra
  disk).
- Finetune split manifests (collections/finetune_dataset/).
- Still in the old repo, port when needed:
  `backend/side_swap_detection.py` (operator-verified side-swap
  detector), 11-class taxonomy (`backend/winner_prompt.py`), eval
  script patterns, downloaded Qwen3-VL-4B/8B models.

**Hard rules inherited:** validate on FULL-LENGTH video from day one
(never tune on short clips); truth files are validation only, never
detector input; every VLM call archives its exact inputs + raw output.

## 2. Why this is hard (honest assessment)

Determining the WINNER of a table-tennis point from a single tripod
camera is far harder than anything shipped so far:

- The ball is 2-5 blurred pixels at 1080p (Phase 0 spike conclusion —
  direct ball tracking is infeasible; that finding stands).
- "Who missed" is a judgment even humans need replays for — net clips,
  edge balls, out-by-2cm.
- A match has 70-100 points. Even 95% per-point accuracy → ~4 wrong
  points per match → wrong burned-in scoreboard → unusable output
  without review. The bar for UNREVIEWED auto is ~99.5%; the realistic
  near-term product is **auto-propose + fast human review**.

The plan is therefore **evaluation-first**: prove signal quality
offline against labeled data BEFORE building GUI.

## 3. Assets already in hand (verified 2026-07-07)

| Asset | Value |
|---|---|
| **551 unique labeled points** across **7 unique matches** (`dataset/<slug>/groundtruth.json` → `score_events`; corpus builder 2026-07-07 found the 9 manifest slugs contain re-archived duplicates of one match) | Free eval corpus for ANY winner-detection approach. Grows with every render. |
| All 9 source videos have **AAC stereo audio** | Audio signals (score call-outs, ball-hit rhythm) are testable. |
| ROI detector (`detect_roi_multiframe`) + 54-entry groundtruth | Table region solved — reuse untouched. |
| Rally detector (`backend/rally_detector.py`) | Motion signal + gap detection exists; needs an UNANCHORED mode (see 5.1). |
| Filenames encode final match score (e.g. `..._1-3.MP4`), operator names sets in projects | Global constraints for the score solver (see 5.4). |
| RTX 5060 Ti 16 GB | Fits a 7-8B VLM quantized (~6-7 GB) with headroom for NVDEC decode. |

### 3.1 Camera geometry (operator, 2026-07-07)

- Fixed tripod per match; **diagonal angle from behind one player**.
- Different matches → different angles/venues; within a match the
  frame is constant (same property the ROI multi-frame detector
  exploits).
- Source quality is 2K (e.g. 2688×1512 observed in dataset) — ROI
  crops retain usable player-level detail; the ball itself is still
  only a few pixels (ball tracking stays ruled out).
- Consequence: the two players are distinguishable as **near-side vs
  far-side** of the table. But players SWITCH SIDES between sets (and
  at 5 in the deciding set) — so near/far → P1/P2 mapping is per-set
  state, not a constant. The solver owns this mapping: side-switch
  events are themselves strong set-boundary evidence, and the initial
  mapping (who starts near) is either operator-confirmed in the Auto
  tab (one click) or inferred by the solver from constraints.

### 3.2 Manual-input flywheel (operator request 2026-07-07)

The operator keeps doing normal manual production; every finished
match must feed the automation side WITHOUT workflow changes. The
archive half already exists — every successful render hardlinks the
source + snapshots project.json + groundtruth.json into
`dataset/<slug>/`. The conversion half is Phase 0 step 1's corpus
builder, run incrementally (idempotent, keyed by manifest entry) so
the corpus grows with each render. What each manual input converts
to:

| Manual input (already captured) | Derived label | Trains / validates |
|---|---|---|
| Score event `timestamp` | Rally END ≈ t − press-lag (GOLD, ±1-2 s operator reaction noise) | Unanchored segmenter eval; clip cutting |
| Score event `who` | Winner of the ±6 s clip (GOLD — burned scoreboard was operator-reviewed in output) | VLM finetune / classifier / eval |
| Anchored detector's backward walk per event | Rally START (SILVER — machine-derived but production-accepted via rendered output) | Full rally segments; segmenter training |
| Score sequence (grammar) | Serve side per rally, deterministic from score (GOLD given events) | Serve-side detector labels — free |
| Set transitions in event sequence | Set boundaries + side-switch times | Solver features; per-set mapping |
| `best_of`, filename final score, names | Match-level constraints | Solver |
| Manual `trim_segments` | ⚠ NOISY partial negatives only — never treat as truth (standing rule) | Nothing directly |

One missing bit per match: the **near/far ↔ P1/P2 initial mapping**
(who starts on the camera side). Vision models see near/far; labels
say P1/P2. Side-switch RULE gives every subsequent set from the
first, so it is literally 1 bit per match. Plan: optional one-click
field in the GUI ("P1 starts near") + a backfill script that shows
one frame per archived match for the operator to answer in bulk
(~9 clicks for the current corpus). Until backfilled, winner-model
training can bootstrap from attempt #1's 71 clips (already near/far
labeled).

Held-out discipline (attempt #1 rule, inherited): fix ≥2 matches as
never-train, never-tune eval-only from day 1; every new render lands
in TRAIN by default so the held-out set stays clean.

#### Data budget — how many manual matches make training "healthy"

Basis: 649 points / 9 matches = ~72 points per BO5 match. Each new
manual match contributes ~72 GOLD winner clips + ~72 free serve-side
labels + ONE new camera angle/venue (operator confirms every match
has a different angle — so each match is also a diversity sample,
which matters more than raw clip count for generalization).

| Corpus | Total matches | What it unlocks |
|---|---|---|
| 649 clips | 9 (today) | ALL of Phase 0: segmentation eval, VLM bake-off, classifier spike v1. Do not wait for more data to start. |
| ~1,000 clips | ~14 (+5) | Classifier retrain #1 with a meaningful val split; ~12 distinct angles. |
| ~1,500 clips | ~20 (+11) | **Healthy floor**: small fine-tuned video classifier can generalize across venues; LoRA VLM finetune enters its comfortable regime. |
| ~2,200 clips | ~30 (+21) | Trained classifier ≥85-90% raw becomes realistic; post-solver flags shrink to ~5-8/match. |
| ~2,900 clips | ~40 (+31) | Near-full-auto regime: classifier + solver plausibly ≥97-99%; flags ~0-5/match — review becomes a 1-minute glance. Beyond this, volume matters less than hard-case mining (deuce endings, edge/net balls, lets). |

Retrain cadence mirrors the ROI habit: re-run corpus build + retrain
at each ~5-match milestone, re-measure on the fixed held-out set
(2-3 matches reserved out of the totals above). Matches whose sets
reach deuce are extra valuable (serve-rotation-at-deuce validation
for the solver) — worth prioritizing for manual input when the
operator has a choice.

## 4. Signal inventory for "who won" (ranked by expected reliability)

> ❌ **Audio is ruled out** (operator, 2026-07-07): venues are large
> halls with many tables playing simultaneously — score call-outs are
> not reliably audible in the recordings. Do not invest in ASR paths.
> This makes the task VISION-ONLY and raises the bar accordingly.

1. **Score-grammar constraint solver.** Table-tennis scoring is a
   strict grammar: 11+2 set wins, best-of-N, serve rotation every 2
   points (every point at deuce), side switch between sets, final
   result often known from the filename. Given rally-end timestamps +
   NOISY winner observations, a Viterbi/HMM pass over score states can
   correct isolated errors and flag inconsistencies with confidence.
   This multiplies whatever raw per-point accuracy the vision models
   give — it is the reason imperfect vision can still work.
2. **Local VLM judgment on rally-end clips.** Feed K frames (~2 fps
   covering last ~4 s of rally + 2 s after) cropped around the table
   ROI to a local vision LLM (shortlist refreshed 2026-07-07 — see
   §4.2) with a structured prompt → `{winner, confidence, reason}`.
   Honest expectation from attempt #1's measured 67%: zero-shot/lightly
   tuned VLMs are weak at this — must be benchmarked, not assumed.
   Post-rally behavioural cues (loser fetches the ball, posture,
   who walks away from the table) may carry more signal than the
   rally itself — prompt for those explicitly.
3. **Trained video classifier — co-equal bet, not a fallback.** With
   649 labeled clips (and growing per render), a small fine-tuned
   classifier (VideoMAE / X3D / even frame-pair CNN on ROI crops)
   frequently beats zero-shot VLMs on narrow binary tasks. With audio
   gone, Phase 0 benchmarks this path alongside the VLM, not after it.
4. **Serve-side detection as a solver observation.** WHO SERVES each
   rally is visually much easier than who won (server stands still at
   the table edge, ball toss posture, ~1-2 s hold at rally start).
   Serve order is deterministic given the score (switch every 2 pts,
   every point at deuce) — so a detected serve-side sequence
   constrains score evolution strongly at deuce and validates it
   elsewhere. Cheap to detect (pose/position in first frames of each
   rally), high solver value.
5. **Cheap heuristics as solver features** (not standalone): which
   side's motion continues after rally end (ball fetch), pause length
   (longer after set point), player position swap (set boundary).

### 4.1 Anchoring is a gradient, not a binary (operator insight 2026-07-07)

Auto Trim's 95-99% recall does NOT transfer to full-auto: manual
score events currently give the rally detector (a) the rally COUNT,
(b) every rally END for free, and (c) a bounded search window per gap
that shields it from off-window noise (adjacent tables, ball
fetching). Unanchored mode loses all three — only the motion SIGNAL
itself (ROI-masked energy + adaptive threshold, proven at full-video
length) carries over. That is precisely what Phase 0 step 2 measures
first.

If unanchored segmentation misses the bar, the fallback is NOT
abandoning automation — it is stepping one rung up an anchor-cost
ladder:

| Anchor level | Operator cost | What the solver gains |
|---|---|---|
| Per-point (today's manual) | ~70-100 taps/match | Everything (this IS current Auto Trim) |
| **Per-set boundary taps** | ~4-7 taps/match | Per-set point-count bounds (11-x or deuce), resync every set — errors cannot accumulate across sets |
| Per-match (filename `_1-3`) | 0 taps | Set count + whole-match point-total constraints |
| None | 0 | Score grammar only |

Per-set taps are ~20x cheaper than manual scoring and restore most of
the resync value. Phase 0 should report segmentation + solver metrics
at BOTH the "filename-only" and "per-set taps" rungs so the Phase 1
product decision is data-driven.

### 4.2 VLM shortlist for the bake-off (web-refreshed 2026-07-07)

Operator constraint: **accuracy first — minutes-per-match inference is
acceptable**. That unlocks ensembles and partial-offload of models
slightly over 16 GB VRAM. Candidates for Phase 0 step 3, in test
order:

1. **Qwen3.5-9B** (Mar 2026, Apache 2.0) — natively multimodal
   (early-fusion, no separate vision adapter), reported to outperform
   Qwen3-VL on visual understanding; on Ollama (`qwen3.5`). Primary
   candidate.
2. **MiniCPM-V 4.5 (8B)** (Apache 2.0) — high-FPS video (up to
   10 fps) via 96x video-token compression; strong on MotionBench —
   motion understanding is exactly our failure surface (fast rally
   endings). int4/AWQ builds fit 16 GB easily.
3. **Qwen3-VL-8B** — strong published video-benchmark line
   (Video-MME, MVBench, Charades-STA temporal grounding); attempt #1
   already downloaded 4B+8B; Ollama `qwen3-vl`; mature Unsloth LoRA
   fine-tune path (freeze ViT, LoRA on decoder — QLoRA 8B fits our
   GPU).
4. **Qwen3-VL-32B @ Q4 with partial CPU offload** (~19 GB) — slow
   (fine per constraint) accuracy-ceiling probe: tells us how much
   headroom bigger models offer before we invest in fine-tuning
   small ones.
5. (Optional) Gemma 4 12B multimodal / InternVL3.5 — only if 1-4
   cluster below the bar. Gemma 4 checked 2026-07-07: natively
   multimodal (12B is encoder-free, E2B/E4B/12B take video), BUT
   Google publishes NO video benchmarks for it (only image/audio/
   reasoning), its video path samples ~1 fps with a ~60 s cap —
   too coarse for sub-second rally endings — and its headline
   differentiator (native audio) is useless here since audio is
   ruled out. Stays optional; do not promote above the shortlist.

Bake-off discipline: same N=200 truth-cut clips, same prompt set,
same metric for every model; self-consistency (k=3-5 samples,
majority vote) measured as a separate row since latency is not a
constraint. Fine-tuning note: video-native fine-tune tooling is
still immature — format training samples as multi-image frame
sequences (ShareGPT-style), which Unsloth supports today.

## 5. Architecture

### 5.1 Backend — new package, zero shared mutation

```
backend/auto_score/            NEW package (mirrors auto-trim layering)
  __init__.py
  rally_segmenter.py           UNANCHORED rally segmentation: reuse
                               rally_detector's motion signal (NVDEC
                               decode + ROI motion + smoothing) but
                               detect rally-end boundaries WITHOUT
                               score-event anchors. rally_detector.py
                               itself is NOT modified; shared pieces
                               imported, new gap logic lives here.
  winner_vlm.py                Frame sampling (ROI crop) + local VLM
                               client (Ollama HTTP) + structured-output
                               prompt + response validation.
  serve_side.py                Detect which side serves each rally
                               (player position/stillness in the first
                               ~2 s) — cheap, feeds the solver.
  solver.py                    Score-grammar Viterbi over rally-end
                               sequence + per-rally observations
                               (winner votes, serve side, pauses) →
                               most-likely score_events + per-event
                               confidence + conflict flags. Owns the
                               per-set near/far ↔ P1/P2 side mapping.
                               Pure logic, fully unit-testable.
  debug_bundle.py              Per-rally evidence archive (frames sent,
                               raw model output, transcript, decision)
                               → temp/auto_score/<job_id>/rally_NNN/.

backend/server/routes_auto_score.py   NEW router: start / SSE events /
                               cancel / job status + review-decision
                               endpoint. Job registry mirrors auto-trim
                               (own dict + lock in state.py, pruned by
                               the same prune_finished_jobs).
```

`ScoreEvent` gains `source: Literal["manual", "auto"] = "manual"` —
the exact pattern `TrimSegment.source` proved: legacy JSONs default to
manual, re-runs can drop only `source=="auto"` events, manual events
are never touched by the auto path.

### 5.2 Frontend — tabs, Auto Trim-style modal flow

- **Live Score panel splits into 2 tabs: `Manual` | `Auto`.** Manual
  tab = the existing panel DOM moved verbatim (same element ids, so
  score.js keeps working unchanged). Tab switching is pure CSS
  show/hide — zero logic change to the manual path.
- **Auto tab** hosts the staged flow (initially mostly disabled):
  1. ROI status (reuses `project.info.roi_quadrilateral`; button opens
     the existing Auto Trim modal to confirm if absent).
  2. "Detect rallies" → SSE progress (mirrors detection.js) → proposed
     rally-end markers on the timeline.
  3. "Detect winners" → per-rally winner + confidence list.
  4. **Review list**: each proposed event shows time, predicted winner,
     confidence color, evidence popover (frames/transcript), ✓/✗/swap
     buttons; jump-to-timestamp on click. Apply → writes
     `score_events` with `source:"auto"` + recompute (reuses the
     existing recomputeAllEvents — derived cache stays consistent).
- New module dir `frontend/auto_score/` (mirrors `frontend/auto_trim/`).

#### 5.2.1 Concrete module + interaction design (2026-07-07)

```
frontend/auto_score/
  index.js      Entry — wires the tab buttons + keyboard map; importing
                arms the module (repo convention). No logic beyond wiring.
  state.js      els refs + session object: {jobId, proposals[],
                reviewCursor, sse} + PROPOSAL_SCHEMA constants.
                A proposal = {id, t_end, side_raw, who_resolved, conf,
                serve_side, flags[], status: pending|accepted|edited|
                deleted, locked}.
  api.js        startJob(kind) / attachSse (named handlers, same shape
                as auto_trim/detection.js post-Phase-5) / applyEvents /
                postCorrection / fetchEvidence.
  review.js     renderReviewList — ONE innerHTML string + ONE delegated
                click listener on the container (Phase-4 convention;
                72 rows re-render in <1 ms). Row actions + cursor moves.
  evidence.js   Lazy popover: fetches the debug-bundle frames for one
                rally (temp/auto_score/<job>/rally_NNN/) on first open.
```

Tab split in index.html — existing panel DOM moves verbatim inside
`#score-tab-manual` (ids unchanged → score.js untouched);
`#score-tab-auto` is a sibling, `hidden` class toggled by the tab
buttons. No JS in the manual path changes.

Review list layout (the product IS this list):

```
 #   time      winner  conf   serve  flag         actions
 12  04:31.2   P1 ███  0.93   P2     —            ✓  ⇄  ✗
 13  04:58.7   P2 █    0.41   P1     ⚠ grammar    ✓  ⇄  ✗
 ...
 58/72 reviewed · 7 flagged      [ Apply 72 events ]  [ Discard ]
```

- Row click → `player.seekTo(p.t_end - 3)` + play (imports from
  player.js like score.js already does); the operator watches 2-3 s
  and verdicts.
- **Keyboard-first review** (this is where 5-8 min/match comes from):
  `1`/`2` = winner P1/P2, `Space` = accept + advance, `S` = swap,
  `X` = delete row, `F` = jump to next flagged, `↑/↓` = move cursor.
  Wired as ONE keydown listener active only while the Auto tab is
  visible and focus is not in an input.
- Confidence color: green ≥0.85, yellow 0.6-0.85, red <0.6 + all
  grammar-conflict rows forced red with a ⚠ tag.
- **Staging semantics**: proposals live ONLY in
  `project.auto_score_draft` (new optional ProjectData field — saved
  with the project, so a review session survives save/load/reload).
  Nothing touches `project.score_events` until **Apply**, which
  `snapshot()`s (undo-able), writes events with `source:"auto"` +
  raw side observation (§5.5), calls `recomputeAllEvents` +
  `syncAllUI` via the registered-callback pattern (no import cycles,
  same as project_io.js/render.js).
- Corrections after winners exist (✓/⇄/✗) call `postCorrection` →
  backend logs to auto_score_groundtruth + re-solves with locked
  constraints (§5.5) → response `{changedIds, clearedFlags}` →
  changed rows return to `pending` + toast "1 edit resolved N flags".
- SSE progress reuses the Auto Trim visual language: stage line +
  progress bar + scrolling log, named handlers + `attachSse`.

### 5.3 Model runtime

- **Ollama** as the local model server (Windows service, localhost
  HTTP, easy model management + quantization). Backend calls
  `http://127.0.0.1:11434/api/chat` — no Python GPU deps added to the
  venv for the VLM path.
- Trained-classifier path reuses the ultralytics/torch stack already
  in the venv where possible (a YOLO-cls or small torch model), so no
  new heavyweight deps for Phase 0.
- Config keys (all optional, feature dark when absent):
  `auto_score_ollama_url`, `auto_score_vlm_model`,
  `auto_score_enabled`.
- VRAM budget: VLM ~6-7 GB + NVDEC decode is fine; **never run
  auto-score inference during a render** (NVENC + VLM contention) —
  job registry check refuses to start one while the other runs.
- ⚠ New dependencies (Ollama as external tool; torch extras if the
  classifier path needs them) require operator approval per repo
  rules — approval is part of Phase 0 sign-off.

### 5.4 Dataset feedback loop (mirrors roi_groundtruth)

Every operator review decision (✓/✗/swap) in the Auto tab appends to
`dataset/auto_score_groundtruth/<video_id>.json`: rally window, model
prediction + confidence + evidence hash, operator verdict. This gives
drift measurement + training data for the Phase 2+ classifier for free,
exactly like ROI confirms did.

### 5.5 Correction → recalculation semantics (operator requirement 2026-07-07)

When the operator fixes ONE wrong winner mid-set, everything after it
must recalculate. Three layers, only the last one is new:

1. **Score-state recompute — already exists.** The manual path's core
   invariant (events are ACTIONS; p1/p2/set are a derived cache
   replayed chronologically by `recomputeAllEvents`) means flipping
   one event's `who` + recompute updates every subsequent score,
   set boundary, and the scoreboard preview. The Auto tab writes
   ordinary `score_events` (+`source:"auto"`), so this works day 1
   with zero new code.

2. **Raw observations must be stored, not just resolved winners.**
   Vision observes NEAR/FAR side; the resolved P1/P2 depends on the
   per-set side mapping (§3.1). A correction can move a derived set
   boundary → the side mapping for every later set flips → storing
   only resolved P1/P2 would silently mis-interpret every later auto
   event. Therefore each auto event carries its raw observation
   (`{side: near|far, confidence, evidence_ref}`) alongside the
   resolved `who`, and corrections trigger a re-resolve of raw →
   P1/P2 for all events after the edit.

3. **Re-solve with the correction as a hard constraint.** An
   operator-corrected event becomes LOCKED. The solver re-runs over
   the remaining unlocked events with the new constraint — one
   correction often lets the grammar disambiguate OTHER
   low-confidence points automatically (fixing 1 point can clear 2-3
   flags, or reveal a new inconsistency elsewhere). The review UI
   must show a downstream diff after each correction ("this edit
   changed N later events / cleared M flags") — never silently
   mutate rows the operator already looked at; changed rows return
   to the review queue.

Every correction also appends to `auto_score_groundtruth` (§5.4) —
operator fixes are the highest-value training labels the flywheel
collects.

## 6. Phased roadmap (each phase independently shippable)

### Phase 0 — Offline feasibility spike (no GUI, no production risk)

Scripts only, under `scripts/auto_score_spike/`:

1. ✅ **Corpus builder** (DONE 2026-07-07):
   `scripts/auto_score_spike/build_corpus.py` →
   `dataset/auto_score_corpus/corpus.jsonl`. 622 records = 551
   unique manual events + 71 attempt-1 rallies; dedup by source
   video (9 slugs → 7 unique matches); held-out pinned (1 singles +
   1 doubles, 173 records); index-only v1 — frames extracted on
   demand downstream instead of pre-cutting ~600 clips. Idempotent;
   `--check` verifies all video paths.
2. **Unanchored rally segmentation eval**: run the motion signal
   without anchors on all 9 matches; measure rally-end
   precision/recall + timestamp error vs labeled events. (Anchored
   recall was 95-99%; unanchored WILL be worse — quantify it. See
   §4.1: the anchors currently supply rally count, rally ends, AND
   bounded search windows — none of those exist here.) Primary
   start-timestamp metric: the 34-point truth in
   `dataset/attempt1/reviewed_matches/match_2_sets_debug_001/`.
3. **VLM bake-off**: Ollama + Qwen2.5-VL-7B on N=200 sampled clips
   (ROI-cropped frames at 2K, downscaled as needed); measure winner
   accuracy, latency/clip, failure taxonomy. Try 2-3 prompt variants
   (incl. one focused on post-rally behaviour: who fetches the ball /
   walks away); log everything.
4. **Trained-classifier spike**: fine-tune a small video/frame
   classifier on 7 matches, test on 2 held-out — same corpus, same
   metric. Run IN PARALLEL with the VLM bake-off, not after.
5. **Serve-side detector spike**: position/stillness heuristic on the
   first ~2 s of each rally; measure serve-side accuracy vs labels
   (derivable: serve order is deterministic from the score sequence).
6. **Solver simulation**: given real rally timestamps + noisy winner
   observations at the accuracies measured in 3-5 (+ serve-side
   votes), how often does the grammar solver reconstruct a perfect
   match / how many points need human review? Simulate at the
   attempt-#1 prior (~67% raw) as the pessimistic case — tells us
   the raw accuracy the models must hit and whether the solver can
   bridge the gap. Report at both anchor rungs from §4.1
   (filename-only vs per-set boundary taps) so Phase 1 can choose
   the cheapest rung that clears the bar.

### 6.1 Per-step feasibility + debug protocol (added 2026-07-07)

**Decoupling rule (answers "rally sai → winner sai?"):** steps 3-5
evaluate winner/serve signals on TRUTH-cut clips (labels from the
corpus, not from our segmenter). Winner accuracy is therefore
measured independently of segmentation quality. The two error
sources only compound when the pipeline is chained (Phase 3+), and
each phase gate blocks promotion until the layer below it holds.
Attempt #1 never did this — its winner stage was starved because
everything ran as one waterfall behind a broken segmenter.

Success bars for unanchored segmentation (semi-auto product needs,
NOT attempt #1's 0.75s-start + LET bar): rally-end recall ≥90%,
precision ≥80%, median |Δt| ≤2 s. A missed rally costs the operator
one "add row" click in review; a false positive costs one "delete
row" click. Report the stricter start-±0.75s number vs the 34-point
truth for continuity with attempt #1, but do not gate on it.

| # | Step | Feasibility (honest) | If it fails → debug |
|---|---|---|---|
| 1 | Corpus builder | 95% — pure plumbing over existing data | Count mismatches vs manifest; blank-line/stale-path tolerance (README_ATTEMPT1). |
| 2 | Unanchored segmentation | 40-50% out-of-box to hit the semi-auto bar; ~70-75% after the improvement ladder below | Signal-trace plot + per-error clips (see ladder). Terminal fallback: per-set anchor taps (§4.1). |
| 3 | VLM bake-off | ~40% chance of ≥80% winner accuracy with the 2026-gen shortlist (§4.2) + self-consistency voting; the 67% attempt-#1 prior was one generation older (Qwen3-VL-4B) with a 9-sample eval | Failure taxonomy per rally-ending class; prompt variants (post-rally behaviour focus); frame-sampling sweep (K, fps, crop margin); escalate hard clips to the 32B ceiling probe. |
| 4 | Trained classifier | ~50-60% chance of ≥80% (720 clips is thin for video models; grows per render) | Confusion matrix by taxonomy class; frame-pair vs clip input; augmentation (h-flip swaps labels — attempt #1's splits already model this). |
| 5 | Serve-side detector | ~75% chance of ≥85-90% | Visualize first-2s player positions per error; it's a position/stillness heuristic — tune on plots, not vibes. |
| 6 | Solver simulation | ~95% it produces the answer; ~60% the answer says "viable at measured accuracies" | If solver can't bridge: raise anchor rung (§4.1) and re-simulate before touching models. |

Product-level feasibility (multiply the chain, be honest):

- **Phase 1 semi-auto** (proposals + operator clicks winner): **~85%**.
  Only needs segmentation at the semi-auto bar; ships real value
  (no more full-match scrubbing) even if winner detection dies.
- **Phase 2-3 review-assisted** (≥90% post-solver, ≤20 flags/match):
  **~55-65%** — hinges on step 3 OR 4 reaching ~75-80% raw plus
  serve-side; the solver simulation tells us before we build GUI.
- **Phase 4 unreviewed full-auto** (≥99% per point): **~15-25% at
  the current 9-match corpus; ~35-45% at a 30-40-match corpus**
  (operator has committed to feeding matches — see §3.2 data
  budget). Full-auto is NOT a separate feature to promise — it is
  the LIMIT of the review dial: as the corpus grows, the number of
  flagged points per match shrinks (72 clicks → ~20 → ~5 → ~0).
  "Phase 4 shipped" simply means the flag count hit zero on 5
  consecutive unseen matches (Gate G4). The reason it is never
  promised outright: one silently-wrong point burns a wrong
  scoreboard into a delivered video, and each new match is a new
  venue/angle (domain shift is per-match by the operator's own
  recording setup) — so the last percent is the expensive one.
  Operator time per match along the dial: ~30-60 min today (scrub +
  find + 72 clicks) → ~5-8 min Phase 1 (auto-seek, 72 quick clicks,
  zero scrubbing) → ~2-3 min Phase 2-3 (review ~15-20 flags) →
  ~1 min glance at Phase 4 quality.

### 6.2 Rally segmentation improvement ladder (attempt #1's fatal blocker)

Attempt #1's rally detection failed for three specific reasons, each
addressed structurally here: (a) signal — global pose wrist-velocity
is noisy and not duration-invariant; ours is ROI-masked motion energy
with a per-match percentile threshold (ROI mask kills adjacent-table
noise, percentile kills duration sensitivity); (b) target — they
needed start ±0.75 s + LET classification, row-perfect, because a
rules engine consumed it raw; we need boundaries loose enough for a
review UI and a noise-tolerant solver; (c) method — v1→v11 rule
patches with no fixed metric; we fix the truth set and measure every
change.

**Instrumentation FIRST, before any tuning** (this is the debug
methodology when detection is wrong):

- **Signal-trace plot**: one PNG per match — motion[] timeline,
  threshold line(s), truth boundaries (green), detected boundaries
  (red). One glance answers WHICH failure mode: missed rally (signal
  under threshold → why?), split rally (mid-rally dip), phantom rally
  (what moved?), offset boundary.
- **Per-error clip export**: every FP/FN auto-cuts a ±5 s clip →
  eyeball the actual motion. Errors become watchable, not abstract.
- Both artifacts land in `scripts/auto_score_spike/out/` per run,
  named by config hash — every tuning iteration is comparable.

**The ladder** — apply one rung at a time, re-measure against the
same truth, keep only rungs that move the number:

1. **Threshold shape**: percentile sweep (p50-p90) + hysteresis
   (high threshold to ENTER rally, low to EXIT) — rally motion is
   bursty; single-threshold splits rallies at dips.
2. **Duration priors**: min rally ≥1.5 s, min gap ≥2 s, merge
   fragments; cheap and kills most splits/phantoms.
3. **Near/far half-ROI alternation**: split the table ROI at the net
   line into two halves; a real rally ALTERNATES motion between
   halves at ball-exchange rhythm — ball fetching, towel breaks,
   umpire motion are one-sided. This ping-pong signature is unique
   to the sport and costs one extra mask.
4. **Periodicity score**: sliding-window autocorrelation of the
   motion signal — rallies oscillate (~0.5-1.5 Hz crossings), dead
   time is aperiodic. Feature, not replacement.
5. **Player-band motion**: extend a band above the table ROI to
   capture torsos — both players in ready stance vs walking away.
6. **Pose features LAST, as one feature among many** — attempt #1's
   mistake was pose as the sole signal on day 1. If rungs 1-5
   plateau, wrist/stance features enter the same measured framework.

Fallback below the ladder: per-set anchor taps (§4.1) — restores
resync at ~4-7 operator clicks per match.

**Gate G0** (decided with operator): best vision combination projects
≥90% per-point AFTER solver on the held-out matches, with a reviewable
confidence signal (low-confidence points flagged ≤20/match). Otherwise:
stop, document findings, ship Phase 1 semi-auto only.

### Phase 1 — Tabs + semi-auto (rally timestamps only)

- GUI tab split (Manual untouched) + `ScoreEvent.source`.
- Backend rally segmentation job (SSE) → proposed event timestamps in
  the Auto tab review list, winner UNSET; operator clicks P1/P2 per
  row (video auto-seeks to each rally end).
- Value shipped: operator stops scrubbing — review 80 proposals ≈
  minutes instead of watching the whole match. This alone is a win
  even if winner detection never ships.

### Phase 2 — Winner suggestions + review UI

- Wire the Phase 0 winning signal(s) (audio and/or VLM) as suggestion
  providers; confidence-colored review list; evidence popover;
  corrections logged to `auto_score_groundtruth`.
- Operator reviews only low-confidence rows; high-confidence rows
  pre-accepted visually but still one-click reversible before Apply.

### Phase 3 — Constraint solver + confidence calibration

- `solver.py` Viterbi over the full match; conflicts (impossible score
  transitions) flagged red; set boundaries + filename final score used
  as anchors. Auto-accept threshold tuned on the corpus.

### Phase 4 — Full auto + hardening

- One-click "Auto score" (segment → winners → solve → review).
  Gate G4 to make it the default flow: ≥99% per-point post-solver
  on ≥5 truly-unseen matches (same bar philosophy as the ROI gate).

## 7. Debug methodology (built-in from day 1)

- **Evidence bundles**: every inference writes its exact inputs
  (frames, audio window, prompt) + raw output to
  `temp/auto_score/<job>/rally_NNN/` — failed predictions are
  replayable offline without re-decoding the video.
- **Eval harness is the regression suite**: `scripts/eval_auto_score.py`
  reruns any provider against the corpus and prints per-match
  confusion + accuracy delta vs last run (the ROI LOO pattern).
- **SSE log panel** in the Auto tab mirrors Auto Trim's live log.
- **Every operator correction is a labeled example** (5.4) — accuracy
  drift measurable from production use alone.
- Pure-logic units (solver, score-call parser, segmentation gap logic)
  get pytest coverage from the start — they're deterministic.

## 8. Risks & fallbacks

| Risk | Mitigation |
|---|---|
| Vision winner accuracy too low (<80% raw) | Solver correction + serve-side votes lift effective accuracy; trained classifier co-developed with VLM (not after); worst case stop at Phase 1 (semi-auto), which is already useful on its own. |
| Zero-shot VLM judges rallies poorly | Prompt for post-rally behaviour (ball fetch, walk-away) rather than rally physics; classifier path is the hedge. |
| Unanchored segmentation misses/splits rallies | Review UI tolerates extra/missing proposals (add/delete row); solver tolerates spurious boundaries. |
| Per-set side switching confuses P1/P2 mapping | Solver owns near/far ↔ P1/P2 as per-set state; side-switch doubles as set-boundary evidence; one-click operator confirmation of the initial side in the Auto tab. |
| VRAM contention with renders | Mutual exclusion between render jobs and auto-score jobs. |
| Ollama dependency weight | Optional at runtime — feature hidden unless configured; venv untouched for the VLM path (HTTP). |
| Scope creep into manual path | Hard rule: manual tab DOM/logic untouched except the tab wrapper; `source` field additive with default. |

## 9. Open questions for the operator

Answered 2026-07-07:
- ~~Audio score call-outs?~~ **No — large multi-table halls, audio
  unusable.** Vision-only strategy locked (see §4 banner).
- Camera: fixed tripod per match, diagonal from behind one player,
  2K, angle varies per match (§3.1).

Still open (answer before/at the Phase 0 gate):
1. Review tolerance: is "check ~15-20 low-confidence points per match"
   acceptable as the Phase 2 steady state?
2. Approve Ollama as an external local-model server (one-time install)?
3. Which 2 matches should be held out as the untouched test set
   (never used for prompt/threshold tuning)?
4. In your convention, is P1 (key A) always the player the camera is
   behind at match start, or does it vary per project? (Decides the
   default for the initial side-mapping click.)
