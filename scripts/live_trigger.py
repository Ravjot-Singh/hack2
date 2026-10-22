"""Fire a live event or campaign; linked Telegram users get the offer pushed immediately (rules permitting).

    python scripts/live_trigger.py event --tenant shopkart --chat 8849102353 --kind cart_abandoned
    python scripts/live_trigger.py event --tenant shopkart --customer 17 --kind birthday   # synthetic, no push
    python scripts/live_trigger.py campaign --tenant shopkart                               # all linked chats

Event kinds: cart_abandoned, points_expiring, birthday. Every rule still applies (holdout, 48h gap, weekly cap,
quiet hours -> the offer is queued and sent by the running bot when quiet hours end).
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from telegram import Bot  # noqa: E402

from app.bot.handlers import BotContext  # noqa: E402
from app.data.generator import IST  # noqa: E402
from app.db.models import Customer, Tenant, TelegramUser  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.tenants.registry import get_tenants  # noqa: E402
from app.triggers.events import EVENTS, fire_event, run_live_campaign  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["event", "campaign"])
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--chat", type=int, help="Telegram chat id of a linked user")
    ap.add_argument("--customer", type=int, help="local customer number, e.g. 17 for SH00017")
    ap.add_argument("--kind", choices=list(EVENTS), default="cart_abandoned")
    args = ap.parse_args()

    cfg = get_tenants()[args.tenant]
    with session_scope() as s:
        tenant_id = s.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
        customer_id = None
        if args.chat:
            customer_id = s.scalar(select(TelegramUser.customer_id).where(
                TelegramUser.tenant_id == tenant_id, TelegramUser.chat_id == args.chat))
        elif args.customer:
            customer_id = s.scalar(select(Customer.id).where(
                Customer.tenant_id == tenant_id, Customer.external_ref == f"{cfg.prefix}{args.customer:05d}"))
    bc = BotContext(cfg, tenant_id)
    now = datetime.now(IST)

    async with Bot(cfg.bot_token) as bot:
        if args.action == "campaign":
            for line in await run_live_campaign(bc, bot, now):
                print(line)
            return
        if customer_id is None:
            sys.exit("Give --chat (a linked Telegram user) or --customer.")
        print(await fire_event(bc, bot, customer_id, args.kind, now))


if __name__ == "__main__":
    asyncio.run(main())
