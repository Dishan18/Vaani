"""Show how a naive retriever (raw question -> BM25, references included, no paper routing) goes wrong.

  .venv\\Scripts\\python.exe -X utf8 scripts\\retriever_traps.py

For each trap: the naive top hit, and the rank of the passage that actually answers (or should block) the question.
No model calls. Writes docs/retriever_traps.txt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.retrieval import BM25Index, Setting, build_pieces, load_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TRAPS = [
    ("Same term, wrong paper", "What word error rate does H-Net achieve on LibriSpeech?",
     lambda p: p.paper == "dynamic_chunking", "H-Net never reports LibriSpeech. Any hit is from Moshi and must not be used."),
    ("Same term, wrong paper", "What dropout rate was used?",
     lambda p: p.paper == "attention" and "Regularization" in p.section, "Attention 5.4 is the intended answer."),
    ("Vocabulary mismatch", "What is the frame rate of the Mimi codec?",
     lambda p: p.paper == "moshi" and "12.5" in p.text and "Audio Tokenization" in p.section, "Moshi 3.3 says '12.5Hz'."),
    ("Similar procedure, other paper", "What learning rate schedule was used to train Helium?",
     lambda p: p.paper == "moshi" and "cosine learning rate" in p.text, "Moshi 4.4: cosine schedule. Attention 5.3 has a warmup formula."),
    ("References section", "Which paper evaluates on ImageNet?",
     lambda p: "references" in p.section.lower(), "Only a bibliography entry matches; no paper reports ImageNet results."),
    ("Flattened numbers", "What was the training cost in FLOPs of the big Transformer?",
     lambda p: p.paper == "attention" and "1019" in p.text, "Extracted text reads '2.3 · 1019' for 2.3 x 10^19."),
]


def main():
    blocks = load_blocks()
    naive = BM25Index(build_pieces(blocks, 1200, exclude_references=False))
    ours = BM25Index(build_pieces(blocks, 1200, exclude_references=True))
    out = []
    for kind, q, is_target, note in TRAPS:
        hits = naive.search(q, 50)
        top = hits[0].piece if hits else None
        rank = next((i + 1 for i, h in enumerate(hits) if is_target(h.piece)), None)
        ours_top = ours.search(q, 1)
        out.append(f"[{kind}] {q}\n  naive top hit: {top.cite() if top else '-'}: \"{top.text[:110] if top else ''}...\"\n"
                   f"  rank of target passage in naive results: {rank}\n"
                   f"  with references excluded, top hit: {ours_top[0].piece.cite() if ours_top else '-'}\n  note: {note}\n")
    text = "\n".join(out)
    (ROOT / "docs" / "retriever_traps.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
