"""Run the pipeline under a load shape and record one row per request.

  .venv\\Scripts\\python.exe -X utf8 scripts\\run_load.py --scenario steady --source standin
  .venv\\Scripts\\python.exe -X utf8 scripts\\run_load.py --scenario real_steady --source real

Scenarios (arrival process; all use Poisson arrivals unless noted):
  steady            8/min for 120 s                   (about what the 3 model buckets sustain)
  burst             2/min background + 40 arrivals in 5 s at t=20 s ("something broke for everyone")
  overload          30/min for 120 s                  (~3x sustainable)
  rpm_halved        steady, but the stand-in's per-minute limits (RPM and TPM) are halved, and the client isn't told
  chaos             steady + 5% provider 503s + 5% 8-second stalls + 5% failed/slow search-tool calls
  daily_exhausted   steady, with the stand-in's daily request budget emptied
  real_steady       4/min for 180 s against real Groq (budget: ~12 questions x <=3 calls)
  real_burst        10 arrivals at once against real Groq
  quality           every question once, sequentially, against real Groq (for grading)

Each person gives up if no answer text arrives within their patience: lognormal, median 15 s, p90 ~30 s.
Rows are appended to results.csv (the brief's columns) and data/results/detail.jsonl (spans, mode, model).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.config import DATA, GROQ_BASE_URL, ROOT, STANDIN_BASE_URL, cost_usd, groq_key  # noqa: E402
from relay.grade import grade, load_questions  # noqa: E402
from relay.limiter import Budgets, ModelLimits  # noqa: E402
from relay.llm import ChatClient  # noqa: E402
from relay.pipeline import Pipeline, PipelineConfig, RetryBudget  # noqa: E402

CSV_COLS = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status", "retries",
            "tokens_in", "tokens_out", "retrieval_setting", "answer_grade"]
# Measured by the probe on this account (2026-11-09): identical for all three models,
# plus qwen's hidden ITPM of 7,000 (learned at runtime from 429 bodies).
LIMITS = {m: ModelLimits(rpm=30, tpm=8000, rpd=1000, tpd=200000)
          for m in ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b")}

SCENARIOS = {
    "steady": dict(kind="poisson", rate_per_min=8, duration_s=120),
    "burst": dict(kind="burst", rate_per_min=2, duration_s=90, burst_at_s=20, burst_n=40, burst_over_s=5),
    "overload": dict(kind="poisson", rate_per_min=30, duration_s=120),
    "rpm_halved": dict(kind="poisson", rate_per_min=8, duration_s=120, admin={"rpm_scale": 0.5, "tpm_scale": 0.5}),
    # Not in the probe: Groq showed no errors or stalls in 119 calls. These rates are assumptions to exercise the paths.
    "chaos": dict(kind="poisson", rate_per_min=8, duration_s=120,
                  admin={"error_rate": 0.05, "stall_rate": 0.05, "stall_s": 8.0},
                  pipeline={"tool_fail_rate": 0.05, "tool_slow_rate": 0.05}),
    "daily_exhausted": dict(kind="poisson", rate_per_min=8, duration_s=60, admin={"exhaust_daily": True}),
    "real_steady": dict(kind="poisson", rate_per_min=4, duration_s=180),
    "real_burst": dict(kind="burst", rate_per_min=0, duration_s=40, burst_at_s=0, burst_n=10, burst_over_s=0.5),
    "quality": dict(kind="sequential"),
}


def arrivals(spec, rng):
    if spec["kind"] == "poisson":
        t, out = 0.0, []
        while True:
            t += rng.expovariate(spec["rate_per_min"] / 60)
            if t > spec["duration_s"]:
                return out
            out.append(t)
    if spec["kind"] == "burst":
        out = arrivals(dict(kind="poisson", rate_per_min=spec["rate_per_min"], duration_s=spec["duration_s"]), rng) \
            if spec["rate_per_min"] else []
        out += [spec["burst_at_s"] + rng.uniform(0, spec["burst_over_s"]) for _ in range(spec["burst_n"])]
        return sorted(out)
    raise ValueError(spec["kind"])


class Admission:
    """Global gate: at most `max_active` pipelines run at once. The rest queue, and the queue is measured."""

    def __init__(self, max_active):
        self.sem = asyncio.Semaphore(max_active)
        self.waiting = 0
        self.peak = 0

    async def __aenter__(self):
        self.waiting += 1
        self.peak = max(self.peak, self.waiting)
        await self.sem.acquire()
        self.waiting -= 1

    async def __aexit__(self, *a):
        self.sem.release()


async def start_standin(port=8765):
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "standin.server", "--port", str(port)], cwd=ROOT)
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                await c.get(f"http://127.0.0.1:{port}/admin/state")
                return proc
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
    proc.kill()
    raise SystemExit("stand-in did not start")


async def run(args):
    spec = SCENARIOS[args.scenario]
    rng = random.Random(args.seed)
    questions = load_questions()
    real = args.source == "real"
    proc = None
    if real:
        client = ChatClient(GROQ_BASE_URL, groq_key())
    else:
        proc = await start_standin(args.port)
        client = ChatClient(STANDIN_BASE_URL.replace(":8765", f":{args.port}"))
    budgets = Budgets(LIMITS)
    pcfg = PipelineConfig(setting=args.setting, verify=args.verify, **spec.get("pipeline", {}))
    for kv in args.set or []:  # e.g. --set max_wait_s=5 --set prefer_wait_s=60
        k, v = kv.split("=", 1)
        cur = getattr(pcfg, k)
        setattr(pcfg, k, type(cur)(v) if not isinstance(cur, bool) else v.lower() in ("1", "true", "yes"))
    rbudget = RetryBudget(ratio=args.retry_ratio)
    if args.naive:
        pcfg.naive, pcfg.max_attempts = True, 4
        rbudget = RetryBudget(ratio=1.0, initial=1e9)
    pipe = Pipeline(client, budgets, pcfg, rbudget, cost_fn=cost_usd)
    label = args.label or args.scenario
    gate = Admission(args.max_active)
    run_tag = dt.datetime.now().strftime("%H%M%S")
    rows, details = [], []

    if spec.get("admin") and not real:
        async with httpx.AsyncClient() as c:
            await c.post(f"http://127.0.0.1:{args.port}/admin/config", json=spec["admin"])

    async def one(i, at_s, q):
        await asyncio.sleep(at_s)
        rid = f"{label}-{run_tag}-{i:04d}"
        start = dt.datetime.now(dt.timezone.utc).isoformat()
        patience = min(60.0, rng.lognormvariate(math.log(args.patience_median_s), 0.5))
        t0 = time.perf_counter()
        # The service only knows its own deadline (an SLO), never this person's hidden patience.
        deadline = time.monotonic() + args.service_deadline_s
        queue_ms = 0.0

        async def served():
            nonlocal queue_ms
            async with gate:
                queue_ms = (time.perf_counter() - t0) * 1000
                return await pipe.answer(rid, q["question"], deadline)
        try:
            out = await asyncio.wait_for(served(), timeout=patience)  # the person gives up at `patience`
            total = (time.perf_counter() - t0) * 1000
            ft = queue_ms + out.first_token_ms if out.first_token_ms is not None else None
            status = out.status
            if ft is not None and ft > patience * 1000:
                status = "timeout"  # the answer came, but the person had already given up
        except asyncio.TimeoutError:
            out, total, ft, status = None, (time.perf_counter() - t0) * 1000, None, "timeout"
        g = grade(q, out.mode, out.text, status) if (out and real) else ""
        rows.append({
            "request_id": rid, "scenario": label, "source": "real" if real else "stand-in", "start_time": start,
            "first_token_ms": round(ft, 1) if ft is not None and status != "timeout" else "",
            "total_ms": round(total, 1), "status": status, "retries": out.retries if out else 0,
            "tokens_in": out.tokens_in if out else 0, "tokens_out": out.tokens_out if out else 0,
            "retrieval_setting": pipe.setting.label(), "answer_grade": g,
        })
        details.append({
            "request_id": rid, "question_id": q["id"], "patience_s": round(patience, 2), "queue_ms": round(queue_ms, 1),
            "mode": out.mode if out else "gave_up", "draft_model": out.draft_model if out else "",
            "llm_calls": out.llm_calls if out else 0, "cost_usd": out.cost_usd if out else 0, "error": out.error if out else "",
            "spans": [s.__dict__ for s in out.spans] if out else [], "text": out.text if out else "",
        })
        print(f"{rid} {status:<12} {(out.mode if out else 'gave_up'):<14} ft={rows[-1]['first_token_ms']!s:<8} "
              f"total={total:7.0f} calls={out.llm_calls if out else 0} grade={g}", flush=True)

    t_start = time.perf_counter()
    if spec["kind"] == "sequential":
        for i, q in enumerate(questions):
            await one(i, 0, q)
    else:
        ats = arrivals(spec, rng)
        await asyncio.gather(*(one(i, at, rng.choice(questions)) for i, at in enumerate(ats)))
    wall = time.perf_counter() - t_start

    await client.aclose()
    if proc:
        async with httpx.AsyncClient() as c:
            state = (await c.get(f"http://127.0.0.1:{args.port}/admin/state")).json()
        print("stand-in:", state["stats"])
        proc.terminate()
        proc.wait(timeout=10)  # free the port before the next run starts its own stand-in

    out_csv = ROOT / args.out
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    new = not out_csv.exists()
    with out_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if new:
            w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["start_time"]))
    # Experiments written to another CSV keep their detail files apart from the submission's.
    side = DATA / "results" if args.out == "results.csv" else out_csv.parent
    stem = "" if args.out == "results.csv" else out_csv.stem + "_"
    side.mkdir(parents=True, exist_ok=True)
    with (side / f"{stem}detail.jsonl").open("a", encoding="utf-8") as f:
        for d in details:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with (side / f"{stem}runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"scenario": label, "base_scenario": args.scenario, "naive": args.naive,
                            "source": args.source, "run_tag": run_tag, "requests": len(rows),
                            "wall_s": round(wall, 1), "spec": spec, "peak_queue": gate.peak,
                            "setting": pipe.setting.label(), "max_active": args.max_active,
                            "pipeline_config": pcfg.__dict__, "service_deadline_s": args.service_deadline_s,
                            "patience_median_s": args.patience_median_s}) + "\n")
    print(f"{len(rows)} requests in {wall:.0f}s, peak queue {gate.peak}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=list(SCENARIOS))
    ap.add_argument("--source", choices=["real", "standin"], default="standin")
    ap.add_argument("--setting", default="small")
    ap.add_argument("--verify", default="auto")
    ap.add_argument("--max-active", type=int, default=12)
    ap.add_argument("--retry-ratio", type=float, default=0.1)
    ap.add_argument("--patience-median-s", type=float, default=15.0)
    ap.add_argument("--service-deadline-s", type=float, default=15.0)
    ap.add_argument("--label", help="scenario name written to results.csv (default: --scenario)")
    ap.add_argument("--naive", action="store_true", help="baseline client: no limiter, retry 429s on the same model")
    ap.add_argument("--set", action="append", help="override a PipelineConfig field, e.g. --set max_wait_s=5")
    ap.add_argument("--port", type=int, default=8765, help="stand-in port (lets stand-in runs go in parallel)")
    ap.add_argument("--out", default="results.csv", help="CSV to append to (experiments can go to a separate file)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.scenario.startswith("real") or a.scenario == "quality":
        a.source = "real"
    asyncio.run(run(a))
