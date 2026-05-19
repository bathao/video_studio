"""Shared types + quad-geometry helpers used by every detection tier.

Kept tier-agnostic: this module imports nothing from the tier modules
under `backend/roi/` so it can act as the common dependency without
creating circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


# Resolve relative to the repo root (matches the layout used by server.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GROUNDTRUTH_DIR = _REPO_ROOT / "dataset" / "roi_groundtruth"


@dataclass
class RoiDetection:
    corners: list[list[float]]   # [[x, y], ...] 4 points, normalized 0-1, TL→TR→BR→BL
    confidence: float            # rough quality estimate, 0-1
    method: str                  # see backend/roi package docstring
    debug: dict = field(default_factory=dict)


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


# Expand fitted rect outward to include arm-swing zone above the surface.
# Used by the naive fallback tier.
_MARGIN_ABOVE_REL = 0.40
_MARGIN_SIDE_REL = 0.05


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
