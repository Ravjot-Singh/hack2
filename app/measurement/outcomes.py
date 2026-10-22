"""Simulated customer response to an offer (or to no offer, for the holdout group).

This plays the role of the real world, so it is allowed to use the hidden persona and price sensitivity that
the engine never sees. That makes the measurement honest: the engine's *estimates* are tested against a
*truth* it couldn't peek at.

  P(buy | no offer) = the customer's own chance of buying in the offer window
  P(buy | offer)    = 1 - (1 - P(buy | no offer)) * (1 - true lift)
  true lift         = offer strength x price sensitivity x persona responsiveness x relevance
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
RELEVANCE = {"cart_recovery": 0.10, "birthday": 0.05, "points_expiry": 0.04}
REDEEM_IF_BOUGHT = 0.85


@dataclass
class SimOutcome:
    purchased: bool
    redeemed: bool
    order_value: int
    discount_cost: int
    margin_rs: int


def offer_strength(offer_type: str, percent: float | None, amount_rs: int | None, points: int | None,
                   est_order: float) -> float:
    if offer_type == "percent_off":
        return min(0.30, 0.04 + 0.012 * (percent or 0))
    if offer_type == "flat_off":
        return min(0.30, 0.04 + 1.2 * (amount_rs or 0) / max(est_order, 1))
    if offer_type == "free_delivery":
        return 0.05
    return min(0.15, 0.02 + 0.0004 * (points or 0))


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
    if offer:
        lift = (offer_strength(offer["type"], offer.get("percent"), offer.get("amount_rs"), offer.get("points"),
                               est_order)
                * (0.4 + 1.2 * price_sensitivity) * PERSONA_RESPONSE.get(true_segment, 1.0)
                + RELEVANCE.get(offer["key"], 0.0))
        p_buy = 1 - (1 - base) * (1 - min(lift, 0.9))
    if rng.random() >= p_buy:
        return SimOutcome(False, False, 0, 0, 0)

    value = max(99.0, rng.lognormvariate(math.log(est_order), 0.35))
    redeemed = False
    cost = 0
    if offer and value >= (offer.get("min_order_value") or 0) and rng.random() < REDEEM_IF_BOUGHT:
        redeemed = True
        cost = discount_for(offer["type"], offer.get("percent"), offer.get("amount_rs"),
                            offer.get("max_discount_rs"), offer.get("points"), value, delivery_fee, point_value)
    return SimOutcome(True, redeemed, int(value), cost, int(value * margin_pct / 100))
