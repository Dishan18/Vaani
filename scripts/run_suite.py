"""Run every load scenario in sequence, then build the tables.

  .venv\\Scripts\\python.exe -X utf8 scripts\\run_suite.py            # stand-in scenarios only (free)
  .venv\\Scripts\\python.exe -X utf8 scripts\\run_suite.py --real     # + real Groq runs (uses daily budget)
  .venv\\Scripts\\python.exe -X utf8 scripts\\run_suite.py --fresh    # delete results.csv first

Real-run budget: real_steady about 12 questions, real_burst 10, quality 4 settings x N questions,
at up to 3 calls each. With 20 questions that's about 340 calls across 3 models: about 11% of the daily
allowance per model, and it refills at 1 request / 86.4 s.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = [sys.executable, "-X", "utf8"]

STANDIN = [
    ("steady", []),
    ("burst", []),
    ("overload", []),
    ("overload", ["--naive", "--label", "overload_naive_retry"]),
    ("rpm_halved", []),
    ("daily_exhausted", []),
    ("chaos", []),
]
REAL = [
    ("real_steady", []),
    ("real_burst", []),
] + [("quality", ["--setting", s, "--label", f"quality_{s}"]) for s in ("small", "medium", "large", "medium_rerank")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--only-real", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    if a.fresh:
        (ROOT / "results.csv").unlink(missing_ok=True)
        shutil.rmtree(ROOT / "data" / "results", ignore_errors=True)
    plan = ([] if a.only_real else STANDIN) + (REAL if (a.real or a.only_real) else [])
    for scen, extra in plan:
        print(f"\n===== {scen} {' '.join(extra)}", flush=True)
        r = subprocess.run(PY + ["scripts/run_load.py", "--scenario", scen] + extra, cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        print("\n".join(r.stdout.strip().splitlines()[-2:]))
        if r.returncode:
            sys.exit(f"{scen} failed with exit code {r.returncode}")
    subprocess.run(PY + ["scripts/make_tables.py"], cwd=ROOT)


if __name__ == "__main__":
    main()
