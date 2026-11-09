"""Settings loaded from the environment and the .env file. The key is never hard-coded."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
STANDIN_BASE_URL = os.environ.get("STANDIN_BASE_URL", "http://127.0.0.1:8765/openai/v1")

# Public list prices from Groq's /models endpoint, captured 2026-11-09 (USD per token).
# The free tier costs nothing. These prices give "cost per 1,000 answers" if the same
# load ran on a paid plan.
PRICES = {
    "openai/gpt-oss-20b": (0.075e-6, 0.30e-6),
    "openai/gpt-oss-120b": (0.15e-6, 0.60e-6),
    "qwen/qwen3.8-27b": (0.80e-6, 4.0e-6),
}


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def groq_key():
    load_env()
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise SystemExit("GROQ_API_KEY not set (put it in .env)")
    return key


def cost_usd(model, tokens_in, tokens_out):
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return tokens_in * p_in + tokens_out * p_out
