"""Offline tests for the LongMemEval-V2 relay (`lme_v2/lmev2_relay.py`), with a fake upstream.

The relay keeps the Qwen3.5-9B runs repeatable behind a proxy that drops idle requests: each question gets its own
seed, a retry sends the identical request (so it repeats the same sample), and proxy errors are retried by the relay
instead of reaching the harness, whose client would retry with a new sample.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "lmev2_relay", Path(__file__).resolve().parents[1] / "lme_v2" / "lmev2_relay.py")
assert _spec is not None and _spec.loader is not None
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

Q1 = [{"role": "user", "content": "When did I last visit the dentist?"}]
Q2 = [{"role": "user", "content": "Which city did I move to?"}]
KEY = "Bearer test-key"


def _seed(messages) -> int:
    # The derivation the round-2 LME-V2 runs used; changing it changes their samples.
    return int(hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:8], 16)


def _request(messages, **params) -> dict:
    return {"model": "qwen3.5-9b", "messages": messages, "temperature": 0.7, "max_tokens": 64, **params}


def _chunks(text: str, reasoning: str = "") -> list[dict]:
    """A vLLM stream: role, reasoning and content deltas, finish_reason, then usage."""
    meta = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "qwen3.5-9b"}
    out = [{**meta, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    if reasoning:
        out.append({**meta, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]})
    half = len(text) // 2
    out.append({**meta, "choices": [{"index": 0, "delta": {"content": text[:half]}}]})
    out.append({**meta, "choices": [{"index": 0, "delta": {"content": text[half:]}, "finish_reason": "stop"}]})
    out.append({**meta, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    return out


def _sse(chunks: list[dict], done: bool = True) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")).encode()


def ok(text: str, reasoning: str = "") -> httpx.Response:
    return httpx.Response(200, content=_sse(_chunks(text, reasoning)), headers={"content-type": "text/event-stream"})


class _Dropped(httpx.SyncByteStream):
    """Part of an answer, then the connection closes, as the proxy did on 2026-10-21."""

    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self):
        yield self.head
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


class Upstream:
    """Answers each request with the next scripted reply and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []
        self.auth: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.auth.append(request.headers.get("authorization"))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def upstream(monkeypatch):
    def install(*replies) -> Upstream:
        fake = Upstream(replies)
        monkeypatch.setattr(relay, "UP", "http://upstream.invalid/v1")
        monkeypatch.setattr(relay, "CLIENT", httpx.Client(transport=httpx.MockTransport(fake)))
        monkeypatch.setattr(relay, "SEM", threading.BoundedSemaphore(4))
        monkeypatch.setattr(relay, "LOG", io.StringIO())
        monkeypatch.setattr(relay, "STATS", {"ok": 0, "retries": 0, "failed": 0})
        monkeypatch.setattr(relay, "RETRY_DELAY_S", 0)
        return fake

    return install


def test_each_question_gets_its_own_seed(upstream):
    fake = upstream(ok("A"), ok("B"), ok("A"), ok("C"))
    for messages in (Q1, Q2, Q1):
        assert relay.relay(_request(messages), KEY)[0] == 200
    assert relay.relay(_request(Q1, seed=7), KEY)[0] == 200
    seeds = [b["seed"] for b in fake.bodies]
    assert seeds[:3] == [_seed(Q1), _seed(Q2), _seed(Q1)]
    assert seeds[0] != seeds[1]
    assert seeds[3] == 7  # a seed the harness sets is kept
    for body, messages in zip(fake.bodies, (Q1, Q2, Q1)):  # only stream, stream_options and seed are added
        assert body == {**_request(messages), "stream": True, "stream_options": {"include_usage": True},
                        "seed": _seed(messages)}
    assert fake.auth == [KEY] * 4


def test_returns_the_assembled_completion(upstream):
    upstream(ok("The answer is 42.", reasoning="Let me think."))
    status, text = relay.relay(_request(Q1), KEY)
    out = json.loads(text)
    assert status == 200
    assert (out["object"], out["id"], out["model"]) == ("chat.completion", "chatcmpl-1", "qwen3.5-9b")
    assert out["choices"] == [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "The answer is 42.", "reasoning_content": "Let me think."}}]
    assert out["usage"]["completion_tokens"] == 5


def test_a_retry_repeats_the_same_sample(upstream):
    fake = upstream(
        httpx.Response(200, stream=_Dropped(_sse(_chunks("The answer is 42.")[:2], done=False))),
        httpx.Response(200, content=_sse(_chunks("The answer is 42.")[:2], done=False)),  # ends with no finish_reason
        httpx.ReadTimeout("timed out"),
        ok("The answer is 42."),
    )
    status, text = relay.relay(_request(Q1), KEY)
    assert status == 200
    assert json.loads(text)["choices"][0]["message"]["content"] == "The answer is 42."
    assert len(fake.bodies) == 4
    assert all(b == fake.bodies[0] for b in fake.bodies)  # same messages, sampling parameters and seed
    assert fake.bodies[0]["seed"] == _seed(Q1)
    assert relay.STATS == {"ok": 1, "retries": 3, "failed": 0}
    assert relay.LOG.getvalue().count(f"seed {_seed(Q1)} ") == 3


@pytest.mark.parametrize("status", [404, 429, 502, 503, 524, 530])
def test_proxy_errors_are_retried(upstream, status):
    fake = upstream(httpx.Response(status, text="proxy error"), ok("Paris"))
    code, text = relay.relay(_request(Q2), KEY)
    assert code == 200
    assert json.loads(text)["choices"][0]["message"]["content"] == "Paris"
    assert len(fake.bodies) == 2 and fake.bodies[0] == fake.bodies[1]


def test_other_errors_are_returned_at_once(upstream):
    fake = upstream(httpx.Response(400, text='{"error": "bad request"}'))
    assert relay.relay(_request(Q1), KEY) == (400, '{"error": "bad request"}')
    assert len(fake.bodies) == 1


def test_a_persistent_proxy_error_is_returned_after_the_last_attempt(upstream):
    fake = upstream(*[httpx.Response(524, text="timeout") for _ in range(relay.ATTEMPTS)])
    assert relay.relay(_request(Q1), KEY) == (524, "timeout")
    assert len(fake.bodies) == relay.ATTEMPTS


def test_a_persistent_drop_fails_after_the_last_attempt(upstream):
    fake = upstream(*[httpx.RemoteProtocolError("dropped") for _ in range(relay.ATTEMPTS)])
    status, text = relay.relay(_request(Q1), KEY)
    assert status == 502
    assert "RemoteProtocolError" in json.loads(text)["error"]["message"]
    assert len(fake.bodies) == relay.ATTEMPTS
    assert relay.STATS == {"ok": 0, "retries": relay.ATTEMPTS - 1, "failed": 1}


def test_the_http_endpoint(upstream):
    fake = upstream(ok("Paris"))
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(_request(Q2)).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": KEY})
        with opener.open(req, timeout=10) as r:
            out = json.loads(r.read())
        assert out["object"] == "chat.completion"
        assert out["choices"][0]["message"]["content"] == "Paris"
        assert fake.auth == [KEY]
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(urllib.request.Request(base + "/completions", data=b"{}"), timeout=10)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
