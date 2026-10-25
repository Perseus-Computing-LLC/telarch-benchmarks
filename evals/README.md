# Memory evaluations

`telarch-evals` runs four public long-term memory benchmarks against the Telarch libraries. [Kibisis](https://github.com/Perseus-Computing-LLC/kibisis) stores each conversation history and recalls candidates for a question. [Harpe](https://github.com/Perseus-Computing-LLC/harpe) picks the candidates that fit the token budget. [Argus](https://github.com/Perseus-Computing-LLC/argus) records every run in a hash-chained ledger.

Each benchmark keeps its own scoring. The official prompts and scoring code are downloaded from pinned upstream commits and checked against recorded SHA-256 hashes before use. None of that code or data is copied into this repository.

No memory-quality figure from these evaluations is published on telarch.ai yet.

## The benchmarks

| Benchmark | Data | Reader prompt | Judge (default) | Scores |
| --- | --- | --- | --- | --- |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | 500 questions. Each has its own chat history, about 115,000 tokens in the S split. The M split and the oracle split are also available. | The official template, chain of thought | `gpt-4o-2024-08-06` with the official prompt | Overall and task-averaged accuracy, accuracy per question type, abstention accuracy |
| [LoCoMo](https://github.com/snap-research/locomo) | 10 conversations between two people. 1,540 questions in categories 1 to 4, or 1,986 with the adversarial category 5. | This harness's own (see `bench_locomo.py`) | `gpt-4o-mini` with the J prompt from mem0's evaluation code | J (LLM judge), token F1, both per category |
| [BEAM](https://github.com/mohammadtavakoli78/BEAM) | 100 conversations at 100K, 500K, 1M and 10M tokens. 20 questions each across 10 memory abilities. | BEAM's `answer_generation_for_rag` | `gpt-4.1-mini` with BEAM's evaluator for each ability | Score per ability, overall mean |
| [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench) | The 14 datasets of the paper's Table 3: 2,071 questions over contexts of 103K to 1.44M tokens, fed in 4,096-token chunks. | The benchmark's templates | SubEM, EventQA recall and Recall@5; `gpt-4o` for LongMemEval (S*); `gpt-4o-2024-05-13` for ∞Bench-Sum | Score per dataset, four competency scores, overall mean |

LongMemEval, LoCoMo and BEAM also report **evidence recall**: the share of the gold evidence that made it into the context the reader saw. It separates memory misses from reader misses.

MemoryAgentBench follows the paper's Table 3. Its four competencies are accurate retrieval, test-time learning, long-range understanding and selective forgetting. A competency score is the mean of its columns, and the overall score is the mean of the four competencies. The reader runs at the agents' temperature, 0.7.

## What answers the questions

`--system` picks what supplies the context:

- `telarch` (default). Every round of the history goes into a Kibisis vault, one vault per history. The question is reduced to its content words. Each word is searched in Kibisis and the per-word scores are summed. Harpe then takes the top 200 candidates and fills an 8,000-token budget, most relevant first (`least_relevant`).
  - Encrypted (the default): each vault gets a random passphrase that never leaves the process. From Kibisis 0.1.0a7 (pinned here), search ranks encrypted entries by BM25 from an index inside the SQLCipher file, with the same scores a plaintext vault gives. Kibisis 0.1.0a5 and 0.1.0a6 kept no plaintext in their full-text index when content was encrypted, so search decrypted every entry and matched substrings in storage order, every hit with the same score; runs made with them record that in `kibisis_search`.
  - `--plaintext`: the vault is unencrypted and search uses SQLite FTS5, so the summed scores are BM25 over an OR of the query words. The operator's own `KIBISIS_PASSPHRASE` and `KIBISIS_PASSPHRASE_FILE` are removed from the benchmark process, since Kibisis would otherwise use them to encrypt the vault.
  - The `least_relevant` policy was chosen on evidence recall, a retrieval-only measure with no model calls, on 60 LongMemEval-S questions (10 per type) with encrypted vaults. Evidence recall was 0.889 per session against 0.828 for `knapsack_dp`. Harpe's knapsack policy gives every item a fixed share of utility, so it can prefer several small, weakly related rounds over one large relevant round.
  - Harpe treats items of priority 0 as pinned, kept regardless of the budget, so the adapter uses priorities 1 to 100.
  - The budget covers the context the reader gets: the selected units as the benchmark renders them, separators and headers included. When that rendering runs over, Harpe packs again with the overhead set aside, and the least relevant unit is dropped while it is still over. Each row records the rendered size as `joined_tokens_o200k`.
- `full-context`. The history itself, newest first, until the rendered history fills the reader's window: 128,000 tokens less the answer limit and 4,000 for the prompt around the history, counted with the reader's tokenizer (`o200k_base`). This is the long-context baseline.
- `none`. No history. This is the floor.

MemoryAgentBench differs in two places:
- `telarch` splits each 4,096-token chunk into passages of up to 512 tokens before storing them. The reader sees the selected passages labelled `Memory i:`, followed by the benchmark's RAG query.
- `full-context` is the benchmark's own long-context agent. Every chunk is wrapped in its memorize template, the newest 128,000 − 4,000 − generation-limit tokens are kept, and the long-context query follows.

Round-2 options for `telarch` (all off by default except the counter):
- `--counter o200k` (default) counts Harpe's budget in the reader's tokens; `heuristic` is Harpe's own counter, which counted 12-17% more than o200k on the round-2 screens (LongMemEval-S x1.17, BEAM 100K x1.12, BEAM 1M x1.13), so an 8,000 budget held about 6,700-6,900 reader tokens.
- `--planner any` runs one Kibisis any-word BM25 search (`match="any"`) instead of one search per content word (`fanout`, the default).
- `--fill-floor F` widens retrieval while Harpe fills less than F of the budget: first the per-word limit, then the candidate cut, up to four doublings, and only while a wider search can find more.
- `--passthrough-under N` hands a history of at most N reader tokens over whole, in exactly the full-context arm's form, so the response cache serves repeats. Report runs that use it as a routed arm, with their cost.
- `--recency R` multiplies each score by 1 + R x the unit's position in the history (0 oldest, 1 newest). `--mmr W` subtracts W x the highest word overlap with an entry already taken. `--bridge N` searches again with names found in the top N hits (2-hop), counting those hits half.
- `--window T` packs single messages instead of whole turns, cutting any message over T reader tokens to its best question-scored T-token window, and caps Harpe at 7,900 tokens so the joined prompt stays under 8,000. Kept as a tested negative: on the full BEAM 1M set, `--window 128` found more of the evidence messages but scored lower than whole-turn packing, so whole turns stay the default.

`--retrieval-only` runs memory and evidence recall with no model calls ($0): rows record what was selected, its size in both counters, and the reader prompt's size. Screens go to `$TELARCH_EVALS_HOME/screens`, never to `runs/`.

Settings that change the result are recorded in each run's manifest: `--budget`, `--candidates`, `--per-term`, `--policy`, `--admission-gate`, `--plaintext` and the round-2 options above. The manifest's `package_sources` names each library's version and origin (index release, git commit, or editable checkout).

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
te memoryagentbench                   # the 14 datasets; --datasets and --per-dataset select fewer

te longmemeval --system full-context                  # the long-context baseline
te longmemeval --split oracle --system full-context   # only the evidence sessions: an upper bound
te longmemeval --reader-model gpt-4o-2024-08-06       # a stronger reader
```

`make evals-smoke` runs a few questions of each benchmark. Every run takes:

- `--max-usd N`: stops starting new questions once the run's estimated spend reaches N. A resumed run counts what its earlier processes spent.
- `--workers N`: parallel model calls, 4 by default.
- `--resume RUN_DIR`: continues a stopped run with the same settings. Failed questions are retried and finished ones are kept.

Model responses are cached under `$TELARCH_EVALS_HOME`, so a rerun with the same settings is not billed twice. That directory defaults to `~/.cache/telarch-evals`. A stored response is never replaced, so a rerun from the cache gets the answers the run itself used. One run holds the cache at a time: a second run on the same directory stops with an error, so give runs that go in parallel their own `TELARCH_EVALS_HOME`, or pass `--no-cache`.

**Cost.** These figures come from the smoke runs with the default models and are estimated at list prices.

| Benchmark | Per question | Full run |
| --- | --- | --- |
| LongMemEval | $0.002 | about $1 for 500 questions |
| LoCoMo | $0.0006 | about $1 for 1,540 questions |
| BEAM | $0.002 | about $1 for the 400 questions at 100K |
| MemoryAgentBench | $0.0015, plus about $0.05 for each ∞Bench-Sum summary the judge reads | about $9 for 2,071 questions |

A `full-context` LongMemEval run sends about 105,000 tokens per question (every LongMemEval-S history fits whole), roughly $8 to $9 with `gpt-4o-mini`. A `full-context` MemoryAgentBench run sends up to 124,000 tokens per question, about $50 at list prices before the provider's prompt-caching discount. A `gpt-4o` reader costs about 16 times as much as `gpt-4o-mini`. The price table is in `telarch_evals/llm.py`, and `--price MODEL=IN,OUT` adds or overrides an entry.

## Output

Each run writes `$TELARCH_EVALS_HOME/runs/<benchmark>/<run id>/`, which contains:

- `manifest.json`. The settings and models, the versions of Kibisis, Harpe and Argus, the commit of this repository, and the URL, license and hash of every input.
- `judgments.jsonl`. One row per question: the answer, the judge's verdict, the memory statistics and the evidence recall. Rows are appended and fsynced as questions finish.
- `summary.json`. The scores, token use and estimated spend.
- `spend.json`. Tokens and estimated spend per model across every process of the run, with the list prices used and the date of the price table. It is rewritten after each model call.
- `predictions.jsonl`. LongMemEval only: hypotheses in the format the official `evaluate_qa.py` reads. The rows in `judgments.jsonl` also carry `question_id` and `autoeval_label`, so the official `print_qa_metrics.py` can score a run directly.

Starts, resumes and completions are appended to the Argus ledger `runs/ledger.jsonl`, with the SHA-256 of each run's files. `te verify` checks the ledger's hash chain and each completed run's files against its latest completion; a completion followed by a resume and a later completion of the same run is listed as superseded, because resuming rewrites the run's files. `te summarize RUN_DIR` recomputes a summary from the rows.

## Reading the numbers

- A score can be compared with published results only when it uses the official judge and the full question set. The CLI warns when the judge is not the official one.
- The reader model moves the score a lot. Report it with every number.
- Subsets such as `--per-type 1` check the pipeline. They are not results.
- LoCoMo publications use different reader prompts. The J judge is the part that makes scores comparable.
- MemoryAgentBench samples the reader at temperature 0.7, as the paper does, so repeated runs differ by a point or two.

## Licenses

This harness is released under the [MIT License](../LICENSE). The datasets and the upstream code it downloads keep their own licenses, which `te sources` lists:

| Source | License |
| --- | --- |
| LongMemEval | MIT |
| BEAM | MIT |
| mem0's judge prompt | Apache-2.0 |
| The LoCoMo data | CC BY-NC 4.0 |
| MemoryAgentBench | MIT |
| NLTK's punkt_tab tokenizer data | Apache-2.0 |
