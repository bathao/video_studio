"""Draw current ROI from spike_roi.json onto each refframe, save annotated PNGs
so we can verify ROI positioning visually."""
import json
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parent.parent
ROI = json.loads((REPO / "scripts/spike_roi.json").read_text(encoding="utf-8"))
OUT = REPO / "scripts/spike_out"
OUT.mkdir(parents=True, exist_ok=True)

manifest = json.loads((REPO / "dataset/manifest.json").read_text(encoding="utf-8"))
for entry in manifest["entries"]:
    slug = entry["slug"]
    refp = REPO / f"dataset/{slug}/refframe.png"
    if not refp.exists():
        print(f"missing: {refp}")
        continue
    img = cv2.imread(str(refp))
    if img is None:
        print(f"cv2 cannot read: {refp}")
        continue
    h, w = img.shape[:2]

    # Draw tight ROI (current)
    if slug in ROI:
        pts = np.array([[round(x * w), round(y * h)] for x, y in ROI[slug]], dtype=np.int32)
        overlay = img.copy()
        cv2.fillPoly(overlay, [pts], (0, 200, 0))
        img = cv2.addWeighted(overlay, 0.25, img, 0.75, 0)
        cv2.polylines(img, [pts], True, (0, 255, 0), 3)
        # Label each corner
        labels = ["TL", "TR", "BR", "BL"]
        for (x, y), lbl in zip(pts, labels):
            cv2.circle(img, (int(x), int(y)), 6, (0, 255, 255), -1)
            cv2.putText(img, lbl, (int(x) + 8, int(y) + 5), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 255), 2, cv2.LINE_AA)

    # Also draw loose ROI for comparison if available
    loose_key = f"_loose_{slug}"
    if loose_key in ROI:
        pts = np.array([[round(x * w), round(y * h)] for x, y in ROI[loose_key]], dtype=np.int32)
        cv2.polylines(img, [pts], True, (255, 150, 0), 2, lineType=cv2.LINE_4)

    # Grid for reference (every 10% with labels)
    for pct in range(10, 100, 10):
        gx = int(pct / 100 * w)
        gy = int(pct / 100 * h)
        cv2.line(img, (gx, 0), (gx, 12), (180, 180, 180), 1)
        cv2.line(img, (0, gy), (12, gy), (180, 180, 180), 1)
        cv2.putText(img, f"{pct}", (gx + 2, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (180, 180, 180), 1, cv2.LINE_AA)
        cv2.putText(img, f"{pct}", (14, gy + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (180, 180, 180), 1, cv2.LINE_AA)

    out = OUT / f"{slug}_roi_check.png"
    cv2.imwrite(str(out), img)
    print(f"-> {out}")
