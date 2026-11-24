from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from telarch_evals.memory import RANKERS, TelarchMemory, Unit


def _utterance(uid: str, session: str, idx: int, speaker: str, content: str) -> Unit:
    txt = f"{speaker}: {content}"
    return Unit(
        uid=uid,
        session=session,
        order=(0, idx),
        date="2026-01-01",
        text=txt,
        rendered=txt,
        turns=[{"role": speaker, "content": content}],
    )


def _round(uid: str, session: str, idx: int, user_txt: str, asst_txt: str) -> Unit:
    txt = f"user: {user_txt}\nassistant: {asst_txt}"
    return Unit(
        uid=uid,
        session=session,
        order=(idx, 0),
        date="2026-01-01",
        text=txt,
        rendered=txt,
        turns=[{"role": "user", "content": user_txt}, {"role": "assistant", "content": asst_txt}],
    )


def test_defaults_leave_config_and_bm25_order_unchanged(tmp_path: Path) -> None:
    mem = TelarchMemory(tmp_path / "m", encrypt=False)
    cfg = mem.config()
    assert "ranker" not in cfg
    assert "word_forms" not in cfg


def test_episode_ranker_surfaces_adjacent_reply_and_user_turn_evidence(tmp_path: Path) -> None:
    # 1. Utterance-level history: u2 is the short answer right after u1's keyword hit.
    units = [
        _utterance("u1", "s1", 1, "Alice", "When did you attend the pottery workshop?"),
        _utterance("u2", "s1", 2, "Bob", "Last Thursday evening!"),
        _utterance("u3", "s2", 3, "Alice", "I saw a pottery display downtown."),
        _utterance("u4", "s3", 4, "Bob", "Pottery glazes need kiln firing."),
    ]
    m_ctrl = TelarchMemory(tmp_path / "ctrl", budget=22, encrypt=False, ranker="bm25")
    m_ep = TelarchMemory(tmp_path / "ep", budget=22, encrypt=False, ranker="episode")
    assert m_ep.config()["ranker"] == RANKERS["episode"]
    m_ctrl.open("h", units)
    m_ep.open("h", units)
    try:
        r_ctrl = m_ctrl.recall("h", "When was the pottery workshop?", lambda us: "\n".join(u.rendered for u in us))
        r_ep = m_ep.recall("h", "When was the pottery workshop?", lambda us: "\n".join(u.rendered for u in us))
        assert "u2" not in {u.uid for u in r_ctrl.units}
        assert {"u1", "u2"} <= {u.uid for u in r_ep.units}
    finally:
        m_ctrl.close("h")
        m_ep.close("h")

    # 2. User/assistant rounds: user-turn match beats assistant-only boilerplate repetition.
    boilerplate = " ".join(["calendar dentist appointment reminder"] * 40)
    rounds = [
        _round("r_noise", "s1", 1, "Can you give me general productivity tips?", boilerplate),
        _round("r_gold", "s2", 2, "My dentist appointment is on March 14 at 9am.", "Got it, noted for March 14."),
    ]
    m_strict = TelarchMemory(tmp_path / "strict", budget=80, encrypt=False, ranker="strict")
    assert m_strict.config()["ranker"] == RANKERS["strict"]
    m_strict.open("r", rounds)
    try:
        rec = m_strict.recall("r", "When is my dentist appointment?", lambda us: "\n\n".join(u.rendered for u in us))
        assert [u.uid for u in rec.units] == ["r_gold"]
    finally:
        m_strict.close("r")


def test_word_forms_flag_checks_kibisis_support_and_records_config(tmp_path: Path) -> None:
    with patch("telarch_evals.memory.kibisis_supports_word_forms", return_value=False):
        with pytest.raises(ValueError, match="--word-forms needs kibisis"):
            TelarchMemory(tmp_path / "wf_no", encrypt=False, word_forms=True)

    with patch("telarch_evals.memory.kibisis_supports_word_forms", return_value=True):
        mem = TelarchMemory(tmp_path / "wf_yes", encrypt=False, word_forms=True)
        assert mem.config()["word_forms"] is True
