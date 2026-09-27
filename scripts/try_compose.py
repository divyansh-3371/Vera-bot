"""Compose messages for selected test pairs and print them. Usage: python scripts/try_compose.py T01 T05 ..."""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("VERA_WAIT_FOR_BUDGET", "1")
os.environ.setdefault("VERA_DISK_CACHE", str(Path(__file__).resolve().parent.parent / ".cache" / "llm"))

from scripts.dataset import load
from vera.composer import compose_async
from vera.llm import get_llm


async def main(ids):
    cats, M, C, T, pairs = load()
    sel = [p for p in pairs if not ids or p["test_id"] in ids]
    for p in sel:
        m = M[p["merchant_id"]]; t = T[p["trigger_id"]]; c = C.get(p["customer_id"]) if p["customer_id"] else None
        t0 = time.time()
        r = await compose_async(cats[m["category_slug"]], m, t, c, deadline=time.time() + 120)
        print(f"=== {p['test_id']} {t['kind']} | {m['identity']['name']} | {r['send_as']} | {r['_source']} | {r['_language']} | {time.time()-t0:.1f}s")
        print(r["body"]); print(f"  cta={r['cta']} | {r['rationale']}\n")
    print(get_llm().stats)

asyncio.run(main(set(sys.argv[1:])))
