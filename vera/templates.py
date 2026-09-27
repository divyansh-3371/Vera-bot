"""Deterministic fallback composer. Used when the LLM is unavailable, too slow, or fails validation.

Every sentence is built only from the fact sheet, so it can never hallucinate.
Quality is lower than the LLM path but always on-voice, specific and valid.
"""
from __future__ import annotations

from typing import Any, Optional


def _hl(style: str, english: str, hinglish: str) -> str:
    return hinglish if style == "hinglish" else english


def _first(xs: list) -> Optional[Any]:
    return xs[0] if xs else None


def fallback_message(facts: dict, kind: str, send_as: str, style: str) -> dict:
    if send_as == "merchant_on_behalf":
        return _customer_fallback(facts, kind, style)
    return _merchant_fallback(facts, kind, style)


def _merchant_fallback(f: dict, kind: str, style: str) -> dict:
    m, trig, peer = f["merchant"], f["trigger"], f.get("peer_benchmark", {})
    d, derived = trig.get("details", {}), trig.get("derived", {})
    sal = m.get("salutation", "Hi")
    offer = _first(m.get("active_offers", []))
    catalog = _first(f["category"].get("catalog_offers", []))
    digest = f.get("digest_item")
    perf = m.get("performance_30d", {})
    cta = "binary_yes_stop"

    if kind == "active_planning_intent":
        topic = d.get("intent_topic") or "the plan you asked about"
        base = offer or catalog
        lines = [f"{sal}, here's a starter draft for your {topic} — edit freely:"]
        if base:
            lines.append(f"• Anchor: {base}")
        lines += [_hl(style, "• Bulk tiers: lower per-unit price as quantity goes up (you set the numbers)",
                      "• Bulk tiers: quantity badhe toh per-unit price kam (numbers aap final karein)"),
                  _hl(style, "• Ordering: WhatsApp the day before; confirmed slot + delivery window",
                      "• Ordering: ek din pehle WhatsApp; confirmed slot + delivery window"),
                  _hl(style, "Want me to draft the outreach message to send next? Reply YES.",
                      "Next step ke liye outreach message draft kar doon? Reply YES.")]
        body = "\n".join(lines)
        offer_txt = f"draft outreach for the {topic}"
    elif kind == "supply_alert" and (d.get("molecule") or d.get("affected_batches")):
        batches = ", ".join(d.get("affected_batches") or [])
        rx = m.get("customer_aggregate", {}).get("chronic_rx_count")
        body = (f"{sal}, urgent: recall on {d.get('molecule', 'a molecule you stock')}"
                + (f" batches {batches}" if batches else "") + (f" by {d.get('manufacturer')}" if d.get("manufacturer") else "") + ". "
                + (f"With {rx} chronic-Rx customers on your list, some may hold these batches. " if rx else "Check shelf stock and recent dispensing. ")
                + _hl(style, "Want me to draft the customer notice + replacement-pickup steps? Reply YES.",
                      "Customer notice + replacement-pickup steps draft kar doon? Reply YES."))
        offer_txt = "draft the recall customer notice and replacement workflow"
    elif digest and kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert", "category_trend_movement"):
        hook = f"{sal}, {digest.get('source')}: {digest.get('title')}."
        detail = digest.get("summary", "")
        detail = detail.split(". ")[0].rstrip(".") + "." if detail else ""
        action = digest.get("actionable") or ""
        ask = _hl(style, "Want me to prepare a 1-page summary + next steps for your team? Reply YES.",
                  "Kya main aapke liye 1-page summary + next steps ready kar doon? Reply YES.")
        body = " ".join(x for x in [hook, detail, (action.rstrip(".") + ".") if action else "", ask] if x)
        offer_txt = "prepare a 1-page summary and next steps"
    elif kind in ("perf_dip", "seasonal_perf_dip") and d.get("metric"):
        base = derived.get("baseline")
        now_est = derived.get("current_estimate")
        change = f"{d.get('metric')} {d.get('delta_pct')} over the last {d.get('window', '7d')}"
        if base is not None and now_est is not None:
            change += f" ({base} → ~{now_est})"
        if kind == "seasonal_perf_dip":
            why = _hl(style, "This matches the usual seasonal lull, so no panic — retention matters more than new spend right now.",
                      "Yeh normal seasonal lull hai — abhi naye spend se zyada retention pe focus karna better hai.")
        elif peer.get("ctr_vs_peer") == "below":
            why = f"Your CTR is {peer.get('ctr')} vs peer avg {peer.get('peer_avg_ctr')}" + \
                  (", and there's no active offer on your listing." if not offer else ".")
        else:
            why = _hl(style, "A fresh post + a clear service-price offer usually recovers this fastest.",
                      "Ek fresh post + clear service-price offer se yeh sabse jaldi recover hota hai.")
        fix = offer or (f"e.g. {catalog}" if catalog else "a fresh offer")
        ask = _hl(style, f"Want me to push a Google post with {fix} today? Reply YES.",
                  f"Kya main aaj hi {fix} ke saath Google post live kar doon? Reply YES.")
        body = f"{sal}, your {change}. {why} {ask}"
        offer_txt = f"publish a Google post featuring {fix}"
    elif kind == "perf_spike" and d.get("metric"):
        driver = d.get("likely_driver")
        body = (f"{sal}, nice — {d.get('metric')} up {d.get('delta_pct')} in the last {d.get('window', '7d')}"
                + (f", most likely from your {driver}." if driver else ".")
                + _hl(style, " Let's double down while it's working. Want me to draft a follow-up post in the same style? Reply YES.",
                      " Jab tak momentum hai, isi style mein ek aur post daal dete hain. Draft kar doon? Reply YES."))
        offer_txt = "draft a follow-up post in the same style"
    elif kind == "milestone_reached" and d.get("milestone_value"):
        rem = derived.get("remaining_to_milestone")
        lead = (f"{sal}, you're at {d.get('value_now')} {d.get('metric', '').replace('count', '').strip() or 'reviews'} — just {rem} away from {d.get('milestone_value')}."
                if rem else f"{sal}, you just crossed {d.get('milestone_value')} {d.get('metric', '')}.")
        body = lead + _hl(style, " A short thank-you note to recent happy customers usually closes the gap within a week. Want me to draft it? Reply YES.",
                          " Recent happy customers ko ek chhota thank-you note bhejenge toh gap ek hafte mein close ho jata hai. Draft kar doon? Reply YES.")
        offer_txt = "draft a thank-you + review-request note"
    elif kind == "review_theme_emerged" and d.get("theme"):
        quote = f' ("{d.get("common_quote")}")' if d.get("common_quote") else ""
        body = (f"{sal}, {d.get('occurrences_30d')} reviews in the last 30 days mention {d.get('theme')}{quote}. "
                + _hl(style, "Worth fixing before it shows up in your rating. I can draft polite replies to these reviews + one ops tip. Reply YES.",
                      "Rating pe asar aane se pehle fix karna better hai. Main in reviews ke polite replies + ek ops tip draft kar sakti hoon. Reply YES."))
        offer_txt = "draft review replies and one ops fix"
    elif kind == "competitor_opened" and d.get("competitor_name"):
        body = (f"{sal}, {d.get('competitor_name')} opened {d.get('distance_km')} km from you"
                + (f" with {d.get('their_offer')}" if d.get("their_offer") else "") + ". "
                + (f"Your {offer} is already live — " if offer else "")
                + _hl(style, "let's make sure your listing shows why patients/customers choose you. Want me to refresh your top Google post this week? Reply YES.",
                      "listing pe clearly dikhna chahiye ki customers aapko kyun choose karte hain. Is hafte top Google post refresh kar doon? Reply YES."))
        offer_txt = "refresh the top Google post against the new competitor"
    elif kind == "renewal_due":
        amt = derived.get("renewal_amount_inr", "")
        body = (f"{sal}, your {d.get('plan', m.get('subscription', {}).get('plan', ''))} plan renews in {d.get('days_remaining', m.get('subscription', {}).get('days_remaining'))} days"
                + (f" ({amt})" if amt else "") + f". Last 30 days: {perf.get('views', '—')} views, {perf.get('calls', '—')} calls. "
                + _hl(style, "Want me to renew it so your listing stays live without a gap? Reply YES.",
                      "Listing bina gap ke live rahe, uske liye renew kar doon? Reply YES."))
        offer_txt = "process the plan renewal"
    elif kind == "gbp_unverified":
        up = d.get("estimated_uplift_pct")
        body = (f"{sal}, your Google profile is still unverified"
                + (f" — verified listings typically see ~{up} more visibility" if up else "") + ". "
                + f"Verification is via {d.get('verification_path', 'a quick Google check')}. "
                + _hl(style, "Want me to walk you through it in 5 minutes? Reply YES.",
                      "5 minute mein step-by-step karwa doon? Reply YES."))
        offer_txt = "walk through Google profile verification"
    elif kind in ("curious_ask_due", "scheduled_recurring"):
        body = (f"{sal}, quick one — which service are customers asking about most this week? "
                + _hl(style, "Tell me and I'll turn it into a Google post + a ready WhatsApp reply for price enquiries.",
                      "Bata dijiye, main usse ek Google post + price enquiries ke liye ready WhatsApp reply bana doongi."))
        cta = "open_ended"
        offer_txt = "turn the top service into a Google post and reply template"
    elif kind == "category_seasonal" and d.get("trends"):
        trends = ", ".join(d["trends"][:3])
        body = (f"{sal}, {d.get('season', 'this season')} demand shift is showing up: {trends}. "
                + _hl(style, "Worth moving the rising items to the front shelf and your Google listing this week. Want me to draft the post + shelf checklist? Reply YES.",
                      "Rising items ko front shelf aur Google listing pe is hafte laana worth hai. Post + shelf checklist draft kar doon? Reply YES."))
        offer_txt = "draft the seasonal post and shelf checklist"
    else:
        # Generic: anchor on the merchant's own numbers vs peers, with a kind-specific why-now lead.
        lead = f"{sal}, "
        if kind == "festival_upcoming" and d.get("festival"):
            lead += f"{d.get('festival')} is on {d.get('date')}. "
        elif kind in KIND_LEADS:
            lead += _hl(style, KIND_LEADS[kind][0], KIND_LEADS[kind][1]) + " "
        stat = f"Last 30 days: {perf.get('views', '—')} views, {perf.get('calls', '—')} calls"
        if peer.get("ctr"):
            stat += f", CTR {peer.get('ctr')} vs peer avg {peer.get('peer_avg_ctr')}"
        stat += "."
        idea = offer or (f"e.g. {catalog}" if catalog else "a service-price offer")
        body = lead + stat + _hl(style, f" Want me to set up a post with {idea} to lift calls? Reply YES.",
                                 f" Calls badhane ke liye {idea} wala post set kar doon? Reply YES.")
        offer_txt = f"set up a Google post featuring {idea}"

    return {"body": body.strip(), "cta": cta, "offer": offer_txt,
            "template_params": [sal, kind.replace("_", " "), offer_txt],
            "rationale": f"Deterministic fallback for {kind}: anchored on trigger details and merchant's own numbers."}


# Why-now leads for triggers whose payload carries no details (stated only from the trigger kind itself).
KIND_LEADS = {
    "dormant_with_vera": ("it's been a while — quick update on your listing.", "kaafi time ho gaya — aapki listing ka quick update."),
    "competitor_opened": ("heads-up: a new competitor listing just opened near you.", "heads-up: aapke paas ek naya competitor listing khula hai."),
    "perf_dip": ("your listing numbers dipped this week.", "is hafte aapki listing ke numbers thode gire hain."),
    "perf_spike": ("your listing had a strong week.", "is hafte aapki listing ne accha perform kiya."),
    "milestone_reached": ("you've hit a new milestone on your listing — congrats!", "aapki listing ne naya milestone cross kiya — congrats!"),
    "review_theme_emerged": ("a pattern is showing up in your recent reviews.", "aapke recent reviews mein ek pattern dikh raha hai."),
    "festival_upcoming": ("a festival window is coming up.", "festival season aa raha hai."),
    "renewal_due": ("your plan renewal is coming up.", "aapka plan renewal aane wala hai."),
    "research_digest": ("this week's category digest is out.", "is hafte ka category digest aa gaya hai."),
}


def _customer_fallback(f: dict, kind: str, style: str) -> dict:
    m, c, trig = f["merchant"], f.get("customer", {}), f["trigger"]
    d = trig.get("details", {})
    name = c.get("address_as", "")
    greet = ("Namaste" if style == "hindi" else "Hi") + (f" {name}" if name and not name.startswith("(") else "")
    biz = m.get("business_name", "")
    offer = _first(m.get("active_offers", []))
    about = c.get("message_is_about")
    slots = d.get("available_slots") or d.get("next_session_options") or []

    if kind == "chronic_refill_due" and d.get("molecule_list"):
        meds = ", ".join(d["molecule_list"])
        body = (f"{greet}, {biz} here. Your monthly medicines ({meds}) run out on {d.get('stock_runs_out_iso', 'soon')}. "
                + ("Delivery to your saved address is available. " if d.get("delivery_address_saved") else "")
                + (f"{offer} applies. " if offer else "")
                + "Reply CONFIRM and we'll pack the same refill.")
        return _c(body, "binary_confirm_cancel", greet, "dispatch the refill", kind)
    if slots:
        opts = " or ".join(f"{i+1}) {s}" for i, s in enumerate(slots[:3]))
        what = humanize_service(d.get("service_due")) or "next session"
        body = (f"{greet}, {biz} here. " + (f"{about}'s " if about else "Your ") + f"{what} is due. "
                + (f"{offer}. " if offer else "") + f"Slots ready: {opts}. Reply with the number that suits you.")
        return _c(body, "multi_choice_slot", greet, "book one of the listed slots", kind)
    if kind == "appointment_tomorrow":
        body = f"{greet}, {biz} here — a reminder of your appointment with us tomorrow. Reply YES to confirm, or tell us a better time."
        return _c(body, "binary_confirm_cancel", greet, "confirm the appointment", kind)
    last = c.get("last_visit")
    body = (f"{greet}, {biz} here. " + (f"It's been a while since your last visit on {last}. " if last else "It's been a while since we saw you. ")
            + (f"{offer} is on right now. " if offer else "")
            + "Reply YES and we'll hold a slot for you this week — no commitment.")
    return _c(body, "binary_yes_stop", greet, "hold a slot this week", kind)


def humanize_service(s: Optional[str]) -> str:
    return (s or "").replace("_", " ")


def _c(body: str, cta: str, greet: str, offer: str, kind: str) -> dict:
    return {"body": body, "cta": cta, "offer": offer, "template_params": [greet, kind.replace("_", " "), offer],
            "rationale": f"Deterministic customer-facing fallback for {kind} built only from trigger + customer facts."}
