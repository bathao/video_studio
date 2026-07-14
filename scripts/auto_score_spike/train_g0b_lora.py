"""QLoRA fine-tune of the G0b winner-detection VLM (plan §3 / gate G0b).

Trains a LoRA adapter on the dataset produced by build_g0b_dataset.py:
8 ROI-cropped frames + PROMPT_B (the measured 60.4% zero-shot recipe)
→ strict-JSON winner answer. 4-bit NF4 base + LoRA on the language
side, vision tower frozen — sized for the operator's RTX 5060 Ti 16GB.

Base model: Qwen/Qwen3.5-9B (verified 2026-07-14 — natively multimodal
`image-text-to-text`, Qwen3_5ForConditionalGeneration, Apache-2.0, not
gated; the fine-tunable weights of the bake-off's Ollama `qwen3.5:9b`).
First run downloads ~19 GB to the HF cache.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/train_g0b_lora.py \
        [--model Qwen/Qwen3.5-9B] [--check] [--epochs 2] \
        [--run-name g0b-v1] [--max-samples N]

    --check loads processor+model, runs one forward pass on one train
    sample, and exits — validates the environment (transformers
    version, VRAM, model id) without committing to a full train.

Deps: pip install -r requirements-g0b.txt (NOT in CI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "dataset" / "g0b"
RUNS_DIR = ROOT / "runs" / "g0b"

# The assistant target deliberately drops the confidence/reason fields
# of the inference prompt's JSON shape: the bake-off measured flat 0.9
# confidences (useless to the solver) and we have no reason labels.
# eval_g0b.py parses the winner key only, so the shapes stay compatible.
TARGET_JSON = '{{"winner": "{side}"}}'


def load_rows(split: str, max_samples: int | None = None) -> list[dict]:
    path = DATA_DIR / f"{split}.jsonl"
    if not path.is_file():
        raise SystemExit(
            f"{path} missing — run build_g0b_dataset.py first")
    rows = [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[:max_samples] if max_samples else rows


def row_to_messages(row: dict, include_answer: bool) -> list[dict]:
    """Chat messages for one example — shared by the train collator and
    eval_g0b.py so train and inference can never drift apart."""
    user_content = (
        [{"type": "image", "image": f} for f in row["frames"]]
        + [{"type": "text", "text": row["prompt"]}]
    )
    messages = [{"role": "user", "content": user_content}]
    if include_answer:
        messages.append({
            "role": "assistant",
            "content": [{"type": "text",
                         "text": TARGET_JSON.format(side=row["target"])}],
        })
    return messages


def make_collator(processor):
    """Batch collator: renders the chat template, tokenizes text+images,
    and supervises ONLY the assistant answer tokens (prompt tokens are
    masked to -100 via the generation-prompt length)."""
    import torch
    from PIL import Image

    def collate(rows: list[dict]):
        texts, prompt_texts, images = [], [], []
        for row in rows:
            full = row_to_messages(row, include_answer=True)
            user_only = row_to_messages(row, include_answer=False)
            texts.append(processor.apply_chat_template(
                full, tokenize=False))
            prompt_texts.append(processor.apply_chat_template(
                user_only, tokenize=False, add_generation_prompt=True))
            images.append([Image.open(f) for f in row["frames"]])
        batch = processor(text=texts, images=images,
                          return_tensors="pt", padding=True)
        labels = batch["input_ids"].clone()
        labels[batch["attention_mask"] == 0] = -100
        # Mask everything up to the assistant turn, per sample.
        for i, ptxt in enumerate(prompt_texts):
            plen = len(processor.tokenizer(
                ptxt, add_special_tokens=False)["input_ids"])
            labels[i, :plen] = -100
        batch["labels"] = labels
        return batch

    return collate


def load_model_and_processor(model_id: str):
    import torch
    from transformers import (
        AutoModelForImageTextToText,
        AutoProcessor,
        BitsAndBytesConfig,
    )

    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        ),
        dtype=torch.bfloat16,
        device_map="auto",
    )
    return model, processor


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B",
                    help="HF repo id of the base VLM (default: the "
                         "fine-tunable weights of the bake-off's "
                         "qwen3.5:9b)")
    ap.add_argument("--run-name", default="g0b-v1")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-samples", type=int, default=None,
                    help="cap train rows (smoke runs)")
    ap.add_argument("--check", action="store_true",
                    help="environment check: one forward pass, then exit")
    args = ap.parse_args()

    train_rows = load_rows("train", args.max_samples)
    val_rows = load_rows("val")
    print(f"train={len(train_rows)} val={len(val_rows)} examples")

    model, processor = load_model_and_processor(args.model)
    collate = make_collator(processor)

    if args.check:
        import torch
        batch = collate(train_rows[:1])
        batch = {k: v.to(model.device) for k, v in batch.items()}
        with torch.no_grad():
            out = model(**batch)
        print(f"CHECK OK — forward pass loss={out.loss.item():.4f}, "
              f"seq_len={batch['input_ids'].shape[1]}, "
              f"vram={torch.cuda.max_memory_allocated() / 2**30:.1f} GiB")
        return 0

    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import Trainer, TrainingArguments

    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        # Language-side attention + MLP only; the vision tower stays
        # frozen (angle-locked family, tiny dataset — plan §3.1).
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()

    out_dir = RUNS_DIR / args.run_name
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(out_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            gradient_checkpointing=True,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            bf16=True,
            logging_steps=5,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=2,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            report_to=[],
            remove_unused_columns=False,
            dataloader_pin_memory=False,
        ),
        train_dataset=train_rows,
        eval_dataset=val_rows,
        data_collator=collate,
    )
    trainer.train()

    adapter_dir = out_dir / "adapter"
    trainer.model.save_pretrained(str(adapter_dir))
    processor.save_pretrained(str(adapter_dir))
    print(f"\nadapter saved -> {adapter_dir}")
    print("next: venv/Scripts/python.exe scripts/auto_score_spike/eval_g0b.py "
          f"--model {args.model} --adapter {adapter_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
