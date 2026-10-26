"""ORM models. Every business table carries tenant_id so several bots/stores share one database.

Money is stored as whole rupees (int). Timestamps are timezone-aware (generated in IST).
"""

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, \
    Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """One business (store) using the platform, with its own bot, catalog, groups and rules."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    brand_voice: Mapped[str] = mapped_column(String(200), default="friendly, warm, short sentences, a few emoji",
                                             server_default="friendly, warm, short sentences, a few emoji")
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    coupon_prefix: Mapped[str | None] = mapped_column(String(4))
    bot_token_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted BotFather token
    bot_username: Mapped[str | None] = mapped_column(String(64))
    catalog: Mapped[dict | None] = mapped_column(JSON)  # {category: {"label": str, "margin_pct": float}}
    segments_yaml: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # bump on edits
    # Kill switch: no automatic offers (campaigns, deals, events) while paused; hand-sent and asked-for still work.
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Account(Base):
    """A business's login (one per business for now)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), unique=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped on logout / password change: session cookies carry it, so older cookies stop working.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_ref: Mapped[str] = mapped_column(String(40))  # the business's own customer id
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(160))
    # Uploaded data may not have these; synthetic data always does.
    gender: Mapped[str | None] = mapped_column(String(1))
    age: Mapped[int | None] = mapped_column(Integer)
    birthday: Mapped[date | None] = mapped_column(Date)
    city: Mapped[str | None] = mapped_column(String(40))
    state: Mapped[str | None] = mapped_column(String(40))
    city_tier: Mapped[int | None] = mapped_column(Integer)
    device: Mapped[str | None] = mapped_column(String(10))
    signup_date: Mapped[date] = mapped_column(Date)
    loyalty_tier: Mapped[str] = mapped_column(String(10), default="bronze", server_default="bronze")
    points_balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Hidden generator traits (synthetic data only), kept to evaluate segmentation and to simulate responses.
    # The offer engine must not read them; it works from observed behaviour (customer_features).
    price_sensitivity: Mapped[float | None] = mapped_column(Float)
    true_segment: Mapped[str | None] = mapped_column(String(20))


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    sku: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(30))
    price: Mapped[int] = mapped_column(Integer)
    margin_pct: Mapped[float] = mapped_column(Float)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(12))  # delivered | cancelled | returned
    items_count: Mapped[int] = mapped_column(Integer)
    gross_amount: Mapped[int] = mapped_column(Integer)
    discount_amount: Mapped[int] = mapped_column(Integer)
    points_redeemed: Mapped[int] = mapped_column(Integer)
    net_amount: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(10))
    coupon_code: Mapped[str | None] = mapped_column(String(20))
    sale_event: Mapped[str | None] = mapped_column(String(40))


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    category: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer)
    returned: Mapped[bool] = mapped_column(Boolean)


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (Index("ix_carts_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12))  # converted | abandoned
    items_count: Mapped[int] = mapped_column(Integer)
    value: Mapped[int] = mapped_column(Integer)
    top_category: Mapped[str] = mapped_column(String(30))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))


class BrowseDaily(Base):
    """Browsing aggregated per customer per day (keeps Neon's free 0.5 GB comfortable)."""

    __tablename__ = "browse_daily"
    __table_args__ = (Index("ix_browse_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    day: Mapped[date] = mapped_column(Date)
    sessions: Mapped[int] = mapped_column(Integer)
    product_views: Mapped[int] = mapped_column(Integer)
    top_category: Mapped[str] = mapped_column(String(30))


class CustomerFeature(Base):
    """Latest behavioural snapshot per customer: features, scores and assigned segment."""

    __tablename__ = "customer_features"

    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tenure_days: Mapped[int] = mapped_column(Integer)
    recency_days: Mapped[int | None] = mapped_column(Integer)
    orders_30: Mapped[int] = mapped_column(Integer)
    orders_90: Mapped[int] = mapped_column(Integer)
    orders_365: Mapped[int] = mapped_column(Integer)
    monetary_365: Mapped[int] = mapped_column(Integer)
    aov: Mapped[int] = mapped_column(Integer)
    discount_share: Mapped[float] = mapped_column(Float)
    return_rate: Mapped[float] = mapped_column(Float)
    sessions_30: Mapped[int] = mapped_column(Integer)
    sessions_90: Mapped[int] = mapped_column(Integer)
    abandoned_carts_7d: Mapped[int] = mapped_column(Integer)
    abandoned_carts_30d: Mapped[int] = mapped_column(Integer)
    last_abandoned_cart_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_abandoned_cart_value: Mapped[int | None] = mapped_column(Integer)
    last_abandoned_cart_category: Mapped[str | None] = mapped_column(String(30))
    top_category: Mapped[str | None] = mapped_column(String(30))
    points_balance: Mapped[int] = mapped_column(Integer)
    points_expiring_30d: Mapped[int] = mapped_column(Integer)
    next_points_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    days_to_birthday: Mapped[int] = mapped_column(Integer)
    churn_risk: Mapped[float] = mapped_column(Float)
    buy_anyway_prob: Mapped[float] = mapped_column(Float)
    deal_affinity: Mapped[float] = mapped_column(Float)
    expected_value_90d: Mapped[int] = mapped_column(Integer)
    segment: Mapped[str] = mapped_column(String(30), index=True)
    segment_reason: Mapped[str] = mapped_column(String(200))


class TelegramUser(Base):
    """A real Telegram chat linked to one synthetic customer (their demo persona)."""

    __tablename__ = "telegram_users"
    __table_args__ = (UniqueConstraint("tenant_id", "chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_name: Mapped[str | None] = mapped_column(String(80))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), unique=True)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deal(Base):
    """A deal for many customers (e.g. "Diwali Deal: up to 15% off"), personalised per customer by the engine."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft | sending | done | failed
    type: Mapped[str] = mapped_column(String(20))  # percent_off | flat_off | free_delivery | bonus_points
    value: Mapped[float] = mapped_column(Float)  # "up to" % / Rs off / points
    applies_to: Mapped[str] = mapped_column(String(12))  # favourite | any | categories
    categories: Mapped[list] = mapped_column(JSON, default=list)
    segments: Mapped[list] = mapped_column(JSON, default=list)  # ["all"] or group keys
    min_order_value: Mapped[int] = mapped_column(Integer, default=0)
    max_discount_rs: Mapped[int | None] = mapped_column(Integer)
    valid_hours: Mapped[int] = mapped_column(Integer, default=72)
    budget_rs: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(160))
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict] = mapped_column(JSON, default=dict)  # counts while/after sending
    error: Mapped[str | None] = mapped_column(String(300))


class ImportJob(Base):
    """A data import (uploaded CSVs or generated demo data) running in the background for one business."""

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # upload | demo
    status: Mapped[str] = mapped_column(String(12))  # running | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str | None] = mapped_column(String(80))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(300))


class OfferDecision(Base):
    """Every decision the engine makes for a customer, including 'no offer', and what happened next."""

    __tablename__ = "offer_decisions"
    __table_args__ = (Index("ix_decisions_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    trigger: Mapped[str] = mapped_column(String(30))  # on_demand | event:<kind> | campaign
    # Simulated decisions (campaign simulations over synthetic customers) never count toward live limits/budget.
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), index=True)
    segment: Mapped[str] = mapped_column(String(30))
    outcome: Mapped[str] = mapped_column(String(12))  # sent | no_offer | holdout | deferred
    block_reason: Mapped[str | None] = mapped_column(String(300))
    shortlist: Mapped[list] = mapped_column(JSON, default=list)  # eligible offer keys, best first
    offer_key: Mapped[str | None] = mapped_column(String(40))
    chosen_by: Mapped[str | None] = mapped_column(String(10))  # rules | ai
    ai_rejected_reason: Mapped[str | None] = mapped_column(String(300))
    ai_reason: Mapped[str | None] = mapped_column(String(300))  # the AI's one-line rationale, for marketing
    headline: Mapped[str | None] = mapped_column(String(120))
    terms: Mapped[str | None] = mapped_column(String(300))
    message: Mapped[str | None] = mapped_column(Text)
    coupon_code: Mapped[str | None] = mapped_column(String(20), unique=True)
    category: Mapped[str | None] = mapped_column(String(30))
    percent: Mapped[float | None] = mapped_column(Float)
    amount_rs: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[int | None] = mapped_column(Integer)
    min_order_value: Mapped[int | None] = mapped_column(Integer)
    max_discount_rs: Mapped[int | None] = mapped_column(Integer)
    liability_rs: Mapped[int] = mapped_column(Integer, default=0)
    expected_net_rs: Mapped[float | None] = mapped_column(Float)
    send_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    response: Mapped[str | None] = mapped_column(String(12))  # claimed | dismissed
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    guardrail: Mapped[list | None] = mapped_column(JSON)  # violations found by the final safety gate (outcome blocked)


class CustomerPreference(Base):
    """What a customer told the bot they like (/preferences). Categories and offer types only boost ranking;
    quiet times delay messages like the store's quiet hours."""

    __tablename__ = "customer_preferences"

    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    offer_types: Mapped[list] = mapped_column(JSON, default=list)
    quiet: Mapped[str | None] = mapped_column(String(20))  # preset key, e.g. "evenings"
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferOutcome(Base):
    """What happened after a decision (simulated for synthetic customers): did they buy, at what cost to us."""

    __tablename__ = "offer_outcomes"

    decision_id: Mapped[int] = mapped_column(ForeignKey("offer_decisions.id"), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    purchased: Mapped[bool] = mapped_column(Boolean)
    redeemed: Mapped[bool] = mapped_column(Boolean)  # used the offer (only possible if one was sent)
    order_value: Mapped[int] = mapped_column(Integer)
    discount_cost: Mapped[int] = mapped_column(Integer)
    margin_rs: Mapped[int] = mapped_column(Integer)  # gross margin of the order before the discount
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RulesVersion(Base):
    """Rules saved from the dashboard. The newest row wins over tenants/<slug>/rules.yaml (Render's disk is
    ephemeral, so edits must live in the database). Old rows are the change history."""

    __tablename__ = "rules_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)  # YAML


class LLMCall(Base):
    """One call to the LLM (Groq). Used for the daily cap and the dashboard."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    purpose: Mapped[str] = mapped_column(String(30))
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))


class PointsLedger(Base):
    __tablename__ = "points_ledger"
    __table_args__ = (Index("ix_points_tenant_customer", "tenant_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(10))  # opening | earn | redeem | expire
    delta: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
