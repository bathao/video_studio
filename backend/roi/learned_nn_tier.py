"""Stage 1: HSV color-histogram nearest-neighbour fallback.

Three-tier learned prediction (exact reuse / weighted blend / mean).
Also hosts `_load_groundtruth_examples`, the shared dataset loader
that both this tier and `orb_tier` consume.
"""

from __future__ import annotations

import json
import threading

import cv2
import numpy as np

from .quad import RoiDetection, _GROUNDTRUTH_DIR


# Image size for similarity feature extraction. Small + fixed → fast, and
# matches groundtruth refframes regardless of source resolution.
_FEAT_W = 256
_FEAT_H = 144

# Reuse the confirmed corners directly when histogram similarity >= this.
# Calibrated against actual operator data (10 confirmed ROIs):
#   - same-arena, same-day:        sim 0.87-0.96  → exact reuse safe
#   - same-arena, different match: sim 0.65-0.90
#   - different arena:             sim 0.00-0.50
# 0.80 keeps exact-reuse for very strong matches; borderline matches
# (0.40-0.80) get the top-K weighted blend instead, which smooths over
# per-camera-angle differences that would dominate a single-example copy.
_NN_HIGH_CONF = 0.65
# Below this, ignore individual matches and fall back to mean-of-all.
_NN_MIN_CONSIDER = 0.40
# Top-K examples to consider in the weighted-blend tier. Keeping K small
# gives locality: each prediction uses only the most relevant examples
# rather than diluting them with unrelated venues.
_BLEND_TOP_K = 3


def _compute_image_features(img_bgr: np.ndarray) -> np.ndarray:
    """HSV 3-D color histogram, normalised. Robust to lighting variance,
    insensitive to small framing changes, fast to compute & compare."""
    resized = cv2.resize(img_bgr, (_FEAT_W, _FEAT_H))
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8],
                        [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def _hist_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Histogram correlation in [-1, 1]; clip to [0, 1] for convenience."""
    s = float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))
    return max(0.0, min(1.0, s))


# Process-wide cache of the groundtruth example set. Invalidated by an
# mtime+size signature over the dataset/roi_groundtruth/ directory, so any
# add/remove/edit of a .json or .jpg there is picked up automatically.
#
# Why cache: each detect_roi_multiframe() call invokes detect_roi() 5×,
# and each detect_roi() previously rebuilt this list from disk = 5 × 53 =
# 265 file reads + 265 cv2.imread + 265 HSV histogram computes per Auto Trim
# click. The orb tier additionally re-extracts ORB features on every call
# because its cache lived inside `ex` dicts that were thrown away after
# each load. Persisting the dicts across calls (and keeping the lazy
# `ex["orb_features"]` mutation) collapses all of that to a one-time cost
# at server startup (or first detect call if warmup didn't run).
_examples_cache: list[dict] | None = None
_examples_cache_sig: tuple | None = None
_examples_cache_lock = threading.Lock()


def _compute_groundtruth_signature() -> tuple:
    """Compact fingerprint of dataset/roi_groundtruth/ for cache
    invalidation. Cheap (one stat per file, no reads) so safe to call on
    every detect."""
    if not _GROUNDTRUTH_DIR.exists():
        return ()
    items = []
    for p in sorted(_GROUNDTRUTH_DIR.iterdir()):
        if p.suffix.lower() not in (".json", ".jpg"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        items.append((p.name, int(st.st_mtime_ns), int(st.st_size)))
    return tuple(items)


def _build_examples_from_disk() -> list[dict]:
    """Heavy build path: reads every .json + .jpg pair under the groundtruth
    dir, computes the HSV histogram feature once per example. ORB features
    are NOT computed here (left to orb_tier's lazy path), so this function
    is safe to call from learned_nn_tier without an orb_tier import."""
    if not _GROUNDTRUTH_DIR.exists():
        return []
    out = []
    for json_path in sorted(_GROUNDTRUTH_DIR.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = data.get("video_id") or json_path.stem
        img_path = _GROUNDTRUTH_DIR / f"{vid}.jpg"
        if not img_path.exists():
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        corners = data.get("latest_corners")
        if not (isinstance(corners, list) and len(corners) == 4):
            continue
        out.append({
            "video_id": vid,
            "video_name": data.get("video_name", vid),
            "corners": corners,
            "features": _compute_image_features(img),
        })
    return out


def _load_groundtruth_examples(exclude_video_id: str | None = None) -> list[dict]:
    """Enumerate dataset/roi_groundtruth/*.json + matching *.jpg pairs.

    Process-wide memoized: subsequent calls hit RAM unless the directory
    signature changed (file added/removed/edited via the Auto Trim Confirm
    flow). `exclude_video_id` filters the cached list at return time so
    leave-one-out callers still see a fresh-looking result without
    invalidating the cache for the rest of the session."""
    global _examples_cache, _examples_cache_sig
    sig = _compute_groundtruth_signature()
    with _examples_cache_lock:
        if _examples_cache is None or _examples_cache_sig != sig:
            _examples_cache = _build_examples_from_disk()
            _examples_cache_sig = sig
        cached = _examples_cache
    if exclude_video_id is None:
        return cached
    return [e for e in cached if e.get("video_id") != exclude_video_id]


def _weighted_average_corners(scored: list[dict], weights: list[float], total_w: float) -> list[list[float]]:
    """Per-corner weighted average over all examples. Each example must
    have a TL/TR/BR/BL ordered `corners` list (the order_clockwise_from_tl
    normalisation at confirm time guarantees this)."""
    avg = []
    for i in range(4):
        x = sum(w * ex["corners"][i][0] for w, ex in zip(weights, scored)) / total_w
        y = sum(w * ex["corners"][i][1] for w, ex in zip(weights, scored)) / total_w
        avg.append([float(x), float(y)])
    return avg


def _try_learned_nn(img: np.ndarray, exclude_video_id: str | None) -> tuple[RoiDetection | None, dict]:
    """Three-tier learned prediction:

      1. STRONG match (sim ≥ 0.85): reuse top match's corners exactly.
      2. WEAK match  (sim ≥ 0.40): similarity-weighted average across ALL
         examples — small-sample interpolation that gets sharper as more
         examples accumulate.
      3. NO match    (sim <  0.40): unweighted MEAN of all examples
         (= operator's typical setup) as the proposal. Only used when at
         least 2 examples exist; with 0-1 examples, fall back to naive.

    Returns (detection_if_any, debug_dict_for_naive_fallback).
    """
    examples = _load_groundtruth_examples(exclude_video_id)
    debug = {"n_groundtruth_examples": len(examples)}
    if not examples:
        return None, debug

    query = _compute_image_features(img)
    scored = []
    for ex in examples:
        sim = _hist_similarity(query, ex["features"])
        scored.append({"video_name": ex["video_name"],
                       "video_id": ex["video_id"],
                       "similarity": sim,
                       "corners": ex["corners"]})
    scored.sort(key=lambda x: -x["similarity"])
    best = scored[0]
    debug["nn_top3"] = [{"video": s["video_name"], "sim": round(s["similarity"], 3)}
                       for s in scored[:3]]

    # Tier 1: strong match — exact reuse.
    if best["similarity"] >= _NN_HIGH_CONF:
        return RoiDetection(
            corners=[list(map(float, p)) for p in best["corners"]],
            confidence=float(best["similarity"]),
            method=f"learned_nn:{best['video_name']}",
            debug={
                "matched_video": best["video_name"],
                "matched_video_id": best["video_id"],
                "similarity": best["similarity"],
                "n_groundtruth_examples": len(examples),
                "nn_top3": debug["nn_top3"],
                "tier": "exact",
            },
        ), debug

    # Tier 2: borderline match — top-K similarity-weighted blend.
    # Uses only the most relevant examples (K=3) so the prediction stays
    # local to a cluster instead of averaging across unrelated venues.
    # Tested empirically against alternatives (NN+mean alpha-blend, all-
    # examples weighted blend); top-K won marginally on the operator's
    # 10-ROI groundtruth set (mean error 0.062 vs 0.064-0.065).
    if best["similarity"] >= _NN_MIN_CONSIDER and len(examples) >= 2:
        top_k = scored[:_BLEND_TOP_K]
        weights = [max(0.0, s["similarity"] - 0.2) ** 2 for s in top_k]
        total_w = sum(weights)
        if total_w > 1e-6:
            blended = _weighted_average_corners(top_k, weights, total_w)
            blend_names = "+".join(s["video_name"][:10] for s in top_k if s["similarity"] >= 0.2)
            return RoiDetection(
                corners=blended,
                confidence=float(best["similarity"]),
                method=f"learned_blend:[{blend_names}]",
                debug={
                    "blend_top_sim": best["similarity"],
                    "blend_examples": [{"v": s["video_name"], "sim": round(s["similarity"], 3)} for s in top_k],
                    "n_groundtruth_examples": len(examples),
                    "nn_top3": debug["nn_top3"],
                    "tier": "weighted_blend_topk",
                },
            ), debug

    # Tier 3: no match — simple mean of all examples (operator's "typical" setup).
    # Only when ≥2 examples; otherwise let naive try.
    if len(examples) >= 2:
        weights = [1.0] * len(examples)
        mean_corners = _weighted_average_corners(scored, weights, float(len(examples)))
        return RoiDetection(
            corners=mean_corners,
            confidence=0.25,
            method=f"learned_mean:{len(examples)}_examples",
            debug={
                "n_groundtruth_examples": len(examples),
                "nn_top3": debug["nn_top3"],
                "tier": "mean_fallback",
                "note": "no example matched well; using mean of all confirms as starting point",
            },
        ), debug

    return None, debug
