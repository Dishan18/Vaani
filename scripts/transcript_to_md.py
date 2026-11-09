"""Make a readable Markdown view of an exported (already redacted) Claude Code transcript.

  .venv\\Scripts\\python.exe -X utf8 scripts\\transcript_to_md.py <conversation_folder>

Keeps, in order: every user message, every assistant text reply, and a one-line note for each tool call.
Leaves out hidden reasoning and tool output (both stay in transcript.jsonl, which is the complete record).
Nothing is reworded.
"""
import json
import sys
from pathlib import Path


def short(inp):
    for k in ("description", "command", "file_path", "pattern", "url", "query", "items", "advance", "action", "prompt"):
        if k in inp:
            v = inp[k]
            return f"{k}: {str(v)[:140]}".replace("\n", " ")
    return str(inp)[:140].replace("\n", " ")


def main():
    folder = Path(sys.argv[1])
    rows = [json.loads(l) for l in (folder / "transcript.jsonl").open(encoding="utf-8")]
    out = ["# Conversation with the assistant (readable view)", "",
           "Generated from transcript.jsonl (the complete, unedited export) by scripts/transcript_to_md.py.",
           "Only change from the original: the Groq API key is replaced with [REDACTED]. Hidden reasoning and tool",
           "output are in transcript.jsonl; tool calls appear here as one-line notes.", ""]
    for r in rows:
        if r.get("type") not in ("user", "assistant"):
            continue
        m = r.get("message") or {}
        c = m.get("content")
        if r["type"] == "user":
            if isinstance(c, str) and c.strip():
                out += ["---", "### User", "", c.strip(), ""]
            elif isinstance(c, list):
                texts = [x.get("text", "") for x in c if x.get("type") == "text" and x.get("text", "").strip()]
                if texts:
                    out += ["---", "### User", "", "\n\n".join(t.strip() for t in texts), ""]
        else:
            for x in c if isinstance(c, list) else []:
                if x.get("type") == "text" and x.get("text", "").strip():
                    out += ["### Assistant", "", x["text"].strip(), ""]
                elif x.get("type") == "tool_use":
                    out.append(f"> tool `{x.get('name')}`: {short(x.get('input') or {})}")
            out.append("")
    dest = folder / "conversation_readable.md"
    dest.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {dest} ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
