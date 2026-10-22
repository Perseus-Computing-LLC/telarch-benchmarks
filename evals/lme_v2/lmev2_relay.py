#!/usr/bin/env python3
"""Local relay between the LongMemEval-V2 harness and the Qwen3.5-9B vLLM server behind RunPod's HTTPS proxy.

The proxy (Cloudflare) drops a request that sends nothing for 100 s (HTTP 524). A non-streaming completion sends
nothing until the whole answer, thinking included, is generated, and the OpenAI client then retries, which draws a
new sample: answers that needed long thinking would be replaced by shorter ones. The relay takes the harness's
non-streaming /chat/completions request, forwards it with stream=true (at most N upstream at a time, so vLLM starts
generating as soon as a request is sent) and returns the assembled non-streaming chat.completion. Messages and
sampling parameters pass through unchanged. It adds a seed derived from the messages, so a retry after a dropped
stream repeats the same sample; every retry is logged.

Usage, from telarch-benchmarks/evals:
    python -m lme_v2.lmev2_relay <upstream_base_url> [port=18000] [max_upstream=24]
then give run_lmev2.py --reader-base-url http://127.0.0.1:<port>/v1. Retries are logged to $RELAY_LOG
(default lmev2-relay.log).
"""
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

# main() sets UP, SEM and LOG from the command line; importing the module starts nothing.
UP = ""
SEM = threading.BoundedSemaphore(24)
LOG = sys.stderr
CLIENT = httpx.Client(timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=None),
                      limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
# The proxy, not vLLM, answers 404 and 5xx while it reconnects (seen 2026-10-21 16:55Z, vLLM logged only 200s),
# so those are retried for about two minutes with the same seed.
RETRY_STATUS = {404, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530}
ATTEMPTS = 9
RETRY_DELAY_S = 15
STATS = {"ok": 0, "retries": 0, "failed": 0}
LOCK = threading.Lock()


def log(*parts):
    LOG.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + " ".join(str(p) for p in parts) + "\n")


class StreamCut(Exception):
    pass


def relay(body, auth):
    req = dict(body)
    req["stream"] = True
    req["stream_options"] = {"include_usage": True}
    if "seed" not in req:
        digest = hashlib.sha256(json.dumps(body.get("messages"), sort_keys=True).encode()).hexdigest()
        req["seed"] = int(digest[:8], 16)
    for attempt in range(ATTEMPTS):
        acc, meta, usage, finish = {}, {}, None, None
        try:
            with SEM:
                with CLIENT.stream("POST", UP + "/chat/completions", json=req,
                                   headers={"Authorization": auth, "Content-Type": "application/json"}) as r:
                    if r.status_code != 200:
                        text = r.read().decode(errors="replace")
                        if r.status_code in RETRY_STATUS and attempt < ATTEMPTS - 1:
                            raise StreamCut(f"HTTP {r.status_code}")
                        return r.status_code, text
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if not meta:
                            meta = {k: chunk.get(k) for k in ("id", "created", "model")}
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            for key, val in (choice.get("delta") or {}).items():
                                if isinstance(val, str) and key != "role":
                                    acc[key] = acc.get(key, "") + val
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
            if finish is None:
                raise StreamCut("stream ended without finish_reason")
            message = {"role": "assistant", "content": acc.pop("content", "")}
            message.update(acc)
            with LOCK:
                STATS["ok"] += 1
            return 200, json.dumps({"id": meta.get("id"), "object": "chat.completion", "created": meta.get("created"),
                                    "model": meta.get("model"), "usage": usage,
                                    "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
        except (httpx.HTTPError, json.JSONDecodeError, StreamCut) as e:
            if attempt < ATTEMPTS - 1:
                with LOCK:
                    STATS["retries"] += 1
                log("retry", attempt + 1, "seed", req["seed"], type(e).__name__, str(e)[:160], "stats", STATS)
                time.sleep(RETRY_DELAY_S)
                continue
            with LOCK:
                STATS["failed"] += 1
            log("failed seed", req["seed"], type(e).__name__, str(e)[:160], "stats", STATS)
            return 502, json.dumps({"error": {"message": f"relay: {type(e).__name__}: {str(e)[:160]}"}})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, text):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, json.dumps({"error": {"message": "the relay serves /v1/chat/completions only"}}))
        status, text = relay(body, self.headers.get("Authorization", ""))
        self._send(status, text)

    def do_GET(self):
        r = CLIENT.get(UP + self.path[len("/v1"):] if self.path.startswith("/v1") else UP + self.path,
                       headers={"Authorization": self.headers.get("Authorization", "")})
        self._send(r.status_code, r.text)

    def log_message(self, *args):
        pass


def main(argv: list[str] | None = None) -> None:
    global UP, SEM, LOG
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit(__doc__)
    UP = args[0].rstrip("/")
    port = int(args[1]) if len(args) > 1 else 18000
    SEM = threading.BoundedSemaphore(int(args[2]) if len(args) > 2 else 24)
    LOG = open(os.environ.get("RELAY_LOG", "lmev2-relay.log"), "a", buffering=1)
    log("relay start", UP, "port", port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
