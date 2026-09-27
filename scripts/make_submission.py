"""Generate submission.jsonl for the 30 canonical test pairs (dataset/expanded/test_pairs.json).

Waits for Groq rate-limit headroom instead of falling back to templates, so every line gets the
full LLM path when possible. Output rows: test_id, body, cta, send_as, suppression_key, rationale
(+ trigger/merchant/customer ids and the source used, for traceability).
Usage: python scripts/make_submission.py [--out submission.jsonl]
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
os.environ.setdefault("VERA_WAIT_FOR_BUDGET", "1")
os.environ.setdefault("VERA_DISK_CACHE", str(ROOT / ".cache" / "llm"))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="   %(name)s: %(message)s")
logging.getLogger("vera.composer").setLevel(logging.INFO)
from scripts.dataset import load  # noqa: E402
from vera.composer import compose_async  # noqa: E402
from vera.llm import get_llm  # noqa: E402


async def main(out_path: Path):
    cats, M, C, T, pairs = load()
    rows = []
    for p in pairs:
        m, t = M[p["merchant_id"]], T[p["trigger_id"]]
        c = C.get(p["customer_id"]) if p.get("customer_id") else None
        t0 = time.time()
        r = await compose_async(cats[m["category_slug"]], m, t, c, deadline=time.time() + 180)
        rows.append({"test_id": p["test_id"], "body": r["body"], "cta": r["cta"], "send_as": r["send_as"],
                     "suppression_key": r["suppression_key"], "rationale": r["rationale"],
                     "trigger_id": p["trigger_id"], "merchant_id": p["merchant_id"], "customer_id": p.get("customer_id"),
                     "template_name": r["template_name"], "template_params": r["template_params"]})
        print(f"{p['test_id']} {t['kind']:<24} {r['_source']:<32} {time.time() - t0:5.1f}s  {r['body'][:80]!r}", flush=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(rows)} rows -> {out_path}\n{get_llm().stats}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    asyncio.run(main(Path(ap.parse_args().out)))
