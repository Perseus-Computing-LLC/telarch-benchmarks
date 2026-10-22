"""BEAM time anchors (round-3 B1 and B2): every turn in a batch carries its batch's time_anchor as its date, and
what every arm renders without --resolve-dates is byte-identical to the loader before B1 (7e990b7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from telarch_evals import bench_beam
from telarch_evals.common import Context, OfflineClient
from telarch_evals.llm import Meter
from telarch_evals.memory import FullContextMemory, TelarchMemory, Unit
from test_offline import FakeBeam

units_for = bench_beam.units_for  # the B1 loader; the screens below swap bench_beam.units_for per run


def _units_for_7e990b7(chat: list) -> list[Unit]:
    """bench_beam.units_for before B1, verbatim: a turn's date is its own time_anchor, if it has one."""
    units: list[Unit] = []
    for bi, batch in enumerate(bench_beam._batches(chat)):
        for ti, group in enumerate(batch.get("turns") or []):
            messages = [m for m in (group if isinstance(group, list) else [group])
                        if isinstance(m, dict) and str(m.get("content", "")).strip()]
            if not messages:
                continue
            date = next((str(m["time_anchor"]) for m in messages if m.get("time_anchor")), "")
            text = "\n".join(f"{m.get('role', 'user')}: {str(m['content']).strip()}" for m in messages)
            session = f"{batch.get('plan', '')}/batch-{batch.get('batch_number', bi)}"
            units.append(Unit(uid=f"{bi}:{ti}", session=session, order=(bi, ti), date=date, text=text,
                              rendered=(f"[{date}]\n" if date else "") + text,
                              turns=[{"role": m.get("role", "user"), "content": str(m["content"])} for m in messages]))
    return units


# Every 100K-10M BEAM chat has one time_anchor per batch, on its first turn (batch 1 here). Batches 2 and 3 add
# what real chats don't have: an anchor after the first turn and on a later message, a second anchor, no anchor
# at all, and an empty turn.
CHAT = [
    {"batch_number": 1, "turns": [
        [{"role": "user", "content": "I start at the new firm tomorrow.", "time_anchor": "March-15-2024"},
         {"role": "assistant", "content": "Good luck at the firm!"}],
        [{"role": "user", "content": "Yesterday I signed the contract."},
         {"role": "assistant", "content": "Congratulations on the contract."}],
        [{"role": "user", "content": "   "}],
        [{"role": "user", "content": "I planted tomatoes in the garden."},
         {"role": "assistant", "content": "Tomatoes like sun."}]]},
    {"batch_number": 2, "turns": [
        [{"role": "user", "content": "The landlord called about the lease."}],
        [{"role": "assistant", "content": "Noted."},
         {"role": "user", "content": "Last week I moved flats.", "time_anchor": "April-02-2024"}],
        [{"role": "user", "content": "Two weeks ago the landlord raised the rent."}],
        [{"role": "user", "content": "The movers came back.", "time_anchor": "April-09-2024"}],
        [{"role": "user", "content": "I paid the movers yesterday."}]]},
    {"batch_number": 3, "turns": [[{"role": "user", "content": "No date in this batch."}]]},
]
UIDS = ["0:0", "0:1", "0:3", "1:0", "1:1", "1:2", "1:3", "1:4", "2:0"]
DATES_BEFORE = ["March-15-2024", "", "", "", "April-02-2024", "", "April-09-2024", "", ""]
DATES_B1 = ["March-15-2024"] * 3 + ["April-02-2024"] * 3 + ["April-09-2024"] * 2 + [""]

QUESTIONS = {"temporal_reasoning": [{"question": "When did I sign the contract?", "rubric": ["14 March 2024"]},
                                    {"question": "When did the landlord raise the rent?", "rubric": ["19 March 2024"]}],
             "information_extraction": [{"question": "What did I plant in the garden?", "rubric": ["tomatoes"]}]}
CONTRACT, RENT, GARDEN = "100K/1/temporal_reasoning/0", "100K/1/temporal_reasoning/1", "100K/1/information_extraction/0"


def _fields(u: Unit) -> tuple:
    return u.uid, u.session, u.order, u.text, u.rendered, u.turns, u.timestamp, u.meta


def test_every_turn_carries_its_batch_anchor_and_nothing_else_changes():
    new, before = units_for(CHAT), _units_for_7e990b7(CHAT)
    assert [_fields(u) for u in new] == [_fields(u) for u in before]
    assert [u.uid for u in new] == UIDS
    assert [u.date for u in before] == DATES_BEFORE
    assert [u.date for u in new] == DATES_B1
    assert new[4].rendered == "[April-02-2024]\nassistant: Noted.\nuser: Last week I moved flats."
    assert new[1].rendered == "user: Yesterday I signed the contract.\nassistant: Congratulations on the contract."


def _screen(tmp_path: Path, monkeypatch, tag: str, system, loader) -> dict[str, dict]:
    """A --retrieval-only BEAM screen of CHAT with the given loader; its rows by question id."""
    data = tmp_path / "beam"
    if not data.exists():
        (data / "1" / "probing_questions").mkdir(parents=True)
        (data / "1" / "chat.json").write_text(json.dumps(CHAT))
        (data / "1" / "probing_questions" / "probing_questions.json").write_text(json.dumps(QUESTIONS))
    monkeypatch.setattr(bench_beam, "units_for", loader)
    ctx = Context(runs_root=tmp_path / tag / "screens", workdir=tmp_path / tag / "work", system=system,
                  reader=OfflineClient("reader"), judge=OfflineClient("judge"), meter=Meter(), workers=1,
                  retrieval_only=True)
    out = bench_beam.run(ctx, data_dir=data, official=FakeBeam(), abilities=sorted(QUESTIONS))
    rows = [json.loads(line) for line in (out / "judgments.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["status"] == "ok" for r in rows)
    return {r["id"]: r for r in rows}


ARMS = {"full-context": lambda work: FullContextMemory(budget=100_000),
        "telarch-encrypted": lambda work: TelarchMemory(work, budget=400),
        "telarch-plaintext": lambda work: TelarchMemory(work, budget=400, encrypt=False)}


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_every_arm_sees_the_same_prompts_without_resolve_dates(tmp_path: Path, monkeypatch, arm: str):
    before = _screen(tmp_path, monkeypatch, "before", ARMS[arm](tmp_path / "w-before"), _units_for_7e990b7)
    new = _screen(tmp_path, monkeypatch, "b1", ARMS[arm](tmp_path / "w-b1"), units_for)
    assert {k: r["prompt_sha256"] for k, r in new.items()} == {k: r["prompt_sha256"] for k, r in before.items()}
    assert all(r["memory"].get("dated_units", 0) == 0 for r in new.values())


def test_resolve_dates_reaches_the_turns_after_the_anchor(tmp_path: Path, monkeypatch):
    def dated(rows: dict[str, dict]) -> dict[str, int]:
        return {k: r["memory"]["dated_units"] for k, r in rows.items()}

    off = _screen(tmp_path, monkeypatch, "off", TelarchMemory(tmp_path / "w-off", budget=400), units_for)
    on = _screen(tmp_path, monkeypatch, "on", TelarchMemory(tmp_path / "w-on", budget=400, resolve_dates=True),
                 units_for)
    before = _screen(tmp_path, monkeypatch, "before",
                     TelarchMemory(tmp_path / "w-before", budget=400, resolve_dates=True), _units_for_7e990b7)
    assert dated(before) == {CONTRACT: 0, RENT: 0, GARDEN: 0}  # neither turn had a date of its own
    assert dated(on) == {CONTRACT: 1, RENT: 1, GARDEN: 0}
    assert on[CONTRACT]["prompt_sha256"] != off[CONTRACT]["prompt_sha256"]
    assert on[GARDEN]["prompt_sha256"] == off[GARDEN]["prompt_sha256"]  # no resolved date: the same prompt

    mem = TelarchMemory(tmp_path / "w-direct", budget=400, resolve_dates=True)
    mem.open("h", units_for(CHAT))
    try:
        seen = {u.uid: u for u in mem.recall("h", "When did the landlord raise the rent?").units}
    finally:
        mem.close("h")
    assert seen["1:2"].rendered == "user: Two weeks ago [19 March 2024] the landlord raised the rent."
    assert seen["1:0"].rendered == "user: The landlord called about the lease."
