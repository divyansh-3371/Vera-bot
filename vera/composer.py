"""compose(category, merchant, trigger, customer?) -> message, with validation and fallback."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .facts import (CUSTOMER_KINDS, INFO_ONLY_KINDS, allowed_numbers, build_facts, customer_language,
                    merchant_language, normalise_number)
from .llm import get_llm
from .prompts import COMPOSER_SYSTEM, CTA_TYPES, composer_user_prompt
from .templates import fallback_message

log = logging.getLogger("vera.composer")

# The dataset is anchored on 2026-04-26 (brief date, IPL trigger date); used when no clock is supplied.
DATASET_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|org|net|io|co)\b)", re.I)
PREAMBLE_RE = re.compile(r"\b(hope you('| a)re (doing )?well|i am reaching out|i'm reaching out|this is vera|i am vera|i'm vera)\b", re.I)


def is_customer_facing(trigger: dict, customer: Optional[dict]) -> bool:
    return bool(customer) or trigger.get("scope") == "customer" or trigger.get("kind") in CUSTOMER_KINDS


def cta_hint(kind: str, facts: dict, customer_facing: bool) -> str:
    details = facts["trigger"].get("details", {})
    if customer_facing:
        if details.get("available_slots") or details.get("next_session_options"):
            return "multi_choice_slot"
        if kind in ("chronic_refill_due", "appointment_tomorrow"):
            return "binary_confirm_cancel"
        return "binary_yes_stop"
    if kind in ("curious_ask_due",):
        return "open_ended"
    if kind in INFO_ONLY_KINDS:
        return "binary_yes_stop or none"
    return "binary_yes_stop"


NUM_CTX_RE = re.compile(r"(₹\s?|Rs\.?\s?|rated\s|rating (?:of\s)?)?(?<![A-Za-z\d])(\d[\d,]*(?:\.\d+)?)"
                        r"(\s?%|\s?-?\s?(?:star|★|⭐|rating)|\s?/\s?5\b)?", re.I)


def fabricated_numbers(body: str, allowed: dict[str, set[str]], strict_plain: bool = True) -> list[str]:
    """Numbers in the body that the fact sheet can't account for, checked per unit."""
    bad = []
    for m in NUM_CTX_RE.finditer(body):
        cur, raw, unit = (m.group(1) or "").strip().lower(), m.group(2), (m.group(3) or "").strip().lower()
        n = normalise_number(raw)
        if cur.startswith(("rated", "rating")):
            unit = "rating"
        if unit == "%":
            ok = n in allowed["percent"]
        elif unit:
            ok = n in allowed["rating"]
        elif cur:   # ₹ / Rs
            ok = n in allowed["rupee"] or n in allowed["plain"] or not strict_plain
        else:
            ok = n in allowed["plain"] or not strict_plain
            if not ok:
                try:
                    ok = float(n) <= 12      # small counts/times Vera proposes ("3 posts", "5 min")
                except ValueError:
                    ok = False
        if not ok:
            bad.append(m.group(0).strip())
    return sorted(set(bad))


def validate(msg: dict, facts: dict, *, previous_bodies: set[str], style: str,
             customer_facing: bool, kind: str = "") -> list[str]:
    """Return a list of problems (empty = OK). Hard problems are prefixed with 'HARD:'."""
    problems: list[str] = []
    body = (msg.get("body") or "").strip()
    if len(body) < 40:
        return ["HARD: body is empty or too short"]
    if URL_RE.search(body):
        problems.append("HARD: contains a URL/domain — remove it")
    low = body.lower()
    for taboo in facts["category"].get("never_say", []):
        phrase = taboo.split(" (")[0].strip().lower()
        if phrase and phrase in low:
            problems.append(f"HARD: uses forbidden phrase '{phrase}'")
    allowed = allowed_numbers(facts)
    bad = fabricated_numbers(body, allowed, strict_plain=kind != "active_planning_intent")
    if bad:
        problems.append(f"HARD: numbers not found in the fact sheet (fabricated?): {bad}")
    if kind == "active_planning_intent":
        # Suggested tier prices must be derived from (i.e. not exceed) a live offer price.
        live = [float(normalise_number(x)) for o in facts["merchant"].get("active_offers", [])
                for x in re.findall(r"₹\s?([\d,]+)", o)]
        for raw in re.findall(r"₹\s?([\d,]+(?:\.\d+)?)", body):
            n = normalise_number(raw)
            if n not in allowed["rupee"] and (not live or float(n) > max(live)):
                problems.append(f"HARD: suggested price ₹{raw} isn't derived from a live offer price {live or '(none)'}")
    if body in previous_bodies:
        problems.append("HARD: identical to a message already sent")
    if PREAMBLE_RE.search(body):
        problems.append("remove the preamble/self-introduction")
    if len(body) > 700:
        problems.append("too long — cut to under 450 characters")
    if msg.get("cta") not in CTA_TYPES:
        problems.append(f"cta must be one of {CTA_TYPES}")
    if msg.get("cta") != "none" and msg.get("cta") != "multi_choice_slot" and low.count("reply ") > 1:
        problems.append("more than one call-to-action — keep a single ask in the last sentence")
    if style == "hinglish" and not re.search(r"\b(aap|aapke|aapka|aapki|kya|hai|hain|main|kar|abhi|bas|chahiye|doon|dein|karein)\b", low):
        problems.append("LANGUAGE: should be natural Hindi-English code-mix (Roman script)")
    active = set(facts["merchant"].get("active_offers", []))
    for title in facts["category"].get("catalog_offers", []):
        i = body.find(title)
        if title not in active and i >= 0 and not re.search(r"(e\.g\.|eg|like|such as|suggest|try|maybe|jaise)\W*$", body[max(0, i - 25):i], re.I):
            problems.append(f"'{title}' is a catalog suggestion, not a live offer — present it as a suggestion (e.g. ...)")
    if customer_facing:
        owner = facts["merchant"].get("salutation", "")
        cust = facts.get("customer", {}).get("address_as", "")
        if owner and owner.split()[-1].lower() in low[:25] and owner.split()[-1].lower() not in cust.lower():
            problems.append(f"HARD: the message greets the merchant owner ('{owner}') — address the customer ('{cust}') instead")
    return problems


_PLAIN_OPENERS = {"your", "the", "a", "an", "this", "these", "it", "it's", "its", "we", "we've", "our", "with", "just",
                  "quick", "great", "good", "nice", "since", "last", "there", "there's", "heads", "time", "thanks",
                  "congrats", "congratulations", "summer", "today", "tomorrow", "yesterday", "as", "following",
                  "aapka", "aapke", "aapki", "aaj", "kal", "abhi", "bas", "is", "yeh", "humare", "hamare"}


def _join(prefix: str, body: str) -> str:
    """Prefix a greeting; lower-case the body's first letter only for plain words (never names like 'Smile Studio')."""
    first = re.split(r"[\s,—–-]", body, maxsplit=1)[0].lower() if body else ""
    if first in _PLAIN_OPENERS:
        body = body[0].lower() + body[1:]
    return prefix + body


def fix_cta_words(body: str) -> str:
    """STOP means opt-out only; never use it for cancel/reschedule."""
    return re.sub(r"\b(reply\s+)?STOP\s+to\s+(cancel|reschedule|skip)", lambda m: f"{m.group(1) or ''}CANCEL to {m.group(2)}", body, flags=re.I)


def ensure_salutation(body: str, facts: dict, customer_facing: bool) -> str:
    """Merchant fit starts with the name (and, for customers, who is writing): fix it in code, not with a retry."""
    if customer_facing:
        name = facts.get("customer", {}).get("address_as", "")
        biz = facts["merchant"].get("business_name") or ""
        key = next((w for w in biz.split() if w.lower().rstrip(".") not in ("dr", "the", "mr", "mrs", "ms")), "")
        has_biz = bool(key) and key.lower().removesuffix("'s") in body.lower()
        if not name or name.startswith("("):
            return body if has_biz or not biz else f"{biz} here — {body}"
        greet_re = re.compile(rf"^(hi|hello|hey|namaste|dear)?\s*{re.escape(name)}(?!\w)[\s,!—–-]*", re.I)
        rest = greet_re.sub("", body, count=1)
        if has_biz and rest != body:
            out = body
        else:
            greeting = "Namaste" if body.lower().startswith("namaste") else "Hi"
            out = _join(f"{greeting} {name}, " + ("" if has_biz else f"{biz} here — "), rest)
        # drop a second greeting of the same name right after the business intro ("... here — Mr. Sharma, aapke")
        return re.sub(rf"((?:here|yahan)\s*[—–-]\s*){re.escape(name)}(?!\w)[\s,]*", r"\1", out, count=1, flags=re.I)
    sal = facts["merchant"].get("salutation", "")
    if not sal or sal.split()[-1].lower() in body[:80].lower():
        return body
    return _join(f"{sal}, ", body)


def template_name(kind: str, customer_facing: bool) -> str:
    return f"{'merchant' if customer_facing else 'vera'}_{kind}_v1"


def conversation_id_for(trigger: dict, merchant_id: str, customer_id: Optional[str]) -> str:
    who = customer_id or merchant_id
    return f"conv_{who}_{trigger.get('kind')}_{trigger.get('id', '')[-12:]}".replace(" ", "_")


async def compose_async(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None, *,
                        now: Optional[datetime] = None, deadline: Optional[float] = None,
                        previous_bodies: Optional[set[str]] = None, use_llm: bool = True) -> dict:
    now = now or DATASET_NOW
    deadline = deadline or (time.time() + 25)
    previous_bodies = previous_bodies or set()
    customer_facing = is_customer_facing(trigger, customer)
    send_as = "merchant_on_behalf" if customer_facing else "vera"
    kind = trigger.get("kind", "generic")

    facts = build_facts(category, merchant, trigger, customer if customer_facing else None, now)
    lang = customer_language(customer) if (customer_facing and customer) else merchant_language(merchant, category)
    facts_json = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    hint = cta_hint(kind, facts, customer_facing)
    user = composer_user_prompt(facts_json, kind, send_as, lang["instruction"], hint)

    llm = get_llm()
    msg, source, problems = None, "fallback", []
    out = await llm.complete_json(COMPOSER_SYSTEM, user, max_tokens=550, deadline=deadline) if use_llm else None
    if out:
        problems = validate(out, facts, previous_bodies=previous_bodies, style=lang["style"], customer_facing=customer_facing, kind=kind)
        # Retries cost a full call against the per-minute token budget, so only repair problems that
        # actually hurt: fabrication/taboo/URL/repeat (HARD) and the wrong language.
        must_fix = [p for p in problems if p.startswith(("HARD", "LANGUAGE"))]
        if problems:
            log.info("compose %s problems (%s): %s", kind, out.get("_model"), problems)
        if not must_fix:
            msg, source = out, out.get("_model", "llm") + ("+soft_issues" if problems else "")
        elif time.time() < deadline - 4:
            retry_user = user + "\n\nYOUR PREVIOUS DRAFT:\n" + json.dumps({"body": out.get("body")}, ensure_ascii=False) + \
                "\n\nFIX THESE PROBLEMS and return the corrected JSON:\n- " + "\n- ".join(problems)
            out2 = await llm.complete_json(COMPOSER_SYSTEM, retry_user, max_tokens=550, deadline=deadline)
            if out2:
                problems2 = validate(out2, facts, previous_bodies=previous_bodies, style=lang["style"], customer_facing=customer_facing, kind=kind)
                if not any(p.startswith("HARD") for p in problems2):
                    msg, source, problems = out2, out2.get("_model", "llm") + "+retry", problems2
        if msg is None and not any(p.startswith("HARD") for p in problems):
            msg, source = out, out.get("_model", "llm") + "+soft_issues"
    if msg is None:
        msg = fallback_message(facts, kind, send_as, lang["style"])
        source = "template"

    body = fix_cta_words(ensure_salutation(msg["body"].strip(), facts, customer_facing))
    cta = msg.get("cta") if msg.get("cta") in CTA_TYPES else "binary_yes_stop"
    params = msg.get("template_params") if isinstance(msg.get("template_params"), list) else []
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": (msg.get("rationale") or "").strip() or f"{kind} trigger for {merchant.get('merchant_id')}",
        "template_name": template_name(kind, customer_facing),
        "template_params": [str(p) for p in params][:5],
        "_offer": msg.get("offer", ""),
        "_source": source,
        "_language": lang["style"],
        "_facts": facts,
    }
