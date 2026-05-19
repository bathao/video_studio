"""YOLOv8-seg inference for foreground-table ROI detection (Phase B).

Lazy-loaded: the model file `assets/models/roi_seg.pt` is checked
on first call. If missing (operator hasn't trained yet), this module's
public `try_yolo_seg(img)` returns None and the detector pipeline falls
through to the classical CV stages.

Why a separate module:
  - Keeps `roi_detector.py` ML-dep free at import time (the backend
    still loads on machines without `ultralytics` installed).
  - Cleanly contains the torch/ultralytics surface area.
  - Lets training and inference share nothing but the trained weights
    file — re-training doesn't require restarting via different code.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODEL_PATH = _REPO_ROOT / "assets" / "models" / "roi_seg.pt"

# Module-level cache. None = not yet attempted to load. False = tried and
# failed (missing file or import error) → don't keep trying. Set =
# loaded model object.
_model_cache: object | None | bool = None
_model_lock = threading.Lock()
_logger = logging.getLogger(__name__)

# Confidence threshold for YOLO predictions to be considered usable.
# Calibrated empirically once model trained; conservative default until
# then to avoid YOLO out-confidence-ing classical CV on noisy predictions.
_YOLO_MIN_CONF = 0.55

# Inference image size. Should match the size the model was trained at
# (or a near multiple) for best mAP. 640 = ultralytics default.
_YOLO_IMGSZ = 640


def _load_model_if_available() -> object | None:
    """Returns the loaded YOLO model, or None if unavailable. Caches
    result so missing-model overhead is one stat() per process."""
    global _model_cache
    if _model_cache is False:
        return None
    if _model_cache is not None:
        return _model_cache  # type: ignore[return-value]

    with _model_lock:
        # Re-check inside lock (double-checked locking).
        if _model_cache is False:
            return None
        if _model_cache is not None:
            return _model_cache  # type: ignore[return-value]

        if not _MODEL_PATH.exists():
            _logger.info(
                "YOLO model not found at %s — Phase B disabled "
                "(classical CV pipeline still active)",
                _MODEL_PATH,
            )
            _model_cache = False
            return None

        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError:
            _logger.warning(
                "ultralytics not installed; YOLO ROI detection disabled. "
                "Run `pip install -r requirements.txt` to enable."
            )
            _model_cache = False
            return None

        try:
            model = YOLO(str(_MODEL_PATH))
            # Warm-up forward pass on a dummy image so the first real
            # detect call doesn't pay JIT/CUDA-load latency.
            dummy = np.zeros((_YOLO_IMGSZ, _YOLO_IMGSZ, 3), dtype=np.uint8)
            _ = model.predict(dummy, imgsz=_YOLO_IMGSZ, verbose=False)
            _model_cache = model
            _logger.info("YOLO model loaded from %s", _MODEL_PATH)
            return model
        except Exception as e:
            _logger.exception("Failed to load YOLO model: %s", e)
            _model_cache = False
            return None


def _polygon_to_quad(polygon_xy: np.ndarray) -> np.ndarray | None:
    """Reduce a segmentation polygon (N points) to a 4-corner quad.

    Hybrid approach:
      1. Try `approxPolyDP` at increasing epsilon. Among all 4-point
         reductions found, pick the one with MAXIMUM area — the most
         "filling" quad is the one whose vertices land on the actual
         hull corners rather than interior perimeter points.
      2. Validate the result has 4 distinct corners (min edge length
         ≥ 1% of hull diagonal). Some YOLO mask shapes cause the
         simplification to collapse two adjacent vertices, producing
         a degenerate triangle-looking quad.
      3. Fall back to `cv2.minAreaRect` (rotated rectangle) — always
         4 distinct corners with 90° angles, slightly overestimates
         perspective trapezoids but never degenerates.

    History:
      - v1 used `approxPolyDP` and returned the FIRST 4-vertex result.
        Could under-fill the hull (kite/diamond shape) when an early
        epsilon picked smooth interior points.
      - v2 used image-axis extremes (argmin/max of x±y). Fixed the
        under-fill but degenerated on tilted/slim masks (where two
        extreme criteria selected the same hull vertex → 3-sided quad).
      - v3 (current) prefers max-area approxPolyDP, falls back to
        minAreaRect. Combines v1's accuracy on clean trapezoids with
        v2's robustness to weird shapes via a non-degenerate fallback.
    """
    if polygon_xy is None or len(polygon_xy) < 4:
        return None
    pts = polygon_xy.astype(np.float32).reshape(-1, 1, 2)
    hull = cv2.convexHull(pts)
    peri = cv2.arcLength(hull, True)

    # Bounding-box diagonal as the scale reference for "degenerate" check.
    hull_pts_xy = hull.reshape(-1, 2)
    bbox_w = hull_pts_xy[:, 0].max() - hull_pts_xy[:, 0].min()
    bbox_h = hull_pts_xy[:, 1].max() - hull_pts_xy[:, 1].min()
    bbox_diag = float(np.hypot(bbox_w, bbox_h))
    min_edge = 0.01 * bbox_diag  # 1% of hull diagonal as min edge length

    best_quad: np.ndarray | None = None
    best_area = -1.0
    for eps_factor in (0.015, 0.02, 0.025, 0.03, 0.04, 0.06, 0.08, 0.10):
        approx = cv2.approxPolyDP(hull, eps_factor * peri, True)
        if len(approx) != 4:
            continue
        quad = approx.reshape(-1, 2).astype(np.float32)
        # Reject degenerate: any edge shorter than 1% of bbox diagonal.
        min_e = min(
            float(np.linalg.norm(quad[i] - quad[(i + 1) % 4]))
            for i in range(4)
        )
        if min_e < min_edge:
            continue
        area = float(abs(cv2.contourArea(quad)))
        if area > best_area:
            best_area = area
            best_quad = quad

    if best_quad is not None:
        return best_quad

    # Fallback: rotated bounding rect, always 4 distinct corners.
    rect = cv2.minAreaRect(hull)
    return cv2.boxPoints(rect).astype(np.float32)


def _order_clockwise_from_tl_local(pts: list[list[float]]) -> list[list[float]]:
    """Same ordering helper as backend.roi.quad._order_clockwise_from_tl —
    kept local so this module doesn't import the larger detector module.
    TL = topmost (smallest y); tiebreak leftmost (smallest x). See
    backend.roi.quad._order_clockwise_from_tl for the rationale (matches
    operator's storage convention 23/30 vs 6/30 for min(x+y))."""
    arr = np.asarray(pts, dtype=np.float64)
    cx = arr[:, 0].mean()
    cy = arr[:, 1].mean()
    angles = np.arctan2(arr[:, 1] - cy, arr[:, 0] - cx)
    order = np.argsort(angles)
    sorted_pts = arr[order]
    ys = sorted_pts[:, 1]
    xs = sorted_pts[:, 0]
    min_y = ys.min()
    tied = np.where(ys <= min_y + 1e-9)[0]
    if len(tied) > 1:
        tl_idx = int(tied[np.argmin(xs[tied])])
    else:
        tl_idx = int(tied[0])
    sorted_pts = np.roll(sorted_pts, -tl_idx, axis=0)
    return [[float(p[0]), float(p[1])] for p in sorted_pts]


def try_yolo_seg(img_bgr: np.ndarray) -> dict | None:
    """Run YOLOv8-seg on a BGR image; return dict or None.

    On success returns:
        {
            "corners": [[x, y], ...]   # 4 normalized corners, TL→TR→BR→BL
            "confidence": float        # YOLO class confidence (0..1)
            "n_detections": int        # how many segmentation masks YOLO produced
            "selected_area_frac": float  # area of selected mask / frame area
            "raw_polygon": [[x, y], ...]  # pre-quad-reduction polygon (debug)
        }

    Returns None when:
        - Model file isn't installed yet (operator hasn't trained)
        - ultralytics import fails
        - Model loaded but produced no detection above _YOLO_MIN_CONF
        - Segmentation polygon was degenerate / couldn't reduce to a quad
    """
    model = _load_model_if_available()
    if model is None:
        return None

    h, w = img_bgr.shape[:2]
    results = model.predict(
        img_bgr,
        imgsz=_YOLO_IMGSZ,
        conf=_YOLO_MIN_CONF,
        verbose=False,
    )
    if not results:
        return None
    r0 = results[0]
    if r0.masks is None or r0.boxes is None or len(r0.boxes) == 0:
        return None

    # Pick highest-confidence prediction. With 1 class (foreground_table)
    # we expect typically 1 mask; multiple masks would mean YOLO saw
    # multiple table candidates → take the most confident.
    confs = r0.boxes.conf.cpu().numpy()
    best_idx = int(np.argmax(confs))
    best_conf = float(confs[best_idx])

    # Mask polygon in pixel coordinates. ultralytics returns masks
    # both as binary tensors and as polygon xy lists; we prefer the
    # polygon since it's already simplified.
    poly_xy = None
    if hasattr(r0.masks, "xy") and r0.masks.xy is not None:
        polys = r0.masks.xy
        if best_idx < len(polys):
            poly_xy = np.asarray(polys[best_idx], dtype=np.float32)

    if poly_xy is None:
        # Fallback: reconstruct polygon from the binary mask.
        mask = r0.masks.data[best_idx].cpu().numpy().astype(np.uint8) * 255
        if mask.shape != (h, w):
            mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contours = sorted(contours, key=cv2.contourArea, reverse=True)
        poly_xy = contours[0].reshape(-1, 2).astype(np.float32)

    quad_px = _polygon_to_quad(poly_xy)
    if quad_px is None:
        return None

    norm = [
        [max(0.0, min(1.0, float(p[0]) / w)),
         max(0.0, min(1.0, float(p[1]) / h))]
        for p in quad_px
    ]
    norm = _order_clockwise_from_tl_local(norm)

    # Selected mask area (for downstream sanity checks)
    poly_area_px = 0.0
    n = len(poly_xy)
    for i in range(n):
        x1, y1 = poly_xy[i]
        x2, y2 = poly_xy[(i + 1) % n]
        poly_area_px += x1 * y2 - x2 * y1
    poly_area_px = abs(poly_area_px) / 2.0

    return {
        "corners": norm,
        "confidence": best_conf,
        "n_detections": int(len(confs)),
        "selected_area_frac": float(poly_area_px / max(w * h, 1.0)),
        "raw_polygon": poly_xy.tolist(),
    }


def model_status() -> dict:
    """Quick status report for diagnostics / debug output. Doesn't trigger
    a model load if not already loaded — just inspects what's available."""
    return {
        "model_path": str(_MODEL_PATH),
        "model_exists": _MODEL_PATH.exists(),
        "model_loaded": _model_cache not in (None, False),
        "load_attempted": _model_cache is not None,
    }
