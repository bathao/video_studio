"""Render debug overlay for a single dataset entry — shows predicted vs
groundtruth corners, blue mask, red mask. Used to diagnose tier-0 failures.

Usage:
    python -X utf8 scripts/debug_roi_overlay.py [video_id_prefix]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import roi_detector  # noqa: E402


_GT_DIR = _REPO_ROOT / "dataset" / "roi_groundtruth"
_OUT_DIR = _REPO_ROOT / "scripts" / "_debug_overlays"
_OUT_DIR.mkdir(exist_ok=True)


def _draw_quad(img: np.ndarray, corners: list[list[float]],
               color: tuple[int, int, int], label: str) -> None:
    h, w = img.shape[:2]
    pts = np.array([[round(x * w), round(y * h)] for x, y in corners], dtype=np.int32)
    cv2.polylines(img, [pts], True, color, 3)
    for (x, y), lbl in zip(pts, ["TL", "TR", "BR", "BL"]):
        cv2.circle(img, (int(x), int(y)), 7, color, -1)
    cv2.putText(img, label, (12, 28 if "PRED" in label else 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    entries = sorted(_GT_DIR.glob("*.json"))
    if prefix:
        entries = [e for e in entries if e.stem.startswith(prefix)]
    if not entries:
        print(f"No entries match prefix '{prefix}'")
        return 1

    for json_path in entries:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        vid = data["video_id"]
        name = data.get("video_name", vid)
        truth = data["latest_corners"]
        refframe = _GT_DIR / f"{vid}.jpg"

        img = cv2.imread(str(refframe))
        if img is None:
            print(f"  skip {vid} (no jpg)")
            continue
        h, w = img.shape[:2]

        det = roi_detector.detect_roi(refframe, exclude_video_id=vid)

        # Compose 3-panel image: original+quads | blue_mask | red_mask
        overlay = img.copy()
        _draw_quad(overlay, truth, (0, 255, 0), "GROUNDTRUTH (green)")
        _draw_quad(overlay, det.corners, (0, 0, 255), f"PRED: {det.method[:30]}")

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        blue_mask = cv2.inRange(hsv, np.array(roi_detector._TT_BLUE_LO),
                                np.array(roi_detector._TT_BLUE_HI))
        red_a = cv2.inRange(hsv, np.array(roi_detector._FLOOR_RED_LO_A),
                            np.array(roi_detector._FLOOR_RED_HI_A))
        red_b = cv2.inRange(hsv, np.array(roi_detector._FLOOR_RED_LO_B),
                            np.array(roi_detector._FLOOR_RED_HI_B))
        red_mask = cv2.bitwise_or(red_a, red_b)
        blue_bgr = cv2.cvtColor(blue_mask, cv2.COLOR_GRAY2BGR)
        red_bgr = cv2.cvtColor(red_mask, cv2.COLOR_GRAY2BGR)

        # Stack horizontally (resize each to common height)
        H = 360
        def fit(im):
            scale = H / im.shape[0]
            return cv2.resize(im, (int(im.shape[1] * scale), H))

        panel = np.hstack([fit(overlay), fit(blue_bgr), fit(red_bgr)])

        # Compute error
        err = sum(((det.corners[i][0] - truth[i][0]) ** 2 + (det.corners[i][1] - truth[i][1]) ** 2) ** 0.5
                  for i in range(4)) / 4.0
        meta = f"{name[:30]}  err={err:.3f}  conf={det.confidence:.2f}"
        cv2.putText(panel, meta, (12, panel.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

        out_path = _OUT_DIR / f"{vid}_{name.replace('/', '_').replace(':', '')}.jpg"
        cv2.imwrite(str(out_path), panel)
        print(f"{vid:18s}  {name:30s}  err={err:.4f}  →  {out_path.name}")

        # Also dump debug dict
        dbg_path = _OUT_DIR / f"{vid}.debug.json"
        dbg_path.write_text(json.dumps({
            "method": det.method,
            "confidence": det.confidence,
            "predicted": det.corners,
            "groundtruth": truth,
            "error": err,
            "debug": det.debug,
        }, indent=2, default=str), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
