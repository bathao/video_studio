"""Public entry points: `detect_roi` (single frame) + `detect_roi_multiframe`.

Coordinates the tier pipeline:

  -1. YOLOv8-seg (Phase B, learned).
   0. Foreground table by colour contrast (Phase A.5, universal).
   1. ORB keypoint match + homography transfer (Phase A).
   2. HSV color-histogram nearest-neighbour (Phase 1a fallback).
   3. Naive color-based fallback.

`detect_roi` runs YOLO + ORB + colour-contrast in parallel and
cross-validates them; agreement promotes to a stronger tier tag.
`detect_roi_multiframe` aggregates per-frame results via tier-priority
gates + cross-tier IoU clustering.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .quad import RoiDetection, _default_roi, _quad_iou
from .yolo_tier import _try_yolo_seg
from .color_contrast_tier import _try_foreground_by_color_contrast
from .orb_tier import _try_orb_match
from .learned_nn_tier import _try_learned_nn
from .naive_tier import _naive_detect


# When ORB and color-contrast both succeed, IoU threshold to consider
# them "agreeing" — agreement → prefer ORB's pixel-accurate corners,
# disagreement → prefer the one with higher confidence.
_AGREEMENT_IOU = 0.45


def detect_roi(refframe_path: Path | str, exclude_video_id: str | None = None) -> RoiDetection:
    """Detect the operator's table in a single refframe.

    `exclude_video_id` lets the caller skip a specific groundtruth entry
    during NN lookup (useful for "what would the algorithm have proposed
    BEFORE the operator confirmed this video?" introspection — required
    for honest leave-one-out validation).
    """
    img = cv2.imread(str(refframe_path))
    if img is None:
        return _default_roi(reason="cannot read image")

    # Run YOLO + ORB + color-contrast in parallel so we can cross-validate.
    # Pre-retrain analysis showed YOLO at high confidence (~0.99) was still
    # picking the WRONG table in ~12% of multi-table scenes — confidence
    # tracks segmentation quality, not table identity. Classical-CV tiers
    # are an independent vote on which table is the operator's.
    yolo_result = _try_yolo_seg(img)
    color_result, color_debug = _try_foreground_by_color_contrast(img)
    orb_result, orb_debug = _try_orb_match(img, exclude_video_id)

    if yolo_result is not None:
        agrees_with_orb = (
            orb_result is not None
            and _quad_iou(yolo_result.corners, orb_result.corners) >= _AGREEMENT_IOU
        )
        agrees_with_color = (
            color_result is not None
            and _quad_iou(yolo_result.corners, color_result.corners) >= _AGREEMENT_IOU
        )

        if agrees_with_orb or agrees_with_color:
            # Agreement = independent confirmation YOLO picked the right
            # table. Promote to a distinct tier so multiframe priority
            # gates can rank "YOLO + at least one classical vote" above
            # bare YOLO.
            yolo_result.debug["orb_agrees"] = agrees_with_orb
            yolo_result.debug["color_agrees"] = agrees_with_color
            if agrees_with_orb and agrees_with_color:
                yolo_result.method = "yolo_seg+both_agree"
            elif agrees_with_orb:
                yolo_result.method = "yolo_seg+orb_agree"
                yolo_result.debug["orb_proposal"] = orb_result.corners
            else:
                yolo_result.method = "yolo_seg+color_agree"
                yolo_result.debug["color_proposal"] = color_result.corners
            yolo_result.confidence = float(min(1.0, yolo_result.confidence + 0.05))
            return yolo_result

        # No agreement with any classical signal. Investigate the
        # disagreement before trusting YOLO outright.
        if orb_result is not None and orb_result.confidence >= 0.80:
            # Strong same-arena ORB match disagrees with YOLO →
            # operator-confirmed corner placement from the matching
            # entry beats a confident-but-table-confused YOLO.
            iou = _quad_iou(yolo_result.corners, orb_result.corners)
            orb_result.debug["yolo_rejected"] = True
            orb_result.debug["yolo_proposal"] = yolo_result.corners
            orb_result.debug["yolo_confidence"] = round(float(yolo_result.confidence), 3)
            orb_result.debug["disagreement_iou"] = round(iou, 3)
            orb_result.method = f"orb_homography_vs_yolo:{orb_result.method.split(':', 1)[-1]}"
            return orb_result

        if (
            color_result is not None
            and color_result.debug.get("red_surround_frac", 0.0) >= 0.50
        ):
            # Strong red-floor surround → the foreground table is
            # universally identifiable from physics alone. YOLO must
            # have locked onto a different blue table.
            iou = _quad_iou(yolo_result.corners, color_result.corners)
            color_result.debug["yolo_rejected"] = True
            color_result.debug["yolo_proposal"] = yolo_result.corners
            color_result.debug["yolo_confidence"] = round(float(yolo_result.confidence), 3)
            color_result.debug["disagreement_iou"] = round(iou, 3)
            color_result.method = "color_contrast_vs_yolo"
            return color_result

        # No competing high-confidence classical signal — keep YOLO.
        # Novel arena without strong red surround AND no similar-POV
        # past example → YOLO is the only thing we've got.
        yolo_result.debug["cross_check"] = "no_classical_competitor"
        return yolo_result

    if color_result is not None and orb_result is not None:
        iou = _quad_iou(color_result.corners, orb_result.corners)
        if iou >= _AGREEMENT_IOU:
            # Both detectors agree on the same region. Prefer ORB —
            # corner placement is sub-pixel accurate via homography.
            orb_result.debug["color_contrast_agreement_iou"] = round(iou, 3)
            orb_result.debug["color_contrast_proposal"] = color_result.corners
            orb_result.method = f"color_contrast+orb_agree:{orb_result.method.split(':', 1)[-1]}"
            return orb_result
        # Disagreement is the failure mode we want to catch: ORB found
        # a "similar" past example but the camera POV / arena layout
        # has shifted enough that the transferred corners land on the
        # wrong region (background tables, walls). Color-contrast keys
        # off the universal "foreground table on red floor" signature.
        #
        # If color-contrast has STRONG red surround (≥ 0.40), it's
        # almost certainly the real foreground table — trust it over
        # ORB regardless of ORB's inlier count. Otherwise the contest
        # is closer, fall back to whichever has higher confidence.
        red_surround = color_result.debug.get("red_surround_frac", 0.0)
        if red_surround >= 0.40:
            color_result.debug["orb_disagreed_iou"] = round(iou, 3)
            color_result.debug["orb_rejected_proposal"] = orb_result.corners
            color_result.debug["orb_rejected_method"] = orb_result.method
            color_result.debug["disagreement_rule"] = "color_strong_red_surround"
            return color_result
        if orb_result.confidence >= color_result.confidence:
            orb_result.debug["color_contrast_disagreed_iou"] = round(iou, 3)
            orb_result.debug["color_contrast_rejected_proposal"] = color_result.corners
            orb_result.debug["disagreement_rule"] = "orb_higher_confidence"
            return orb_result
        color_result.debug["orb_disagreed_iou"] = round(iou, 3)
        color_result.debug["orb_rejected_proposal"] = orb_result.corners
        color_result.debug["orb_rejected_method"] = orb_result.method
        color_result.debug["disagreement_rule"] = "color_higher_confidence"
        return color_result

    if color_result is not None:
        color_result.debug["orb_attempt"] = orb_debug
        return color_result
    if orb_result is not None:
        orb_result.debug["color_contrast_attempt"] = color_debug
        return orb_result

    # Stage 2: HSV histogram NN/blend/mean (fallback when neither
    # top-tier succeeded — e.g. no red floor AND no similar past arena).
    learned, nn_debug = _try_learned_nn(img, exclude_video_id)
    if learned is not None:
        learned.debug["orb_attempt"] = orb_debug
        learned.debug["color_contrast_attempt"] = color_debug
        return learned

    # Stage 3: naive color-based (no groundtruth at all)
    naive = _naive_detect(img)
    naive.debug["orb_attempt"] = orb_debug
    naive.debug["color_contrast_attempt"] = color_debug
    if nn_debug:
        naive.debug.update(nn_debug)
    return naive


# IoU threshold for two per-frame results to be considered "the same ROI".
# 0.50 = corners line up well enough that the difference is just sub-blob
# noise (player at a slightly different spot, ball in flight, etc.). Lower
# would over-cluster (different tables called the same); higher would
# under-cluster (good results split into singletons).
_MULTIFRAME_CLUSTER_IOU = 0.50


def detect_roi_multiframe(
    refframe_paths: list,
    exclude_video_id: str | None = None,
) -> RoiDetection:
    """Run detect_roi on each of N refframes and aggregate via
    cluster+median, robust to per-frame failures from player occlusion.

    Pipeline:
      1. Run single-frame detect on every refframe.
      2. Drop frames that returned "default" (detection fully failed).
      3. Pairwise IoU across surviving results; union-find cluster by
         IoU ≥ _MULTIFRAME_CLUSTER_IOU. Same-region detections collapse
         to one cluster regardless of which tier they came from.
      4. Pick the largest cluster (ties broken by total confidence).
         This embodies "majority vote across frames" — player occlusion
         in 1-2 frames doesn't sway the result as long as the rest
         agree on the same table.
      5. Per-corner MEDIAN within the winning cluster. Median (vs mean)
         is robust to the one frame in the cluster that's slightly off
         due to a ball flying through the contour.

    Confidence: mean of cluster member confidences, plus a small bonus
    for cluster size (5/5 agreement is much stronger than 2/5).

    Falls back to the single highest-confidence frame when no cluster
    of size ≥ 2 exists (all frames disagree — typical for venues outside
    the dataset coverage where each frame's noise dominates).
    """
    if not refframe_paths:
        return _default_roi(reason="no frames provided")

    per_frame: list[dict] = []
    for i, p in enumerate(refframe_paths):
        det = detect_roi(p, exclude_video_id=exclude_video_id)
        if det.method == "default":
            continue
        per_frame.append({"frame_idx": i, "det": det})

    if not per_frame:
        return _default_roi(reason="all frames failed detection")

    n = len(per_frame)
    if n == 1:
        det = per_frame[0]["det"]
        det.debug["multiframe"] = {
            "n_frames": len(refframe_paths),
            "n_valid": 1,
            "note": "only one frame yielded a non-default detection",
        }
        return det

    # Tier-priority aggregation. Naive majority voting fails when
    # learned_nn / learned_blend / learned_mean return CORRELATED
    # answers across frames (they all default to a similar guess based
    # on the dataset, not on the frame's actual content). 3 correlated
    # wrong answers should not out-vote 1 grounded right answer.
    #
    # Tiers in trust order: color_contrast and ORB are derived from the
    # frame's own pixels (universal physics / spatial keypoint match) so
    # they reflect the frame content. learned_* tiers reflect dataset
    # similarity and produce the same prediction regardless of which
    # frame we query — clustering would inflate their vote count.
    #
    # Within a tier, we still IoU-cluster + median to denoise small
    # frame-to-frame variations. Across tiers, we accept the *first*
    # tier (highest priority) that has a usable answer:
    #   - color_contrast / orb / agree: singleton accepted (these tiers
    #     have strong internal validation: red_frac ≥ 0.30, solidity ≥
    #     0.82, ORB sanity checks at 15+ inliers).
    #   - learned_*: require IoU cluster of ≥ 2 frames within that tier,
    #     so a stray default-of-mean answer doesn't dominate.
    # Priority gates: each gate specifies (tier, min_cluster_size). The
    # first gate that finds a same-tier cluster of at least min_size wins.
    # ORB-with-consensus beats color-contrast singleton because spatial
    # keypoint matches across multiple frames are extremely reliable
    # (same arena). Color-contrast singleton beats learned_* because the
    # latter's "votes" are correlated (same dataset queried every frame).
    # Priority order calibrated from labeled-error history (analyze_detector_errors.py):
    # - ORB 5/5 cluster: 0% fail across 8 entries (mean err 0.007) → most reliable
    # - YOLO 5/5 cluster: 12% fail across 8 entries, 1 catastrophic 0.44 → reliable
    #   but confidence-uncorrelated with accuracy
    # - color_contrast singleton: 100% fail when alone → never trust solo
    # - learned_* tiers: mostly fail → require strong consensus or skip
    _PRIORITY_GATES: list[tuple[str, int]] = [
        # YOLO + classical-CV agreement: independent confirmation, strongest signal.
        # Even a single frame counts because it's a cross-tier vote.
        ("yolo_seg+both_agree", 1),
        ("yolo_seg+orb_agree", 1),
        ("yolo_seg+color_agree", 1),
        ("color_contrast+orb_agree", 1),
        # ORB cluster of 3+: same-arena keypoint match across most frames.
        # Empirically 0% fail in groundtruth history.
        ("orb_homography", 3),
        # Disagreement-resolved fallbacks (YOLO was rejected by a strong
        # classical signal in detect_roi). These already passed an internal
        # check, so singleton is acceptable.
        ("orb_homography_vs_yolo", 1),
        ("color_contrast_vs_yolo", 1),
        # YOLO cluster of 2+: multi-frame consensus on the same table. Stops
        # a stray YOLO frame from dominating when other frames disagree.
        ("yolo_seg", 2),
        # ORB cluster of 2+: weaker than 3+ but still grounded in keypoints.
        ("orb_homography", 2),
        # Color-contrast cluster of 2+: universal-physics consensus.
        ("color_contrast_foreground", 2),
        # YOLO singleton: only one frame produced YOLO output, no classical
        # cross-check available → last resort before classical-only tiers.
        # Demoted from rank 1 because confidence does not track accuracy.
        ("yolo_seg", 1),
        # Strong learned_* consensus (4+ frames) — correlated votes are
        # acceptable when the cluster is unanimous.
        ("learned_blend", 4),
        ("learned_nn", 4),
        ("learned_mean", 4),
        # ORB singleton: low evidence but still better than learned_*.
        ("orb_homography", 1),
        # ⚠ color_contrast_foreground singleton (the old rank-7 gate)
        # has been REMOVED — labeled-history shows 100% fail rate when
        # this fires alone. The cluster≥2 gate above keeps the tier
        # available for the cases it actually works.
        ("learned_blend", 2),
        ("learned_nn", 2),
        ("learned_mean", 2),
        ("color_blue", 2),
        ("color_green", 2),
    ]

    def _tier(method: str) -> str:
        return method.split(":", 1)[0]

    by_tier: dict[str, list[dict]] = {}
    for pf in per_frame:
        by_tier.setdefault(_tier(pf["det"].method), []).append(pf)

    def _iou_cluster_indices(items: list[dict]) -> list[list[int]]:
        m = len(items)
        if m == 0:
            return []
        parent_local = list(range(m))
        def _find_local(x: int) -> int:
            while parent_local[x] != x:
                parent_local[x] = parent_local[parent_local[x]]
                x = parent_local[x]
            return x
        for i in range(m):
            for j in range(i + 1, m):
                v = _quad_iou(items[i]["det"].corners, items[j]["det"].corners)
                if v >= _MULTIFRAME_CLUSTER_IOU:
                    ra, rb = _find_local(i), _find_local(j)
                    if ra != rb:
                        parent_local[ra] = rb
        clusters_local: dict[int, list[int]] = {}
        for i in range(m):
            clusters_local.setdefault(_find_local(i), []).append(i)
        return list(clusters_local.values())

    per_frame_debug = [
        {"frame": pf["frame_idx"], "method": pf["det"].method,
         "conf": round(pf["det"].confidence, 3),
         "tier": _tier(pf["det"].method)}
        for pf in per_frame
    ]

    # Cache per-tier best cluster so we don't recluster across gate retries.
    tier_best_cluster: dict[str, list[dict]] = {}
    for tier, items in by_tier.items():
        clusters_local = _iou_cluster_indices(items)
        clusters_local.sort(
            key=lambda c: (len(c), sum(items[i]["det"].confidence for i in c)),
            reverse=True,
        )
        if clusters_local:
            tier_best_cluster[tier] = [items[i] for i in clusters_local[0]]

    # Cross-tier IoU cluster (geometric consensus across tiers). Catches the
    # case where multiple frames agree on the SAME ROI but their tier tags
    # differ (one frame got `+both_agree`, others got `+orb_agree`, etc.).
    # Without this, a single high-priority-tag frame outranks a larger
    # cluster of slightly-different tags pointing at the same correct
    # answer — the DucTu failure mode observed 2026-05-18 khuya: f0 had
    # `+both_agree` but wrong corners; f1/f3/f4 had `+orb_agree` with
    # correct corners; old per-tier priority logic picked the singleton.
    #
    # Excluded tiers: `learned_*` (correlated cross-frame, not independent
    # votes) and `default` (failure marker).
    _CROSS_TIER_TRUSTED = {
        "yolo_seg+both_agree",
        "yolo_seg+orb_agree",
        "yolo_seg+color_agree",
        "yolo_seg",
        "color_contrast+orb_agree",
        "color_contrast_foreground",
        "orb_homography",
        "orb_homography_vs_yolo",
        "color_contrast_vs_yolo",
    }
    cross_tier_frames = [
        pf for pf in per_frame if _tier(pf["det"].method) in _CROSS_TIER_TRUSTED
    ]
    cross_tier_clusters = _iou_cluster_indices(cross_tier_frames)
    cross_tier_clusters.sort(
        key=lambda c: (len(c), sum(cross_tier_frames[i]["det"].confidence for i in c)),
        reverse=True,
    )
    cross_tier_winner: list[dict] = []
    if cross_tier_clusters and len(cross_tier_clusters[0]) >= 2:
        cross_tier_winner = [cross_tier_frames[i] for i in cross_tier_clusters[0]]

    winning_tier: str | None = None
    winning_items: list[dict] = []
    winning_cluster_size_within_tier = 0
    used_cross_tier = False

    # Cross-tier cluster of size N beats any per-tier cluster of size < N.
    # Catches: 4 frames with slightly different tags agreeing geometrically
    # should win over 1 frame with the highest-priority tag.
    cross_size = len(cross_tier_winner)
    best_per_tier_size = max(
        (len(tier_best_cluster[t]) for t in tier_best_cluster),
        default=0,
    )
    if cross_size >= 2 and cross_size > best_per_tier_size:
        # Pick the most trustworthy tier tag among cluster members for the
        # final method label. Priority: agreement tiers > bare YOLO/ORB >
        # disagreement-resolved > fallback.
        _TIER_TRUST_RANK = [
            "yolo_seg+both_agree",
            "color_contrast+orb_agree",
            "yolo_seg+orb_agree",
            "yolo_seg+color_agree",
            "yolo_seg",
            "orb_homography",
            "orb_homography_vs_yolo",
            "color_contrast_vs_yolo",
            "color_contrast_foreground",
        ]
        tiers_in_cluster = {_tier(it["det"].method) for it in cross_tier_winner}
        winning_tier = next(
            (t for t in _TIER_TRUST_RANK if t in tiers_in_cluster),
            next(iter(tiers_in_cluster)),
        )
        winning_items = cross_tier_winner
        winning_cluster_size_within_tier = len(cross_tier_winner)
        used_cross_tier = True
    else:
        for tier, min_size in _PRIORITY_GATES:
            best_local = tier_best_cluster.get(tier)
            if not best_local or len(best_local) < min_size:
                continue
            winning_tier = tier
            winning_items = best_local
            winning_cluster_size_within_tier = len(best_local)
            break

    if winning_tier is None:
        # No tier passed its min-size gate → fall back to single highest-
        # confidence frame across all tiers.
        per_frame.sort(key=lambda x: -x["det"].confidence)
        best_single = per_frame[0]["det"]
        best_single.debug["multiframe"] = {
            "n_frames": len(refframe_paths),
            "n_valid": n,
            "note": "no tier reached its singleton/cluster threshold; using highest-confidence single frame",
            "per_frame": per_frame_debug,
        }
        return best_single

    cluster_dets = [it["det"] for it in winning_items]

    if len(cluster_dets) == 1:
        det = cluster_dets[0]
        det.debug["multiframe"] = {
            "n_frames": len(refframe_paths),
            "n_valid": n,
            "winning_tier": winning_tier,
            "winning_tier_frames": [it["frame_idx"] for it in winning_items],
            "note": f"single high-priority frame from tier '{winning_tier}'",
            "per_frame": per_frame_debug,
        }
        return det

    # Per-corner median across same-tier cluster members.
    median_corners: list[list[float]] = []
    for corner_idx in range(4):
        xs = np.array([d.corners[corner_idx][0] for d in cluster_dets])
        ys = np.array([d.corners[corner_idx][1] for d in cluster_dets])
        median_corners.append([float(np.median(xs)), float(np.median(ys))])

    cross_marker = "*" if used_cross_tier else ""
    method_tag = f"multiframe[{len(cluster_dets)}/{n}]{cross_marker}:{winning_tier}"

    # Confidence: mean of cluster + size bonus (5/5 unanimous = strong).
    size_bonus = min(0.15, (len(cluster_dets) - 1) * 0.04)
    mean_conf = sum(d.confidence for d in cluster_dets) / len(cluster_dets)
    final_conf = float(min(1.0, mean_conf + size_bonus))

    return RoiDetection(
        corners=median_corners,
        confidence=final_conf,
        method=method_tag,
        debug={
            "tier": "multiframe_tier_priority",
            "n_frames": len(refframe_paths),
            "n_valid": n,
            "winning_tier": winning_tier,
            "winning_tier_frames": [it["frame_idx"] for it in winning_items],
            "winning_cluster_size_within_tier": winning_cluster_size_within_tier,
            "per_frame": per_frame_debug,
        },
    )
