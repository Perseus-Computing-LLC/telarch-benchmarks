"""telarch-evals: long-term memory benchmarks for the Telarch libraries.

LongMemEval, LoCoMo, BEAM and MemoryAgentBench.

  telarch-evals fetch {longmemeval,locomo,beam,memoryagentbench,all}   download and hash-check the inputs
  telarch-evals longmemeval|locomo|beam|memoryagentbench [options]     run a benchmark
  telarch-evals summarize RUN_DIR                     recompute a run's summary from its rows
  telarch-evals verify                                check the run ledger and the run files
  telarch-evals sources                               list the pinned inputs and their licenses
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import (
    __version__,
    bench_beam,
    bench_locomo,
    bench_longmemeval,
    bench_memoryagentbench,
)
from .common import Context, OfflineClient
from .llm import Cache, ChatClient, Meter
from .memory import make_system, COUNTERS, PLANNERS
from .official import BEAM_ABILITIES
from .runlog import verify
from .sources import (
    BEAM_SIZES,
    LONGMEMEVAL_SPLITS,
    MAB_CODE,
    MAB_SPLITS,
    SOURCES,
    beam_conversations,
    fetch,
    home,
    import_local,
)

DEFAULTS = {
    "longmemeval": {"reader": "gpt-4o-mini-2024-07-18", "judge": bench_longmemeval.OFFICIAL_JUDGE,
                    "reader_max_tokens": 1024, "judge_max_tokens": 10, "reader_temperature": 0.0},
    "locomo": {"reader": "gpt-4o-mini-2024-07-18", "judge": "gpt-4o-mini", "reader_max_tokens": 200,
               "judge_max_tokens": 256, "reader_temperature": 0.0},
    "beam": {"reader": "gpt-4o-mini-2024-07-18", "judge": bench_beam.OFFICIAL_JUDGE, "reader_max_tokens": 2048,
             "judge_max_tokens": 1024, "reader_temperature": 0.0},
    # Each MemoryAgentBench dataset sets its own generation limit; reader_max_tokens is only a fallback.
    "memoryagentbench": {"reader": "gpt-4o-mini-2024-07-18", "judge": bench_memoryagentbench.OFFICIAL_JUDGE,
                         "reader_max_tokens": 2000, "judge_max_tokens": 10,
                         "reader_temperature": bench_memoryagentbench.READER_TEMPERATURE},
}
POLICIES = ("least_relevant", "knapsack_dp", "lowest_priority", "duo_attention")


def _common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("memory system")
    g.add_argument("--system", choices=["telarch", "full-context", "none"], default="telarch",
                   help="telarch (Kibisis + Harpe), full-context baseline, or no memory (default telarch)")
    g.add_argument("--budget", type=int, help="context token budget (default 8000 for telarch, 100000 for full-context)")
    g.add_argument("--candidates", type=int, default=200, help="Kibisis candidates handed to Harpe (default 200)")
    g.add_argument("--per-term", type=int, default=50, help="Kibisis results per query word (default 50)")
    g.add_argument("--policy", choices=POLICIES, default="least_relevant",
                   help="Harpe selection policy (default least_relevant: most relevant first until the budget is full)")
    g.add_argument("--admission-gate", action="store_true", help="keep Kibisis write-time admission gating on")
    g.add_argument("--plaintext", action="store_true", help="store histories unencrypted (default: encrypted)")
    g.add_argument("--no-memory-security", action="store_true",
                   help="open encrypted vaults with kibisis memory_security=False: SQLCipher's process-wide "
                        "cipher_memory_security stays off (default: on, as kibisis ships)")
    g.add_argument("--counter", choices=COUNTERS, default="o200k",
                   help="token counter Harpe packs with: o200k (the reader's tokens, default) or heuristic (Harpe's own)")
    g.add_argument("--planner", choices=sorted(PLANNERS), default="fanout",
                   help="fanout: one Kibisis search per content word (default); any: one any-word BM25 search")
    g.add_argument("--fill-floor", type=float, default=0.0,
                   help="widen retrieval while Harpe fills less than this share of the budget (default 0: off)")
    g.add_argument("--passthrough-under", type=int, default=0, metavar="TOKENS",
                   help="hand histories of at most this many reader tokens over whole, as full-context does (default 0: off)")
    g.add_argument("--recency", type=float, default=0.0, help="boost newer units: score x (1 + R x position) (default 0)")
    g.add_argument("--mmr", type=float, default=0.0, help="MMR diversity weight over word overlap (default 0: off)")
    g.add_argument("--bridge", type=int, default=0, metavar="N",
                   help="2-hop: search again with names from the top N hits (default 0: off)")
    m = p.add_argument_group("models")
    m.add_argument("--reader-model", help="model that answers the questions")
    m.add_argument("--judge-model", help="judge model (default: the benchmark's official judge)")
    m.add_argument("--reader-max-tokens", type=int)
    m.add_argument("--reader-temperature", type=float,
                   help="reader temperature (default: the benchmark's protocol; 0.7 for MemoryAgentBench, else 0)")
    m.add_argument("--base-url", help="OpenAI-compatible endpoint for both models (default api.openai.com)")
    m.add_argument("--judge-base-url", help="endpoint for the judge, if different")
    m.add_argument("--api-key-env", default="OPENAI_API_KEY",
                   help="name of the variable holding the API key; NONE for a keyless local server")
    m.add_argument("--judge-api-key-env", help="key variable for the judge, if different")
    m.add_argument("--env-file", help="dotenv file to read the key variable from when it is not exported")
    m.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT",
                   help="USD per million input,output tokens for a model missing from the price table")
    r = p.add_argument_group("run")
    r.add_argument("--workers", type=int, default=4, help="parallel model calls (default 4)")
    r.add_argument("--max-usd", type=float, help="stop starting questions once estimated spend reaches this")
    r.add_argument("--resume", type=Path, help="continue an earlier run directory with the same settings")
    r.add_argument("--runs-dir", type=Path, help="where runs and the run ledger go (default $TELARCH_EVALS_HOME/runs)")
    r.add_argument("--no-cache", action="store_true", help="do not reuse cached model responses")
    r.add_argument("--retrieval-only", action="store_true", help="$0 screen: memory and evidence recall only, no model calls; runs go to $TELARCH_EVALS_HOME/screens")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="telarch-evals", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download and hash-check the pinned inputs")
    f.add_argument("benchmark", choices=["longmemeval", "locomo", "beam", "memoryagentbench", "all"])
    f.add_argument("--split", choices=sorted(LONGMEMEVAL_SPLITS), default="s", help="LongMemEval split (default s)")
    f.add_argument("--size", choices=BEAM_SIZES, default="100K", help="BEAM size (default 100K)")
    f.add_argument("--import-file", type=Path, help="seed the cache from a local copy of the dataset file")

    sub.add_parser("sources", help="list the pinned inputs and their licenses")
    s = sub.add_parser("summarize", help="recompute a run's summary from judgments.jsonl")
    s.add_argument("run_dir", type=Path)
    v = sub.add_parser("verify", help="check the run ledger's hash chain and every completed run's files")
    v.add_argument("--runs-dir", type=Path)

    lme = sub.add_parser("longmemeval", help="LongMemEval with the official reader prompt and judge")
    _common(lme)
    lme.add_argument("--split", choices=sorted(LONGMEMEVAL_SPLITS), default="s")
    lme.add_argument("--limit", type=int, help="first N questions")
    lme.add_argument("--per-type", type=int, help="first N questions of each question type")
    lme.add_argument("--types", help="comma-separated question types")
    lme.add_argument("--ids", type=Path, help="file with one question_id per line")
    lme.add_argument("--no-cot", action="store_true", help="the plain reader template instead of chain-of-thought")
    lme.add_argument("--data", type=Path, help="a local LongMemEval-format file instead of the pinned split")

    loc = sub.add_parser("locomo", help="LoCoMo with the J judge and token F1")
    _common(loc)
    loc.add_argument("--conversations", type=int, help="first N of the 10 conversations")
    loc.add_argument("--per-conversation", type=int, help="first N selected questions of each conversation")
    loc.add_argument("--categories", default="1,2,3,4", help="question categories (default 1,2,3,4)")
    loc.add_argument("--data", type=Path, help="a local LoCoMo-format file instead of the pinned locomo10.json")

    beam = sub.add_parser("beam", help="BEAM with its per-ability evaluators")
    _common(beam)
    beam.add_argument("--size", choices=BEAM_SIZES, default="100K")
    beam.add_argument("--conversations", help="comma-separated conversation ids (default: all of that size)")
    beam.add_argument("--max-conversations", type=int, help="first N conversations")
    beam.add_argument("--per-ability", type=int, help="first N questions of each ability (each has 2)")
    beam.add_argument("--abilities", help="comma-separated abilities (default: all ten)")
    beam.add_argument("--data-dir", type=Path, help="local directory of <conversation>/chat.json and probing_questions/")

    mab = sub.add_parser("memoryagentbench", help="MemoryAgentBench: the 14 datasets of the paper's Table 3")
    _common(mab)
    mab.add_argument("--datasets", help="comma-separated dataset sources (default: all 14)")
    mab.add_argument("--per-dataset", type=int, help="first N questions of each dataset")
    mab.add_argument("--rows", type=int, help="first N contexts of each dataset")
    mab.add_argument("--data-dir", type=Path, help="local directory of <split>.parquet or <split>.json files")
    return p


def _prices(items: list[str]) -> dict[str, tuple[float, float]]:
    out = {}
    for item in items:
        model, _, pair = item.partition("=")
        inp, _, outp = pair.partition(",")
        out[model.strip()] = (float(inp), float(outp))
    return out


def cmd_fetch(args: argparse.Namespace) -> int:
    keys: list[str] = []
    if args.benchmark in ("longmemeval", "all"):
        keys += [LONGMEMEVAL_SPLITS[args.split], "longmemeval/evaluate_qa.py", "longmemeval/run_generation.py"]
    if args.benchmark in ("locomo", "all"):
        keys += ["locomo/locomo10.json", "mem0/llm_judge.py"]
    if args.benchmark in ("beam", "all"):
        keys += ["beam/compute_metrics.py", "beam/prompts.py"]
        for conv in beam_conversations(args.size):
            keys += [f"beam/chats/{args.size}/{conv}/chat.json",
                     f"beam/chats/{args.size}/{conv}/probing_questions/probing_questions.json"]
    if args.benchmark in ("memoryagentbench", "all"):
        keys += [f"memoryagentbench/{split}.parquet" for split in MAB_SPLITS] + list(MAB_CODE)
    if args.import_file:
        if args.benchmark not in ("longmemeval", "locomo"):
            raise SystemExit("--import-file works with longmemeval or locomo")
        print(import_local(keys[0], args.import_file))
    for key in keys:
        fetch(key)
    print(f"{len(keys)} files ready under {home() / 'sources'}")
    return 0


def cmd_sources() -> int:
    for key, src in SOURCES.items():
        print(f"{key:44} {src.license:13} {src.url}")
    print(f"{'beam/chats/<size>/<id>/...':44} {'MIT':13} 200 files pinned by git blob SHA-1 in data/beam_manifest.json")
    return 0


def cmd_summarize(run_dir: Path) -> int:
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    rows = [json.loads(line) for line in (run_dir / "judgments.jsonl").read_text("utf-8").splitlines() if line.strip()]
    module = {"longmemeval": bench_longmemeval, "locomo": bench_locomo, "beam": bench_beam,
              "memoryagentbench": bench_memoryagentbench}[manifest["benchmark"]]
    print(json.dumps(module.summarize(rows), indent=2, sort_keys=True))
    return 0


def headline(summary: dict) -> str:
    bench = summary.get("benchmark")
    lines = [f"{bench}: {summary.get('rows')} scored, {summary.get('errors')} errors, "
             f"{'complete' if summary.get('complete') else 'INCOMPLETE'}"
             + (f" (stopped: {summary['stopped']})" if summary.get("stopped") else "")]
    if bench == "longmemeval":
        lines.append(f"  overall accuracy {summary.get('overall_accuracy')}, task-averaged {summary.get('task_averaged_accuracy')}")
        for name, v in summary.get("by_question_type", {}).items():
            lines.append(f"    {name:28} {v['accuracy']}  (n={v['n']})")
        lines.append(f"  evidence session recall {summary.get('evidence_session_recall')}")
    elif bench == "locomo":
        lines.append(f"  J {summary.get('j_overall')}, F1 {summary.get('f1_overall')}, evidence recall {summary.get('evidence_recall')}")
        for name, v in summary.get("by_category", {}).items():
            lines.append(f"    {name:14} J {v['j']}  F1 {v['f1']}  (n={v['n']})")
    elif bench == "beam":
        lines.append(f"  overall {summary.get('overall')}")
        for name, v in summary.get("by_ability", {}).items():
            lines.append(f"    {name:26} {v['score']}  (questions={v['questions']})")
    elif bench == "memoryagentbench":
        cats = summary.get("categories", {})
        lines.append(f"  overall {summary.get('overall')}  ({', '.join(f'{k} {v}' for k, v in cats.items())})")
        lines.append("    " + "  ".join(f"{k} {v}" for k, v in summary.get("columns", {}).items()))
    ctx_tokens = summary.get("context_tokens", {})
    if ctx_tokens.get("n"):
        lines.append(f"  context tokens mean {ctx_tokens['mean']}, p95 {ctx_tokens['p95']}")
    usage = summary.get("usage", {})
    if usage:
        lines.append(f"  estimated spend ${usage.get('estimated_usd')} (list prices)"
                     + (f"; unpriced: {', '.join(usage['unpriced_models'])}" if usage.get("unpriced_models") else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "sources":
        return cmd_sources()
    if args.command == "summarize":
        return cmd_summarize(args.run_dir)
    if args.command == "verify":
        result = verify(args.runs_dir or home() / "runs")
        print(json.dumps(result, indent=2))
        return 0 if result.get("valid") else 1

    bench = args.command
    defaults = DEFAULTS[bench]
    judge_model = args.judge_model or defaults["judge"]
    if judge_model != defaults["judge"]:
        print(f"note: {judge_model} is not the official {bench} judge ({defaults['judge']}); scores will not be "
              "comparable with published results", file=sys.stderr)
    meter = Meter(_prices(args.price))
    cache = None if args.no_cache else Cache(home() / "llm-cache.sqlite")
    temperature = defaults["reader_temperature"] if args.reader_temperature is None else args.reader_temperature
    if args.retrieval_only:
        reader, judge = OfflineClient("reader"), OfflineClient("judge")
    else:
        reader = ChatClient(args.reader_model or defaults["reader"], base_url=args.base_url,
                            api_key_env=args.api_key_env, env_file=args.env_file,
                            max_tokens=args.reader_max_tokens or defaults["reader_max_tokens"],
                            temperature=temperature, cache=cache, meter=meter)
        judge = ChatClient(judge_model, base_url=args.judge_base_url or args.base_url,
                           api_key_env=args.judge_api_key_env or args.api_key_env, env_file=args.env_file,
                           max_tokens=defaults["judge_max_tokens"], cache=cache, meter=meter)
    (home() / "work").mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="memory-", dir=home() / "work"))
    try:
        system = make_system(args.system, workdir, budget=args.budget, candidates=args.candidates,
                             per_term=args.per_term, policy=args.policy, admission_gate=args.admission_gate,
                             encrypt=not args.plaintext,
                             reader_max_tokens=args.reader_max_tokens or defaults["reader_max_tokens"],
                             **({"counter": args.counter, "planner": args.planner, "fill_floor": args.fill_floor,
                                 "passthrough_under": args.passthrough_under, "recency": args.recency,
                                 "mmr": args.mmr, "bridge": args.bridge,
                                 "memory_security": not args.no_memory_security} if args.system == "telarch" else {}))
        runs_root = args.runs_dir or home() / ("screens" if args.retrieval_only else "runs")
        ctx = Context(runs_root=runs_root, workdir=workdir, system=system, reader=reader, judge=judge, meter=meter,
                      workers=args.workers, max_usd=args.max_usd, resume=args.resume,
                      retrieval_only=args.retrieval_only)
        if bench == "longmemeval":
            ids = set(args.ids.read_text("utf-8").split()) if args.ids else None
            types = {t.strip() for t in args.types.split(",")} if args.types else None
            out = bench_longmemeval.run(ctx, split=args.split, cot=not args.no_cot, limit=args.limit,
                                        per_type=args.per_type, ids=ids, types=types, data_path=args.data)
        elif bench == "locomo":
            categories = {int(c) for c in args.categories.split(",") if c.strip()}
            out = bench_locomo.run(ctx, conversations=args.conversations, per_conversation=args.per_conversation,
                                   categories=categories, data_path=args.data)
        elif bench == "memoryagentbench":
            datasets = [d.strip() for d in args.datasets.split(",") if d.strip()] if args.datasets else None
            out = bench_memoryagentbench.run(ctx, datasets=datasets, per_dataset=args.per_dataset, rows=args.rows,
                                             data_dir=args.data_dir)
        else:
            convs = [c.strip() for c in args.conversations.split(",")] if args.conversations else None
            if convs is None and args.max_conversations:
                convs = beam_conversations(args.size)[: args.max_conversations]
            abilities = [a.strip() for a in args.abilities.split(",")] if args.abilities else None
            unknown = set(abilities or []) - set(BEAM_ABILITIES)
            if unknown:
                raise SystemExit(f"unknown BEAM abilities: {', '.join(sorted(unknown))}")
            out = bench_beam.run(ctx, size=args.size, conversations=convs, per_ability=args.per_ability,
                                 abilities=abilities, data_dir=args.data_dir,
                                 reader_max_tokens=args.reader_max_tokens or defaults["reader_max_tokens"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    print(headline(summary))
    print(f"run directory: {out}")
    return 0 if summary.get("complete") else 3
