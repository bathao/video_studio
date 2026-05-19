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

from .detector import detect_roi, detect_roi_multiframe
from .quad import RoiDetection, render_debug_overlay

__all__ = [
    "detect_roi",
    "detect_roi_multiframe",
    "RoiDetection",
    "render_debug_overlay",
]
