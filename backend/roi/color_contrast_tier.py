"""Stage 0: foreground table by colour contrast (universal physics).

Detects the foreground table using physical features that are near-
universal across the operator's typical recording venues: ITTF-standard
blue table on red carpet/floor. Distinguishes the foreground table from
background tables (which are also blue) by measuring red-surroundedness.
Example-independent — works on never-seen venues.
"""

from __future__ import annotations

import cv2
import numpy as np

from .quad import RoiDetection, _fit_quad_to_blob, _order_clockwise_from_tl


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
