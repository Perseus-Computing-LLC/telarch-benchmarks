# Telarch on LongMemEval-V2 (text only)

`telarch_backend.py` registers a LongMemEval-V2 memory backend, `telarch`, and `run_lmev2.py` runs it, or the
official `no_retrieval` baseline, through the benchmark's own harness. No file of the benchmark is changed.

- **Pins:** repository commit `2cc8c54`; dataset `xiaowu0162/longmemeval-v2` at revision
  `f152293e235517d504809563c833d7190b8c713b` (Apache-2.0).
- **Text only:** each state's goal, outcome, URL, thought, action and accessibility-tree text are stored;
  trajectory screenshots are not downloaded or used. Question screenshots are passed to the reader as the
  harness does.
- **Retrieval:** the telarch-evals Telarch arm (`TelarchMemory`) with the settings frozen in
  `$TELARCH_EVALS_HOME/reports/round2-config.json`. Passages are ingested while the harness inserts trajectories,
  so the harness's query time covers search and packing only.
- **Not a leaderboard entry:** leaderboard runs need the Qwen3.5-9B reader. These runs use gpt-4o-mini, so they
  drop `top_k` (OpenAI rejects it) and the Qwen thinking switch, and cap answers at 4,096 tokens (the official
  20,000 is a thinking budget). The judge is the official one: gpt-5.2 with medium reasoning.

## Set up

```bash
git clone https://github.com/xiaowu0162/LongMemEval-V2.git && git -C LongMemEval-V2 checkout 2cc8c54
uv venv --python 3.11 venv311 && . venv311/bin/activate
# the benchmark's unpinned requirements, resolved as of its commit date
uv pip install --exclude-newer 2026-08-09T15:31:22-07:00 -r LongMemEval-V2/requirements.txt -e LongMemEval-V2
uv pip install tiktoken harpe==0.1.0a4 argus-ledger==0.1.0a4 kibisis==0.1.0a7
uv pip install --no-deps -e <telarch-benchmarks>/evals
# the harness counts memory-context tokens with the Qwen3.5-9B processor, which needs torch and torchvision;
# the CPU builds of the versions in requirements-torch.txt are enough (no model weights are loaded)
uv pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0+cpu torchvision==0.21.0+cpu
```

Download the text files only, then run the official validation without its screenshot check:

```python
from huggingface_hub import snapshot_download
snapshot_download(repo_id="xiaowu0162/longmemeval-v2", repo_type="dataset",
                  revision="f152293e235517d504809563c833d7190b8c713b", local_dir="data/longmemeval-v2",
                  allow_patterns=["questions.jsonl", "trajectories.jsonl", "haystacks/*", "question_screenshots/*",
                                  "checksums.sha256", "LICENSE", "DATA_CARD.md", "SCHEMA.md"])
from data.public_data import validate_public_data          # from the LongMemEval-V2 checkout
validate_public_data(Path("data/longmemeval-v2"), tier="small", check_screenshots=False)
```

## Run

From `telarch-benchmarks/evals`:

```bash
PYTHONPATH=/path/to/LongMemEval-V2:. venv311/bin/python -m lme_v2.run_lmev2 --method telarch \
    --domain web --tier small --data-root data/longmemeval-v2 --env-file /path/to/.env
```

Each run writes the harness's outputs (including `aggregated_metrics.json`) under
`$TELARCH_EVALS_HOME/lme-v2/runs/`, and appends an `lmev2.run.completed` event with the SHA-256 of every output
JSON file to the Argus ledger `$TELARCH_EVALS_HOME/lme-v2/ledger.jsonl`.

### A reader behind a proxy

Some hosts put the reader behind an HTTPS proxy that drops a request when nothing comes back for 100 seconds
(RunPod's returns HTTP 524). A long answer is then cut off, and the harness's client retries with a new sample,
which favors short answers. `lmev2_relay.py` runs between the harness and the reader:

- It forwards each request with streaming on, so tokens arrive before the proxy's limit, and returns the whole
  answer as one response. Messages and sampling parameters pass through unchanged.
- It gives each question its own seed, derived from its messages, so a retry repeats the same sample.
- It retries proxy errors and dropped streams for about two minutes, and logs every retry to `$RELAY_LOG`
  (default `lmev2-relay.log`).

```bash
venv311/bin/python -m lme_v2.lmev2_relay https://<reader-host>/v1 18000 24   # upstream, port, requests in flight
```

Then add `--reader-base-url http://127.0.0.1:18000/v1` to the `run_lmev2` command. The tests are in
`tests/test_lmev2_relay.py`.
