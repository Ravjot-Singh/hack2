# Personalised Offer Engine

Decides which offer each customer of an e-commerce platform should receive, and delivers it through a Telegram bot.
Built for the J-PAL South Asia Punjab Jobs AI Bootcamp, Hackathon 2.

- **Rules first**: business rules (discount caps, budgets, frequency limits, quiet hours) define what is allowed.
- **AI inside the limits**: a Groq-hosted LLM picks from the rule-approved offers and writes the message.
- **Validator always wins**: every AI choice is re-checked against the rules before it is sent.
- **Synthetic data only**: realistic customers, orders, browsing, carts and reward points are generated locally.
- **Multi-tenant**: each store gets its own bot, rules and catalog under `tenants/<slug>/`.

> Status: Stage 1 (scaffold). Sections below grow as stages land.

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

## Tests
```bash
pytest
```
Tests use a temporary SQLite database and never call Neon, Telegram or Groq.

## Adding another bot / store
1. Copy `tenants/_template` to `tenants/<new-slug>` and edit `tenant.yaml` (slug must equal the folder name).
2. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in the env var named by `bot_token_env`.
3. Restart the app; the tenant is registered automatically.
