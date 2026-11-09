"""Render writeup/writeup.md to writeup/writeup.pdf (A4), inserting the tables from docs/tables.md.

  .venv\\Scripts\\python.exe -X utf8 scripts\\build_writeup.py

In writeup.md, a line {{TABLE1}} is replaced by writeup/table1.md, and {{TABLE2A}}, {{TABLE2B}} and {{TABLE3}} by the
matching tables generated from results.csv, so the numbers in the PDF always match the raw file.
Prints the page count (the brief allows 2 pages of writing) and fails loudly on em dashes or unfilled placeholders.
"""
import re
import sys
from pathlib import Path

import markdown
import pymupdf

ROOT = Path(__file__).resolve().parent.parent
W = ROOT / "writeup"

CSS = """
* { font-family: sans-serif; }
body { font-size: 7.6pt; line-height: 1.22; color: #111; }
h1 { font-size: 11pt; margin: 0 0 2pt 0; }
h2 { font-size: 8.6pt; margin: 5pt 0 1.5pt 0; color: #0b3d6e; }
p { margin: 0 0 2.5pt 0; }
ul { margin: 0 0 2pt 0; padding-left: 10pt; }
li { margin: 0; }
table { border-collapse: collapse; margin: 1.5pt 0 3pt 0; width: 100%; }
th, td { border: 0.4pt solid #888; padding: 0.6pt 2pt; font-size: 6.2pt; text-align: left; vertical-align: top; }
th { background: #e8eef5; }
code { font-family: monospace; font-size: 7pt; }
.small { font-size: 6.6pt; color: #333; }
"""


def table_from(md_text, title):
    m = re.search(rf"## {re.escape(title)}.*?\n\n(\|.*?)(?:\n\n|\Z)", md_text, re.S)
    if not m:
        sys.exit(f"table '{title}' not found in docs/tables.md")
    return m.group(1).strip()


def main():
    src = (W / "writeup.md").read_text(encoding="utf-8")
    tables = (ROOT / "docs" / "tables.md").read_text(encoding="utf-8")
    src = src.replace("{{TABLE1}}", (W / "table1.md").read_text(encoding="utf-8").strip())
    src = src.replace("{{TABLE2A}}", table_from(tables, "Table 2a: Latency"))
    src = src.replace("{{TABLE2B}}", table_from(tables, "Table 2b: Outcomes"))
    src = src.replace("{{TABLE3}}", table_from(tables, "Table 3: Retrieval settings"))
    if "—" in src:
        sys.exit("em dash found in writeup.md")
    todo = re.findall(r"\[\[YOU:[^\]]*\]\]", src)
    html = markdown.markdown(src, extensions=["tables"])
    html = re.sub(r"\[\[YOU:([^\]]*)\]\]", r"<span style='background:#ffe9a8'>[YOU:\1]</span>", html)
    story = pymupdf.Story(html=html, user_css=CSS)
    out = W / "writeup.pdf"
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4")
    area = page + (30, 26, -30, -26)
    pages, more = 0, 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(area)
        story.draw(dev)
        writer.end_page()
        pages += 1
    writer.close()
    print(f"wrote {out} : {pages} page(s)")
    if todo:
        print(f"{len(todo)} [[YOU: ...]] placeholder(s) still to fill (highlighted in the PDF):")
        for t in todo:
            print("  ", t[:110])


if __name__ == "__main__":
    main()
