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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED = ("harpe", "kibisis", "argus-ledger")
# Files that `make bench` rewrites. Changes there do not make the measuring code differ from the commit.
OUTPUTS = ("reports/", "claims.json", "sbom/")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def hardware() -> str:
    model = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            model = next((line.split(":", 1)[1] for line in f if line.startswith("model name")), "")
    except OSError:
        pass
    model = " ".join(re.sub(r"\((R|TM)\)", "", model).split()) or platform.processor() or "unknown CPU"
    return f"{platform.system()} {platform.machine()}, {model}, {os.cpu_count()} logical CPUs"


def pinning(pin: str) -> str:
    """How the run placed the programs and scripts; `pin` is the command prefix run_once.sh or the
    Makefile put before each program: a taskset, optionally preceded by nice and ionice."""
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
        with open("/sys/devices/cpu_core/cpus", encoding="utf-8") as f:  # present on hybrid Intel CPUs only
            lo, _, hi = f.read().strip().partition("-")
        if m and int(lo) <= int(m.group(1)) and int(m.group(2)) <= int(hi or lo):
            text += " (performance-core threads)"
    except (OSError, ValueError):
        pass
    return text
