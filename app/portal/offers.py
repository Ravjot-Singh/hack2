"""Single-customer offers from the portal: preview the best offer, build + check a custom one, deliver."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from telegram.error import TelegramError

from app.db.models import OfferDecision
from app.db.session import session_scope
from app.delivery.push import push_offer, with_bot
from app.offers import service
from app.offers.service import OfferResult
from app.portal.auth import Business
from app.rules.engine import CustomerContext, Decision, evaluate
from app.rules.policy import TenantPolicy
from app.rules.schema import DYNAMIC_CATEGORIES, RulesConfig, check_rules

log = logging.getLogger(__name__)

CUSTOM_KEY = "custom"
DUPLICATE_WINDOW = timedelta(minutes=2)
FIELD_NAMES = {"value": "Amount", "min_order_value": "Minimum order", "max_discount_rs": "Maximum discount",
               "valid_hours": "Valid for", "type": "Offer type", "category": "Category", "label": "Offer name"}


def preview(biz: Business, customer_id: int, now: datetime, policy: TenantPolicy | None = None) -> tuple[dict, Decision]:
    """What the operator could send this customer right now (real offer history + today's budget; frequency
    limits relaxed as for operator sends), nothing recorded."""
    policy = policy or biz.policy
    lim = policy.rules.limits
    with session_scope() as s:
        f = service._features(s, biz.tenant_id, customer_id, policy, now)
        recent = service._recent_offers(s, customer_id, now, max(7, lim.same_offer_cooldown_days))
        budget = service.budget_used_today(s, biz.tenant_id, now)
        opted_out = service.chat_opted_out(s, customer_id)
    ctx = CustomerContext(customer_id, f, f["segment"], opted_out, recent)
    return f, evaluate(policy.rules, policy.catalog, ctx, budget, now, relax_frequency=True)


def only_offer(policy: TenantPolicy, offer_key: str) -> TenantPolicy | None:
    offers = [o for o in policy.rules.offers if o.key == offer_key]
    if not offers:
        return None
    return TenantPolicy(policy.rules.model_copy(update={"offers": offers}), policy.catalog, policy.segments,
                        policy.version_id)


@dataclass
class CustomCheck:
    form: dict
    problems: list[str] = field(default_factory=list)  # the offer itself breaks a limit
    blocked: list[str] = field(default_factory=list)  # this customer can't get it right now
    policy: TenantPolicy | None = None
    decision: Decision | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and not self.blocked and self.decision is not None and bool(self.decision.eligible)


def custom_form_defaults(decision: Decision, f: dict) -> dict:
    c = decision.best
    if c is None:
        return {"type": "percent_off", "value": 10, "category": "top_category" if f.get("top_category") else "any",
                "min_order_value": 499, "max_discount_rs": 200, "valid_hours": 72, "label": "A special offer for you"}
    value = c.percent if c.type == "percent_off" else c.amount_rs if c.type == "flat_off" else c.points or 0
    return {"type": c.type, "value": value, "category": c.category, "min_order_value": c.min_order_value,
            "max_discount_rs": c.max_discount_rs or 200, "valid_hours": 72, "label": c.label}


def check_custom(biz: Business, customer_id: int, form: dict, now: datetime) -> CustomCheck:
    """Turn the form into a one-off offer and run it through every limit, for this customer."""
    policy = biz.policy
    result = CustomCheck(form)
    otype = form.get("type") or "percent_off"
    category = form.get("category") or "any"
    try:
        offer = {
            "key": CUSTOM_KEY, "label": (form.get("label") or "A special offer for you").strip()[:60],
            "description": "Custom offer created in the portal", "type": otype,
            "value": 0 if otype == "free_delivery" else float(form.get("value") or 0),
            "category": category, "fallback_category": "any" if category in ("top_category", "from_cart") else None,
            "min_order_value": int(float(form.get("min_order_value") or 0)),
            "max_discount_rs": int(float(form["max_discount_rs"])) if otype == "percent_off" and form.get(
                "max_discount_rs") else None,
            "valid_hours": int(float(form.get("valid_hours") or 72)),
            "segments": ["all"], "priority": 10, "expected_response": 0.15,
        }
    except (ValueError, OverflowError):
        result.problems.append("Please enter numbers only in the amount, minimum order, maximum discount and validity.")
        return result
    if otype in ("flat_off", "bonus_points") and offer["value"] < 1 or otype == "percent_off" and offer["value"] <= 0:
        result.problems.append("Enter an amount above zero.")
        return result
    if offer["category"] not in DYNAMIC_CATEGORIES and offer["category"] not in policy.catalog.margins:
        result.problems.append("Please choose a category.")
        return result
    data = policy.rules.model_dump(mode="json")
    data["offers"] = [offer]
    try:
        rules = RulesConfig(**data)
    except ValidationError as exc:
        for e in exc.errors():
            name = FIELD_NAMES.get(str(e["loc"][-1]), str(e["loc"][-1]))
            result.problems.append(f"{name}: {e['msg']}.")
        return result
    result.problems = [p.replace(f"Offer '{offer['label']}' ({CUSTOM_KEY})", "This offer")
                       for p in check_rules(rules, policy.catalog.margins, {s.key for s in policy.segments.segments})]
    if result.problems:
        return result
    result.policy = TenantPolicy(rules, policy.catalog, policy.segments, policy.version_id)
    _, result.decision = preview(biz, customer_id, now, result.policy)
    if result.decision.customer_block:
        result.blocked.append(result.decision.customer_block)
    elif not result.decision.eligible:
        result.blocked += [b.reason for b in result.decision.blocked]
    return result


def just_sent(biz: Business, customer_id: int, now: datetime) -> bool:
    """An operator offer went to this customer moments ago (double click, resubmitted form, two tabs)."""
    with session_scope() as s:
        return s.scalar(select(OfferDecision.id).where(
            OfferDecision.tenant_id == biz.tenant_id, OfferDecision.customer_id == customer_id,
            OfferDecision.trigger == "operator", OfferDecision.outcome.in_(("sent", "deferred")),
            OfferDecision.created_at > now - DUPLICATE_WINDOW).limit(1)) is not None


DUPLICATE_FLASH = {"kind": "warn", "title": "Not sent again",
                   "lines": ["You sent this customer an offer a moment ago. Wait a couple of minutes if you really "
                             "want to send another."]}


def send(biz: Business, customer_id: int, policy: TenantPolicy, now: datetime) -> OfferResult:
    return service.request_offer(biz.tenant_id, customer_id, policy, biz.tenant.name, biz.tenant.prefix, "operator",
                                 now, biz.tenant.brand_voice, tenant_slug=biz.tenant.slug)


async def deliver(running_bots: dict, biz: Business, result: OfferResult) -> dict:
    """Push to Telegram when possible; returns a flash message for the operator."""
    if result.kind == "offer" and result.chat_id:
        try:
            await with_bot(running_bots, biz.tenant.slug, biz.tenant.bot_token,
                           lambda bot: push_offer(bot, result.chat_id, result.decision_id, result.message))
            return {"kind": "ok", "title": f"Sent on Telegram: {result.headline}", "lines": [f"Coupon {result.coupon}"]}
        except LookupError:
            return {"kind": "warn", "title": f"Saved: {result.headline}",
                    "lines": [f"Coupon {result.coupon}. Connect your Telegram bot in Settings to deliver offers."]}
        except TelegramError as exc:
            log.warning("Telegram delivery failed: %s", exc)
            return {"kind": "warn", "title": f"Saved: {result.headline}",
                    "lines": [f"Coupon {result.coupon}. Telegram didn't accept the message ({exc})."]}
    if result.kind == "offer":
        return {"kind": "ok", "title": f"Saved: {result.headline}",
                "lines": [f"Coupon {result.coupon}. This customer isn't on Telegram, so share the code through "
                          "your own channels (email, SMS, app)."]}
    if result.kind == "deferred":
        return {"kind": "ok", "title": f"Queued: {result.headline}",
                "lines": [f"It's quiet hours, so it goes out on Telegram at {result.next_time:%d %b, %I:%M %p}."]}
    reasons = {"opted_out": "This customer has turned offers off.",
               "too_soon": "This customer got an offer recently; the frequency limits don't allow another yet.",
               "none": "No offer passed the rules for this customer right now."}
    return {"kind": "err", "title": "Not sent", "lines": [reasons.get(result.kind, result.kind)]}
