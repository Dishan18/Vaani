"""Automatic grading against questions.jsonl gold labels.

Gold format (one JSON object per line):
  {"id": "q01", "question": "...", "answerable": true,
   "paper": "attention", "pages": [7], "must_include": ["4000"]}
  {"id": "q12", "question": "...", "answerable": false}

Grades (the brief's enum):
  correct            answered, every must_include pattern present, and a cited source on the gold paper/page (+-1)
  wrong              answered but missing the fact or the right citation, or answered an unanswerable question
  refused_correctly  declined (or gave passages only) on a question the papers don't answer
  refused_wrongly    declined on a question the papers DO answer

Requests that never produced a reply (busy / timeout / error) get an empty grade: there's no answer to grade.
Stand-in runs get an empty grade too, because their text is synthetic.
"""
import json
import re

from .config import ROOT

SHORT_TO_PAPER = {"Attention": "attention", "Moshi": "moshi", "H-Net": "dynamic_chunking"}


def load_questions(path=None):
    p = path or (ROOT / "questions.jsonl")
    if not p.exists():
        p = ROOT / "questions.example.jsonl"
    with p.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip() and not l.lstrip().startswith("//")]


def cited_sources(text):
    """[(paper, page_start, page_end)] parsed from the rendered 'Sources:' / passage lines."""
    out = []
    for short, pages in re.findall(r"(Attention|Moshi|H-Net), [^\n]*?, pp?\.(\d+(?:-\d+)?)", text):
        a, _, b = pages.partition("-")
        out.append((SHORT_TO_PAPER[short], int(a), int(b or a)))
    return out


def normalize(text: str) -> str:
    """Models emit Unicode hyphens and spaces (seen: 'moving‑average' with U+2011). Fold them to ASCII before matching."""
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = re.sub(r"[   ​]", " ", text)
    return text.replace("-", " ")


def grade(q: dict, mode: str, text: str, status: str) -> str:
    if mode in ("busy", "gave_up") or (status != "ok" and mode != "retrieval_only"):
        return ""
    declined = mode in ("refused", "retrieval_only")
    if not q.get("answerable", True):
        return "refused_correctly" if declined else "wrong"
    if declined:
        return "refused_wrongly"
    body = normalize(text.split("\n\nSources:")[0])
    if not all(re.search(p, body, re.I) for p in q.get("must_include", [])):
        return "wrong"
    gold_pages = set(q.get("pages", []))
    for paper, a, b in cited_sources(text):
        if paper == q.get("paper") and (not gold_pages or any(a - 1 <= g <= b + 1 for g in gold_pages)):
            return "correct"
    return "wrong"
