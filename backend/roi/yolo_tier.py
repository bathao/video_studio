"""Stage -1: YOLOv8-seg (Phase B, learned segmentation).

Lazy-loaded from `assets/models/roi_seg.pt` via `backend.roi_yolo`.
Falls through (returns None) when the model file is absent or
produces no high-confidence detection.
"""

from __future__ import annotations

import numpy as np

from .quad import RoiDetection


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
        from .. import roi_yolo
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
