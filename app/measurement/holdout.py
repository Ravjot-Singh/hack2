"""Holdout (control) group: a fixed, random-looking share of customers who never get proactive offers.

Assignment is a stable hash of (tenant, customer), so a customer is always in the same group and nobody can
game it. On-demand requests from real people are not part of the experiment and are always answered.
"""

import hashlib


def in_holdout(tenant_slug: str, customer_id: int, holdout_pct: float) -> bool:
    digest = hashlib.sha256(f"{tenant_slug}:{customer_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 10_000 < holdout_pct * 100
