"""Summarise data/probe/probe_raw.jsonl into Table 1 evidence and data/probe/calibration.json.

Usage:  .venv\\Scripts\\python.exe -X utf8 scripts\\analyze_probe.py
"""
import json
import math
import re
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.config import DATA  # noqa: E402
from relay.limiter import parse_duration  # noqa: E402

rows = [json.loads(l) for l in (DATA / "probe" / "probe_raw.jsonl").open(encoding="utf-8")]


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


print(f"{len(rows)} probe requests, {sum(r['tokens_in'] + r['tokens_out'] for r in rows):,} tokens, "
      f"{sum(r['status'] == 429 for r in rows)} x 429\n")

print("== Limits seen in headers (per model)")
for m in sorted({r["model"] for r in rows}):
    hs = [r["headers"] for r in rows if r["model"] == m]
    print(f"  {m}: limit-requests={ {h.get('x-ratelimit-limit-requests') for h in hs} - {None} } "
          f"limit-tokens={ {h.get('x-ratelimit-limit-tokens') for h in hs} - {None} }")

print("\n== Refill evidence: reset-requests / (limit - remaining) should be 86.4 s if RPD refills continuously")
ratios = []
for r in rows:
    h = r["headers"]
    try:
        used = float(h["x-ratelimit-limit-requests"]) - float(h["x-ratelimit-remaining-requests"])
        if used > 0:
            ratios.append(parse_duration(h["x-ratelimit-reset-requests"]) / used)
    except (KeyError, TypeError, ValueError):
        pass
if ratios:
    print(f"  seconds per request of RPD refill: median {st.median(ratios):.2f}, min {min(ratios):.2f}, max {max(ratios):.2f}, n={len(ratios)}")

print("\n== 429s")
for r in [r for r in rows if r["status"] == 429]:
    m = re.search(r"on ([a-z ]+) \((\w+)\): Limit (\d+), Used (\d+), Requested (\d+)", r["error_body"])
    print(f"  {r['phase']:<16} {r['model']:<22} {m.groups() if m else r['error_body'][:80]} retry-after={r['headers'].get('retry-after')}")

print("\n== Recovery (first 200 after the first 429)")
for ph in ("C_rpm_wall_recovery", "D_tpm_wall_recovery"):
    rs = [r for r in rows if r["phase"] == ph]
    ok = [r for r in rs if r["status"] == 200]
    print(f"  {ph}: {len(rs)} polls; first 200 at {ok[0]['note'] if ok else 'never'}")

print("\n== TPM accounting (phase A): remaining-tokens drop vs actual usage")
for m in sorted({r["model"] for r in rows}):
    a = [r for r in rows if r["phase"] == "A_headers" and r["model"] == m]
    if len(a) == 2:
        drop = float(a[0]["headers"]["x-ratelimit-remaining-tokens"]) - float(a[1]["headers"]["x-ratelimit-remaining-tokens"])
        print(f"  {m}: 2nd call max_tokens=2000, used {a[1]['tokens_in'] + a[1]['tokens_out']} tokens; remaining fell by {drop:.0f} "
              f"(1st call used {a[0]['tokens_in'] + a[0]['tokens_out']}, fell {8000 - float(a[0]['headers']['x-ratelimit-remaining-tokens']):.0f})")

print("\n== Latency (phase B, real Groq, successful calls)")
calib = {"models": {}}
for m in sorted({r["model"] for r in rows}):
    b = [r for r in rows if r["phase"] == "B_latency" and r["model"] == m and r["status"] == 200]
    for kind in ("short", "long"):
        bk = [r for r in b if r["note"] == kind]
        fb = [r["first_byte_ms"] for r in bk if r["first_byte_ms"]]
        ft = [r["first_token_ms"] for r in bk if r["first_token_ms"]]
        tt = [r["total_ms"] for r in bk]
        if fb:
            print(f"  {m:<22} {kind:<5} n={len(bk):<3} first_byte p50={pct(fb,50):.0f} p95={pct(fb,95):.0f} | "
                  f"first_token p50={pct(ft,50):.0f} p95={pct(ft,95):.0f} | total p50={pct(tt,50):.0f} p95={pct(tt,95):.0f}"
                  f" | empty={sum(1 for r in bk if not r['first_token_ms'])}")
    rates = []
    for r in b:
        if r["first_token_ms"] and r["tokens_out"] > 20 and r["total_ms"] > r["first_token_ms"] + 20:
            rates.append(r["tokens_out"] / ((r["total_ms"] - r["first_token_ms"]) / 1000))
    ft_all = [r["first_token_ms"] for r in b if r["first_token_ms"]]
    reasoning = [r["reasoning_tokens"] for r in b]
    if ft_all:
        calib["models"][m] = {"ttfb_p50_ms": round(pct(ft_all, 50)), "ttfb_p95_ms": round(pct(ft_all, 95)),
                              "tokens_per_s": round(st.median(rates)) if rates else None}
        print(f"  {m:<22} output speed after first token: median {st.median(rates) if rates else float('nan'):.0f} tok/s; "
              f"reasoning tokens per call median {st.median(reasoning) if reasoning else 0}")

calib["models"].setdefault("qwen/qwen3.8-27b", {})["itpm"] = 7000
print("\n== Concurrency (phase E, qwen): first-token ms by burst size (200s only)")
e = [r for r in rows if r["phase"] == "E_concurrency"]
base = None
for k in sorted({r["concurrency"] for r in e}):
    ft = [r["first_token_ms"] for r in e if r["concurrency"] == k and r["status"] == 200 and r["first_token_ms"]]
    n429 = sum(1 for r in e if r["concurrency"] == k and r["status"] == 429)
    if ft:
        med = st.median(ft)
        base = base or med
        print(f"  burst={k:<3} ok={len(ft):<3} 429={n429:<3} median={med:.0f} max={max(ft):.0f}  (x{med / base:.2f} vs burst=1)")
calib["load_alpha"], calib["load_knee"] = 0.04, 4
(DATA / "probe" / "calibration.json").write_text(json.dumps(calib, indent=2), encoding="utf-8")
print("\nwrote data/probe/calibration.json:", json.dumps(calib))
