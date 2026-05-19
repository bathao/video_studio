"""Stage 2: naive HSV-color fallback.

Used only when no groundtruth exists at all (cold-start). Restricted
to the lower-mid band of the frame, filters by aspect ratio and a
conservative width cap so it can't latch onto walls or background
dividers.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from .quad import (
    RoiDetection,
    _default_roi,
    _expand_rect,
    _order_clockwise_from_tl,
)


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
