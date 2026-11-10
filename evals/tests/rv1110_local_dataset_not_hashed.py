"""severity: medium
finding: a run on a local dataset (--data) records no hash of it; the manifest names only the file's name, and a resume accepts a different file

Location: evals/telarch_evals/bench_longmemeval.py:255-257 (`"dataset": str(path.name)` in the config and
`sources = [] if data_path else [record(...)]`), the same in bench_locomo.py:95 and bench_memoryagentbench.py:245
(--data / --data-dir); evals/telarch_evals/runlog.py:72-81 (a resume compares the config only).

Effect: evals/README.md:154 says the manifest holds "the URL, license and hash of every input". For the pinned
download that is true. For `--data FILE` the questions and gold answers that were scored are identified by a
base name alone: a run on an edited or subsampled copy named like the official file has the same manifest as a run
on the official one, the Argus ledger pins the manifest but not the data, and `--resume` continues a run over a
file whose contents changed in between, mixing rows scored against two datasets into one accuracy.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import telarch_evals
from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import NoMemory      # needs no tokenizer; what is retrieved does not matter here

EVALS = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, model, reply):
        self.model, self.reply, self.calls = model, reply, []

    def describe(self):
        return {"model": self.model}

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        return SimpleNamespace(text=self.reply, model=self.model, usage={"prompt_tokens": 3, "completion_tokens": 1}, cached=False)


class FakeLongMemEval:
    reader_template_cot = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
    reader_template_plain = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer:"

    def anscheck_prompt(self, task, question, answer, response, abstention=False):
        return f"gold={answer} response={response} abstention={abstention}"

    def judge_model(self, name):
        return name


def write_data(path: Path, gold: str, ids=("q1", "q2")) -> Path:
    session = [{"role": "user", "content": "I adopted a beagle named Biscuit.", "has_answer": True},
               {"role": "assistant", "content": "Congratulations!"}]
    base = {"question_date": "2023/06/01 (Thu) 10:00", "haystack_session_ids": ["s1"],
            "haystack_dates": ["2023/05/01 (Mon) 09:00"], "haystack_sessions": [session],
            "question_type": "single-session-user", "question": "What breed is Biscuit?", "answer_session_ids": ["s1"]}
    path.write_text(json.dumps([{**base, "question_id": i, "answer": gold} for i in ids]), encoding="utf-8")
    return path


def run(tmp_path, data, resume=None):
    ctx = Context(runs_root=tmp_path / "runs", workdir=tmp_path / "work", system=NoMemory(),
                  reader=FakeClient("reader", "It is a beagle."), judge=FakeClient("gpt-4o-2024-08-06", "yes"),
                  meter=Meter(), workers=1, resume=resume)
    return bench_longmemeval.run(ctx, data_path=data, official=FakeLongMemEval())


def test_control_a_local_data_run_completes_and_names_the_file(tmp_path):
    assert Path(telarch_evals.__file__).resolve().is_relative_to(EVALS)
    out = run(tmp_path, write_data(tmp_path / "longmemeval_s_cleaned.json", "beagle"))
    summary = json.loads((out / "summary.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    assert summary["complete"] and summary["rows"] == 2 and summary["overall_accuracy"] == 1.0
    assert manifest["config"]["dataset"] == "longmemeval_s_cleaned.json"
    assert all("sha256" in s or "git_blob_sha1" in s for s in manifest["sources"])     # the pinned code files are hashed


def test_the_manifest_holds_the_hash_of_the_dataset_that_was_scored(tmp_path):
    data = write_data(tmp_path / "longmemeval_s_cleaned.json", "beagle")
    out = run(tmp_path, data)
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    assert digest in (out / "manifest.json").read_text(), (
        "manifest.json identifies the scored dataset as 'longmemeval_s_cleaned.json' and carries no hash of it")


def test_a_resume_over_a_dataset_whose_contents_changed_is_refused(tmp_path):
    data = write_data(tmp_path / "lme.json", "beagle", ids=("q1",))
    out = run(tmp_path, data)
    write_data(data, "a poodle; the user never had a beagle", ids=("q1", "q2"))       # same name, other gold answers
    with pytest.raises(SystemExit):
        run(tmp_path, data, resume=out)
