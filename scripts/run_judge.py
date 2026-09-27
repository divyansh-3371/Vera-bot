"""Run magicpin's judge_simulator.py against the local bot using Groq, without editing the provided file.

The judge uses a model the bot doesn't (separate Groq rate bucket) and retries on 429s.
Usage: python scripts/run_judge.py [scenario]   (warmup|phase2_short|auto_reply_hell|intent_transition|hostile|all|full_evaluation)
"""
import os
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

import judge_simulator as js  # noqa: E402

js.BOT_URL = os.environ.get("BOT_URL", "http://localhost:8080")
js.LLM_PROVIDER = "groq"
js.LLM_API_KEY = os.environ["GROQ_API_KEY"]
js.LLM_MODEL = os.environ.get("JUDGE_MODEL", "openai/gpt-oss-20b")
js.TEST_SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "all"

# Groq's edge rejects urllib's default User-Agent with 403; send a normal one.
_urlopen = js.urlrequest.urlopen


def _urlopen_with_ua(req, *a, **kw):
    if hasattr(req, "add_header") and "groq.com" in getattr(req, "full_url", ""):
        req.add_header("User-Agent", "magicpin-judge-sim/1.0")
    return _urlopen(req, *a, **kw)


js.urlrequest.urlopen = _urlopen_with_ua

_orig = js.GroqProvider.complete


def _patient_complete(self, prompt, system=None):
    for attempt in range(8):
        try:
            return _orig(self, prompt, system)
        except urlerror.HTTPError as e:
            if e.code in (429, 503) and attempt < 7:
                time.sleep(8)
                continue
            raise


js.GroqProvider.complete = _patient_complete
js.main()
