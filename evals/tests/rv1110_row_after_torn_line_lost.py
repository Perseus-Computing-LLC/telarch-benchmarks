"""severity: low
finding: after a torn last line, the first row a resumed run writes is glued to the fragment and silently lost

Location: evals/telarch_evals/runlog.py:113-125 (rows() skips any line that does not parse, "a torn final line
from an interrupted write"), :106-111 (_drop_failed_rows rewrites judgments.jsonl only when a parsed row has
failed, so the unterminated fragment stays at the end of the file), :104 and :127-131 (the file is reopened for
append and the next row is written straight after the fragment, on the same line).

Effect: a run killed mid-write and resumed loses the first answer it finishes afterwards, a paid reader and judge
call: the row is on disk but joined to the fragment, so rows() drops it. The summary then has one row fewer than
expected with `errors: 0`, and the row's question is answered again by a second resume. Nothing is reported; the
score of that process is computed without the row.
"""
from pathlib import Path

import telarch_evals
from telarch_evals.runlog import RunLog

EVALS = Path(__file__).resolve().parents[1]
CONFIG = {"system": "telarch", "benchmark": "longmemeval"}


def interrupted_run(tmp_path, tail: str) -> Path:
    log = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[])
    log.write({"id": "q1", "status": "ok"})
    log._fh.close()                                               # the process dies here, without finish()
    with open(log.dir / "judgments.jsonl", "a", encoding="utf-8") as fh:
        fh.write(tail)
    return log.dir


def resume_and_write_q3(tmp_path, run_dir) -> list:
    resumed = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[], resume=run_dir)
    assert resumed.done == {"q1"}
    resumed.write({"id": "q3", "status": "ok", "answer": "paid for"})
    resumed._fh.close()
    return [row["id"] for row in resumed.rows()]


def test_control_a_cleanly_ended_file_keeps_the_row_written_after_a_resume(tmp_path):
    assert Path(telarch_evals.__file__).resolve().is_relative_to(EVALS)
    run_dir = interrupted_run(tmp_path, tail="")
    assert resume_and_write_q3(tmp_path, run_dir) == ["q1", "q3"]


def test_the_row_written_after_a_torn_line_is_not_lost(tmp_path):
    run_dir = interrupted_run(tmp_path, tail='{"id": "q2", "status": "ok", "answ')          # torn: no newline
    ids = resume_and_write_q3(tmp_path, run_dir)
    last_line = (run_dir / "judgments.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert "q3" in ids, f"rows() returns {ids}; the row is on disk inside the unparseable line {last_line!r}"
