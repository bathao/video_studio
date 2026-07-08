"""Pose-based playzone features (segmentation ladder rung 6 + step 5 prep).

The pixel-level classifiers (static frame / temporal stack / masked
stack) all failed to generalize across venues (held-out 49.7-61.1%).
Pose keypoints are venue-invariant by construction: this script decodes
playzone crops at STACK_FPS, runs YOLOv8n-pose, and caches per-timestep
features describing WHERE PEOPLE ARE relative to the operator-anchored
table ROI:

  t, n_persons,
  near_present, near_dist, near_motion,   (person below the net line)
  far_present,  far_dist,  far_motion     (person above the net line)

- *_present: a person whose bbox center sits in that half of the zone
- *_dist:    normalized distance from the person's bbox center to the
             table quad centerline (0 = at the table)
- *_motion:  mean keypoint displacement vs the previous sampled frame
             (visible keypoints only) — a venue-free "activity" signal

Downstream: a tiny classifier on these features -> rally/junk filter;
first-seconds stillness pattern -> serve-side detection (step 5).

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/pose_features.py [slug ...]
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
from scripts.auto_score_spike.motion_cache import OUT_DIR, video_key  # noqa: E402
from scripts.auto_score_spike.frame_dataset import (  # noqa: E402
    STACK_FPS, playzone_polygon,
)

POSE_W = 640  # pose wants some resolution; crop is scaled to this width


def _net_line(corners: list[list[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Midpoints of the left/right table edges in normalized coords —
    the net line separating near/far halves."""
    tl, tr, br, bl = [np.array(c) for c in corners]
    return (tl + bl) / 2, (tr + br) / 2


def _side_of_net(pt: np.ndarray, ml: np.ndarray, mr: np.ndarray) -> float:
    """>0 below the net line (near half), <0 above (far half)."""
    d = mr - ml
    return float(np.cross(d, pt - ml))


def build_pose_features(slug: str, model) -> Path | None:
    slug_dir = ROOT / "dataset" / slug
    gt = json.loads((slug_dir / "groundtruth.json").read_text(encoding="utf-8"))
    video = next(p for p in slug_dir.iterdir() if p.stem == "source")
    key = video_key(video.resolve())
    meta_p = OUT_DIR / key / "meta.json"
    if not meta_p.is_file():
        print(f"{slug}: no motion cache — skipped")
        return None
    out_npz = OUT_DIR / key / "pose_features.npz"
    if out_npz.is_file():
        return out_npz

    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    W, H = gt["source_video"]["width"], gt["source_video"]["height"]
    poly = playzone_polygon(meta["roi_corners"])
    xs = [p[0] * W for p in poly]
    ys = [p[1] * H for p in poly]
    box = (max(0, int(min(xs))), max(0, int(min(ys))),
           min(W, int(max(xs))), min(H, int(max(ys))))
    bw, bh = box[2] - box[0], box[3] - box[1]
    out_h = max(2, int(round(POSE_W * bh / bw / 2)) * 2)

    def to_crop(pt_norm):
        return np.array([(pt_norm[0] * W - box[0]) / bw * POSE_W,
                         (pt_norm[1] * H - box[1]) / bh * out_h])

    ml, mr = _net_line(meta["roi_corners"])
    ml_c, mr_c = to_crop(ml), to_crop(mr)
    zone_c = np.array([to_crop(p) for p in poly], dtype=np.float32)
    import cv2
    zone_path = zone_c.astype(np.int32)
    diag = float(np.hypot(POSE_W, out_h))

    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda", "-i", str(video),
        "-vf", f"fps={STACK_FPS},crop={bw}:{bh}:{box[0]}:{box[1]},scale={POSE_W}:{out_h}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = POSE_W * out_h * 3

    rows: list[list[float]] = []
    prev_kp: dict[str, np.ndarray | None] = {"near": None, "far": None}
    i = 0
    batch: list[np.ndarray] = []

    def process(results):
        nonlocal prev_kp
        for r in results:
            t = (len(rows)) / STACK_FPS
            persons = []
            if r.keypoints is not None and r.boxes is not None:
                for kp, bconf, xyxy in zip(
                        r.keypoints.data.cpu().numpy(),
                        r.boxes.conf.cpu().numpy(),
                        r.boxes.xyxy.cpu().numpy()):
                    if bconf < 0.4:
                        continue
                    cx, cy = (xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2
                    if cv2.pointPolygonTest(zone_path, (float(cx), float(cy)), False) < 0:
                        continue
                    persons.append((np.array([cx, cy]), kp))
            feat = {"near": [0.0, 1.0, 0.0], "far": [0.0, 1.0, 0.0]}
            new_prev = {"near": None, "far": None}
            for center, kp in persons:
                side = "near" if _side_of_net(center, ml_c, mr_c) > 0 else "far"
                if feat[side][0] == 1.0:
                    continue  # keep the first (largest-conf) person per side
                d = np.cross(mr_c - ml_c, center - ml_c) / (np.linalg.norm(mr_c - ml_c) + 1e-9)
                vis = kp[:, 2] > 0.3
                motion = 0.0
                if prev_kp[side] is not None:
                    pv = prev_kp[side]
                    both = vis & (pv[:, 2] > 0.3)
                    if both.any():
                        motion = float(np.linalg.norm(
                            kp[both, :2] - pv[both, :2], axis=1).mean()) / diag
                feat[side] = [1.0, abs(float(d)) / diag, motion]
                new_prev[side] = kp
            prev_kp = new_prev
            rows.append([t, float(len(persons))] + feat["near"] + feat["far"])

    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            batch.append(np.frombuffer(buf, dtype=np.uint8).reshape(out_h, POSE_W, 3).copy())
            i += 1
            if len(batch) >= 32:
                process(model.predict(batch, imgsz=POSE_W, verbose=False))
                batch.clear()
        if batch:
            process(model.predict(batch, imgsz=POSE_W, verbose=False))
    finally:
        proc.stdout.close()
        proc.wait(timeout=10)

    arr = np.asarray(rows, dtype=np.float32)
    np.savez_compressed(out_npz, features=arr, fps=STACK_FPS,
                        columns=json.dumps([
                            "t", "n_persons",
                            "near_present", "near_dist", "near_motion",
                            "far_present", "far_dist", "far_motion"]))
    print(f"{slug}: {len(rows)} pose feature rows -> {out_npz.name}")
    return out_npz


def main() -> int:
    from ultralytics import YOLO
    model = YOLO("yolov8n-pose.pt")

    if len(sys.argv) > 1:
        slugs = sys.argv[1:]
    else:
        recs = [json.loads(l) for l in
                (ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        slugs = sorted({r["slug"] for r in recs
                        if r["label_source"] == "dataset"
                        and r["match_type"] == "single"})
    for slug in slugs:
        build_pose_features(slug, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
