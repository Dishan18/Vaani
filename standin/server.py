"""Stand-in model service: an OpenAI-compatible streaming endpoint we control.

It copies what the probe measured on Groq:
  * per-model continuously refilling buckets for RPM, TPM, RPD, TPD and an optional ITPM
  * the same x-ratelimit-* headers, and 429 bodies in Groq's exact format
    ("Rate limit reached ... on tokens per minute (TPM): Limit 8000, Used ..., Requested ...")
  * retry-after computed for the size of *this* request, as Groq's is
  * time to first byte drawn from a lognormal fitted to the probe's p50/p95 per model
  * output streamed in chunks at the measured tokens/s
And it adds what a free tier can't be pushed into on demand:
  * slower under load: TTFB is multiplied by (1 + load_alpha * max(0, in_flight - load_knee))
  * failures: error_rate -> HTTP 503; stall_rate -> waits stall_s before the first byte, then succeeds slowly
  * runtime control: POST /admin/config {"rpm_scale": 0.5} halves RPM; {"exhaust_daily": true} empties RPD

Run:  .venv\\Scripts\\python.exe -X utf8 -m standin.server [--port 8765]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
CALIB = ROOT / "data" / "probe" / "calibration.json"

DEFAULT_MODEL = {
    "ttfb_p50_ms": 450, "ttfb_p95_ms": 900, "tokens_per_s": 450, "chunk_tokens": 3,
    "rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000, "itpm": None,
}
DEFAULTS = {
    "models": {
        "openai/gpt-oss-20b": dict(DEFAULT_MODEL),
        "openai/gpt-oss-120b": dict(DEFAULT_MODEL, ttfb_p50_ms=650, ttfb_p95_ms=1100, tokens_per_s=500),
        "qwen/qwen3.8-27b": dict(DEFAULT_MODEL, ttfb_p50_ms=330, ttfb_p95_ms=900, itpm=7000),
    },
    "error_rate": 0.0, "stall_rate": 0.0, "stall_s": 8.0,
    "load_alpha": 0.04, "load_knee": 4, "rpm_scale": 1.0, "tpm_scale": 1.0,
}


class Bucket:
    def __init__(self, cap, per_s):
        self.cap, self.rate, self.level, self.t = cap, cap / per_s, cap, time.monotonic()

    def refill(self):
        now = time.monotonic()
        self.level = min(self.cap, self.level + (now - self.t) * self.rate)
        self.t = now
        return self.level

    def reset_s(self):  # time until full, as Groq's x-ratelimit-reset-* reports it
        return (self.cap - self.refill()) / self.rate

    def need_s(self, amount):
        return max(0.0, (amount - self.refill()) / self.rate)


class ModelState:
    def __init__(self, name, c, scale_rpm=1.0, scale_tpm=1.0):
        self.name, self.c = name, c
        self.rpm = Bucket(c["rpm"] * scale_rpm, 60)
        self.tpm = Bucket(c["tpm"] * scale_tpm, 60)
        self.rpd = Bucket(c["rpd"], 86400)
        self.tpd = Bucket(c["tpd"], 86400)
        self.itpm = Bucket(c["itpm"] * scale_tpm, 60) if c.get("itpm") else None


def fmt_dur(s):
    if s < 1:
        return f"{round(s * 1000)}ms"
    m, sec = divmod(s, 60)
    return f"{int(m)}m{sec:.1f}s" if m else f"{sec:.3f}s"


class StandIn:
    def __init__(self, cfg):
        self.cfg = cfg
        self.in_flight = 0
        self.stats = {"requests": 0, "ok": 0, "429": 0, "503": 0, "stalled": 0, "peak_in_flight": 0}
        self.models = {}
        self.rebuild()

    def rebuild(self):
        self.models = {m: ModelState(m, c, self.cfg["rpm_scale"], self.cfg["tpm_scale"])
                       for m, c in self.cfg["models"].items()}

    def headers(self, st: ModelState, rpm_hit=False):
        h = {
            "x-ratelimit-limit-requests": str(int(st.rpd.cap)),
            "x-ratelimit-remaining-requests": str(int(st.rpd.refill())),
            "x-ratelimit-reset-requests": fmt_dur(st.rpd.reset_s()),
            "x-request-id": "req_standin_" + uuid.uuid4().hex[:12],
        }
        if not rpm_hit:  # Groq omits the token headers on an RPM 429 (probe phase C)
            h.update({
                "x-ratelimit-limit-tokens": str(int(st.tpm.cap)),
                "x-ratelimit-remaining-tokens": str(int(st.tpm.refill())),
                "x-ratelimit-reset-tokens": fmt_dur(st.tpm.reset_s()),
            })
        return h

    def too_many(self, st, label, code_type, limit, used, requested, wait_s, rpm_hit=False):
        self.stats["429"] += 1
        body = {"error": {
            "message": f"Rate limit reached for model `{st.name}` in organization `org_standin` service tier `on_demand` "
                       f"on {label}: Limit {int(limit)}, Used {int(used)}, Requested {int(requested)}. "
                       f"Please try again in {wait_s:.3f}s.",
            "type": code_type, "code": "rate_limit_exceeded"}}
        h = self.headers(st, rpm_hit)
        h["retry-after"] = str(max(1, math.ceil(wait_s)))
        return web.json_response(body, status=429, headers=h)

    async def chat(self, request: web.Request):
        self.stats["requests"] += 1
        req = await request.json()
        model = req.get("model")
        st = self.models.get(model)
        if not st:
            return web.json_response({"error": {"message": f"model {model} not found", "type": "invalid_request_error"}}, status=404)
        msgs = req.get("messages", [])
        tin = max(1, int(sum(len(m.get("content", "")) for m in msgs) / 3.2))  # probe: 3.1-3.3 chars per token
        max_tokens = int(req.get("max_tokens", 400))
        reply = make_reply(msgs, json_mode=bool(req.get("response_format")))
        tout = min(max_tokens, max(1, int(len(reply) / 3.2)))
        charge = tin + max_tokens  # probe 429 bodies: Groq's "Requested" = prompt tokens + max_tokens

        # Admission, in the order Groq appears to check (RPD, RPM, ITPM, TPM)
        if st.rpd.refill() < 1:
            return self.too_many(st, "requests per day (RPD)", "requests", st.rpd.cap, st.rpd.cap, 1, st.rpd.need_s(1))
        if st.rpm.refill() < 1:
            return self.too_many(st, "requests per minute (RPM)", "requests", st.rpm.cap, st.rpm.cap, 1, st.rpm.need_s(1), rpm_hit=True)
        if st.itpm and st.itpm.refill() < tin:
            return self.too_many(st, "input tokens per minute (ITPM)", "tokens", st.itpm.cap,
                                 st.itpm.cap - st.itpm.level, tin, st.itpm.need_s(tin))
        if st.tpm.refill() < charge:
            return self.too_many(st, "tokens per minute (TPM)", "tokens", st.tpm.cap,
                                 st.tpm.cap - st.tpm.level, charge, st.tpm.need_s(charge))
        if st.tpd.refill() < charge:
            return self.too_many(st, "tokens per day (TPD)", "tokens", st.tpd.cap, st.tpd.cap, charge, st.tpd.need_s(charge))
        st.rpd.level -= 1; st.rpm.level -= 1; st.tpm.level -= charge; st.tpd.level -= charge
        if st.itpm:
            st.itpm.level -= tin

        if random.random() < self.cfg["error_rate"]:
            self.stats["503"] += 1
            await asyncio.sleep(random.uniform(0.05, 0.3))
            return web.json_response({"error": {"message": "Service Unavailable", "type": "internal_server_error"}}, status=503)

        self.in_flight += 1
        self.stats["peak_in_flight"] = max(self.stats["peak_in_flight"], self.in_flight)
        try:
            c = st.c
            mu = math.log(c["ttfb_p50_ms"])
            sigma = max(0.05, (math.log(c["ttfb_p95_ms"]) - mu) / 1.645)
            ttfb = random.lognormvariate(mu, sigma)
            ttfb *= 1 + self.cfg["load_alpha"] * max(0, self.in_flight - self.cfg["load_knee"])
            if random.random() < self.cfg["stall_rate"]:
                self.stats["stalled"] += 1
                ttfb += self.cfg["stall_s"] * 1000
            h = self.headers(st)
            resp = web.StreamResponse(status=200, headers={**h, "Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await asyncio.sleep(ttfb / 1000)
            cid = "chatcmpl-standin-" + uuid.uuid4().hex[:10]
            words = re.findall(r"\S+\s*", reply)
            step = max(1, c["chunk_tokens"])
            gap = step / c["tokens_per_s"]
            for i in range(0, len(words), step):
                piece = "".join(words[i:i + step])
                await resp.write(sse({"id": cid, "object": "chat.completion.chunk", "model": model,
                                      "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}))
                await asyncio.sleep(gap)
            # Settle the TPM charge to actual output, like a real provider would
            st.tpm.level += max_tokens - tout
            await resp.write(sse({"id": cid, "object": "chat.completion.chunk", "model": model,
                                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                  "x_groq": {"usage": {"prompt_tokens": tin, "completion_tokens": tout,
                                                       "total_tokens": tin + tout}}}))
            await resp.write(b"data: [DONE]\n\n")
            self.stats["ok"] += 1
            return resp
        except (ConnectionResetError, asyncio.CancelledError, OSError):
            self.stats["client_gone"] = self.stats.get("client_gone", 0) + 1  # the caller hung up mid-stream
            raise
        finally:
            self.in_flight -= 1

    async def admin_config(self, request):
        upd = await request.json()
        for k in ("error_rate", "stall_rate", "stall_s", "load_alpha", "load_knee", "rpm_scale", "tpm_scale"):
            if k in upd:
                self.cfg[k] = upd[k]
        if upd.get("reset", False) or "rpm_scale" in upd or "tpm_scale" in upd:
            self.rebuild()
        if upd.get("exhaust_daily"):
            for st in self.models.values():
                st.rpd.level = 0.0
        return web.json_response({"ok": True, "cfg": {k: v for k, v in self.cfg.items() if k != "models"}})

    async def admin_state(self, request):
        return web.json_response({"in_flight": self.in_flight, "stats": self.stats,
                                  "models": {m: {"rpm": s.rpm.refill(), "tpm": s.tpm.refill(), "rpd": s.rpd.refill()}
                                             for m, s in self.models.items()}})


def sse(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def make_reply(msgs, json_mode):
    """Plausible content so the pipeline's parsing and citation gate are exercised for real."""
    sys_ = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    if "route questions" in sys_:
        q = user.strip()
        return json.dumps({"papers": [], "query": q, "alt_query": q})
    if "Pick the" in sys_:
        return json.dumps({"best": [1, 2, 3, 4]})
    if "Check whether every claim" in sys_:
        return json.dumps({"supported": True, "unsupported": []})
    m = re.search(r"\[S1\] \([^)]*\)\n(.+?)(?:\n\n\[S2\]|\n\nQuestion:)", user, re.S)
    if m:
        sents = [s for s in re.split(r"(?<=[.!?])\s+", m.group(1).strip()) if len(s) > 20][:3]
        return " ".join(f"{s.rstrip('.')} [S1]." for s in sents)[:900] or "NOT_IN_PAPERS: nothing relevant."
    return "NOT_IN_PAPERS: the sources provided do not cover this."


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CALIB.exists():
        cal = json.loads(CALIB.read_text(encoding="utf-8"))
        for m, c in cal.get("models", {}).items():
            cfg["models"].setdefault(m, dict(DEFAULT_MODEL)).update({k: v for k, v in c.items() if v is not None})
        for k in ("load_alpha", "load_knee"):
            if k in cal:
                cfg[k] = cal[k]
    return cfg


def make_app(cfg=None):
    s = StandIn(cfg or load_cfg())
    app = web.Application()
    app.router.add_post("/openai/v1/chat/completions", s.chat)
    app.router.add_post("/admin/config", s.admin_config)
    app.router.add_get("/admin/state", s.admin_state)
    app["standin"] = s
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    web.run_app(make_app(), host="127.0.0.1", port=a.port, print=lambda *_: print(f"stand-in on :{a.port}", flush=True))
