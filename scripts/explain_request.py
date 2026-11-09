"""Split one request's latency into waiting, working and retrying, from its recorded spans (heading 4).

  .venv\\Scripts\\python.exe -X utf8 scripts\\explain_request.py                  # slowest real request that got a reply
  .venv\\Scripts\\python.exe -X utf8 scripts\\explain_request.py <request_id>

How we know: every model call and every wait for rate-limit budget is timed by the pipeline and saved as a span
(step, start, end, kind, model) in data/results/detail.jsonl. Time spent in the admission queue is saved as
queue_ms. The first token time comes from results.csv. Anything not covered by a span is local work
(retrieval, checks, rendering) plus scheduling overhead.
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    rows = {r["request_id"]: r for r in csv.DictReader((ROOT / "results.csv").open(encoding="utf-8"))}
    det = {d["request_id"]: d for d in map(json.loads, (ROOT / "data" / "results" / "detail.jsonl").open(encoding="utf-8"))}
    if len(sys.argv) > 1:
        rid = sys.argv[1]
    else:
        cands = [r for r in rows.values() if r["source"] == "real" and r["first_token_ms"]
                 and not r["scenario"].startswith("quality")]
        # Prefer the slowest real request that waited for budget or retried: that's where the time question is interesting.
        eventful = [r for r in cands if any(s["kind"] in ("wait", "retry") for s in det[r["request_id"]]["spans"])]
        rid = max(eventful or cands, key=lambda r: float(r["first_token_ms"]))["request_id"]
    r, d = rows[rid], det[rid]
    q = d["queue_ms"]
    ft = float(r["first_token_ms"]) if r["first_token_ms"] else None
    total = float(r["total_ms"])
    print(f"request {rid}  scenario={r['scenario']} source={r['source']} status={r['status']} mode={d['mode']} "
          f"draft_model={d['draft_model']} retries={r['retries']}")
    print(f"first token {ft} ms, total {total} ms, admission queue {q} ms\n")
    print(f"{'step':<11}{'kind':<7}{'start':>8}{'end':>8}{'ms':>7}  model / detail")
    sums = {"wait": q, "work": 0.0, "retry": 0.0}
    print(f"{'queue':<11}{'wait':<7}{0:>8.0f}{q:>8.0f}{q:>7.0f}  admission queue (max 12 pipelines at once)")
    before_ft = after_ft = 0.0
    for s in d["spans"]:
        a, b = s["start_ms"] + q, s["end_ms"] + q
        dur = b - a
        sums[s["kind"]] += dur
        if ft is None or b <= ft + 1.0:  # 1 ms tolerance for rounding in the CSV
            before_ft += dur
        else:
            after_ft += dur
        print(f"{s['step']:<11}{s['kind']:<7}{a:>8.0f}{b:>8.0f}{dur:>7.0f}  {s['model']} {s['detail']}")
    covered = sum(sums.values())
    print(f"\nwaiting  {sums['wait']:8.0f} ms  ({100 * sums['wait'] / total:.0f}%)  admission queue + rate-limit budget waits")
    print(f"working  {sums['work']:8.0f} ms  ({100 * sums['work'] / total:.0f}%)  model calls that succeeded")
    print(f"retrying {sums['retry']:8.0f} ms  ({100 * sums['retry'] / total:.0f}%)  failed attempts (429, timeout, error, empty)")
    print(f"other    {total - covered:8.0f} ms  ({100 * (total - covered) / total:.0f}%)  retrieval, checks, rendering, scheduling")
    if ft is not None:
        print(f"\nBefore the first token: {q + before_ft:.0f} ms of spans. After it: {after_ft:.0f} ms. "
              f"Answers are held until the citation check passes, so all model time lands before the first token.")


if __name__ == "__main__":
    main()
