# Memory evaluations

`telarch-evals` runs three public long-term memory benchmarks against the Telarch libraries. [Kibisis](https://github.com/Perseus-Computing-LLC/kibisis) stores each conversation history and recalls candidates for a question. [Harpe](https://github.com/Perseus-Computing-LLC/harpe) picks the candidates that fit the token budget. [Argus](https://github.com/Perseus-Computing-LLC/argus) records every run in a hash-chained ledger.

Each benchmark keeps its own scoring. The official prompts and scoring code are downloaded from pinned upstream commits and checked against recorded SHA-256 hashes before use. None of that code or data is copied into this repository.

No memory-quality figure from these evaluations is published on telarch.ai yet.

## The benchmarks

| Benchmark | Data | Reader prompt | Judge (default) | Scores |
| --- | --- | --- | --- | --- |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | 500 questions. Each has its own chat history, about 115,000 tokens in the S split. The M split and the oracle split are also available. | The official template, chain of thought | `gpt-4o-2024-08-06` with the official prompt | Overall and task-averaged accuracy, accuracy per question type, abstention accuracy |
| [LoCoMo](https://github.com/snap-research/locomo) | 10 conversations between two people. 1,540 questions in categories 1 to 4, or 1,986 with the adversarial category 5. | This harness's own (see `bench_locomo.py`) | `gpt-4o-mini` with the J prompt from mem0's evaluation code | J (LLM judge), token F1, both per category |
| [BEAM](https://github.com/mohammadtavakoli78/BEAM) | 100 conversations at 100K, 500K, 1M and 10M tokens. 20 questions each across 10 memory abilities. | BEAM's `answer_generation_for_rag` | `gpt-4.1-mini` with BEAM's evaluator for each ability | Score per ability, overall mean |

Every benchmark also reports **evidence recall**: the share of the gold evidence that made it into the context the reader saw. It separates memory misses from reader misses.

## What answers the questions

`--system` picks what supplies the context:

- `telarch` (default). Every round of the history goes into an encrypted Kibisis vault, one vault per history, with a random passphrase that never leaves the process. The question is reduced to its content words. Each word is searched in Kibisis, and the BM25 scores are summed, which matches the BM25 score of an OR query. Harpe then takes the top 200 candidates and fills an 8,000-token budget, most relevant first (`least_relevant`).
  - That policy was chosen on evidence recall, a retrieval-only measure with no model calls, measured on 60 LongMemEval-S questions (10 per type). Evidence recall was 0.889 per session against 0.828 for `knapsack_dp`. Harpe's knapsack policy gives every item a fixed share of utility, so it can prefer several small, weakly related rounds over one large relevant round.
  - Harpe treats items of priority 0 as pinned, kept regardless of the budget, so the adapter uses priorities 1 to 100.
- `full-context`. The history itself, newest first, until 100,000 tokens. This is the long-context baseline.
- `none`. No history. This is the floor.

Settings that change the result are recorded in each run's manifest: `--budget`, `--candidates`, `--per-term`, `--policy`, `--admission-gate` and `--plaintext`.

## Set up

```bash
make evals-setup   # evals/.venv with telarch-evals and its pinned dependencies (uv if present, else pip)
make evals-test    # tests with fake models; the tests marked "network" also fetch the official code
```

The model API key is read from an environment variable by name, `OPENAI_API_KEY` unless `--api-key-env` says otherwise. If it is not exported, `--env-file path/.env` reads that one variable from a dotenv file. The key is never written to a manifest, log or result.

Any OpenAI-compatible endpoint works with `--base-url`. For a local server without a key, pass `--api-key-env NONE`.

## Run

```bash
alias te='evals/.venv/bin/python -m telarch_evals'
te fetch all                          # LongMemEval S, LoCoMo, BEAM 100K and the official code (about 300 MB)

te longmemeval --per-type 1           # 6 questions: a check of the whole pipeline
te longmemeval                        # all 500 questions
te locomo                             # all 10 conversations
te beam --size 100K                   # 20 conversations; also 500K, 1M, 10M

te longmemeval --system full-context                  # the long-context baseline
te longmemeval --split oracle --system full-context   # only the evidence sessions: an upper bound
te longmemeval --reader-model gpt-4o-2024-08-06       # a stronger reader
```

`make evals-smoke` runs a few questions of each benchmark. Every run takes:

- `--max-usd N`: stops starting new questions once the estimated spend reaches N.
- `--workers N`: parallel model calls, 4 by default.
- `--resume RUN_DIR`: continues a stopped run with the same settings. Failed questions are retried and finished ones are kept.

Model responses are cached under `$TELARCH_EVALS_HOME`, so a rerun with the same settings is not billed twice. That directory defaults to `~/.cache/telarch-evals`.

**Cost.** These figures come from the smoke runs with the default models and are estimated at list prices.

| Benchmark | Per question | Full run |
| --- | --- | --- |
| LongMemEval | $0.002 | about $1 for 500 questions |
| LoCoMo | $0.0006 | about $1 for 1,540 questions |
| BEAM | $0.002 | about $1 for the 400 questions at 100K |

A `full-context` LongMemEval run sends about 100,000 tokens per question, roughly $7 to $8 with `gpt-4o-mini`. A `gpt-4o` reader costs about 16 times as much as `gpt-4o-mini`. The price table is in `telarch_evals/llm.py`, and `--price MODEL=IN,OUT` adds or overrides an entry.

## Output

Each run writes `$TELARCH_EVALS_HOME/runs/<benchmark>/<run id>/`, which contains:

- `manifest.json`. The settings and models, the versions of Kibisis, Harpe and Argus, the commit of this repository, and the URL, license and hash of every input.
- `judgments.jsonl`. One row per question: the answer, the judge's verdict, the memory statistics and the evidence recall. Rows are appended and fsynced as questions finish.
- `summary.json`. The scores, token use and estimated spend.
- `predictions.jsonl`. LongMemEval only: hypotheses in the format the official `evaluate_qa.py` reads. The rows in `judgments.jsonl` also carry `question_id` and `autoeval_label`, so the official `print_qa_metrics.py` can score a run directly.

Starts, resumes and completions are appended to the Argus ledger `runs/ledger.jsonl`, with the SHA-256 of each run's files. `te verify` checks the ledger's hash chain and every completed run's files against it. `te summarize RUN_DIR` recomputes a summary from the rows.

## Reading the numbers

- A score can be compared with published results only when it uses the official judge and the full question set. The CLI warns when the judge is not the official one.
- The reader model moves the score a lot. Report it with every number.
- Subsets such as `--per-type 1` check the pipeline. They are not results.
- LoCoMo publications use different reader prompts. The J judge is the part that makes scores comparable.

## Licenses

This harness is released under the [MIT License](../LICENSE). The datasets and the upstream code it downloads keep their own licenses, which `te sources` lists:

| Source | License |
| --- | --- |
| LongMemEval | MIT |
| BEAM | MIT |
| mem0's judge prompt | Apache-2.0 |
| The LoCoMo data | CC BY-NC 4.0 |
