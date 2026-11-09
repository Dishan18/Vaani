# The Relay (Vaani ML Engineering Intern assignment)

Cited question answering over three arXiv papers, built to stay up under bursty load and provider rate limits.

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then put your Groq key in .env
```

## Run

```
.venv\Scripts\python.exe scripts\fetch_papers.py              # papers at exact arXiv versions
.venv\Scripts\python.exe scripts\extract_papers.py            # -> data/blocks.jsonl
.venv\Scripts\python.exe -X utf8 scripts\probe_groq.py        # real Groq probe (budgeted; see docs/probe_plan.md)
.venv\Scripts\python.exe -X utf8 scripts\analyze_probe.py     # Table 1 evidence + stand-in calibration
.venv\Scripts\python.exe -X utf8 scripts\run_load.py --scenario burst            # stand-in
.venv\Scripts\python.exe -X utf8 scripts\run_load.py --scenario quality          # real Groq, graded
```

## Layout

| Path | What |
|---|---|
| `relay/llm.py` | One streaming client for Groq and the stand-in. Records first byte, first visible token, headers and chunk timings. |
| `relay/limiter.py` | Client-side mirror of Groq's continuously refilling buckets, synced from headers and 429 bodies |
| `relay/retrieval.py` | Section-aware chunking + BM25, with 5 retrieval settings (small, medium, large, medium_rerank, small_lean) |
| `relay/pipeline.py` | understand → retrieve (tool, loop) → decide → draft → check → send, plus the degrade ladder |
| `relay/grade.py` | Auto-grading against `questions.jsonl` |
| `standin/server.py` | Stand-in model service (limits, 429s, latency, stalls, errors, slowdown under load) |
| `scripts/run_load.py` | Load shapes → `results.csv` + `data/results/detail.jsonl` |
| `scripts/run_suite.py` | Runs every scenario (`--real` adds the Groq runs), then `make_tables.py` |
| `scripts/make_tables.py` | Tables 2a / 2b / 3 computed from `results.csv` only, written to `docs/tables.md` |
| `scripts/explain_request.py` | Splits one request's latency into waiting, working and retrying (heading 4) |
| `scripts/retriever_traps.py` | How a naive retriever goes wrong on this library (no model calls) |
| `scripts/regrade.py` | Re-applies the grader to stored answers (used once, after a grader fix) |
| `scripts/build_writeup.py` | Renders `writeup/writeup.md` to `writeup/writeup.pdf`, inserting the tables |
| `scripts/redact_transcript.py` | Copies an exported conversation with the Groq key from `.env` replaced by [REDACTED] |
| `questions.jsonl` | The 18 evaluation questions with gold facts |
| `docs/findings_and_decisions.md` | Decision log with the evidence for each choice |
| `docs/` | Probe plan, findings, and the decisions to review |

## Data

- `data/papers/`: the three PDFs, fetched by exact version from arxiv.org
  (1706.03762v7, 2410.00037v1, 2507.07955v2). The version stamp on page 1 was checked by `extract_papers.py`.
- `data/blocks.jsonl`: produced by `scripts/extract_papers.py`. One text block per line with
  `paper, arxiv, page, section, text`. Running headers and footers are removed.

Known extraction limits (relevant to retrieval):
- Floating tables and figures are attributed to the section whose text precedes them in reading order.
  For example, Attention Table 2 (page 8) is labelled "5.4 Regularization" rather than "6.1 Machine Translation".
- Superscripts are flattened: `1.0 · 10^20` is extracted as `1.0 · 1020`.
- Table cells are extracted row by row without their column headers.
