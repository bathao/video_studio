"""Diagnose the 2 bad cases:
  - 0331_Trung_1_2-3.MP4 (CONFIRMED, has GT) - detector err only 0.0104
  - 0502_vs_VanCuong_3-2_FS.MP4 (NOT confirmed)
"""
import json, sys
from pathlib import Path
import cv2, numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.roi_detector import detect_roi_multiframe
from backend import server as srv

ARCHIVE = Path(r"D:/Table Tennis Video/2026/Match")
GT_DIR = Path("dataset/roi_groundtruth")
OUT = Path("scripts/_inspect_cache")
OUT.mkdir(parents=True, exist_ok=True)

def find_gt(name):
    for jf in GT_DIR.glob("*.json"):
        with open(jf, encoding='utf-8') as f:
            d = json.load(f)
        if d.get('video_name', '') == name:
            return d['latest_corners']
    return None

for name in ['0331_Trung_1_2-3.MP4', '0502_vs_VanCuong_3-2_FS.MP4']:
    video = ARCHIVE / name
    if not video.exists():
        print(f"MISSING: {video}")
        continue
    print(f"\n=== {name} ===")
    refp = srv._extract_refframe(video)
    img = cv2.imread(str(refp))
    h, w = img.shape[:2]
    multi = srv._extract_multi_refframes(video)
    print(f"  Refframe: {w}x{h}, multi frames: {len(multi)}")

    # Show every multi-frame's individual detection
    from backend.roi_detector import detect_roi
    print(f"  Per-frame:")
    for i, fp in enumerate(multi):
        det1 = detect_roi(fp)
        c = det1.corners
        print(f"    f{i}: method={det1.method}  conf={det1.confidence:.3f}  corners=[{c[0][0]:.2f},{c[0][1]:.2f}]..[{c[2][0]:.2f},{c[2][1]:.2f}]")

    # Full multiframe
    det = detect_roi_multiframe(multi)
    print(f"  AGGREGATE: method={det.method}  conf={det.confidence:.3f}")
    print(f"    corners: {[(round(p[0],3), round(p[1],3)) for p in det.corners]}")
    gt = find_gt(name)
    if gt:
        a = np.array(gt); b = np.array(det.corners)
        best = min(float(np.mean(np.linalg.norm(a - np.roll(b, k, axis=0), axis=1))) for k in range(4))
        print(f"    GT corners: {[(round(p[0],3), round(p[1],3)) for p in gt]}")
        print(f"    corner_err vs GT: {best:.4f}")
    else:
        print(f"    NO GT (not yet confirmed)")

    # Save overlay
    overlay = img.copy()
    proposed_px = np.array([[p[0]*w, p[1]*h] for p in det.corners], dtype=np.int32).reshape(-1, 1, 2)
    cv2.polylines(overlay, [proposed_px], True, (0, 255, 0), 3)
    for i, p in enumerate(proposed_px.reshape(-1, 2)):
        cv2.circle(overlay, tuple(p), 8, (0, 255, 0), -1)
        cv2.putText(overlay, ["TL","TR","BR","BL"][i], tuple(p + np.array([10, -10])),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    if gt:
        gt_px = np.array([[p[0]*w, p[1]*h] for p in gt], dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [gt_px], True, (0, 0, 255), 3)
    outp = OUT / f"diag_2bad_{name.replace('.', '_')}.jpg"
    cv2.imwrite(str(outp), overlay)
    print(f"  -> {outp.absolute()}")
