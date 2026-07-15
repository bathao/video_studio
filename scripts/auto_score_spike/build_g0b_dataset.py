"""Build the G0b winner-detection fine-tune dataset from the corpus.

Converts every side-labeled singles record into one supervised example:
the EXACT frame recipe + prompt the bake-off measured (8 ROI-cropped
640px frames over the [t-6s, t+6s] rally-end window, PROMPT_B with
geometry-defined near/far) and the TRUE near/far winner as the target.
Frames are reused from / cached into out/vlm_frames/ — records already
touched by the bake-off cost nothing.

Truth derivation (no fitting, mirrors rescore_vlm.py but reads the
side fields carried on each corpus record since 893b263): P1's side in
set k = `p1_side_set1` flipped k times when `swap_sides_each_set`,
plus one extra flip for every set-5 point after either player's
DISPLAYED score reaches 5 when `set5_mid_swap` (score_after already
includes handicap set-start points, so handicap matches replay
correctly). Records without side labels are skipped and counted.

Splits (by MATCH, never by record):
  train     — split=="train", train_eligible, side-labeled; the last
              --val-matches matches (by slug date) become `val` for
              early stopping so the pinned held-out stays EVAL-ONLY.
  eval      — split=="held_out" + train_eligible (pinned singles) and
              any non-standard camera_angle records (out-of-family).

Output (gitignored, regenerable):
  dataset/g0b/train.jsonl / val.jsonl / eval.jsonl
      {"id", "frames": [...8 abs paths...], "prompt", "target",
       "match_id", "set_index"}
  dataset/g0b/stats.json — per-split counts + skip reasons.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/build_g0b_dataset.py
        [--val-matches 1] [--k-frames 8]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import (  # noqa: E402
    OUT_DIR as CACHE_DIR,
    get_motion_dual,
    video_key,
)
from scripts.auto_score_spike.vlm_bakeoff import (  # noqa: E402
    K_FRAMES,
    PROMPT_B,
    extract_frames,
)

CORPUS = ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl"
OUT_DIR = ROOT / "dataset" / "g0b"

FLIP = {"near": "far", "far": "near"}


def derive_true_sides(records: list[dict]) -> dict[str, str]:
    """record id -> true near/far side of the point winner.

    Only records with a usable `p1_side_set1` ("near"/"far" — side-on
    left/right axes are out-of-family and unusable for this prompt)
    get an entry."""
    infos: dict[str, dict] = {}
    for r in records:
        p1_side = r.get("p1_side_set1")
        if p1_side not in ("near", "far"):
            continue
        k = int(r["set_index"])
        if r.get("swap_sides_each_set", True) and k % 2 == 1:
            p1_side = FLIP[p1_side]
        infos[r["id"]] = {"p1_side": p1_side, "rec": r}

    # Mid-set-5 swap: every set-5 point AFTER either player's displayed
    # score first reaches 5 flips again. score_after includes handicap
    # set-start points, so this is the operator's actual swap moment.
    by_match: dict[str, list[dict]] = defaultdict(list)
    for info in infos.values():
        by_match[info["rec"]["match_id"]].append(info)
    for match_infos in by_match.values():
        if not match_infos[0]["rec"].get("set5_mid_swap"):
            continue
        set5 = sorted(
            (i for i in match_infos if int(i["rec"]["set_index"]) == 4),
            key=lambda i: i["rec"]["t_event"],
        )
        swapped = False
        for i in set5:
            if swapped:
                i["p1_side"] = FLIP[i["p1_side"]]
            if not swapped and max(i["rec"]["score_after"]) >= 5:
                swapped = True

    return {
        rid: (info["p1_side"] if info["rec"]["who"] == 1
              else FLIP[info["p1_side"]])
        for rid, info in infos.items()
    }


def ensure_video_cached(video: Path) -> None:
    """extract_frames reads roi_corners from the motion cache's
    meta.json — decode any video the spike has never touched (one-time,
    minutes; same canonical path eval_unseen uses)."""
    if (CACHE_DIR / video_key(video.resolve()) / "meta.json").is_file():
        return
    print(f"  motion cache miss -> decoding {video.name} (one-time)")
    get_motion_dual(video)


def example_row(rec: dict, target: str, k_frames: int) -> dict | None:
    frames = extract_frames(rec)
    if len(frames) < k_frames:
        return None
    return {
        "id": rec["id"],
        "frames": [str(p) for p in frames[:k_frames]],
        "prompt": PROMPT_B.format(k=k_frames),
        "target": target,
        "match_id": rec["match_id"],
        "set_index": rec["set_index"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--val-matches", type=int, default=1,
                    help="newest N train matches held out as the val "
                         "split for early stopping (default 1)")
    ap.add_argument("--k-frames", type=int, default=K_FRAMES,
                    help=f"frames per example (default {K_FRAMES} — the "
                         "measured bake-off recipe; change only with a "
                         "fresh baseline)")
    args = ap.parse_args()

    records = [json.loads(l) for l in
               CORPUS.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    records = [r for r in records if r.get("label_source") == "dataset"]
    truth = derive_true_sides(records)

    skips: Counter = Counter()
    pools: dict[str, list[dict]] = {"train": [], "eval": []}
    for r in records:
        if r.get("match_type") != "single":
            skips["doubles"] += 1
            continue
        if r.get("camera_angle", "standard") != "standard":
            # Out-of-family angle: eval-only per plan §3.1, and only if
            # its axis labels were near/far (they are left/right → no
            # truth) — count for visibility either way.
            skips["non_standard_angle_eval_only"] += 1
            continue
        if r["id"] not in truth:
            skips["no_side_label"] += 1
            continue
        if r["split"] == "held_out":
            pools["eval"].append(r)
        elif r.get("train_eligible"):
            pools["train"].append(r)
        else:
            skips["train_ineligible"] += 1

    # Val = the newest N train matches. The slug's trailing
    # YYYYMMDD_HHMMSS is the archive date — extract it explicitly;
    # sorting on split("_") picked aTrung_20260528 over
    # match_001_20260714 (prefix compared before the date) and made a
    # May match the early-stopping val split (caught 2026-07-15).
    def _slug_date(s: str) -> str:
        m = re.search(r"(\d{8}_\d{6})$", s)
        return m.group(1) if m else s

    train_matches = sorted(
        {r["slug"] for r in pools["train"]},
        key=_slug_date,
    )
    val_slugs = set(train_matches[-args.val_matches:]) if args.val_matches else set()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for video in sorted({ROOT / r["video"]
                         for recs_ in pools.values() for r in recs_}):
        ensure_video_cached(video)

    counts: dict[str, int] = {}
    frame_fail: Counter = Counter()
    split_of = {"train": [], "val": [], "eval": []}
    for r in pools["train"]:
        split_of["val" if r["slug"] in val_slugs else "train"].append(r)
    split_of["eval"] = pools["eval"]

    for split, recs in split_of.items():
        rows = []
        for r in recs:
            row = example_row(r, truth[r["id"]], args.k_frames)
            if row is None:
                frame_fail[split] += 1
                continue
            rows.append(row)
        path = OUT_DIR / f"{split}.jsonl"
        path.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
            encoding="utf-8",
        )
        counts[split] = len(rows)
        print(f"{split:5s}: {len(rows):4d} examples "
              f"({len({x['match_id'] for x in rows})} matches) -> {path}")

    stats = {
        "counts": counts,
        "val_matches": sorted(val_slugs),
        "train_matches": [s for s in train_matches if s not in val_slugs],
        "k_frames": args.k_frames,
        "skips": dict(skips),
        "frame_extract_failures": dict(frame_fail),
        "target_balance": {
            split: dict(Counter(
                json.loads(l)["target"]
                for l in (OUT_DIR / f"{split}.jsonl")
                .read_text(encoding="utf-8").splitlines()))
            for split in split_of
        },
    }
    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"skips: {dict(skips)}")
    print(f"balance: {stats['target_balance']}")

    # Fail loud: an empty train or eval split means every downstream
    # step (baseline, fine-tune, verdict) would run on nothing and
    # still exit 0 — exactly the 2026-07-14 retro-label bug.
    empty = [s for s in ("train", "eval") if not counts.get(s)]
    if empty:
        print(f"ERROR: empty split(s) {empty} — check side labels / "
              "corpus rebuild before training", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
