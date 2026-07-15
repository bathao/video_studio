"""P(rally-in-progress) timeline cache (step 2 escalation, integration).

One GPU pass per match: decode ROI-band crops at 2 fps, classify each
frame with the trained rally-frame classifier, cache the probability
timeline as .npz next to the motion cache. Any segmentation logic can
then consult P(rally)(t) without touching the video again.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/rally_prob_cache.py [slug ...]
      (no args = all corpus matches)
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
    CROP, STACK_EMIT_EVERY, STACK_FPS, crop_box,
)

WEIGHTS = OUT_DIR / "cls_runs" / "rally_stack" / "weights" / "best.pt"
PROB_FPS = STACK_FPS / STACK_EMIT_EVERY  # one prob sample per emitted stack


def build_prob_timeline(slug: str, model) -> Path | None:
    slug_dir = ROOT / "dataset" / slug
    gt = json.loads((slug_dir / "groundtruth.json").read_text(encoding="utf-8"))
    video = next(p for p in slug_dir.iterdir() if p.stem == "source")
    key = video_key(video.resolve())
    meta_p = OUT_DIR / key / "meta.json"
    if not meta_p.is_file():
        print(f"{slug}: no motion cache — skipped")
        return None
    out_npz = OUT_DIR / key / "rally_prob.npz"
    if out_npz.is_file():
        return out_npz

    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    W, H = gt["source_video"]["width"], gt["source_video"]["height"]
    box = crop_box(meta["roi_corners"], W, H)
    bw, bh = box[2] - box[0], box[3] - box[1]

    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda", "-i", str(video),
        "-vf", f"fps={STACK_FPS},crop={bw}:{bh}:{box[0]}:{box[1]},scale={CROP}:{CROP}",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = CROP * CROP
    probs: list[float] = []
    batch: list[np.ndarray] = []

    def flush():
        if not batch:
            return
        results = model.predict(batch, imgsz=CROP, verbose=False)
        for r in results:
            names = r.names
            p = r.probs.data.tolist()
            rally_i = next(i for i, n in names.items() if n == "rally")
            probs.append(float(p[rally_i]))
        batch.clear()

    window: list[np.ndarray] = []
    i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            window.append(np.frombuffer(buf, dtype=np.uint8).reshape(CROP, CROP).copy())
            if len(window) > 3:
                window.pop(0)
            i += 1
            if len(window) < 3 or (i % STACK_EMIT_EVERY):
                continue
            batch.append(np.stack([window[0], window[1], window[2]], axis=-1))
            if len(batch) >= 128:
                flush()
        flush()
    finally:
        proc.stdout.close()
        proc.wait(timeout=10)

    if not probs:
        # Writing an empty rally_prob.npz poisons every downstream
        # consumer (mean over empty → nan) while looking like a cache.
        raise SystemExit(
            f"{slug}: decoded 0 frames (ffmpeg exit {proc.returncode}) — "
            "refusing to write an empty prob cache")
    np.savez_compressed(out_npz, prob=np.asarray(probs, dtype=np.float32),
                        fps=PROB_FPS)
    print(f"{slug}: {len(probs)} prob samples @ {PROB_FPS} fps -> {out_npz.name}")
    return out_npz


def main() -> int:
    from ultralytics import YOLO
    if not WEIGHTS.is_file():
        print(f"missing weights {WEIGHTS} — run train_frame_cls.py first")
        return 1
    model = YOLO(str(WEIGHTS))

    if len(sys.argv) > 1:
        slugs = sys.argv[1:]
    else:
        recs = [json.loads(l) for l in
                (ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        slugs = sorted({r["slug"] for r in recs if r["label_source"] == "dataset"})
    for slug in slugs:
        build_prob_timeline(slug, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
