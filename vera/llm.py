"""Groq LLM client with a per-model token budget, model failover, and a response cache.

Groq's free tier caps each model at ~8K tokens/minute, with a separate bucket per
model. We keep a sliding 60s window of our own usage per model and route each call
to the first model with headroom; a 429 puts that model on cooldown and we fail
over. If every model is exhausted or the deadline is near, callers get None and
use their deterministic fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("vera.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# gpt-oss-20b was tested and dropped: it fabricated offers; the deterministic template is safer.
DEFAULT_MODELS = "openai/gpt-oss-120b,qwen/qwen3.8-27b"

# Extra params per model family (keep hidden reasoning short / off to save tokens + latency).
MODEL_PARAMS = {
    "openai/gpt-oss": {"reasoning_effort": "low"},
    "qwen/": {"reasoning_effort": "none"},
}


def _model_params(model: str) -> dict:
    for prefix, params in MODEL_PARAMS.items():
        if model.startswith(prefix):
            return params
    return {}


class _Budget:
    def __init__(self, tpm: int) -> None:
        self.tpm = tpm
        self.events: deque[list] = deque()   # [ts, tokens], mutable so a reservation can be corrected
        self.cooldown_until = 0.0

    def used(self) -> int:
        cutoff = time.time() - 60
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        return sum(n for _, n in self.events)

    def can_spend(self, n: int) -> bool:
        return time.time() >= self.cooldown_until and self.used() + n <= self.tpm

    def spend(self, n: int) -> list:
        event = [time.time(), n]
        self.events.append(event)
        return event


class LLMClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("GROQ_API_KEY", "")
        self.models = [m.strip() for m in os.environ.get("VERA_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
        tpm = int(os.environ.get("VERA_TPM_PER_MODEL", "7200"))   # 10% under the 8K cap
        self.budgets = {m: _Budget(tpm) for m in self.models}
        self.cache: dict[str, dict] = {}
        self.disk_cache = Path(os.environ["VERA_DISK_CACHE"]) if os.environ.get("VERA_DISK_CACHE") else None
        if self.disk_cache:
            self.disk_cache.mkdir(parents=True, exist_ok=True)
        self._client: Optional[httpx.AsyncClient] = None
        self.stats = {"calls": 0, "cache_hits": 0, "failovers": 0, "exhausted": 0, "errors": 0}
        # Offline batch mode (submission generation): wait for budget instead of giving up.
        self.wait_for_budget = os.environ.get("VERA_WAIT_FOR_BUDGET") == "1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0),
                                             headers={"Authorization": f"Bearer {self.api_key}"})
        return self._client

    @staticmethod
    def _key(messages: list[dict], max_tokens: int) -> str:
        raw = json.dumps({"m": messages, "t": max_tokens}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _disk_get(self, key: str) -> Optional[dict]:
        if not self.disk_cache:
            return None
        p = self.disk_cache / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _disk_put(self, key: str, value: dict) -> None:
        if self.disk_cache:
            (self.disk_cache / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 550,
                            deadline: Optional[float] = None) -> Optional[dict]:
        """Return the parsed JSON object from the model, or None if unavailable in time."""
        if not self.enabled:
            return None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = self._key(messages, max_tokens)
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]
        hit = self._disk_get(key)
        if hit is not None and hit.get("_model") in self.models:   # ignore results from models no longer in use
            self.stats["cache_hits"] += 1
            self.cache[key] = hit
            return hit

        # Groq charges prompt tokens + the full max_tokens reservation against the per-minute budget.
        estimate = int((len(system) + len(user)) / 3.3) + max_tokens
        while True:
            result = await self._try_models(messages, key, estimate, max_tokens, deadline)
            if result is not None or not self.wait_for_budget:
                break
            if deadline and time.time() > deadline - 3:
                break
            await asyncio.sleep(5)
        if result is None:
            self.stats["exhausted"] += 1
        return result

    async def _try_models(self, messages, key, estimate, max_tokens, deadline) -> Optional[dict]:
        for i, model in enumerate(self.models):
            remaining = (deadline - time.time()) if deadline else 20.0
            if remaining < 2.0:
                break
            budget = self.budgets[model]
            if not budget.can_spend(estimate):
                continue
            if i > 0:
                self.stats["failovers"] += 1
            reservation = budget.spend(estimate)   # reserve; corrected after the call
            try:
                self.stats["calls"] += 1
                resp = await self._http().post(GROQ_URL, timeout=min(remaining - 0.5, 20.0), json={
                    "model": model, "messages": messages, "temperature": 0, "seed": 7,
                    "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                    **_model_params(model),
                })
            except (httpx.TimeoutException, httpx.TransportError) as e:
                log.warning("llm %s transport error: %s", model, e)
                self.stats["errors"] += 1
                continue
            if resp.status_code == 429:
                retry = resp.headers.get("retry-after")
                budget.cooldown_until = time.time() + (float(retry) if retry and retry.replace(".", "").isdigit() else 20)
                log.warning("llm %s rate limited; cooling down", model)
                continue
            if resp.status_code != 200:
                log.warning("llm %s HTTP %s: %s", model, resp.status_code, resp.text[:300])
                self.stats["errors"] += 1
                continue
            data = resp.json()
            prompt_tokens = data.get("usage", {}).get("prompt_tokens")
            if prompt_tokens:
                reservation[1] = int(prompt_tokens) + max_tokens
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            parsed = _parse_json(content)
            if parsed is None:
                log.warning("llm %s returned unparseable content: %s", model, content[:200])
                self.stats["errors"] += 1
                continue
            parsed["_model"] = model
            self.cache[key] = parsed
            self._disk_put(key, parsed)
            return parsed
        return None


def _parse_json(text: str) -> Optional[dict]:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                obj = json.loads(m.group())
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


_LLM: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _LLM
    if _LLM is None:
        _LLM = LLMClient()
    return _LLM


async def gather_with_deadline(coros, deadline: float):
    """Run coroutines concurrently; results for ones not done by the deadline are None."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    timeout = max(0.1, deadline - time.time())
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    return [t.result() if t in done and not t.exception() else None for t in tasks]
