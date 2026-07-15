"""G0b milestone overnight driver — corpus refresh through LoRA verdict.

Sequential, fail-fast; full output streams to temp/g0b_overnight.log in
the repo (survives this session either way).
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"c:\Users\MSI\video_studio_v3")
LOG = ROOT / "temp" / "g0b_overnight.log"
PY = str(ROOT / "venv" / "Scripts" / "python.exe")

NEW_SLUGS = ["20260714_061844", "20260714_070155", "20260714_212307"]

STEPS = [
    ("corpus rebuild (adds tonight's match)",
     [PY, "-X", "utf8", "scripts/auto_score_spike/build_corpus.py"]),
    ("truly-unseen eval on the 3 newest matches",
     [PY, "-X", "utf8", "scripts/auto_score_spike/eval_unseen.py", *NEW_SLUGS]),
    ("G0b dataset build (frame extraction, resumes cache)",
     [PY, "-X", "utf8", "scripts/auto_score_spike/build_g0b_dataset.py"]),
    ("trainer environment check (one forward pass)",
     [PY, "scripts/auto_score_spike/train_g0b_lora.py", "--check"]),
    # Baseline must be RE-measured whenever the frame recipe changes —
    # 2026-07-15 letterboxing (FRAME_PAD_SIZE) replaced the 55.7%
    # unpadded number; train and eval share open_frame so they can't
    # drift apart.
    ("zero-shot HF baseline on held-out (padded recipe)",
     [PY, "scripts/auto_score_spike/eval_g0b.py", "--model", "Qwen/Qwen3.5-9B"]),
    # Epoch 1 ONLY (plan phase 4, halved risk): eval right after; a
    # second epoch is a separate decision — auto-resume continues from
    # the epoch-1 checkpoint when relaunched with --epochs 2.
    ("QLoRA fine-tune (g0b-v1, epoch 1)",
     [PY, "scripts/auto_score_spike/train_g0b_lora.py", "--run-name", "g0b-v1",
      "--epochs", "1"]),
    ("held-out eval + G0b verdict (epoch 1)",
     [PY, "scripts/auto_score_spike/eval_g0b.py", "--model", "Qwen/Qwen3.5-9B",
      "--adapter", "runs/g0b/g0b-v1/adapter"]),
]


def main() -> int:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n{'#' * 70}\nG0B OVERNIGHT RUN — started "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{'#' * 70}\n")
        for name, cmd in STEPS:
            hdr = f"\n===== [{time.strftime('%H:%M:%S')}] {name} =====\n"
            print(hdr, flush=True)
            log.write(hdr)
            log.flush()
            t0 = time.time()
            rc = subprocess.call(cmd, cwd=str(ROOT), stdout=log,
                                 stderr=subprocess.STDOUT)
            note = f"----- {name}: exit {rc} after {(time.time() - t0) / 60:.1f} min\n"
            print(note, flush=True)
            log.write(note)
            log.flush()
            if rc != 0:
                print(f"ABORTED at step: {name} — details in {LOG}", flush=True)
                return 1
    print("G0B OVERNIGHT PIPELINE COMPLETE — verdict at the end of "
          f"{LOG}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
