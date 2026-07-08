"""Re-score VLM bake-off responses with the DERIVED side mapping.

The bake-off's majority-mapping metric fits the near/far <-> P1/P2
mapping per (match, set) on the model's own answers — slightly
optimistic. With side_truth.json (operator backfill 2026-07-08) the
mapping is DERIVED: P1's side in set k = set-1 side flipped k times,
plus one extra flip in a 5th set after either player reaches 5 when
that match swapped mid-decider. This scores each model against the
true near/far winner of every clip — no fitting anywhere.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/rescore_vlm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.motion_cache import OUT_DIR  # noqa: E402

CORPUS = ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl"
SIDE_TRUTH = Path(__file__).parent / "side_truth.json"
EVAL_DIR = OUT_DIR / "vlm_eval"

FLIP = {"near": "far", "far": "near"}


def true_side_by_id() -> dict[str, str]:
    """record id -> true near/far side of the point winner."""
    truth = json.loads(SIDE_TRUTH.read_text(encoding="utf-8"))["matches"]
    recs = [json.loads(l) for l in CORPUS.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[dict]] = {}
    for r in recs:
        if r["label_source"] == "dataset":
            by_video.setdefault(Path(r["video"]).name, []).append(r)
    # corpus is index-only; the video basename inside dataset/<slug>/ is
    # source.<ext>, so key side truth by the ORIGINAL filename recorded
    # in the record id instead.
    out: dict[str, str] = {}
    for r in recs:
        if r["label_source"] != "dataset":
            continue
        src_name = r["id"].split("#")[0]
        entry = truth.get(src_name)
        if entry is None:
            continue
        k = int(r["set_index"])
        p1_side = entry["p1_side_set1"]
        if k % 2 == 1:
            p1_side = FLIP[p1_side]
        out[r["id"]] = {"p1_side": p1_side, "rec": r, "swap5": entry["set5_swap_at_5"]}
    # Mid-set-5 swap: replay each match's set-5 score to find the point
    # where either player reaches 5; every LATER point flips again.
    by_match: dict[str, list] = {}
    for rid, info in out.items():
        by_match.setdefault(rid.split("#")[0], []).append(info)
    for src_name, infos in by_match.items():
        if not infos[0]["swap5"]:
            continue
        set5 = sorted(
            (i for i in infos if int(i["rec"]["set_index"]) == 4),
            key=lambda i: i["rec"]["t_event"],
        )
        p1 = p2 = 0
        swapped = False
        for i in set5:
            if swapped:
                i["p1_side"] = FLIP[i["p1_side"]]
            if i["rec"]["who"] == 1:
                p1 += 1
            else:
                p2 += 1
            if not swapped and max(p1, p2) == 5:
                swapped = True
    return {
        rid: (info["p1_side"] if info["rec"]["who"] == 1 else FLIP[info["p1_side"]])
        for rid, info in out.items()
    }


def main() -> int:
    truth = true_side_by_id()
    print(f"{'model':16} {'valid':>7} {'true-acc':>9}   (derived mapping, no fit)")
    for model_dir in sorted(EVAL_DIR.iterdir()):
        resp = model_dir / "responses.jsonl"
        if not resp.is_file():
            continue
        rows = [json.loads(l) for l in resp.read_text(encoding="utf-8").splitlines()]
        scored = [
            (r["pred"] == truth[r["id"]])
            for r in rows
            if r.get("pred") in ("near", "far") and r["id"] in truth
        ]
        if not scored:
            continue
        acc = sum(scored) / len(scored)
        print(f"{model_dir.name:16} {len(scored):>4}/60 {acc:>8.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
