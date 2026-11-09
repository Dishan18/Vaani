# Findings so far, and decisions for you to own

Built on 2026-11-09 during the first build session. **Every decision below was proposed by the AI assistant.**
The brief grades which choices are yours, so review each one, and overrule or keep it *with a reason*.
Overruled decisions are good material for "How I worked with the assistant".

## Measured on the real Groq free tier (probe: 119 requests, ~98K tokens, raw data in `data/probe/probe_raw.jsonl`)

| Finding | Evidence | Table 1 confidence |
|---|---|---|
| RPM = 30 per model | 429 body: `requests per minute (RPM): Limit 30, Used 30` after 30 tiny calls in ~4 s | high |
| TPM = 8,000 per model | `x-ratelimit-limit-tokens: 8000`; 429 body `TPM: Limit 8000` | high |
| RPD = 1,000 per model | `x-ratelimit-limit-requests: 1000` | high (not hit on purpose) |
| **RPD refills continuously: 1 request every 86.4 s** | `reset-requests / requests used = 86.40 s` on all 119 calls; there's no midnight reset | high |
| **qwen3.8-27b has a hidden ITPM = 7,000** | appears only in the 429 body (`input tokens per minute (ITPM): Limit 7000`). The headers say 8,000. | high |
| TPD = 200K (docs only) | no TPD header was observed | low |
| Limits are per model | 429 bodies name the model, and three simultaneous walls didn't interfere | medium |
| Requests in flight | no 429 or slowdown at 8 concurrent; the 16-burst hit ITPM first, so the in-flight limit is unknown | low |
| 429 format | HTTP 429, JSON `{"error":{"message","type":"requests"/"tokens","code":"rate_limit_exceeded"}}`, `retry-after` in whole seconds. An RPM 429 omits the token headers. | high |
| **Recovery: about 2 s, not a minute** | the first poll 2 s after the RPM or TPM 429 succeeded. Buckets refill continuously. | high |
| **`retry-after` depends on request size** | TPM 429 for a 4,319-token request said 21 s, yet a tiny call 2 s later succeeded | high |
| Groq doesn't get slower under concurrency | qwen median TTFT 370 ms alone vs 391 ms in a burst of 16 | medium (small n) |
| gpt-oss can return HTTP 200 with **empty content** | `max_tokens=16`: all 16 tokens went to hidden reasoning | high |
| Latency (first visible token p50/p95) | 120b 695/936 ms, 20b 659/909 ms, qwen 352/489 ms. Output speed 633 / 1522 / 486 tokens/s. | n = 16 per model |
| TPM is charged partly in advance | `max_tokens=2000` on a 101-token call dropped `remaining-tokens` by 293 (20b) / 512 (120b) | medium |

**Consequence that shapes everything:** about 2.5K tokens per answer against 8K TPM per model means roughly 3 full answers/min per model, or about 9–10/min across all three. Any burst above that *must* degrade.

## Decisions taken (review them)

1. **One decision shaped it most: never queue someone past the deadline. Degrade instead.** The ladder is: full answer (router + 120b draft + verifier) → lean (draft only) → weaker model (qwen or 20b) → passages-only reply (no LLM, still cited) → "busy, retry in N s". The limiter predicts the wait *before* calling, counting the queue ahead, so failures are fast instead of slow.
2. **Spread steps across models**, because each model has its own buckets: the router and verifier run on 20b, drafting on 120b, and qwen is overflow. *Alternative:* use one model for everything. That's simpler, but gives a third of the capacity.
3. **Answers are buffered until a deterministic citation check passes**, so nothing is streamed and then retracted. The cost is roughly 0.3–0.6 s of extra time to first token (Groq outputs 500–1500 tokens/s). *Alternative:* stream the draft and verify afterwards.
4. **"Fast" means a service deadline of 15 s.** The simulated person's patience is lognormal with a 15 s median, and the service never sees it. Both are configurable (`--service-deadline-s`, `--patience-median-s`). Pick your own numbers and justify them.
5. **Retry policy:** at most 1 retry per step. Retry on a *different* model's bucket first, never the one that just returned 429. A global retry budget caps retries at 10% of attempts. Never retry a daily-limit 429. *Worth testing:* `--retry-ratio 1.0` under `overload` as the "retrying made things worse" case.
6. **Retrieval is BM25**, with no embeddings: zero tokens, about 0.1 ms, and no extra rate-limited dependency. Pieces never cross a section boundary, so each one has a single citation. The four settings to compare are in `relay/retrieval.py` `SETTINGS`.
7. **Verifier (LLM) runs only with headroom** (`verify=auto`), since the deterministic gate always runs. Under pressure, answers skip the LLM check.
8. **A passages-only reply isn't counted as a success.** In `results.csv` its status is the cause: `rate_limited`, `timeout` or `error`. Its grade is `refused_*`, because the person got evidence but no answer. *Alternative:* count it as `ok`. That would flatter "Succeeded %".
9. **The chaos rates (5% 503s, 5% 8 s stalls, 5% tool failures) are assumptions.** Groq showed none of these in 119 calls, so they aren't calibrated.
10. **Stand-in quality is not graded.** Its text is synthetic, so `answer_grade` is left empty for stand-in rows and for requests that got no reply. Quality is graded only on real runs.

## Bugs caught during the build (candidates for "one thing it got wrong")
- The load harness first gave the pipeline each person's *hidden patience* as its deadline, which lets the system cheat. It now uses a fixed service deadline.
- The limiter waited on the preferred model's bucket even when fallback models were free, and it ignored the queue ahead of it. As a result, passage-only replies arrived at 11–14 s when they should have been instant.
- The citation gate rejected correct answers because gpt-oss-120b writes `[ S1 ]` with U+202F narrow no-break spaces. 4 of 5 answerable questions were wrongly refused until `normalize_citations` was added.

## Naive-retriever traps already seen (for that heading)
1. **Ambiguity across papers.** "What dropout rate was used?" ranks a Moshi appendix above Attention §5.4.
2. **Vocabulary mismatch.** "Mimi codec frame rate" misses §3.3, which says "12.5 Hz" but never "frame rate".
3. **Floating tables and flattened exponents.** Attention's Table 2 lands under "5.4 Regularization", and `1.0 · 10^20` is extracted as `1.0 · 1020`.
4. *(Also a candidate)* **The References sections**, which match keyword queries for things a paper only cites. They're excluded by default (`exclude_references`).

## Still to do
- **Write ≥15 questions yourself** in `questions.jsonl` (format in `questions.example.jsonl`). Include unanswerable ones.
- Then run `scripts/run_suite.py --real --fresh` for the final `results.csv`, which covers all stand-in scenarios, the real runs and all 4 retrieval settings. `docs/tables.md` is regenerated from it.
- The "retrying made things worse" evidence is `overload` vs `overload_naive_retry` (same load, naive client with no limiter that retries the same model up to 3 times).
