"""severity: medium
finding: gap1/gap2/gap3 measure a sibling checkout outside the repository at whatever version it is, and publish under the vendored version

Location: scripts/gap1_harpe_quality.py:27-31, scripts/gap2_harpe_alternatives.py:26-29,
scripts/gap3_kibisis_multiproc_crashes.py:29-33: `sys.path.insert(0, os.path.join(ROOT, "..", "<lib>", "src"))`.
ROOT is the repository, so the entry is the repository's *parent*: `../harpe/src`, `../kibisis/src`. The Python
packages vendored at ROOT/harpe/src/harpe and ROOT/kibisis/src/kibisis are never on the path. Without a sibling
the scripts use whatever `harpe` / `kibisis` is installed (.github/workflows/ci.yml:32-43 installs the matching
releases for that case, so the sibling import is intended). What is missing is any check: no script compares the
imported package's version with the vendored one, no report records the version or path of what was imported,
scripts/build_claims.py:108 stamps the claims with the vendored Cargo versions, and scripts/vendor_check.py
compares only those.

Effect: harpe_quality_recall_pct, kib_multiproc_split_brain, kib_stale_writes_rejected and the gap2 timings are
published as figures of harpe 0.1.0-alpha.12 / kibisis 0.1.0-alpha.15 while the code measured is a working copy
next to the clone at whatever commit it happens to be. In this review workspace the scripts import
`../kibisis/src/kibisis`, which is 0.1.0a16; the vendored copy is 0.1.0a15. Whether the published 2026-11-05 runs
were measured that way cannot be told from the reports, which is the defect. `make bisect-bench` has the same
hole: both arms import the same package (bisect_bench.py:81-83 ignores the worktree), so these figures cannot
differ between refs.

The test builds <tmp>/ws/repo (the real script, bench_env.py and a marker package where the vendored one lives)
and <tmp>/ws/<lib> (a marker sibling at another version) and imports the real script. Correct is either to use
the vendored package or to refuse to start. A fix that keeps the sibling but records its version in the report
and has build_claims compare it would need this test's assertion widened; no such record exists on this head.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
HARPE = ("harpe", {"engine": ["ContextBudgetEngine"], "models": ["ContextItem", "TokenBudget", "PrunePolicy"]})
KIBISIS = ("kibisis", {"lease": ["FileLeaseCoordinator"], "protocols": ["LeaseConflictError"]})
CASES = {"gap1_harpe_quality": HARPE, "gap2_harpe_alternatives": HARPE, "gap3_kibisis_multiproc_crashes": KIBISIS}
VENDORED, SIBLING = "0.1.0a15", "0.1.0a16"
PROBE = (
    "import importlib, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "script = importlib.import_module(sys.argv[2])\n"
    "lib = importlib.import_module(sys.argv[3])\n"
    "print(json.dumps({'script': script.__file__, 'root': script.ROOT, 'lib': lib.__file__, 'version': lib.__version__}))\n"
)


def marker_package(src_dir: Path, lib: str, modules: dict, version: str):
    pkg = src_dir / lib
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    for module, names in modules.items():
        body = "".join(f"class {n}(Exception):\n    pass\n\n" for n in names)
        (pkg / f"{module}.py").write_text(body, encoding="utf-8")


def import_script(tmp_path: Path, script: str, sibling_version):
    """Returns (repo, result) where result is None if the script refused to import."""
    lib, modules = CASES[script]
    repo = tmp_path / "ws" / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in (f"{script}.py", "bench_env.py"):
        shutil.copy2(SCRIPTS / name, repo / "scripts" / name)
    marker_package(repo / lib / "src", lib, modules, VENDORED)
    (repo / lib / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0-alpha.15"\n', encoding="utf-8")
    if sibling_version:
        marker_package(tmp_path / "ws" / lib / "src", lib, modules, sibling_version)
    out = subprocess.run([sys.executable, "-B", "-c", PROBE, str(repo / "scripts"), script, lib],
                         capture_output=True, text=True, cwd=tmp_path, timeout=120)
    if out.returncode != 0:
        return repo.resolve(), None, out.stderr[-1500:]
    return repo.resolve(), json.loads(out.stdout.strip().splitlines()[-1]), ""


@pytest.mark.parametrize("script", sorted(CASES))
def test_control_a_sibling_at_the_vendored_version_is_imported_and_the_script_knows_its_repository(tmp_path, script):
    repo, got, err = import_script(tmp_path, script, sibling_version=VENDORED)
    assert got is not None, err
    assert Path(got["script"]).resolve().parent == repo / "scripts"
    assert Path(got["root"]).resolve() == repo
    assert got["version"] == VENDORED


@pytest.mark.parametrize("script", sorted(CASES))
def test_a_sibling_at_another_version_is_not_measured_as_the_vendored_one(tmp_path, script):
    repo, got, err = import_script(tmp_path, script, sibling_version=SIBLING)
    if got is None:
        return                                                    # refusing to start is correct
    lib_path = Path(got["lib"]).resolve()                         # the script's own path entry contains ".."
    assert got["version"] == VENDORED and lib_path.is_relative_to(repo), (
        f"{script}.py will measure {CASES[script][0]} {got['version']} from {lib_path}; the repository {repo} vendors "
        f"{VENDORED}, and that is the version the claims will carry")
