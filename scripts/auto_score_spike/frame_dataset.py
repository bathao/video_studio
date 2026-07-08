"""Build the rally-in-progress frame dataset (Phase 0 step 2 escalation).

Auto-labels frames from data the operator already produced:

- POSITIVE  = frames inside groundtruth kept_segments whose motion is
  above the per-video p70 threshold -> a rally is visibly in progress
  (kept segments are anchored-detector output accepted via rendered
  output).
- NEG-HARD  = frames inside trim_segments with motion ABOVE p70 -> the
  exact confusable junk (ball fetch, toweling, warm-up) that caps the
  1-D signal at 59-90% recall.
- NEG-EASY  = low-motion trim frames (subsampled) for class variety.

Frames are cropped around the detected table ROI expanded upward to
include the players' bodies, resized to 224x224. Boundary frames
(within 2 s of a kept/trim edge) are skipped — label noise.

Held-out matches (corpus split=held_out) are extracted too but land in
out/frames/heldout_<slug>/ and must NEVER be trained on.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/frame_dataset.py [--only-cached]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.config import config  # noqa: E402
from backend.rally_detector import BALANCED, adaptive_threshold, smooth_motion  # noqa: E402
from scripts.auto_score_spike.motion_cache import OUT_DIR, video_key  # noqa: E402

FRAMES_DIR = OUT_DIR / "frames"
SAMPLE_FPS = 2.0
CROP = 224
EDGE_GUARD_S = 2.0
NEG_EASY_EVERY = 6  # keep 1 in N low-motion break frames

# --- v2: temporal-stack samples --------------------------------------
# v1 single static frames measured DEAD on held-out (AUC 0.578,
# medians inverted) — rally-vs-junk is a MOTION distinction a single
# frame cannot show. v2 stacks gray(t-0.4), gray(t), gray(t+0.4) into
# the RGB channels: motion becomes colored ghosting, statics stay gray.
FRAMES2_DIR = OUT_DIR / "frames_stack"
STACK_FPS = 2.5           # decode cadence -> neighbors are +-0.4 s
STACK_EMIT_EVERY = 2      # one sample every 0.8 s

# v3: refframes showed the expanded CROP is full of BACKGROUND TABLES
# with their own rallies (crowded multi-table halls — the operator's
# original warning). The stack ghosting then shows rally-like motion
# during OUR table's breaks -> labels look contradictory -> AUC caps
# at ~0.64. v3 blacks out everything outside a perspective-extended
# table polygon (own table + both players' zones) before stacking.
PLAYZONE_TOP_EXT = 0.9    # extend TL/TR along the table's long axis (far player)
PLAYZONE_BOT_EXT = 0.85   # extend BL/BR the other way — near player LOOMS large
                          # in perspective (refframe check 2026-07-08); below the
                          # table is just floor, so a big value costs nothing
PLAYZONE_SIDE_EXT = 0.25  # widen along the table's short axis (wide returns)


def playzone_polygon(roi_corners) -> list[list[float]]:
    """Table quad extended along ITS OWN axes (operator principle
    2026-07-08: the table ROI is the center of everything). Both the
    long-axis (Y: players) and short-axis (X: wide returns) extensions
    use the quad's edge vectors, so the expansion automatically follows
    the camera's perspective — no absolute pixel numbers. Background
    tables stay outside; normalized coords."""
    tl, tr, br, bl = [np.array(c, dtype=np.float64) for c in roi_corners]
    tl2 = tl + (tl - bl) * PLAYZONE_TOP_EXT + (tl - tr) * PLAYZONE_SIDE_EXT
    tr2 = tr + (tr - br) * PLAYZONE_TOP_EXT + (tr - tl) * PLAYZONE_SIDE_EXT
    bl2 = bl + (bl - tl) * PLAYZONE_BOT_EXT + (bl - br) * PLAYZONE_SIDE_EXT
    br2 = br + (br - tr) * PLAYZONE_BOT_EXT + (br - bl) * PLAYZONE_SIDE_EXT
    return [c.clip(0.0, 1.0).tolist() for c in (tl2, tr2, br2, bl2)]


def load_cached_meta(video: Path) -> dict | None:
    d = OUT_DIR / video_key(video.resolve())
    meta_p, npz = d / "meta.json", d / "motion.npz"
    if not (meta_p.is_file() and npz.is_file()):
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["motion"] = np.load(npz)["motion"]
    return meta


def crop_box(roi_corners, W: int, H: int) -> tuple[int, int, int, int]:
    """Expanded square-ish box: table bbox widened 15% sideways, 45% up
    (players' torsos), 10% down."""
    xs = [c[0] for c in roi_corners]
    ys = [c[1] for c in roi_corners]
    x0, x1 = min(xs) * W, max(xs) * W
    y0, y1 = min(ys) * H, max(ys) * H
    w, h = x1 - x0, y1 - y0
    x0 -= 0.15 * w
    x1 += 0.15 * w
    y0 -= 0.45 * h
    y1 += 0.10 * h
    return (max(0, int(x0)), max(0, int(y0)), min(W, int(x1)), min(H, int(y1)))


def in_any(t: float, segs: list[tuple[float, float]], guard: float) -> bool:
    return any(s + guard <= t <= e - guard for s, e in segs)


def extract_match(slug: str, split: str) -> dict | None:
    slug_dir = ROOT / "dataset" / slug
    gt = json.loads((slug_dir / "groundtruth.json").read_text(encoding="utf-8"))
    video = next(p for p in slug_dir.iterdir() if p.stem == "source")
    meta = load_cached_meta(video)
    if meta is None:
        return None

    out_name = (f"heldout_{slug}" if split == "held_out" else slug)
    out_dir = FRAMES_DIR / out_name
    manifest_p = out_dir / "manifest.json"
    if manifest_p.is_file():
        return json.loads(manifest_p.read_text(encoding="utf-8"))

    kept = [(s["start"], s["end"]) for s in gt["kept_segments"]]
    trims = [(s["start"], s["end"]) for s in gt["project"]["trim_segments"]]
    fps = float(meta["fps"])
    sm = smooth_motion(meta["motion"], int(round(BALANCED.smooth_window_s * fps)))
    thr_hi = adaptive_threshold(sm, 70.0)
    thr_lo = adaptive_threshold(sm, 40.0)
    W, H = gt["source_video"]["width"], gt["source_video"]["height"]
    box = crop_box(meta["roi_corners"], W, H)
    bw, bh = box[2] - box[0], box[3] - box[1]

    import cv2

    for sub in ("pos", "neg"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},crop={bw}:{bh}:{box[0]}:{box[1]},scale={CROP}:{CROP}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = CROP * CROP * 3
    counts = {"pos": 0, "neg_hard": 0, "neg_easy": 0}
    i = 0
    easy_i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            t = i / SAMPLE_FPS + 0.5 / SAMPLE_FPS
            i += 1
            mi = min(len(sm) - 1, int(t * fps))
            m = sm[mi]
            label = None
            if in_any(t, kept, EDGE_GUARD_S) and m >= thr_hi:
                label, sub = "pos", "pos"
            elif in_any(t, trims, EDGE_GUARD_S):
                if m >= thr_hi:
                    label, sub = "neg_hard", "neg"
                elif m < thr_lo:
                    easy_i += 1
                    if easy_i % NEG_EASY_EVERY == 0:
                        label, sub = "neg_easy", "neg"
            if label is None:
                continue
            img = np.frombuffer(buf, dtype=np.uint8).reshape(CROP, CROP, 3)
            cv2.imwrite(str(out_dir / sub / f"t{t:08.2f}_{label}.jpg"), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
            counts[label] += 1
    finally:
        proc.stdout.close()
        proc.wait(timeout=10)

    manifest = {"slug": slug, "split": split, "counts": counts, "crop_box": box}
    manifest_p.write_text(json.dumps(manifest, indent=1), encoding="utf-8", newline="\n")
    return manifest


FRAMES3_DIR = OUT_DIR / "frames_stack_masked"


def extract_match_stacked(slug: str, split: str) -> dict | None:
    """v3 extraction: temporal-stack samples with the playzone polygon
    mask applied (background tables blacked out), into FRAMES3_DIR.
    Labeling rules unchanged; sample timestamp = center frame's time."""
    slug_dir = ROOT / "dataset" / slug
    gt = json.loads((slug_dir / "groundtruth.json").read_text(encoding="utf-8"))
    video = next(p for p in slug_dir.iterdir() if p.stem == "source")
    meta = load_cached_meta(video)
    if meta is None:
        return None

    out_name = (f"heldout_{slug}" if split == "held_out" else slug)
    out_dir = FRAMES3_DIR / out_name
    manifest_p = out_dir / "manifest.json"
    if manifest_p.is_file():
        return json.loads(manifest_p.read_text(encoding="utf-8"))

    kept = [(s["start"], s["end"]) for s in gt["kept_segments"]]
    trims = [(s["start"], s["end"]) for s in gt["project"]["trim_segments"]]
    fps = float(meta["fps"])
    sm = smooth_motion(meta["motion"], int(round(BALANCED.smooth_window_s * fps)))
    thr_hi = adaptive_threshold(sm, 70.0)
    thr_lo = adaptive_threshold(sm, 40.0)
    W, H = gt["source_video"]["width"], gt["source_video"]["height"]

    import cv2

    poly = playzone_polygon(meta["roi_corners"])
    xs = [p[0] * W for p in poly]
    ys = [p[1] * H for p in poly]
    pad_x, pad_y = 0.02 * W, 0.02 * H
    box = (max(0, int(min(xs) - pad_x)), max(0, int(min(ys) - pad_y)),
           min(W, int(max(xs) + pad_x)), min(H, int(max(ys) + pad_y)))
    bw, bh = box[2] - box[0], box[3] - box[1]
    mask_pts = np.array(
        [[(px * W - box[0]) / bw * CROP, (py * H - box[1]) / bh * CROP]
         for px, py in poly], dtype=np.int32)
    zone_mask = np.zeros((CROP, CROP), dtype=np.uint8)
    cv2.fillPoly(zone_mask, [mask_pts], 1)

    for sub in ("pos", "neg"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda", "-i", str(video),
        "-vf", f"fps={STACK_FPS},crop={bw}:{bh}:{box[0]}:{box[1]},scale={CROP}:{CROP}",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = CROP * CROP
    counts = {"pos": 0, "neg_hard": 0, "neg_easy": 0}
    window: list[np.ndarray] = []
    i = 0
    easy_i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            gray = np.frombuffer(buf, dtype=np.uint8).reshape(CROP, CROP) * zone_mask
            window.append(gray)
            if len(window) > 3:
                window.pop(0)
            i += 1
            if len(window) < 3 or (i % STACK_EMIT_EVERY):
                continue
            t = (i - 2) / STACK_FPS  # center frame timestamp
            mi = min(len(sm) - 1, int(t * fps))
            m = sm[mi]
            label = None
            if in_any(t, kept, EDGE_GUARD_S) and m >= thr_hi:
                label, sub = "pos", "pos"
            elif in_any(t, trims, EDGE_GUARD_S):
                if m >= thr_hi:
                    label, sub = "neg_hard", "neg"
                elif m < thr_lo:
                    easy_i += 1
                    if easy_i % NEG_EASY_EVERY == 0:
                        label, sub = "neg_easy", "neg"
            if label is None:
                continue
            stack = np.stack([window[0], window[1], window[2]], axis=-1)
            cv2.imwrite(str(out_dir / sub / f"t{t:08.2f}_{label}.jpg"), stack,
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
            counts[label] += 1
    finally:
        proc.stdout.close()
        proc.wait(timeout=10)

    manifest = {"slug": slug, "split": split, "counts": counts, "crop_box": box,
                "mode": "stack3_masked", "stack_fps": STACK_FPS,
                "playzone": poly}
    manifest_p.write_text(json.dumps(manifest, indent=1), encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only-cached", action="store_true",
                    help="skip matches whose motion cache is not built yet")
    ap.add_argument("--stack", action="store_true",
                    help="v2 temporal-stack extraction into frames_stack/")
    args = ap.parse_args()

    recs = [json.loads(l) for l in
            (ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    seen = {}
    for r in recs:
        if r["label_source"] == "dataset":
            seen[r["slug"]] = r["split"]

    total = {"pos": 0, "neg_hard": 0, "neg_easy": 0}
    extract = extract_match_stacked if args.stack else extract_match
    for slug, split in sorted(seen.items()):
        m = extract(slug, split)
        if m is None:
            print(f"{slug}: motion cache missing -> {'skipped' if args.only_cached else 'SKIPPED (build cache first)'}")
            continue
        print(f"{slug} [{split}]: {m['counts']}")
        for k, v in m["counts"].items():
            total[k] += v
    print(f"TOTAL: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
