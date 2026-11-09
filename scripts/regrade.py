"""Re-apply relay/grade.py to the stored answers and rewrite only the answer_grade column of results.csv.

Used once after fixing the grader (Unicode hyphens like 'moving‑average' made correct answers fail the
pattern match). No model calls: the answer text is in data/results/detail.jsonl. Prints every change.
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.grade import grade, load_questions  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main():
    qs = {q["id"]: q for q in load_questions()}
    det = {d["request_id"]: d for d in map(json.loads, (ROOT / "data" / "results" / "detail.jsonl").open(encoding="utf-8"))}
    path = ROOT / "results.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    cols = list(rows[0].keys())
    changed = 0
    for r in rows:
        if r["source"] != "real":
            continue
        d = det[r["request_id"]]
        new = grade(qs[d["question_id"]], d["mode"], d["text"], r["status"])
        if new != r["answer_grade"]:
            print(f"{r['request_id']} {d['question_id']}: {r['answer_grade']} -> {new}")
            r["answer_grade"] = new
            changed += 1
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"{changed} grades changed")


if __name__ == "__main__":
    main()
