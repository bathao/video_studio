"""Stage 1: ORB keypoint match + RANSAC homography transfer.

For each groundtruth example, computes a homography that maps the
example's confirmed corners into the query frame. Wins when the
camera POV is similar (typical case: same tripod across matches in a
session). Pixel-accurate but vulnerable to confidently transferring
corners from the wrong arena (background-table failure mode).
"""

from __future__ import annotations

import cv2
import numpy as np

from .quad import RoiDetection, _GROUNDTRUTH_DIR
from .learned_nn_tier import _load_groundtruth_examples


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
