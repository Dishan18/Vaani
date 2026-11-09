"""Budgeted probe of the real Groq free tier. See docs/probe_plan.md.

Usage:  .venv\\Scripts\\python.exe -X utf8 scripts\\probe_groq.py [--phases A,C,D,E,B]
Appends to data/probe/probe_raw.jsonl (never overwrites earlier runs).
"""
import argparse
import asyncio
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.config import DATA, GROQ_BASE_URL, groq_key  # noqa: E402
from relay.llm import ChatClient, LLMError  # noqa: E402

M20, M120, MQ = "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"
MAX_REQUESTS = 220
OUT = DATA / "probe" / "probe_raw.jsonl"

sent = 0
run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
t_run0 = time.perf_counter()
blocks = [json.loads(l) for l in (DATA / "blocks.jsonl").open(encoding="utf-8")]
rng = random.Random(7)


def context_of(chars):
    """Contiguous real paper text of about `chars` characters (~4 chars/token)."""
    i = rng.randrange(len(blocks) - 200)
    out, n = [], 0
    while n < chars:
        b = blocks[i]
        out.append(f"[{b['paper']} p{b['page']}] {b['text']}")
        n += len(b["text"])
        i += 1
    return "\n".join(out)


def tiny_msgs():
    return [{"role": "user", "content": f"Reply with the single word OK. ({rng.random():.6f})"}]


def router_msgs():
    q = rng.choice(["What optimizer settings were used?", "How is the chunk boundary chosen?",
                    "What dataset was used for speech pretraining?", "Why divide by sqrt(dk)?"])
    return [{"role": "system", "content": "Classify the question. Return JSON {\"papers\": [...], \"query\": str, \"answerable\": bool}. "
             "Papers: attention (Transformer, 2017), moshi (speech-text dialogue model), dynamic_chunking (H-Net)."},
            {"role": "user", "content": q + " " + context_of(600)}]


def draft_msgs(ctx_chars=7000):
    return [{"role": "system", "content": "Answer only from the passages. Cite as [paper pPAGE]. If the passages do not say, say so."},
            {"role": "user", "content": f"Passages:\n{context_of(ctx_chars)}\n\nQuestion: Summarise the main claim of these passages in 4 sentences."}]


def log(row):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


async def call(client, phase, model, msgs, max_tokens, *, keep_chunks=False, note="", conc=1, seq=0):
    global sent
    if sent >= MAX_REQUESTS:
        raise SystemExit("probe budget exhausted (MAX_REQUESTS)")
    sent += 1
    start_wall = dt.datetime.now(dt.timezone.utc).isoformat()
    offset = time.perf_counter() - t_run0
    kind = "ok"
    try:
        r = await client.chat(model, msgs, max_tokens=max_tokens, timeout_s=60)
    except LLMError as e:
        r, kind = e.result, e.kind
        if r is None:
            from relay.llm import CallResult
            r = CallResult(model=model)
            r.error_body = str(e)
    row = {
        "run_id": run_id, "phase": phase, "model": model, "note": note, "concurrency": conc, "seq": seq,
        "start_time": start_wall, "offset_s": round(offset, 3), "outcome": kind, "status": r.status,
        "first_byte_ms": r.first_byte_ms, "first_token_ms": r.first_token_ms, "total_ms": round(r.total_ms, 1),
        "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "reasoning_tokens": r.reasoning_tokens,
        "max_tokens": max_tokens, "prompt_chars": sum(len(m["content"]) for m in msgs),
        "headers": r.headers, "error_body": r.error_body[:1500], "text_head": r.text[:120],
    }
    if keep_chunks:
        row["chunk_times_ms"] = r.chunk_times_ms
    log(row)
    tag = f"{phase:<12}{model.split('/')[-1]:<14}{kind:<13}{r.status} ttft={r.first_token_ms and round(r.first_token_ms)} total={round(r.total_ms)} in={r.tokens_in} out={r.tokens_out}"
    rem = r.headers.get("x-ratelimit-remaining-tokens"), r.headers.get("x-ratelimit-remaining-requests"), r.headers.get("retry-after")
    print(f"{tag}  remTPM={rem[0]} remRPD={rem[1]} retry-after={rem[2]}", flush=True)
    return row


async def phase_a(c):
    for m in (M20, M120, MQ):
        await call(c, "A_headers", m, tiny_msgs(), 16, note="max_tokens=16")
        await call(c, "A_headers", m, tiny_msgs(), 2000, note="max_tokens=2000")


async def recovery(c, phase, model, cap_s=90):
    t0 = time.perf_counter()
    i = 0
    while time.perf_counter() - t0 < cap_s:
        await asyncio.sleep(2)
        i += 1
        row = await call(c, phase + "_recovery", model, tiny_msgs(), 16, seq=i,
                         note=f"since_first_429_s={time.perf_counter() - t0:.1f}")
        if row["status"] == 200:
            print(f"   {model} recovered after {time.perf_counter() - t0:.1f}s", flush=True)
            return


async def phase_c(c):
    """RPM wall on gpt-oss-20b: tiny calls, 5 in flight."""
    hit = asyncio.Event()
    n = 0

    async def worker():
        nonlocal n
        while not hit.is_set() and n < 45:
            n += 1
            row = await call(c, "C_rpm_wall", M20, tiny_msgs(), 16, conc=5, seq=n)
            if row["status"] == 429:
                hit.set()
    await asyncio.gather(*(worker() for _ in range(5)))
    if hit.is_set():
        await recovery(c, "C_rpm_wall", M20)


async def phase_d(c):
    """TPM wall on gpt-oss-120b: ~3K-token prompts, one every 1.5 s."""
    hit = False
    tasks = []
    for i in range(8):
        tasks.append(asyncio.ensure_future(call(c, "D_tpm_wall", M120, draft_msgs(12000), 16, seq=i + 1)))
        await asyncio.sleep(1.5)
        if any(t.done() and t.result()["status"] == 429 for t in tasks):
            hit = True
            break
    rows = await asyncio.gather(*tasks)
    if hit or any(r["status"] == 429 for r in rows):
        await recovery(c, "D_tpm_wall", M120)


async def phase_e(c):
    """Concurrency on qwen: bursts of 1, 4, 8, 16 simultaneous small calls."""
    for k in (1, 4, 8, 16):
        await asyncio.gather(*(call(c, "E_concurrency", MQ, router_msgs(), 40, conc=k, seq=i + 1, keep_chunks=True)
                               for i in range(k)))
        await asyncio.sleep(3)


async def phase_b(c):
    async def per_model(m):
        for i in range(16):
            if i % 2 == 0:
                await call(c, "B_latency", m, router_msgs(), 80, keep_chunks=True, note="short", seq=i + 1)
            else:
                await call(c, "B_latency", m, draft_msgs(), 300, keep_chunks=True, note="long", seq=i + 1)
            await asyncio.sleep(12)
    await asyncio.gather(*(per_model(m) for m in (M20, M120, MQ)))


async def main(phases):
    c = ChatClient(GROQ_BASE_URL, groq_key())
    try:
        if "A" in phases:
            await phase_a(c)
        walls = [f(c) for p, f in (("C", phase_c), ("D", phase_d), ("E", phase_e)) if p in phases]
        if walls:
            await asyncio.gather(*walls)
        if "B" in phases:
            if walls:
                print("cooldown 65s before latency phase", flush=True)
                await asyncio.sleep(65)
            await phase_b(c)
    finally:
        await c.aclose()
        print(f"done: {sent} requests sent, run_id={run_id}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="A,C,D,E,B")
    asyncio.run(main(set(ap.parse_args().phases.split(","))))
