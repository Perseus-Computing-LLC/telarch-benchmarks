"""severity: medium
finding: --max-usd never stops a run whose model has no price; the spend it is compared with stays $0

Location: evals/telarch_evals/llm.py:167-176 (Meter._usd adds nothing for a model that has neither a
gateway-reported cost nor a row in the price table), evals/telarch_evals/runlog.py:202-204 (execute stops only when
`meter.usd() >= max_usd`).

Effect: the price table knows 14 OpenAI ids. With any other reader or judge on a gateway that does not send
`usage.cost`, `--max-usd 5` is accepted, is recorded as the run's cap, and can never trigger: the run goes on to
its last question at an "estimated spend" of $0.00. The summary lists the model under `unpriced_models`, after
the money is spent. A cap that cannot be evaluated should stop the run (or refuse to start), not pass.
"""
from pathlib import Path

import telarch_evals
from telarch_evals.llm import Meter
from telarch_evals.runlog import RunLog, execute

EVALS = Path(__file__).resolve().parents[1]
CONFIG = {"system": "telarch", "benchmark": "longmemeval"}


def run_ten_questions(tmp_path, model: str, max_usd: float):
    meter = Meter({"priced/model": (1.0, 0.0)})                   # $1 per million prompt tokens
    log = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[])
    done = []

    def work(job_id, _):
        meter.add(model, 400_000, 0, cached=False)                # a billed request of 400,000 prompt tokens
        done.append(job_id)
        return {"id": job_id}

    stop = execute(log, ((f"q{i}", None) for i in range(10)), work, workers=1, meter=meter, max_usd=max_usd)
    log.finish({"rows": len(done), "stopped": stop})
    return stop, done, meter.snapshot()


def test_control_a_priced_model_is_stopped_at_the_cap(tmp_path):
    assert Path(telarch_evals.__file__).resolve().is_relative_to(EVALS)
    stop, done, spend = run_ten_questions(tmp_path, "priced/model", max_usd=1.0)
    assert stop and "--max-usd 1.0" in stop
    assert len(done) < 10 and spend["unpriced_models"] == []


def test_an_unpriced_model_does_not_run_through_the_cap(tmp_path):
    stop, done, spend = run_ten_questions(tmp_path, "vendor/unknown", max_usd=1.0)
    assert spend["unpriced_models"] == ["vendor/unknown"] and spend["estimated_usd"] == 0.0
    assert stop is not None and len(done) < 10, (
        f"--max-usd 1.0 with an unpriced model: {len(done)} of 10 questions billed "
        f"({spend['by_model']['vendor/unknown']['prompt_tokens']:,} prompt tokens), stop reason {stop!r}")
