# B-A finding: `swarm_partition` non-convergence at 25 % / 50 % loss is a harness artefact

Date: 2026-10-28 · Issue: telarch-benchmarks #25 · Read: `telarch-benchmarks/src/bin/swarm_partition.rs` @ v0.1.0-alpha.9 (314 lines)

## Verdict

**Harness artefact. Not a Kibisis lease-protocol finding, and not an Argus finding.** The figures
`swarm_25pct_missing = 3,150` and `swarm_50pct_missing = 11,250` are the closed-form output of the
program's own loss model, and no suite protocol is exercised by the "reconciliation" phase at all.

## What the program does (phase 2, "Mesh Reconnect & Reconciliation", lines 167–190)

For every buffered message and every other node it makes **one** delivery attempt:

```
drop_hash  = (msg.seq * 31 + target_id * 17) % 100
dropped    if drop_hash < loss_pct
retry_hash = (drop_hash * 37 + 13) % 100
retry succeeds if retry_hash >= loss_pct
```

Both hashes are pure functions of `(seq, target_id)`. A `(message, node)` pair whose `drop_hash`
*and* `retry_hash` both fall under `loss_pct` can **never** be delivered — not by this pass, not by
any number of further passes with the same model. The pass is a single sweep; there is no digest
exchange, no re-request of missing sequence numbers, no second round. "Reconciliation" is one
unicast attempt plus one retry.

## Closed-form check (reproduced in Python, matches the three Greg runs and the EC2 runs exactly)

| loss | `drop_hash` residues that also fail the retry | never-delivered pairs | reported `missing_messages` |
|---|---|---|---|
| 25 % | 7 of 100: {0, 3, 8, 11, 16, 19, 24} | 45,000 × 7 % = **3,150** | 3,150 |
| 50 % | 25 of 100 | 45,000 × 25 % = **11,250** | 11,250 |

The figures are identical on every host because nothing in them depends on timing, the OS, or the
crates; "3 Greg + 5 EC2 runs agree" is the determinism of integer arithmetic, not corroboration of a
protocol defect.

## What the suite contributes to this program

- **Kibisis**: `InMemoryMemoryVault::put()` as a sink for each applied mutation (line 7 import; `vault.put` in
  `apply_mutation` / `receive_remote`). No lease is acquired, no fencing token is checked, no replication
  or anti-entropy API exists in kibisis to be exercised. The dedup set `received_seqs` is a harness
  `HashSet`, not vault state.
- **Argus**: `InMemoryLedger::append()` per local/replicated mutation and `verify_chain()` per node in
  phase 3. Every node's chain verifies in every scenario (`audit_verification_pass: true`) — that is
  the only suite property the program tests, and it holds.
- `identical_head_hash_pairs = 0` even at 0 % loss is expected: nodes append their own mutations
  first and replicated ones in arrival order, so heads differ by construction. It is not a convergence
  metric and should not be read as one.

## `nodes_converged` semantics

`nodes_converged = (missing_messages == 0)` where `missing = expected_remote − |received_seqs|`.
That is a correct definition of "every node holds every message". The README's current wording
("no: with one retry per dropped packet the 10 nodes end with different message sets; what holds is
per-node chain integrity") is accurate about the result but leaves the cause open; the cause is the
loss model, stated above.

## What the fix card should decide (not done here — B-A is the read)

1. Either model **transient** loss (fresh randomness per attempt, seeded for reproducibility) and run
   anti-entropy rounds until no node's set changes — then the program measures *reconciliation cost*
   (rounds, messages, ms) to convergence, which is a real figure — or
2. keep the permanent-loss model and drop the word "reconciliation": the scenario then measures
   per-node chain integrity under partial delivery, which is what it measures today.

Either way the evidence page should not carry "nodes converged: no" as if a suite protocol failed.
The program never gave one a chance to succeed.
