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
        [--run-name g0b-v1] [--max-samples N] [--max-steps N] \
        [--mem-fraction 0.90]

    --check loads processor+model, runs one forward pass on one train
    sample, and exits — validates the environment (transformers
    version, VRAM, model id) without committing to a full train.
    --max-steps bounds a run for measured proofs (12 = memory
    diagnostic, 30 = GO/NO-GO) — resume continues past it later.

Live progress: every step appends a memory/timing line to stdout and
rewrites runs/g0b/<run>/heartbeat.json, which the Training dashboard
(📊 top bar) renders as a progress bar + step speed + ETA + health.

Deps: pip install -r requirements-g0b.txt (NOT in CI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# NOTE: PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True is NOT
# supported on Windows (PyTorch prints a warning and ignores it —
# verified 2026-07-15), so fragmentation from per-match tensor shapes
# is mitigated by the per-step empty_cache in TrainHealth instead.

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "dataset" / "g0b"
RUNS_DIR = ROOT / "runs" / "g0b"

# The assistant target deliberately drops the confidence/reason fields
# of the inference prompt's JSON shape: the bake-off measured flat 0.9
# confidences (useless to the solver) and we have no reason labels.
# eval_g0b.py parses the winner key only, so the shapes stay compatible.
TARGET_JSON = '{{"winner": "{side}"}}'


def write_heartbeat(out_dir: Path, **fields) -> None:
    """Atomically (re)write out_dir/heartbeat.json for the Training
    dashboard. Display-only — never let it kill a run."""
    hb = {"ts": time.time(), **fields}
    tmp = out_dir / "heartbeat.json.tmp"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(hb), encoding="utf-8")
        tmp.replace(out_dir / "heartbeat.json")
    except OSError:
        pass


def load_rows(split: str, max_samples: int | None = None) -> list[dict]:
    path = DATA_DIR / f"{split}.jsonl"
    if not path.is_file():
        raise SystemExit(
            f"{path} missing — run build_g0b_dataset.py first")
    rows = [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[:max_samples] if max_samples else rows


# One fixed canvas for every frame. Per-match ROI crops differ in
# height (640x188 .. 640x272 across the corpus) → per-step tensor
# shapes varied → allocator fragmentation (~1.7 GiB) OOM'd the 16 GiB
# card even though the raw demand fit (12-step diagnostic, 2026-07-15;
# Windows has no expandable_segments to absorb it). Letterboxing to
# one shape makes every step allocate identically. RECIPE CHANGE: the
# zero-shot baseline must be (re)measured with the same padding —
# eval_g0b.py shares open_frame so it can't drift.
FRAME_PAD_SIZE = (640, 272)


def open_frame(path: str):
    """Open one cached frame letterboxed onto the FRAME_PAD_SIZE canvas
    (black bars, centered). Shared by the train collator and eval."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    tw, th = FRAME_PAD_SIZE
    if (w, h) == (tw, th):
        return im
    if w > tw or h > th:  # future-proof: crop bigger than the canvas
        im.thumbnail((tw, th))
        w, h = im.size
    canvas = Image.new("RGB", (tw, th))
    canvas.paste(im, ((tw - w) // 2, (th - h) // 2))
    return canvas


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
            images.append([open_frame(f) for f in row["frames"]])
        batch = processor(text=texts, images=images,
                          return_tensors="pt", padding=True)
        # Prompt length must be measured on the EXPANDED sequence: the
        # processor blows each <|image_pad|> up to ~40 real tokens, so
        # tokenizing the prompt TEXT undercounted by ~1200 and left the
        # whole prompt (image pads included) supervised — the model
        # spent epoch 1 learning to predict its own prompt while the
        # 7 answer tokens drowned (val loss 0.0009 vs 45% generate
        # accuracy exposed it, 2026-07-15). Running the processor on
        # the prompt-only text with the same images gives the true
        # per-sample boundary.
        pbatch = processor(text=prompt_texts, images=images,
                           return_tensors="pt", padding=True)
        labels = batch["input_ids"].clone()
        labels[batch["attention_mask"] == 0] = -100
        for i in range(len(rows)):
            plen = int(pbatch["attention_mask"][i].sum())
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
    ap.add_argument("--max-steps", type=int, default=-1,
                    help="stop after N optimizer steps (bounded proof "
                         "runs: 12 = memory diagnostic, 30 = GO/NO-GO); "
                         "-1 trains the full --epochs")
    ap.add_argument("--mem-fraction", type=float, default=0.90,
                    help="hard cap on the CUDA allocation as a fraction "
                         "of dedicated VRAM — overflow becomes a loud "
                         "OOM instead of a silent WDDM spill-and-crawl")
    ap.add_argument("--check", action="store_true",
                    help="environment check: one forward pass, then exit")
    args = ap.parse_args()

    # First heartbeat within seconds of launch — model load takes
    # ~2 min and an empty dashboard during it reads as "not running".
    if not args.check:
        write_heartbeat(RUNS_DIR / args.run_name, run_name=args.run_name,
                        status="running", step=0, total_steps=0,
                        note="loading dataset + model (~2 min)…")

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

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import Trainer, TrainingArguments

    # Fail-loud memory ceiling: without this, exceeding dedicated VRAM
    # does not error on Windows — WDDM pages to system RAM and the run
    # crawls (60 s -> 450 s/step, 2026-07-15). With it, the allocator
    # raises OOM the moment the cap is hit.
    torch.cuda.set_per_process_memory_fraction(args.mem_fraction)
    print(f"CUDA per-process memory cap: {args.mem_fraction:.0%}")

    # Hand-rolled replacement for peft's prepare_model_for_kbit_training.
    # The library version blanket-upcasts EVERY non-quantized param to
    # fp32 — its .to(float32) on the ~151k-vocab embedding tried to
    # allocate 3.79 GiB in one shot and OOM'd against the cap (caught by
    # the 12-step diagnostic, 2026-07-15). Recasting to bf16 afterwards
    # (the previous fix) was too late: the allocation spike had already
    # happened. Reproduce only what training actually needs:
    #   · tiny params (norms, biases) in fp32 for numeric stability;
    #   · big frozen matrices (embedding, lm_head) stay bf16;
    #   · embedding outputs require grad so gradient checkpointing can
    #     backprop through frozen layers into the LoRA adapters.
    upcast = 0
    for _, param in model.named_parameters():
        param.requires_grad = False
        if param.dtype == torch.bfloat16 and param.numel() <= 1_000_000:
            param.data = param.data.to(torch.float32)
            upcast += param.numel()
    model.enable_input_require_grads()
    print(f"upcast {upcast / 1e6:.1f}M small params to fp32 "
          "(big frozen matrices stay bf16)")
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

    from transformers import TrainerCallback

    out_dir = RUNS_DIR / args.run_name

    class TrainHealth(TrainerCallback):
        """Step timing guard + live heartbeat, in one callback.

        Guard: VRAM overflow on Windows does NOT crash — WDDM spills to
        system RAM and steps slow ~100x (measured 1642 s/step on
        2026-07-15). 3 consecutive steps over LIMIT_S abort the run.

        Heartbeat: writes out_dir/heartbeat.json after EVERY step so
        the Training dashboard (and the operator) can see progress,
        step speed, ETA and VRAM live instead of staring at a mute
        100%-GPU graph. Written atomically (tmp + replace)."""
        LIMIT_S = 300.0

        def __init__(self):
            self._t0 = 0.0
            self._slow = 0
            self._recent: list[float] = []  # last step durations

        def _write_heartbeat(self, state, status: str, note: str = ""):
            spst = (sum(self._recent) / len(self._recent)
                    if self._recent else 0.0)
            total = int(state.max_steps or 0)
            done = int(state.global_step or 0)
            loss = next((h["loss"] for h in reversed(state.log_history)
                         if "loss" in h), None)
            write_heartbeat(
                out_dir,
                run_name=args.run_name,
                status=status,             # running | done | error
                step=done,
                total_steps=total,
                seconds_per_step=round(spst, 1),
                eta_min=(round((total - done) * spst / 60.0)
                         if spst and total else None),
                loss=loss,
                vram_alloc_gb=round(
                    torch.cuda.memory_allocated() / 2**30, 2),
                vram_reserved_gb=round(
                    torch.cuda.memory_reserved() / 2**30, 2),
                note=note,
            )

        def on_train_begin(self, targs, state, control, **kw):
            self._write_heartbeat(state, "running", "train starting")

        def on_step_begin(self, targs, state, control, **kw):
            self._t0 = time.time()

        def on_step_end(self, targs, state, control, **kw):
            # Release cached-but-unused blocks every step so allocator
            # growth can't ratchet past the card (costs ~ms).
            torch.cuda.empty_cache()
            dt = time.time() - self._t0
            self._recent = (self._recent + [dt])[-5:]
            # Per-step memory log — the Phase-2 diagnostic reads this
            # to separate fragmentation (reserved grows, allocated
            # flat) from a true leak (allocated grows).
            print(f"step {state.global_step}/{state.max_steps} "
                  f"{dt:.0f}s alloc={torch.cuda.memory_allocated() / 2**30:.2f}G "
                  f"reserved={torch.cuda.memory_reserved() / 2**30:.2f}G",
                  flush=True)
            self._write_heartbeat(state, "running")
            if dt > self.LIMIT_S:
                self._slow += 1
                if self._slow >= 3:
                    self._write_heartbeat(
                        state, "error", "aborted: 3 consecutive slow steps")
                    raise RuntimeError(
                        f"3 consecutive steps > {self.LIMIT_S:.0f}s — "
                        "GPU memory is likely spilling to system RAM; "
                        "shrink the footprint instead of letting this "
                        "run for days")
            else:
                self._slow = 0

        def on_train_end(self, targs, state, control, **kw):
            self._write_heartbeat(state, "done")

    trainer = Trainer(
        callbacks=[TrainHealth()],
        model=model,
        args=TrainingArguments(
            output_dir=str(out_dir),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            bf16=True,
            logging_steps=5,
            max_steps=args.max_steps,
            # Checkpoint every 5 steps (operator decision 2026-07-15:
            # runs kept dying early, losing everything — ~1% overhead
            # buys a ≤5-min worst-case loss). eval is decoupled at 50
            # steps; load_best is dropped — the plan's epoch-1 eval
            # gate replaces in-training best-model selection.
            eval_strategy="steps",
            eval_steps=50,
            save_strategy="steps",
            save_steps=5,
            save_total_limit=4,
            report_to=[],
            remove_unused_columns=False,
            dataloader_pin_memory=False,
        ),
        train_dataset=train_rows,
        eval_dataset=val_rows,
        data_collator=collate,
    )
    # Auto-resume: a paused/killed run left checkpoint-N dirs behind —
    # pick up where it stopped instead of retraining from step 0. The
    # driver re-runs this same command on relaunch, so resume must be
    # the default, not a flag someone has to remember. A shutdown can
    # kill the process MID-save, so skip checkpoints missing
    # trainer_state.json (partial writes) rather than crashing on them.
    last_ckpt = None
    if out_dir.is_dir():
        for d in sorted(out_dir.glob("checkpoint-*"),
                        key=lambda p: int(p.name.rsplit("-", 1)[1]),
                        reverse=True):
            if (d / "trainer_state.json").is_file():
                last_ckpt = str(d)
                break
            print(f"skipping partial checkpoint {d.name}")
    if last_ckpt:
        print(f"resuming from {last_ckpt}")
    try:
        trainer.train(resume_from_checkpoint=last_ckpt)
    except BaseException as e:
        # The error must OUTLIVE the process on the dashboard — the
        # operator watched a run die and the reason vanish with it
        # (2026-07-15). KeyboardInterrupt/kill land here too.
        write_heartbeat(out_dir, run_name=args.run_name, status="error",
                        step=int(trainer.state.global_step or 0),
                        total_steps=int(trainer.state.max_steps or 0),
                        note=f"{type(e).__name__}: {str(e)[:300]}")
        raise

    adapter_dir = out_dir / "adapter"
    trainer.model.save_pretrained(str(adapter_dir))
    processor.save_pretrained(str(adapter_dir))
    print(f"\nadapter saved -> {adapter_dir}")
    print("next: venv/Scripts/python.exe scripts/auto_score_spike/eval_g0b.py "
          f"--model {args.model} --adapter {adapter_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
