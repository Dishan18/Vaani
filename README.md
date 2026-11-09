# The Relay (Vaani ML Engineering Intern assignment)

Cited question answering over three arXiv papers, built to stay up under bursty load and provider rate limits.

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # then put your Groq key in .env
```

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
