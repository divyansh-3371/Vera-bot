"""In-memory state: versioned contexts, conversations, and per-merchant engagement memory.

Everything lives in one process (run uvicorn with a single worker). A lock guards
mutations because FastAPI may interleave requests on the event loop around awaits.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Turn:
    role: str            # "vera" | "merchant" | "customer"
    body: str
    ts: float = field(default_factory=time.time)


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: Optional[str]
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    send_as: str = "vera"
    turns: list[Turn] = field(default_factory=list)
    status: str = "open"          # open | waiting | ended
    mode: str = "pitch"           # pitch | action
    auto_reply_count: int = 0
    last_offer: str = ""          # what Vera last proposed (drives action mode)
    language: str = ""            # detected from the latest inbound turn

    def bodies_sent(self) -> set[str]:
        return {t.body.strip() for t in self.turns if t.role == "vera"}


@dataclass
class MerchantMemory:
    """Cross-conversation memory about a merchant (auto-replies, opt-outs, cadence)."""
    opted_out: bool = False
    opted_out_customers: set[str] = field(default_factory=set)   # customers who said STOP to merchant_on_behalf messages
    auto_reply_texts: dict[str, int] = field(default_factory=dict)   # normalised text -> count
    auto_reply_hits: int = 0
    unanswered_nudges: int = 0
    sent_suppression_keys: set[str] = field(default_factory=set)
    last_sent_at: float = 0.0


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.aliases: dict[tuple[str, str], str] = {}
        self.conversations: dict[str, Conversation] = {}
        self.merchants_mem: dict[str, MerchantMemory] = {}
        self.started = time.time()

    # ---------- contexts ----------
    def put_context(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[str, int]:
        """Returns (status, current_version). status: stored | duplicate | stale."""
        with self._lock:
            key = (scope, context_id)
            cur = self.contexts.get(key)
            if cur is not None:
                if cur["version"] == version:
                    return "duplicate", version
                if cur["version"] > version:
                    return "stale", cur["version"]
            entry = {"version": version, "payload": payload, "stored_at": time.time()}
            self.contexts[key] = entry
            # The judge's context_id may be a short form ("m_001_drmeera") while triggers reference the
            # payload's own id ("m_001_drmeera_dentist_delhi"); index under both.
            own_id = payload.get({"category": "slug", "merchant": "merchant_id",
                                  "customer": "customer_id", "trigger": "id"}[scope])
            if own_id and own_id != context_id:
                self.aliases[(scope, own_id)] = context_id
            return "stored", version

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        entry = self.contexts.get((scope, context_id))
        if entry is None and (scope, context_id) in self.aliases:
            entry = self.contexts.get((scope, self.aliases[(scope, context_id)]))
        return entry["payload"] if entry else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        for scope, _ in list(self.contexts.keys()):
            out[scope] = out.get(scope, 0) + 1
        return out

    def category_for(self, merchant: Optional[dict]) -> Optional[dict]:
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug"))

    # ---------- conversations ----------
    def conversation(self, conversation_id: str, merchant_id: Optional[str] = None,
                     customer_id: Optional[str] = None) -> Conversation:
        with self._lock:
            conv = self.conversations.get(conversation_id)
            if conv is None:
                conv = Conversation(conversation_id, merchant_id, customer_id,
                                    send_as="merchant_on_behalf" if customer_id else "vera")
                self.conversations[conversation_id] = conv
            else:
                conv.merchant_id = conv.merchant_id or merchant_id
                conv.customer_id = conv.customer_id or customer_id
            return conv

    def memory(self, merchant_id: Optional[str]) -> MerchantMemory:
        with self._lock:
            key = merchant_id or "_unknown"
            if key not in self.merchants_mem:
                self.merchants_mem[key] = MerchantMemory()
            return self.merchants_mem[key]

    def reset(self) -> None:
        with self._lock:
            self.contexts.clear()
            self.aliases.clear()
            self.conversations.clear()
            self.merchants_mem.clear()


STORE = Store()
