"""Chunking + BM25 retrieval over the extracted paper blocks.

Why BM25 and not embeddings: an embedding API would be another rate-limited
dependency on the hot path, and a local embedding model needs torch. BM25 costs
zero tokens, runs in ~1 ms over ~2k chunks, and the questions are technical
(exact terms like "warmup_steps", "Mimi", "ratio loss") where lexical match is
strong. Its weaknesses are exactly what the "naive retriever" section is about.

A retrieval *setting* = (piece size, pieces fetched, re-rank yes/no):
  * piece_chars: blocks inside one (paper, section) are packed up to this size.
    Pieces never cross a section boundary, so every piece has one citation.
  * k: pieces handed to the drafting model.
  * rerank: fetch 3k candidates by BM25, then let a cheap model pick the best k.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .config import DATA

PAPER_TITLES = {
    "attention": "Attention Is All You Need (arXiv 1706.03762v7)",
    "moshi": "Moshi (arXiv 2410.00037v1)",
    "dynamic_chunking": "Dynamic Chunking / H-Net (arXiv 2507.07955v2)",
}
SHORT = {"attention": "Attention", "moshi": "Moshi", "dynamic_chunking": "H-Net"}

STOP = set("""a an the of to in on for and or is are was were be been by with as at from that this these those
it its into than then which what who whom how why when where does do did can could should would will
use used using about between not no their there they them we our you your i me my""".split())


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    toks = re.findall(r"[a-z0-9_]+(?:\.[0-9]+)?", text)
    return [t for t in toks if t not in STOP and len(t) > 1]


@dataclass
class Setting:
    name: str
    piece_chars: int
    k: int
    rerank: bool = False
    exclude_references: bool = True

    def label(self):
        return f"{self.name}(piece={self.piece_chars}c,k={self.k},rerank={'yes' if self.rerank else 'no'})"


SETTINGS = {
    "small": Setting("small", piece_chars=600, k=8),
    "medium": Setting("medium", piece_chars=1200, k=5),
    "large": Setting("large", piece_chars=2400, k=3),
    "medium_rerank": Setting("medium_rerank", piece_chars=1200, k=4, rerank=True),
    # What a person gets in lean mode with the chosen setting: 4 small pieces, no router, no verifier.
    "small_lean": Setting("small_lean", piece_chars=600, k=4),
}


@dataclass
class Piece:
    id: str
    paper: str
    section: str
    page_start: int
    page_end: int
    text: str
    tokens: list = field(default_factory=list, repr=False)

    def cite(self) -> str:
        pages = f"p.{self.page_start}" if self.page_start == self.page_end else f"pp.{self.page_start}-{self.page_end}"
        return f"{SHORT[self.paper]}, {self.section}, {pages}"


@dataclass
class Hit:
    piece: Piece
    score: float


def load_blocks():
    with (DATA / "blocks.jsonl").open(encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def build_pieces(blocks, piece_chars: int, exclude_references: bool = True) -> list[Piece]:
    pieces: list[Piece] = []
    cur: Optional[dict] = None

    def flush():
        if cur and cur["text"].strip():
            pid = f"{cur['paper']}:{len(pieces)}"
            pieces.append(Piece(pid, cur["paper"], cur["section"], cur["p0"], cur["p1"], cur["text"].strip()))

    for b in blocks:
        if exclude_references and b["section"].lower().startswith("references"):
            continue
        same = cur and cur["paper"] == b["paper"] and cur["section"] == b["section"]
        if same and len(cur["text"]) + len(b["text"]) <= piece_chars:
            cur["text"] += " " + b["text"]
            cur["p1"] = b["page"]
        else:
            flush()
            cur = {"paper": b["paper"], "section": b["section"], "p0": b["page"], "p1": b["page"], "text": b["text"]}
            # A single oversized block is split on sentence boundaries.
            while len(cur["text"]) > piece_chars * 1.5:
                cut = cur["text"].rfind(". ", 0, piece_chars)
                cut = cut + 1 if cut > piece_chars // 3 else piece_chars
                head, cur["text"] = cur["text"][:cut], cur["text"][cut:].lstrip()
                pieces.append(Piece(f"{b['paper']}:{len(pieces)}", b["paper"], b["section"], b["page"], b["page"], head))
    flush()
    return pieces


class BM25Index:
    def __init__(self, pieces: list[Piece], k1: float = 1.4, b: float = 0.75):
        self.pieces = pieces
        self.k1, self.b = k1, b
        self.df: Counter = Counter()
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths = []
        for i, p in enumerate(pieces):
            # Section title is indexed with the text: "3.2.1 Scaled Dot-Product Attention" is strong evidence.
            p.tokens = tokenize(p.section + " " + p.text)
            tf = Counter(p.tokens)
            for t, c in tf.items():
                self.postings[t].append((i, c))
            self.df.update(tf.keys())
            lengths.append(len(p.tokens))
        self.lengths = lengths
        self.avgdl = sum(lengths) / max(1, len(lengths))
        n = len(pieces)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in self.df.items()}

    def search(self, query: str, k: int, papers: Optional[list[str]] = None) -> list[Hit]:
        scores: dict[int, float] = defaultdict(float)
        for t in set(tokenize(query)):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in self.postings[t]:
                dl = self.lengths[i]
                scores[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        allowed = set(papers) if papers else None
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for i, s in ranked:
            if allowed and self.pieces[i].paper not in allowed:
                continue
            out.append(Hit(self.pieces[i], s))
            if len(out) >= k:
                break
        return out

    def max_possible(self, query: str) -> float:
        """Upper bound used to normalise scores: sum of idf of the query terms we know."""
        return sum(self.idf.get(t, 0.0) for t in set(tokenize(query))) * (self.k1 + 1) or 1.0


_cache: dict[tuple, BM25Index] = {}


def get_index(setting: Setting) -> BM25Index:
    key = (setting.piece_chars, setting.exclude_references)
    if key not in _cache:
        _cache[key] = BM25Index(build_pieces(load_blocks(), setting.piece_chars, setting.exclude_references))
    return _cache[key]
