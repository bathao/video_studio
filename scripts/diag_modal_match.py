"""Reproduce EXACTLY what the modal sees:
  - Background: result of server._extract_refframe (midpoint via ffmpeg)
  - Polygon overlay: result of detect_roi_multiframe (5 frames)
  - GT corners overlay (from dataset/roi_groundtruth/)

Saves to scripts/_inspect_cache/modal_match_<name>.jpg
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.roi_detector import detect_roi_multiframe
from backend import server as srv

ARCHIVE = Path(r"D:/Table Tennis Video/2026/Match")
GT_DIR = Path("dataset/roi_groundtruth")
OUT = Path("scripts/_inspect_cache")
OUT.mkdir(parents=True, exist_ok=True)

targets = ['0307_NgocHieu_0_3.MP4', '0307_NgoTruongPhu_2_3.MP4', '0418_aMinhthia_1.MP4']


def find_gt(name):
    for jf in GT_DIR.glob("*.json"):
        with open(jf, encoding='utf-8') as f:
            d = json.load(f)
        if d.get('video_name', '') == name:
            return d['latest_corners']
    return None


for name in targets:
    video = ARCHIVE / name
    if not video.exists():
        print(f"MISSING: {video}")
        continue
    print(f"\n=== {name} ===")
    # 1) Get the refframe the modal would display
    refp = srv._extract_refframe(video)
    print(f"  refframe: {refp.absolute()}")
    img = cv2.imread(str(refp))
    h, w = img.shape[:2]
    print(f"  refframe size: {w}x{h}")

    # 2) Extract multi-refframes and run full multiframe detector
    multi = srv._extract_multi_refframes(video)
    print(f"  multi frames: {len(multi)}")
    det = detect_roi_multiframe(multi)
    if det is None:
        print(f"  detect failed")
        continue
    print(f"  method: {det.method}  confidence: {det.confidence:.3f}")
    print(f"  corners: {[(round(p[0],3), round(p[1],3)) for p in det.corners]}")

    # 3) Overlay on refframe
    overlay = img.copy()
    proposed_px = np.array([[p[0]*w, p[1]*h] for p in det.corners], dtype=np.int32).reshape(-1, 1, 2)
    cv2.polylines(overlay, [proposed_px], True, (0, 255, 0), 3)
    for i, p in enumerate(proposed_px.reshape(-1, 2)):
        cv2.circle(overlay, tuple(p), 8, (0, 255, 0), -1)
        cv2.putText(overlay, ["TL","TR","BR","BL"][i], tuple(p + np.array([10, -10])),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    gt = find_gt(name)
    if gt:
        gt_px = np.array([[p[0]*w, p[1]*h] for p in gt], dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [gt_px], True, (0, 0, 255), 3)
        # corner_err
        a = np.array(gt, dtype=np.float64)
        b = np.array(det.corners, dtype=np.float64)
        best = float('inf')
        for k in range(4):
            c = np.roll(b, k, axis=0)
            e = float(np.mean(np.linalg.norm(a - c, axis=1)))
            if e < best:
                best = e
        cv2.putText(overlay, f"green=detector  red=GT  err={best:.4f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
        cv2.putText(overlay, f"method={det.method}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        print(f"  corner_err vs GT: {best:.4f}")

    outp = OUT / f"modal_match_{name.replace('.', '_')}.jpg"
    cv2.imwrite(str(outp), overlay)
    print(f"  -> {outp.absolute()}")
