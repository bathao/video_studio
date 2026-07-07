"""Motion-signal cache for the auto-score spike (Phase 0 step 2).

For a given video: extract 5 evenly-spaced refframes -> run the
production ROI detector (read-only reuse; the don't-touch-ROI rule
only bars ALGORITHM changes) -> compute the ROI motion signal via
backend.rally_detector.compute_motion_signal -> cache everything under
scripts/auto_score_spike/out/<video_key>/.

The decode is the slow part (minutes per full match); caching it means
segmentation tuning iterates in milliseconds. Cache key =
sha1(path|size|mtime)[:16], same idiom as temp/refframes.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/motion_cache.py <video> [more videos...]
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.config import config  # noqa: E402
from backend.ffmpeg_runner import probe_video  # noqa: E402
from backend.rally_detector import BALANCED, compute_motion_signal  # noqa: E402

OUT_DIR = ROOT / "scripts" / "auto_score_spike" / "out"
REFFRAME_FRACTIONS = (0.10, 0.30, 0.50, 0.70, 0.90)


def video_key(video: Path) -> str:
    st = video.stat()
    ident = f"{video.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]


def _extract_refframes(video: Path, duration: float, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for frac in REFFRAME_FRACTIONS:
        out = dest / f"ref_{int(frac * 100):02d}.jpg"
        if not out.is_file():
            cmd = [
                config.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{duration * frac:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3",
                "-y", str(out),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        paths.append(out)
    return paths


def get_motion(video: Path) -> dict:
    """Return {motion, fps, duration, roi_corners, roi_method}; build the
    cache on first call."""
    from backend.roi import detect_roi_multiframe  # lazy: pulls torch

    video = video.resolve()
    key = video_key(video)
    cache_dir = OUT_DIR / key
    npz_path = cache_dir / "motion.npz"
    meta_path = cache_dir / "meta.json"

    if npz_path.is_file() and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        motion = np.load(npz_path)["motion"]
        return {**meta, "motion": motion}

    duration = float(probe_video(video).get("duration", 0.0))
    if duration <= 0:
        raise RuntimeError(f"probe failed for {video}")

    refs = _extract_refframes(video, duration, cache_dir / "refframes")
    t0 = time.time()
    det = detect_roi_multiframe(refs)
    roi_seconds = time.time() - t0

    t0 = time.time()
    motion = compute_motion_signal(video, det.corners, BALANCED, duration_hint=duration)
    decode_seconds = time.time() - t0

    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_path, motion=motion)
    meta = {
        "video": str(video),
        "key": key,
        "duration": duration,
        "fps": BALANCED.decode_fps,
        "roi_corners": det.corners,
        "roi_method": det.method,
        "roi_confidence": det.confidence,
        "roi_seconds": round(roi_seconds, 1),
        "decode_seconds": round(decode_seconds, 1),
    }
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8", newline="\n")
    return {**meta, "motion": motion}


def split_quad_at_net(corners: list[list[float]]) -> tuple[list, list]:
    """Split the table quad (TL,TR,BR,BL normalized) into far/near
    halves along the net line — the segment joining the midpoints of
    the left (TL-BL) and right (TR-BR) sides. With the operator's
    diagonal-from-behind tripod framing, the TL-TR edge is the far end
    of the table and BL-BR the near end."""
    tl, tr, br, bl = corners
    ml = [(tl[0] + bl[0]) / 2, (tl[1] + bl[1]) / 2]
    mr = [(tr[0] + br[0]) / 2, (tr[1] + br[1]) / 2]
    far_q = [tl, tr, mr, ml]
    near_q = [ml, mr, br, bl]
    return far_q, near_q


def get_motion_dual(video: Path) -> dict:
    """get_motion + per-half signals (near/far of the net). When nothing
    is cached yet, ALL THREE signals (full, near, far) are computed in a
    single decode pass — decode dominates the cost, so this halves the
    cold-cache time vs get_motion() followed by a second pass."""
    from backend.rally_detector import BALANCED as P
    from backend.rally_detector import _iter_frames, build_roi_mask

    video = video.resolve()
    key = video_key(video)
    cache_dir = OUT_DIR / key
    npz_path = cache_dir / "motion.npz"
    dual_path = cache_dir / "motion_dual.npz"
    meta_path = cache_dir / "meta.json"

    if npz_path.is_file() and dual_path.is_file() and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        d = np.load(dual_path)
        return {**meta, "motion": np.load(npz_path)["motion"],
                "near": d["near"], "far": d["far"]}

    from backend.roi import detect_roi_multiframe  # lazy: pulls torch
    import cv2

    duration = float(probe_video(video).get("duration", 0.0))
    if duration <= 0:
        raise RuntimeError(f"probe failed for {video}")
    refs = _extract_refframes(video, duration, cache_dir / "refframes")
    t0 = time.time()
    det = detect_roi_multiframe(refs)
    roi_seconds = time.time() - t0

    far_q, near_q = split_quad_at_net(det.corners)
    masks = {}
    for name, quad in (("full", det.corners), ("far", far_q), ("near", near_q)):
        m = build_roi_mask(quad, P.decode_width, P.decode_height)
        if m.sum() <= 0:
            raise RuntimeError(f"degenerate {name} quad")
        masks[name] = (m.astype(bool), float(m.sum()))

    sig: dict[str, list[float]] = {name: [] for name in masks}
    prev_gray = None
    t0 = time.time()
    for frame in _iter_frames(video, P):
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            for name, (mb, ms) in masks.items():
                sig[name].append(float(diff[mb].sum()) / (255.0 * ms))
        prev_gray = gray
    decode_seconds = time.time() - t0

    motion = np.asarray(sig["full"], dtype=np.float32)
    near = np.asarray(sig["near"], dtype=np.float32)
    far = np.asarray(sig["far"], dtype=np.float32)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_path, motion=motion)
    np.savez_compressed(dual_path, near=near, far=far)
    meta = {
        "video": str(video),
        "key": key,
        "duration": duration,
        "fps": P.decode_fps,
        "roi_corners": det.corners,
        "roi_method": det.method,
        "roi_confidence": det.confidence,
        "roi_seconds": round(roi_seconds, 1),
        "decode_seconds": round(decode_seconds, 1),
    }
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8", newline="\n")
    print(f"  cached {video.name}: decode {decode_seconds:.0f}s, roi={det.method}")
    return {**meta, "motion": motion, "near": near, "far": far}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for arg in sys.argv[1:]:
        video = Path(arg)
        info = get_motion(video)
        print(
            f"{video.name}: {len(info['motion'])} samples @ {info['fps']} fps, "
            f"dur={info['duration']:.1f}s, roi={info['roi_method']} "
            f"(conf {info['roi_confidence']:.2f}), decode={info.get('decode_seconds', '?')}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
