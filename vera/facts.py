"""Deterministic fact extraction: turns the 4 raw contexts into a compact, verifiable fact sheet.

The LLM is only ever shown this fact sheet, and the validator only accepts numbers
that appear in it. That is the main defence against hallucinated data.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

# Trigger kinds whose payload points at a category digest item, and the digest kind to fall back on.
DIGEST_KIND_FOR_TRIGGER = {
    "research_digest": "research",
    "regulation_change": "compliance",
    "cde_opportunity": "cde",
    "supply_alert": "alert",
    "category_trend_movement": "trend",
}

CUSTOMER_KINDS = {
    "recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "appointment_tomorrow",
    "trial_followup", "chronic_refill_due", "wedding_package_followup", "unplanned_slot_open",
}

# Trigger kinds that need the merchant's say-so (YES/STOP) vs. pure information.
INFO_ONLY_KINDS = {"milestone_reached"}

# Plain-language event names for triggers whose payload is empty.
WHY_NOW_PLAIN = {
    "milestone_reached": "their listing just reached a milestone",
    "competitor_opened": "a new competitor listing opened nearby",
    "perf_dip": "their listing numbers dipped this week",
    "perf_spike": "their listing had a strong week",
    "review_theme_emerged": "a theme is repeating in recent reviews",
    "dormant_with_vera": "it's been a while since you last spoke",
    "curious_ask_due": "the weekly check-in on what customers are asking for",
    "festival_upcoming": "a festival is coming up",
    "renewal_due": "their plan renewal is coming up",
    "research_digest": "this week's category digest is out",
    "recall_due": "their routine recall/check-up window is open",
    "appointment_tomorrow": "their appointment is tomorrow",
    "customer_lapsed_soft": "it's been a while since their last visit",
    "chronic_refill_due": "their regular refill is due",
    "trial_followup": "following up after their trial",
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pct(x: Any) -> Optional[str]:
    """0.021 -> '2.1%', -0.5 -> '-50%'."""
    if not isinstance(x, (int, float)):
        return None
    v = round(x * 100, 1)
    return f"{int(v) if v == int(v) else v}%"


def parse_dt(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def nice_date(s: Any) -> Optional[str]:
    dt = parse_dt(s)
    return f"{dt.strftime('%a')} {dt.day} {MONTHS[dt.month - 1]} {dt.year}" if dt else None


def humanize(token: Any) -> str:
    """'6_month_cleaning' -> '6 month cleaning'."""
    return str(token).replace("_", " ").strip()


# ----------------------------------------------------------------------------- names / language

def merchant_salutation(merchant: dict, category_slug: str) -> str:
    ident = merchant.get("identity", {})
    first = (ident.get("owner_first_name") or "").strip()
    if not first:
        return f"{ident.get('name', 'there')} team"
    if category_slug == "dentists":
        return first if first.lower().startswith("dr") else f"Dr. {first}"
    return first


def merchant_language(merchant: dict, category: dict) -> dict:
    """Decide the register for merchant-facing copy.

    Every seed merchant lists 'hi', but a Chennai owner with ['en','ta','hi'] should
    not get heavy Hinglish. We weight by position in the languages list, the
    category's code-mix norm, and how the merchant actually wrote to us before.
    """
    langs = merchant.get("identity", {}).get("languages", []) or ["en"]
    code_mix = category.get("voice", {}).get("code_mix", "")
    merchant_msgs = [t.get("body", "") for t in merchant.get("conversation_history", []) if t.get("from") == "merchant"]
    wrote_hinglish = any(detect_language(m) in ("hinglish", "hindi") for m in merchant_msgs)
    wrote_english = any(detect_language(m) == "english" for m in merchant_msgs)

    hi_rank = langs.index("hi") if "hi" in langs else 99
    if wrote_hinglish or (hi_rank <= 1 and "english_primary" not in code_mix and not wrote_english):
        style = "hinglish"
    else:
        style = "english"
    return {"style": style, "languages": langs,
            "instruction": LANGUAGE_INSTRUCTIONS[style]}


def customer_language(customer: dict) -> dict:
    pref = (customer.get("identity", {}).get("language_pref") or "en").lower()
    if pref in ("hi",):
        style = "hindi"
    elif pref.startswith("hi"):
        style = "hinglish"
    elif pref[:2] in ("ta", "te", "kn", "mr", "bn", "ml", "gu"):
        style = "english_regional"
    else:
        style = "english"
    instr = LANGUAGE_INSTRUCTIONS[style]
    if style == "english_regional":
        greeting = {"ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara", "mr": "Namaskar",
                    "bn": "Nomoshkar", "ml": "Namaskaram", "gu": "Kem cho"}.get(pref[:2], "Namaste")
        instr += f" You may open with the greeting '{greeting}'; everything else in plain English."
    return {"style": style, "pref": pref, "instruction": instr}


LANGUAGE_INSTRUCTIONS = {
    "english": "Write in plain, warm Indian English. No Hindi.",
    "hinglish": ("Write in natural Hindi-English code-mix in Roman script, the way an Indian business owner "
                 "texts: English for numbers, business and technical terms; Hindi for connective phrases "
                 "(e.g. 'aapke', 'abhi', 'kya main', 'bas', 'chahiye'). Roughly 60% English, 40% Hindi."),
    "hindi": ("Write mostly in Hindi using Roman script (Hinglish spelling), keeping medicine names, "
              "numbers and prices in English. Respectful 'aap' form."),
    "english_regional": "Write in plain, warm Indian English.",
}

_HINDI_WORDS = set("""hai hain nahi nahin kya kyu kyun mujhe mera meri mere aap aapka aapke aapki hum humko
karo karna kar karenge chahiye chahte haan ha ji theek thik accha acha bhai abhi baad mein main ek bhi
kaise kab kahan wala wali kuch sab bahut shukriya dhanyavad judna judrna batao bataiye dekho dekhte
lekin aur toh to hoga hogi raha rahi rahe sakte sakti""".split())


def detect_language(text: str) -> str:
    """Cheap per-turn language detector: hindi (Devanagari) | hinglish | english."""
    if not text:
        return "english"
    if re.search(r"[ऀ-ॿ]", text):
        return "hindi"
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return "english"
    hits = sum(1 for w in words if w in _HINDI_WORDS)
    return "hinglish" if hits >= 2 or (hits >= 1 and len(words) <= 4) else "english"


# ----------------------------------------------------------------------------- category helpers

def find_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    digest = category.get("digest", []) or []
    by_id = {d.get("id"): d for d in digest}
    payload = trigger.get("payload", {}) or {}
    for v in payload.values():
        if isinstance(v, str) and v in by_id:
            return by_id[v]
    want = DIGEST_KIND_FOR_TRIGGER.get(trigger.get("kind", ""))
    if want:
        for d in digest:
            if d.get("kind") == want:
                return d
    return None


def month_beats(category: dict, now: datetime) -> list[str]:
    """Seasonal beats whose month_range covers the current month."""
    out = []
    for beat in category.get("seasonal_beats", []) or []:
        rng = beat.get("month_range", "")
        parts = [p.strip()[:3].title() for p in rng.split("-")]
        if not parts or parts[0] not in MONTHS:
            continue
        start = MONTHS.index(parts[0])
        end = MONTHS.index(parts[-1]) if parts[-1] in MONTHS else start
        m = now.month - 1
        inside = start <= m <= end if start <= end else (m >= start or m <= end)
        if inside:
            out.append(f"{rng}: {beat.get('note')}")
    return out


def peer_comparison(merchant: dict, category: dict) -> dict:
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    out: dict[str, Any] = {"peer_scope": humanize(peer.get("scope", ""))}
    ctr, pctr = perf.get("ctr"), peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(pctr, (int, float)) and pctr:
        out["ctr"] = pct(ctr)
        out["peer_avg_ctr"] = pct(pctr)
        out["ctr_vs_peer"] = "below" if ctr < pctr else "above" if ctr > pctr else "at"
    for metric, peer_key in (("views", "avg_views_30d"), ("calls", "avg_calls_30d"), ("directions", "avg_directions_30d")):
        mine, theirs = perf.get(metric), peer.get(peer_key)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)) and theirs:
            out[f"{metric}_30d"] = mine
            out[f"peer_avg_{metric}_30d"] = theirs
    for k in ("avg_rating", "avg_review_count", "avg_photos", "avg_post_freq_days", "retention_6mo_pct"):
        if k in peer:
            out[f"peer_{k}"] = pct(peer[k]) if k.endswith("_pct") else peer[k]
    return out


# ----------------------------------------------------------------------------- fact sheet

def build_facts(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
                now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    slug = category.get("slug") or merchant.get("category_slug", "")
    ident = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    sub = merchant.get("subscription", {}) or {}
    payload = {k: v for k, v in (trigger.get("payload", {}) or {}).items()
               if k not in ("placeholder", "metric_or_topic")}
    is_placeholder = bool((trigger.get("payload") or {}).get("placeholder"))

    offers_active = [o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active"]
    offers_other = [f"{o.get('title')} ({o.get('status')})" for o in merchant.get("offers", []) if o.get("status") != "active"]

    delta = perf.get("delta_7d", {}) or {}
    facts: dict[str, Any] = {
        "today": now.strftime("%a %d %b %Y"),
        "merchant": {
            "business_name": ident.get("name"),
            "salutation": merchant_salutation(merchant, slug),
            "locality": ident.get("locality"), "city": ident.get("city"),
            "google_profile_verified": ident.get("verified"),
            "established_year": ident.get("established_year"),
            "subscription": {k: sub[k] for k in ("status", "plan", "days_remaining", "days_since_expiry") if sub.get(k) is not None},
            "performance_30d": {k: perf[k] for k in ("views", "calls", "directions", "leads") if k in perf},
            "ctr_30d": pct(perf.get("ctr")),
            "change_last_7d": {k.replace("_pct", ""): pct(v) for k, v in delta.items()},
            "active_offers": offers_active,
            "past_offers": offers_other,
            "customer_aggregate": {k: (pct(v) if k.endswith("_pct") and isinstance(v, float) and v <= 1 else v)
                                   for k, v in (merchant.get("customer_aggregate") or {}).items()},
            "signals": [humanize(s) for s in merchant.get("signals", [])],
            "review_themes": [_review_theme(r) for r in merchant.get("review_themes", []) or []],
            "recent_conversation": [
                f"{t.get('from')}: {t.get('body')}" for t in (merchant.get("conversation_history") or [])[-3:]],
        },
        "peer_benchmark": peer_comparison(merchant, category),
        "trigger": {
            "kind": trigger.get("kind"), "source": trigger.get("source"),
            "urgency_1_to_5": trigger.get("urgency"),
            "details": _humanize_payload(payload),
        },
        "category": {
            "slug": slug,
            "voice_tone": humanize(category.get("voice", {}).get("tone", "")),
            "register": humanize(category.get("voice", {}).get("register", "")),
            "vocabulary_ok": (category.get("voice", {}).get("vocab_allowed") or [])[:12],
            "never_say": category.get("voice", {}).get("vocab_taboo") or [],
            "catalog_offers": [o.get("title") for o in category.get("offer_catalog", [])],
            "in_season_now": month_beats(category, now),
            "search_trends": [f"'{t.get('query')}' {pct(t.get('delta_yoy'))} YoY (age {t.get('segment_age')})"
                              for t in (category.get("trend_signals") or [])[:3]],
        },
    }
    if is_placeholder or not payload:
        facts["trigger"]["note"] = ("This trigger carries no event-specific numbers. Still name the event plainly in "
                                    f"sentence 1 ({WHY_NOW_PLAIN.get(trigger.get('kind'), humanize(trigger.get('kind', '')))}), "
                                    "then anchor on the merchant's own views/calls/CTR/offers; do not invent event details.")

    digest_item = find_digest_item(category, trigger)
    if digest_item:
        facts["digest_item"] = {k: digest_item.get(k) for k in
                                ("title", "source", "trial_n", "patient_segment", "summary", "actionable", "date", "credits")
                                if digest_item.get(k) is not None}
    elif trigger.get("kind") in ("curious_ask_due", "dormant_with_vera", "scheduled_recurring", "perf_dip",
                                 "perf_spike", "festival_upcoming", "seasonal_perf_dip", "category_seasonal"):
        # One optional knowledge hook: the most relevant non-compliance digest item.
        for d in category.get("digest", []) or []:
            if d.get("kind") not in ("compliance", "alert"):
                facts["optional_knowledge_hook"] = {k: d.get(k) for k in ("title", "source", "summary") if d.get(k)}
                break

    lib = category.get("patient_content_library") or []
    if lib and (customer or trigger.get("kind") in ("research_digest", "curious_ask_due", "dormant_with_vera")):
        facts["shareable_content_titles"] = [c.get("title") for c in lib[:3]]

    # Derived, pre-computed "why now" numbers so the model never does arithmetic.
    derived = _derive(trigger, payload, merchant, now)
    if derived:
        facts["trigger"]["derived"] = derived

    if customer:
        facts["customer"] = _customer_facts(customer, now)
        # The customer never sees merchant analytics; keep the sheet focused on what they can use.
        for k in ("performance_30d", "ctr_30d", "change_last_7d", "signals", "review_themes",
                  "customer_aggregate", "subscription", "recent_conversation", "google_profile_verified"):
            facts["merchant"].pop(k, None)
        facts.pop("peer_benchmark", None)
        facts.pop("optional_knowledge_hook", None)
        facts["category"].pop("search_trends", None)
        # Customers can only be offered what the business actually runs.
        facts["category"].pop("catalog_offers", None)
        facts["trigger"].get("derived", {}).pop("days_until_due_date", None)
    return facts


def _review_theme(r: dict) -> str:
    s = f"{humanize(r.get('theme'))} ({r.get('sentiment')}, {r.get('occurrences_30d')}x in 30d)"
    if r.get("common_quote"):
        s += f': "{r["common_quote"]}"'
    return s


def _humanize_payload(payload: dict) -> dict:
    out = {}
    for k, v in payload.items():
        if k in ("category", "merchant_id", "customer_id"):
            continue
        if isinstance(v, float) and -1 <= v <= 1 and ("pct" in k or "delta" in k or "uplift" in k):
            out[k] = pct(v)
        elif isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}T", v):
            out[k] = nice_date(v) + (f", {parse_dt(v).strftime('%I:%M%p').lstrip('0').lower()}" if "T00:00:00" not in v else "")
        elif isinstance(v, list) and v and isinstance(v[0], dict) and "label" in v[0]:
            out[k] = [s.get("label") for s in v]
        elif isinstance(v, list):
            out[k] = [_trend_token(x) if isinstance(x, str) else x for x in v]
        elif isinstance(v, str):
            out[k] = humanize(v) if "_" in v and " " not in v and not v.startswith(("d_", "AT")) else v
        else:
            out[k] = v
    return out


def _trend_token(x: str) -> str:
    """'ORS_demand_+40' -> 'ORS demand +40%'; other tokens are just humanized."""
    m = re.match(r"^(.*?)_([+-]\d+(?:\.\d+)?)$", x)
    if m:
        return f"{humanize(m.group(1))} {m.group(2)}%"
    return humanize(x)


def _derive(trigger: dict, payload: dict, merchant: dict, now: datetime) -> dict:
    d: dict[str, Any] = {}
    kind = trigger.get("kind")
    for key in ("deadline_iso", "due_date", "date", "stock_runs_out_iso", "wedding_date"):
        dt = parse_dt(payload.get(key)) if isinstance(payload.get(key), str) and "T" in str(payload.get(key)) \
            else parse_dt(f"{payload.get(key)}T00:00:00+00:00") if isinstance(payload.get(key), str) else None
        if dt:
            days = (dt.date() - now.date()).days
            if 0 <= days <= 400:
                d[f"days_until_{key.replace('_iso', '')}"] = days
    if kind == "milestone_reached":
        now_v, target = payload.get("value_now"), payload.get("milestone_value")
        if isinstance(now_v, (int, float)) and isinstance(target, (int, float)) and target > now_v:
            d["remaining_to_milestone"] = target - now_v
    if kind in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
        base, delta = payload.get("vs_baseline"), payload.get("delta_pct")
        if isinstance(base, (int, float)) and isinstance(delta, (int, float)):
            d["baseline"] = base
            d["current_estimate"] = round(base * (1 + delta))
    if kind == "renewal_due" and isinstance(payload.get("renewal_amount"), (int, float)):
        d["renewal_amount_inr"] = f"₹{payload['renewal_amount']:,}"
    return d


def _customer_facts(customer: dict, now: datetime) -> dict:
    ident = customer.get("identity", {}) or {}
    rel = customer.get("relationship", {}) or {}
    name = ident.get("name") or ""
    addressee, about = name, None
    m = re.match(r"(.+?)\s*\(parent:\s*(.+?)\)", name)
    if m:
        about, addressee = m.group(1).strip(), m.group(2).strip()
    if name.startswith("("):     # "(walk-in, no profile)"
        addressee = ""
    prefs = customer.get("preferences", {}) or {}
    last = parse_dt(f"{rel.get('last_visit')}T00:00:00+00:00") if rel.get("last_visit") else None
    out = {
        "address_as": addressee or "(no name on file — use a neutral greeting)",
        "message_is_about": about,
        "state": humanize(customer.get("state", "")),
        "age_band": ident.get("age_band"),
        "senior_citizen": ident.get("senior_citizen"),
        "last_visit": nice_date(f"{rel.get('last_visit')}T00:00:00+00:00") if rel.get("last_visit") else None,
        "visits_total": rel.get("visits_total"),
        "services_received": [humanize(s) for s in rel.get("services_received", []) if s != "..."],
        "preferences": {k: humanize(v) if isinstance(v, str) else v for k, v in prefs.items()},
        "consent_scope": [humanize(s) for s in (customer.get("consent", {}) or {}).get("scope", [])],
    }
    if last:
        days = (now.date() - last.date()).days
        if 0 < days < 1500:
            out["days_since_last_visit"] = days
            out["months_since_last_visit"] = round(days / 30.4)
    return {k: v for k, v in out.items() if v not in (None, [], {}, "")}


def allowed_numbers(facts: dict) -> dict[str, set[str]]:
    """Numeric tokens in the fact sheet, bucketed by unit so '4.5-star' can't match '4.5%' by accident.

    Buckets: plain (any number), percent (followed by %), rupee (preceded by ₹, or money-ish keys),
    rating (values of keys containing 'rating').
    """
    import json
    text = json.dumps(facts, ensure_ascii=False)
    buckets: dict[str, set[str]] = {"plain": set(), "percent": set(), "rupee": set(), "rating": set()}
    for m in re.finditer(r"(₹\s?)?(\d[\d,]*(?:\.\d+)?)(\s?%)?", text):
        n = normalise_number(m.group(2))
        buckets["plain"].add(n)
        if m.group(1):
            buckets["rupee"].add(n)
        if m.group(3):
            buckets["percent"].add(n)

    def walk(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            n = normalise_number(str(obj))
            if "rating" in key:
                buckets["rating"].add(n)
            if any(w in key for w in ("amount", "price", "value", "fee", "mrp", "ltv")):
                buckets["rupee"].add(n)
    walk(facts)
    return buckets


def normalise_number(tok: str) -> str:
    tok = tok.replace(",", "")
    if "." in tok:
        tok = tok.rstrip("0").rstrip(".")
    return tok
