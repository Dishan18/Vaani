# Probe plan: learning the real Groq free tier

**Goal:** fill Table 1 and calibrate the stand-in using as little of the shared daily budget as possible.
**Hard cap:** 220 requests and about 120K tokens, spread across three models. That is under 10% of any one model's daily allowance.

## What the docs already say (and why they can't be trusted alone)
- `x-ratelimit-limit-requests` / `-remaining-requests` / `-reset-requests` are **per day** (RPD).
- `x-ratelimit-limit-tokens` / `-remaining-tokens` / `-reset-tokens` are **per minute** (TPM).
- `retry-after` (seconds) appears only on a 429.
- The docs table (30 RPM, 1K RPD, 8K TPM, 200K TPD for gpt-oss-20b, gpt-oss-120b and qwen3.8-27b) is labelled as the *Developer* plan. Free-tier numbers have to come from the headers.
- The docs say nothing about RPM headers, TPD headers, concurrency limits or the 429 body. **Those are what the probe finds out.**

## Phases (in order)

| Phase | Model | What we send | What it tells us | Cost |
|---|---|---|---|---|
| A. headers | all 3 | 2 tiny calls each: `max_tokens=16` and `max_tokens=2000`, both asking for a one-word reply | RPD and TPM from the headers. Whether TPM is charged on `max_tokens` (a reservation) or on tokens actually used: compare how far `remaining-tokens` drops. | 6 req |
| C. rpm_wall | gpt-oss-20b | tiny calls, 5 in flight, until the first 429 (cap 45) | the real RPM, the 429 body and headers, `retry-after` | ≤45 req |
| D. tpm_wall | gpt-oss-120b | ~3K-token prompts, 1 every 1.5 s, until a 429 (cap 8) | how a TPM 429 differs from an RPM 429 | ≤8 req |
| C/D. recovery | the walled model | 1 tiny call every 2 s after the first 429, until a 200 (cap 90 s) | time until it recovers, compared with what `retry-after` claimed | ≤45 req |
| E. concurrency | qwen3.8-27b | bursts of 1, 4, 8 and 16 at once (small prompts) | whether there's an in-flight limit, and how latency grows with concurrency (calibrates the stand-in's "slower under load") | 29 req |
| B. latency | all 3 | 16 each, alternating a short router-shaped call (~300 tokens in) and a long draft-shaped call (~1.8K tokens in, 300 out), paced to stay under 70% of TPM | the spread of time to first byte, time to first token and total time, plus inter-chunk gaps for streaming calibration | 48 req |

C, D and E run at the same time because each uses a different model. If limits are per model, they won't interfere, and the headers will confirm whether they are.
B runs after a 65 s cooldown so the walls don't skew the latency numbers.

## Not probed on purpose
- **The per-day wall.** Hitting it would burn a whole day of budget. RPD and its reset time come from the `-requests` headers instead (confidence: medium).
- **TPD.** It isn't in the headers according to the docs. It stays at the documented value with low confidence unless a header shows up.

## Outputs
- `data/probe/probe_raw.jsonl`: one row per request, with every rate-limit header, the error body, usage, reasoning tokens and chunk timings.
- `scripts/analyze_probe.py` turns that file into Table 1 and the stand-in calibration file `data/probe/calibration.json`.
