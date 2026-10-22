#!/usr/bin/env python3
"""Gap 2: Harpe's Python package against two re-implemented trimmers.

Measures latency (p50, p95, p99) in one Python process, on identical inputs:
1. Harpe Python (harpe.ContextBudgetEngine)
2. A recent-first trim modelled on LangChain's trim_messages (not LangChain code)
3. A sliding-window buffer modelled on LlamaIndex's ChatMemoryBuffer (not LlamaIndex code)

- Small context: 10 items, 60-token budget (2,000 iterations)
- Large context: 200 items, 1,500-token budget (500 iterations)

Also reports Harpe Python's 200-item p50 as a share of a 500 ms and a 1,000 ms model call.
The Rust crate's call times come from a separate program (reports/microbenchmarks_phase1.json).
"""

import json
import math
import os
import sys
import time
from typing import List, Dict, Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "..", "harpe", "src"))

from harpe.engine import ContextBudgetEngine
from harpe.models import ContextItem, TokenBudget, PrunePolicy


def count_tokens_heuristic(text: str) -> int:
    words = len(text.split())
    chars = len(text)
    return max(words, math.ceil(chars / 4.0))


# -------------------------------------------------------------
# Recent-first trim modelled on LangChain's trim_messages
# -------------------------------------------------------------
def langchain_trim_messages(
    messages: List[Dict[str, Any]],
    max_tokens: int,
    strategy: str = "last",
    keep_system: bool = True,
) -> List[Dict[str, Any]]:
    """Re-implementation of trim_messages(strategy="last"); not LangChain code."""
    if not messages:
        return []

    system_msg = None
    candidates = []
    for m in messages:
        if keep_system and m.get("role") == "system" and system_msg is None:
            system_msg = m
        else:
            candidates.append(m)

    system_tokens = count_tokens_heuristic(system_msg["content"]) if system_msg else 0
    available = max(0, max_tokens - system_tokens)

    accumulated = 0
    kept = []
    if strategy == "last":
        for m in reversed(candidates):
            t = count_tokens_heuristic(m["content"])
            if accumulated + t <= available:
                accumulated += t
                kept.append(m)
            else:
                break
        kept.reverse()
    else:  # priority or first
        for m in candidates:
            t = count_tokens_heuristic(m["content"])
            if accumulated + t <= available:
                accumulated += t
                kept.append(m)
            else:
                break

    result = []
    if system_msg:
        result.append(system_msg)
    result.extend(kept)
    return result


# -------------------------------------------------------------
# Sliding-window buffer modelled on LlamaIndex's ChatMemoryBuffer
# -------------------------------------------------------------
class LlamaIndexChatMemoryBuffer:
    """Re-implementation of ChatMemoryBuffer's head pruning; not LlamaIndex code."""

    def __init__(self, token_limit: int):
        self.token_limit = token_limit
        self.chat_history: List[Dict[str, Any]] = []

    def set(self, messages: List[Dict[str, Any]]) -> None:
        self.chat_history = list(messages)

    def get(self) -> List[Dict[str, Any]]:
        total_tokens = sum(count_tokens_heuristic(m["content"]) for m in self.chat_history)
        if total_tokens <= self.token_limit:
            return list(self.chat_history)

        # Sliding window pruning from head, preserving system
        pruned = list(self.chat_history)
        while pruned and total_tokens > self.token_limit:
            if pruned[0].get("role") == "system" and len(pruned) > 1:
                removed = pruned.pop(1)
            else:
                removed = pruned.pop(0)
            total_tokens -= count_tokens_heuristic(removed["content"])

        return pruned


def generate_scenario_items(count: int) -> List[Dict[str, Any]]:
    items = []
    # Item 0: system / pinned
    items.append({
        "id": "sys_0",
        "role": "system",
        "content": "You are a high-assurance autonomous agent operating with deterministic context budgeting.",
        "priority": 0,
        "pinned": True,
    })
    for i in range(1, count):
        items.append({
            "id": f"item_{i}",
            "role": "user" if i % 2 == 1 else "assistant",
            "content": f"Context observation step {i}: state parameter delta is {i * 1.732} with status code 200.",
            "priority": (i % 5) + 1,
            "pinned": False,
        })
    return items


def benchmark_function(fn, iterations: int) -> Dict[str, float]:
    latencies_us = []
    # Warmup
    for _ in range(min(50, iterations)):
        fn()

    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        t1 = time.perf_counter_ns()
        latencies_us.append((t1 - t0) / 1000.0)

    latencies_us.sort()
    n = len(latencies_us)
    p50 = latencies_us[int(n * 0.50)]
    p95 = latencies_us[int(n * 0.95)]
    p99 = latencies_us[int(n * 0.99)]
    mean = sum(latencies_us) / n
    return {
        "p50_us": round(p50, 3),
        "p95_us": round(p95, 3),
        "p99_us": round(p99, 3),
        "mean_us": round(mean, 3),
    }


def main():
    print("=" * 80)
    print("   TELARCH GAP 2: HARPE PYTHON LATENCY VS RE-IMPLEMENTED TRIMMERS   ")
    print("        Linux x86_64, Intel Core i5-13500 (20 threads)           ")
    print("=" * 80)

    # 1. Prepare Scenarios
    small_raw = generate_scenario_items(10)
    large_raw = generate_scenario_items(200)

    small_harpe_items = [
        ContextItem(
            id=x["id"],
            content=x["content"],
            priority=x["priority"],
            pinned=x["pinned"],
        )
        for x in small_raw
    ]
    large_harpe_items = [
        ContextItem(
            id=x["id"],
            content=x["content"],
            priority=x["priority"],
            pinned=x["pinned"],
        )
        for x in large_raw
    ]

    small_budget = TokenBudget(total_budget=60)
    large_budget = TokenBudget(total_budget=1500)
    harpe_engine = ContextBudgetEngine()

    small_llamaindex = LlamaIndexChatMemoryBuffer(token_limit=60)
    small_llamaindex.set(small_raw)
    large_llamaindex = LlamaIndexChatMemoryBuffer(token_limit=1500)
    large_llamaindex.set(large_raw)

    # Iteration counts
    small_iters = 2000
    large_iters = 500

    print(f"\n[1/2] Small Context Benchmark (10 items, 60-token budget, {small_iters} iterations):")
    res_small_harpe = benchmark_function(
        lambda: harpe_engine.synthesize(small_harpe_items, small_budget, PrunePolicy.LOWEST_PRIORITY),
        small_iters,
    )
    res_small_langchain = benchmark_function(
        lambda: langchain_trim_messages(small_raw, 60, strategy="last", keep_system=True),
        small_iters,
    )
    res_small_llamaindex = benchmark_function(
        lambda: small_llamaindex.get(),
        small_iters,
    )

    print(f"  • Harpe Python:              p50 = {res_small_harpe['p50_us']} µs | p95 = {res_small_harpe['p95_us']} µs | p99 = {res_small_harpe['p99_us']} µs")
    print(f"  • LangChain-style trim:      p50 = {res_small_langchain['p50_us']} µs | p95 = {res_small_langchain['p95_us']} µs | p99 = {res_small_langchain['p99_us']} µs")
    print(f"  • LlamaIndex-style buffer:   p50 = {res_small_llamaindex['p50_us']} µs | p95 = {res_small_llamaindex['p95_us']} µs | p99 = {res_small_llamaindex['p99_us']} µs")

    print(f"\n[2/2] Large Context Benchmark (200 items, 1,500-token budget, {large_iters} iterations):")
    res_large_harpe = benchmark_function(
        lambda: harpe_engine.synthesize(large_harpe_items, large_budget, PrunePolicy.LOWEST_PRIORITY),
        large_iters,
    )
    res_large_langchain = benchmark_function(
        lambda: langchain_trim_messages(large_raw, 1500, strategy="last", keep_system=True),
        large_iters,
    )
    res_large_llamaindex = benchmark_function(
        lambda: large_llamaindex.get(),
        large_iters,
    )

    print(f"  • Harpe Python:              p50 = {res_large_harpe['p50_us']} µs | p95 = {res_large_harpe['p95_us']} µs | p99 = {res_large_harpe['p99_us']} µs")
    print(f"  • LangChain-style trim:      p50 = {res_large_langchain['p50_us']} µs | p95 = {res_large_langchain['p95_us']} µs | p99 = {res_large_langchain['p99_us']} µs")
    print(f"  • LlamaIndex-style buffer:   p50 = {res_large_llamaindex['p50_us']} µs | p95 = {res_large_llamaindex['p95_us']} µs | p99 = {res_large_llamaindex['p99_us']} µs")

    # Harpe Python's 200-item p50 as a share of one model call (same process as the timings above)
    p50 = res_large_harpe["p50_us"]
    share = {f"llm_turn_{ms}ms_pct": round(p50 / (ms * 10.0), 4) for ms in (500, 1000)}

    print("\n--- HARPE PYTHON, 200 ITEMS (P50), AS A SHARE OF ONE MODEL CALL ---")
    for ms in (500, 1000):
        print(f"  • {ms:,} ms call: {share[f'llm_turn_{ms}ms_pct']}% ({p50} µs)")

    report = {
        "benchmark": "telarch-gap2-harpe-alternatives",
        "suite": "Telarch Suite (TCI)",
        "host": "Linux x86_64, Intel Core i5-13500 (20 threads)",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "small_context_10_items_60_budget": {
            "harpe_python": res_small_harpe,
            "langchain_trim_messages": res_small_langchain,
            "llamaindex_chat_buffer": res_small_llamaindex,
        },
        "large_context_200_items_1500_budget": {
            "harpe_python": res_large_harpe,
            "langchain_trim_messages": res_large_langchain,
            "llamaindex_chat_buffer": res_large_llamaindex,
        },
        "turn_latency_share": {"basis": "harpe_python p50, 200 items", **share},
    }

    out_file = os.path.join(ROOT, "reports", "gap2_harpe_alternatives.json")
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[REPORT] Saved machine-readable report to: {out_file}")


if __name__ == "__main__":
    main()
