"""Train the rally-in-progress frame classifier (step 2 escalation).

Consumes the auto-labeled crops from frame_dataset.py:
  out/frames/<slug>/{pos,neg}/*.jpg          train-pool matches
  out/frames/heldout_<slug>/{pos,neg}/*.jpg  held-out (TEST ONLY)

Assembles an ultralytics classify dataset (rally / junk), trains
YOLOv8n-cls, then reports:
  - val accuracy (1 train-pool match held out as val)
  - TEST accuracy on the 2 held-out matches (never trained, never tuned)

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/train_frame_cls.py [--epochs 15]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import OUT_DIR  # noqa: E402

FRAMES_DIR = OUT_DIR / "frames_stack_masked"  # v3 masked stacks
CLS_DIR = OUT_DIR / "frames_stack_masked_cls"
VAL_SLUG_HINT = "aTrung_20260528_232216"  # best neg balance of the train pool

# Operator directive 2026-07-08: focus singles; doubles data stays on
# disk but is excluded from training AND testing until doubles gets
# its own pass.
DOUBLES_SLUGS = {"match_001_20260531_224626", "match_001_20260605_114322"}


def assemble() -> dict:
    if CLS_DIR.exists():
        shutil.rmtree(CLS_DIR)
    counts: dict[str, int] = {}
    for match_dir in sorted(FRAMES_DIR.iterdir()):
        if not match_dir.is_dir():
            continue
        if match_dir.name.removeprefix("heldout_") in DOUBLES_SLUGS:
            continue
        if match_dir.name.startswith("heldout_"):
            split = "test"
        elif match_dir.name == VAL_SLUG_HINT:
            split = "val"
        else:
            split = "train"
        for sub, cls in (("pos", "rally"), ("neg", "junk")):
            src = match_dir / sub
            if not src.is_dir():
                continue
            dst = CLS_DIR / split / cls
            dst.mkdir(parents=True, exist_ok=True)
            for jpg in src.glob("*.jpg"):
                link = dst / f"{match_dir.name}_{jpg.name}"
                try:
                    import os
                    os.link(jpg, link)  # hardlink — zero copy cost
                except OSError:
                    shutil.copy2(jpg, link)
                counts[f"{split}/{cls}"] = counts.get(f"{split}/{cls}", 0) + 1
    return counts


def evaluate(model, split_dir: Path) -> tuple[float, dict]:
    """Per-class accuracy on a {rally,junk} folder tree."""
    per = {}
    correct = total = 0
    for cls in ("rally", "junk"):
        files = sorted((split_dir / cls).glob("*.jpg"))
        if not files:
            continue
        c = 0
        for i in range(0, len(files), 256):
            batch = [str(f) for f in files[i:i + 256]]
            for r in model.predict(batch, imgsz=224, verbose=False):
                names = r.names
                if names[int(r.probs.top1)] == cls:
                    c += 1
        per[cls] = c / len(files)
        correct += c
        total += len(files)
    return (correct / total if total else 0.0), per


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--epochs", type=int, default=15)
    args = ap.parse_args()

    counts = assemble()
    print("dataset:", json.dumps(counts, indent=1))
    if not (CLS_DIR / "train").is_dir() or not (CLS_DIR / "val").is_dir():
        print("missing train/val splits — run frame_dataset.py first")
        return 1
    # Directory existence is not content: empty dirs pass the check
    # above and YOLO then "trains" on nothing.
    for split in ("train", "val"):
        if not any(k.startswith(f"{split}/") and v for k, v in counts.items()):
            print(f"ERROR: {split} split has 0 images — "
                  "run frame_dataset.py first", file=sys.stderr)
            return 1

    from ultralytics import YOLO

    model = YOLO("yolov8n-cls.pt")
    model.train(
        data=str(CLS_DIR), epochs=args.epochs, imgsz=224, batch=128,
        project=str(OUT_DIR / "cls_runs"), name="rally_stack", exist_ok=True,
        verbose=False,
        # The RGB channels ENCODE TIME (t-0.4 / t / t+0.4). Color-space
        # augmentation scrambles that encoding — disable everything
        # that touches channel values; keep only spatial hflip.
        auto_augment=None, erasing=0.0, hsv_h=0.0, hsv_s=0.0, hsv_v=0.0,
        fliplr=0.5, crop_fraction=1.0,
    )

    best = OUT_DIR / "cls_runs" / "rally_stack" / "weights" / "best.pt"
    model = YOLO(str(best))
    for split in ("val", "test"):
        d = CLS_DIR / split
        if not d.is_dir():
            continue
        acc, per = evaluate(model, d)
        if not per:
            print(f"{split}: EMPTY split — no images to evaluate")
            continue
        print(f"{split}: overall {acc:.1%}  per-class {json.dumps({k: round(v, 3) for k, v in per.items()})}")
    print(f"weights: {best}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
