#!/usr/bin/env bash
# Run every benchmark program and gap script once, pinned to a fixed CPU set as in the published runs,
# and keep a copy of the resulting reports under reports/runs/<date>/<run>/ so build_claims.py can take
# medians across runs. Usage: scripts/run_once.sh <date> <run-name>   e.g. 2026-10-28 run1
#
# Runs are sequential on purpose: the programs share the pinned cores and the FileLedger phase
# shares the disk, so running them side by side would change the numbers being measured.
#
# TELARCH_CPUSET (default 0-9) is the taskset list and TELARCH_NICE (default 15) the niceness. The
# published host shares its 20 CPUs and its disk with other work, so the programs run on ten of the
# twelve performance-core threads at low CPU and I/O priority (ionice best-effort, lowest level):
# two performance threads stay free for whatever else the machine is doing, and a run cannot
# starve it. The prefix is recorded in run_env.txt; build_claims.py quotes it in the hardware line.
#
# ARGUS_FILE_EVENTS (default 100000) is how many events the on-disk FileLedger phase appends with a
# sync_data each. The full million took 1,161 s on this host's btrfs scratch disk, so that figure is
# kept as a separate single run (reports/step3_argus_file_ledger_1m.json) and the repeated runs use
# fewer events. Do not edit this script while a run is in progress: bash reads it incrementally.
set -euo pipefail

DATE="${1:?date, e.g. 2026-10-28}"
RUN="${2:?run name, e.g. run1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$( [ -x /opt/data/work/telarch-suite/.venv/bin/python ] && echo /opt/data/work/telarch-suite/.venv/bin/python || echo python3 )}"
CPUSET="${TELARCH_CPUSET:-0-9}"
NICE="${TELARCH_NICE:-15}"
PIN="nice -n $NICE"
# ionice -c2 -n7: best-effort class at its lowest level, so the FileLedger phase's sync_data per event
# queues behind other disk users rather than ahead of them.
if command -v ionice >/dev/null 2>&1; then PIN="$PIN ionice -c2 -n7"; fi
if command -v taskset >/dev/null 2>&1; then PIN="$PIN taskset -c $CPUSET"; fi
export TELARCH_PIN="$PIN"

DEST="reports/runs/$DATE/$RUN"
mkdir -p reports reports/raw_csvs "$DEST"

# Record what the machine looked like when the run started, so a noisy run can be identified later.
{
  echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "pin=$PIN"
  echo "cpuset=$CPUSET"
  echo "nice=$NICE"
  echo "ionice=$(command -v ionice >/dev/null 2>&1 && echo 'best-effort class 2, level 7' || echo none)"
  echo "loadavg=$(cut -d' ' -f1-3 /proc/loadavg)"
  echo "commit=$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
  echo "rustc=$(rustc --version)"
  echo "argus_file_events=${ARGUS_FILE_EVENTS:-100000}"
} > "$DEST/run_env.txt"

$PIN ./target/release/telarch-benchmarks --json reports/microbenchmarks_phase1.json
$PIN ./target/release/knapsack_cost_curve --json reports/step1_knapsack_cost_curve.json
$PIN ./target/release/adversarial_contention --json reports/step2_adversarial_contention.json
$PIN ./target/release/argus_1m_scale --json reports/step3_argus_1m_scale.json --file-events "${ARGUS_FILE_EVENTS:-100000}"
$PIN ./target/release/argus_anchored_checkpoints --json reports/gap6_argus_anchored_checkpoints.json
$PIN ./target/release/restricted_payload_test --json reports/restricted_payload.json
$PIN ./target/release/swarm_partition --json reports/swarm_partition.json
$PIN "$PYTHON" scripts/gap1_harpe_quality.py
$PIN "$PYTHON" scripts/gap2_harpe_alternatives.py
$PIN "$PYTHON" scripts/gap3_kibisis_multiproc_crashes.py
$PIN "$PYTHON" scripts/gap4_kibisis_model_check.py
$PIN "$PYTHON" scripts/gap5_supply_chain.py
$PIN "$PYTHON" scripts/gap7_hygiene.py

cp reports/*.json "$DEST/"
echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$DEST/run_env.txt"
echo "run_once: reports copied to $DEST"
