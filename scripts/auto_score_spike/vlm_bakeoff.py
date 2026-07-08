"""VLM winner-detection bake-off (AUTO_SCORE_PLAN Phase 0 step 3).

For each sampled corpus point: extract K ROI-cropped frames around the
score event, send to a local VLM via Ollama, parse {winner: near|far,
confidence, reason}. Because near/far <-> P1/P2 mapping per set is
unknown, two metrics are reported:

- pairwise agreement (mapping-free): for point pairs within one set,
  does the model agree with truth on same-winner vs different-winner?
  0.5 = chance.
- majority-mapping accuracy: per (match, set) resolve the mapping by
  majority vote, then score. Slightly optimistic at low accuracy
  (mapping fitted on the same data) — rank models by BOTH.

Frames are cached under out/vlm_frames/<record_id>/ so model swaps
re-run instantly. Evidence (frames + raw responses) lands under
out/vlm_eval/<model>/ per the plan's debug-bundle protocol.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/vlm_bakeoff.py \
        --model qwen3-vl:8b [--n 60] [--split train] [--seed 7]
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.config import config  # noqa: E402
from scripts.auto_score_spike.motion_cache import OUT_DIR, video_key  # noqa: E402
from scripts.auto_score_spike.frame_dataset import crop_box  # noqa: E402

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
FRAMES_CACHE = OUT_DIR / "vlm_frames"
EVAL_DIR = OUT_DIR / "vlm_eval"
K_FRAMES = 8
FRAME_W = 640

PROMPT = """You are watching a table tennis point from a fixed camera placed diagonally behind one player. The player closest to the camera is the NEAR player; the opponent across the net is the FAR player.

These {k} frames sample the END of one rally and the seconds right after it (in time order). Decide who WON this point. Useful cues, in rough order of reliability:
- Post-rally behaviour: the LOSER usually goes to fetch the ball or looks down/away; the WINNER may celebrate, clench a fist, or walk confidently back to serve/receive.
- The final stroke: a ball into the net or off the table means the last hitter lost the point (unless it was a winner shot the opponent never touched).
- Body language immediately after the last frame of play.

Answer with STRICT JSON, nothing else:
{{"winner": "near" | "far", "confidence": 0.0-1.0, "reason": "<one short sentence>"}}"""

# Variant B (2026-07-08): the derived-mapping rescore showed qwen3.5's
# answers are systematically INVERTED (true-acc 41% + pairwise 59% —
# flip-symmetric), i.e. the model separates winner from loser at ~59%
# but confuses which player the word "near" refers to. This variant
# defines the two players by unmistakable image geometry instead of
# camera distance.
PROMPT_B = """You are watching a table tennis point from a fixed camera. Two players are visible:
- The BOTTOM player: appears LARGE, at the bottom of the frame, back mostly toward the camera.
- The TOP player: appears SMALL, in the upper part of the frame, across the net, facing the camera.

These {k} frames sample the END of one rally and the seconds right after it (in time order). Decide who WON this point. Useful cues, in rough order of reliability:
- Post-rally behaviour: the LOSER usually goes to fetch the ball or looks down/away; the WINNER may celebrate, clench a fist, or walk confidently back to serve/receive.
- The final stroke: a ball into the net or off the table means the last hitter lost the point (unless it was a winner shot the opponent never touched).
- Body language immediately after the last frame of play.

Answer with STRICT JSON, nothing else — use "near" for the BOTTOM (large) player and "far" for the TOP (small) player:
{{"winner": "near" | "far", "confidence": 0.0-1.0, "reason": "<one short sentence>"}}"""

PROMPTS = {"a": PROMPT, "b": PROMPT_B}


# ---------------------------------------------------------------------------
# Frame extraction (cached per record)
# ---------------------------------------------------------------------------


def extract_frames(rec: dict) -> list[Path]:
    rid = re.sub(r"[^A-Za-z0-9_.-]", "_", rec["id"])
    dest = FRAMES_CACHE / rid
    existing = sorted(dest.glob("f*.jpg"))
    if len(existing) == K_FRAMES:
        return existing

    video = ROOT / rec["video"]
    meta_p = OUT_DIR / video_key(video) / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    ws, we = rec["window_start"], rec["window_end"]

    import cv2
    import numpy as np

    probe = json.loads(subprocess.run(
        [config.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True, check=True).stdout)
    W = probe["streams"][0]["width"]
    H = probe["streams"][0]["height"]
    box = crop_box(meta["roi_corners"], W, H)
    bw, bh = box[2] - box[0], box[3] - box[1]
    out_h = max(2, int(round(FRAME_W * bh / bw / 2)) * 2)

    fps = K_FRAMES / max(0.5, we - ws)
    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ws:.3f}", "-to", f"{we:.3f}", "-i", str(video),
        "-vf", f"fps={fps:.5f},crop={bw}:{bh}:{box[0]}:{box[1]},scale={FRAME_W}:{out_h}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = FRAME_W * out_h * 3
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    i = 0
    while i < K_FRAMES:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        img = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, FRAME_W, 3)
        p = dest / f"f{i:02d}.jpg"
        cv2.imwrite(str(p), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        paths.append(p)
        i += 1
    proc.stdout.close()
    proc.wait(timeout=10)
    return paths


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------


def ask_vlm(model: str, frames: list[Path], prompt: str = PROMPT) -> dict:
    images = [base64.b64encode(p.read_bytes()).decode() for p in frames]
    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt.format(k=len(frames)),
            "images": images,
        }],
        "format": "json",
        "stream": False,
        # 8 vision-encoded frames overflow the 4096-token default ctx
        "options": {"temperature": 0.0, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        out = json.loads(resp.read())
    latency = time.time() - t0
    raw = out.get("message", {}).get("content", "")
    try:
        parsed = json.loads(raw)
        winner = str(parsed.get("winner", "")).strip().lower()
        conf = float(parsed.get("confidence", 0.5))
        reason = str(parsed.get("reason", ""))[:300]
    except Exception:
        winner, conf, reason = "", 0.5, f"UNPARSEABLE: {raw[:200]}"
    if winner not in ("near", "far"):
        winner = ""
    return {"winner": winner, "confidence": conf, "reason": reason,
            "latency_s": round(latency, 2), "raw": raw[:500]}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def score(results: list[dict]) -> dict:
    valid = [r for r in results if r["pred"] in ("near", "far")]
    # pairwise same/diff agreement within each (match, set)
    from collections import defaultdict
    groups = defaultdict(list)
    for r in valid:
        groups[(r["match_id"], r["set_index"])].append(r)
    agree = total_pairs = 0
    for rows in groups.values():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                same_true = rows[i]["who"] == rows[j]["who"]
                same_pred = rows[i]["pred"] == rows[j]["pred"]
                agree += (same_true == same_pred)
                total_pairs += 1
    # majority-mapping accuracy per (match, set)
    correct = 0
    for rows in groups.values():
        m1 = sum((r["pred"] == "near") == (r["who"] == 1) for r in rows)
        correct += max(m1, len(rows) - m1)
    return {
        "n": len(results),
        "n_valid": len(valid),
        "pairwise_agreement": round(agree / total_pairs, 4) if total_pairs else None,
        "mapping_accuracy": round(correct / len(valid), 4) if valid else None,
        "mean_latency_s": round(
            sum(r["latency_s"] for r in results) / max(1, len(results)), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--split", default="train", choices=("train", "held_out"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--include-doubles", action="store_true",
                    help="operator directive 2026-07-08: singles-only by default")
    ap.add_argument("--ids-file", type=Path, default=None,
                    help="responses.jsonl of a finished model: evaluate the "
                         "exact same record ids (comparable across corpus "
                         "rebuilds) instead of sampling")
    ap.add_argument("--prompt", default="a", choices=sorted(PROMPTS),
                    help="prompt variant (b = geometry-defined near/far)")
    ap.add_argument("--tag", default="",
                    help="suffix for the eval output dir, e.g. promptB")
    args = ap.parse_args()

    recs = [json.loads(l) for l in
            (ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    pool = [r for r in recs
            if r["label_source"] == "dataset" and r["split"] == args.split
            and (args.include_doubles or r["match_type"] == "single")]
    if args.ids_file:
        want = [json.loads(l)["id"] for l in
                args.ids_file.read_text(encoding="utf-8").splitlines()]
        by_id = {r["id"]: r for r in pool}
        sample = [by_id[i] for i in dict.fromkeys(want) if i in by_id]
        if len(sample) < len(set(want)):
            print(f"warning: {len(set(want)) - len(sample)} ids from "
                  f"{args.ids_file.name} not in the current pool")
    else:
        rng = random.Random(args.seed)
        sample = rng.sample(pool, min(args.n, len(pool)))

    dir_name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.model)
    if args.tag:
        dir_name += f"__{args.tag}"
    model_dir = EVAL_DIR / dir_name
    model_dir.mkdir(parents=True, exist_ok=True)

    # Resume-safe: skip records this model already answered (a killed
    # chain leaves a partial responses.jsonl; re-running must append
    # only the missing clips, never duplicate).
    resp_path = model_dir / "responses.jsonl"
    done_ids: set[str] = set()
    if resp_path.is_file():
        done_ids = {json.loads(l)["id"] for l in
                    resp_path.read_text(encoding="utf-8").splitlines()}
    if done_ids:
        skipped = len([r for r in sample if r["id"] in done_ids])
        sample = [r for r in sample if r["id"] not in done_ids]
        print(f"resume: {skipped} clips already answered, {len(sample)} to go")

    results = []
    for i, rec in enumerate(sample, 1):
        frames = extract_frames(rec)
        if len(frames) < 4:
            print(f"[{i}/{len(sample)}] {rec['id']}: frame extraction failed, skipped")
            continue
        ans = ask_vlm(args.model, frames, PROMPTS[args.prompt])
        row = {
            "id": rec["id"], "match_id": rec["match_id"],
            "set_index": rec["set_index"], "who": rec["who"],
            "pred": ans["winner"], "confidence": ans["confidence"],
            "latency_s": ans["latency_s"], "reason": ans["reason"],
        }
        results.append(row)
        (model_dir / "responses.jsonl").open("a", encoding="utf-8").write(
            json.dumps({**row, "raw": ans["raw"]}, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(sample)}] {rec['id']}: pred={ans['winner'] or '??'} "
              f"conf={ans['confidence']:.2f} ({ans['latency_s']:.1f}s)")

    # Score from the FULL responses file, not just this run's rows —
    # after a resume the in-memory `results` holds only the tail.
    all_rows = [json.loads(l) for l in
                resp_path.read_text(encoding="utf-8").splitlines()]
    summary = score(all_rows)
    summary["model"] = args.model
    summary["split"] = args.split
    summary["seed"] = args.seed
    (model_dir / "summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8", newline="\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
