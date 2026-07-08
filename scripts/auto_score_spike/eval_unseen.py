"""Association-recall eval for the v7 segmenter on corpus singles.

Persisted version of the session-2 ad-hoc metric: a manual score event
at t_event claims ONE proposal whose END falls in [t-12s, t+2s]
(asymmetric — operator press-lag median 3.5s, p75 up to 11s). Greedy
one-to-one, nearest |dt| first. This is the metric behind the
"v7 = 85.5% singles" number in docs/TODO.md.

Runs on singles matches only (operator directive: doubles excluded
from train + eval). Matches not yet in the motion cache are decoded
on first touch (ROI detect + full NVDEC decode, minutes per match).

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/eval_unseen.py [slug-substr ...]
        no args  -> every singles match in the corpus
        slug ... -> only matches whose slug contains a given substring
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import get_motion_dual  # noqa: E402
from scripts.auto_score_spike.eval_segmentation import (  # noqa: E402
    CORPUS_JSONL, SEGMENTERS,
)

WIN_BEFORE_S = 12.0  # proposal end may precede the press by this much
WIN_AFTER_S = 2.0    # ... or trail it by this much

# Frozen from the 2026-07-08 train-singles sweep (docs/TODO.md step 2).
V7_TUNED = dict(split_over_s=8.0, valley_frac=0.9,
                edge_guard_s=1.5, exit_sustain_s=1.2)

# 2026-07-08 pm: the coverage-miss audit showed EVERY remaining miss
# peaks >= p77 — short spikes killed by min_rally_s=1.5, not quiet
# rallies. Rescue: shorter min_rally + lower exit percentile.
# Coverage recall train 98.0% / held-out 94.9% / truly-unseen 97.8%
# at ~1.15x the v1-tuned proposal count.
V7_TUNED2 = {**V7_TUNED, "min_rally_s": 1.0, "pct_lo": 55.0}


def assoc_recall(
    truth: list[float],
    intervals: list[tuple[float, float]],
    *,
    coverage: bool = False,
) -> tuple[int, int]:
    """Greedy one-to-one: each event claims the nearest unclaimed
    proposal whose END is inside [t - WIN_BEFORE_S, t + WIN_AFTER_S].

    coverage=True additionally accepts a proposal that CONTAINS the
    event even when its end trails past t + WIN_AFTER_S (a rally blob
    with a stuck ball-fetch tail) — the right notion of "usable" for
    the Phase 1 review GUI, where the operator gets the whole clip.
    """
    pairs = sorted(
        (abs(t - e), ti, ei)
        for ti, t in enumerate(truth)
        for ei, (s, e) in enumerate(intervals)
        if (t - WIN_BEFORE_S <= e <= t + WIN_AFTER_S)
        or (coverage and s <= t <= e)
    )
    used_t: set[int] = set()
    used_e: set[int] = set()
    for _, ti, ei in pairs:
        if ti in used_t or ei in used_e:
            continue
        used_t.add(ti)
        used_e.add(ei)
    return len(used_t), len(truth)


def main() -> int:
    recs = [json.loads(l) for l in
            CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[float]] = {}
    for r in recs:
        if r["label_source"] != "dataset" or r["match_type"] != "single":
            continue
        by_video.setdefault(r["video"], []).append(r["t_event"])

    wanted = sys.argv[1:]
    totals: dict[str, list[int]] = {}
    for video_rel, truth in sorted(by_video.items()):
        slug = Path(video_rel).parent.name
        if wanted and not any(w in slug for w in wanted):
            continue
        info = get_motion_dual(ROOT / video_rel)
        print(f"{slug}  ({len(truth)} events)")
        for name, kw in (("v7-default", {}), ("v7-tuned", V7_TUNED),
                         ("v7-tuned2", V7_TUNED2)):
            intervals = SEGMENTERS["v7"](info, **kw)
            hit, n = assoc_recall(sorted(truth), intervals)
            cov, _ = assoc_recall(sorted(truth), intervals, coverage=True)
            totals.setdefault(name, [0, 0, 0, 0])
            totals[name][0] += hit
            totals[name][1] += n
            totals[name][2] += len(intervals)
            totals[name][3] += cov
            print(f"  {name:12} assoc {hit}/{n} = {hit / n:5.1%}"
                  f"   coverage {cov}/{n} = {cov / n:5.1%}"
                  f"   proposals {len(intervals)}")

    print()
    for name, (hit, n, props, cov) in totals.items():
        print(f"TOTAL {name:12} assoc {hit}/{n} = {hit / n:5.1%}"
              f"   coverage {cov}/{n} = {cov / n:5.1%}"
              f"   proposals {props}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
