"""Offline Step-1 verification for backend/rally_detector.py.

Runs the Balanced preset against the 3 spike dataset entries and compares
trim-coverage statistics to the PHASE0_REPORT numbers
(`J_fg_p70_rmin5`, recall 92-98%, extras 276-323s).

Usage:
    venv/Scripts/python scripts/verify_rally_detector.py
    venv/Scripts/python scripts/verify_rally_detector.py --entry match_001_20260516_215553

ROI is fetched via `backend.roi.detect_roi_multiframe` (the same call
the modal makes), so this script dogfoods the ROI auto-detect that was
unblocked on 2026-05-20 — no hardcoded corners.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.models import ScoreEvent, TrimSegment  # noqa: E402
from backend.rally_detector import BALANCED, run_rally_detection  # noqa: E402


SPIKE_ENTRIES = [
    "match_001_20260516_215553",  # E1: 0510_NguyenKhanhTan, 22 min, 75 events
    "match_001_20260516_223928",  # E2: 0510_HoangHuuHa,    20 min, 79 events
    "match_001_20260516_230801",  # E3: 0406_DatDo,         14 min, 65 events
]


# PHASE0_REPORT Balanced (J_fg_p70_rmin5) target numbers for sanity check.
# STALE: these targets predate the `post_match_keep_s=30` handshake-keep
# feature in rally_detector.py and now report false MISSes. Refresh the
# numbers before using this script as a gate again — see docs/TODO.md.
EXPECTED = {
    "match_001_20260516_215553": {"recall_min": 0.95, "extras_max": 380},
    "match_001_20260516_223928": {"recall_min": 0.88, "extras_max": 340},
    "match_001_20260516_230801": {"recall_min": 0.90, "extras_max": 320},
}


def _interval_total(intervals: list[tuple[float, float]]) -> float:
    return sum(max(0.0, e - s) for s, e in intervals)


def _interval_intersection(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> float:
    """Total seconds in (a ∩ b). Naive O(n+m); fine for ≤100 entries each."""
    total = 0.0
    i = j = 0
    a = sorted(a)
    b = sorted(b)
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            total += e - s
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def _extract_refframes(video_path: Path) -> list[Path]:
    """Reuse the server-side multi-refframe extractor + cache location."""
    from backend.server.routes_auto_trim import _extract_multi_refframes
    return _extract_multi_refframes(video_path)


def _detect_roi(video_path: Path) -> tuple[list[list[float]], str, float]:
    from backend.roi import detect_roi_multiframe
    frames = _extract_refframes(video_path)
    det = detect_roi_multiframe(frames)
    return det.corners, det.method, det.confidence


def _load_entry(slug: str) -> dict:
    base = REPO / "dataset" / slug
    gt = json.loads((base / "groundtruth.json").read_text(encoding="utf-8"))
    return {
        "slug": slug,
        "source": base / f"source{Path(gt['source_video']['path']).suffix}",
        "duration": float(gt["source_video"]["duration_sec"]),
        "events": [ScoreEvent(**ev) for ev in gt["project"]["score_events"]],
        "manual_trims": [
            (float(t["start"]), float(t["end"]))
            for t in gt["project"]["trim_segments"]
        ],
    }


def _verify_one(slug: str) -> dict:
    print(f"\n=== {slug} ===", flush=True)
    entry = _load_entry(slug)
    if not entry["source"].exists():
        print(f"  ! source missing: {entry['source']}")
        return {"slug": slug, "ok": False, "reason": "source missing"}

    print(f"  source: {entry['source'].name}  ({entry['duration']:.1f}s, "
          f"{len(entry['events'])} events, {len(entry['manual_trims'])} manual trims)",
          flush=True)

    t0 = time.time()
    corners, method, conf = _detect_roi(entry["source"])
    print(f"  ROI ({method}, conf={conf:.3f}) in {time.time() - t0:.1f}s:")
    for c in corners:
        print(f"    [{c[0]:.3f}, {c[1]:.3f}]")

    t0 = time.time()
    last_pct = [-1]

    def _emit(kind: str, data: dict) -> None:
        if kind == "progress" and data.get("frame_total"):
            pct = int(100 * data["frame_n"] / data["frame_total"])
            if pct - last_pct[0] >= 10:
                print(f"    decode {pct}%  ({data['frame_n']}/{data['frame_total']})",
                      flush=True)
                last_pct[0] = pct
        elif kind == "stage":
            print(f"    stage: {data}", flush=True)
        elif kind == "done":
            print(f"    done: {data}", flush=True)

    trims = run_rally_detection(
        entry["source"], corners, entry["events"], BALANCED,
        emit=_emit,
    )
    detect_time = time.time() - t0

    auto = [(t.start, t.end) for t in trims]
    manual = entry["manual_trims"]

    inter = _interval_intersection(auto, manual)
    manual_total = _interval_total(manual)
    auto_total = _interval_total(auto)
    extras = auto_total - inter
    recall = (inter / manual_total) if manual_total > 0 else 0.0

    target = EXPECTED.get(slug, {})
    recall_ok = recall >= target.get("recall_min", 0)
    extras_ok = extras <= target.get("extras_max", float("inf"))

    print(f"  result: {len(auto)} trims, total {auto_total:.1f}s "
          f"({auto_total / entry['duration'] * 100:.1f}% of video)")
    print(f"    recall:  {recall:.3f}  (target ≥ {target.get('recall_min', '?'):.2f})  "
          f"{'OK' if recall_ok else 'MISS'}")
    print(f"    extras:  {extras:.1f}s  (target ≤ {target.get('extras_max', '?')}s)  "
          f"{'OK' if extras_ok else 'MISS'}")
    print(f"    detect:  {detect_time:.1f}s for {entry['duration']:.0f}s source  "
          f"({entry['duration'] / detect_time:.1f}x realtime)")

    return {
        "slug": slug,
        "ok": recall_ok and extras_ok,
        "recall": recall,
        "extras": extras,
        "trims": len(auto),
        "detect_time_s": detect_time,
        "method": method,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", help="single slug to verify (default: all 3 spike entries)")
    args = ap.parse_args()

    slugs = [args.entry] if args.entry else SPIKE_ENTRIES
    results = [_verify_one(s) for s in slugs]

    print("\n=== Summary ===")
    for r in results:
        status = "OK  " if r.get("ok") else "MISS"
        if not r.get("ok") and "reason" in r:
            print(f"  {status}  {r['slug']}: {r['reason']}")
        else:
            print(f"  {status}  {r['slug']}: "
                  f"recall={r['recall']:.3f}  extras={r['extras']:.1f}s  "
                  f"trims={r['trims']}  ({r['method']})")

    ok_count = sum(1 for r in results if r.get("ok"))
    print(f"\nResult: {ok_count}/{len(results)} pass")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
