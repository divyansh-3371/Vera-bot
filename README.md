# Vera — magicpin merchant AI (challenge submission)

**Deliverables:** `bot.py` (HTTP bot + `compose()`), `submission.jsonl` (30 test pairs), `conversation_handlers.py` (`respond()`), this README. A small demo chat UI is served at `/` (isolated state; the judge only uses `/v1/*`).

## Approach

```
contexts ──► fact sheet (code) ──► kind-specific LLM prompt ──► validator ──► repair retry ──► template fallback
                                                                  │
inbound reply ──► rule router (auto-reply / opt-out / intent / off-topic / hostile / later) ──► LLM writes words only
```

1. **Deterministic fact sheet, not raw JSON.** Code extracts only what's relevant per trigger: the resolved digest item, the peer gap (CTR 2.1% vs 3.0%), 7-day deltas, active vs catalog offers, in-season beats, and customer consent/preferences. It also pre-computes the "why now" numbers (days to deadline, baseline→now, reviews to milestone), so the LLM never does arithmetic. Empty trigger payloads are flagged explicitly ("name the event, don't invent details").
2. **One prompt per trigger family** (30 kinds): research gets a source citation, a dip gets a quantified loss plus a likely cause, a seasonal dip reassures and points to retention, IPL on a weekend recommends delivery over dine-in, and planning intent delivers a draft instead of asking questions.
3. **Validator.** Every number in a message must exist in the fact sheet *with the same unit*, so "4.5-star" can't borrow "4.5%". It also rejects taboo words, URLs, multiple CTAs, repeated messages, a missing Hinglish mix, customer messages that greet the owner, and catalog offers presented as live. Only hard problems trigger a retry. The name and business are inserted in code rather than spending a retry on them.
4. **Reply router.** Rules decide the move, instantly and deterministically:
   - Auto-reply (canned phrases, or the same text repeated across conversations): one owner nudge → wait 24h → end.
   - "Stop" or spam: end and opt the merchant out.
   - Weak yes plus a question: answer it.
   - Strong commitment ("let's do it", "judna hai"): switch to **action mode**, which delivers the draft and bans qualifying phrases.
   - Off-topic (GST): polite decline and steer back.
   - Abuse: one apology offering STOP, then end.
   - Language is detected on every turn.
5. **Restraint in `/tick`.**
   - At most one message per recipient per tick; extra triggers wait in a pending queue.
   - Most urgent first; deduplicated on `suppression_key`.
   - Skip customers without reminder opt-in, opted-out merchants, and merchants with 3 unanswered nudges or in a wait state.
   - Never guess when a customer context is missing.

## Tradeoffs

- **Groq free tier = 8K tokens/min per model.** Calls are spread across `gpt-oss-120b` and `qwen3.8-27b` using a sliding-window token budget, with a deterministic template when both are exhausted. The bot therefore never times out or sends an empty message, but under heavy load some messages are template-quality. `gpt-oss-20b` was tested and dropped because it invented offers.
- **Rules over LLM for conversation control:** predictable and instant, but novel phrasings can misroute. The LLM still writes every content reply.
- **Trigger expiry is not enforced** for triggers the judge lists as available, since the judge's list is the source of truth. Deferred triggers do respect `expires_at`.
- **In-memory state, single worker**, as the brief allows. A restart loses the test state.

## What additional context would have helped most

1. A merchant **rating / review count** field. Peer stats have `avg_rating`, but merchants don't, so rating comparisons are impossible without inventing numbers.
2. **Open slots and appointment times** for generated customer triggers (`appointment_tomorrow`, `recall_due`), plus a consistent dataset clock. Some triggers imply April 2026 and others November.
3. Which **past Vera messages the merchant ignored**, to vary the lever instead of just the wording.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "GROQ_API_KEY=gsk_..." > .env
python dataset/generate_dataset.py --out dataset/expanded      # needed by the scripts
scripts/serve.sh                                               # bot on :8080 (1 worker); demo UI at http://localhost:8080/
.venv/bin/python scripts/local_harness.py                      # warmup + ticks + replay scenarios
.venv/bin/python scripts/run_judge.py all                      # magicpin's judge_simulator via Groq
.venv/bin/python scripts/make_submission.py                    # regenerate submission.jsonl
.venv/bin/python -m pytest -q tests/                           # offline rule tests
```
Windows (PowerShell): `python -m venv .venv; .venv\Scripts\pip install -r requirements.txt`, then `.venv\Scripts\python -m uvicorn bot:app --port 8080 --workers 1` and use `.venv\Scripts\python` for the scripts above.
Optional env: `TEAM_NAME`, `TEAM_MEMBERS` (comma-separated), `CONTACT_EMAIL`, `VERA_MODELS`, `VERA_TPM_PER_MODEL`.
