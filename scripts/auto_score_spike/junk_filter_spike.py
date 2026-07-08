"""Pose-based junk filter for v7-tuned2 proposals (Phase 1 GUI UX).

v7-tuned2 emits ~2x the true event count (precision ~50%) — every
junk proposal is a review card the operator must skip. This spike
measures how much junk a venue-invariant pose rule can drop WITHOUT
losing real rallies: a proposal is plausible-rally only if both
players are present near the table for most of the interval.

Labels: coverage-claimed proposals (the assoc metric's one-to-one
matching against manual score events) = RALLY; unclaimed = JUNK.
Caveat: unclaimed is a NOISY negative (the segmenter also fires on
warm-up rallies between matches, lets, practice serves) — treat the
measured precision gain as a lower bound on usefulness, and recall
loss on CLAIMED proposals as the hard constraint (must stay ~0).

Features per proposal from pose_features.npz (2.5 fps):
  both_frac   fraction of samples with BOTH near+far present
  near_mot    mean near-player keypoint motion
  far_mot     mean far-player keypoint motion

Rule swept on train singles, reported on held-out + truly-unseen:
  keep iff both_frac >= B and (near_mot + far_mot) >= M

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/junk_filter_spike.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import OUT_DIR, get_motion_dual, video_key  # noqa: E402
from scripts.auto_score_spike.eval_segmentation import CORPUS_JSONL, SEGMENTERS  # noqa: E402
from scripts.auto_score_spike.eval_unseen import (  # noqa: E402
    V7_TUNED2, WIN_AFTER_S, WIN_BEFORE_S,
)

NEW_MATCH = "match_001_20260708_143451"


def bucket(video_rel: str, split: str) -> str:
    if NEW_MATCH in video_rel:
        return "unseen"
    return "held" if split == "held_out" else "train"


def load_proposals():
    recs = [json.loads(l) for l in CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[dict]] = {}
    for r in recs:
        if r["label_source"] == "dataset" and r["match_type"] == "single":
            by_video.setdefault(r["video"], []).append(r)

    out = []  # {bucket, claimed, both_frac, mot_sum}
    for video_rel, events in sorted(by_video.items()):
        truth = sorted(e["t_event"] for e in events)
        b = bucket(video_rel, events[0]["split"])
        video = ROOT / video_rel
        info = get_motion_dual(video)
        intervals = SEGMENTERS["v7"](info, **V7_TUNED2)

        pairs = sorted(
            (abs(t - e), ti, ei)
            for ti, t in enumerate(truth)
            for ei, (s, e) in enumerate(intervals)
            if (t - WIN_BEFORE_S <= e <= t + WIN_AFTER_S) or (s <= t <= e)
        )
        used_t, used_e = set(), set()
        for _, ti, ei in pairs:
            if ti in used_t or ei in used_e:
                continue
            used_t.add(ti)
            used_e.add(ei)

        npz = np.load(OUT_DIR / video_key(video.resolve()) / "pose_features.npz")
        arr = npz["features"]
        fps = float(npz["fps"])
        for ei, (s, e) in enumerate(intervals):
            win = arr[int(s * fps):max(int(s * fps) + 1, int(e * fps))]
            if len(win) == 0:
                continue
            both = float(((win[:, 2] > 0) & (win[:, 5] > 0)).mean())
            mot = float(np.nanmean(win[:, 4]) + np.nanmean(win[:, 7]))
            out.append({
                "bucket": b,
                "claimed": ei in used_e,
                "both_frac": both,
                "mot_sum": mot,
            })
    return out


def apply_rule(rows, b_min: float, m_min: float):
    kept_claimed = sum(1 for r in rows if r["claimed"]
                       and r["both_frac"] >= b_min and r["mot_sum"] >= m_min)
    claimed = sum(1 for r in rows if r["claimed"])
    kept_junk = sum(1 for r in rows if not r["claimed"]
                    and r["both_frac"] >= b_min and r["mot_sum"] >= m_min)
    junk = sum(1 for r in rows if not r["claimed"])
    return kept_claimed, claimed, kept_junk, junk


def main() -> int:
    rows = load_proposals()
    train = [r for r in rows if r["bucket"] == "train"]

    print(f"proposals: {len(rows)} total "
          f"({sum(1 for r in rows if r['claimed'])} claimed)\n")
    print(f"{'rule':28} {'bucket':>7} {'rally kept':>12} {'junk dropped':>13}")
    best = None
    for b_min in (0.0, 0.3, 0.5, 0.7, 0.8):
        for m_min in (0.0, 0.001, 0.002):
            kc, c, kj, j = apply_rule(train, b_min, m_min)
            if c == 0 or j == 0:
                continue
            rally_kept = kc / c
            junk_dropped = 1 - kj / j
            # constraint: lose at most 1% of claimed rallies on train
            if rally_kept >= 0.99 and (best is None or junk_dropped > best[0]):
                best = (junk_dropped, b_min, m_min)
    if best is None:
        print("no rule keeps >=99% of claimed rallies on train")
        return 0
    _, b_min, m_min = best
    print(f"picked on train: both_frac>={b_min}, mot_sum>={m_min}\n")
    for bk in ("train", "held", "unseen"):
        sub = [r for r in rows if r["bucket"] == bk]
        kc, c, kj, j = apply_rule(sub, b_min, m_min)
        print(f"{'both>=' + str(b_min) + ' mot>=' + str(m_min):28} {bk:>7} "
              f"{kc}/{c} = {kc / max(1, c):5.1%}   "
              f"{j - kj}/{j} = {1 - kj / max(1, j):5.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
