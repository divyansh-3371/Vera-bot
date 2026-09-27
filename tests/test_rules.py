"""Offline tests for the deterministic parts (no network): classifier, validator, greeting fixes, store, planner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from vera.composer import DATASET_NOW, ensure_salutation, fabricated_numbers, validate  # noqa: E402
from vera.facts import allowed_numbers, build_facts, detect_language, _trend_token  # noqa: E402
from vera.replies import classify  # noqa: E402
from vera.store import Store  # noqa: E402

CATEGORY = {
    "slug": "dentists",
    "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed", "100% safe"], "code_mix": "hindi_english_natural"},
    "offer_catalog": [{"title": "Dental Cleaning @ ₹299"}, {"title": "Teeth Whitening @ ₹1,499"}],
    "peer_stats": {"avg_ctr": 0.03, "avg_views_30d": 1820},
    "digest": [{"id": "d_1", "kind": "research", "title": "3-mo fluoride recall", "source": "JIDA Oct 2026, p.14",
                "trial_n": 2100, "summary": "38% lower caries recurrence."}],
}
MERCHANT = {
    "merchant_id": "m_1", "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar",
                 "city": "Delhi", "languages": ["en", "hi"]},
    "performance": {"views": 2410, "calls": 18, "ctr": 0.021},
    "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
    "customer_aggregate": {"high_risk_adult_count": 124},
}
TRIGGER = {"id": "t1", "kind": "research_digest", "scope": "merchant", "payload": {"top_item_id": "d_1"}}


@pytest.mark.parametrize("text,expected", [
    ("Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.", "auto_reply"),
    ("Aapki jaankari ke liye bahut-bahut shukriya. Main team tak pahuncha deti hoon.", "auto_reply"),
    ("Not interested. Stop messaging me.", "opt_out"),
    ("Stop messaging me. This is useless spam.", "opt_out"),
    ("no thanks", "not_interested"),
    ("Why do you keep bothering me, this is useless", "hostile"),
    ("Btw can you also help me with my GST filing this month?", "off_topic"),
    ("Ok lets do it. Whats next?", "accept"),
    ("Mujhe magicpin judrna hai.", "accept"),
    ("Yes please send the abstract", "accept"),
    ("haan kar do", "accept"),
    ("2", "accept"),
    ("Haan interesting hai, par kitna time lagega?", "question"),
    ("Sounds good but what does it cost?", "question"),
    ("busy right now, call later", "later"),
    # edge cases: negations and words that only look like a routing keyword
    ("don't stop, go ahead", "accept"),
    ("stop worrying, let's do it", "accept"),
    ("please stop", "opt_out"),
    ("I'm not busy, tell me more", "engage"),
    ("Will the heatwave weather affect my sales?", "question"),
    ("you idiots", "hostile"),
    ("this is irritating", "hostile"),
    ("मुझे नहीं चाहिए", "not_interested"),
    ("मैसेज मत भेजो", "opt_out"),
])
def test_classify(text, expected):
    assert classify(text) == expected


def test_detect_language():
    assert detect_language("Haan theek hai, kar do") == "hinglish"
    assert detect_language("Please send the details") == "english"
    assert detect_language("हाँ ठीक है") == "hindi"


def test_trend_token():
    assert _trend_token("ORS_demand_+40") == "ORS demand +40%"
    assert _trend_token("cold_cough_demand_-60") == "cold cough demand -60%"


def facts():
    return build_facts(CATEGORY, MERCHANT, TRIGGER, None, DATASET_NOW)


def test_fabrication_is_unit_aware():
    allowed = allowed_numbers(facts())
    assert fabricated_numbers("JIDA p.14: 2,100-patient trial, 38% lower, your 124 high-risk adults", allowed) == []
    assert fabricated_numbers("your 4.5-star rating", allowed) == ["4.5-star"]
    assert fabricated_numbers("calls up 55%", allowed) == ["55%"]
    assert fabricated_numbers("draft 3 posts in 5 min", allowed) == []      # small proposal numbers are fine


def test_validate_flags_url_taboo_and_repeat():
    body = "Dr. Meera, JIDA Oct 2026 p.14 is out — results guaranteed. See www.example.com. Want the summary? Reply YES."
    problems = validate({"body": body, "cta": "binary_yes_stop"}, facts(), previous_bodies={body},
                        style="english", customer_facing=False)
    joined = " ".join(problems)
    assert "URL" in joined and "guaranteed" in joined and "identical" in joined


def test_customer_message_must_not_greet_owner():
    cust = {"customer_id": "c1", "identity": {"name": "Priya", "language_pref": "hi-en mix"},
            "relationship": {}, "state": "lapsed_soft", "preferences": {}, "consent": {}}
    f = build_facts(CATEGORY, MERCHANT, {"kind": "recall_due", "scope": "customer", "payload": {}}, cust, DATASET_NOW)
    problems = validate({"body": "Dr. Meera, aapka cleaning due hai. Reply YES to book.", "cta": "binary_yes_stop"}, f,
                        previous_bodies=set(), style="hinglish", customer_facing=True)
    assert any("greets the merchant owner" in p for p in problems)
    assert "catalog_offers" not in f["category"]          # customers only get live offers


def test_ensure_salutation():
    f = {"merchant": {"business_name": "Dr. Meera's Dental Clinic", "salutation": "Dr. Meera"},
         "customer": {"address_as": "Priya"}}
    assert ensure_salutation("Your cleaning is due.", f, True).startswith("Hi Priya, Dr. Meera's Dental Clinic here — your")
    assert ensure_salutation("Hi Priya, Meera's clinic here.", f, True) == "Hi Priya, Meera's clinic here."
    assert ensure_salutation("Your CTR is 2.1%.", {"merchant": {"salutation": "Dr. Meera"}}, False) == "Dr. Meera, your CTR is 2.1%."


def test_store_versioning_and_alias():
    s = Store()
    assert s.put_context("merchant", "m_1_short", 2, {"merchant_id": "m_1_full"}) == ("stored", 2)
    assert s.put_context("merchant", "m_1_short", 2, {}) == ("duplicate", 2)
    assert s.put_context("merchant", "m_1_short", 1, {}) == ("stale", 2)
    assert s.get("merchant", "m_1_full") == {"merchant_id": "m_1_full"}


def test_bugfixes_from_demo_run():
    from vera.composer import fix_cta_words
    f = {"merchant": {"business_name": "Apollo Health Plus Pharmacy", "salutation": "Ramesh"},
         "customer": {"address_as": "Mr. Sharma"}}
    out = ensure_salutation("Hi Mr. Sharma, Apollo Health Plus Pharmacy here — Mr. Sharma, aapke refill due.", f, True)
    assert out.count("Mr. Sharma") == 1
    assert ensure_salutation("Smile Studio opened 1.3km away.", {"merchant": {"salutation": "Dr. Meera"}}, False) \
        == "Dr. Meera, Smile Studio opened 1.3km away."
    assert "STOP" not in fix_cta_words("Confirm, or reply STOP to cancel.")
    allowed = {"plain": {"4.4"}, "percent": set(), "rupee": set(), "rating": {"4.4"}}
    assert fabricated_numbers("Your 4.8 rating; rated 4.9; 4.7/5", allowed) == ["4.7/5", "4.8 rating", "rated 4.9"]
    assert fabricated_numbers("peer avg rating of 4.4", allowed) == []


def test_reply_fallbacks_never_repeat():
    from vera.replies import _fallback_variants
    v = _fallback_variants("ANSWER", "draft 3 posts", "Dr. Meera", "hinglish", False)
    assert len(set(v)) == len(v) >= 2
    assert all("go ahead with draft" not in x for x in v)


def test_send_never_repeats_verbatim():
    from vera.replies import _send
    from vera.store import Conversation
    conv = Conversation("c1", "m_1")
    bodies = [_send(conv, "I'll check and update you here.", "open_ended", "")["body"] for _ in range(8)]
    assert len(set(bodies)) == len(bodies)


def test_customer_stop_does_not_opt_out_merchant():
    import asyncio
    from vera.replies import respond
    store = Store()
    store.put_context("merchant", "m_1", 1, MERCHANT)
    store.put_context("category", "dentists", 1, CATEGORY)
    conv = store.conversation("cc1", "m_1", "c_1")
    out = asyncio.run(respond(conv, "STOP", store=store))
    assert out["action"] == "end"
    mem = store.memory("m_1")
    assert not mem.opted_out and "c_1" in mem.opted_out_customers


def test_offer_normalisation_and_unsupported_claims():
    from vera.replies import UNSUPPORTED_CLAIM, normalise_offer
    assert normalise_offer("Drafting the Google post for your review") == "draft the Google post for your review"
    assert normalise_offer("I'll publish the offer.") == "publish the offer"
    assert normalise_offer("Draft 3 posts") == "draft 3 posts"
    assert UNSUPPORTED_CLAIM.search("It's included in your active Pro plan, so there is no extra cost.")
    assert UNSUPPORTED_CLAIM.search("Reply CONFIRM and I'll send you the live link")
    assert not UNSUPPORTED_CLAIM.search("I'll draft the post for you to review.")
