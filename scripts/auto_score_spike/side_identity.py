"""Side-identity tracker spike v2 (plan §6 step 5 extension).

Detects side swaps by re-identifying the NEAR player across set
boundaries. v1 compared near-vs-far descriptors and failed (2/19
boundaries): the far crop is ~20 px of mostly floor, so descriptors
encoded the POSITION, not the person. v2 fixes both flaws:

- NEAR slot only — the near player is large and reliably cropped; the
  question becomes "is the near player of set k+1 the same person as
  the near player of set k?" (truth: never, by the swap rule).
- Keypoint-tight lower-body crop (hips->ankles from YOLO-pose), i.e.
  shorts + legs + shoes — operator 2026-07-08: shirts may change
  between sets, the lower body does not.
- Per-match calibrated threshold: within-set half-vs-half distances
  form the same-person null; a boundary distance counts as a SWAP
  when it exceeds `T = max(null) * MARGIN` for that match.

Measured against known truth: 19 set boundaries = swaps, 23 within-set
pairs = non-swaps, mid-set-5 verdicts from side_truth.json
(aTrung=swapped, 0611_Tim=not).

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/side_identity.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.config import config  # noqa: E402
from backend.ffmpeg_runner import probe_video  # noqa: E402
from scripts.auto_score_spike.motion_cache import OUT_DIR, video_key  # noqa: E402
from scripts.auto_score_spike.eval_segmentation import CORPUS_JSONL  # noqa: E402
from scripts.auto_score_spike.frame_dataset import playzone_polygon  # noqa: E402
from scripts.auto_score_spike.pose_features import _net_line, _side_of_net  # noqa: E402

SAMPLES_PER_HALF = 4      # frames sampled per set half
RALLY_LEAD_S = 3.0        # sample this long before the score press
FRAME_W = 640
H_BINS, S_BINS = 16, 8
MARGIN = 1.15             # swap iff boundary dist > max(within-set null) * MARGIN
KP_CONF = 0.3
# COCO keypoint indices
HIPS, KNEES, ANKLES = (11, 12), (13, 14), (15, 16)


def _extract_frame(video: Path, t: float, box, out_h: int) -> np.ndarray | None:
    bw, bh = box[2] - box[0], box[3] - box[1]
    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.2f}", "-i", str(video),
        "-vf", f"crop={bw}:{bh}:{box[0]}:{box[1]},scale={FRAME_W}:{out_h}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if len(raw) < FRAME_W * out_h * 3:
        return None
    return np.frombuffer(raw[:FRAME_W * out_h * 3], dtype=np.uint8).reshape(
        out_h, FRAME_W, 3).copy()


def _lower_body_hist(frame: np.ndarray, kp: np.ndarray) -> np.ndarray | None:
    """HSV histogram of the hips->ankles region, keypoint-cropped."""
    import cv2
    pts = []
    for grp in (HIPS, KNEES, ANKLES):
        for i in grp:
            if kp[i, 2] >= KP_CONF:
                pts.append(kp[i, :2])
    if len(pts) < 3:
        return None
    pts = np.asarray(pts)
    x1, y1 = pts.min(axis=0)
    x2, y2 = pts.max(axis=0)
    pad = 0.15 * max(x2 - x1, y2 - y1)
    x1, x2 = int(max(0, x1 - pad)), int(min(frame.shape[1], x2 + pad))
    y1, y2 = int(max(0, y1 - pad * 0.5)), int(min(frame.shape[0], y2 + pad))
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 4:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [H_BINS, S_BINS],
                        [0, 180, 0, 256]).flatten()
    n = hist.sum()
    return (hist / n).astype(np.float32) if n else None


def _dist(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return float("nan")
    bc = float(np.sqrt(a * b).sum())
    return float(np.sqrt(max(0.0, 1.0 - bc)))


def collect_near_descriptors(model, video: Path, roi_corners, events):
    """-> {(set_index, half): median lower-body hist of the NEAR player}."""
    info = probe_video(video)
    W, H = int(info["width"]), int(info["height"])
    poly = playzone_polygon(roi_corners)
    xs = [p[0] * W for p in poly]
    ys = [p[1] * H for p in poly]
    box = (max(0, int(min(xs))), max(0, int(min(ys))),
           min(W, int(max(xs))), min(H, int(max(ys))))
    bw, bh = box[2] - box[0], box[3] - box[1]
    out_h = max(2, int(round(FRAME_W * bh / bw / 2)) * 2)

    def to_crop(pt_norm):
        return np.array([(pt_norm[0] * W - box[0]) / bw * FRAME_W,
                         (pt_norm[1] * H - box[1]) / bh * out_h])

    ml_c, mr_c = (to_crop(p) for p in _net_line(roi_corners))

    by_set: dict[int, list[dict]] = {}
    for e in sorted(events, key=lambda r: r["t_event"]):
        by_set.setdefault(int(e["set_index"]), []).append(e)

    out: dict[tuple[int, int], np.ndarray | None] = {}
    for k, evs in sorted(by_set.items()):
        for half, hevs in {0: evs[: len(evs) // 2], 1: evs[len(evs) // 2:]}.items():
            picks = hevs[:SAMPLES_PER_HALF] if half == 0 else hevs[-SAMPLES_PER_HALF:]
            hists: list[np.ndarray] = []
            for e in picks:
                t = max(0.0, e["t_event"] - RALLY_LEAD_S)
                frame = _extract_frame(video, t, box, out_h)
                if frame is None:
                    continue
                res = model.predict(frame, imgsz=FRAME_W, verbose=False)[0]
                if res.keypoints is None or res.boxes is None:
                    continue
                best = None
                for conf, xyxy, kp in zip(res.boxes.conf.cpu().numpy(),
                                          res.boxes.xyxy.cpu().numpy(),
                                          res.keypoints.data.cpu().numpy()):
                    if conf < 0.4:
                        continue
                    c = np.array([(xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2])
                    if _side_of_net(c, ml_c, mr_c) <= 0:
                        continue  # far side — ignore entirely (v1 lesson)
                    if best is None or conf > best[0]:
                        best = (float(conf), kp)
                if best is None:
                    continue
                h = _lower_body_hist(frame, best[1])
                if h is not None:
                    hists.append(h)
            out[(k, half)] = np.median(np.stack(hists), axis=0) if hists else None
    return out


def main() -> int:
    from ultralytics import YOLO
    model = YOLO("yolov8n-pose.pt")

    side_truth = json.loads(
        (Path(__file__).parent / "side_truth.json").read_text(encoding="utf-8")
    )["matches"]
    recs = [json.loads(l) for l in CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[dict]] = {}
    for r in recs:
        if r["label_source"] == "dataset" and r["match_type"] == "single":
            by_video.setdefault(r["video"], []).append(r)

    boundary_ok = boundary_n = 0
    control_ok = control_n = 0
    set5_results = {}
    for video_rel, events in sorted(by_video.items()):
        video = ROOT / video_rel
        src_name = events[0]["id"].split("#")[0]
        slug = Path(video_rel).parent.name
        key = video_key(video.resolve())
        meta = json.loads((OUT_DIR / key / "meta.json").read_text(encoding="utf-8"))
        segs = collect_near_descriptors(model, video, meta["roi_corners"], events)
        sets = sorted({k for k, _ in segs})
        truth5 = side_truth.get(src_name, {}).get("set5_swap_at_5")

        # Same-person null: within-set half-vs-half distances (excluding
        # a set 5 that truly swapped mid-set).
        null: list[float] = []
        within: dict[int, float] = {}
        for k in sets:
            d = _dist(segs.get((k, 0)), segs.get((k, 1)))
            if np.isnan(d):
                continue
            within[k] = d
            if not (k == 4 and truth5):
                null.append(d)
        if len(null) < 2:
            print(f"{slug[:24]:24} insufficient descriptors — skipped")
            continue
        thr = max(null) * MARGIN

        for k in sets:
            if k not in within:
                continue
            if k == 4 and truth5 is not None:
                sw = within[k] > thr
                set5_results[src_name] = (sw, truth5, within[k], thr)
                continue
            # Leave-one-out control — judging a null member against a
            # threshold that includes itself would be trivially correct.
            others = [d2 for k2, d2 in within.items()
                      if k2 != k and not (k2 == 4 and truth5)]
            if not others:
                continue
            control_n += 1
            ok = within[k] <= max(others) * MARGIN
            control_ok += ok
            if not ok:
                print(f"{slug[:24]:24} set{k + 1} halves: d={within[k]:.3f} "
                      f"thr_loo={max(others) * MARGIN:.3f}  <-- FALSE SWAP")

        for k1, k2 in zip(sets, sets[1:]):
            d = _dist(segs.get((k1, 1)), segs.get((k2, 0)))
            if np.isnan(d):
                continue
            sw = d > thr
            boundary_n += 1
            boundary_ok += sw
            flag = "" if sw else "  <-- MISSED SWAP"
            print(f"{slug[:24]:24} set{k1 + 1}->set{k2 + 1}: d={d:.3f} thr={thr:.3f} swap={sw}{flag}")

    print(f"\nboundary swaps detected: {boundary_ok}/{boundary_n}")
    print(f"within-set non-swaps correct: {control_ok}/{control_n}")
    for src, (sw, truth, d, thr) in set5_results.items():
        verdict = "OK" if sw == truth else "WRONG"
        print(f"set-5 mid-swap {src}: d={d:.3f} thr={thr:.3f} "
              f"detected={sw} truth={truth} {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
