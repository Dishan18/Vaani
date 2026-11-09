"""The Relay pipeline: understand -> retrieve (tool, may loop) -> decide -> draft -> check -> send.

Design in one paragraph
-----------------------
The free tier gives each model only 8,000 tokens/min and 1,000 requests/day,
and those buckets are *per model*. A full answer costs ~2-3K tokens, so one
model alone serves ~3 answers/min. The pipeline therefore spreads its steps
across models (router + verifier on gpt-oss-20b, drafting on gpt-oss-120b,
qwen3.8-27b as overflow) and asks the limiter *before* each call whether it
fits the person's deadline. When it doesn't, we degrade instead of queueing:
  full answer -> skip router/verifier -> weaker drafting model -> retrieval-only
  reply (cited passages, no LLM) -> honest "busy, retry in N s".
Answers are gated: nothing is released until the deterministic citation check
passes, because a wrong answer is worse than a slow one. Groq generates at
hundreds of tokens/s, so buffering a ~250-token draft costs well under a second.
That's the trade-off: time to first token is slightly worse in exchange for
never streaming an answer we then have to retract.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .limiter import Budgets, Unavailable
from .llm import CallTimeout, ChatClient, LLMError, RateLimited
from .retrieval import PAPER_TITLES, SETTINGS, Hit, Setting, get_index

ROUTER_MODEL = "openai/gpt-oss-20b"
DRAFT_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]  # preference order
VERIFY_MODEL = "openai/gpt-oss-20b"
NOT_IN_PAPERS = "NOT_IN_PAPERS"


@dataclass
class PipelineConfig:
    setting: str = "small"             # chosen from Table 3: 14/14 answerable correct, fewest tokens per answer
    patience_s: float = 20.0           # default deadline if the caller gives none
    step_timeout_s: float = 12.0       # hard cap on any single model call
    first_token_timeout_s: float = 6.0  # "succeeds slowly" calls are cut here
    max_attempts: int = 2              # 1 try + at most 1 retry per step
    verify: str = "auto"               # "off" | "auto" (only with headroom) | "on"
    use_router: str = "auto"           # "off" | "auto" | "on"
    refuse_score: float = 0.12         # normalised BM25 below this -> "the papers don't say"
    loop_score: float = 0.25           # below this, run a second retrieval round
    daily_low: float = 0.15            # under 15% daily left: drop router + verifier
    draft_max_tokens: int = 500        # gpt-oss reasoning shares this budget (probe phase A), and Groq charges the
                                       # full max_tokens at admission, so this is kept just above what drafts use
    prefer_wait_s: float = 2.0         # how long to wait for the best model before taking a weaker one
    naive: bool = False                # baseline for the retry experiment: ignore the limiter, retry the same model
    max_wait_s: float = 3.0            # longest we wait for model budget before degrading. Overload experiment
                                       # (2 seeds): success ~equal at 3/6/15 s, but 3 s had no give-ups and the lowest p95
    lean_k: int = 4                    # pieces sent to the drafter in lean mode (fewer input tokens when limits are
                                       # close). Quality at this level is measured as Table 3's small_lean row
    tool_fail_rate: float = 0.0        # chaos knobs for the retrieval tool
    tool_slow_rate: float = 0.0
    tool_timeout_s: float = 1.0


@dataclass
class Span:
    step: str
    start_ms: float
    end_ms: float
    kind: str           # work | wait | retry
    model: str = ""
    detail: str = ""


@dataclass
class Outcome:
    request_id: str
    question: str
    status: str = "ok"              # ok | rate_limited | timeout | error
    mode: str = "full"              # full | lean | weaker_model | retrieval_only | refused | busy
    text: str = ""
    first_token_ms: Optional[float] = None   # when the first answer text was released to the person
    total_ms: float = 0.0
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    draft_model: str = ""
    citations: list = field(default_factory=list)
    spans: list = field(default_factory=list)
    cost_usd: float = 0.0
    error: str = ""


class RetryBudget:
    """Retries are capped at ~10% of first attempts across all requests. Under overload, retries are
    extra load, and unbounded retries turn a brownout into an outage."""

    def __init__(self, ratio=0.1, initial=5.0):
        self.ratio, self.tokens, self.cap = ratio, initial, 20.0

    def on_attempt(self):
        self.tokens = min(self.cap, self.tokens + self.ratio)

    def try_spend(self) -> bool:
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def est_tokens(messages, max_tokens):
    """What Groq charges at admission, calibrated from the probe's 429 bodies: prompt tokens (about 3.1 characters
    per token; e.g. 13,208 chars with max_tokens=16 was 'Requested 4319') plus the full max_tokens.
    The difference from actual usage is settled after the call."""
    return sum(len(m["content"]) for m in messages) / 3.1 + 20 + max_tokens


class Pipeline:
    def __init__(self, client: ChatClient, budgets: Budgets, cfg: PipelineConfig | None = None,
                 retry_budget: RetryBudget | None = None, cost_fn=None):
        self.client = client
        self.budgets = budgets
        self.cfg = cfg or PipelineConfig()
        self.retry_budget = retry_budget or RetryBudget()
        self.cost_fn = cost_fn
        self.setting: Setting = SETTINGS[self.cfg.setting]
        self.index = get_index(self.setting)

    # ------------------------------------------------------------------ helpers
    async def _llm(self, out: Outcome, t0: float, deadline: float, step: str, models: list[str],
                   messages: list, max_tokens: int, expect_out: int, json_mode=False):
        """One logical model call with budget reservation, fallback and a bounded retry."""
        attempt, last_err = 0, None
        candidates = [m for m in models if m in self.budgets.by_model]
        while attempt < self.cfg.max_attempts and candidates:
            if attempt > 0:
                if not self.retry_budget.try_spend():
                    break
                out.retries += 1
            self.retry_budget.on_attempt()
            attempt += 1
            est = est_tokens(messages, max_tokens)
            # Model choice: the preferred model if it's free within `prefer_wait_s`, otherwise the first fallback
            # that's free now, otherwise wait for whichever frees up first (if that fits the deadline).
            chosen, wait_start = None, time.perf_counter()
            waits = [(self.budgets[m].queued_wait(est)[0], m) for m in candidates]
            order = []
            if waits[0][0] <= self.cfg.prefer_wait_s:
                order.append(waits[0][1])
            order += [m for w, m in waits[1:] if w <= 0.05]
            order += [m for w, m in sorted(waits) if m not in order]
            if self.cfg.naive:
                order, chosen = [], candidates[0]  # naive client: no budget check, always the preferred model
            wait_deadline = min(deadline - 1.5, time.monotonic() + self.cfg.max_wait_s)
            for m in order:
                try:
                    await self.budgets[m].reserve(est, wait_deadline)
                    chosen = m
                    break
                except Unavailable as e:
                    last_err = e
            waited_ms = (time.perf_counter() - wait_start) * 1000
            if waited_ms > 5:
                out.spans.append(Span(step, (wait_start - t0) * 1000, (wait_start - t0) * 1000 + waited_ms,
                                      "wait", chosen or "", "budget"))
            if chosen is None:
                raise last_err  # nothing fits: the caller degrades
            call_start = time.perf_counter()
            remaining = deadline - time.monotonic()
            try:
                out.llm_calls += 1
                r = await self.client.chat(chosen, messages, max_tokens=max_tokens, json_mode=json_mode,
                                           timeout_s=max(1.0, min(self.cfg.step_timeout_s, remaining)),
                                           first_token_timeout_s=min(self.cfg.first_token_timeout_s, remaining))
                self.budgets[chosen].observe_headers(r.headers)
                self.budgets[chosen].settle(est, r.tokens_in + r.tokens_out)
                out.tokens_in += r.tokens_in
                out.tokens_out += r.tokens_out
                if self.cost_fn:
                    out.cost_usd += self.cost_fn(chosen, r.tokens_in, r.tokens_out)
                kind = "retry" if attempt > 1 else "work"
                out.spans.append(Span(step, (call_start - t0) * 1000, (time.perf_counter() - t0) * 1000, kind, chosen,
                                      f"ttft={r.first_token_ms and round(r.first_token_ms)}"))
                if not r.text.strip():
                    # Phase A: gpt-oss can spend the whole max_tokens on hidden reasoning and return HTTP 200 with
                    # no text. Treat that as a failure of this attempt.
                    last_err = LLMError(f"{chosen} returned empty content", r)
                    candidates = candidates[1:] + candidates[:1]
                    continue
                return r, chosen
            except RateLimited as e:
                if e.result:
                    self.budgets[chosen].observe_429(e.result.error_body, e.result.headers)
                out.spans.append(Span(step, (call_start - t0) * 1000, (time.perf_counter() - t0) * 1000,
                                      "retry", chosen, "429"))
                last_err = e
                if self.cfg.naive:
                    await asyncio.sleep(random.uniform(0.2, 0.5))  # naive: same model again, short backoff
                else:
                    candidates = [m for m in candidates if m != chosen] or candidates  # different bucket first
            except CallTimeout as e:
                out.spans.append(Span(step, (call_start - t0) * 1000, (time.perf_counter() - t0) * 1000,
                                      "retry", chosen, "timeout"))
                last_err = e
                candidates = [m for m in candidates if m != chosen] or candidates
            except LLMError as e:
                out.spans.append(Span(step, (call_start - t0) * 1000, (time.perf_counter() - t0) * 1000,
                                      "retry", chosen, "error"))
                last_err = e
                await asyncio.sleep(random.uniform(0.1, 0.3))
        raise last_err or LLMError(f"{step}: no attempt possible")

    async def _tool_search(self, query: str, papers, k: int):
        """The retrieval *tool*. It has its own timeout, because tools time out in production."""
        async def run():
            if self.cfg.tool_slow_rate and random.random() < self.cfg.tool_slow_rate:
                await asyncio.sleep(self.cfg.tool_timeout_s * 2)
            if self.cfg.tool_fail_rate and random.random() < self.cfg.tool_fail_rate:
                raise RuntimeError("search tool failed")
            return self.index.search(query, k, papers)
        try:
            return await asyncio.wait_for(run(), self.cfg.tool_timeout_s)
        except (asyncio.TimeoutError, RuntimeError):
            # One fast retry without the paper filter; local search is cheap to retry.
            return self.index.search(query, k, None)

    def _norm(self, query, hits: list[Hit]) -> float:
        return hits[0].score / self.index.max_possible(query) if hits else 0.0

    # ------------------------------------------------------------------ the pipeline
    async def answer(self, request_id: str, question: str, deadline: Optional[float] = None) -> Outcome:
        t0 = time.perf_counter()
        deadline = deadline or (time.monotonic() + self.cfg.patience_s)
        out = Outcome(request_id, question)
        ms = lambda: (time.perf_counter() - t0) * 1000  # noqa: E731
        drafter = DRAFT_MODELS[0]
        # Lean mode = draft only (1 call instead of 3). Used when the daily budget is low, when time is short,
        # or when the drafting model is already backed up. Then router/verifier tokens are better spent on drafts.
        lean = (self.budgets.daily_fraction_left(drafter) < self.cfg.daily_low
                or deadline - time.monotonic() < 8
                or self.budgets[drafter].queued_wait(2000)[0] > self.cfg.prefer_wait_s)
        try:
            # 1. Understand: which papers, a better search query, a second query for the loop.
            papers, query, alt_query = None, question, None
            use_router = self.cfg.use_router == "on" or (self.cfg.use_router == "auto" and not lean)
            if use_router:
                try:
                    r, _ = await self._llm(out, t0, deadline, "understand", [ROUTER_MODEL, "qwen/qwen3.8-27b"],
                                           self._router_msgs(question), max_tokens=300, expect_out=120, json_mode=True)
                    plan = _parse_json(r.text)
                    papers = [p for p in plan.get("papers", []) if p in PAPER_TITLES] or None
                    query = plan.get("query") or question
                    alt_query = plan.get("alt_query")
                except (Unavailable, LLMError):
                    lean = True  # fall through with the raw question
            # 2. Retrieve (tool). Loop once if evidence is weak.
            s = time.perf_counter()
            n_fetch = self.setting.k * (3 if self.setting.rerank else 1)
            hits = await self._tool_search(query, papers, n_fetch)
            score = self._norm(query, hits)
            if score < self.cfg.loop_score:
                for q2, p2 in ((alt_query, None), (question, None)):
                    if not q2:
                        continue
                    more = await self._tool_search(q2, p2, n_fetch)
                    if self._norm(q2, more) > score:
                        hits, score, query = more, self._norm(q2, more), q2
                        break
            out.spans.append(Span("retrieve", (s - t0) * 1000, ms(), "work", "", f"score={score:.2f}"))
            # 3. Decide.
            if not hits or score < self.cfg.refuse_score:
                return self._finish(out, t0, "refused", self._refusal_text(question, hits))
            if self.setting.rerank and not lean and len(hits) > self.setting.k:
                try:
                    r, _ = await self._llm(out, t0, deadline, "rerank", [ROUTER_MODEL],
                                           self._rerank_msgs(question, hits), max_tokens=200, expect_out=40,
                                           json_mode=True)
                    order = [int(i) for i in _parse_json(r.text).get("best", []) if str(i).isdigit()]
                    picked = [hits[i - 1] for i in order if 1 <= i <= len(hits)]
                    hits = (picked + [h for h in hits if h not in picked])[: self.setting.k]
                except (Unavailable, LLMError, ValueError):
                    hits = hits[: self.setting.k]
            else:
                hits = hits[: self.setting.k]
            if lean:
                hits = hits[: min(len(hits), self.cfg.lean_k)]  # limit is close: spend fewer input tokens
            # 4. Draft. Fallback models are separate buckets; using one means a weaker answer.
            try:
                r, model = await self._llm(out, t0, deadline, "draft", DRAFT_MODELS, self._draft_msgs(question, hits),
                                           max_tokens=self.cfg.draft_max_tokens, expect_out=350)
            except (Unavailable, LLMError) as e:
                # The person gets cited passages, but no answer, so this is NOT counted as a success.
                # Status records why: a limit (rate_limited), a slow provider (timeout), or a failure (error).
                out.error = str(e)
                out.status = ("rate_limited" if isinstance(e, (Unavailable, RateLimited))
                              else "timeout" if isinstance(e, CallTimeout) else "error")
                return self._finish(out, t0, "retrieval_only", self._retrieval_only_text(hits, e))
            out.draft_model = model
            mode = "full" if model == DRAFT_MODELS[0] and not lean else ("weaker_model" if model != DRAFT_MODELS[0] else "lean")
            draft = normalize_citations(r.text)
            # 5. Check. A deterministic gate always runs; the LLM verifier runs only when there's headroom.
            ok, why = self._check(draft, len(hits))
            if ok and draft.startswith(NOT_IN_PAPERS):
                return self._finish(out, t0, "refused", self._refusal_text(question, hits, draft))
            if ok and self._should_verify(lean, deadline):
                try:
                    v, _ = await self._llm(out, t0, deadline, "verify", [VERIFY_MODEL], self._verify_msgs(question, hits, draft),
                                           max_tokens=300, expect_out=80, json_mode=True)
                    verdict = _parse_json(v.text)
                    if verdict.get("supported") is False:
                        ok, why = False, "verifier: " + "; ".join(map(str, verdict.get("unsupported", [])))[:200]
                except (Unavailable, LLMError):
                    pass  # verification is best-effort; the deterministic gate already passed
            if not ok:
                out.error = why
                return self._finish(out, t0, "refused",
                                    "I couldn't produce an answer I can back up with the papers, so I'm not going to guess. "
                                    + self._sources_hint(hits))
            # 6. Send.
            return self._finish(out, t0, mode, self._render(draft, hits))
        except Unavailable as e:
            out.status, out.error = "rate_limited", str(e)
            return self._finish(out, t0, "busy", self._busy_text(e.wait_s))
        except CallTimeout as e:
            out.status, out.error = "timeout", str(e)
            return self._finish(out, t0, "busy", self._busy_text(10))
        except Exception as e:  # noqa: BLE001
            out.status, out.error = "error", repr(e)
            return self._finish(out, t0, "busy", "Something went wrong on our side. Please try again.")

    def _should_verify(self, lean, deadline):
        if self.cfg.verify == "off" or lean:
            return False
        if self.cfg.verify == "on":
            return True
        head = self.budgets[VERIFY_MODEL].headroom()
        return head["tpm"] > 3000 and deadline - time.monotonic() > 5

    def _finish(self, out: Outcome, t0, mode, text):
        out.mode, out.text = mode, text
        out.first_token_ms = (time.perf_counter() - t0) * 1000  # released after the gate
        out.total_ms = out.first_token_ms
        return out

    # ------------------------------------------------------------------ checks and rendering
    def _check(self, draft: str, n: int) -> tuple[bool, str]:
        if not draft:
            return False, "empty draft"
        if draft.startswith(NOT_IN_PAPERS):
            return True, ""
        cited = [int(x) for x in re.findall(r"\[S(\d+)\]", draft)]
        if not cited:
            return False, "no citations"
        if any(c < 1 or c > n for c in cited):
            return False, f"cites a source that wasn't provided: {sorted(set(cited))} of {n}"
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", draft) if len(x) > 40]
        uncited = [x for x in sentences if "[S" not in x]
        if sentences and len(uncited) / len(sentences) > 0.6:
            return False, "most sentences are uncited"
        return True, ""

    def _render(self, draft: str, hits: list[Hit]) -> str:
        used = sorted({int(x) for x in re.findall(r"\[S(\d+)\]", draft)})
        text = re.sub(r"\[S(\d+)\]", lambda m: f"[{m.group(1)}]", draft)
        refs = "\n".join(f"[{i}] {hits[i - 1].piece.cite()}" for i in used)
        return f"{text}\n\nSources:\n{refs}"

    def _sources_hint(self, hits):
        if not hits:
            return ""
        return "The closest passages I found: " + "; ".join(h.piece.cite() for h in hits[:3]) + "."

    def _refusal_text(self, question, hits, draft=""):
        reason = draft[len(NOT_IN_PAPERS):].strip(" :") if draft else ""
        msg = "The three papers in this library don't answer that, so I won't guess."
        if reason:
            msg += f" ({reason})"
        hint = self._sources_hint(hits)
        return msg + (" " + hint if hint else "")

    def _retrieval_only_text(self, hits, err):
        lines = ["I can't write a full answer right now (our model capacity is used up), but these are the passages "
                 "from the papers that match your question:"]
        for i, h in enumerate(hits[:3], 1):
            lines.append(f"[{i}] {h.piece.cite()}: \"{h.piece.text[:300].strip()}...\"")
        wait = getattr(err, "wait_s", None)
        if wait and wait < 3600:
            lines.append(f"A full answer should be possible again in about {max(1, round(wait / 60))} min.")
        return "\n".join(lines)

    def _busy_text(self, wait_s):
        return (f"We're getting more questions than we can answer well right now. Rather than give you a rushed answer, "
                f"please try again in about {max(5, round(wait_s))} seconds.")

    # ------------------------------------------------------------------ prompts
    def _router_msgs(self, q):
        sys_ = ("You route questions about exactly three papers:\n"
                "attention = 'Attention Is All You Need' (Transformer, 2017)\n"
                "moshi = 'Moshi' (Kyutai speech-text dialogue model, Mimi codec, Helium LM)\n"
                "dynamic_chunking = 'Dynamic Chunking for End-to-End Hierarchical Sequence Modeling' (H-Net)\n"
                "Return JSON only: {\"papers\": [ids likely to contain the answer; [] if unsure], "
                "\"query\": \"keyword search query using the papers' own terminology\", "
                "\"alt_query\": \"a differently-worded second query\"}")
        return [{"role": "system", "content": sys_}, {"role": "user", "content": q}]

    def _rerank_msgs(self, q, hits):
        cands = "\n".join(f"{i}. ({h.piece.cite()}) {h.piece.text[:350]}" for i, h in enumerate(hits, 1))
        return [{"role": "system", "content": f"Pick the {self.setting.k} passages most useful for answering the question. "
                 "Return JSON only: {\"best\": [numbers, best first]}"},
                {"role": "user", "content": f"Question: {q}\n\nPassages:\n{cands}"}]

    def _draft_msgs(self, q, hits):
        src = "\n\n".join(f"[S{i}] ({h.piece.cite()})\n{h.piece.text}" for i, h in enumerate(hits, 1))
        sys_ = ("You answer engineers' questions using ONLY the numbered sources, which are excerpts from three research papers. "
                "Rules: (1) Put a citation like [S2] after every claim. (2) Use numbers exactly as written in the sources. "
                "(3) If the sources don't contain the answer, reply with exactly 'NOT_IN_PAPERS: ' followed by one short "
                "sentence saying what is missing. Don't use outside knowledge and don't guess. "
                "(4) Be concise: at most 5 sentences. Note that PDF extraction can flatten exponents (e.g. '10^20' may "
                "appear as '1020').")
        return [{"role": "system", "content": sys_}, {"role": "user", "content": f"Sources:\n{src}\n\nQuestion: {q}"}]

    def _verify_msgs(self, q, hits, draft):
        src = "\n\n".join(f"[S{i}] {h.piece.text[:900]}" for i, h in enumerate(hits, 1))
        return [{"role": "system", "content": "Check whether every claim in the ANSWER is supported by the cited source. "
                 "Return JSON only: {\"supported\": true|false, \"unsupported\": [short claims that are not supported]}"},
                {"role": "user", "content": f"Sources:\n{src}\n\nQuestion: {q}\n\nANSWER:\n{draft}"}]


def normalize_citations(text: str) -> str:
    """Canonicalise the citation formats models actually emit to [S1].
    Seen on real gpt-oss-120b output: '[ S1 ]' (narrow no-break spaces). Also handles
    '[S1, S3]', '[S1][S2]', '【S1】' and '(S1)'."""
    text = re.sub(r"[   ​]", " ", text).strip()
    text = text.replace("【", "[").replace("】", "]")
    text = re.sub(r"\(\s*(S\s*\d+(?:\s*[,;]\s*S?\s*\d+)*)\s*\)", r"[\1]", text)

    def split_group(m):
        nums = re.findall(r"\d+", m.group(1))
        return "".join(f"[S{n}]" for n in nums)
    return re.sub(r"\[\s*(S\s*\d+(?:\s*[,;\N{EN DASH}-]\s*S?\s*\d+)*)\s*\]", split_group, text)


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
