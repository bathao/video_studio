"""Regression test for backend/roi_detector.py.

Runs leave-one-out across every entry in dataset/roi_groundtruth/.
For each entry: locates the source video, extracts 5 evenly-spaced
frames, runs detect_roi_multiframe, compares to truth.

Falls back to single-frame detect on the cached refframe if the video
file is missing.

Usage:
    python -X utf8 scripts/test_roi_detector.py
    python -X utf8 scripts/test_roi_detector.py \\
        --search-path "D:/Table Tennis Video/2026/Match" \\
        --alias "Tan.MP4=0510_NguyenKhanhTan_3-1.MP4" \\
        --alias "Ha.MP4=0510_HoangHuuHa_1-3.MP4" \\
        --alias "Dat.MP4=0406_DatDo_0-3.MP4"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Add repo root to path so `backend` imports resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend import roi as roi_detector  # noqa: E402

_GT_DIR = _REPO_ROOT / "dataset" / "roi_groundtruth"
_VIDEOS_DIR = _REPO_ROOT / "videos"
_FRAME_CACHE = _REPO_ROOT / "scripts" / "_multiframe_cache"
_FRAME_CACHE.mkdir(exist_ok=True)


_MULTIFRAME_N = 5
_MULTIFRAME_MARGIN = 0.10


def _locate_video(
    video_name: str,
    extra_search_paths: list[Path],
    aliases: dict[str, str],
) -> Path | None:
    """Search videos/ + extra paths recursively. Try aliases when given."""
    names_to_try = [video_name]
    if video_name in aliases:
        names_to_try.append(aliases[video_name])
    for name in names_to_try:
        for root in [_VIDEOS_DIR, *extra_search_paths]:
            if not root.exists():
                continue
            for p in root.rglob(name):
                if p.is_file():
                    return p
    return None


def _probe_duration(video: Path) -> float:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return float(proc.stdout.strip())
    except Exception:
        return 60.0


def _extract_multiframes(video: Path, video_id: str, max_w: int = 960) -> list[Path]:
    """Extract N evenly-spaced frames to scripts/_multiframe_cache/."""
    paths = [_FRAME_CACHE / f"{video_id}_f{i}.jpg" for i in range(_MULTIFRAME_N)]
    if all(p.exists() for p in paths):
        return paths

    dur = _probe_duration(video)
    step = (1.0 - 2 * _MULTIFRAME_MARGIN) / max(1, (_MULTIFRAME_N - 1))
    fractions = [_MULTIFRAME_MARGIN + i * step for i in range(_MULTIFRAME_N)]

    for i, frac in enumerate(fractions):
        out = paths[i]
        if out.exists():
            continue
        t = max(0.0, dur * frac)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale='min({max_w},iw)':-2",
            "-q:v", "3",
            str(out),
        ]
        subprocess.run(cmd, capture_output=True, text=True)

    return [p for p in paths if p.exists()]


def _mean_corner_error(predicted: list[list[float]], truth: list[list[float]]) -> float:
    """Rotation-invariant mean Euclidean error between two 4-corner quads.

    Both quads describe the same visual rectangle but their starting
    vertex may differ — operator stores corners in the order they
    clicked (TL → TR → BR → BL visually); detector pipelines reorder
    via `_order_clockwise_from_tl` which uses min(x+y) and can pick a
    different starting corner for perspective-trapezoid tables. We try
    all 4 cyclic rotations of `predicted` against `truth`, take the
    minimum mean distance. Corners that visually overlap → near-zero
    error regardless of label assignment.
    """
    best = float("inf")
    for shift in range(4):
        e = sum(
            ((predicted[(i + shift) % 4][0] - truth[i][0]) ** 2 +
             (predicted[(i + shift) % 4][1] - truth[i][1]) ** 2) ** 0.5
            for i in range(4)
        ) / 4.0
        if e < best:
            best = e
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--search-path", action="append", default=[],
                    help="Extra directory to recursively search for source "
                         "videos. Repeatable.")
    ap.add_argument("--alias", action="append", default=[],
                    help="Old=New name alias for renamed source files. Repeatable.")
    args = ap.parse_args()

    extra_paths = [Path(p) for p in args.search_path if Path(p).exists()]
    aliases: dict[str, str] = {}
    for a in args.alias:
        if "=" in a:
            old, new = a.split("=", 1)
            aliases[old.strip()] = new.strip()

    entries = sorted(_GT_DIR.glob("*.json"))
    if not entries:
        print(f"No groundtruth entries found in {_GT_DIR}")
        return 1

    print(f"Running multi-frame LOO across {len(entries)} dataset entries")
    print(f"  search paths: videos/ + {extra_paths}")
    print(f"  aliases: {aliases}\n")
    print(f"{'#':>2}  {'video':22s}  {'method':46s}  {'err':>6s}  {'conf':>5s}  notes")
    print("-" * 120)

    results = []
    for i, json_path in enumerate(entries, 1):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        vid = data["video_id"]
        name = data.get("video_name", vid)
        truth = data["latest_corners"]

        video = _locate_video(name, extra_paths, aliases)
        if video is not None:
            frames = _extract_multiframes(video, vid)
            if not frames:
                continue
            det = roi_detector.detect_roi_multiframe(frames, exclude_video_id=vid)
            n_frames = len(frames)
        else:
            # Fallback: use only the saved refframe (1-frame "multi")
            refframe = _GT_DIR / f"{vid}.jpg"
            det = roi_detector.detect_roi(refframe, exclude_video_id=vid)
            n_frames = 1

        err = _mean_corner_error(det.corners, truth)

        notes = []
        dbg = det.debug or {}
        if "cluster_size" in dbg:
            notes.append(f"cluster={dbg['cluster_size']}/{dbg['n_valid']}")
        if "color_contrast_agreement_iou" in dbg:
            notes.append(f"iou={dbg['color_contrast_agreement_iou']}")
        notes.append(f"frames={n_frames}")

        results.append({
            "video": name,
            "method": det.method,
            "err": err,
            "conf": det.confidence,
            "n_frames": n_frames,
        })
        method_short = det.method[:46]
        name_short = name[:22]
        print(f"{i:>2}  {name_short:22s}  {method_short:46s}  {err:6.4f}  {det.confidence:5.2f}  {' '.join(notes)}")

    print("-" * 120)
    if not results:
        print("No results — all videos missing?")
        return 1
    mean_err = sum(r["err"] for r in results) / len(results)
    max_err = max(r["err"] for r in results)
    by_method: dict[str, int] = {}
    for r in results:
        key = r["method"].split(":", 1)[0]
        by_method[key] = by_method.get(key, 0) + 1
    print(f"\nMean LOO error: {mean_err:.4f}")
    print(f"Max LOO error:  {max_err:.4f}")
    print(f"Method breakdown: {by_method}")

    threshold = 0.10
    bad = [r for r in results if r["err"] > threshold]
    if bad:
        print(f"\n{len(bad)} entries exceed err threshold {threshold}:")
        for r in bad:
            print(f"  {r['video']}  err={r['err']:.4f}  method={r['method']}")
        return 2

    print(f"\nAll {len(results)} entries within err ≤ {threshold}. OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
