# Personalised Offer Engine

Decides which offer each customer of an e-commerce platform should receive, and delivers it through a Telegram bot.
Built for the J-PAL South Asia Punjab Jobs AI Bootcamp, Hackathon 2.

- **Rules first**: business rules (discount caps, budgets, frequency limits, quiet hours) define what is allowed.
- **AI inside the limits**: a Groq-hosted LLM picks from the rule-approved offers and writes the message.
- **Validator always wins**: every AI choice is re-checked against the rules before it is sent.
- **Synthetic data only**: realistic customers, orders, browsing, carts and reward points are generated locally.
- **Multi-tenant**: each store gets its own bot, rules and catalog under `tenants/<slug>/`.

> Status: Stage 9 (dashboard). Next: deploy to Render. Sections below grow as stages land.

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

## Telegram bot

```bash
python scripts/run_bot.py     # runs every tenant's bot locally (polling); needs SHOPKART_BOT_TOKEN in .env
```

In Telegram: **/start** → pick a kind of shopper (or *Surprise me*) and you're matched with a synthetic customer
of that type → **Get my offer** → **Claim** or **Not now**. Other commands: `/profile`, `/offers`, `/switch`,
`/stop`, `/resume`, `/help`.

- Every request is recorded in `offer_decisions`, including "no offer" with the rule that stopped it.
- The rules apply to real use too. After an offer you'll be told when the next one is allowed (48h gap, max 2 a
  week), and `/stop` is respected immediately.
- The offer box (discount, terms, coupon) is always generated from the rule-checked offer. Only the friendly
  intro above it will later be written by the AI.

## AI agent and validator

When the LLM is on, Groq (`openai/gpt-oss-120b`) gets the customer's facts and **only the offers that already
passed the rules**. It picks one and writes a short intro, plus a one-line reason for the marketing team.
Then the validator checks the answer before anything is sent:

- the chosen offer must be on the rule-approved shortlist;
- the intro may not state numbers, amounts, percentages or deadlines (those come only from the offer box,
  which is built from the rules), promise extras, include links or HTML, or mention other categories;
- customer data is passed as JSON data, never as instructions, and a model that "obeys" injected text is
  caught by the same checks.

If the LLM is off, over its limits, fails, or its answer is rejected, the rules' own top offer and a hand-written
template are used, and the reason is recorded on the decision.

**Staying inside the free tier:** `LLM_ENABLED=false` by default; per-minute and per-day caps
(`LLM_MAX_CALLS_PER_MINUTE`, `LLM_MAX_CALLS_PER_DAY`), with the daily count stored in the database; answers
cached per customer per day; tests and simulations never call Groq.

```bash
python scripts/try_agent.py --tenant shopkart --customer 17 --llm   # one real call, prints the message
```

## Triggers

Offers can start three ways, and every rule applies to each:
- **On demand**: a person taps *Get my offer* or sends `/offers`.
- **Live events**: something happens to a customer (an abandoned cart, points about to expire, a birthday coming
  up). Their profile updates and the engine reacts at once, pushing the offer to Telegram. At night the offer is
  queued and sent when quiet hours end.
- **Campaigns**: an admin runs one for every customer or every linked chat.

```bash
python scripts/live_trigger.py event --tenant shopkart --chat <telegram chat id> --kind cart_abandoned
python scripts/live_trigger.py campaign --tenant shopkart
```

## Measuring whether offers work

10% of customers (`holdout_pct` in rules.yaml) are a **holdout group**: they never get proactive offers, and
for each one we record the offer they *would* have got. Comparing customers who got an offer with matched
holdout customers shows what the offers really added: the conversion uplift with a 95% confidence interval,
the extra profit after discounts, and the rupees returned per rupee of discount, by offer and by segment.

```bash
python scripts/simulate_campaign.py --tenant shopkart --days 7 --reset        # real daily budget
python scripts/simulate_campaign.py --tenant shopkart --days 1 --reset --budget 5000000   # fund every offer
```

Simulated customers respond according to their *hidden* persona, which the engine never sees, so the report
tests the engine's estimates against a ground truth. The budget always goes to the most valuable offers first.
Simulated decisions never count toward real users' limits or the live budget, and never call the LLM.

## Operator portal (for businesses)

Any e-commerce business can create an account at `/app/register` and run its offers from `/app`. The portal is
being built in phases. Available now:
- **Sign-up and log-in**, and a **Home** page with today's numbers, a setup checklist, and the latest activity
  (which offers went to whom and why, or which rule held one back).
- **Customers**: search and filter by group or "on Telegram"; each customer's page explains their behaviour in
  plain words and recommends the best offer the rules allow, with alternatives. Send it in one click, or
  **create a custom offer** (type, amount, category, minimum order, validity). It's checked against every rule
  first and you see exactly why if it isn't allowed. Customers on Telegram get it instantly; for everyone else
  you get a coupon code to share. Offers you send by hand skip only the "how often" limits (gap between offers,
  weekly cap, no-repeat window), with a heads-up if the customer heard from you recently; every discount, budget
  and margin rule still applies, and so does a customer's opt-out.
- **Deals**: describe a deal once (e.g. "Diwali Deal: up to 15% off", for everyone or chosen customer groups,
  on each customer's favourite category, the whole store, or chosen categories, with a total budget). The
  preview shows who would get what (percentages are lowered to each category's discount limit), how many are on
  Telegram, the control group, why others are left out, and the most it can cost. Launch sends it in the
  background; the deal page shows progress, claims, the result against the control group, and a CSV of coupons
  for customers you'll reach through your own channels.
- **Activity**: every decision, filterable by result.
- **Data**: upload your customers, products, orders and order items as CSV files (plus optional abandoned carts
  and reward points). Templates with example rows are provided. Files are checked first: any problem is
  listed with its file and row number, and nothing changes until everything passes. Or generate realistic demo
  data in one click. After an import your categories and margins, offer rules and customer groups update
  automatically.
- **Results**: what your offers and deals added compared with the control group, by deal, offer and customer
  group, plus Telegram claims.
- **Settings**:
  - **Telegram bot**: create one with @BotFather and paste the token. It's checked with Telegram, stored
    encrypted, and starts automatically; share `t.me/<your bot>` with customers.
  - **Business profile**: your name, and the brand voice the AI writes in.
  - **Categories & margins**: edit them; discount limits are lowered automatically to stay below margins.
  - **Offer rules**: every limit and automatic offer in plain language, with change history.
  - **Customer groups**: edit the thresholds and names; customers are regrouped in the background.

To give the ShopKart demo store a login (you'll be asked for a password):

```bash
python scripts/create_account.py --tenant shopkart --email demo@shopkart.example
```

Add `SESSION_SECRET` and `TOKEN_ENCRYPTION_KEY` (any long random strings) to `.env`.

## Developer console (/admin)

```bash
uvicorn app.main:app --reload       # then open http://127.0.0.1:8000/admin
```

Log in with `ADMIN_USER` / `ADMIN_PASSWORD` from `.env`. The pages:

- **Overview**: customers, live offers today, budget used, AI usage, campaign decisions per day, segment sizes,
  and the latest decisions.
- **Results**: what the offers really added compared with the holdout group, with uncertainty ranges, extra
  profit and rupees returned per rupee of discount, by offer and by segment.
- **Segments**: each group in plain words, its rule, its size and behaviour, and its top customers.
- **Decisions**: every decision, including "no offer" with the rule that stopped it and why an AI answer was
  used or blocked.
- **Customer page**: the full profile, what the customer would get right now (ranked, with reasons), every offer
  the rules blocked and why, and their offer history.
- **Rules**: edit limits, discount caps per category, and each offer's amount, minimum order, validity, priority
  and audience. Every save is checked first: a discount above a category's margin, for example, is refused with
  a clear message. Saved rules reach the bot within seconds and every change is kept in a history.
- **Actions**: run a simulated campaign, send a live campaign to linked Telegram users, or fire a live event
  (abandoned cart, points expiring, birthday) at a user.

## Tests
```bash
pytest
```
Tests use a temporary SQLite database and never call Neon, Telegram or Groq.

## Adding another bot / store
1. Copy `tenants/_template` to `tenants/<new-slug>` and edit `tenant.yaml` (slug must equal the folder name).
2. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in the env var named by `bot_token_env`.
3. Restart the app; the tenant is registered automatically.
