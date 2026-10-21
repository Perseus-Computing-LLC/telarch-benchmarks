"""Run LongMemEval-V2 through its official harness (unchanged) with the Telarch backend or the official
no-retrieval baseline, text only, and record the outputs' SHA-256 in an Argus ledger.

    venv311/bin/python -m lme_v2.run_lmev2 --method telarch --domain web --tier small --limit 10 \
        --data-root /opt/data/work/lme-v2/data/longmemeval-v2 --env-file /opt/data/.env

Run it from telarch-benchmarks/evals with LongMemEval-V2 on PYTHONPATH (or installed). --official-reader uses the
leaderboard's fixed reader as the official runner does: Qwen/Qwen3.5-9B on an OpenAI-compatible server
(--reader-base-url, key from --reader-key-file), temperature 0.6, top_p 0.95, top_k 20, 20,000 completion tokens,
thinking on. Without it the reader defaults to gpt-4o-mini, with these deviations, all forced by the OpenAI reader:
no top_k (OpenAI rejects it), no Qwen thinking switch, and --max-completion-tokens 4096 (gpt-4o-mini caps output at
16,384). The judge is the official one: gpt-5.2, medium reasoning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("TELARCH_EVALS_HOME", "/opt/data/work/telarch-evals"))


def _load_key(env_file: str | None, name: str) -> None:
    """Put `name` into this process's environment from a dotenv file when it is not exported. Never printed."""
    if os.environ.get(name) or not env_file:
        return
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            os.environ[name] = line.split("=", 1)[1].strip().strip('"').strip("'")
            return


def _round2_params(config_path: Path) -> dict[str, object]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    frozen = config.get("frozen") or config["reference"]
    keys = ("budget", "candidates", "per_term", "policy", "counter", "planner", "fill_floor", "recency", "mmr", "bridge")
    params = {k: frozen[k] for k in keys if k in frozen}
    params["encrypt"] = bool(frozen.get("encrypted", True))
    return params


USAGE: dict[str, dict[str, int]] = {}


def _tally(model: object, usage: object) -> None:
    if usage is None:
        return
    entry = USAGE.setdefault(str(model), {"calls": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0})
    entry["calls"] += 1
    get = (lambda k: getattr(usage, k, None)) if not isinstance(usage, dict) else usage.get
    entry["input_tokens"] += int(get("prompt_tokens") or get("input_tokens") or 0)
    entry["output_tokens"] += int(get("completion_tokens") or get("output_tokens") or 0)
    details = get("completion_tokens_details") or get("output_tokens_details")
    if details is not None:
        rd = details.get("reasoning_tokens") if isinstance(details, dict) else getattr(details, "reasoning_tokens", None)
        entry["reasoning_tokens"] += int(rd or 0)


def _tap_openai_usage() -> None:
    """Count tokens per model for every OpenAI call in this process (reader and judge). Observes only: each
    request and response is passed through unchanged. The harness records the reader's usage, not the judge's."""
    import functools

    from openai.resources.chat import completions as chat
    from openai.resources import responses

    def wrap(cls, name, is_async):
        original = getattr(cls, name)
        if getattr(original, "_telarch_tap", False):
            return
        if is_async:
            @functools.wraps(original)
            async def tapped(self, *a, **kw):
                result = await original(self, *a, **kw)
                _tally(getattr(result, "model", kw.get("model")), getattr(result, "usage", None))
                return result
        else:
            @functools.wraps(original)
            def tapped(self, *a, **kw):
                result = original(self, *a, **kw)
                _tally(getattr(result, "model", kw.get("model")), getattr(result, "usage", None))
                return result
        tapped._telarch_tap = True
        setattr(cls, name, tapped)

    wrap(chat.Completions, "create", False)
    wrap(chat.AsyncCompletions, "create", True)
    wrap(responses.Responses, "create", False)
    wrap(responses.AsyncResponses, "create", True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _ledger_record(output_dir: Path, run_config: dict[str, object]) -> dict[str, object]:
    try:
        from argus_ledger import AppendOnlyLedger
    except ImportError:  # older source checkouts name it argus
        from argus import AppendOnlyLedger
    files = {str(p.relative_to(output_dir)): _sha256(p) for p in sorted(output_dir.rglob("*"))
             if p.is_file() and "runtime_inputs" not in p.parts and "telarch-vaults" not in p.parts
             and p.suffix in (".json", ".jsonl")}
    ledger_dir = HOME / "lme-v2"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    payload = {"output_dir": str(output_dir), "files": files, "config": run_config}
    AppendOnlyLedger(str(ledger_dir / "ledger.jsonl")).append("lmev2.run.completed", "telarch-evals", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--method", choices=["telarch", "no_retrieval"], required=True)
    p.add_argument("--domain", choices=["web", "enterprise"], required=True)
    p.add_argument("--tier", choices=["small", "medium"], default="small")
    p.add_argument("--limit", type=int, help="first N selected questions")
    p.add_argument("--question-ids", help="comma-separated question ids")
    p.add_argument("--data-root", type=Path, default=Path("/opt/data/work/lme-v2/data/longmemeval-v2"))
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--round2-config", type=Path, default=HOME / "reports/round2-config.json")
    p.add_argument("--reader-model", default="gpt-4o-mini-2024-07-18")
    p.add_argument("--reader-base-url", default="https://api.openai.com/v1")
    p.add_argument("--reader-temperature", type=float, default=0.6)
    p.add_argument("--reader-top-p", type=float, default=0.95)
    p.add_argument("--reader-top-k", type=int, help="reader top_k (the official Qwen3.5-9B setting is 20)")
    p.add_argument("--reader-thinking", choices=["on", "off"], default="on",
                   help="Qwen3.5-9B thinking (official: on); the harness sends the switch only to Qwen/Qwen3.5-9B")
    p.add_argument("--reader-api-key-env", default="OPENAI_API_KEY",
                   help="environment variable with the reader endpoint's key (default OPENAI_API_KEY)")
    p.add_argument("--reader-key-file", type=Path,
                   help="file with the reader endpoint's key, read into this process only (LMEV2_READER_API_KEY)")
    p.add_argument("--official-reader", action="store_true",
                   help="the leaderboard's fixed reader: Qwen/Qwen3.5-9B, temperature 0.6, top_p 0.95, top_k 20, "
                        "20,000 completion tokens, thinking on; needs --reader-base-url")
    p.add_argument("--max-completion-tokens", type=int, default=4096)
    p.add_argument("--memory-context-max-tokens", type=int, default=200000)
    p.add_argument("--reader-max-concurrent-requests", type=int, default=16)
    p.add_argument("--evaluator-model", default="gpt-5.2")
    p.add_argument("--evaluator-reasoning-effort", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--evaluator-max-completion-tokens", type=int, default=4096)
    p.add_argument("--env-file", help="dotenv file with OPENAI_API_KEY when it is not exported")
    args = p.parse_args(argv)

    from data.public_data import materialize_runtime_haystack, materialize_runtime_questions
    from lme_v2 import telarch_backend  # noqa: F401 - registers memory_type "telarch"

    if args.official_reader:
        if args.reader_base_url == "https://api.openai.com/v1":
            raise SystemExit("--official-reader needs --reader-base-url: an OpenAI-compatible Qwen/Qwen3.5-9B server")
        args.reader_model, args.reader_temperature, args.reader_top_p = "Qwen/Qwen3.5-9B", 0.6, 0.95
        args.reader_top_k, args.max_completion_tokens, args.reader_thinking = 20, 20000, "on"
    if args.reader_key_file:
        os.environ["LMEV2_READER_API_KEY"] = args.reader_key_file.read_text(encoding="utf-8").strip()
        args.reader_api_key_env = "LMEV2_READER_API_KEY"
    _load_key(args.env_file, "OPENAI_API_KEY")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set (pass --env-file)")
    if not os.environ.get(args.reader_api_key_env):
        raise SystemExit(f"{args.reader_api_key_env} is not set (the reader's key)")
    data_root = args.data_root.expanduser().resolve()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output_dir = (args.output_dir or HOME / "lme-v2/runs" / f"{stamp}-{args.method}-{args.domain}-{args.tier}").resolve()
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ids = [q.strip() for q in args.question_ids.split(",")] if args.question_ids else None
    selected = materialize_runtime_questions(data_root=data_root, domain=args.domain, question_ids=ids,
                                             limit=args.limit, output_path=runtime_dir / "questions.json")
    materialize_runtime_haystack(data_root=data_root, tier=args.tier, selected_questions=selected,
                                 output_path=runtime_dir / "haystack.json")
    if args.method == "telarch":
        memory_config = {"memory_type": "telarch", "memory_params": _round2_params(args.round2_config)}
    else:
        memory_config = {"memory_type": "no_retrieval", "memory_params": {}}
    (runtime_dir / "memory_config.json").write_text(json.dumps(memory_config, indent=2) + "\n", encoding="utf-8")
    harness_argv = [
        "evaluation.harness", "--domain", args.domain,
        "--questions-path", str(runtime_dir / "questions.json"),
        "--haystack-path", str(runtime_dir / "haystack.json"),
        "--trajectories-path", str(data_root / "trajectories.jsonl"),
        "--memory-config-path", str(runtime_dir / "memory_config.json"),
        "--output-dir", str(output_dir),
        "--model", args.reader_model, "--base-url", args.reader_base_url, "--api-key-env", args.reader_api_key_env,
        "--temperature", str(args.reader_temperature), "--top-p", str(args.reader_top_p),
        *(["--top-k", str(args.reader_top_k)] if args.reader_top_k is not None else []),
        *(["--reader-disable-thinking"] if args.reader_thinking == "off" else []),
        "--max-completion-tokens", str(args.max_completion_tokens),
        "--memory-context-max-tokens", str(args.memory_context_max_tokens),
        "--reader-max-concurrent-requests", str(args.reader_max_concurrent_requests),
        "--prompt-build-max-workers", "1",
        "--evaluator-model", args.evaluator_model, "--evaluator-api-key-env", "OPENAI_API_KEY",
        "--evaluator-reasoning-effort", args.evaluator_reasoning_effort,
        "--evaluator-max-completion-tokens", str(args.evaluator_max_completion_tokens),
    ]
    run_config = {"method": args.method, "domain": args.domain, "tier": args.tier, "limit": args.limit,
                  "question_ids": ids, "memory_config": memory_config, "harness_argv": harness_argv[1:],
                  "lme_v2_commit": os.environ.get("LMEV2_COMMIT"), "dataset_revision": os.environ.get("LMEV2_REVISION")}
    (output_dir / "telarch_run_config.json").write_text(json.dumps(run_config, indent=2) + "\n", encoding="utf-8")
    _tap_openai_usage()
    old = sys.argv
    try:
        sys.argv = harness_argv
        from evaluation.harness import main as harness_main

        harness_main()
    finally:
        sys.argv = old
        (output_dir / "usage_by_model.json").write_text(json.dumps(USAGE, indent=2, sort_keys=True) + "\n",
                                                        encoding="utf-8")
    record = _ledger_record(output_dir, run_config)
    print(json.dumps({"output_dir": str(output_dir), "ledger_files": len(record["files"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
