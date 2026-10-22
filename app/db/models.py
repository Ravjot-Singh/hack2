"""ORM models. Every business table carries tenant_id so several bots/stores share one database.

Money is stored as whole rupees (int). Timestamps are timezone-aware (generated in IST).
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_ref: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))
    gender: Mapped[str] = mapped_column(String(1))
    age: Mapped[int] = mapped_column(Integer)
    birthday: Mapped[date] = mapped_column(Date)
    city: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(40))
    city_tier: Mapped[int] = mapped_column(Integer)
    device: Mapped[str] = mapped_column(String(10))
    signup_date: Mapped[date] = mapped_column(Date)
    price_sensitivity: Mapped[float] = mapped_column(Float)
    loyalty_tier: Mapped[str] = mapped_column(String(10))
    points_balance: Mapped[int] = mapped_column(Integer)
    # Ground-truth persona used only to evaluate segmentation; the offer engine must not read it.
    true_segment: Mapped[str] = mapped_column(String(20))


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
