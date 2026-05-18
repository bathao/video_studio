"""
Analyze detector accuracy from dataset/roi_groundtruth/*.json history.

Each history entry records what the detector PROPOSED and what the operator
CONFIRMED. The delta is labeled error — group by detector_method tag to see
which tier is failing and by how much.

Run:
  ./venv/Scripts/python.exe -X utf8 scripts/analyze_detector_errors.py
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "dataset" / "roi_groundtruth"

FAIL_THRESHOLD = 0.05  # operator-visible error; >5% means clearly off


def rot_invariant_err(pred: list, truth: list) -> float:
    """Mean L2 distance between predicted and truth corners, minimised
    over the 4 cyclic rotations. Catches corner-label drift bugs while
    measuring true geometric error."""
    pred = [(float(x), float(y)) for x, y in pred]
    truth = [(float(x), float(y)) for x, y in truth]
    best = math.inf
    for k in range(4):
        rotated = pred[k:] + pred[:k]
        err = sum(
            math.hypot(rotated[i][0] - truth[i][0], rotated[i][1] - truth[i][1])
            for i in range(4)
        ) / 4.0
        best = min(best, err)
    return best


def summarize(label: str, errors: list[float]) -> str:
    if not errors:
        return f"{label}: (no data)"
    errors = sorted(errors)
    n = len(errors)
    mean = sum(errors) / n
    median = errors[n // 2]
    fails = sum(1 for e in errors if e > FAIL_THRESHOLD)
    return (
        f"{label:48s}  n={n:3d}  "
        f"mean={mean:.4f}  median={median:.4f}  "
        f"max={errors[-1]:.4f}  "
        f"fails(>{FAIL_THRESHOLD})={fails}/{n} ({100*fails/n:.0f}%)"
    )


def main() -> None:
    files = sorted(GT_DIR.glob("*.json"))
    print(f"Scanning {len(files)} groundtruth files in {GT_DIR}\n")

    # Per-method-tier error pool from ALL history entries
    by_method: dict[str, list[float]] = defaultdict(list)
    by_method_edited: dict[str, list[float]] = defaultdict(list)
    by_method_unedited: dict[str, list[float]] = defaultdict(list)
    # Confidence histogram per method
    conf_by_method: dict[str, list[float]] = defaultdict(list)

    # Per-video LATEST detector_proposed vs latest_corners (most useful —
    # one data point per real video, tells you current accuracy)
    latest_per_video: list[tuple[str, str, float, float, str]] = []

    total_history = 0
    edited_count = 0

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {f.name}: {e}")
            continue
        video_name = data.get("video_name", f.stem)
        history = data.get("history", [])
        latest_corners = data.get("latest_corners")

        for h in history:
            total_history += 1
            pred = h.get("detector_proposed")
            truth = h.get("corners")
            method = (h.get("detector_method") or "unknown").strip()
            conf = float(h.get("detector_confidence") or 0.0)
            was_edited = bool(h.get("was_edited", False))
            if not pred or not truth or len(pred) != 4 or len(truth) != 4:
                continue
            err = rot_invariant_err(pred, truth)
            by_method[method].append(err)
            conf_by_method[method].append(conf)
            if was_edited:
                edited_count += 1
                by_method_edited[method].append(err)
            else:
                by_method_unedited[method].append(err)

        # Latest record per video — what operator currently sees
        if history and latest_corners:
            h_last = history[-1]
            pred = h_last.get("detector_proposed")
            if pred and len(pred) == 4:
                err = rot_invariant_err(pred, latest_corners)
                method = (h_last.get("detector_method") or "unknown").strip()
                conf = float(h_last.get("detector_confidence") or 0.0)
                latest_per_video.append(
                    (video_name, method, err, conf, f.stem)
                )

    # ---- Report ----
    print(f"Total history entries: {total_history}")
    print(f"  was_edited=true:     {edited_count} ({100*edited_count/max(total_history,1):.0f}%)  ← detector wrong enough operator corrected")
    print(f"  was_edited=false:    {total_history - edited_count}  ← operator accepted as-is")
    print()

    print("=" * 100)
    print("Per-method error (ALL history entries)")
    print("=" * 100)
    for method in sorted(by_method.keys()):
        print(summarize(method, by_method[method]))
    print()

    print("=" * 100)
    print("Per-method error — WAS_EDITED=true subset (operator had to correct)")
    print("=" * 100)
    for method in sorted(by_method_edited.keys()):
        print(summarize(method, by_method_edited[method]))
    print()

    print("=" * 100)
    print("Per-method error — WAS_EDITED=false subset (operator accepted)")
    print("=" * 100)
    for method in sorted(by_method_unedited.keys()):
        print(summarize(method, by_method_unedited[method]))
    print()

    print("=" * 100)
    print("Confidence distribution per method (mean / min / max)")
    print("=" * 100)
    for method in sorted(conf_by_method.keys()):
        cs = conf_by_method[method]
        if not cs:
            continue
        print(f"  {method:60s}  n={len(cs):3d}  mean_conf={sum(cs)/len(cs):.3f}  range=[{min(cs):.3f},{max(cs):.3f}]")
    print()

    print("=" * 100)
    print("Worst 15 LATEST-per-video errors (one row per video)")
    print("=" * 100)
    latest_per_video.sort(key=lambda r: -r[2])
    for video, method, err, conf, vid in latest_per_video[:15]:
        flag = "✗" if err > FAIL_THRESHOLD else " "
        print(f"  {flag} err={err:.4f}  conf={conf:.3f}  [{method:40s}]  {video}  ({vid})")
    print()

    print("=" * 100)
    print("Best 5 LATEST-per-video errors")
    print("=" * 100)
    for video, method, err, conf, vid in latest_per_video[-5:]:
        flag = "✗" if err > FAIL_THRESHOLD else " "
        print(f"  {flag} err={err:.4f}  conf={conf:.3f}  [{method:40s}]  {video}")
    print()

    print("=" * 100)
    print("Latest-per-video summary")
    print("=" * 100)
    latest_errs = [r[2] for r in latest_per_video]
    print(summarize("LATEST per video", latest_errs))
    by_latest_method: dict[str, list[float]] = defaultdict(list)
    for _, method, err, _, _ in latest_per_video:
        by_latest_method[method].append(err)
    for method in sorted(by_latest_method.keys()):
        print("  " + summarize(method, by_latest_method[method]))


if __name__ == "__main__":
    main()
