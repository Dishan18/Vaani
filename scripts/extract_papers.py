"""Extract the three papers into citable text blocks.

Reads data/papers/*.pdf and writes data/blocks.jsonl, one line per text block:
    {"paper": "attention", "arxiv": "1706.03762v7", "page": 3,
     "section": "3.2.1 Scaled Dot-Product Attention", "text": "..."}

Page numbers are 1-based PDF pages. Section is the most recent bold numbered
heading seen so far, which is what an answer will cite alongside the page.
Also prints the arXiv stamp found on page 1 so the version can be checked.

Usage:  .venv\\Scripts\\python.exe scripts\\extract_papers.py
"""
import json
import re
import sys
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
PAPERS = {
    "attention": ("1706.03762v7", "attention_1706.03762v7.pdf"),
    "moshi": ("2410.00037v1", "moshi_2410.00037v1.pdf"),
    "dynamic_chunking": ("2507.07955v2", "dynamic_chunking_2507.07955v2.pdf"),
}

# "3", "3.2", "3.2.1", or appendix "A", "A.1", followed by a capitalised title.
HEADING_RE = re.compile(r"^((?:\d+|[A-H])(?:\.\d+){0,3})\.?\s+([A-Z][^\n]{1,90})$")
# Moshi titles its appendices "Appendix C. Title".
APPENDIX_RE = re.compile(r"^Appendix [A-H]\.?\s+[A-Z][^\n]{1,90}$")
UNNUMBERED = {"abstract", "references", "acknowledgements", "acknowledgments",
              "appendix", "conclusion", "introduction"}
BOLD_FLAG = 16


def is_bold(line):
    return all((s["flags"] & BOLD_FLAG) or "Bold" in s["font"] or "CMBX" in s["font"]
               for s in line["spans"] if s["text"].strip())


def heading_of(block):
    """Return a heading string if this block is a bold section heading, else None."""
    lines = [l for l in block["lines"] if "".join(s["text"] for s in l["spans"]).strip()]
    if not lines or len(lines) > 2 or not all(is_bold(l) for l in lines):
        return None
    text = " ".join("".join(s["text"] for s in l["spans"]).strip() for l in lines)
    text = re.sub(r"\s+", " ", text)
    if HEADING_RE.match(text) or APPENDIX_RE.match(text) or text.lower().strip(" .") in UNNUMBERED:
        return text
    return None


def block_text(block):
    out = []
    for l in block["lines"]:
        out.append("".join(s["text"] for s in l["spans"]))
    text = " ".join(t.strip() for t in out)
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # join hyphenated line breaks
    return re.sub(r"\s+", " ", text).strip()


def extract(paper, arxiv, path):
    doc = fitz.open(path)
    stamp = re.search(r"arXiv:\s*\d{4}\.\d{5}v\d+", doc[0].get_text())
    print(f"{paper}: {doc.page_count} pages, page-1 stamp: "
          f"{stamp.group(0) if stamp else 'NOT FOUND'} (expected {arxiv})")
    pages = [[b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
             for page in doc]

    # Running headers/footers: short block texts (digits stripped, so page
    # numbers match) that recur on many pages. They are noise for retrieval.
    seen_on = {}
    for pno, blocks_on_page in enumerate(pages):
        for b in blocks_on_page:
            key = re.sub(r"\d+", "#", block_text(b))
            if len(key) < 150:
                seen_on.setdefault(key, set()).add(pno)
    repeated = {k for k, v in seen_on.items() if len(v) >= max(3, doc.page_count // 4)}

    section = "Front matter"
    blocks = []
    dropped = 0
    for pno, blocks_on_page in enumerate(pages, start=1):
        for b in blocks_on_page:
            h = heading_of(b)
            if h:
                section = h
                continue
            text = block_text(b)
            if len(text) < 3:
                continue
            if re.sub(r"\d+", "#", text) in repeated:
                dropped += 1
                continue
            blocks.append({"paper": paper, "arxiv": arxiv, "page": pno,
                           "section": section, "text": text})
    print(f"    dropped {dropped} running header/footer blocks: {sorted(repeated)[:3]}")
    return blocks


def main():
    out = ROOT / "data" / "blocks.jsonl"
    all_blocks = []
    for paper, (arxiv, fname) in PAPERS.items():
        path = ROOT / "data" / "papers" / fname
        if not path.exists():
            sys.exit(f"missing {path}")
        all_blocks += extract(paper, arxiv, path)
    with out.open("w", encoding="utf-8") as f:
        for b in all_blocks:
            f.write(json.dumps(b, ensure_ascii=False) + "\n")
    print(f"wrote {len(all_blocks)} blocks to {out}")
    for paper in PAPERS:
        secs = []
        for b in all_blocks:
            if b["paper"] == paper and (not secs or secs[-1] != b["section"]):
                secs.append(b["section"])
        print(f"\n{paper}: {len(secs)} sections detected")
        for s in secs:
            print("   ", s)


if __name__ == "__main__":
    main()
