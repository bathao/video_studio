"""Auto-detect table ROI from a refframe.

Five-stage detector (highest accuracy first):

  -1. **YOLOv8-seg (Phase B, if model trained)** — learned segmentation
      model directly outputs a mask of the foreground table. Lazy-loaded
      from `assets/models/roi_seg.pt`; falls through to classical CV
      tiers if the model file is absent (operator hasn't trained yet).
      When confidence ≥ _YOLO_MIN_CONF and the mask passes shape sanity
      checks, this tier wins outright — ML beats hand-engineered features
      on truly-unseen arenas, which is the whole point of Phase B.

  0. **Foreground table by color contrast (Phase A.5, universal)** —
     Detects the foreground table using physical features that are
     near-universal across the operator's typical recording venues:
     ITTF-standard blue table on red carpet/floor. Distinguishes the
     foreground table from background tables (which are also blue) by
     measuring "red-surroundedness" — the foreground table sits on red
     floor, background tables are surrounded by other tables / walls /
     spectators. Example-independent, so it generalises to never-seen
     venues without needing a dataset entry — provided they share the
     blue-table + red-floor combo.

  1. **ORB keypoint match + homography transfer (Phase A)** —
     ORB features extracted from the query refframe are matched against
     each stored groundtruth refframe via RANSAC. A successful match
     yields a perspective transformation that maps the example's pixel-
     space corners onto the query frame, giving near-pixel-accurate ROI
     transfer when the camera POV is similar (typical case: same tripod
     position across matches within a session). Pixel-accurate when it
     succeeds — but vulnerable to confidently transferring corners from
     the wrong arena (background-table failure mode).

     Stage 0 and Stage 1 run as a HYBRID: when both succeed, we IoU
     their proposed quads. Agreement → prefer ORB (more accurate corner
     placement). Disagreement → prefer color-contrast (universal
     physical features, less likely to confuse arenas).

  2. **HSV color-histogram nearest-neighbour (Phase 1a fallback)** —
     when neither stage 0 nor stage 1 found a strong match (e.g. venue
     without red floor AND no similar past arena), compare the query
     against all examples by HSV histogram correlation. 3-tier
     prediction: exact reuse for very similar histograms, top-K
     weighted blend for borderline, mean-of-all for novel inputs.

  3. **Naive color-based fallback** — when no groundtruth exists at all.
     Restricted to the lower-mid band of the frame, filters by aspect
     ratio and a conservative width cap so it can't latch onto walls or
     background dividers.

Output: 4 normalized [x, y] corners ordered TL → TR → BR → BL, plus a
method tag the modal shows ("color_contrast_foreground",
"orb_homography:<src>", "color_contrast+orb_agree:<src>",
"learned_nn:<src>", "learned_blend:[...]", "learned_mean:<n>",
"color_blue", "default"). Callers do not need to know which path ran.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


# Resolve relative to the repo root (matches the layout used by server.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_GROUNDTRUTH_DIR = _REPO_ROOT / "dataset" / "roi_groundtruth"


@dataclass
class RoiDetection:
    corners: list[list[float]]   # [[x, y], ...] 4 points, normalized 0-1, TL→TR→BR→BL
    confidence: float            # rough quality estimate, 0-1
    method: str                  # see module docstring
    debug: dict = field(default_factory=dict)


# ----- Stage -1: YOLOv8-seg (Phase B, learned segmentation) ---------------

# Mask-area sanity bounds for accepted YOLO predictions. Below the floor
# the model probably matched a partial table or an irrelevant small blob;
# above the ceiling it likely fired on a wall + multiple tables (similar
# to the classical-CV merger failure mode, just from the model side).
_YOLO_MIN_AREA_FRAC = 0.010
_YOLO_MAX_AREA_FRAC = 0.40


def _try_yolo_seg(img: np.ndarray) -> RoiDetection | None:
    """Stage -1: run YOLOv8-seg, return a RoiDetection or None.

    Returns None when the model file isn't installed (Phase B not
    activated), the model produces no high-confidence detection, or
    the predicted mask fails area sanity. Otherwise returns a
    RoiDetection with method="yolo_seg" and confidence from the model's
    own score. Downstream is the top of the priority chain, so a passing
    YOLO result wins outright over classical CV stages.
    """
    try:
        from . import roi_yolo
    except ImportError:
        return None
    result = roi_yolo.try_yolo_seg(img)
    if result is None:
        return None

    area_frac = result.get("selected_area_frac", 0.0)
    if not (_YOLO_MIN_AREA_FRAC <= area_frac <= _YOLO_MAX_AREA_FRAC):
        return None

    return RoiDetection(
        corners=result["corners"],
        confidence=float(result["confidence"]),
        method="yolo_seg",
        debug={
            "tier": "yolo_seg",
            "yolo_confidence": round(float(result["confidence"]), 3),
            "n_detections": result.get("n_detections", 0),
            "selected_area_frac": round(area_frac, 4),
        },
    )


# ----- Stage 0: Foreground table by colour contrast (universal physics) ---

# Table-tennis tabletop blue. ITTF standard is ~"Joola Inside" blue (RGB
# 25,80,150-ish → HSV hue ~110°). Hue band 95-130 covers camera/white-
# balance drift across the operator's venues. Saturation floor 80 weeds
# out grey walls / concrete; value bounds drop pure-black shadows.
_TT_BLUE_LO = (95, 80, 45)
_TT_BLUE_HI = (130, 255, 220)

# Floor/carpet red — split mask because red wraps the hue circle.
_FLOOR_RED_LO_A = (0, 65, 45)
_FLOOR_RED_HI_A = (12, 255, 230)
_FLOOR_RED_LO_B = (168, 65, 45)
_FLOOR_RED_HI_B = (180, 255, 230)

# Pixel radius used to dilate each blue blob to sample its surrounding
# context. ~30 px at 960×540 ≈ 3% of frame width.
_CONTRAST_RING_PX = 30

# Acceptance thresholds — calibrated to reject blue walls, blue skirt
# strips that span multiple tables, and large background-table merges.
# Lowered min area from 0.025 → 0.012: with the ellipse-9×9 open now
# properly disconnecting foreground tables from surrounding mass, the
# isolated table blob itself is small (~1.5% of frame after erosion).
_FG_MIN_AREA_FRAC = 0.012       # < 1.2% of frame → too small
_FG_MAX_AREA_FRAC = 0.22        # > 22% of frame → likely wall + skirts merged
_FG_MIN_RED_SURROUND = 0.30     # min red fraction in surrounding ring
_FG_ASPECT_MIN = 1.3            # minAreaRect long/short ratio bounds
_FG_ASPECT_MAX = 4.5
_FG_ACCEPT_SCORE = 0.0035       # red_frac × area_frac × centrality product floor

# Horizontal centrality bias — foreground tables in the operator's
# recordings sit near the horizontal centre of frame; side background
# tables visible at frame edges have similar blue+red signature but
# off-centre positions. Soft penalty (multiplicative) rather than hard
# reject so a table that's slightly off-axis still passes. Falls off
# linearly from 1.0 at cx=0.50 to 0.30 at cx=0.0 or 1.0.
_FG_CENTRALITY_FLOOR = 0.30

# White court-line refinement. Foreground table tops have prominent
# white court lines (perimeter + centerline + doubles dividers) that
# fences/barriers/walls don't. Apply CONDITIONALLY — only fires when
# the winning blob is unusually large (area_frac ≥ _WHITE_REFINE_AREA),
# suggesting it merged with adjacent non-table blue regions (skirt
# strip, fence/barrier, wall). On clean blobs that already fit the
# table, refinement shrinks the quad below the actual table edge and
# can break the cc+orb_agree path → regression.
# HSV white mask: low S (≤70), high V (≥130). Less restrictive than
# ITTF spec — captures slightly-yellowed lines + WB drift.
_WHITE_LO = (0, 0, 130)
_WHITE_HI = (180, 70, 255)
# Only refine when blob is large enough to suspect merger.
# Calibrated empirically: clean fg-table blobs are 1.5-3.5% of frame
# after the ellipse-9×9 open. >4% suggests adjacent blue mass merged in.
_WHITE_REFINE_AREA = 0.04
# Minimum white pixels inside blob to trigger refinement (else fall
# back to blob-based fit). 200 px ≈ enough for partial court lines.
_WHITE_MIN_PIXELS = 200
# Expand the white-line bounding hull outward from its centroid so the
# corners reach the actual table edge (court lines sit inset).
_WHITE_EXPAND = 0.15
# Solidity (= contour_area / convex_hull_area). A clean table-top blob
# is near-convex (solidity > 0.90). Player occlusion bites a chunk out
# of the table → solidity drops (ThienQ7 case: 0.66 from a player
# standing on the table). 0.55 is permissive enough to let bitten-but-
# real blobs through; the quad-vs-blob area-ratio check downstream
# catches the actual failure mode (over-extended fit on tendril blobs).
_FG_MIN_SOLIDITY = 0.55
# When the fitted quad covers significantly more area than the actual
# blob pixels, the blob has tendrils / wraparounds and the quad is
# over-extended. Hard cap at 1.5× — observed unseen failure had
# quad/blob ≈ 2.2.
_FG_MAX_QUAD_BLOB_RATIO = 1.5
# Blob bounding-box must sit within these normalized Y bounds. Walls
# typically reach y=0 (frame top); floor reaches y=1. Real table tops
# in the operator's confirmed ROIs all sit in y ∈ [0.40, 0.85].
_FG_MIN_TOP_Y = 0.25
_FG_MAX_BOTTOM_Y = 0.95

# Search band: ignore blobs whose centre is outside the lower-mid of
# the frame. Cuts off walls (centre in upper half) without affecting
# tables (centre near 0.55-0.70).
_FG_CENTER_Y_RANGE = (0.40, 0.85)

# When ORB and color-contrast both succeed, IoU threshold to consider
# them "agreeing" — agreement → prefer ORB's pixel-accurate corners,
# disagreement → prefer the one with higher confidence.
_AGREEMENT_IOU = 0.45


def _fit_quad_to_blob(cnt: np.ndarray) -> np.ndarray:
    """Fit a 4-corner polygon to a contour. Tries approxPolyDP on the
    convex hull at increasing epsilon until exactly 4 vertices are
    found (captures true perspective trapezoid). Falls back to
    minAreaRect (rotated bbox) when no clean 4-point approximation
    exists — happens when the blob is fragmented or has unusual shape."""
    hull = cv2.convexHull(cnt)
    peri = cv2.arcLength(hull, True)
    for eps_factor in (0.015, 0.02, 0.025, 0.03, 0.04, 0.06, 0.08, 0.10):
        approx = cv2.approxPolyDP(hull, eps_factor * peri, True)
        if len(approx) == 4:
            return approx.reshape(-1, 2).astype(np.float32)
    rect = cv2.minAreaRect(cnt)
    return cv2.boxPoints(rect).astype(np.float32)


def _refine_quad_via_white_court_lines(
    blob_mask: np.ndarray,
    hsv: np.ndarray,
) -> tuple[np.ndarray | None, dict]:
    """Refine the table outline using white court lines inside the blob.

    Returns (quad_px or None, debug). When None, caller falls back to
    blob-based fit (`_fit_quad_to_blob` on the contour).

    Algorithm:
      1. HSV white mask (low S, high V) ∩ blob_mask = court-line pixels
         strictly inside the candidate blob.
      2. If too few white pixels, refinement isn't reliable → return None.
      3. Convex hull of all white pixels → tight bounding region of the
         actual table top (court lines trace the perimeter + centerline).
      4. approxPolyDP → 4-corner trapezoid; minAreaRect fallback.
      5. Expand the quad outward from its centroid by `_WHITE_EXPAND`
         (court lines sit slightly inset from the table edge).

    Failure modes this fixes:
      - Blob spans fg-table + adjacent blue fence: fence has no white,
        so step 1 keeps only the table portion. Refined quad sits on
        the table, not the merged region.
      - Blob is correct but blob-based fit drifts slightly outside the
        actual table: court lines pin the perimeter tighter.

    Falls back when:
      - Table has no visible court lines (rare; lighting / camera angle).
      - Player's white shirt is fully inside the table → hull gets pulled
        wide. Mitigation: court lines dominate pixel count when table is
        visible at all; player shirt is a minority.
    """
    white_mask = cv2.inRange(hsv, np.array(_WHITE_LO), np.array(_WHITE_HI))
    white_in_blob = cv2.bitwise_and(white_mask, blob_mask)
    n_white = int(cv2.countNonZero(white_in_blob))
    debug = {"white_pixels_in_blob": n_white}
    if n_white < _WHITE_MIN_PIXELS:
        debug["note"] = f"only {n_white} < {_WHITE_MIN_PIXELS} white pixels in blob; skip refinement"
        return None, debug

    # All white pixel coordinates inside the blob.
    coords = cv2.findNonZero(white_in_blob)
    if coords is None or len(coords) < 4:
        debug["note"] = "no white pixel coords"
        return None, debug

    hull = cv2.convexHull(coords)
    # Fit 4-corner trapezoid to the white-pixel hull.
    peri = cv2.arcLength(hull, True)
    quad_px: np.ndarray | None = None
    for eps_factor in (0.015, 0.02, 0.025, 0.03, 0.04, 0.06, 0.08, 0.10):
        approx = cv2.approxPolyDP(hull, eps_factor * peri, True)
        if len(approx) == 4:
            quad_px = approx.reshape(-1, 2).astype(np.float32)
            break
    if quad_px is None:
        rect = cv2.minAreaRect(coords)
        quad_px = cv2.boxPoints(rect).astype(np.float32)

    # Expand the quad outward from its centroid by _WHITE_EXPAND so the
    # corners reach the actual table edge, not the inset court line.
    centroid = quad_px.mean(axis=0)
    quad_px = centroid + (quad_px - centroid) * (1.0 + _WHITE_EXPAND)

    debug["expand"] = _WHITE_EXPAND
    return quad_px, debug


def _quad_iou(q1: list[list[float]], q2: list[list[float]], grid: int = 256) -> float:
    """Approximate IoU between two normalized quads via rasterisation."""
    m1 = np.zeros((grid, grid), dtype=np.uint8)
    m2 = np.zeros((grid, grid), dtype=np.uint8)
    p1 = np.array([[max(0, min(grid - 1, int(round(x * grid)))),
                    max(0, min(grid - 1, int(round(y * grid))))] for x, y in q1], dtype=np.int32)
    p2 = np.array([[max(0, min(grid - 1, int(round(x * grid)))),
                    max(0, min(grid - 1, int(round(y * grid))))] for x, y in q2], dtype=np.int32)
    cv2.fillPoly(m1, [p1], 255)
    cv2.fillPoly(m2, [p2], 255)
    inter = float(cv2.countNonZero(cv2.bitwise_and(m1, m2)))
    union = float(cv2.countNonZero(cv2.bitwise_or(m1, m2)))
    if union < 1.0:
        return 0.0
    return inter / union


def _try_foreground_by_color_contrast(img: np.ndarray) -> tuple[RoiDetection | None, dict]:
    """Stage 0: detect the foreground table using universal physical features.

    Pipeline:
      1. HSV mask for table-blue → connected blobs.
      2. HSV mask for floor-red → context mask.
      3. For each blue blob, dilate by ~30px and measure what fraction of
         that ring is red. Foreground table → red ring (sits on red
         floor). Background table → mixed ring (other tables, walls,
         people). Crucial discriminator: blue alone doesn't distinguish
         foreground from background; "blue surrounded by red" does.
      4. Filter by area + aspect + red_surround thresholds.
      5. Score = area_frac × red_frac; pick max.
      6. Fit quad (approxPolyDP for perspective trapezoid, fallback to
         minAreaRect).

    Example-independent — works on never-seen venues. Falls through to
    later stages when the venue lacks red floor or has unusual colour.
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    debug: dict = {}

    blue_mask = cv2.inRange(hsv, np.array(_TT_BLUE_LO), np.array(_TT_BLUE_HI))
    red_a = cv2.inRange(hsv, np.array(_FLOOR_RED_LO_A), np.array(_FLOOR_RED_HI_A))
    red_b = cv2.inRange(hsv, np.array(_FLOOR_RED_LO_B), np.array(_FLOOR_RED_HI_B))
    red_mask = cv2.bitwise_or(red_a, red_b)

    # OPEN with an ELLIPSE kernel to sever thin connections — skirt
    # strip necks between adjacent tables, wall-to-skirt seams, and
    # foreground-to-background-table bridges. Critical detail: ellipse
    # vs rect kernel. A rect 9×9 leaves the kernel's 4 corner pixels
    # intact, which preserves diagonal connectors that bridge two
    # otherwise separate blobs. An ellipse 9×9 has no corners and
    # cleanly snips diagonal threads (~3 px wide) that survive a rect
    # open of the same size. Validated on TuanPhu/FQuang/ThienQ7
    # arenas where rect kept fg-table merged with background mass;
    # ellipse 9×9 separates it cleanly.
    # CLOSE with a smaller rect to fill court-line gaps + net-shadow
    # pinholes inside the table without re-bridging gaps the open cut.
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, k_open)
    blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_CLOSE, k_close)

    contours, _ = cv2.findContours(blue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    debug["n_blue_blobs"] = len(contours)
    debug["red_frac_global"] = round(float(cv2.countNonZero(red_mask)) / (h * w), 3)
    if not contours:
        return None, debug

    frame_area = float(w * h)
    ring_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (2 * _CONTRAST_RING_PX + 1, 2 * _CONTRAST_RING_PX + 1),
    )

    candidates: list[dict] = []
    rejected: list[dict] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        area_frac = area / frame_area
        rect = cv2.minAreaRect(cnt)
        rw, rh = rect[1]
        cx_norm = float(rect[0][0]) / w
        cy_norm = float(rect[0][1]) / h
        bx, by, bw, bh = cv2.boundingRect(cnt)
        top_y = by / h
        bot_y = (by + bh) / h

        reason = None
        if area_frac < _FG_MIN_AREA_FRAC:
            reason = f"area<{_FG_MIN_AREA_FRAC}"
        elif area_frac > _FG_MAX_AREA_FRAC:
            reason = f"area>{_FG_MAX_AREA_FRAC} (likely wall/skirts merged)"
        elif min(rw, rh) < 1:
            reason = "degenerate rect"
        elif not (_FG_ASPECT_MIN <= max(rw, rh) / min(rw, rh) <= _FG_ASPECT_MAX):
            reason = f"aspect={max(rw,rh)/min(rw,rh):.2f} out of [{_FG_ASPECT_MIN}, {_FG_ASPECT_MAX}]"
        elif top_y < _FG_MIN_TOP_Y:
            reason = f"top_y={top_y:.2f} < {_FG_MIN_TOP_Y} (touches frame top, likely wall)"
        elif bot_y > _FG_MAX_BOTTOM_Y:
            reason = f"bot_y={bot_y:.2f} > {_FG_MAX_BOTTOM_Y} (touches frame bottom)"
        elif not (_FG_CENTER_Y_RANGE[0] <= cy_norm <= _FG_CENTER_Y_RANGE[1]):
            reason = f"center_y={cy_norm:.2f} outside {_FG_CENTER_Y_RANGE}"

        # Solidity = area / convex_hull_area. Catches "blob wraps around
        # multiple tables via thin tendrils" cases where geometric
        # filters above pass but the blob is fundamentally concave.
        hull_area = 0.0
        solidity = 1.0
        if reason is None:
            hull = cv2.convexHull(cnt)
            hull_area = float(cv2.contourArea(hull))
            if hull_area > 0:
                solidity = area / hull_area
                if solidity < _FG_MIN_SOLIDITY:
                    reason = f"solidity={solidity:.2f} < {_FG_MIN_SOLIDITY} (blob is concave, likely tendrils)"

        if reason is not None:
            rejected.append({"area_frac": round(area_frac, 4), "reason": reason})
            continue

        aspect = max(rw, rh) / min(rw, rh)

        # Build per-blob mask, dilate, ring = dilation - blob.
        blob_mask = np.zeros_like(blue_mask)
        cv2.drawContours(blob_mask, [cnt], -1, 255, cv2.FILLED)
        dilated = cv2.dilate(blob_mask, ring_kernel)
        ring = cv2.subtract(dilated, blob_mask)
        ring_area = float(cv2.countNonZero(ring))
        if ring_area < 1:
            continue
        ring_red = cv2.bitwise_and(ring, red_mask)
        red_frac = float(cv2.countNonZero(ring_red)) / ring_area

        if red_frac < _FG_MIN_RED_SURROUND:
            rejected.append({"area_frac": round(area_frac, 4),
                             "reason": f"red_frac={red_frac:.2f} < {_FG_MIN_RED_SURROUND}"})
            continue

        # Centrality penalty: foreground tables sit near horizontal centre.
        # Linear from 1.0 at cx=0.50 down to _FG_CENTRALITY_FLOOR at the
        # frame edges. Multiplicative on score so a foreground blob with
        # decent red+area at cx=0.5 beats an edge blob with similar red+area.
        centrality = max(_FG_CENTRALITY_FLOOR, 1.0 - abs(cx_norm - 0.5) * 2.0)

        score = red_frac * area_frac * centrality
        candidates.append({
            "score": score,
            "area_frac": area_frac,
            "red_frac": red_frac,
            "aspect": aspect,
            "centrality": centrality,
            "rect_center_norm": [cx_norm, cy_norm],
            "cnt": cnt,
        })
    debug["rejected"] = rejected[:5]

    debug["n_candidates"] = len(candidates)
    if not candidates:
        return None, debug

    candidates.sort(key=lambda c: -c["score"])
    debug["top_candidates"] = [
        {"area_frac": round(c["area_frac"], 4),
         "red_frac": round(c["red_frac"], 3),
         "aspect": round(c["aspect"], 2),
         "centrality": round(c["centrality"], 2),
         "score": round(c["score"], 5),
         "center": c["rect_center_norm"]}
        for c in candidates[:5]
    ]

    winner = candidates[0]
    if winner["red_frac"] < _FG_MIN_RED_SURROUND or winner["score"] < _FG_ACCEPT_SCORE:
        debug["fg_result"] = (
            f"best score={winner['score']:.4f} red_frac={winner['red_frac']:.2f} "
            f"below thresholds (need score≥{_FG_ACCEPT_SCORE}, red≥{_FG_MIN_RED_SURROUND})"
        )
        return None, debug

    # White court-line refinement: tries to fit the quad to the white
    # court lines inside the blob (tighter than blob-based fit). ONLY
    # apply when the winning blob is unusually large — clean
    # foreground-table blobs already fit well, and refinement on those
    # can shrink the quad below the actual table edge and break the
    # cc+orb_agree pathway (observed regression on vsVinh from 0.015 →
    # 0.144 when refinement was unconditional).
    used_refinement = False
    refine_debug: dict = {"applied": False, "reason": ""}
    if winner["area_frac"] >= _WHITE_REFINE_AREA:
        winner_blob_mask = np.zeros_like(blue_mask)
        cv2.drawContours(winner_blob_mask, [winner["cnt"]], -1, 255, cv2.FILLED)
        refined_quad, refine_debug = _refine_quad_via_white_court_lines(winner_blob_mask, hsv)
        refine_debug["applied"] = refined_quad is not None
        if refined_quad is not None:
            quad_px = refined_quad
            used_refinement = True
    if not used_refinement:
        if refine_debug.get("reason") == "":
            refine_debug["reason"] = (
                f"blob area_frac {winner['area_frac']:.3f} < "
                f"{_WHITE_REFINE_AREA} threshold (clean blob, no refinement needed)"
            )
        quad_px = _fit_quad_to_blob(winner["cnt"])

    # Quad-vs-blob area ratio: if the fitted quad covers significantly
    # more pixel area than the actual blob, the quad is over-extended
    # (blob has tendrils, fit smooths over them). Reject so a less-bad
    # tier can take over. Skip this check when the quad was refined via
    # court lines — by design, court-line refinement may produce a
    # SMALLER quad than the blob (which is the whole point: when the
    # blob spans table + fence, the quad correctly shrinks to the table
    # portion where court lines are; quad/blob ratio < 1 is good here).
    blob_area_px = float(cv2.contourArea(winner["cnt"]))
    quad_area_px = 0.0
    n_quad = len(quad_px)
    for k in range(n_quad):
        x1, y1 = quad_px[k]
        x2, y2 = quad_px[(k + 1) % n_quad]
        quad_area_px += float(x1 * y2 - x2 * y1)
    quad_area_px = abs(quad_area_px) / 2.0
    # Force Python float — quad_px is numpy.float32 from _fit_quad_to_blob /
    # _refine_quad_via_white_court_lines, and the accumulator above picks up
    # numpy's dtype. Leaks into debug dict → FastAPI/Pydantic chokes when
    # serializing the response (PydanticSerializationError: numpy.float32).
    quad_blob_ratio = float(quad_area_px / max(blob_area_px, 1.0))
    if not used_refinement and quad_blob_ratio > _FG_MAX_QUAD_BLOB_RATIO:
        debug["fg_result"] = (
            f"quad/blob area ratio = {quad_blob_ratio:.2f} > "
            f"{_FG_MAX_QUAD_BLOB_RATIO} (quad over-extends blob)"
        )
        return None, debug

    norm = [[float(p[0]) / w, float(p[1]) / h] for p in quad_px]
    norm = _order_clockwise_from_tl(norm)
    norm = [[max(0.0, min(1.0, p[0])), max(0.0, min(1.0, p[1]))] for p in norm]

    # Confidence: red_frac is the discriminator; area gives a small lift
    # for tables that fill more of the frame. Capped at 0.85 — lower than
    # ORB's 0.90+ ceiling so ORB wins disagreement ties on the trained
    # set where it tends to be more accurate. Color-contrast still wins
    # on fresh arenas (ORB returns None, no tie to break).
    conf = 0.50 + min(0.35, winner["red_frac"] * 0.5 + winner["area_frac"] * 1.2)
    conf = float(min(0.85, conf))

    return RoiDetection(
        corners=norm,
        confidence=conf,
        method="color_contrast_foreground",
        debug={
            "tier": "foreground_color_contrast",
            "area_frac": round(winner["area_frac"], 4),
            "red_surround_frac": round(winner["red_frac"], 3),
            "aspect": round(winner["aspect"], 2),
            "score": round(winner["score"], 5),
            "quad_blob_ratio": round(quad_blob_ratio, 2),
            "white_line_refined": used_refinement,
            "white_refine_debug": refine_debug,
            "n_blue_blobs": len(contours),
            "n_candidates": len(candidates),
            "top_candidates": debug["top_candidates"],
            "red_frac_global": debug["red_frac_global"],
        },
    ), debug


# ----- Stage 1: ORB keypoint matching + homography transfer ---------------

# ORB feature parameters. 1000 keypoints is enough for refframes at 960×540;
# higher counts add cost without much benefit. scaleFactor 1.2 / nlevels 8
# gives reasonable scale invariance for slight tripod zoom differences.
_ORB_N_FEATURES = 1000
_ORB_SCALE_FACTOR = 1.2
_ORB_N_LEVELS = 8

# Minimum number of RANSAC inliers to accept a homography. Calibrated for
# refframes with moderate texture (banners, equipment, court lines).
#   < 15 inliers → unreliable, fallback to HSV
#   15-30        → same scene but POV drift, acceptable
#   ≥ 30         → very strong match, near-pixel accurate
_ORB_MIN_INLIERS = 15
# Reprojection threshold for RANSAC (pixels). Tighter = stricter geometric
# consistency; 4.0 px works well on 960px-wide refframes.
_ORB_RANSAC_REPROJ_THRESH = 4.0
# Sanity bounds on transformed corners (normalized). Allow slight overshoot
# (camera reframe) but reject obviously invalid homographies.
_ORB_CORNER_SANITY = (-0.15, 1.15)


def _compute_orb_features(img_bgr: np.ndarray) -> dict | None:
    """Compute ORB keypoints + descriptors on a grayscale version of the
    refframe. Returns None if too few keypoints (e.g. very low-texture
    scene), which falls the caller through to HSV matching."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(
        nfeatures=_ORB_N_FEATURES,
        scaleFactor=_ORB_SCALE_FACTOR,
        nlevels=_ORB_N_LEVELS,
    )
    kp, des = orb.detectAndCompute(gray, None)
    if des is None or len(kp) < 20:
        return None
    return {
        "keypoints": np.array([k.pt for k in kp], dtype=np.float32),
        "descriptors": des,
        "image_w": int(img_bgr.shape[1]),
        "image_h": int(img_bgr.shape[0]),
    }


def _match_orb_homography(query_feat: dict, train_feat: dict) -> dict:
    """RANSAC homography between query and train ORB features.

    Returns dict with:
      - `inliers`: int (RANSAC inlier count)
      - `H`: 3×3 homography matrix mapping TRAIN-frame pixels → QUERY-frame
             pixels (or None if no valid H)
      - `ratio`: inliers / min(query_kp_count, train_kp_count)
      - `n_matches`: total descriptor matches before RANSAC
    """
    fail = {"inliers": 0, "H": None, "ratio": 0.0, "n_matches": 0}
    if query_feat is None or train_feat is None:
        return fail

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(query_feat["descriptors"], train_feat["descriptors"])
    fail["n_matches"] = len(matches)
    if len(matches) < 8:
        return fail

    # Build point pairs. m.queryIdx indexes into query features; m.trainIdx
    # indexes into train features. We want H s.t. H * train_pt = query_pt,
    # so cv2.findHomography(src=train, dst=query, ...).
    src_pts = np.float32([train_feat["keypoints"][m.trainIdx] for m in matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([query_feat["keypoints"][m.queryIdx] for m in matches]).reshape(-1, 1, 2)

    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, _ORB_RANSAC_REPROJ_THRESH)
    if H is None or mask is None:
        return fail

    inliers = int(mask.sum())
    n_kp = min(len(query_feat["keypoints"]), len(train_feat["keypoints"]))
    return {
        "inliers": inliers,
        "H": H,
        "ratio": inliers / max(n_kp, 1),
        "n_matches": len(matches),
    }


def _transfer_corners_via_homography(
    corners_norm: list[list[float]],
    train_w: int, train_h: int,
    query_w: int, query_h: int,
    H: np.ndarray,
) -> list[list[float]]:
    """Map normalized [0,1] corners from train frame → query frame via H."""
    train_px = np.array(
        [[c[0] * train_w, c[1] * train_h] for c in corners_norm],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    query_px = cv2.perspectiveTransform(train_px, H).reshape(-1, 2)
    return [[float(p[0] / query_w), float(p[1] / query_h)] for p in query_px]


def _try_orb_match(img: np.ndarray, exclude_video_id: str | None) -> tuple[RoiDetection | None, dict]:
    """Stage 0: ORB feature matching with homography transfer.

    For each groundtruth example, compute RANSAC homography from its
    refframe to the query. Pick the one with most inliers. If above
    threshold, transfer that example's ROI corners through the homography
    and return — this is the most accurate path because corners get
    transformed pixel-by-pixel, accounting for slight POV/zoom changes.
    """
    examples = _load_groundtruth_examples(exclude_video_id)
    debug: dict = {"n_groundtruth_examples": len(examples)}
    if not examples:
        return None, debug

    query_orb = _compute_orb_features(img)
    if query_orb is None:
        debug["orb_query"] = "insufficient_keypoints"
        return None, debug
    debug["orb_query_keypoints"] = len(query_orb["keypoints"])

    # Load + cache ORB features per example. Computed lazily because the
    # groundtruth set is small (~10s of files) and ORB extraction is fast.
    scored = []
    for ex in examples:
        if "orb_features" not in ex:
            ex_img = cv2.imread(str(_GROUNDTRUTH_DIR / f"{ex['video_id']}.jpg"))
            if ex_img is None:
                continue
            ex["orb_features"] = _compute_orb_features(ex_img)
        if ex["orb_features"] is None:
            continue
        m = _match_orb_homography(query_orb, ex["orb_features"])
        scored.append({**ex, "match": m})

    if not scored:
        debug["orb_result"] = "no_examples_with_features"
        return None, debug

    scored.sort(key=lambda r: -r["match"]["inliers"])
    debug["orb_top3"] = [
        {
            "video": r["video_name"],
            "inliers": r["match"]["inliers"],
            "ratio": round(r["match"]["ratio"], 3),
            "matches": r["match"]["n_matches"],
        }
        for r in scored[:3]
    ]

    best = scored[0]
    inliers = best["match"]["inliers"]
    H = best["match"]["H"]
    if inliers < _ORB_MIN_INLIERS or H is None:
        debug["orb_result"] = f"best_inliers={inliers} below threshold {_ORB_MIN_INLIERS}"
        return None, debug

    # Transfer corners
    feat = best["orb_features"]
    transferred = _transfer_corners_via_homography(
        best["corners"],
        train_w=feat["image_w"], train_h=feat["image_h"],
        query_w=img.shape[1], query_h=img.shape[0],
        H=H,
    )

    # Sanity check 1: corners must stay in plausible normalized bounds.
    lo, hi = _ORB_CORNER_SANITY
    if not all(lo <= c[0] <= hi and lo <= c[1] <= hi for c in transferred):
        debug["orb_result"] = f"corners out of bounds {transferred}"
        return None, debug

    # Sanity check 2: per-corner displacement from source ≤ 12%. Same tripod
    # position rarely shifts corners more than a few %. When ORB matches on
    # busy scene features (banners, equipment) but the table plane isn't
    # well-represented, the homography over-fits and warps corners wildly
    # — observed in early validation (MinhDuong → 27% displacement).
    src = best["corners"]
    max_disp = max(
        ((transferred[i][0] - src[i][0]) ** 2 + (transferred[i][1] - src[i][1]) ** 2) ** 0.5
        for i in range(4)
    )
    if max_disp > 0.12:
        debug["orb_result"] = f"corner displacement {max_disp:.3f} > 0.12 (homography likely over-fit)"
        return None, debug

    # Sanity check 3: transferred polygon area should be in same order of
    # magnitude as source. Catches cases where H scales the quad oddly
    # (very small / very large / sliver).
    def _poly_area(pts: list[list[float]]) -> float:
        n = len(pts)
        s = 0.0
        for i in range(n):
            x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0
    src_area = _poly_area(src)
    new_area = _poly_area(transferred)
    if src_area < 1e-4 or not (0.5 <= new_area / src_area <= 2.0):
        debug["orb_result"] = f"area ratio {new_area/src_area:.2f} out of [0.5, 2.0]"
        return None, debug

    # Clamp to [0, 1] for downstream consumers
    transferred = [[max(0.0, min(1.0, c[0])), max(0.0, min(1.0, c[1]))] for c in transferred]

    # Confidence: maps inliers to [0.7, 1.0] over the [_ORB_MIN_INLIERS, 100] range.
    # Penalize small additional displacement (we already rejected > 0.12).
    conf = 0.7 + min(0.3, max(0, inliers - _ORB_MIN_INLIERS) / 200.0)
    conf *= max(0.5, 1.0 - max_disp * 4)  # disp 0 → ×1.0, disp 0.12 → ×0.52

    return RoiDetection(
        corners=transferred,
        confidence=float(conf),
        method=f"orb_homography:{best['video_name']}",
        debug={
            "matched_video": best["video_name"],
            "matched_video_id": best["video_id"],
            "inliers": inliers,
            "n_matches": best["match"]["n_matches"],
            "ratio": best["match"]["ratio"],
            "n_groundtruth_examples": len(examples),
            "orb_top3": debug["orb_top3"],
            "tier": "orb_homography",
        },
    ), debug


# ----- Stage 1: learned nearest-neighbor ----------------------------------

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


def _load_groundtruth_examples(exclude_video_id: str | None = None) -> list[dict]:
    """Enumerate dataset/roi_groundtruth/*.json + matching *.jpg pairs.
    Skips entries whose video_id matches `exclude_video_id` so a detector
    call on a re-confirm doesn't trivially self-match."""
    if not _GROUNDTRUTH_DIR.exists():
        return []
    out = []
    for json_path in sorted(_GROUNDTRUTH_DIR.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        vid = data.get("video_id") or json_path.stem
        if exclude_video_id is not None and vid == exclude_video_id:
            continue
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


# ----- Stage 2: naive color-based fallback --------------------------------

# Table-top hue/value ranges in HSV. Blue tabletops (the most common, e.g.
# Double Fish, Joola Inside) live around hue 110°. Green tabletops (less
# common but seen in older clubs) live around hue 60-75°. Saturation
# minimums weed out grey concrete floors; value minimums weed out shadows.
_HSV_RANGES = {
    "color_blue":  ((100, 90, 60), (135, 255, 255)),
    "color_green": ((40, 80, 50), (85, 255, 255)),
}

# Tables sit in the central-lower part of the frame. Outside this band we
# don't even consider blobs — too many distractions from background tables.
_SEARCH_BAND_X = (0.10, 0.90)
_SEARCH_BAND_Y = (0.45, 0.95)

# Minimum blob area as a fraction of the frame to be considered a real table.
_MIN_BLOB_FRAC = 0.01
# Maximum width/height bounds (fraction of frame) — rejects walls + banners.
_MAX_BLOB_W_FRAC = 0.65
_MAX_BLOB_H_FRAC = 0.55
# Aspect ratio bounds for table-shaped blobs (width/height of fitted rect).
_ASPECT_MIN = 0.7
_ASPECT_MAX = 6.0

# Expand fitted rect outward to include arm-swing zone above the surface.
_MARGIN_ABOVE_REL = 0.40
_MARGIN_SIDE_REL = 0.05


def _naive_detect(img: np.ndarray) -> RoiDetection:
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    band_mask = np.zeros((h, w), dtype=np.uint8)
    y0 = int(h * _SEARCH_BAND_Y[0])
    y1 = int(h * _SEARCH_BAND_Y[1])
    x0 = int(w * _SEARCH_BAND_X[0])
    x1 = int(w * _SEARCH_BAND_X[1])
    band_mask[y0:y1, x0:x1] = 255

    frame_area = w * h
    best: RoiDetection | None = None

    for method, (lo, hi) in _HSV_RANGES.items():
        color_mask = cv2.inRange(hsv, np.array(lo), np.array(hi))
        color_mask = cv2.bitwise_and(color_mask, band_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_CLOSE, kernel)
        color_mask = cv2.morphologyEx(color_mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(color_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue

        # Filter contours: enough area, plausible shape, doesn't span too
        # much of the frame (walls / banners), with sensible aspect ratio.
        candidates = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < _MIN_BLOB_FRAC * frame_area:
                continue
            bx, by, bw, bh = cv2.boundingRect(cnt)
            if bw > _MAX_BLOB_W_FRAC * w or bh > _MAX_BLOB_H_FRAC * h:
                continue
            rect = cv2.minAreaRect(cnt)
            rw, rh = rect[1]
            if min(rw, rh) < 1:
                continue
            aspect = max(rw, rh) / min(rw, rh)
            if aspect < _ASPECT_MIN or aspect > _ASPECT_MAX:
                continue
            candidates.append((cnt, area, rect))

        if not candidates:
            continue

        # Prefer larger AND centred — score = area / dist_to_centre
        cx0, cy0 = w / 2, h / 2
        def score(item):
            cnt, area, _rect = item
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                return -1
            ccx = M["m10"] / M["m00"]
            ccy = M["m01"] / M["m00"]
            dist = math.hypot(ccx - cx0, ccy - cy0) / math.hypot(cx0, cy0)
            return area * (1.0 - 0.6 * dist)
        candidates.sort(key=score, reverse=True)
        cnt, area, rect = candidates[0]

        box = cv2.boxPoints(rect)
        box = _expand_rect(box, w, h)
        norm = [[float(p[0]) / w, float(p[1]) / h] for p in box]
        norm = _order_clockwise_from_tl(norm)

        # Cap naive confidence at 0.80 — even a perfect color match isn't
        # as trustworthy as a confirmed example.
        conf = min(0.80, area / (0.10 * frame_area))

        det = RoiDetection(
            corners=norm,
            confidence=conf,
            method=method,
            debug={
                "blob_area_px": int(area),
                "blob_area_frac": float(area / frame_area),
                "rect_center": [float(rect[0][0]) / w, float(rect[0][1]) / h],
                "rect_size": [float(rect[1][0]) / w, float(rect[1][1]) / h],
                "rect_angle_deg": float(rect[2]),
            },
        )
        if best is None or det.confidence > best.confidence:
            best = det

    if best is not None and best.confidence >= 0.15:
        return best
    fallback = _default_roi(reason="no qualifying blob")
    if best is not None:
        fallback.debug["low_conf_guess"] = {
            "corners": best.corners,
            "method": best.method,
            "confidence": best.confidence,
        }
    return fallback


# ----- multi-frame aggregation --------------------------------------------

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


# ----- public entry point -------------------------------------------------

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


# ----- helpers ------------------------------------------------------------

def _default_roi(reason: str = "default") -> RoiDetection:
    return RoiDetection(
        corners=[
            [0.25, 0.55],  # TL
            [0.65, 0.55],  # TR
            [0.67, 0.85],  # BR
            [0.23, 0.85],  # BL
        ],
        confidence=0.0,
        method="default",
        debug={"reason": reason},
    )


def _expand_rect(box: np.ndarray, w: int, h: int) -> np.ndarray:
    box = np.asarray(box, dtype=np.float32)
    cx = float(box[:, 0].mean())
    box[:, 0] = cx + (box[:, 0] - cx) * (1.0 + _MARGIN_SIDE_REL)
    sorted_by_y = box[np.argsort(box[:, 1])]
    upper_y_mean = float(sorted_by_y[:2, 1].mean())
    lower_y_mean = float(sorted_by_y[2:, 1].mean())
    height = max(1.0, lower_y_mean - upper_y_mean)
    new_box = box.copy()
    for i, p in enumerate(box):
        if p[1] < (upper_y_mean + lower_y_mean) / 2:
            new_box[i, 1] = max(0.0, p[1] - height * _MARGIN_ABOVE_REL)
    new_box[:, 0] = np.clip(new_box[:, 0], 0, w - 1)
    new_box[:, 1] = np.clip(new_box[:, 1], 0, h - 1)
    return new_box


def _order_clockwise_from_tl(pts: list[list[float]]) -> list[list[float]]:
    """Reorder 4 quad corners as TL → TR → BR → BL (clockwise from TL).

    "TL" = topmost corner (smallest y), tiebreak leftmost (smallest x).
    Empirically validated against 30 operator-confirmed groundtruth ROIs:
    23/30 match `min_y` convention vs only 6/30 for `min(x+y)`. The
    operator visually identifies "back-left of the table" as TL — for a
    low-angle perspective trapezoid this corner has the smallest image-y
    (table back is far from camera → upper in frame). Using `min(x+y)`
    instead would pick a side corner when the table is wide and slightly
    off-centre, breaking corner-pair correspondence with the groundtruth.
    """
    arr = np.asarray(pts, dtype=np.float64)
    cx = arr[:, 0].mean()
    cy = arr[:, 1].mean()
    angles = np.arctan2(arr[:, 1] - cy, arr[:, 0] - cx)
    order = np.argsort(angles)
    sorted_pts = arr[order]
    # TL = smallest y; ties broken by smallest x. argmin on (y, x) lex
    # ordering via stable secondary sort.
    ys = sorted_pts[:, 1]
    xs = sorted_pts[:, 0]
    min_y = ys.min()
    # Indices tied for min y (within 1px-equivalent epsilon for safety).
    tied = np.where(ys <= min_y + 1e-9)[0]
    if len(tied) > 1:
        tl_idx = int(tied[np.argmin(xs[tied])])
    else:
        tl_idx = int(tied[0])
    sorted_pts = np.roll(sorted_pts, -tl_idx, axis=0)
    return [[float(p[0]), float(p[1])] for p in sorted_pts]


def render_debug_overlay(refframe_path: Path | str, detection: RoiDetection,
                          out_path: Path | str) -> None:
    img = cv2.imread(str(refframe_path))
    if img is None:
        return
    h, w = img.shape[:2]
    pts = np.array([[round(x * w), round(y * h)] for x, y in detection.corners], dtype=np.int32)
    overlay = img.copy()
    cv2.fillPoly(overlay, [pts], (0, 200, 0))
    img = cv2.addWeighted(overlay, 0.25, img, 0.75, 0)
    cv2.polylines(img, [pts], True, (0, 255, 0), 3)
    for (x, y), lbl in zip(pts, ["TL", "TR", "BR", "BL"]):
        cv2.circle(img, (int(x), int(y)), 8, (0, 255, 255), -1)
        cv2.putText(img, lbl, (int(x) + 10, int(y) + 6), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 255), 2, cv2.LINE_AA)
    txt = f"{detection.method}  conf={detection.confidence:.2f}"
    cv2.putText(img, txt, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)
    cv2.imwrite(str(out_path), img)
