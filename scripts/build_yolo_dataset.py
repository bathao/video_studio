"""Convert `dataset/roi_groundtruth/*.json` + sibling .jpg refframes into
YOLOv8-seg format under `dataset/yolo_seg/`.

YOLOv8-seg expects:
    dataset/yolo_seg/
        images/train/<name>.jpg
        images/val/<name>.jpg
        labels/train/<name>.txt    # "<class_id> x1 y1 x2 y2 ..." normalized
        labels/val/<name>.txt
        data.yaml                  # paths + class names

Single class: "foreground_table" (class id 0).

Train/val split is deterministic by video_id hash so re-runs put the
same videos (with ALL their frames) in the same split — no leakage of
the same match across train↔val.

Multi-frame extraction (`--frames-per-video N`, default 5): operator
records on a FIXED tripod, so a single match's ROI corners apply to
every frame of that video. Extracting N evenly-spaced frames per video
multiplies the effective training set N× with REAL variance that
augmentation can't synthesize: different player positions / occlusions,
ball locations, scoreboard states, spectator background. Missing source
videos fall back to the single cached refframe.

Usage:
    python -X utf8 scripts/build_yolo_dataset.py             # default: 5 frames/video, 80/20 split
    python -X utf8 scripts/build_yolo_dataset.py --frames-per-video 1   # legacy single-frame mode
    python -X utf8 scripts/build_yolo_dataset.py --val 0.15
    python -X utf8 scripts/build_yolo_dataset.py --force     # rebuild from scratch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GT_DIR = _REPO_ROOT / "dataset" / "roi_groundtruth"
_OUT_DIR = _REPO_ROOT / "dataset" / "yolo_seg"
_VIDEOS_DIR = _REPO_ROOT / "videos"

# Match the inference-time multiframe sampling so training distribution
# = inference distribution: 10/30/50/70/90% of duration (margin 0.10).
_SAMPLE_MARGIN = 0.10


def _split_for(split_key: str, val_frac: float) -> str:
    """Deterministic train/val assignment by hash of split_key.
    Caller is responsible for picking a key that groups same-source entries
    together — e.g. alias-resolved video name — so renamed/duplicate
    confirms of the same video can't leak between train and val.
    """
    h = int(hashlib.sha1(split_key.encode("utf-8")).hexdigest()[:8], 16)
    return "val" if (h % 10000) < int(val_frac * 10000) else "train"


def _format_label_line(corners: list[list[float]]) -> str:
    """YOLO seg format: '<class_id> x1 y1 x2 y2 x3 y3 x4 y4' (normalized).

    The corners are already in normalized [0, 1] space and ordered
    TL → TR → BR → BL by `_order_clockwise_from_tl` at confirm time, so
    we can emit them directly. The polygon is closed implicitly by the
    parser (last vertex connects back to first).
    """
    coords: list[str] = ["0"]  # class_id 0 = foreground_table
    for x, y in corners:
        # Clamp into [0, 1] to be safe — operator could drag a corner
        # slightly off-frame; YOLO trainer rejects out-of-bounds labels.
        x = max(0.0, min(1.0, float(x)))
        y = max(0.0, min(1.0, float(y)))
        coords.append(f"{x:.6f}")
        coords.append(f"{y:.6f}")
    return " ".join(coords)


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _locate_video(
    video_name: str,
    stored_path: str | None,
    extra_search_paths: list[Path],
    aliases: dict[str, str] | None = None,
) -> Path | None:
    """Find the source video. Search order:
    1. Stored video_path from groundtruth.json (fast path when unchanged).
    2. `aliases[video_name]` — operator-supplied rename mapping.
    3. `videos/` recursive (the live workspace folder).
    4. Each `extra_search_paths` recursively (operator-supplied archives).
    5. `dataset/<slug>/source.*` — auto-archived hardlinks from past renders.
    Returns None when no match found.

    `aliases` maps short-name (groundtruth video_name) → long-name (current
    file). Used when operator renamed files after confirming the ROI — the
    groundtruth keeps the old name but the file lives under a new one.
    """
    if stored_path:
        p = Path(stored_path)
        if p.is_file():
            return p

    # Build the list of names to try: original first, then any alias.
    names_to_try = [video_name]
    if aliases and video_name in aliases:
        names_to_try.append(aliases[video_name])

    for name in names_to_try:
        for root in [_VIDEOS_DIR, *extra_search_paths]:
            if not root.exists():
                continue
            for p in root.rglob(name):
                if p.is_file():
                    return p

    # Fallback: scan dataset/<slug>/ archives. Each archive has a
    # `source.<ext>` hardlink to the original. Match by stem alone since
    # the archive renames to "source<ext>". Look up via manifest.json.
    manifest = _REPO_ROOT / "dataset" / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("entries", []):
                if entry.get("source_video_name") == video_name:
                    slug = entry.get("slug")
                    if slug:
                        slug_dir = _REPO_ROOT / "dataset" / slug
                        for cand in slug_dir.glob("source.*"):
                            if cand.is_file():
                                return cand
        except (OSError, json.JSONDecodeError):
            pass

    return None


def _probe_duration(video: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return float(proc.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None


def _extract_frame(video: Path, t_sec: float, out_path: Path, max_w: int = 1280) -> bool:
    """ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=min(W,iw):-2 <out>.
    Returns True on success. Cap width so labels stay below YOLO's imgsz
    while keeping aspect ratio."""
    if out_path.exists():
        return True
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t_sec:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale='min({max_w},iw)':-2",
        "-q:v", "3",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _frame_sample_times(duration: float, n: int) -> list[float]:
    """N evenly-spaced timestamps in [margin, 1-margin] × duration."""
    if n <= 1:
        return [duration / 2.0]
    step = (1.0 - 2 * _SAMPLE_MARGIN) / (n - 1)
    return [duration * (_SAMPLE_MARGIN + i * step) for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--val", type=float, default=0.20,
                    help="Validation fraction (default 0.20)")
    ap.add_argument("--frames-per-video", type=int, default=5,
                    help="Frames to extract per video via ffmpeg "
                         "(default 5; legacy single-frame mode = 1). "
                         "Operator's tripod is fixed so ROI corners apply "
                         "to every frame of a video.")
    ap.add_argument("--search-path", action="append", default=[],
                    help="Extra directory to recursively search for source "
                         "videos when `videos/` doesn't have them (operator's "
                         "archive folder). Can repeat: --search-path PATH1 "
                         "--search-path PATH2.")
    ap.add_argument("--alias", action="append", default=[],
                    help="Old-name → new-name mapping for renamed source "
                         "videos. Format: 'OLD=NEW' (e.g. "
                         "'Tan.MP4=0510_NguyenKhanhTan_3-1.MP4'). Repeatable. "
                         "Used when operator renamed a file after confirming "
                         "its ROI — the groundtruth keeps the old name.")
    ap.add_argument("--force", action="store_true",
                    help="Delete existing yolo_seg/ before rebuilding")
    args = ap.parse_args()

    extra_paths = [Path(p) for p in args.search_path]
    for p in extra_paths:
        if not p.exists():
            print(f"WARNING: search path does not exist: {p}")

    aliases: dict[str, str] = {}
    for a in args.alias:
        if "=" not in a:
            print(f"WARNING: alias missing '=' separator: {a!r}")
            continue
        old, new = a.split("=", 1)
        aliases[old.strip()] = new.strip()

    if not _GT_DIR.exists():
        print(f"ERROR: groundtruth dir {_GT_DIR} not found")
        return 1

    entries = sorted(_GT_DIR.glob("*.json"))
    if not entries:
        print(f"ERROR: no .json entries in {_GT_DIR}")
        return 1

    if args.force and _OUT_DIR.exists():
        print(f"Removing existing {_OUT_DIR}")
        shutil.rmtree(_OUT_DIR)

    for split in ("train", "val"):
        _ensure_dir(_OUT_DIR / "images" / split)
        _ensure_dir(_OUT_DIR / "labels" / split)

    counts = {"train": 0, "val": 0}
    skipped = 0
    videos_used_multiframe = 0
    videos_fallback_single = 0
    n_frames = max(1, int(args.frames_per_video))

    for json_path in entries:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {json_path.name}: parse error {e}")
            skipped += 1
            continue

        vid = data.get("video_id") or json_path.stem
        corners = data.get("latest_corners")
        if not isinstance(corners, list) or len(corners) != 4:
            print(f"  skip {vid}: no valid corners")
            skipped += 1
            continue

        refframe_src = _GT_DIR / f"{vid}.jpg"
        # Group same-source entries by resolving aliases to the canonical
        # (current-file) name. Two confirms of the same video — one before
        # operator renamed the file, one after — must land in the same split
        # or val metrics become leaky (same frames in train + val).
        video_name = data.get("video_name", vid)
        canonical_name = aliases.get(video_name, video_name)
        split = _split_for(canonical_name, args.val)
        label_line = _format_label_line(corners) + "\n"

        # Try multi-frame extraction first when the source video is locatable.
        # Falls back to the cached refframe alone when video is missing or
        # ffmpeg probe fails — partial data is better than dropping the entry.
        frames_written = 0
        if n_frames > 1:
            video = _locate_video(
                data.get("video_name", ""),
                data.get("video_path"),
                extra_paths,
                aliases,
            )
            if video is not None:
                duration = _probe_duration(video)
                if duration and duration > 5.0:
                    for i, t in enumerate(_frame_sample_times(duration, n_frames)):
                        img_dst = _OUT_DIR / "images" / split / f"{vid}_f{i}.jpg"
                        lbl_dst = _OUT_DIR / "labels" / split / f"{vid}_f{i}.txt"
                        if _extract_frame(video, t, img_dst):
                            lbl_dst.write_text(label_line, encoding="utf-8")
                            frames_written += 1
                            counts[split] += 1
                    if frames_written > 0:
                        videos_used_multiframe += 1

        # Always include the cached refframe (midpoint of first kept segment)
        # — guaranteed to have a player at the table per groundtruth.py. This
        # is the canonical "good" frame and was used by all prior training
        # runs, so keeping it means new runs can't regress on what we know
        # worked. Tagged `_ref` to avoid colliding with multi-frame indices.
        if refframe_src.exists():
            img_dst = _OUT_DIR / "images" / split / f"{vid}_ref.jpg"
            lbl_dst = _OUT_DIR / "labels" / split / f"{vid}_ref.txt"
            shutil.copy2(refframe_src, img_dst)
            lbl_dst.write_text(label_line, encoding="utf-8")
            frames_written += 1
            counts[split] += 1

        if frames_written == 0:
            print(f"  skip {vid}: no refframe + multi-frame extraction failed")
            skipped += 1
            continue

        if n_frames > 1 and frames_written == 1:
            # Got refframe only — multi-frame extraction silently failed.
            videos_fallback_single += 1

    # YOLOv8 data.yaml — paths relative to this file's location at train time.
    # Use absolute path here so `ultralytics` resolves it regardless of cwd.
    data_yaml = _OUT_DIR / "data.yaml"
    data_yaml.write_text(
        f"path: {_OUT_DIR.resolve().as_posix()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"names:\n"
        f"  0: foreground_table\n",
        encoding="utf-8",
    )

    print()
    print(f"Built YOLOv8-seg dataset at {_OUT_DIR}")
    print(f"  frames-per-video target: {n_frames}")
    print(f"  videos with multi-frame extracted: {videos_used_multiframe}")
    print(f"  videos refframe-only (multi-frame failed): {videos_fallback_single}")
    print(f"  train: {counts['train']} examples")
    print(f"  val:   {counts['val']} examples")
    print(f"  skipped entries (no usable images at all): {skipped}")
    print(f"  data.yaml: {data_yaml}")
    print()
    if counts["val"] == 0:
        print("ERROR: validation set is empty. Cannot measure val metrics.")
        return 2
    if counts["train"] < 10:
        # Hard floor, not a warning: an exit-code-only orchestrator
        # (GUI retrain) would otherwise happily fine-tune on a handful
        # of examples produced by a bad glob and promote the result.
        print("ERROR: training set is degenerate (<10 examples) — a bad")
        print("groundtruth glob, not a real corpus. YOLOv8 fine-tune needs")
        print("25+ examples for usable accuracy. Refusing to build.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
