# Personalised Offer Engine

Decides which offer each customer of an e-commerce platform should receive, and delivers it through a Telegram bot.
Built for the J-PAL South Asia Punjab Jobs AI Bootcamp, Hackathon 2.

- **Rules first**: business rules (discount caps, budgets, frequency limits, quiet hours) define what is allowed.
- **AI inside the limits**: a Groq-hosted LLM picks from the rule-approved offers and writes the message.
- **Validator always wins**: every AI choice is re-checked against the rules before it is sent.
- **Synthetic data only**: realistic customers, orders, browsing, carts and reward points are generated locally.
- **Multi-tenant**: each store gets its own bot, rules and catalog under `tenants/<slug>/`.

> Status: Stage 4 (rules engine). Sections below grow as stages land.

## Setup

1. Python 3.11+, then:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a free Postgres database on [Neon](https://neon.tech) and copy its connection string.
3. Copy `.env.example` to `.env` and fill in `DATABASE_URL` (and later the bot token / Groq key).
4. Create tables:
   ```bash
   alembic upgrade head
   ```
5. Run:
   ```bash
   uvicorn app.main:app --reload
   ```
   Open http://127.0.0.1:8000/health and http://127.0.0.1:8000/health/db.

## Synthetic data

```bash
python scripts/seed.py --tenant shopkart --dry-run   # generate + print a realism report
python scripts/seed.py --tenant shopkart             # generate + load into the database
```

5,000 customers with 12 months of orders, order items, carts, daily browsing and a reward-points ledger
(about 420k rows, roughly 50 MB). The same seed always gives the same data.

How it stays realistic:
- **Hidden personas** (loyal, regular, bargain hunter, occasional, new, at-risk, dormant, window shopper) set
  purchase frequency, price sensitivity, coupon and points use, and cart abandonment.
- **Indian shopping calendar**: Big Festive Sale, Diwali build-up and post-Diwali lull, Republic/Independence Day
  sales, end-of-season fashion sale, monsoon dip, plus weekend and payday lifts.
- **Lifecycle**: new sign-ups get a welcome burst; at-risk customers fade out over recent months; dormant ones
  stopped months ago.
- **Category economics**: fashion returns ~22% vs grocery ~1%; grocery baskets are many cheap items, electronics
  few expensive ones; COD is commoner in smaller cities.
- **Points**: 1 point per Rs 100 kept, redeemable up to 10% of an order, expiring after 180 days.

The report checks the result against known e-commerce patterns: top 20% of customers ≈ 65–70% of revenue,
≈ 70% cart abandonment, festive-season spikes.

## Customer features and segments

```bash
python scripts/build_features.py --tenant shopkart
```

For every customer the engine works out, from behaviour only:
- **What they did**: days since last order, orders in 30/90/365 days, spend, average order value, share of
  discounted orders, return rate, visits, abandoned carts, points balance and points expiring soon, days to birthday.
- **Churn risk**: how far past their *own* usual buying gap they are, plus how much they have slowed down.
- **Chance of buying anyway** in the next 14 days: high means a discount would mostly be wasted margin.
- **Deal affinity**: how often they buy with a discount.

Customers are then grouped using `tenants/shopkart/segments.yaml`, a plain-language file the marketing team can
edit: groups are checked top to bottom, each with simple conditions such as `orders_365_min: 4`. Every customer
gets a reason, for example *"days since last order: 163 (>= 120); churn risk: 91% (>= 60%)"*.

## Offer rules

All offers and limits live in `tenants/shopkart/rules.yaml`, written for a marketing team (every setting is
commented). It has two parts:

- **Limits** no offer can break: discount cap per category (kept below that category's margin), max Rs per offer,
  max 2 offers per customer per week, 48h between offers, no repeat of the same offer within 14 days,
  a daily budget, quiet hours (21:00–09:00), and two margin guards: no money-off offers to people who would
  likely buy anyway, or to serial returners.
- **Offers**: cart comeback, win-back, birthday treat, second-order welcome, points-expiry nudge, deal of the
  day, loyalty bonus points and free delivery, each with who may get it and a priority.

The file is checked when loaded, so a mistake such as a 12% cap on 8%-margin electronics, or Rs 100 off a
Rs 999 order under a 10% cap, is rejected with a readable message instead of reaching customers.

For each customer the engine returns a ranked shortlist with the reasons each offer qualifies, and a reason for
every offer it blocked. It also estimates each offer's expected profit: the extra margin it brings in, minus the
discount paid to everyone who uses it, including people who would have bought anyway.

```bash
python scripts/preview_offers.py --tenant shopkart                 # what every segment would get
python scripts/preview_offers.py --tenant shopkart --customer 17    # one customer, every decision explained
```

## Tests
```bash
pytest
```
Tests use a temporary SQLite database and never call Neon, Telegram or Groq.

## Adding another bot / store
1. Copy `tenants/_template` to `tenants/<new-slug>` and edit `tenant.yaml` (slug must equal the folder name).
2. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in the env var named by `bot_token_env`.
3. Restart the app; the tenant is registered automatically.
