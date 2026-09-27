"""Vera bot — HTTP server for the magicpin AI challenge judge harness, plus the offline compose() entry point.

Run locally:  .venv/bin/uvicorn bot:app --host 0.0.0.0 --port 8080
(single worker only: all state is in memory)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, Request            # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse      # noqa: E402
from pydantic import BaseModel, Field           # noqa: E402

from vera.composer import compose_async, conversation_id_for, is_customer_facing   # noqa: E402
from vera.demo import router as demo_router      # noqa: E402
from vera.facts import parse_dt                  # noqa: E402
from vera.llm import get_llm                     # noqa: E402
from vera.replies import respond                 # noqa: E402
from vera.store import SCOPES, STORE, Turn       # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vera.bot")

TICK_BUDGET_S = float(os.environ.get("VERA_TICK_BUDGET_S", "8.0"))     # judge budget is 10s
REPLY_BUDGET_S = float(os.environ.get("VERA_REPLY_BUDGET_S", "8.0"))
MAX_ACTIONS_PER_TICK = 20
MAX_UNANSWERED_NUDGES = 3

# Triggers deferred by the one-message-per-recipient rule; reconsidered on later ticks until they expire.
PENDING: dict[str, float] = {}

app = FastAPI(title="Vera — magicpin merchant AI")
app.include_router(demo_router)   # demo UI at / (isolated state; the judge only uses /v1/*)


@app.exception_handler(RequestValidationError)
async def malformed_request(request: Request, exc: RequestValidationError):
    """The testing brief specifies 400 (not FastAPI's default 422) for malformed bodies."""
    details = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors())
    return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request", "details": details})


# ----------------------------------------------------------------------------- offline entry point

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Challenge §7.1 contract: returns body, cta, send_as, suppression_key, rationale."""
    out = asyncio.run(compose_async(category, merchant, trigger, customer, deadline=time.time() + 28))
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}


# ----------------------------------------------------------------------------- health / metadata

@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - STORE.started), "contexts_loaded": STORE.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Team Vera"),
        "team_members": [m.strip() for m in os.environ.get("TEAM_MEMBERS", "").split(",") if m.strip()],
        "model": ",".join(get_llm().models) + " (Groq) + deterministic fallback",
        "approach": ("Fact-sheet grounded composer: deterministic fact extraction per trigger kind -> kind-specific "
                     "LLM prompt -> validator (unit-aware number provenance, taboo, URL, CTA, language, repetition) "
                     "-> one repair retry -> template fallback. Rule-based reply router for auto-reply, opt-out, "
                     "intent-to-action, off-topic and hostility; LLM writes content only."),
        "contact_email": os.environ.get("CONTACT_EMAIL", ""),
        "version": "1.0.0",
        "submitted_at": os.environ.get("SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


# ----------------------------------------------------------------------------- context push

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope",
                                                      "details": f"scope must be one of {list(SCOPES)}"})
    status, current = STORE.put_context(body.scope, body.context_id, body.version, body.payload)
    if status == "stale":
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version",
                                                      "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}",
            "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **({"note": "duplicate version, no-op"} if status == "duplicate" else {})}


# ----------------------------------------------------------------------------- tick

class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = Field(default_factory=list)


def _plan(trigger_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Pick which triggers deserve a message this tick. Returns (candidates, skip_reasons)."""
    skipped, candidates = [], []
    seen_recipients: set[str] = set()
    triggers = [(tid, STORE.get("trigger", tid)) for tid in dict.fromkeys(trigger_ids)]
    triggers = [(tid, t) for tid, t in triggers if t]
    # Most urgent first; among equals, the one expiring soonest.
    triggers.sort(key=lambda x: (-(x[1].get("urgency") or 0),
                                 parse_dt(x[1].get("expires_at")) or datetime.max.replace(tzinfo=timezone.utc)))
    for tid, trg in triggers:
        PENDING.pop(tid, None)          # re-added below only if deferred again
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = STORE.get("merchant", mid)
        category = STORE.category_for(merchant)
        if not merchant or not category:
            skipped.append(f"{tid}: merchant/category context missing")
            continue
        cid = trg.get("customer_id") or (trg.get("payload") or {}).get("customer_id")
        customer = STORE.get("customer", cid) if cid else None
        customer_facing = is_customer_facing(trg, customer)
        if customer_facing and not customer:
            skipped.append(f"{tid}: customer-scoped but customer context not pushed; not guessing")
            continue
        if customer_facing and (customer.get("preferences", {}) or {}).get("reminder_opt_in") is False:
            skipped.append(f"{tid}: customer has not opted in to reminders")
            continue
        mem = STORE.memory(mid)
        if mem.opted_out:
            skipped.append(f"{tid}: merchant opted out")
            continue
        key = trg.get("suppression_key") or tid
        if key in mem.sent_suppression_keys:
            skipped.append(f"{tid}: already sent ({key})")
            continue
        if not customer_facing:
            if mem.unanswered_nudges >= MAX_UNANSWERED_NUDGES:
                skipped.append(f"{tid}: {mem.unanswered_nudges} unanswered nudges; pausing outreach")
                continue
            if any(c.merchant_id == mid and c.status == "waiting" and c.send_as == "vera"
                   for c in STORE.conversations.values()):
                skipped.append(f"{tid}: backing off (merchant conversation in wait state)")
                continue
        recipient = cid if customer_facing else mid
        if recipient in seen_recipients:
            PENDING[tid] = time.time()
            skipped.append(f"{tid}: one message per recipient per tick; deferred to a later tick")
            continue
        seen_recipients.add(recipient)
        candidates.append({"trigger_id": tid, "trigger": trg, "merchant": merchant, "category": category,
                           "customer": customer if customer_facing else None, "merchant_id": mid,
                           "customer_id": cid if customer_facing else None})
        if len(candidates) >= MAX_ACTIONS_PER_TICK:
            break
    return candidates, skipped


@app.post("/v1/tick")
async def tick(body: TickBody):
    started = time.time()
    deadline = started + TICK_BUDGET_S
    now = parse_dt(body.now) or datetime.now(timezone.utc)
    for tid in list(PENDING):
        exp = parse_dt((STORE.get("trigger", tid) or {}).get("expires_at"))
        if exp and exp < now:
            PENDING.pop(tid, None)
    candidates, skipped = _plan(list(body.available_triggers) + [t for t in PENDING if t not in body.available_triggers])
    for s in skipped:
        log.info("tick skip %s", s)

    def prev_bodies(mid):
        return {t.body for c in STORE.conversations.values() if c.merchant_id == mid for t in c.turns if t.role == "vera"}

    async def one(c):
        return await compose_async(c["category"], c["merchant"], c["trigger"], c["customer"], now=now,
                                   deadline=deadline - 0.3, previous_bodies=prev_bodies(c["merchant_id"]))

    tasks = [asyncio.ensure_future(one(c)) for c in candidates]
    done, pending = await asyncio.wait(tasks, timeout=max(0.1, deadline - time.time())) if tasks else (set(), set())
    actions = []
    for c, task in zip(candidates, tasks):
        if task in done and not task.exception():
            msg = task.result()
        else:
            task.cancel()
            msg = await compose_async(c["category"], c["merchant"], c["trigger"], c["customer"], now=now,
                                      use_llm=False, previous_bodies=prev_bodies(c["merchant_id"]))
        conv_id = conversation_id_for(c["trigger"], c["merchant_id"], c["customer_id"])
        n = 2
        base = conv_id
        while conv_id in STORE.conversations:
            conv_id, n = f"{base}_{n}", n + 1
        conv = STORE.conversation(conv_id, c["merchant_id"], c["customer_id"])
        conv.trigger_id, conv.send_as, conv.last_offer = c["trigger_id"], msg["send_as"], msg.get("_offer", "")
        conv.turns.append(Turn("vera", msg["body"]))
        mem = STORE.memory(c["merchant_id"])
        mem.sent_suppression_keys.add(msg["suppression_key"])
        if msg["send_as"] == "vera":
            mem.unanswered_nudges += 1
        mem.last_sent_at = time.time()
        actions.append({
            "conversation_id": conv_id, "merchant_id": c["merchant_id"], "customer_id": c["customer_id"],
            "send_as": msg["send_as"], "trigger_id": c["trigger_id"],
            "template_name": msg["template_name"], "template_params": msg["template_params"],
            "body": msg["body"], "cta": msg["cta"], "suppression_key": msg["suppression_key"],
            "rationale": msg["rationale"],
        })
        log.info("tick send %s via %s [%s]: %s", conv_id, msg["_source"], msg["_language"], msg["body"][:120])
    log.info("tick done: %d actions, %d skipped, %.2fs", len(actions), len(skipped), time.time() - started)
    return {"actions": actions}


# ----------------------------------------------------------------------------- reply

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    started = time.time()
    conv = STORE.conversation(body.conversation_id, body.merchant_id, body.customer_id)
    if body.from_role == "customer" and not conv.turns:
        conv.send_as = "merchant_on_behalf"
    mem = STORE.memory(conv.merchant_id)
    if mem.opted_out and conv.status == "ended":
        return {"action": "end", "rationale": "Merchant previously opted out; staying silent."}
    now = parse_dt(body.received_at)
    try:
        result = await asyncio.wait_for(respond(conv, body.message, now=now, deadline=started + REPLY_BUDGET_S),
                                        timeout=REPLY_BUDGET_S + 0.5)
    except asyncio.TimeoutError:
        result = {"action": "wait", "wait_seconds": 600, "rationale": "Internal timeout composing reply; retrying shortly."}
    log.info("reply %s <- %r -> %s %s (%.2fs)", body.conversation_id, body.message[:80], result["action"],
             (result.get("body") or "")[:100], time.time() - started)
    return result


@app.post("/v1/teardown")
async def teardown():
    STORE.reset()
    PENDING.clear()
    return {"status": "ok", "wiped": True}
