#!/usr/bin/env python3
"""Gap 1: Harpe Answer Quality Under a Budget Benchmark.

Evaluates 4 context management strategies across 50 multi-turn evaluation tasks
with planted critical facts, system policies, and distractors over 3 seeds:
1. No Budget (Unbounded accumulation)
2. Sliding Window (FIFO truncate)
3. Recent-first trim modelled on LangChain's trim_messages (not LangChain code)
4. Harpe priority-ordered selection (pinned items kept)

The planted facts carry higher priority than the distractors, so Harpe keeps them by design.

Reports:
- Fact recall accuracy (%) with 95% confidence intervals over 3 seeds
- Token count & reduction ratio (%) with 95% CIs
"""

import json
import math
import os
import random
import sys
from typing import List, Dict, Any

import bench_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "..", "harpe", "src"))

from harpe.engine import ContextBudgetEngine
from harpe.models import ContextItem, TokenBudget, PrunePolicy


def count_tokens_heuristic(text: str) -> int:
    words = len(text.split())
    chars = len(text)
    return max(words, math.ceil(chars / 4.0))


def generate_task(task_id: int, seed: int) -> Dict[str, Any]:
    rng = random.Random(seed * 1000 + task_id)

    # 1. Critical System Policy (Pinned, priority 0)
    policy_rule = f"MANDATE_RULE_{task_id % 7}: Always sign transactions with key auth_key_{task_id}."
    sys_item = {
        "id": "sys_policy",
        "role": "system",
        "content": f"System Security Policy: {policy_rule}",
        "priority": 0,
        "pinned": True,
        "is_target": True,
        "fact_id": "system_policy",
    }

    # 2. Planted Needle Fact (Priority 1)
    secret_code = f"ALPHA_{task_id}_{rng.randint(1000, 9999)}"
    needle_item = {
        "id": "planted_needle",
        "role": "user",
        "content": f"Crucial project milestone detail: the required cryptographic unlock code is {secret_code}.",
        "priority": 1,
        "pinned": False,
        "is_target": True,
        "fact_id": "unlock_code",
    }

    # 3. Intermediate Decision Fact (Priority 2)
    decision = f"DECISION_{task_id}: Route deployment payload through primary gateway cluster {task_id % 3}."
    decision_item = {
        "id": "decision_fact",
        "role": "assistant",
        "content": f"Architectural state record: {decision}",
        "priority": 2,
        "pinned": False,
        "is_target": True,
        "fact_id": "gateway_route",
    }

    # 4. Background Distractors (Priority 3-5, low priority)
    distractors = []
    num_distractors = rng.randint(25, 40)
    for d in range(num_distractors):
        distractors.append({
            "id": f"distractor_{d}",
            "role": "user" if d % 2 == 0 else "assistant",
            "content": f"Background debug trace record {d} for job {task_id}: memory heap offset {hex(rng.randint(0x1000, 0xFFFF))} status normal telemetry loop ping.",
            "priority": rng.randint(3, 5),
            "pinned": False,
            "is_target": False,
            "fact_id": None,
        })

    # Assemble all items in chronological arrival order
    all_items = [sys_item]
    # Insert needle early in conversation (prone to FIFO eviction)
    insert_pos = rng.randint(2, 6)
    all_items.extend(distractors[:insert_pos])
    all_items.append(needle_item)
    # Insert decision midway
    decision_pos = rng.randint(insert_pos + 1, min(insert_pos + 8, len(distractors)))
    all_items.extend(distractors[insert_pos:decision_pos])
    all_items.append(decision_item)
    all_items.extend(distractors[decision_pos:])

    return {
        "task_id": task_id,
        "items": all_items,
        "target_facts": ["system_policy", "unlock_code", "gateway_route"],
    }


def evaluate_retention(selected_items: List[Dict[str, Any]], target_facts: List[str]) -> float:
    retained = set()
    for item in selected_items:
        fid = item.get("fact_id")
        if fid in target_facts:
            retained.add(fid)
    return len(retained) / len(target_facts)


def run_evaluation_seed(seed: int, num_tasks: int = 50, budget_ceiling: int = 400) -> Dict[str, Any]:
    engine = ContextBudgetEngine()
    budget = TokenBudget(total_budget=budget_ceiling)

    results = {
        "no_budget": {"accuracy": [], "tokens": []},
        "sliding_window": {"accuracy": [], "tokens": []},
        "langchain_trim": {"accuracy": [], "tokens": []},
        "harpe_priority": {"accuracy": [], "tokens": []},
    }

    for t in range(num_tasks):
        task = generate_task(t, seed)
        raw_items = task["items"]
        targets = task["target_facts"]

        # 1. No Budget (Unbounded baseline)
        nb_tokens = sum(count_tokens_heuristic(x["content"]) for x in raw_items)
        nb_acc = evaluate_retention(raw_items, targets)
        results["no_budget"]["tokens"].append(nb_tokens)
        results["no_budget"]["accuracy"].append(nb_acc)

        # 2. Sliding Window (head truncation)
        sw_kept = []
        sw_tokens = 0
        for it in reversed(raw_items):
            tk = count_tokens_heuristic(it["content"])
            if sw_tokens + tk <= budget_ceiling:
                sw_tokens += tk
                sw_kept.append(it)
            else:
                break
        sw_kept.reverse()
        sw_acc = evaluate_retention(sw_kept, targets)
        results["sliding_window"]["tokens"].append(sw_tokens)
        results["sliding_window"]["accuracy"].append(sw_acc)

        # 3. LangChain trim_messages (preserves system, recent unpinned)
        lc_kept = []
        sys_item = raw_items[0] if raw_items and raw_items[0]["pinned"] else None
        sys_tk = count_tokens_heuristic(sys_item["content"]) if sys_item else 0
        avail = max(0, budget_ceiling - sys_tk)
        lc_tokens = sys_tk
        unpinned = [x for x in raw_items if not x["pinned"]]
        lc_unpinned = []
        for it in reversed(unpinned):
            tk = count_tokens_heuristic(it["content"])
            if lc_tokens + tk <= budget_ceiling:
                lc_tokens += tk
                lc_unpinned.append(it)
            else:
                break
        lc_unpinned.reverse()
        if sys_item:
            lc_kept.append(sys_item)
        lc_kept.extend(lc_unpinned)
        lc_acc = evaluate_retention(lc_kept, targets)
        results["langchain_trim"]["tokens"].append(lc_tokens)
        results["langchain_trim"]["accuracy"].append(lc_acc)

        # 4. Harpe priority-ordered selection
        harpe_context_items = [
            ContextItem(
                id=x["id"],
                content=x["content"],
                priority=x["priority"],
                pinned=x["pinned"],
                metadata={"fact_id": x.get("fact_id")},
            )
            for x in raw_items
        ]
        report = engine.synthesize(harpe_context_items, budget, PrunePolicy.LOWEST_PRIORITY)
        harpe_selected = [
            {"id": x.id, "fact_id": x.metadata.get("fact_id") if x.metadata else None}
            for x in report.included_items
        ]
        harpe_acc = evaluate_retention(harpe_selected, targets)
        results["harpe_priority"]["tokens"].append(report.consumed_tokens)
        results["harpe_priority"]["accuracy"].append(harpe_acc)

    return results


def calc_ci95(data: List[float]) -> Dict[str, float]:
    n = len(data)
    if n == 0:
        return {"mean": 0.0, "ci_half": 0.0, "low": 0.0, "high": 0.0}
    mean = sum(data) / n
    variance = sum((x - mean) ** 2 for x in data) / max(1, n - 1)
    std = math.sqrt(variance)
    # Student's t-value for 95% CI (df=2 for 3 seeds is ~4.303; if across all samples use normal 1.96)
    t_val = 1.96 if n > 30 else 4.303
    ci_half = t_val * (std / math.sqrt(n))
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "ci_half": round(ci_half, 4),
        "ci_95_low": round(max(0.0, mean - ci_half), 4),
        "ci_95_high": round(mean + ci_half, 4),
    }


def main():
    print("=" * 80)
    print("      TELARCH GAP 1: HARPE ANSWER QUALITY UNDER BUDGET BENCHMARK        ")
    print("       50 Evaluation Tasks | 3 Seeds | 400-Token Budget                 ")
    print("========================================================================\n")

    seeds = [42, 1337, 2026]
    seed_runs = []
    for s in seeds:
        print(f"Running evaluation with random seed {s}...")
        seed_runs.append(run_evaluation_seed(s, num_tasks=50, budget_ceiling=400))

    strategies = ["no_budget", "sliding_window", "langchain_trim", "harpe_priority"]
    summary = {}

    print("\n--- RESULTS OVER 3 SEEDS (50 TASKS EACH, N=150 TOTAL EVALUATIONS) ---")
    for strat in strategies:
        all_acc = []
        all_tokens = []
        for r in seed_runs:
            all_acc.extend(r[strat]["accuracy"])
            all_tokens.extend(r[strat]["tokens"])

        acc_ci = calc_ci95(all_acc)
        tok_ci = calc_ci95(all_tokens)

        summary[strat] = {
            "accuracy_pct": {
                "mean": round(acc_ci["mean"] * 100.0, 2),
                "ci_95_low": round(acc_ci["ci_95_low"] * 100.0, 2),
                "ci_95_high": round(acc_ci["ci_95_high"] * 100.0, 2),
            },
            "tokens_per_task": tok_ci,
        }

        print(f"\n[{strat.upper()}]")
        print(f"  • Accuracy (Key Fact Recall): {summary[strat]['accuracy_pct']['mean']}% (95% CI: [{summary[strat]['accuracy_pct']['ci_95_low']}%, {summary[strat]['accuracy_pct']['ci_95_high']}%])")
        print(f"  • Mean Tokens / Task:         {tok_ci['mean']} (CI: [{tok_ci['ci_95_low']}, {tok_ci['ci_95_high']}])")

    # Compute comparative metrics vs no_budget
    nb_tokens = summary["no_budget"]["tokens_per_task"]["mean"]
    harpe_tokens = summary["harpe_priority"]["tokens_per_task"]["mean"]
    token_savings_pct = round((1.0 - (harpe_tokens / nb_tokens)) * 100.0, 2)
    harpe_acc = summary["harpe_priority"]["accuracy_pct"]["mean"]
    sw_acc = summary["sliding_window"]["accuracy_pct"]["mean"]
    lc_acc = summary["langchain_trim"]["accuracy_pct"]["mean"]

    print("\n--- COMPARATIVE SUMMARY (AT 400-TOKEN CEILING) ---")
    print(f"  • Harpe Token Reduction:      {token_savings_pct}% fewer tokens vs unbudgeted baseline")
    print(f"  • Fact Recall Retention:      {harpe_acc}% (Harpe) vs {sw_acc}% (Sliding Window) vs {lc_acc}% (LangChain trim)")

    final_report = {
        "benchmark": "telarch-gap1-harpe-quality",
        "suite": "Telarch Suite (TCI)",
        "host": "Linux x86_64, Intel Core i5-13500 (20 threads)",
        "timestamp_utc": bench_env.utc_now(),
        "evaluation_tasks_per_seed": 50,
        "seeds": seeds,
        "budget_ceiling_tokens": 400,
        "summary": summary,
        "comparison": {
            "harpe_token_savings_pct": token_savings_pct,
            "harpe_accuracy_pct": harpe_acc,
            "sliding_window_accuracy_pct": sw_acc,
            "langchain_trim_accuracy_pct": lc_acc,
        },
    }

    out_file = os.path.join(ROOT, "reports", "gap1_harpe_quality.json")
    with open(out_file, "w") as f:
        json.dump(final_report, f, indent=2)
    print(f"\n[REPORT] Saved machine-readable report to: {out_file}")


if __name__ == "__main__":
    main()
