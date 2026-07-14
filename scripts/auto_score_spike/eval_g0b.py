"""Evaluate a G0b winner-detection model on the pinned held-out split.

Runs the base VLM (optionally + a LoRA adapter from train_g0b_lora.py)
on dataset/g0b/eval.jsonl — the pinned held-out singles match(es) that
NEVER touch training or early stopping — and scores TRUE accuracy
against the derived near/far winner (no mapping fit anywhere).

Verdict per plan §3 gate G0b (stop-loss from the 2026-07-10 decision):
    >= 80%   PASS       — winner path unlocked, Phase 2 GUI justified
    75-80%   MARGINAL   — grow the corpus / iterate before building GUI
    <  75%   STOP-LOSS  — do not build on this; rethink the approach
Reference points: zero-shot qwen3.5:9b + PROMPT_B via Ollama = 60.4%;
chance = ~50%.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/eval_g0b.py \
        --model <hf-repo-id> [--adapter runs/g0b/g0b-v1/adapter] \
        [--split eval] [--max-samples N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.auto_score_spike.train_g0b_lora import (  # noqa: E402
    load_model_and_processor,
    load_rows,
    row_to_messages,
)

OUT_DIR = ROOT / "dataset" / "g0b"

_WINNER_RE = re.compile(r'"winner"\s*:\s*"(near|far)"', re.IGNORECASE)


def parse_winner(text: str) -> str:
    m = _WINNER_RE.search(text)
    if m:
        return m.group(1).lower()
    # Fallback: bare word answer.
    low = text.strip().lower()
    if "near" in low and "far" not in low:
        return "near"
    if "far" in low and "near" not in low:
        return "far"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="HF repo id of the base VLM")
    ap.add_argument("--adapter", type=Path, default=None,
                    help="LoRA adapter dir (omit for the zero-shot baseline)")
    ap.add_argument("--split", default="eval", choices=("eval", "val", "train"))
    ap.add_argument("--max-samples", type=int, default=None)
    ap.add_argument("--tag", default="",
                    help="suffix for the responses file name")
    args = ap.parse_args()

    import torch
    from PIL import Image

    rows = load_rows(args.split, args.max_samples)
    print(f"{args.split}: {len(rows)} examples, "
          f"{len({r['match_id'] for r in rows})} matches")

    model, processor = load_model_and_processor(args.model)
    if args.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(args.adapter))
        print(f"adapter loaded: {args.adapter}")
    model.eval()

    name = (args.adapter.parent.name if args.adapter else "zero-shot")
    if args.tag:
        name += f"__{args.tag}"
    resp_path = OUT_DIR / f"responses_{args.split}_{name}.jsonl"

    results = []
    with resp_path.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(rows):
            messages = row_to_messages(row, include_answer=False)
            text = processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            images = [Image.open(f) for f in row["frames"]]
            batch = processor(text=[text], images=[images],
                              return_tensors="pt").to(model.device)
            with torch.no_grad():
                out_ids = model.generate(
                    **batch, max_new_tokens=32, do_sample=False)
            answer = processor.tokenizer.decode(
                out_ids[0][batch["input_ids"].shape[1]:],
                skip_special_tokens=True)
            pred = parse_winner(answer)
            correct = pred == row["target"]
            results.append({"id": row["id"], "match_id": row["match_id"],
                            "pred": pred, "target": row["target"],
                            "correct": correct})
            fh.write(json.dumps({**results[-1], "raw": answer[:300]},
                                ensure_ascii=False) + "\n")
            if (i + 1) % 10 == 0:
                acc_so_far = sum(r["correct"] for r in results) / len(results)
                print(f"  {i + 1}/{len(rows)}  running acc {acc_so_far:.1%}")

    valid = [r for r in results if r["pred"]]
    acc = sum(r["correct"] for r in valid) / max(1, len(valid))
    print(f"\nvalid answers : {len(valid)}/{len(results)}")
    print(f"TRUE accuracy : {acc:.1%}   (zero-shot ollama baseline 60.4%, chance ~50%)")

    per_match = defaultdict(list)
    for r in valid:
        per_match[r["match_id"]].append(r["correct"])
    for m, oks in sorted(per_match.items()):
        print(f"  {m:45s} {sum(oks):3d}/{len(oks):3d}  {sum(oks) / len(oks):.1%}")

    verdict = ("PASS — winner path unlocked (>=80%)" if acc >= 0.80
               else "MARGINAL — grow the corpus / iterate (75-80%)" if acc >= 0.75
               else "STOP-LOSS — do not build on this (<75%)")
    print(f"\nVERDICT: {verdict}")
    print(f"responses -> {resp_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
