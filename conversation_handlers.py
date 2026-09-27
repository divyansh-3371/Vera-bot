"""Optional multi-turn contract (challenge brief §7.4): respond(state, merchant_message) -> dict.

`state` is a dict describing the conversation so far:
    {
      "conversation_id": "conv_...",
      "merchant": {...MerchantContext...},
      "category": {...CategoryContext...},          # optional; looked up from merchant if already stored
      "customer": {...CustomerContext...} | None,   # set for customer-facing conversations
      "trigger": {...TriggerContext...} | None,
      "turns": [{"from": "vera"|"merchant"|"customer", "body": "..."}],
      "last_offer": "draft 3 Google posts"           # optional
    }
Returns the same shape as POST /v1/reply: {"action": "send"|"wait"|"end", "body"?, "cta"?, "wait_seconds"?, "rationale"}.
The HTTP server (bot.py) uses the same `vera.replies.respond` underneath.
"""
from __future__ import annotations

import asyncio
import time

from vera.replies import respond as _respond
from vera.store import STORE, Turn


def respond(state: dict, merchant_message: str) -> dict:
    merchant = state.get("merchant") or {}
    mid = merchant.get("merchant_id")
    if merchant:
        STORE.put_context("merchant", mid, 1, merchant)
    if state.get("category"):
        STORE.put_context("category", state["category"].get("slug"), 1, state["category"])
    customer = state.get("customer")
    if customer:
        STORE.put_context("customer", customer.get("customer_id"), 1, customer)
    trigger = state.get("trigger")
    if trigger:
        STORE.put_context("trigger", trigger.get("id"), 1, trigger)

    conv_id = state.get("conversation_id") or f"conv_{mid}_{int(time.time())}"
    conv = STORE.conversation(conv_id, mid, customer.get("customer_id") if customer else None)
    if not conv.turns:
        conv.turns = [Turn(t.get("from", "vera"), t.get("body", "")) for t in state.get("turns", [])]
        conv.trigger_id = trigger.get("id") if trigger else None
        conv.last_offer = state.get("last_offer", "")
    return asyncio.run(_respond(conv, merchant_message, deadline=time.time() + 25))
