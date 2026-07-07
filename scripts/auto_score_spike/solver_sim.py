"""Score-grammar solver simulation (AUTO_SCORE_PLAN Phase 0 step 6).

Question: given rally-end timestamps and NOISY per-rally winner
observations at accuracy p, how much does a Viterbi pass over the
table-tennis scoring grammar recover — and how many points still need
human review?

Uses the REAL winner sequences from the corpus (551 points across 7
matches) as ground truth; observation noise is simulated by flipping
each observed winner independently with probability 1-p. Reported at
the plan 4.1 anchor rungs:

- none:        grammar only (11+2 sets, best-of-5)
- final-score: + final set score known from the filename
- per-set:     + exact set-boundary indices known (operator taps)

Solver = exact forward DP (Viterbi) over score states
(sets_a, sets_b, pts_a, pts_b); forward-backward marginals give a
per-point confidence, and points whose posterior < flag threshold are
counted as "flags" (the review queue).

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/solver_sim.py [--trials 40]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS_JSONL = ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl"

SET_POINTS = 11
FLAG_POSTERIOR = 0.90


# ---------------------------------------------------------------------------
# Scoring grammar
# ---------------------------------------------------------------------------


def apply_point(state: tuple, who: int, sets_to_win: int) -> tuple | None:
    """state = (sets_a, sets_b, pts_a, pts_b). Returns the next state or
    None if the match was already over (no legal transition)."""
    sa, sb, a, b = state
    if sa >= sets_to_win or sb >= sets_to_win:
        return None
    if who == 1:
        a += 1
    else:
        b += 1
    if (a >= SET_POINTS or b >= SET_POINTS) and abs(a - b) >= 2:
        if a > b:
            sa += 1
        else:
            sb += 1
        a = b = 0
    return (sa, sb, a, b)


def set_completed(prev: tuple, nxt: tuple) -> bool:
    return (nxt[0] + nxt[1]) > (prev[0] + prev[1])


# ---------------------------------------------------------------------------
# Forward-backward over the winner sequence
# ---------------------------------------------------------------------------


def solve(
    observations: list[int],
    p_obs: float,
    best_of: int,
    *,
    final_sets: tuple[int, int] | None = None,
    set_end_indices: set[int] | None = None,
) -> tuple[list[int], list[float]]:
    """Exact forward-backward. observations[i] in {1, 2}. Returns
    (MAP winner sequence via max-product, posterior P(winner=map_choice)
    per point via sum-product)."""
    sets_to_win = best_of // 2 + 1
    n = len(observations)
    log_hit = math.log(p_obs)
    log_miss = math.log(1.0 - p_obs)

    # forward max-product AND sum-product together
    start = (0, 0, 0, 0)
    fwd_max: list[dict] = [{start: (0.0, None, None)}]  # state -> (logp, prev_state, who)
    fwd_sum: list[dict] = [{start: 1.0}]

    for i, obs in enumerate(observations):
        cur_max: dict = {}
        cur_sum: dict = defaultdict(float)
        for state, (lp, _, _) in fwd_max[-1].items():
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    ended = set_completed(state, nxt)
                    if ended != (i in set_end_indices):
                        continue
                step = log_hit if who == obs else log_miss
                cand = lp + step
                if nxt not in cur_max or cand > cur_max[nxt][0]:
                    cur_max[nxt] = (cand, state, who)
        for state, prob in fwd_sum[-1].items():
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    ended = set_completed(state, nxt)
                    if ended != (i in set_end_indices):
                        continue
                w = p_obs if who == obs else (1.0 - p_obs)
                cur_sum[nxt] += prob * w
        if not cur_max:
            raise RuntimeError(f"no legal path at point {i}")
        fwd_max.append(cur_max)
        fwd_sum.append(dict(cur_sum))

    def accept_final(state: tuple) -> bool:
        if final_sets is not None:
            return (state[0], state[1]) == final_sets
        return True

    finals = {s: v for s, v in fwd_max[-1].items() if accept_final(s)}
    if not finals:
        finals = fwd_max[-1]  # constraint impossible (shouldn't happen) — fall back

    # --- MAP backtrace
    best_state = max(finals, key=lambda s: finals[s][0])
    map_seq: list[int] = []
    state = best_state
    for i in range(n, 0, -1):
        lp, prev, who = fwd_max[i][state]
        map_seq.append(who)
        state = prev
    map_seq.reverse()

    # --- backward sum-product for marginals
    bwd: list[dict] = [dict.fromkeys(fwd_sum[-1], 0.0)]
    for s in fwd_sum[-1]:
        bwd[0][s] = 1.0 if accept_final(s) else 0.0
    for i in range(n - 1, -1, -1):
        obs = observations[i]
        prev_b: dict = defaultdict(float)
        nxt_b = bwd[0]
        for state in fwd_sum[i]:
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None or nxt not in nxt_b:
                    continue
                if set_end_indices is not None:
                    ended = set_completed(state, nxt)
                    if ended != (i in set_end_indices):
                        continue
                w = p_obs if who == obs else (1.0 - p_obs)
                prev_b[state] += w * nxt_b[nxt]
        bwd.insert(0, dict(prev_b))

    posteriors: list[float] = []
    for i in range(n):
        obs = observations[i]
        mass = {1: 0.0, 2: 0.0}
        for state, fprob in fwd_sum[i].items():
            if state not in bwd[i] or bwd[i].get(state, 0.0) == 0.0:
                pass
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    ended = set_completed(state, nxt)
                    if ended != (i in set_end_indices):
                        continue
                w = p_obs if who == obs else (1.0 - p_obs)
                mass[who] += fprob * w * bwd[i + 1].get(nxt, 0.0)
        total = mass[1] + mass[2]
        chosen = map_seq[i]
        posteriors.append(mass[chosen] / total if total > 0 else 0.5)

    return map_seq, posteriors


# ---------------------------------------------------------------------------
# Simulation harness
# ---------------------------------------------------------------------------


def load_matches() -> list[dict]:
    recs = [json.loads(l) for l in CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_match: dict[str, list[dict]] = {}
    for r in recs:
        if r["label_source"] == "dataset":
            by_match.setdefault(r["match_id"], []).append(r)
    matches = []
    for mid, rows in by_match.items():
        rows.sort(key=lambda r: r["t_event"])
        winners = [r["who"] for r in rows]
        final = rows[-1]["sets_after"]
        # a set COMPLETES at point i-1 when point i belongs to the next
        # set; the last point always completes a set (match end).
        set_ends = {
            i - 1 for i, r in enumerate(rows[1:], 1)
            if r["set_index"] > rows[i - 1]["set_index"]
        }
        set_ends.add(len(rows) - 1)
        matches.append({
            "match_id": mid,
            "winners": winners,
            "best_of": rows[0]["best_of"],
            "final_sets": (final[0], final[1]),
            "set_end_indices": set_ends,
        })
    return matches


def solve_conf(
    observations: list[int],
    confidences: list[float],
    best_of: int,
    *,
    final_sets: tuple[int, int] | None = None,
    set_end_indices: set[int] | None = None,
) -> tuple[list[int], list[float]]:
    """Like solve() but with a PER-POINT observation confidence c_i:
    P(obs_i correct) = c_i. Models a real vision system that knows
    which of its calls are shaky — the solver can then prefer flipping
    low-confidence points to satisfy the grammar."""
    sets_to_win = best_of // 2 + 1
    n = len(observations)
    start = (0, 0, 0, 0)
    fwd_max: list[dict] = [{start: (0.0, None, None)}]
    fwd_sum: list[dict] = [{start: 1.0}]

    for i, obs in enumerate(observations):
        c = min(max(confidences[i], 1e-6), 1 - 1e-6)
        lh, lm = math.log(c), math.log(1 - c)
        cur_max: dict = {}
        cur_sum: dict = defaultdict(float)
        for state, (lp, _, _) in fwd_max[-1].items():
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    if set_completed(state, nxt) != (i in set_end_indices):
                        continue
                cand = lp + (lh if who == obs else lm)
                if nxt not in cur_max or cand > cur_max[nxt][0]:
                    cur_max[nxt] = (cand, state, who)
        for state, prob in fwd_sum[-1].items():
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    if set_completed(state, nxt) != (i in set_end_indices):
                        continue
                cur_sum[nxt] += prob * (c if who == obs else 1 - c)
        if not cur_max:
            raise RuntimeError(f"no legal path at point {i}")
        fwd_max.append(cur_max)
        fwd_sum.append(dict(cur_sum))

    def accept_final(state: tuple) -> bool:
        return final_sets is None or (state[0], state[1]) == final_sets

    finals = {s: v for s, v in fwd_max[-1].items() if accept_final(s)}
    if not finals:
        finals = fwd_max[-1]
    best_state = max(finals, key=lambda s: finals[s][0])
    map_seq: list[int] = []
    state = best_state
    for i in range(n, 0, -1):
        _, prev, who = fwd_max[i][state]
        map_seq.append(who)
        state = prev
    map_seq.reverse()

    bwd: dict = {s: (1.0 if accept_final(s) else 0.0) for s in fwd_sum[-1]}
    bwds = [bwd]
    for i in range(n - 1, -1, -1):
        obs = observations[i]
        c = min(max(confidences[i], 1e-6), 1 - 1e-6)
        prev_b: dict = defaultdict(float)
        nxt_b = bwds[0]
        for state in fwd_sum[i]:
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None or nxt not in nxt_b:
                    continue
                if set_end_indices is not None:
                    if set_completed(state, nxt) != (i in set_end_indices):
                        continue
                prev_b[state] += (c if who == obs else 1 - c) * nxt_b[nxt]
        bwds.insert(0, dict(prev_b))

    posteriors: list[float] = []
    for i in range(n):
        obs = observations[i]
        c = min(max(confidences[i], 1e-6), 1 - 1e-6)
        mass = {1: 0.0, 2: 0.0}
        for state, fprob in fwd_sum[i].items():
            for who in (1, 2):
                nxt = apply_point(state, who, sets_to_win)
                if nxt is None:
                    continue
                if set_end_indices is not None:
                    if set_completed(state, nxt) != (i in set_end_indices):
                        continue
                mass[who] += fprob * (c if who == obs else 1 - c) * bwds[i + 1].get(nxt, 0.0)
        total = mass[1] + mass[2]
        posteriors.append(mass[map_seq[i]] / total if total > 0 else 0.5)
    return map_seq, posteriors


def run_confidence(trials: int, seed: int = 20260708) -> None:
    """Two-tier observation model: a fraction `hard_frac` of points is
    HARD (low accuracy, low reported confidence); the rest are EASY.
    Approximates a real vision model with calibrated confidence."""
    matches = load_matches()
    rng = random.Random(seed)
    tiers = (
        ("easy95/hard60 30%hard", 0.30, 0.95, 0.60),
        ("easy95/hard60 20%hard", 0.20, 0.95, 0.60),
        ("easy90/hard55 30%hard", 0.30, 0.90, 0.55),
    )
    print("\nconfidence-aware solver (per-set rung), flags = posterior < 0.9:")
    print(f"{'obs model':>24} | {'raw acc':>8} | {'post acc':>8} | {'flags/match':>11} | {'err in unflagged':>16}")
    for name, hard_frac, p_easy, p_hard in tiers:
        raw_c = post_c = total = flags = runs = 0
        unflagged_err = 0
        for m in matches:
            for _ in range(trials):
                obs, conf = [], []
                for w in m["winners"]:
                    hard = rng.random() < hard_frac
                    p = p_hard if hard else p_easy
                    o = w if rng.random() < p else (3 - w)
                    obs.append(o)
                    conf.append(p)
                map_seq, post = solve_conf(
                    obs, conf, m["best_of"],
                    final_sets=m["final_sets"],
                    set_end_indices=m["set_end_indices"],
                )
                raw_c += sum(a == b for a, b in zip(obs, m["winners"]))
                post_c += sum(a == b for a, b in zip(map_seq, m["winners"]))
                total += len(m["winners"])
                flags += sum(1 for q in post if q < FLAG_POSTERIOR)
                unflagged_err += sum(
                    1 for q, a, b in zip(post, map_seq, m["winners"])
                    if q >= FLAG_POSTERIOR and a != b
                )
                runs += 1
        print(f"{name:>24} | {raw_c / total:>8.1%} | {post_c / total:>8.1%} | "
              f"{flags / runs:>11.1f} | {unflagged_err / runs:>16.2f}")


def run(trials: int, seed: int = 20260708) -> None:
    matches = load_matches()
    rng = random.Random(seed)
    rungs = ("none", "final-score", "per-set")
    accuracies = (0.67, 0.75, 0.80, 0.85, 0.90)

    print(f"matches: {len(matches)}, total points: {sum(len(m['winners']) for m in matches)}")
    print(f"{'raw p':>6} | " + " | ".join(f"{r:^26}" for r in rungs))
    print(f"{'':>6} | " + " | ".join(f"{'post-acc':>9} {'flags/match':>12}" for _ in rungs))

    for p in accuracies:
        cells = []
        for rung in rungs:
            correct = total = flags = runs = 0
            for m in matches:
                for _ in range(trials):
                    obs = [
                        w if rng.random() < p else (3 - w)
                        for w in m["winners"]
                    ]
                    kw = {}
                    if rung in ("final-score", "per-set"):
                        kw["final_sets"] = m["final_sets"]
                    if rung == "per-set":
                        kw["set_end_indices"] = m["set_end_indices"]
                    map_seq, post = solve(obs, p, m["best_of"], **kw)
                    correct += sum(a == b for a, b in zip(map_seq, m["winners"]))
                    total += len(m["winners"])
                    flags += sum(1 for q in post if q < FLAG_POSTERIOR)
                    runs += 1
            cells.append((correct / total, flags / runs))
        row = " | ".join(f"{acc:>9.1%} {fl:>12.1f}" for acc, fl in cells)
        print(f"{p:>6.0%} | {row}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--uniform-only", action="store_true")
    args = ap.parse_args()
    run(args.trials)
    if not args.uniform_only:
        run_confidence(args.trials)
    return 0


if __name__ == "__main__":
    sys.exit(main())
