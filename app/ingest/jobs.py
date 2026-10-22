"""Background data jobs: load an uploaded or generated dataset for a business, then refresh everything that
depends on it (catalog + margins, rules fitted to the new categories, customer profiles and groups)."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

import yaml
from sqlalchemy import select

from app.data.generator import Dataset, generate
from app.data.loader import load_dataset
from app.db.models import ImportJob, RulesVersion, Tenant
from app.db.session import get_engine, session_scope
from app.features.store import refresh_features
from app.rules.policy import adapt_rules, clear_policy_cache, current_policy, save_rules
from app.tenants.registry import TEMPLATE_DIR, catalog_from_file, clear_tenant_cache

log = logging.getLogger(__name__)
DEMO_SIZES = (500, 1000, 2000)


def _stage(job_id: int, stage: str, **changes) -> None:
    with session_scope() as s:
        job = s.get(ImportJob, job_id)
        job.stage = stage
        for k, v in changes.items():
            setattr(job, k, v)


def apply_dataset(job_id: int, tenant_id: int, slug: str, ds: Dataset, catalog: dict, now: datetime) -> dict:
    """Replace the business's data with ds; returns a summary. Runs in a worker thread."""
    _stage(job_id, f"Saving {len(ds.customers):,} customers and {len(ds.orders):,} orders")
    with get_engine().begin() as conn:
        counts = load_dataset(conn, tenant_id, ds, progress=lambda *_: None)
    with session_scope() as s:
        tenant = s.get(Tenant, tenant_id)
        tenant.catalog = catalog
        tenant.config_version += 1
        latest = s.execute(select(RulesVersion.content).where(RulesVersion.tenant_id == tenant_id)
                           .order_by(RulesVersion.id.desc()).limit(1)).scalar()
    clear_tenant_cache()
    clear_policy_cache(slug)
    if latest is not None:  # saved rules must fit the new categories (unsaved defaults adapt by themselves)
        old = yaml.safe_load(latest)
        new = adapt_rules(old, catalog)
        if new != old:
            save_rules(slug, tenant_id, None, new, "system", "Adjusted to your new product categories", now)
    _stage(job_id, "Working out each customer's profile and group")
    policy = current_policy(slug)
    with get_engine().begin() as conn:
        rows = refresh_features(conn, tenant_id, now, policy.segments)
    groups = {}
    for r in rows:
        groups[r["segment"]] = groups.get(r["segment"], 0) + 1
    return {**counts, "categories": len(catalog), "groups": groups}


def run_upload(job_id: int, tenant_id: int, slug: str, ds: Dataset, catalog: dict, now_fn) -> None:
    _finish(job_id, lambda: apply_dataset(job_id, tenant_id, slug, ds, catalog, now_fn()), now_fn)


def run_demo(job_id: int, tenant_id: int, slug: str, prefix: str, size: int, now_fn) -> None:
    def work() -> dict:
        _stage(job_id, f"Generating {size:,} demo customers with a year of history")
        now = now_fn()
        ds = generate(TEMPLATE_DIR / "catalog.yaml", n_customers=size, seed=random.randrange(10**6),
                      as_of=now.date(), ref_prefix=prefix)
        return apply_dataset(job_id, tenant_id, slug, ds, catalog_from_file(TEMPLATE_DIR / "catalog.yaml"), now)
    _finish(job_id, work, now_fn)


def run_regroup(job_id: int, tenant_id: int, slug: str, now_fn) -> None:
    """Customer groups were edited: re-assign every customer."""
    def work() -> dict:
        _stage(job_id, "Re-assigning customers to groups")
        policy = current_policy(slug)
        with get_engine().begin() as conn:
            rows = refresh_features(conn, tenant_id, now_fn(), policy.segments)
        groups: dict[str, int] = {}
        for r in rows:
            groups[r["segment"]] = groups.get(r["segment"], 0) + 1
        return {"customers": len(rows), "groups": groups}
    _finish(job_id, work, now_fn)


def _finish(job_id: int, work, now_fn) -> None:
    try:
        summary = work()
        _stage(job_id, "Done", status="done", summary=summary, finished_at=now_fn())
    except Exception as exc:  # report, never leave a job "running"
        log.exception("Data job %s failed", job_id)
        _stage(job_id, "Failed", status="failed", error=str(exc)[:300], finished_at=now_fn())


def create_job(tenant_id: int, kind: str, now: datetime) -> int | None:
    """A new running job, or None if this business already has one running."""
    with session_scope() as s:
        if s.scalar(select(ImportJob.id).where(ImportJob.tenant_id == tenant_id, ImportJob.status == "running")):
            return None
        job = ImportJob(tenant_id=tenant_id, kind=kind, status="running", created_at=now, stage="Starting",
                        summary={})
        s.add(job)
        s.flush()
        return job.id


_running: set[asyncio.Task] = set()


def start(fn, *args) -> None:
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(fn, *args))
    _running.add(task)
    task.add_done_callback(_running.discard)


def mark_interrupted_jobs() -> None:
    with session_scope() as s:
        for job in s.scalars(select(ImportJob).where(ImportJob.status == "running")):
            job.status, job.error = "failed", "Interrupted by a server restart. Please run it again."
