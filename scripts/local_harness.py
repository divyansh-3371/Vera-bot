"""Local end-to-end harness mirroring the judge lifecycle (warmup -> ticks -> replies -> replay scenarios).

Usage: python scripts/local_harness.py [--url http://localhost:8080] [--ticks 3] [--per-tick 5]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.dataset import load  # noqa: E402


def post(c, path, body, budget):
    t = time.time()
    r = c.post(path, json=body, timeout=30)
    dt = time.time() - t
    flag = "  ⚠ OVER BUDGET" if dt > budget else ""
    return r, dt, flag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--ticks", type=int, default=2)
    ap.add_argument("--per-tick", type=int, default=5)
    args = ap.parse_args()
    cats, M, C, T, pairs = load()
    c = httpx.Client(base_url=args.url)

    print("== warmup: pushing base contexts")
    t0 = time.time()
    for scope, items in (("category", cats), ("merchant", M), ("customer", C)):
        for cid, payload in items.items():
            r = c.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": payload,
                                            "delivered_at": "2026-04-26T10:00:00Z"})
            assert r.status_code == 200 and r.json()["accepted"], (scope, cid, r.text)
    print(f"   pushed in {time.time() - t0:.1f}s ->", c.get("/v1/healthz").json()["contexts_loaded"])
    r = c.post("/v1/context", json={"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 0, "payload": {}})
    print("   stale push ->", r.status_code, r.json())
    r = c.post("/v1/context", json={"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    print("   bad scope  ->", r.status_code, r.json())

    # Triggers from the canonical test pairs, pushed then ticked in batches.
    tids = [p["trigger_id"] for p in pairs]
    for tid in tids:
        c.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": T[tid]})
    convs = []
    for i in range(args.ticks):
        batch = tids[i * args.per_tick:(i + 1) * args.per_tick]
        r, dt, flag = post(c, "/v1/tick", {"now": "2026-04-26T10:%02d:00Z" % (i * 5), "available_triggers": batch}, 10)
        acts = r.json()["actions"]
        print(f"\n== tick {i + 1}: {len(batch)} triggers -> {len(acts)} actions in {dt:.2f}s{flag}")
        for a in acts:
            missing = [k for k in ("conversation_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body") if not a.get(k)]
            print(f"-- {a['trigger_id']} [{a['send_as']}, {a['cta']}]{' MISSING ' + str(missing) if missing else ''}\n{a['body']}")
            convs.append(a)

    print("\n== replies on the first merchant-facing conversation")
    conv = next((a for a in convs if a["send_as"] == "vera"), None)
    if conv:
        for turn, msg in enumerate(["Hmm, how much does this cost me?", "Ok let's do it. What's next?"], start=2):
            r, dt, flag = post(c, "/v1/reply", {"conversation_id": conv["conversation_id"], "merchant_id": conv["merchant_id"],
                                                 "from_role": "merchant", "message": msg, "turn_number": turn}, 10)
            print(f"merchant: {msg}\n  bot ({dt:.2f}s{flag}): {json.dumps(r.json(), ensure_ascii=False)}")

    mid = "m_001_drmeera_dentist_delhi"
    scenarios = {
        "auto_reply_hell": ["Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."] * 4,
        "intent_transition": ["Haan interesting hai, par kitna time lagega?", "Aur kya karna padega mujhe?", "Ok let's do it"],
        "hostile_offtopic": ["Why do you keep bothering me, this is useless", "Can you help me file my GST this month?"],
        "hard_no": ["Not interested. Stop messaging me."],
    }
    for name, msgs in scenarios.items():
        print(f"\n== replay: {name}")
        cid = f"conv_replay_{name}_{int(time.time())}"
        for turn, msg in enumerate(msgs, start=2):
            r, dt, flag = post(c, "/v1/reply", {"conversation_id": cid, "merchant_id": mid, "from_role": "merchant",
                                                 "message": msg, "turn_number": turn}, 10)
            d = r.json()
            print(f"merchant: {msg}\n  bot ({dt:.2f}s{flag}): {d['action']}"
                  f"{' ' + str(d.get('wait_seconds')) + 's' if d['action'] == 'wait' else ''} {d.get('body', '')}")
            if d["action"] == "end":
                break
    print("\n== healthz", c.get("/v1/healthz").json())


if __name__ == "__main__":
    main()
