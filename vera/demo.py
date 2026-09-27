"""Demo UI backend (/ and /demo/*). Uses its own Store so demo clicks never touch the judge's state."""
from __future__ import annotations

import glob
import json
import subprocess
import sys
import time
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .composer import compose_async
from .replies import respond
from .store import Store, Turn

ROOT = Path(__file__).resolve().parent.parent
EXPANDED = ROOT / "dataset" / "expanded"
DEMO_STORE = Store()
_PAIRS: list[dict] = []

router = APIRouter()


def _ensure_loaded() -> None:
    if _PAIRS:
        return
    if not (EXPANDED / "test_pairs.json").exists():
        subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"), "--out", str(EXPANDED)],
                       check=True, capture_output=True)
    for scope, folder, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                               ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
        for f in glob.glob(str(EXPANDED / folder / "*.json")):
            d = json.load(open(f, encoding="utf-8"))
            DEMO_STORE.put_context(scope, d[key], 1, d)
    _PAIRS.extend(json.load(open(EXPANDED / "test_pairs.json", encoding="utf-8"))["pairs"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@router.get("/demo/scenarios")
async def scenarios():
    _ensure_loaded()
    out = []
    for p in _PAIRS:
        m = DEMO_STORE.get("merchant", p["merchant_id"]) or {}
        t = DEMO_STORE.get("trigger", p["trigger_id"]) or {}
        c = DEMO_STORE.get("customer", p.get("customer_id")) if p.get("customer_id") else None
        out.append({"test_id": p["test_id"], "kind": t.get("kind"), "merchant": m.get("identity", {}).get("name"),
                    "category": m.get("category_slug"), "city": m.get("identity", {}).get("city"),
                    "customer": (c or {}).get("identity", {}).get("name")})
    return {"scenarios": out}


class StartBody(BaseModel):
    test_id: str


@router.post("/demo/start")
async def start(body: StartBody):
    _ensure_loaded()
    pair = next((p for p in _PAIRS if p["test_id"] == body.test_id), None)
    if not pair:
        raise HTTPException(404, "unknown test_id")
    merchant = DEMO_STORE.get("merchant", pair["merchant_id"])
    trigger = DEMO_STORE.get("trigger", pair["trigger_id"])
    customer = DEMO_STORE.get("customer", pair["customer_id"]) if pair.get("customer_id") else None
    category = DEMO_STORE.category_for(merchant)
    DEMO_STORE.merchants_mem.pop(pair["merchant_id"], None)      # every demo run starts with a clean slate
    t0 = time.time()
    msg = await compose_async(category, merchant, trigger, customer, deadline=time.time() + 20)
    conv_id = f"demo_{body.test_id}_{int(time.time() * 1000)}"
    conv = DEMO_STORE.conversation(conv_id, pair["merchant_id"], pair.get("customer_id") if customer else None)
    conv.trigger_id, conv.send_as, conv.last_offer = pair["trigger_id"], msg["send_as"], msg.get("_offer", "")
    conv.turns.append(Turn("vera", msg["body"]))
    ident = merchant.get("identity", {})
    return {
        "conversation_id": conv_id,
        "message": {k: msg[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale", "template_name")},
        "source": msg["_source"], "language": msg["_language"], "latency_ms": int((time.time() - t0) * 1000),
        "context": {
            "merchant": ident.get("name"), "owner": ident.get("owner_first_name"),
            "place": f"{ident.get('locality')}, {ident.get('city')}", "category": merchant.get("category_slug"),
            "languages": ident.get("languages"), "trigger_kind": trigger.get("kind"),
            "trigger_payload": {k: v for k, v in (trigger.get("payload") or {}).items() if k != "placeholder"},
            "customer": (customer or {}).get("identity", {}).get("name"),
            "active_offers": [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"],
        },
    }


class DemoReply(BaseModel):
    conversation_id: str
    message: str


@router.post("/demo/reply")
async def demo_reply(body: DemoReply):
    conv = DEMO_STORE.conversations.get(body.conversation_id)
    if conv is None:
        raise HTTPException(404, "start a scenario first")
    if conv.status == "ended":
        return {"action": "end", "rationale": "Conversation already closed.", "latency_ms": 0}
    t0 = time.time()
    result = await respond(conv, body.message, deadline=time.time() + 12, store=DEMO_STORE)
    return {**result, "latency_ms": int((time.time() - t0) * 1000)}
