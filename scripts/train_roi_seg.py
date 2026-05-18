"""Train (or re-train) the YOLOv8-seg foreground-table segmentation model.

Reads `dataset/yolo_seg/data.yaml` (built by `build_yolo_dataset.py`),
fine-tunes YOLOv8n-seg with heavy augmentation tuned for our small-
dataset / single-class scenario, and writes the best checkpoint to
`assets/models/roi_seg.pt`.

Defaults are chosen for the operator's local setup (RTX 5060 Ti, 16GB).
Override via CLI flags. Designed to complete in 30-60 min on first run,
much less on subsequent re-trains.

Usage:
    # Standard training (after build_yolo_dataset.py):
    python -X utf8 scripts/train_roi_seg.py

    # Faster sanity-check run (fewer epochs):
    python -X utf8 scripts/train_roi_seg.py --epochs 30

    # CPU-only fallback (slow; ~2-3x training time):
    python -X utf8 scripts/train_roi_seg.py --device cpu

Expected output:
    runs/segment/roi_seg_*/    — training logs, plots, weights
    assets/models/roi_seg.pt   — copy of best.pt (what the backend loads)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA_YAML = _REPO_ROOT / "dataset" / "yolo_seg" / "data.yaml"
_MODEL_OUT = _REPO_ROOT / "assets" / "models" / "roi_seg.pt"
_RUNS_DIR = _REPO_ROOT / "runs" / "segment"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--epochs", type=int, default=120,
                    help="Training epochs (default 120). With small datasets,"
                         " more epochs + heavy augmentation > fewer epochs.")
    ap.add_argument("--imgsz", type=int, default=640,
                    help="Image size (default 640). 480 is faster, 800 sharper.")
    ap.add_argument("--batch", type=int, default=8,
                    help="Batch size (default 8 for ~10GB VRAM headroom).")
    ap.add_argument("--device", default="0",
                    help='CUDA device id or "cpu" (default "0").')
    ap.add_argument("--base", default="yolov8n-seg.pt",
                    help="Base model for transfer learning (default yolov8n-seg.pt;"
                         " 's', 'm' variants are larger + slower).")
    ap.add_argument("--name", default="roi_seg",
                    help="Run name (saved under runs/segment/<name>).")
    ap.add_argument("--patience", type=int, default=30,
                    help="Early-stop patience (epochs without val improvement).")
    args = ap.parse_args()

    if not _DATA_YAML.exists():
        print(f"ERROR: {_DATA_YAML} not found.")
        print("Run `python -X utf8 scripts/build_yolo_dataset.py` first.")
        return 1

    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError:
        print("ERROR: `ultralytics` not installed.")
        print("Run: pip install -r requirements.txt")
        return 1

    print(f"Loading base model: {args.base}")
    model = YOLO(args.base)

    print(f"Training on {_DATA_YAML}")
    print(f"  epochs={args.epochs} imgsz={args.imgsz} batch={args.batch} device={args.device}")
    print()

    # Augmentation tuned for our small-dataset / single-class scenario:
    # - mosaic 1.0: composite 4 images per training step → multiplies
    #   effective dataset size, exposes model to varied backgrounds
    # - mixup 0.15: blend pairs → strong regularizer
    # - hsv_h/s/v: simulate camera white-balance + lighting drift across
    #   different venues. Strong values (0.02 / 0.7 / 0.6) since the
    #   operator records under wildly different sodium / LED / mixed lighting
    # - degrees 8 / translate 0.10 / scale 0.30: geometric jitter mimics
    #   small tripod re-positioning between matches
    # - perspective 0.0008: subtle 3D warp; tables are already seen at
    #   strong perspective so we keep this small to avoid distortion
    # - flipud 0 / fliplr 0.5: horizontal flip is fine (tables symmetric);
    #   vertical flip would put red floor at top → unphysical, breaks
    #   the "table on red floor" learned prior
    results = model.train(
        data=str(_DATA_YAML),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(_RUNS_DIR.parent),
        name=f"segment/{args.name}",
        exist_ok=False,
        patience=args.patience,
        # Augmentation
        mosaic=1.0,
        mixup=0.15,
        hsv_h=0.02,
        hsv_s=0.7,
        hsv_v=0.6,
        degrees=8.0,
        translate=0.10,
        scale=0.30,
        perspective=0.0008,
        flipud=0.0,
        fliplr=0.5,
        # Loss weights — bias toward mask accuracy (we don't care about
        # box accuracy directly, the downstream `roi_yolo.py` extracts
        # corners from the segmentation mask polygon).
        box=4.0,
        cls=0.5,
        dfl=1.5,
        # Optimizer
        optimizer="auto",
        lr0=0.01,
        cos_lr=True,
        # Misc
        plots=True,
        seed=42,
        verbose=True,
    )

    # Find the best checkpoint and copy it to assets/models/.
    save_dir = Path(results.save_dir)  # ultralytics returns the run dir
    best = save_dir / "weights" / "best.pt"
    if not best.exists():
        print(f"WARNING: best.pt not found at {best}")
        print(f"Check {save_dir} for available weights.")
        return 2

    _MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, _MODEL_OUT)
    print()
    print(f"✓ Best model copied to {_MODEL_OUT}")
    print(f"  Source run: {save_dir}")
    print()
    print("Restart the backend to pick up the new model:")
    print("  - Close run.bat window, then re-launch run.bat")
    print("  - First detect call will lazy-load the model (~1-2 s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
