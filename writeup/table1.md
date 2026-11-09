| Limit | Value you found | How you found it | Confidence |
|---|---|---|---|
| Requests per minute | 30 per model | 30 tiny calls to gpt-oss-20b in 4 s; call 31 got 429 "requests per minute (RPM): Limit 30, Used 30" | high |
| Tokens per minute | 8,000 per model; qwen also has 7,000 input tokens/min (ITPM) | `x-ratelimit-limit-tokens: 8000` on every reply; 429 bodies "TPM: Limit 8000" and, for qwen only, "ITPM: Limit 7000" (no header shows it) | high |
| Requests per day | 1,000 per model, refilled continuously at 1 request per 86.4 s | `x-ratelimit-limit-requests: 1000`; `reset-requests / requests used` = 86.40 s on all 119 probe calls. Not hit on purpose | high (value), medium (behaviour at zero) |
| Tokens per day | 200,000 per model (docs) | Groq docs table only; no header reports it, and no run (probe, Table 3, real loads) ever got a daily-limit 429 | low |
| Requests in flight at once | no limit seen up to 8 | bursts of 1/4/8/16 to qwen: 8 at once all OK with no slowdown; at 16 the ITPM limit hit first, so higher is unknown | low |
| What it returns when a limit is hit | HTTP 429, JSON `error.type` requests or tokens, `code` rate_limit_exceeded, message with Limit / Used / Requested; `retry-after` in seconds sized to this request; an RPM 429 has no token headers | raw 429 responses in data/probe/probe_raw.jsonl | high |
| Time until it recovers | about 2 s for a small request; `retry-after` said 2 s (RPM) and 21 s (TPM, for a 4,319-token request) | first poll 2 s after each 429 succeeded; buckets refill continuously (RPM 0.5/s, TPM 133 tokens/s) | high |
