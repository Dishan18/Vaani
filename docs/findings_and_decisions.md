# Decision log (final configuration)

Every decision below is backed by a file in this repo. The write-up (writeup/writeup.pdf) is the summary; this is the audit trail.

| Decision | Final value | Evidence |
|---|---|---|
| Never queue past available budget; degrade instead | ladder: other model, lean, cited passages, busy | Table 2b; data/experiments/iteration0_standin.csv (first version: overload 51.6% ok, 15.6% gave up) vs results.csv (82.8% ok, 0% gave up) |
| Spread steps across models | router + re-rank: qwen3.8-27b then gpt-oss-20b; draft: gpt-oss-120b then qwen then 20b; verify: gpt-oss-20b | limits are per model (probe 429 bodies name the model); router on 20b: median 569 ms, 5.2% empty replies; on qwen: 220 ms, no empties (detail spans) |
| Limiter estimates Groq's admission charge | prompt chars / 3.1 + 20 + max_tokens, settled to actual usage afterwards | probe 429 bodies: 13,208 chars with max_tokens 16 = "Requested 4319"; before the fix 16 of 72 Table 3 drafts got a surprise 429, after it 0 of 18 |
| Longest wait for model budget | 3 s (`max_wait_s`) | data/experiments/wait_cap.csv: two seeds; success about equal at 3, 6 and 15 s, but 3 s had no give-ups and the lowest p95 |
| First-token timeout | 3 s | slowest healthy real first token in 345 calls: 2,240 ms; data/experiments/first_token_timeout.csv: with 20% stalls, 94.1% ok at 3 s vs 76.5% at 6 s |
| Best-effort steps (verify, re-rank) | one attempt, skipped on failure | slowest chaos request went from 8,073 ms (75% retrying) to 3,816 ms |
| Retry policy | at most 1 retry per step, on a different model's bucket, global budget 10% of attempts, none after a daily-limit 429 | overload vs overload_naive_retry in results.csv: 70 calls and one 429 vs 240 calls and 204 429s |
| Retrieval setting | small: 600-character pieces, 8 fetched, no re-rank | Table 3: 14/14 answerable correct, fewest tokens (1,679 per answer); re-rank cost 51% more tokens for no gain |
| Retrieval under pressure (lean) | 4 pieces, no router, no verifier | Table 3 small_lean: 12/14 correct at 874 tokens per answer |
| Release answers only after the checks | buffered, not streamed | Groq drafts in a median 678 ms (92 real calls), so holding the draft costs well under a second |
| Passages-only replies are not successes | status rate_limited / timeout / error, grade refused_* | make_tables.py docstring |
| Stand-in answers are not graded | answer_grade empty for stand-in rows | their text is synthetic |

## Known costs of these choices
- The LLM verifier wrongly rejected one correct real answer (real_steady q11). It runs only when there is headroom.
- A fresh process starts with full local buckets and only learns the real state from the first reply's headers. The one passages-only reply in real_burst came from this.
- Chaos rates (5% 503s, 5% 8 s stalls, 5% tool failures) and the stand-in's slowdown under load are assumptions. Groq showed none of these in the probe.
