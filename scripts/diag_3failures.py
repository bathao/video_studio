"""Diagnose why TL corner is biased left on NgoTruongPhu/aMinhthia/NgocHieu.

For each: extract 5 frames, run YOLO directly (raw polygon), then run
the full multiframe detector. Output:
  - Raw YOLO polygon area + bbox
  - Quad after _polygon_to_quad
  - Quad from minAreaRect fallback (for comparison)
  - Corner error vs GT for each
  - Overlay images saved to scripts/_inspect_cache/diag3_*
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.roi_yolo import _load_model_if_available, _polygon_to_quad

ARCHIVE = Path(r"D:/Table Tennis Video/2026/Match")
GT_DIR = Path("dataset/roi_groundtruth")
OUT = Path("scripts/_inspect_cache")
OUT.mkdir(parents=True, exist_ok=True)


def extract_frames(video: Path, n: int = 5):
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = []
    for pct in [0.1, 0.3, 0.5, 0.7, 0.9]:
        fi = int(total * pct)
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, fr = cap.read()
        if ok:
            frames.append((pct, fr))
    cap.release()
    return frames


def corner_err(q1, q2):
    a = np.array(q1, dtype=np.float64)
    b = np.array(q2, dtype=np.float64)
    best = float('inf')
    best_k = 0
    for k in range(4):
        c = np.roll(b, k, axis=0)
        e = float(np.mean(np.linalg.norm(a - c, axis=1)))
        if e < best:
            best = e
            best_k = k
    return best, best_k


def find_gt(name):
    for jf in GT_DIR.glob("*.json"):
        with open(jf, encoding='utf-8') as f:
            d = json.load(f)
        if d.get('video_name', '') == name:
            return d['latest_corners']
    return None


def quad_from_minAreaRect(polygon_xy):
    pts = polygon_xy.astype(np.float32).reshape(-1, 1, 2)
    hull = cv2.convexHull(pts)
    rect = cv2.minAreaRect(hull)
    return cv2.boxPoints(rect).astype(np.float32)


def draw_quad(img, quad_px, color, label):
    out = img.copy()
    if quad_px is not None:
        pts = quad_px.astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [pts], True, color, 3)
        for i, p in enumerate(quad_px.astype(np.int32)):
            cv2.circle(out, tuple(p), 8, color, -1)
            cv2.putText(out, str(i), tuple(p + np.array([10, -10])),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.putText(out, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
    return out


targets = ['0307_NgocHieu_0_3.MP4', '0307_NgoTruongPhu_2_3.MP4', '0418_aMinhthia_1.MP4']
model = _load_model_if_available()
print(f"Model loaded: {model is not None}")

for name in targets:
    video = ARCHIVE / name
    if not video.exists():
        print(f"MISSING: {video}")
        continue
    gt = find_gt(name)
    print(f"\n=== {name} ===")
    print(f"  GT: {[(round(p[0],3), round(p[1],3)) for p in gt]}")
    frames = extract_frames(video)
    for pct, fr in frames:
        h, w = fr.shape[:2]
        results = model.predict(fr, imgsz=640, conf=0.55, verbose=False)
        if not results or results[0].masks is None or results[0].boxes is None or len(results[0].boxes) == 0:
            print(f"  pct={pct} NO DETECTION")
            continue
        r0 = results[0]
        confs = r0.boxes.conf.cpu().numpy()
        bi = int(np.argmax(confs))
        bc = float(confs[bi])
        poly_xy = np.asarray(r0.masks.xy[bi], dtype=np.float32)
        # Polygon area
        n = len(poly_xy)
        poly_area = abs(sum(poly_xy[i][0]*poly_xy[(i+1)%n][1] - poly_xy[(i+1)%n][0]*poly_xy[i][1] for i in range(n))) / 2.0
        # x-range of mask
        x_min = poly_xy[:, 0].min()
        x_max = poly_xy[:, 0].max()
        # Run polygon_to_quad current
        quad_v3 = _polygon_to_quad(poly_xy)
        quad_minarea = quad_from_minAreaRect(poly_xy)
        # Normalize quads
        def norm_quad(q):
            return [[float(p[0])/w, float(p[1])/h] for p in q]
        qn_v3 = norm_quad(quad_v3) if quad_v3 is not None else None
        qn_ma = norm_quad(quad_minarea)
        # Order: top-most first, then by angle. Use the same _order_clockwise as in roi_yolo
        from backend.roi_yolo import _order_clockwise_from_tl_local
        if qn_v3 is not None:
            qn_v3 = _order_clockwise_from_tl_local(qn_v3)
        qn_ma = _order_clockwise_from_tl_local(qn_ma)
        err_v3, _ = corner_err(gt, qn_v3) if qn_v3 else (float('inf'), 0)
        err_ma, _ = corner_err(gt, qn_ma)
        print(f"  pct={pct} conf={bc:.3f}  poly_pts={n}  poly_area_frac={poly_area/(w*h):.3f}  x_range=[{x_min/w:.3f}..{x_max/w:.3f}]  err_v3={err_v3:.4f}  err_minArea={err_ma:.4f}")
        # Save overlay: raw polygon (cyan) + v3 quad (green) + minArea quad (yellow) + GT (red)
        overlay = fr.copy()
        # Raw polygon
        cv2.polylines(overlay, [poly_xy.astype(np.int32).reshape(-1, 1, 2)], True, (255, 255, 0), 2)
        # v3 quad
        if quad_v3 is not None:
            cv2.polylines(overlay, [quad_v3.astype(np.int32).reshape(-1, 1, 2)], True, (0, 255, 0), 3)
        # minArea
        cv2.polylines(overlay, [quad_minarea.astype(np.int32).reshape(-1, 1, 2)], True, (0, 255, 255), 2)
        # GT
        gt_px = np.array([[p[0]*w, p[1]*h] for p in gt], dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [gt_px], True, (0, 0, 255), 3)
        # Legend
        cv2.putText(overlay, f"cyan=raw poly  green=v3 quad  yellow=minArea  red=GT", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        cv2.putText(overlay, f"err_v3={err_v3:.4f}  err_minArea={err_ma:.4f}  conf={bc:.3f}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        ovp = OUT / f"diag3_{name.replace('.', '_')}_pct{int(pct*100)}.jpg"
        cv2.imwrite(str(ovp), overlay)
        print(f"    -> {ovp.absolute()}")
