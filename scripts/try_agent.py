"""Run the full pipeline (rules -> AI -> validator) for one customer and print the message. Nothing is sent or
recorded, except the LLM call itself in llm_calls (which counts toward the daily cap).

    python scripts/try_agent.py --tenant shopkart --customer 17          # rules + template only
    python scripts/try_agent.py --tenant shopkart --customer 17 --llm    # one real Groq call (needs GROQ_API_KEY)
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.agent.chooser import choose  # noqa: E402
from app.agent.groq_client import groq_json, llm_status  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.generator import IST  # noqa: E402
from app.db.models import Customer, Tenant  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.offers.service import _features, intro_context  # noqa: E402
from app.rules.engine import CustomerContext, evaluate  # noqa: E402
from app.rules.policy import current_policy  # noqa: E402
from app.tenants.registry import get_tenants  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--customer", type=int, required=True, help="local number, e.g. 17 for SH00017")
    ap.add_argument("--llm", action="store_true", help="enable Groq for this run only")
    args = ap.parse_args()
    if args.llm:
        os.environ["LLM_ENABLED"] = "true"
        get_settings.cache_clear()

    cfg = get_tenants()[args.tenant]
    policy = current_policy(args.tenant, cfg.path)
    now = datetime.now(IST)
    ref = f"{args.tenant[:2].upper()}{args.customer:05d}"
    with session_scope() as s:
        tenant_id = s.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        cust = s.scalar(select(Customer).where(Customer.tenant_id == tenant_id, Customer.external_ref == ref))
        f = _features(s, tenant_id, cust.id, policy, now)
        name = cust.name
    d = evaluate(policy.rules, policy.catalog, CustomerContext(cust.id, f, f["segment"]), 0, now)
    print(f"{ref} {name} ({f['segment']}); shortlist: {[c.offer_key for c in d.eligible] or 'none'}")
    if not d.eligible:
        return
    if args.llm:
        print("LLM status:", llm_status(now) or "ready")
    choice = choose(policy, tenant_id, cust.id, cfg.name, cfg.brand_voice, name, f, d.eligible,
                    intro_context(name, cfg.name, d.best, f, policy), now, groq_json if args.llm else None)
    print(f"chosen by {choice.chosen_by}: {choice.candidate.offer_key} ({choice.candidate.headline})")
    if choice.ai_reason:
        print("AI reason:", choice.ai_reason)
    if choice.ai_rejected_reason:
        print("Fallback because:", choice.ai_rejected_reason)
    print("\n" + re.sub(r"<[^>]+>", "", choice.intro_html))
    print(f"[{choice.candidate.headline} | {choice.candidate.terms}]")


if __name__ == "__main__":
    main()
