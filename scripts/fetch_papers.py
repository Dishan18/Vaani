"""Download the three papers at their exact arXiv versions into data/papers/.

Usage:  .venv\\Scripts\\python.exe scripts\\fetch_papers.py
"""
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PAPERS = {
    "attention_1706.03762v7.pdf": "https://arxiv.org/pdf/1706.03762v7",
    "moshi_2410.00037v1.pdf": "https://arxiv.org/pdf/2410.00037v1",
    "dynamic_chunking_2507.07955v2.pdf": "https://arxiv.org/pdf/2507.07955v2",
}


def main():
    out = ROOT / "data" / "papers"
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": "relay-assignment/0.1"}) as c:
        for name, url in PAPERS.items():
            dest = out / name
            if dest.exists() and dest.read_bytes()[:5] == b"%PDF-":
                print(f"have {name}")
                continue
            r = c.get(url)
            r.raise_for_status()
            if r.content[:5] != b"%PDF-":
                raise SystemExit(f"{url} did not return a PDF")
            dest.write_bytes(r.content)
            print(f"fetched {name} ({len(r.content):,} bytes)")


if __name__ == "__main__":
    main()
