"""Gateway-reported spend (Nous Portal / OpenRouter-style ``usage.cost``).

The harness's price table only knows OpenAI model ids. Routed through a gateway that reports the cost of
each request in ``usage.cost`` (the Nous Portal inference API does, in USD, promos applied), the meter
must count that figure: it is the bill, and ``--max-usd`` has to see it. A model with no table price and
no reported cost stays "unpriced" so the summary says so instead of showing $0.
"""
from __future__ import annotations

import json
from pathlib import Path

from telarch_evals.llm import Cache, ChatClient, Meter


def _client(cache: Cache, meter: Meter, model: str, usage: dict) -> ChatClient:
    c = ChatClient(model, api_key_env="TELARCH_TEST_KEY", cache=cache, meter=meter)
    c._post = lambda body: {"model": model, "choices": [{"message": {"content": "READY"}}], "usage": dict(usage)}
    return c


def test_reported_cost_is_metered_and_counts_toward_the_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    meter = Meter()
    meter.track(tmp_path / "spend.json")
    nous = _client(Cache(tmp_path / "c.sqlite"), meter, "inception/mercury-2.5",
                   {"prompt_tokens": 6, "completion_tokens": 225, "cost": 3.399e-05,
                    "completion_tokens_details": {"reasoning_tokens": 220}})
    nous.complete("q1")
    nous.complete("q2")
    entry = meter.by_model["inception/mercury-2.5"]
    assert entry["requests"] == 2 and entry["completion_tokens"] == 450
    assert abs(entry["reported_usd"] - 2 * 3.399e-05) < 1e-12
    assert abs(meter.usd() - 2 * 3.399e-05) < 1e-12
    snap = meter.snapshot()
    assert snap["unpriced_models"] == []
    assert snap["reported_usd"] == round(2 * 3.399e-05, 6)
    # a cache hit bills nothing
    nous.complete("q1")
    assert meter.by_model["inception/mercury-2.5"]["cache_hits"] == 1 and abs(meter.usd() - 2 * 3.399e-05) < 1e-12
    # the figures survive a resumed run (spend.json round-trip)
    again = Meter()
    again.track(tmp_path / "spend.json")
    assert abs(again.usd() - 2 * 3.399e-05) < 1e-12
    assert json.loads((tmp_path / "spend.json").read_text())["by_model"]["inception/mercury-2.5"]["reported_usd"] > 0


def test_table_price_and_reported_cost_do_not_double_count(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    meter = Meter()
    # gpt-4.1-mini is in the table (0.40 / 1.60 per 1M); the gateway also reports a (promo) cost. The bill wins.
    c = _client(Cache(tmp_path / "c.sqlite"), meter, "gpt-4.1-mini",
                {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cost": 0.20})
    c.complete("q")
    assert abs(meter.usd() - 0.20) < 1e-12
    assert meter.snapshot()["priced_by"]["gpt-4.1-mini"] == "reported"


def test_free_models_report_zero_and_are_not_unpriced(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    meter = Meter()
    _client(Cache(tmp_path / "c.sqlite"), meter, "upstage/solar-mini4:free",
            {"prompt_tokens": 20, "completion_tokens": 3, "cost": 0}).complete("q")
    snap = meter.snapshot()
    assert meter.usd() == 0.0 and snap["unpriced_models"] == [] and snap["priced_by"]["upstage/solar-mini4:free"] == "reported"


def test_no_table_price_and_no_reported_cost_stays_unpriced(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    meter = Meter()
    _client(Cache(tmp_path / "c.sqlite"), meter, "vendor/unknown", {"prompt_tokens": 20, "completion_tokens": 3}).complete("q")
    snap = meter.snapshot()
    assert snap["unpriced_models"] == ["vendor/unknown"] and "vendor/unknown" not in snap["priced_by"]
    # a malformed cost is ignored, not counted
    _client(Cache(tmp_path / "d.sqlite"), meter, "vendor/odd", {"prompt_tokens": 1, "completion_tokens": 1, "cost": "n/a"}).complete("q")
    assert meter.usd() == 0.0 and "vendor/odd" in meter.snapshot()["unpriced_models"]
