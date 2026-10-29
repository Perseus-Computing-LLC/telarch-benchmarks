"""Bitemporal gold from the vault journal (B-R5) and a benchmark scenario from the program's own ledger and
vault (R-6).

Two clocks. The Kibisis audit journal is *transaction time*: when the vault learned something (put, update,
state_transition, supersede, archive), hash-chained. Entities carry *valid time*: the interval the fact is
asserted to hold (valid_from / valid_until). Questions "as of T" have mechanical answers under each clock,
and the interesting cases are where the clocks disagree — a lesson valid before it was recorded, a rule
still on file after it stopped applying. The gold here is derived from those records, never written by
hand, and is pinned to the journal head hash so it can be regenerated and checked.

Nothing in a gold item or a scenario event is content: keys, states, kinds, times, counts, opaque session
refs. Keys can still be private; generated artifacts stay local unless they pass the outbound gate.

Inputs are plain row dicts (journal rows, entity rows, Argus ledger rows) so the module needs neither
kibisis nor the vault passphrase; ``scripts/export_kibisis_journal.py`` produces the two vault exports with
the interpreter that has kibisis.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCENARIO_VERSION = 1
KINDS = ("state_at", "current_at", "valid_at", "superseded_by")
_EVENT_FIELDS = ("ts", "source", "seq", "type", "tool", "session_ref", "outcome", "category", "key", "action",
                 "from", "to", "author", "ratio_bp", "transitions")
_TERMINAL_STATES = ("revoked", "archived")


def _ref(scope: str, category: str, key: str) -> str:
    return f"{scope or 'default'}/{category}/{key}"


def _ref_from_colon(scope: str, text: str | None) -> str | None:
    """Journal metadata names a successor as 'category:key'."""
    if not text:
        return None
    cat, _, key = str(text).partition(":")
    return _ref(scope, cat, key) if key else None


def _meta(row: dict) -> dict:
    m = row.get("metadata_json") or row.get("metadata") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except json.JSONDecodeError:
            m = {}
    return m if isinstance(m, dict) else {}


def _canon_digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- transaction-time timelines (B-R5)

def derive_timelines(journal: list[dict]) -> dict[str, list[dict]]:
    """Per entity ref, the ordered list of states the vault recorded, each ``{ts, seq, action, state,
    version, author, superseded_by}``. ``state`` is the epistemic state after the event; archive is
    ``archived``. Later events carry forward the fields they do not change."""
    rows = sorted(journal, key=lambda r: (float(r["timestamp"]), int(r.get("sequence") or 0)))
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        ref = _ref(r.get("scope") or "default", r["category"], r["key"])
        m = _meta(r)
        prev = out[ref][-1] if out[ref] else {"state": "absent", "version": 0, "superseded_by": None}
        state, version, sup = prev["state"], prev["version"], prev["superseded_by"]
        action = r["action"]
        if action in ("put", "update", "adopt", "migrate"):
            state = m.get("epistemic_state") or (state if state != "absent" else "candidate")
            version = int(m.get("version") or (version + 1 if action == "update" else max(version, 1)))
        elif action == "state_transition":
            state = m.get("to") or state
        elif action in ("supersede", "supersession"):
            sup = _ref_from_colon(r.get("scope") or "default", m.get("superseded_by")) or sup
        elif action == "archive":
            state = "archived"
        elif action in ("provenance_reclassified", "refuse"):
            pass   # provenance edits do not move the epistemic state; refusals never landed
        out[ref].append({"ts": float(r["timestamp"]), "seq": int(r.get("sequence") or 0), "action": action,
                         "state": state, "version": version, "author": r.get("author_id"), "superseded_by": sup})
    return dict(out)


def _as_of(timeline: list[dict], t: float) -> dict | None:
    cur = None
    for e in timeline:
        if e["ts"] <= t:
            cur = e
        else:
            break
    return cur


def state_at(timelines: dict[str, list[dict]], ref: str, t: float) -> str:
    e = _as_of(timelines.get(ref, []), t)
    return e["state"] if e else "absent"


def current_at(timelines: dict[str, list[dict]], ref: str, t: float, _depth: int = 0) -> str:
    """Follow supersessions recorded by T. The chain head is the key that was current as of T."""
    e = _as_of(timelines.get(ref, []), t)
    if not e or not e["superseded_by"] or e["superseded_by"] == ref or _depth > 64:
        return ref
    return current_at(timelines, e["superseded_by"], t, _depth + 1)


def superseded_by(timelines: dict[str, list[dict]], ref: str) -> dict | None:
    for e in timelines.get(ref, []):
        if e["action"] in ("supersede", "supersession") and e["superseded_by"]:
            return {"key": e["superseded_by"], "at": e["ts"]}
    return None


# ---------------------------------------------------------------- valid time + gold items

def _entity_index(entities: list[dict]) -> dict[str, dict]:
    return {_ref(e.get("scope") or "default", e["category"], e["key"]): e for e in entities}


def valid_at(entity: dict | None, t: float) -> str:
    """valid time: ``valid`` inside [valid_from, valid_until), ``not_yet`` before, ``expired`` after,
    ``unbounded`` when the entity declares no interval, ``absent`` without an entity."""
    if entity is None:
        return "absent"
    vf, vu = entity.get("valid_from"), entity.get("valid_until")
    if vf is None and vu is None:
        return "unbounded"
    if vf is not None and t < float(vf):
        return "not_yet"
    if vu is not None and t >= float(vu):
        return "expired"
    return "valid"


def gold_item(journal: list[dict], entities: list[dict], kind: str, ref: str, as_of: float | None,
              timelines: dict[str, list[dict]] | None = None) -> dict:
    tl = timelines if timelines is not None else derive_timelines(journal)
    ents = _entity_index(entities)
    if kind != "superseded_by" and as_of is None:
        raise ValueError(f"{kind} needs an as_of time")
    as_of_t = float(as_of) if as_of is not None else 0.0
    if kind == "state_at":
        answer: Any = state_at(tl, ref, as_of_t)
        deriv = {"clock": "transaction", "events_at_or_before": sum(1 for e in tl.get(ref, []) if e["ts"] <= as_of_t)}
    elif kind == "current_at":
        answer = current_at(tl, ref, as_of_t)
        deriv = {"clock": "transaction", "chain_hops": _hops(tl, ref, as_of_t)}
    elif kind == "valid_at":
        answer = valid_at(ents.get(ref), as_of_t)
        deriv = {"clock": "valid", "recorded_at_as_of": state_at(tl, ref, as_of_t)}
    elif kind == "superseded_by":
        answer = superseded_by(tl, ref)
        deriv = {"clock": "transaction"}
    else:
        raise ValueError(f"unknown gold kind {kind!r}")
    ident = _canon_digest([kind, ref, as_of])[:16]
    return {"id": ident, "kind": kind, "key": ref, "as_of": as_of, "answer": answer, "derivation": deriv}


def _hops(tl, ref, t):
    hops, cur = 0, ref
    while hops < 64:
        e = _as_of(tl.get(cur, []), t)
        if not e or not e["superseded_by"] or e["superseded_by"] == cur:
            return hops
        cur, hops = e["superseded_by"], hops + 1
    return hops


def derive_gold(journal: list[dict], entities: list[dict], probes_per_key: int = 3, seed: int = 0) -> list[dict]:
    """Seeded probe times per key: one just before each recorded event, one just after, plus random draws
    inside the span, so every state boundary is exercised. valid_at probes add the valid-time boundaries."""
    tl = derive_timelines(journal)
    ents = _entity_index(entities)
    rng = random.Random(seed)
    all_ts = [e["ts"] for t in tl.values() for e in t]
    if not all_ts:
        return []
    span = (min(all_ts) - 3600.0, max(all_ts) + 3600.0)
    gold: list[dict] = []
    for ref in sorted(tl):
        boundaries = sorted({e["ts"] for e in tl[ref]})
        ent = ents.get(ref)
        for b in (ent.get("valid_from"), ent.get("valid_until")) if ent else ():
            if b is not None:
                boundaries.append(float(b))
        probes = set()
        for b in boundaries:
            probes.add(b - 1.0)
            probes.add(b + 1.0)
        for _ in range(probes_per_key):
            probes.add(round(rng.uniform(*span), 3))
        for t in sorted(probes):
            gold.append(gold_item(journal, entities, "state_at", ref, t, tl))
            if any(e["superseded_by"] for e in tl[ref]):
                gold.append(gold_item(journal, entities, "current_at", ref, t, tl))
            if ent is not None and (ent.get("valid_from") is not None or ent.get("valid_until") is not None):
                gold.append(gold_item(journal, entities, "valid_at", ref, t, tl))
        if superseded_by(tl, ref):
            gold.append(gold_item(journal, entities, "superseded_by", ref, None, tl))
    return gold


# ---------------------------------------------------------------- answerers and scoring

def answer_oracle(g: dict, journal: list[dict], entities: list[dict]) -> dict:
    return {"id": g["id"], "answer": gold_item(journal, entities, g["kind"], g["key"], g["as_of"])["answer"]}


def answer_naive_latest(g: dict, entities: list[dict]) -> dict:
    """The baseline every non-temporal memory is: whatever the vault says *now* is the answer for every T."""
    ents = _entity_index(entities)
    e = ents.get(g["key"])
    if g["kind"] == "state_at":
        ans: Any = "absent" if e is None else ("archived" if e.get("archived") else e.get("epistemic_state"))
    elif g["kind"] == "current_at":
        cur, hops = g["key"], 0
        while hops < 64 and ents.get(cur, {}).get("superseded_by"):
            nxt = _ref_from_colon(ents[cur].get("scope") or "default", ents[cur]["superseded_by"])
            if not nxt or nxt == cur:
                break
            cur, hops = nxt, hops + 1
        ans = cur
    elif g["kind"] == "valid_at":
        ans = "absent" if e is None else ("unbounded" if e.get("valid_from") is None and e.get("valid_until") is None
                                          else ("expired" if e.get("valid_until") is not None else "valid"))
    else:   # superseded_by: the entity's own pointer, time unknown to a snapshot
        nxt = e and _ref_from_colon(e.get("scope") or "default", e.get("superseded_by"))
        ans = {"key": nxt, "at": None} if nxt else None
    return {"id": g["id"], "answer": ans}


def score(answers: list[dict], gold: list[dict]) -> dict:
    by_id = {a["id"]: a.get("answer") for a in answers}
    per_kind: dict[str, list[bool]] = defaultdict(list)
    for g in gold:
        per_kind[g["kind"]].append(by_id.get(g["id"]) == g["answer"])
    flat = [ok for oks in per_kind.values() for ok in oks]
    return {"n": len(flat), "correct": sum(flat), "accuracy": (sum(flat) / len(flat)) if flat else 0.0,
            "by_kind": {k: {"n": len(v), "correct": sum(v), "accuracy": sum(v) / len(v)} for k, v in sorted(per_kind.items())}}


# ---------------------------------------------------------------- scenario (R-6)

def _ledger_event(r: dict) -> dict | None:
    et = r.get("event_type") or ""
    p = r.get("payload") or {}
    base = {"ts": _ts(r), "source": "ledger", "seq": int(r.get("sequence") or 0), "type": et}
    if et == "tool_execution":
        base.update(tool=str(p.get("tool") or ""), session_ref=str(p.get("session_ref") or ""),
                    outcome=str(p.get("outcome") or p.get("status") or ""))
    elif et in ("dtoc.compress", "harpe.synthesize"):
        before, after = p.get("tokens_before"), p.get("tokens_after")
        if before:
            base["ratio_bp"] = int(round(10_000 * float(after or 0) / float(before)))
    elif et == "tci.self_improvement_cycle":
        base["transitions"] = [[str(a), str(b), str(c)] for a, b, c in (p.get("transitions") or []) if c]
    elif et.startswith("kibisis."):
        base.update(category=str(p.get("category") or ""), key=str(p.get("key") or ""),
                    **{"from": str(p.get("from") or ""), "to": str(p.get("to") or "")})
    else:
        return None
    return base


def _ts(r: dict) -> float:
    t = r.get("timestamp") or r.get("ts")
    if isinstance(t, (int, float)):
        return float(t)
    from datetime import datetime
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()


def _journal_event(r: dict) -> dict:
    m = _meta(r)
    return {"ts": float(r["timestamp"]), "source": "journal", "seq": int(r.get("sequence") or 0),
            "type": "kibisis.journal", "category": r["category"], "key": r["key"], "action": r["action"],
            "author": str(r.get("author_id") or ""), **{"from": str(m.get("from") or ""), "to": str(m.get("to") or "")}}


def build_scenario(ledger: list[dict], journal: list[dict], entities: list[dict], probes_per_key: int = 3,
                   seed: int = 0) -> dict:
    events = [e for e in (_ledger_event(r) for r in ledger) if e] + [_journal_event(r) for r in journal]
    events = [{k: v for k, v in e.items() if k in _EVENT_FIELDS and v not in ("", None, [])} for e in events]
    events.sort(key=lambda e: (e["ts"], e["source"], e["seq"]))
    decaying = sum(1 for e in events if e.get("type") == "tci.self_improvement_cycle"
                   for t in e.get("transitions", []) if t[2] in ("graduated", "retired", "parked", "regressed"))
    contradictions = sum(1 for e in events if (e.get("action") in ("supersede", "supersession"))
                         or e.get("to") in ("revoked", "disputed"))
    authors: dict[str, set] = defaultdict(set)
    for r in journal:
        if r.get("author_id"):
            authors[_ref(r.get("scope") or "default", r["category"], r["key"])].add(r["author_id"])
    concurrent = sum(1 for a in authors.values() if len(a) > 1)
    budget = sum(1 for e in events if e.get("type") in ("dtoc.compress", "harpe.synthesize"))
    gold = derive_gold(journal, entities, probes_per_key=probes_per_key, seed=seed)
    sc = {"version": SCENARIO_VERSION, "seed": seed, "probes_per_key": probes_per_key,
          "features": {"decaying_lessons": decaying, "contradictions": contradictions,
                       "concurrent_writers": concurrent, "budget_pressure": budget},
          "counts": {"events": len(events), "ledger_events": sum(1 for e in events if e["source"] == "ledger"),
                     "journal_events": sum(1 for e in events if e["source"] == "journal"), "entities": len(entities),
                     "gold": len(gold), "gold_by_kind": dict(Counter(g["kind"] for g in gold))},
          "journal_head": (journal[-1].get("event_hash") if journal else None),
          "ledger_head": (ledger[-1].get("event_hash") if ledger else None),
          "events": events, "gold": gold}
    sc["digest"] = _canon_digest(sc)
    return sc


def write_scenario(sc: dict, out_dir: Path, entities: list[dict] | None = None) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scenario.json").write_text(json.dumps(sc, sort_keys=True, indent=1) + "\n", "utf-8")
    (out_dir / "gold.jsonl").write_text("".join(json.dumps(g, sort_keys=True) + "\n" for g in sc["gold"]), "utf-8")
    ents = entities if entities is not None else _entities_from_scenario(sc)
    base = score([answer_naive_latest(g, ents) for g in sc["gold"]], sc["gold"])
    base["scenario_digest"] = sc["digest"]
    base["answerer"] = "naive_latest"
    (out_dir / "baseline_naive.json").write_text(json.dumps(base, sort_keys=True, indent=1) + "\n", "utf-8")
    return out_dir


def _entities_from_scenario(sc: dict) -> list[dict]:
    """Without the entity export, the naive answerer only knows what the gold's keys imply: nothing. It
    then answers 'absent' everywhere, which is the honest floor."""
    return []


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]
