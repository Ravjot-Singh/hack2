"""Simulated customer response to an offer (or to no offer, for the holdout group).

This plays the role of the real world, so it is allowed to use the hidden persona and price sensitivity that
the engine never sees. That makes the measurement honest: the engine's *estimates* are tested against a
*truth* it couldn't peek at.

  P(buy | no offer) = the customer's own chance of buying in the offer window
  P(buy | offer)    = 1 - (1 - P(buy | no offer)) * (1 - true lift)
  true lift         = offer strength x price sensitivity x persona responsiveness + relevance
Offer strength follows what the customer actually saves as a share of their basket (a "20% off, max ₹200" offer
on a ₹5,000 basket is worth 4%), calibrated so typical offers add a few percentage points of conversion and
cart reminders around ten, in line with published e-commerce benchmarks.
Most buyers who hold an offer use it, including those who would have bought anyway (that is the real cost
of discounts, and why the engine avoids "sure buyers").
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# How strongly each hidden persona reacts to offers.
PERSONA_RESPONSE = {
    "loyal_high_value": 0.6, "regular": 0.8, "bargain_hunter": 1.6, "occasional": 1.0,
    "new": 1.2, "at_risk": 0.9, "dormant": 0.35, "window_shopper": 0.7,
}
RELEVANCE = {"cart_recovery": 0.06, "birthday": 0.02, "points_expiry": 0.02}  # timely reasons to buy
MAX_STRENGTH = 0.12
REDEEM_IF_BOUGHT = 0.85


@dataclass
class SimOutcome:
    purchased: bool
    redeemed: bool
    order_value: int
    discount_cost: int
    margin_rs: int


def offer_strength(offer_type: str, percent: float | None, amount_rs: int | None, points: int | None,
                   est_order: float, max_discount_rs: int | None = None) -> float:
    """How much the offer pulls, from the share of the basket the customer saves."""
    order = max(est_order, 1)
    if offer_type == "percent_off":
        saved_pct = min(percent or 0, 100 * (max_discount_rs or order) / order)
    elif offer_type == "flat_off":
        saved_pct = 100 * (amount_rs or 0) / order
    elif offer_type == "free_delivery":
        return 0.025
    else:  # bonus points feel worth less than the same amount off
        return min(MAX_STRENGTH, 0.003 + 0.002 * 100 * (points or 0) / order)
    return min(MAX_STRENGTH, 0.005 + 0.0045 * saved_pct)


def discount_for(offer_type: str, percent, amount_rs, max_discount_rs, points, order_value: float,
                 delivery_fee: int, point_value: float) -> int:
    if offer_type == "percent_off":
        return int(min(order_value * (percent or 0) / 100, max_discount_rs or order_value))
    if offer_type == "flat_off":
        return int(amount_rs or 0)
    if offer_type == "free_delivery":
        return delivery_fee
    return int((points or 0) * point_value)


def simulate(rng: random.Random, *, features: dict, true_segment: str, price_sensitivity: float,
             window_hours: float, offer: dict | None, margin_pct: float, delivery_fee: int,
             point_value: float) -> SimOutcome:
    """offer=None for holdout / no offer. offer keys: key, type, percent, amount_rs, points, max_discount_rs,
    min_order_value."""
    p14 = features.get("buy_anyway_prob", 0.0)
    base = 1 - (1 - p14) ** (window_hours / (14 * 24))
    est_order = float(features.get("aov") or 900)
    if features.get("last_abandoned_cart_value") and offer and offer["key"] == "cart_recovery":
        est_order = float(features["last_abandoned_cart_value"])

    p_buy = base
    min_order = (offer or {}).get("min_order_value") or 0
    if offer:
        strength = offer_strength(offer["type"], offer.get("percent"), offer.get("amount_rs"), offer.get("points"),
                                  est_order, offer.get("max_discount_rs"))
        if min_order > 1.5 * est_order:  # a minimum far above their usual basket puts people off
            strength *= 0.5
        lift = (strength * (0.4 + 1.2 * price_sensitivity) * PERSONA_RESPONSE.get(true_segment, 1.0)
                + RELEVANCE.get(offer["key"], 0.0))
        p_buy = 1 - (1 - base) * (1 - min(lift, 0.9))
    if rng.random() >= p_buy:
        return SimOutcome(False, False, 0, 0, 0)

    value = max(99.0, rng.lognormvariate(math.log(est_order), 0.35))
    if offer and value < min_order and rng.random() < 0.5:
        value = float(min_order)  # many shoppers add items to reach the minimum and use the offer
    redeemed = False
    cost = 0
    if offer and value >= min_order and rng.random() < REDEEM_IF_BOUGHT:
        redeemed = True
        cost = discount_for(offer["type"], offer.get("percent"), offer.get("amount_rs"),
                            offer.get("max_discount_rs"), offer.get("points"), value, delivery_fee, point_value)
    return SimOutcome(True, redeemed, int(value), cost, int(value * margin_pct / 100))
