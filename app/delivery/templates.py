"""Telegram message texts (HTML parse mode).

An offer message = a short personal intro + a fixed offer box. The intro may later be written by the AI;
the box (headline, terms, coupon) is always built here from the rule-checked Candidate, so the AI can never
change what is actually being offered.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from app.rules.engine import Candidate

INTROS = {
    "cart_recovery": "🛒 <b>{first}, your cart misses you!</b>\n"
                     "You left {cat} items worth {cart_value} behind. Finish your order and save:",
    "win_back": "💛 <b>We miss you, {first}!</b>\n"
                "It's been a while. Here's something special to welcome you back:",
    "birthday": "🎂 <b>Happy birthday week, {first}!</b>\nA little treat from all of us at {store}:",
    "second_order": "👋 <b>Great to have you, {first}!</b>\nLoved your first order? Here's a thank-you for the next one:",
    "points_expiry": "⏳ <b>{first}, {expiring} of your points expire on {expiry_date}.</b>\n"
                     "Use them before they're gone, and we'll add a bonus:",
    "deal_flash": "⚡ <b>Deal of the day, {first}!</b>\nToday only, on your favourite {cat}:",
    "loyalty_bonus": "🌟 <b>Thank you, {first}!</b>\n"
                     "You're one of our favourite shoppers, so here's a little extra on your next order:",
    "free_delivery": "🚚 <b>On us, {first}!</b>\nSpotted something you like? Your next order ships free:",
}
TYPE_INTROS = {
    "percent_off": "🎁 <b>A special offer for you, {first}!</b>",
    "flat_off": "🎁 <b>A special offer for you, {first}!</b>",
    "free_delivery": "🚚 <b>Free delivery for you, {first}!</b>",
    "bonus_points": "⭐ <b>Bonus points for you, {first}!</b>",
}
FOOTER = "<i>Demo store · synthetic shopper profile · no real purchase</i>"


def rs(v: float) -> str:
    return f"₹{v:,.0f}"


class _Safe(dict):
    def __missing__(self, key):
        return "…"


def template_intro(c: Candidate, ctx: dict) -> str:
    """ctx holds raw values (first, store, cat, cart_value, expiring, expiry_date); they are HTML-escaped here."""
    text = INTROS.get(c.offer_key, TYPE_INTROS.get(c.type, TYPE_INTROS["percent_off"]))
    return text.format_map(_Safe({k: escape(str(v)) for k, v in ctx.items() if v is not None}))


def deal_intro(deal_name: str, first_name: str) -> str:
    return f"🎉 <b>{escape(deal_name)}</b>\n{escape(first_name)}, here's your personal deal:"


def offer_box(c: Candidate, coupon: str) -> str:
    return (f"🎁 <b>{escape(c.headline)}</b>\n"
            f"<i>{escape(c.terms)}</i>\n"
            f"Code: <code>{escape(coupon)}</code>")


def offer_message(intro_html: str, c: Candidate, coupon: str) -> str:
    return f"{intro_html}\n\n{offer_box(c, coupon)}\n\n{FOOTER}"


def welcome(store: str, tg_first: str) -> str:
    return (f"👋 <b>Hi {escape(tg_first)}, welcome to {escape(store)}!</b>\n\n"
            "This is a demo of a personalised offer engine. You'll be matched with a <b>synthetic shopper</b> "
            "(made-up name, orders and carts), and the engine will pick the offer that makes sense for them, "
            "within the store's rules.\n\n"
            "Pick the kind of shopper you'd like to be:")


def profile_card(cust: dict, f: dict, seg_label: str, seg_description: str, category_labels: dict) -> str:
    cat = lambda k: category_labels.get(k, k)  # noqa: E731
    lines = [
        "👤 <b>Your shopper profile</b> <i>(synthetic)</i>",
        f"<b>{escape(cust['name'])}</b> · {escape(cust['city'])} · {cust['age']} yrs · "
        f"{escape(cust['loyalty_tier'].title())} member",
        "",
        f"🏷 <b>{escape(seg_label)}</b>: {escape(seg_description)}",
        "",
        f"📦 {f['orders_365']} orders in the last 12 months · {rs(f['monetary_365'])} spent"
        + (f" · avg {rs(f['aov'])}" if f["aov"] else ""),
    ]
    if f["recency_days"] is not None:
        lines.append(f"🕒 Last order {f['recency_days']} days ago"
                     + (f" · ❤️ {escape(cat(f['top_category']))}" if f["top_category"] else ""))
    else:
        lines.append("🕒 No orders in the last 12 months")
    if f["abandoned_carts_7d"]:
        lines.append(f"🛒 Left a {escape(cat(f['last_abandoned_cart_category']))} cart worth "
                     f"{rs(f['last_abandoned_cart_value'])} this week")
    pts = f"⭐ {f['points_balance']:,} points"
    if f["points_expiring_30d"] and f["next_points_expiry"]:
        pts += f" ({f['points_expiring_30d']:,} expiring {f['next_points_expiry']:%d %b})"
    lines.append(pts)
    if f["days_to_birthday"] <= 7:
        lines.append("🎂 Birthday " + ("today!" if f["days_to_birthday"] == 0 else f"in {f['days_to_birthday']} days"))
    lines += ["", f"<i>Why this group: {escape(f['segment_reason'])}</i>"]
    return "\n".join(lines)


def no_offer(reason_kind: str, next_time: datetime | None) -> str:
    if reason_kind == "opted_out":
        return "🔕 You've paused offers. Send /resume to turn them back on."
    if reason_kind == "too_soon":
        when = f" Check back after <b>{next_time:%d %b, %I:%M %p}</b>." if next_time else ""
        return ("⏸ You've had an offer recently. We keep it to a couple a week so they stay special." + when)
    return ("🙂 No offer for you right now, and that's on purpose: we only send offers that genuinely make "
            "sense for you. Try again later!")


HELP = (
    "<b>What I can do</b>\n"
    "/offers – get your personalised offer\n"
    "/profile – see your synthetic shopper profile\n"
    "/switch – become a different kind of shopper\n"
    "/stop – pause offers · /resume – turn them back on\n"
    "/help – this message\n\n"
    "<i>Every offer is chosen within the store's rules: discount caps, budgets and limits on how often "
    "you hear from us.</i>"
)
