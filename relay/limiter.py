"""Client-side mirror of the provider's rate limits, kept honest by its headers.

Phase A of the probe showed Groq's limits are *continuously refilling buckets*,
not fixed windows:
  * after 1 request, x-ratelimit-reset-requests = 1m26.4s = 86400 s / 1000 RPD
  * after 100 tokens, x-ratelimit-reset-tokens = 750 ms = 100 / 8000 TPM * 60 s
So the daily budget never "resets at midnight"; it trickles back at 1 request
every 86.4 s. We model each limit as a token bucket and, after every response,
overwrite our estimate with the server's `remaining` value (limits can change
without notice; the server is the source of truth).

Before a call, `reserve()` answers: can this model take a call of ~N tokens
before my deadline? If not, the caller degrades (smaller model, skip a step,
retrieval-only reply) instead of queueing past the person's patience.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional


def parse_duration(s: Optional[str]) -> Optional[float]:
    """Groq durations look like '750ms', '2.947s', '1m26.4s', '2h3m1s'."""
    if not s:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


class Bucket:
    def __init__(self, capacity: float, per_seconds: float):
        self.capacity = capacity
        self.rate = capacity / per_seconds
        self.level = capacity
        self.t = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        self.level = min(self.capacity, self.level + (now - self.t) * self.rate)
        self.t = now

    def wait_for(self, amount: float) -> float:
        """Seconds until `amount` is available (0 if now). inf if it never fits."""
        self._refill()
        if amount > self.capacity:
            return float("inf")
        return max(0.0, (amount - self.level) / self.rate)

    def take(self, amount: float):
        self._refill()
        self.level -= amount

    def sync(self, remaining: float, limit: Optional[float] = None):
        self._refill()
        if limit and abs(limit - self.capacity) > 0.5:  # the provider changed the limit
            per = self.capacity / self.rate
            self.capacity, self.rate = limit, limit / per
        self.level = min(self.capacity, remaining)


@dataclass
class ModelLimits:
    rpm: float = 30
    tpm: float = 8000
    rpd: float = 1000
    tpd: float = 200000


class Unavailable(Exception):
    """The model can't serve this call before the deadline. The caller should degrade."""

    def __init__(self, model, wait_s, reason):
        super().__init__(f"{model}: {reason} (wait {wait_s:.1f}s)")
        self.model, self.wait_s, self.reason = model, wait_s, reason


class ModelBudget:
    def __init__(self, model: str, limits: ModelLimits):
        self.model = model
        self.rpm = Bucket(limits.rpm, 60)
        self.tpm = Bucket(limits.tpm, 60)
        self.rpd = Bucket(limits.rpd, 86400)
        self.tpd = Bucket(limits.tpd, 86400)
        self.blocked_until = 0.0  # set from retry-after on a 429
        self.lock = asyncio.Lock()
        self.pending_tokens = 0.0
        self.pending_calls = 0

    def wait_needed(self, est_tokens: float) -> tuple[float, str]:
        waits = {
            "rpm": self.rpm.wait_for(1),
            "tpm": self.tpm.wait_for(est_tokens),
            "rpd": self.rpd.wait_for(1),
            "tpd": self.tpd.wait_for(est_tokens),
            "retry_after": max(0.0, self.blocked_until - time.monotonic()),
        }
        reason = max(waits, key=waits.get)
        return waits[reason], reason

    def queued_wait(self, est_tokens: float) -> tuple[float, str]:
        """Like wait_needed, but counting the callers already waiting ahead of us for this bucket."""
        wait, reason = self.wait_needed(est_tokens + self.pending_tokens)
        rpm_wait = self.rpm.wait_for(1 + self.pending_calls)
        return (rpm_wait, "rpm") if rpm_wait > wait else (wait, reason)

    async def reserve(self, est_tokens: float, deadline: float) -> float:
        """Wait (if it fits the deadline) and take budget. Returns seconds waited.
        Fails fast when the queue ahead of us means the deadline can't be met."""
        wait, reason = self.queued_wait(est_tokens)
        if time.monotonic() + wait > deadline:
            raise Unavailable(self.model, wait, reason)
        self.pending_tokens += est_tokens
        self.pending_calls += 1
        try:
            return await self._reserve_locked(est_tokens, deadline)
        finally:
            self.pending_tokens -= est_tokens
            self.pending_calls -= 1

    async def _reserve_locked(self, est_tokens: float, deadline: float) -> float:
        waited = 0.0
        async with self.lock:
            while True:
                wait, reason = self.wait_needed(est_tokens)
                if wait <= 0:
                    self.rpm.take(1); self.tpm.take(est_tokens); self.rpd.take(1); self.tpd.take(est_tokens)
                    return waited
                if time.monotonic() + wait > deadline:
                    raise Unavailable(self.model, wait, reason)
                await asyncio.sleep(min(wait, 0.25) + 0.01)
                waited += min(wait, 0.25) + 0.01

    def settle(self, est_tokens: float, actual_tokens: float):
        """Correct the estimate once usage is known."""
        self.tpm.take(actual_tokens - est_tokens)
        self.tpd.take(actual_tokens - est_tokens)

    def observe_headers(self, h: dict):
        def num(k):
            try:
                return float(h[k])
            except (KeyError, ValueError):
                return None
        rem_t, lim_t = num("x-ratelimit-remaining-tokens"), num("x-ratelimit-limit-tokens")
        rem_r, lim_r = num("x-ratelimit-remaining-requests"), num("x-ratelimit-limit-requests")
        if rem_t is not None:
            self.tpm.sync(rem_t, lim_t)
        if rem_r is not None:
            self.rpd.sync(rem_r, lim_r)
        ra = h.get("retry-after")
        if ra:
            try:
                self.blocked_until = max(self.blocked_until, time.monotonic() + float(ra))
            except ValueError:
                pass

    def observe_429(self, body: str, headers: dict):
        """Learn limits that appear only in the 429 body (probe phase E: qwen's ITPM of 7,000 isn't in any header)."""
        self.observe_headers(headers)
        m = re.search(r"on ([a-z ]+) \((\w+)\): Limit (\d+), Used (\d+)", body or "")
        if not m:
            return m
        kind, limit, used = m.group(2), float(m.group(3)), float(m.group(4))
        if kind == "ITPM":
            # Model the input-token limit by shrinking our TPM bucket to it. That's conservative but simple.
            if limit < self.tpm.capacity:
                self.tpm.sync(max(0.0, limit - used), limit)
        elif kind == "TPM":
            self.tpm.sync(max(0.0, limit - used), limit)
        elif kind == "RPM":
            self.rpm.sync(max(0.0, limit - used), limit)
        elif kind == "RPD":
            self.rpd.sync(max(0.0, limit - used), limit)
        elif kind == "TPD":
            self.tpd.sync(max(0.0, limit - used), limit)
        return kind

    def headroom(self) -> dict:
        for b in (self.rpm, self.tpm, self.rpd, self.tpd):
            b._refill()
        return {"rpm": self.rpm.level, "tpm": self.tpm.level, "rpd": self.rpd.level, "tpd": self.tpd.level}


class Budgets:
    def __init__(self, limits: dict[str, ModelLimits]):
        self.by_model = {m: ModelBudget(m, l) for m, l in limits.items()}

    def __getitem__(self, model) -> ModelBudget:
        return self.by_model[model]

    def daily_fraction_left(self, model) -> float:
        b = self.by_model[model]
        b.rpd._refill(); b.tpd._refill()
        return min(b.rpd.level / b.rpd.capacity, b.tpd.level / b.tpd.capacity)
