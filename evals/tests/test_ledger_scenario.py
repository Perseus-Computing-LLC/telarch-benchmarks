"""B-R5 + R-6 (P4): bitemporal gold re-derived from the vault journal; a benchmark scenario built from the
program's own Argus ledger and Kibisis vault.

B-R5. The vault's audit journal is a transaction-time record (put / update / state_transition / supersede /
archive, each with a timestamp and a hash chain); entities also carry a valid-time interval (valid_from /
valid_until). From those two clocks the gold for "as of T" questions is derived mechanically, so nobody
hand-writes a temporal label and the gold is reproducible from the journal head hash. Question kinds:

- state_at        — the epistemic state of (category, key) as of transaction time T
                    (absent / candidate / verified / disputed / revoked / archived)
- current_at      — which key in a supersession chain was current as of T
- valid_at        — whether the entry was *valid* at T (valid time), which can differ from whether it
                    existed at T (transaction time): the bitemporal cases
- superseded_by   — the successor key and when

R-6. A scenario is the redacted event stream (ledger + journal, no content, no raw session ids) plus the
gold, with counts of the four pressures the roadmap names: decaying lessons (TCI lifecycle transitions),
contradictions (supersessions, revocations), concurrent writers (keys touched by more than one author) and
budget pressure (context compressions). A naive "latest state" answerer is the built-in baseline; it must
lose on the as-of questions, which is the point of having a bitemporal gold at all.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from telarch_evals import ledger_scenario as ls

T0 = 1_790_000_000.0
H = 3600.0


def J(seq, ts, action, cat, key, author="a1", **meta):
    return {"sequence": seq, "timestamp": ts, "action": action, "category": cat, "key": key, "scope": "default",
            "value_sha256": hashlib.sha256(f"{cat}:{key}:{seq}".encode()).hexdigest(), "prev_hash": "p",
            "event_hash": hashlib.sha256(f"e{seq}".encode()).hexdigest(), "author_id": author,
            "metadata_json": json.dumps(meta)}


def E(cat, key, state="verified", valid_from=None, valid_until=None, archived=False, supersedes=None,
      superseded_by=None, created_at=T0):
    return {"category": cat, "key": key, "scope": "default", "epistemic_state": state, "valid_from": valid_from,
            "valid_until": valid_until, "archived": archived, "supersedes": supersedes, "superseded_by": superseded_by,
            "created_at": created_at}


@pytest.fixture
def journal():
    return [
        J(1, T0 + 0 * H, "put", "decision", "rule_v1", epistemic_state="candidate", version=1),
        J(2, T0 + 2 * H, "state_transition", "decision", "rule_v1", **{"from": "candidate", "to": "verified",
                                                                     "event": "admit", "origin": "model", "reviewer": "user"}),
        J(3, T0 + 5 * H, "put", "decision", "rule_v2", author="a2", epistemic_state="candidate", version=1),
        J(4, T0 + 5 * H + 1, "supersede", "decision", "rule_v1", author="a2", superseded_at=T0 + 5 * H + 1,
          superseded_by="decision:rule_v2"),
        J(5, T0 + 6 * H, "state_transition", "decision", "rule_v2", author="a2", **{"from": "candidate", "to": "verified",
                                                                     "event": "admit", "origin": "model", "reviewer": "user"}),
        J(6, T0 + 9 * H, "state_transition", "decision", "rule_v1", **{"from": "verified", "to": "revoked",
                                                                     "event": "revoke", "origin": "model", "reviewer": "user"}),
        J(7, T0 + 10 * H, "archive", "decision", "rule_v1"),
        # a lesson that was valid before it was recorded (valid_from earlier than the put) and expired by valid_until
        J(8, T0 + 4 * H, "put", "insight", "lesson", epistemic_state="verified", version=1),
        J(9, T0 + 7 * H, "update", "insight", "lesson", epistemic_state="verified", version=2),
    ]


@pytest.fixture
def entities():
    return [
        E("decision", "rule_v1", state="revoked", archived=True, superseded_by="decision:rule_v2", valid_from=T0,
          valid_until=T0 + 5 * H + 1),
        E("decision", "rule_v2", state="verified", supersedes="decision:rule_v1", valid_from=T0 + 5 * H + 1,
          created_at=T0 + 5 * H),
        E("insight", "lesson", state="verified", valid_from=T0 + 1 * H, valid_until=T0 + 8 * H, created_at=T0 + 4 * H),
    ]


def test_timelines_follow_transaction_time(journal):
    tl = ls.derive_timelines(journal)
    r1 = tl["default/decision/rule_v1"]
    assert [(round((e["ts"] - T0) / H), e["state"]) for e in r1] == [
        (0, "candidate"), (2, "verified"), (5, "verified"), (9, "revoked"), (10, "archived")]
    assert r1[2]["superseded_by"] == "default/decision/rule_v2"
    assert ls.state_at(tl, "default/decision/rule_v1", T0 - 1) == "absent"
    assert ls.state_at(tl, "default/decision/rule_v1", T0 + 1 * H) == "candidate"
    assert ls.state_at(tl, "default/decision/rule_v1", T0 + 9.5 * H) == "revoked"
    assert ls.state_at(tl, "default/decision/rule_v1", T0 + 11 * H) == "archived"
    assert ls.current_at(tl, "default/decision/rule_v1", T0 + 3 * H) == "default/decision/rule_v1"
    assert ls.current_at(tl, "default/decision/rule_v1", T0 + 5 * H + 2) == "default/decision/rule_v2"


def test_gold_is_derived_deterministic_and_bitemporal(journal, entities):
    gold = ls.derive_gold(journal, entities, probes_per_key=3, seed=7)
    kinds = {g["kind"] for g in gold}
    assert kinds == {"state_at", "current_at", "valid_at", "superseded_by"}
    assert all(set(g) >= {"id", "kind", "key", "as_of", "answer", "derivation"} for g in gold)
    assert gold == ls.derive_gold(journal, entities, probes_per_key=3, seed=7)
    assert gold != ls.derive_gold(journal, entities, probes_per_key=3, seed=8)
    # the bitemporal case: the lesson was valid at T0+2h (valid time) but did not exist yet (transaction time)
    lesson_valid = [g for g in gold if g["kind"] == "valid_at" and g["key"] == "default/insight/lesson"]
    assert lesson_valid, gold
    probe = ls.gold_item(journal, entities, "valid_at", "default/insight/lesson", T0 + 2 * H)
    assert probe["answer"] == "valid" and probe["derivation"]["recorded_at_as_of"] == "absent"
    probe2 = ls.gold_item(journal, entities, "valid_at", "default/insight/lesson", T0 + 9 * H)
    assert probe2["answer"] == "expired" and probe2["derivation"]["recorded_at_as_of"] == "verified"
    sb = ls.gold_item(journal, entities, "superseded_by", "default/decision/rule_v1", None)
    assert sb["answer"] == {"key": "default/decision/rule_v2", "at": T0 + 5 * H + 1}
    # no content, ever: the gold carries keys, states and times only
    assert "content" not in json.dumps(gold)


def test_naive_latest_baseline_loses_on_as_of_questions(journal, entities):
    gold = ls.derive_gold(journal, entities, probes_per_key=4, seed=1)
    oracle = ls.score([ls.answer_oracle(g, journal, entities) for g in gold], gold)
    naive = ls.score([ls.answer_naive_latest(g, entities) for g in gold], gold)
    assert oracle["accuracy"] == 1.0 and oracle["n"] == len(gold)
    assert naive["accuracy"] < 1.0
    assert naive["by_kind"]["state_at"]["accuracy"] < 1.0   # it answers 'revoked'/'archived' for every T
    assert set(naive["by_kind"]) == {g["kind"] for g in gold}


def _ledger_rows():
    rows = []
    seq = 1
    def add(ts, et, payload):
        nonlocal seq
        rows.append({"sequence": seq, "timestamp": ts, "event_type": et, "actor": "hermes", "payload": payload,
                     "event_hash": hashlib.sha256(f"l{seq}".encode()).hexdigest()})
        seq += 1
    for i in range(6):
        add(T0 + i * H, "tool_execution", {"tool": "terminal", "session_ref": "s1" * 16, "outcome": "ok",
                                           "command": "cat /etc/passwd  # must never appear in a scenario"})
    add(T0 + 1 * H, "dtoc.compress", {"session_ref": "s1" * 16, "tokens_before": 90000, "tokens_after": 30000})
    add(T0 + 3 * H, "tci.self_improvement_cycle", {"transitions": [["terminal.x", "active", "graduated"],
                                                                     ["read.y", "graduated", "retired"]]})
    add(T0 + 4 * H, "kibisis.state_transition", {"category": "decision", "key": "rule_v1", "from": "verified",
                                                 "to": "revoked"})
    return rows


def test_scenario_is_redacted_counted_and_digested(journal, entities, tmp_path):
    ledger = _ledger_rows()
    sc = ls.build_scenario(ledger, journal, entities, probes_per_key=2, seed=3)
    assert sc["version"] == 1
    # decaying: two lifecycle transitions; contradictions: 1 supersede + 1 revoke in the journal + 1 revoke in the
    # ledger; concurrent writers: rule_v1 is touched by a1 and a2 (rule_v2 by a2 alone); budget: one compression
    assert sc["features"] == {"decaying_lessons": 2, "contradictions": 3, "concurrent_writers": 1, "budget_pressure": 1}
    text = json.dumps(sc)
    assert "passwd" not in text and "command" not in text and "content" not in text
    evs = sc["events"]
    assert evs == sorted(evs, key=lambda e: (e["ts"], e["source"], e["seq"]))
    assert {e["source"] for e in evs} == {"ledger", "journal"}
    assert all(set(e) <= {"ts", "source", "seq", "type", "tool", "session_ref", "outcome", "category", "key", "action",
                          "from", "to", "author", "ratio_bp", "transitions"} for e in evs)
    assert sc["gold"] and sc["journal_head"] == journal[-1]["event_hash"] and sc["ledger_head"] == ledger[-1]["event_hash"]
    canon = {k: v for k, v in sc.items() if k != "digest"}
    assert sc["digest"] == hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # written out and read back byte-identical
    out = ls.write_scenario(sc, tmp_path / "sc")
    assert (out / "scenario.json").exists() and (out / "gold.jsonl").exists() and (out / "baseline_naive.json").exists()
    assert json.loads((out / "scenario.json").read_text())["digest"] == sc["digest"]
    base = json.loads((out / "baseline_naive.json").read_text())
    assert base["accuracy"] < 1.0 and base["scenario_digest"] == sc["digest"]


def test_cli_builds_a_scenario_from_exports(journal, entities, tmp_path):
    from telarch_evals.cli import main
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in journal) + "\n")
    (tmp_path / "entities.jsonl").write_text("\n".join(json.dumps(e) for e in entities) + "\n")
    (tmp_path / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in _ledger_rows()) + "\n")
    rc = main(["ledger-scenario", "--journal", str(tmp_path / "journal.jsonl"), "--entities", str(tmp_path / "entities.jsonl"),
               "--ledger", str(tmp_path / "audit.jsonl"), "--out", str(tmp_path / "out"), "--seed", "5", "--probes-per-key", "2"])
    assert rc == 0
    sc = json.loads((tmp_path / "out" / "scenario.json").read_text())
    assert sc["features"]["decaying_lessons"] == 2 and len(sc["gold"]) > 0
