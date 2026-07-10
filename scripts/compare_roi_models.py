"""Compare two YOLO ROI models on the confirmed groundtruth refframes.

Truth = the operator-confirmed `latest_corners` in each
`dataset/roi_groundtruth/<video_id>.json`; both models run through the
EXACT production inference path (`backend.roi_yolo.try_yolo_seg`,
including quad extraction + corner ordering) on the sibling `.jpg`
refframes. Error = mean normalized corner distance (min over the 4
cyclic rotations), reported in % of the frame.

Reports three blocks:
  1. ALL confirmed refframes — old vs new stats table.
  2. The subset confirmed AFTER the old model was trained (frames the
     old model never saw) — where flywheel learning should show first.
  3. A single machine-readable ``SUMMARY:`` line with a verdict
     (IMPROVED / TAIL IMPROVED / EQUIVALENT / REGRESSED).

Run automatically as the last step of every GUI-triggered retrain
(`backend/server/retrain.py` backs the previous weights up to
`assets/models/roi_seg.prev.pt`, trains, then invokes this script and
surfaces the SUMMARY line in the Auto Trim modal). Manual usage:

    python scripts/compare_roi_models.py                 # prev vs current
    python scripts/compare_roi_models.py --old runs/segment/roi_seg-6/weights/best.pt

Caveat to keep in mind when reading the numbers: both models have
typically TRAINED on most of these frames, so this is a
production-replay benchmark ("does the model reproduce operator truth
on known venues"), not a generalization test — the honest
generalization check is the first detect on a truly new venue.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

GT_DIR = ROOT / "dataset" / "roi_groundtruth"
CURRENT_PT = ROOT / "assets" / "models" / "roi_seg.pt"
PREV_PT = ROOT / "assets" / "models" / "roi_seg.prev.pt"

# "Good enough" threshold for the headline count: 2% of the frame is
# ~50 px at 2K — comfortably inside what the motion mask tolerates.
WITHIN_PCT = 2.0


def corner_err(pred, truth) -> float:
    """Mean corner distance in % of frame, min over cyclic rotations
    (both quads are ordered clockwise-from-TL; rotation-min guards
    against TL tie-breaks landing on a different corner)."""
    p = np.asarray(pred, dtype=np.float64)
    t = np.asarray(truth, dtype=np.float64)
    return min(
        float(np.linalg.norm(np.roll(p, k, axis=0) - t, axis=1).mean())
        for k in range(4)
    ) * 100.0


def load_entries(old_train_time: float) -> list[dict]:
    entries = []
    for j in sorted(GT_DIR.glob("*.json")):
        img_path = j.with_suffix(".jpg")
        if not img_path.exists():
            continue
        d = json.loads(j.read_text(encoding="utf-8"))
        truth = d.get("latest_corners")
        if not truth or len(truth) != 4:
            continue
        last_confirm = max(
            (float(h.get("confirmed_at", 0) or 0) for h in d.get("history", [])),
            default=0.0,
        )
        entries.append({
            "id": d.get("video_name", j.stem),
            "img": img_path,
            "truth": truth,
            "unseen_by_old": last_confirm > old_train_time,
        })
    return entries


def run_model(pt_path: Path, entries: list[dict]) -> dict[str, float | None]:
    """Errors per entry id; None = model produced no detection."""
    import backend.roi_yolo as ry

    ry._MODEL_PATH = Path(pt_path)
    ry.invalidate_model_cache()
    out: dict[str, float | None] = {}
    for e in entries:
        img = cv2.imread(str(e["img"]))
        if img is None:
            out[e["id"]] = None
            continue
        r = ry.try_yolo_seg(img)
        out[e["id"]] = None if r is None else corner_err(r["corners"], e["truth"])
    return out


def stats(errs_all: list[float | None]) -> dict:
    errs = [v for v in errs_all if v is not None]
    misses = sum(1 for v in errs_all if v is None)
    if not errs:
        return {"n": len(errs_all), "miss": misses, "mean": float("nan"),
                "median": float("nan"), "p90": float("nan"),
                "max": float("nan"), "within": 0}
    a = np.asarray(errs)
    return {
        "n": len(errs_all),
        "miss": misses,
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
        "within": int((a <= WITHIN_PCT).sum()),
    }


def verdict(old: dict, new: dict) -> str:
    """Coarse comparative call, tuned to catch what matters: a clearly
    better/worse mean, a regression in coverage, or a tail cleanup."""
    if new["miss"] > old["miss"]:
        return "REGRESSED (new model misses frames the old one caught)"
    if new["within"] < old["within"] - 1 or new["mean"] > old["mean"] + 0.30:
        return "REGRESSED (consider restoring roi_seg.prev.pt)"
    if new["mean"] < old["mean"] - 0.25:
        return "IMPROVED"
    if new["within"] > old["within"] or new["max"] < old["max"] - 0.20:
        return "TAIL IMPROVED (averages equal, worst cases better)"
    return "EQUIVALENT"


def fmt_row(tag: str, s: dict) -> str:
    return (f"  {tag}: n={s['n']} miss={s['miss']} mean={s['mean']:.2f}% "
            f"median={s['median']:.2f}% p90={s['p90']:.2f}% max={s['max']:.2f}% "
            f"| within {WITHIN_PCT:.0f}%: {s['within']}/{s['n'] - s['miss']}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", type=Path, default=None,
                    help="old model .pt (default: assets/models/roi_seg.prev.pt)")
    ap.add_argument("--new", type=Path, default=CURRENT_PT,
                    help="new model .pt (default: assets/models/roi_seg.pt)")
    args = ap.parse_args(argv)

    old_pt = args.old or PREV_PT
    if not old_pt.exists():
        print(f"old model not found: {old_pt} — nothing to compare against")
        return 2
    if not args.new.exists():
        print(f"new model not found: {args.new}")
        return 2

    entries = load_entries(old_pt.stat().st_mtime)
    if not entries:
        print("no confirmed refframes in dataset/roi_groundtruth — cannot compare")
        return 2
    subset = [e for e in entries if e["unseen_by_old"]]
    print(f"confirmed refframes: {len(entries)} "
          f"(confirmed after old model's training: {len(subset)})")

    t0 = time.time()
    old = run_model(old_pt, entries)
    new = run_model(args.new, entries)
    print(f"inference done in {time.time() - t0:.1f}s\n")

    so = stats([old[e["id"]] for e in entries])
    sn = stats([new[e["id"]] for e in entries])
    print("== ALL confirmed refframes ==")
    print(fmt_row("OLD", so))
    print(fmt_row("NEW", sn))

    if subset:
        print("\n== Confirmed AFTER old model's training (old never saw these) ==")
        print(fmt_row("OLD", stats([old[e["id"]] for e in subset])))
        print(fmt_row("NEW", stats([new[e["id"]] for e in subset])))
        print("\nPer-entry (err % old -> new):")
        for e in subset:
            o, n = old[e["id"]], new[e["id"]]
            fo = "miss" if o is None else f"{o:.2f}"
            fn = "miss" if n is None else f"{n:.2f}"
            print(f"  {e['id'][:48]:48s} {fo:>6} -> {fn:>6}")

    v = verdict(so, sn)
    detected = sn["n"] - sn["miss"]
    print(f"\nSUMMARY: {v} — within-{WITHIN_PCT:.0f}%: "
          f"{so['within']}->{sn['within']}/{detected}; "
          f"mean {so['mean']:.2f}->{sn['mean']:.2f}%; "
          f"worst {so['max']:.2f}->{sn['max']:.2f}%")
    # Leave the production model as the loaded one if this process's
    # cache is ever reused (defensive — normally we exit right after).
    import backend.roi_yolo as ry
    ry._MODEL_PATH = CURRENT_PT
    ry.invalidate_model_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
