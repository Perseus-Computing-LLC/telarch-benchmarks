"""severity: medium
finding: claims.json names the commit build_claims.py was run at; the commit= each run recorded is never read

Location: scripts/build_claims.py:106-110 and :683 (benchmark_commit, and every claim's `commit`, come from
bench_env.commit() of the checkout at the moment build_claims.py runs), :790-796 (aggregate opens each run's
run_env.txt and reads only `pin=`). scripts/run_once.sh:47 writes `commit=` per run; :52-58 run whatever
binaries are in target/release without building them.

Effect: "Medians of 3 sequential runs ... at commit `X`" in README.md is the HEAD of whoever regenerated
claims.json, whatever the runs were built from. Runs from different commits are medianed together without a word.
On this head the committed data already disagrees: reports/runs/2026-11-05/run2 and run3 recorded
`commit=851f928-dirty`, claims.json says `851f928`. (For the single 1M-event run build_claims does take the
commit from run_env.txt, lines 397-400, so the intent is there.)
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bc():
    mod = _load("build_claims")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    return mod


def run_commits(run_dirs):
    out = []
    for d in run_dirs:
        lines = (Path(d) / "run_env.txt").read_text(encoding="utf-8").splitlines()
        out.append(next(line[len("commit="):].strip() for line in lines if line.startswith("commit=")))
    return out


def aggregated(bc, tmp_path, commits):
    dirs = []
    for i, commit in enumerate(commits, 1):
        d = tmp_path / f"run{i}"
        d.mkdir()
        (d / "run_env.txt").write_text(f"pin=nice -n 15 taskset -c 0-9\ncommit={commit}\n", encoding="utf-8")
        dirs.append(str(d))
    claim = {"value": 9.9, "run_count": 1, "median": None, "range": None, "note": "", "hardware": "h", "commit": "ccccccc"}
    out = {"meta": {"benchmark_commit": "ccccccc", "hardware": "h", "note": ""}, "claims": {"harpe_small_p50_us": claim}}
    bc.aggregate(out, [{"harpe_small_p50_us": {"value": v}} for v in (1.0, 2.0, 3.0)], dirs)
    return out


def test_control_aggregate_reads_each_runs_run_env(bc, tmp_path):
    out = aggregated(bc, tmp_path, ["ccccccc"] * 3)
    assert "taskset -c 0-9" in out["meta"]["hardware"]            # pin= was taken from the run_env.txt files
    assert out["claims"]["harpe_small_p50_us"]["value"] == 2.0
    assert out["meta"]["benchmark_commit"] == "ccccccc"


def test_runs_built_from_other_commits_are_not_published_under_the_builders_commit(bc, tmp_path):
    try:
        out = aggregated(bc, tmp_path, ["aaaaaaa", "bbbbbbb-dirty", "aaaaaaa"])
    except (AssertionError, ValueError, SystemExit):
        return                                                    # refusing to build is correct
    text = json.dumps(out)
    assert "aaaaaaa" in text and "bbbbbbb-dirty" in text, (
        "three runs built from aaaaaaa / bbbbbbb-dirty are published as commit "
        f"{out['meta']['benchmark_commit']!r} (claim commit {out['claims']['harpe_small_p50_us']['commit']!r}); "
        "the commits the runs recorded appear nowhere in the output")


def test_the_committed_runs_were_built_from_the_commit_claims_json_names(bc):
    """What the defect already let through in the committed data."""
    claims = json.loads((REPO / "claims.json").read_text(encoding="utf-8"))
    runs = [REPO / r for r in claims["meta"]["runs"]]
    assert [str(r) for r in runs] == bc.discover_runs()
    assert set(run_commits(runs)) == {claims["meta"]["benchmark_commit"]}
