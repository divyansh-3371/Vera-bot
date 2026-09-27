"""Score submission.jsonl with the judge_simulator's own LLM rubric (Groq), printing reasons for weak dimensions.

Usage: python scripts/score.py [submission.jsonl] [--model openai/gpt-oss-120b] [--only T01,T05]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
import httpx  # noqa: E402
import judge_simulator as js  # noqa: E402
from scripts.dataset import load  # noqa: E402

_urlopen = js.urlrequest.urlopen
js.urlrequest.urlopen = lambda req, *a, **k: (req.add_header("User-Agent", "magicpin-judge-sim/1.0"), _urlopen(req, *a, **k))[1]


class FullContextScorer(js.LLMScorer):
    """Same rubric and output format as judge_simulator, but the judge sees the full 4 contexts (as the real judge does)."""

    def __init__(self, llm):
        super().__init__(llm, None)

    def score(self, action, category, merchant, trigger, customer=None):
        voice = {k: category.get("voice", {}).get(k) for k in ("tone", "register", "vocab_taboo")}
        digest = [{k: d.get(k) for k in ("id", "title", "source", "trial_n", "summary", "date", "credits")}
                  for d in category.get("digest", [])]
        cat = {"slug": category.get("slug"), "voice": voice, "offer_catalog": [o["title"] for o in category.get("offer_catalog", [])],
               "peer_stats": category.get("peer_stats"), "digest": digest,
               "seasonal_beats": category.get("seasonal_beats"), "trend_signals": category.get("trend_signals")}
        prompt = f"""SCORE THIS MESSAGE. Judge fabrication ONLY against the full contexts below: a fact that
appears anywhere in them (including peer_stats, customer_aggregate, review_themes, conversation_history,
digest, customer relationship) is NOT fabricated. Numbers derived by simple arithmetic from them are fine.

=== CATEGORY CONTEXT ===
{json.dumps(cat, ensure_ascii=False)}

=== MERCHANT CONTEXT ===
{json.dumps(merchant, ensure_ascii=False)}

=== TRIGGER CONTEXT ===
{json.dumps(trigger, ensure_ascii=False)}

=== CUSTOMER CONTEXT ===
{json.dumps(customer, ensure_ascii=False) if customer else 'None (merchant-facing)'}

=== BOT'S MESSAGE ===
Body: "{action.get('body', '')}"
CTA: {action.get('cta', 'none')}
Send As: {action.get('send_as', 'vera')}
Rationale: {action.get('rationale', '')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""
        for attempt in range(12):
            try:
                r = httpx.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                               headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                               json={"model": self.llm.model, "temperature": 0, "max_tokens": 900,
                                     "reasoning_effort": "low", "response_format": {"type": "json_object"},
                                     "messages": [{"role": "system", "content": self.SYSTEM},
                                                  {"role": "user", "content": prompt}]})
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("retry-after", 10)) + 1)
                    continue
                r.raise_for_status()
                return self._parse_response(r.json()["choices"][0]["message"]["content"], action)
            except Exception as e:
                print(f"   judge error: {e}")
                time.sleep(5)
        return self._fallback_score(action)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--model", default="openai/gpt-oss-120b")
    ap.add_argument("--only", default="")
    ap.add_argument("--limited", action="store_true",
                    help="use judge_simulator's limited-context prompt (default: full context, like the real judge)")
    args = ap.parse_args()
    cats, M, C, T, _ = load()
    llm = js.GroqProvider(os.environ["GROQ_API_KEY"], args.model)
    scorer = js.LLMScorer(llm, None) if args.limited else FullContextScorer(llm)
    js.print_llm = lambda *_: None
    rows = [json.loads(line) for line in open(args.path, encoding="utf-8")]
    only = set(filter(None, args.only.split(",")))
    totals = []
    for r in rows:
        if only and r["test_id"] not in only:
            continue
        m = M[r["merchant_id"]]
        for attempt in range(10):
            try:
                s = scorer.score(r, cats[m["category_slug"]], m, T[r["trigger_id"]], C.get(r.get("customer_id") or ""))
                break
            except urlerror.HTTPError:
                time.sleep(8)
        if "Fallback" in (s.specificity_reason or ""):
            time.sleep(10)
            s = scorer.score(r, cats[m["category_slug"]], m, T[r["trigger_id"]], C.get(r.get("customer_id") or ""))
        totals.append(s.total)
        dims = [("spec", s.specificity, s.specificity_reason), ("cat", s.category_fit, s.category_fit_reason),
                ("merch", s.merchant_fit, s.merchant_fit_reason), ("trig", s.decision_quality, s.decision_quality_reason),
                ("eng", s.engagement_compulsion, s.engagement_reason)]
        print(f"{r['test_id']} {T[r['trigger_id']]['kind']:<24} {s.total:>2}/50  " + " ".join(f"{n}={v}" for n, v, _ in dims))
        for n, v, why in dims:
            if v < 8:
                print(f"      {n}: {why}")
    if totals:
        print(f"\nAVERAGE {sum(totals) / len(totals):.1f}/50 over {len(totals)} messages")


main()
