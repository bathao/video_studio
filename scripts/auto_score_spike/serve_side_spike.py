"""Serve-side detector spike (plan §6 step 5).

Which side (near/far) serves each rally? Labels are DERIVED from the
score sequence: serve alternates every 2 total points (every point
from 10-10), the starting server alternates per set, and the set-1
starting server is ONE free bit per match — both hypotheses are
scored and the better one is kept (report is therefore mildly
optimistic; with ~70 points/match the bit costs ~1/70 of accuracy).

Features come from pose_features.npz (2.5 fps near/far presence,
distance-to-table, keypoint motion) over windows anchored on the
v7-tuned2 rally intervals each score event claims (coverage
matching): the serve phase (first 2 s of the rally) and the pre-serve
ritual (2 s before the start). Candidate discriminants are simple
near-minus-far contrasts; each is scored as "predict near-serves if
value > 0" plus its sign-flipped twin.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/serve_side_spike.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import OUT_DIR, video_key  # noqa: E402
from scripts.auto_score_spike.eval_segmentation import CORPUS_JSONL, SEGMENTERS  # noqa: E402
from scripts.auto_score_spike.eval_unseen import (  # noqa: E402
    V7_TUNED2, WIN_AFTER_S, WIN_BEFORE_S,
)
from scripts.auto_score_spike.motion_cache import get_motion_dual  # noqa: E402

FLIP = {"near": "far", "far": "near"}
SIDE_TRUTH = json.loads(
    (Path(__file__).parent / "side_truth.json").read_text(encoding="utf-8")
)["matches"]

SERVE_WIN_S = 2.0   # serve phase: first N seconds of the rally
PRE_WIN_S = 2.0     # pre-serve ritual: N seconds before rally start


def server_of_point(start_server: int, total_before: int) -> int:
    """1 or 2. Serve flips every 2 points until 20 total, then every point."""
    if total_before < 20:
        shifts = total_before // 2
    else:
        shifts = 10 + (total_before - 20)
    return start_server if shifts % 2 == 0 else 3 - start_server


def p1_side_for_point(entry: dict, set_idx: int, swapped_mid5: bool) -> str:
    side = entry["p1_side_set1"]
    if set_idx % 2 == 1:
        side = FLIP[side]
    if set_idx == 4 and swapped_mid5:
        side = FLIP[side]
    return side


def build_rows():
    """-> list of {video, t_start, server_side(near|far), split} rows,
    one per score event whose rally interval was found, for BOTH
    set-1-server hypotheses (h1: P1 serves first, h2: P2)."""
    recs = [json.loads(l) for l in CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[dict]] = {}
    for r in recs:
        if r["label_source"] == "dataset" and r["match_type"] == "single":
            by_video.setdefault(r["video"], []).append(r)

    rows = []
    for video_rel, events in sorted(by_video.items()):
        events.sort(key=lambda r: r["t_event"])
        src_name = events[0]["id"].split("#")[0]
        entry = SIDE_TRUTH[src_name]
        video = ROOT / video_rel
        info = get_motion_dual(video)
        intervals = SEGMENTERS["v7"](info, **V7_TUNED2)

        # greedy coverage claim (same as eval_unseen)
        pairs = sorted(
            (abs(t["t_event"] - e), ti, ei)
            for ti, t in enumerate(events)
            for ei, (s, e) in enumerate(intervals)
            if (t["t_event"] - WIN_BEFORE_S <= e <= t["t_event"] + WIN_AFTER_S)
            or (s <= t["t_event"] <= e)
        )
        used_t, used_e = set(), set()
        claim: dict[int, int] = {}
        for _, ti, ei in pairs:
            if ti in used_t or ei in used_e:
                continue
            used_t.add(ti)
            used_e.add(ei)
            claim[ti] = ei

        # replay for serve labels
        per_set: dict[int, list[int]] = {}
        for ti, e in enumerate(events):
            per_set.setdefault(int(e["set_index"]), []).append(ti)
        for k, tis in sorted(per_set.items()):
            p1 = p2 = 0
            mid5 = bool(entry["set5_swap_at_5"]) if k == 4 else False
            swapped = False
            for ti in tis:
                e = events[ti]
                total = p1 + p2
                side_p1 = p1_side_for_point(
                    entry, k, mid5 and swapped)
                for hyp, s1 in (("h1", 1), ("h2", 2)):
                    # starting server alternates each set
                    start = s1 if k % 2 == 0 else 3 - s1
                    srv = server_of_point(start, total)
                    srv_side = side_p1 if srv == 1 else FLIP[side_p1]
                    if ti in claim:
                        rows.append({
                            "video": video_rel,
                            "src": src_name,
                            "hyp": hyp,
                            "t_start": intervals[claim[ti]][0],
                            "server_side": srv_side,
                            "split": e["split"],
                        })
                if e["who"] == 1:
                    p1 += 1
                else:
                    p2 += 1
                if mid5 and not swapped and max(p1, p2) == 5:
                    swapped = True
    return rows


def features_for(video_rel: str, t_start: float) -> dict[str, float] | None:
    key = video_key((ROOT / video_rel).resolve())
    npz_p = OUT_DIR / key / "pose_features.npz"
    if not npz_p.is_file():
        return None
    npz = np.load(npz_p)
    arr = npz["features"]
    fps = float(npz["fps"])
    i0 = int(t_start * fps)
    serve = arr[i0:max(i0 + 1, int((t_start + SERVE_WIN_S) * fps))]
    pre = arr[max(0, int((t_start - PRE_WIN_S) * fps)):max(1, i0)]
    if len(serve) == 0 or len(pre) == 0:
        return None
    # columns: t, n_persons, near_present, near_dist, near_motion,
    #          far_present, far_dist, far_motion
    def agg(win):
        return {
            "near_dist": float(np.nanmean(win[:, 3])),
            "near_mot": float(np.nanmean(win[:, 4])),
            "far_dist": float(np.nanmean(win[:, 6])),
            "far_mot": float(np.nanmean(win[:, 7])),
        }
    s, p = agg(serve), agg(pre)
    return {
        "serve_mot_nf": s["near_mot"] - s["far_mot"],
        "serve_dist_nf": s["near_dist"] - s["far_dist"],
        "pre_mot_nf": p["near_mot"] - p["far_mot"],
        "pre_dist_nf": p["near_dist"] - p["far_dist"],
        "pre_minus_serve_mot_nf": (p["near_mot"] - p["far_mot"])
                                  - (s["near_mot"] - s["far_mot"]),
    }


def main() -> int:
    rows = build_rows()
    feats_cache: dict[tuple[str, float], dict | None] = {}
    data = []
    for r in rows:
        kk = (r["video"], r["t_start"])
        if kk not in feats_cache:
            feats_cache[kk] = features_for(r["video"], r["t_start"])
        f = feats_cache[kk]
        if f is None:
            continue
        data.append({**r, **f})

    feat_names = ["serve_mot_nf", "serve_dist_nf", "pre_mot_nf",
                  "pre_dist_nf", "pre_minus_serve_mot_nf"]
    print(f"{len(data) // 2} labeled rallies with pose features "
          f"(x2 hypotheses)\n")
    print(f"{'discriminant':26} {'train':>8} {'held':>8}   (best set-1-server bit per match)")
    for name in feat_names:
        for sign in (1, -1):
            accs = {"train": [0, 0], "held_out": [0, 0]}
            # pick the better hypothesis PER MATCH on train only
            by_src: dict[str, dict[str, list]] = {}
            for d in data:
                by_src.setdefault(d["src"], {}).setdefault(d["hyp"], []).append(d)
            for src, hyps in by_src.items():
                def acc_of(rows_):
                    ok = sum(
                        1 for d in rows_
                        if (("near" if sign * d[name] > 0 else "far")
                            == d["server_side"])
                    )
                    return ok, len(rows_)
                # choose hypothesis by train rows of this match
                best_hyp, best = None, -1.0
                for hyp, rows_ in hyps.items():
                    tr = [d for d in rows_ if d["split"] == "train"]
                    pool = tr if tr else rows_  # held-out matches: fit on themselves (noted)
                    ok, n = acc_of(pool)
                    if n and ok / n > best:
                        best, best_hyp = ok / n, hyp
                for d in hyps[best_hyp]:
                    part = "train" if d["split"] == "train" else "held_out"
                    ok = ("near" if sign * d[name] > 0 else "far") == d["server_side"]
                    accs[part][0] += ok
                    accs[part][1] += 1
            tr = accs["train"]
            he = accs["held_out"]
            label = name if sign == 1 else f"-{name}"
            tr_s = f"{tr[0] / tr[1]:.1%}" if tr[1] else "n/a"
            he_s = f"{he[0] / he[1]:.1%}" if he[1] else "n/a"
            print(f"{label:26} {tr_s:>8} {he_s:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
