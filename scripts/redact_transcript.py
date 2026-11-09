"""Copy an exported conversation (zip or folder) with the Groq API key replaced by [REDACTED].

  .venv\\Scripts\\python.exe -X utf8 scripts\\redact_transcript.py <export.zip> <output_folder>

The key is read from .env, so it never appears in this script or the repo. Every text file is scanned, the key is
replaced, and the script then fails loudly if anything that looks like a Groq key (gsk_...) is left.
"""
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.config import groq_key  # noqa: E402


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    key = groq_key()
    work = Path(tempfile.mkdtemp())
    if src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            z.extractall(work)
    else:
        shutil.copytree(src, work, dirs_exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(work, out)
    replaced, leftovers = 0, []
    for f in out.rglob("*"):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if key in text:
            replaced += text.count(key)
            text = text.replace(key, "[REDACTED]")
            f.write_text(text, encoding="utf-8")
        if re.search(r"gsk_[A-Za-z0-9]{20,}", text):
            leftovers.append(str(f))
    print(f"replaced {replaced} occurrence(s) of the key; output in {out}")
    if leftovers:
        sys.exit(f"something that looks like a Groq key is still in: {leftovers}")


if __name__ == "__main__":
    main()
