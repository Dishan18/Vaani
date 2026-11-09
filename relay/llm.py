"""Streaming client for any OpenAI-compatible chat endpoint (Groq or the stand-in).

The probe, the pipeline and the load runner all measure through this one code
path, so the real runs and the stand-in runs are timed the same way.

Each call returns a CallResult with:
  * first_byte_ms:  first SSE chunk of any kind (includes hidden reasoning)
  * first_token_ms: first *visible* content token, which is what a person sees
  * total_ms, usage, every x-ratelimit-* / retry-after header, and the chunk
    arrival times (used to calibrate how the stand-in streams).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx


class LLMError(Exception):
    kind = "error"

    def __init__(self, msg, result: "CallResult | None" = None):
        super().__init__(msg)
        self.result = result


class RateLimited(LLMError):
    kind = "rate_limited"


class CallTimeout(LLMError):
    kind = "timeout"


@dataclass
class CallResult:
    model: str
    status: int = 0
    first_byte_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    total_ms: float = 0.0
    text: str = ""
    reasoning_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    headers: dict = field(default_factory=dict)
    error_body: str = ""
    chunk_times_ms: list = field(default_factory=list)

    @property
    def retry_after_s(self) -> Optional[float]:
        v = self.headers.get("retry-after")
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None


def _keep_header(k: str) -> bool:
    k = k.lower()
    return k.startswith("x-ratelimit") or k in ("retry-after", "x-groq-region", "x-request-id")


class ChatClient:
    def __init__(self, base_url: str, api_key: str = "standin", max_connections: int = 64):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=30.0),
            http2=False,
        )

    async def aclose(self):
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        messages: list,
        *,
        max_tokens: int = 400,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
        first_token_timeout_s: Optional[float] = None,
        json_mode: bool = False,
        reasoning_effort: Optional[str] = "low",
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        extra: Optional[dict] = None,
    ) -> CallResult:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort is not None:
            if model.startswith("openai/gpt-oss"):
                body["reasoning_effort"] = reasoning_effort
                body["include_reasoning"] = False
            elif model.startswith("qwen/"):
                body["reasoning_effort"] = "none" if reasoning_effort in ("none", "low") else reasoning_effort
        if extra:
            body.update(extra)

        res = CallResult(model=model)
        t0 = time.perf_counter()
        ms = lambda: (time.perf_counter() - t0) * 1000.0  # noqa: E731

        async def _run():
            async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body) as r:
                res.status = r.status_code
                res.headers = {k.lower(): v for k, v in r.headers.items() if _keep_header(k)}
                if r.status_code != 200:
                    res.error_body = (await r.aread()).decode("utf-8", "replace")[:2000]
                    res.total_ms = ms()
                    if r.status_code == 429:
                        raise RateLimited(f"429 from {model}", res)
                    raise LLMError(f"HTTP {r.status_code} from {model}: {res.error_body[:200]}", res)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    now = ms()
                    if res.first_byte_ms is None:
                        res.first_byte_ms = now
                    res.chunk_times_ms.append(round(now, 1))
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                    if usage:
                        res.tokens_in = usage.get("prompt_tokens", 0)
                        res.tokens_out = usage.get("completion_tokens", 0)
                        det = usage.get("completion_tokens_details") or {}
                        res.reasoning_tokens = det.get("reasoning_tokens", 0) or 0
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("reasoning"):
                            res.reasoning_chars += len(delta["reasoning"])
                        piece = delta.get("content")
                        if piece:
                            if res.first_token_ms is None:
                                res.first_token_ms = now
                            res.text += piece
                            if on_token:
                                await on_token(piece)
            res.total_ms = ms()

        async def _guarded():
            # A call that "succeeds slowly" is cut off if its first visible token
            # does not arrive in time, not only if the whole call overruns.
            task = asyncio.ensure_future(_run())
            if first_token_timeout_s:
                deadline = time.perf_counter() + first_token_timeout_s
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    if res.first_token_ms is None and time.perf_counter() > deadline and not task.done():
                        task.cancel()
                        res.total_ms = ms()
                        raise CallTimeout(f"no first token from {model} in {first_token_timeout_s}s", res)
            return await task

        try:
            await asyncio.wait_for(_guarded(), timeout=timeout_s)
        except asyncio.TimeoutError:
            res.total_ms = ms()
            raise CallTimeout(f"{model} exceeded {timeout_s}s", res)
        except (httpx.TimeoutException,) as e:
            res.total_ms = ms()
            raise CallTimeout(f"{model} transport timeout: {e!r}", res)
        except httpx.HTTPError as e:
            res.total_ms = ms()
            raise LLMError(f"{model} transport error: {e!r}", res)
        return res
