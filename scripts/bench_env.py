"""Run details shared by the report scripts: commit, toolchain, crate versions, hardware and time.

Read from the checkout and the machine when a script runs, so a regenerated report never carries
values typed in for an earlier run.
"""

import os
import platform
import re
import socket
import subprocess
import time
import tomllib
from decimal import ROUND_HALF_EVEN, Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED = ("harpe", "kibisis", "argus-ledger")
# Files that `make bench` rewrites. Changes there do not make the measuring code differ from the commit.
OUTPUTS = ("reports/", "claims.json", "sbom/")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
LOADAVG = "/proc/loadavg"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def loadavg(source=None) -> dict:
    """One /proc/loadavg reading: the 1/5/15-minute load averages x100 as ints and the running/total scheduling
    entities, with the UTC second it was read. Never raises: an unreadable or garbled source is recorded as
    {"at", "error"}, so a run on a host without /proc/loadavg still completes."""
    at = utc_now()
    try:
        with open(source if source is not None else LOADAVG) as f:
            fields = f.read().split()
        running, total = fields[3].split("/", 1)
        x100 = [int((Decimal(v) * 100).to_integral_value(rounding=ROUND_HALF_EVEN)) for v in fields[:3]]
        return {"at": at, "load1_x100": x100[0], "load5_x100": x100[1], "load15_x100": x100[2],
                "running": int(running), "total": int(total)}
    except (OSError, IndexError, ValueError, ArithmeticError) as e:
        return {"at": at, "error": f"{type(e).__name__}: {e}"}


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def commit(full: bool = False) -> str:
    """SHA of HEAD, with "-dirty" when tracked files other than the outputs differ from it."""
    sha = git("rev-parse", *([] if full else ["--short=7"]), "HEAD").strip()
    if not sha:
        return "unknown"
    changed = [line[3:] for line in git("status", "--porcelain", "--untracked-files=no").splitlines() if line]
    return sha + ("-dirty" if any(not p.startswith(OUTPUTS) for p in changed) else "")


def has_hostname(text: str) -> bool:
    """True when text contains this machine's hostname as a whole word."""
    host = socket.gethostname()
    return len(host) >= 3 and re.search(rf"(?<![\w-]){re.escape(host)}(?![\w-])", text) is not None


def toolchain() -> str:
    """The Rust toolchain pinned in rust-toolchain.toml, which rustup uses for every cargo command here."""
    try:
        with open(os.path.join(ROOT, "rust-toolchain.toml"), "rb") as f:
            return f"rustc {tomllib.load(f)['toolchain']['channel']}"
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def crate_versions() -> dict:
    """Package versions of the vendored library crates."""
    out = {}
    for d in VENDORED:
        with open(os.path.join(ROOT, d, "Cargo.toml"), "rb") as f:
            out[d] = tomllib.load(f)["package"]["version"]
    return out


def pep440(cargo_version: str) -> str:
    """A Cargo.toml version as the Python package of the same release writes it: 0.1.0-alpha.15 -> 0.1.0a15."""
    return re.sub(r"-(alpha|beta|rc)\.?(\d+)$", lambda m: {"alpha": "a", "beta": "b", "rc": "rc"}[m.group(1)] + m.group(2),
                  cargo_version)


def require_vendored(module) -> dict:
    """Stop unless the imported library is the version vendored here; return what a report records about it.

    The claims built from a gap report carry the vendored version (<lib>/Cargo.toml), so a script that measured
    another version of the package must not write a report at all.
    """
    lib = module.__name__
    with open(os.path.join(ROOT, lib, "Cargo.toml"), "rb") as f:
        vendored = tomllib.load(f)["package"]["version"]
    version = getattr(module, "__version__", None)
    path = os.path.dirname(os.path.realpath(module.__file__))
    if version != pep440(vendored):
        raise SystemExit(f"{lib} {version} imported from {path} is not the vendored {lib} {vendored} "
                         f"({pep440(vendored)}); its figures would be published under the vendored version")
    root = os.path.realpath(ROOT)
    inside = os.path.commonpath([path, root]) == root
    return {"package": lib, "version": version, "vendored_version": vendored,
            "path": os.path.relpath(path, root) if inside else "outside the repository"}


def hardware() -> str:
    model = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            model = next((line.split(":", 1)[1] for line in f if line.startswith("model name")), "")
    except OSError:
        pass
    model = " ".join(re.sub(r"\((R|TM)\)", "", model).split()) or platform.processor() or "unknown CPU"
    return f"{platform.system()} {platform.machine()}, {model}, {os.cpu_count()} logical CPUs"


# Hybrid Intel CPUs: which logical CPUs are performance-core threads, as /sys/devices/cpu_core/cpus reads on a
# machine with that CPU model. claims.json is rebuilt on machines that did not measure (CI runs
# `make claims-check`), so the range for a run comes from the run's record (run_once.sh writes
# performance_cpus=) or, for runs made before that line was recorded, from this table by the CPU model in the
# reports' host line. It is never read from the machine that happens to build claims.json.
PERFORMANCE_CPUS_BY_MODEL = {
    "13th Gen Intel Core i5-13500": "0-11",  # read on the measuring host; 6 performance cores, 2 threads each
}


def local_performance_cpus() -> str:
    """This machine's performance-core CPU list, "" when it is not a hybrid CPU. For the run that is being
    made on this machine only; see PERFORMANCE_CPUS_BY_MODEL."""
    try:
        with open("/sys/devices/cpu_core/cpus", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def performance_cpus(host: str, recorded: str = "") -> str:
    """The performance-core CPU list of the machine a run was made on: what the run recorded, else what the
    table gives for the CPU model in `host`, else "" (not known, so nothing is said about it)."""
    if recorded:
        return recorded
    return next((cpus for model, cpus in PERFORMANCE_CPUS_BY_MODEL.items() if f", {model}, " in f", {host}, "), "")


def pinning(pin: str, performance: str = "") -> str:
    """How the run placed the programs and scripts; `pin` is the command prefix run_once.sh or the
    Makefile put before each program: a taskset, optionally preceded by nice and ionice. `performance` is
    the performance-core CPU list of the machine that ran it (performance_cpus()), "" when not known."""
    parts = pin.split()
    if not parts:
        return "programs and scripts not pinned to CPUs"
    # Name the taskset first and the priorities after it: `taskset -c 0-9` is what a reader compares
    # across runs, and the nice and ionice levels say why a run under them is not a ceiling.
    cmds, i = {}, 0
    while i < len(parts):
        name, i = os.path.basename(parts[i]), i + 1
        args = []
        while i < len(parts) and parts[i].startswith("-"):
            args.append(parts[i])
            i += 1
            if args[-1] in ("-n", "-c") and i < len(parts) and not parts[i].startswith("-"):
                args.append(parts[i])
                i += 1
        cmds[name] = args
    if "taskset" in cmds:
        text = "Rust programs and Python scripts run under `taskset " + " ".join(cmds["taskset"]) + "`"
    else:
        text = "Rust programs and Python scripts run under `" + " ".join([os.path.basename(parts[0])] + parts[1:]) + "`"
    extras = []
    if "nice" in cmds:
        extras.append("at nice " + cmds["nice"][-1])
    if "ionice" in cmds:
        extras.append("ionice " + " ".join(cmds["ionice"]))
    if extras:
        text += " " + ", ".join(extras)
    m = re.search(r"-c\s+(\d+)-(\d+)$", pin)
    try:
        lo, _, hi = performance.strip().partition("-")
        if m and int(lo) <= int(m.group(1)) and int(m.group(2)) <= int(hi or lo):
            text += " (performance-core threads)"
    except ValueError:  # "" (not known) or a list that is not one range: say nothing
        pass
    return text
