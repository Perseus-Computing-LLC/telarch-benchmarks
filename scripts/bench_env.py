"""Run details shared by the report scripts: commit, toolchain, crate versions, hardware and time.

Read from the checkout and the machine when a script runs, so a regenerated report never carries
values typed in for an earlier run.
"""

import os
import platform
import re
import subprocess
import time
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED = ("harpe", "kibisis", "argus-ledger")
# Files that `make bench` rewrites. Changes there do not make the measuring code differ from the commit.
OUTPUTS = ("reports/", "claims.json", "sbom/")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def commit(full: bool = False) -> str:
    """SHA of HEAD, with "-dirty" when tracked files other than the outputs differ from it."""
    sha = _git("rev-parse", *([] if full else ["--short=7"]), "HEAD").strip()
    if not sha:
        return "unknown"
    changed = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no").splitlines() if line]
    return sha + ("-dirty" if any(not p.startswith(OUTPUTS) for p in changed) else "")


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
    """How `make bench` placed the programs; `pin` is the taskset prefix the Makefile used, if any."""
    parts = pin.split()
    if not parts:
        return "programs not pinned to CPUs"
    text = "Rust programs run under `" + " ".join([os.path.basename(parts[0])] + parts[1:]) + "`"
    m = re.search(r"-c\s+(\d+)-(\d+)$", pin)
    try:
        with open("/sys/devices/cpu_core/cpus", encoding="utf-8") as f:  # present on hybrid Intel CPUs only
            lo, _, hi = f.read().strip().partition("-")
        if m and int(lo) <= int(m.group(1)) and int(m.group(2)) <= int(hi or lo):
            text += " (performance-core threads)"
    except (OSError, ValueError):
        pass
    return text + "; Python scripts not pinned"
