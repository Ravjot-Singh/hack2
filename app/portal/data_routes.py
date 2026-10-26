"""Portal "Data" page: CSV templates, upload + check + import, demo data, and import progress."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select

from app.db.models import Customer, ImportJob, Order, Product, Tenant
from app.db.session import session_scope
from app.ingest import jobs
from app.ingest.csv_import import FILES, MAX_BYTES, parse_files, template_csv
from app.portal import routes as base
from app.portal.auth import Business, current_business

router = APIRouter(prefix="/app")


def _data_page(request: Request, biz: Business, report=None, upload_error: str | None = None) -> HTMLResponse:
    with session_scope() as s:
        counts = {
            "customers": s.scalar(select(func.count()).select_from(Customer).where(Customer.tenant_id == biz.tenant_id)),
            "products": s.scalar(select(func.count()).select_from(Product).where(Product.tenant_id == biz.tenant_id)),
            "orders": s.scalar(select(func.count()).select_from(Order).where(Order.tenant_id == biz.tenant_id)),
        }
        first, last = s.execute(select(func.min(Order.created_at), func.max(Order.created_at))
                                .where(Order.tenant_id == biz.tenant_id)).one()
        catalog = s.get(Tenant, biz.tenant_id).catalog or {}
        recent = s.scalars(select(ImportJob).where(ImportJob.tenant_id == biz.tenant_id, ImportJob.kind != "checks")
                           .order_by(ImportJob.id.desc()).limit(5)).all()
        job_list = [{k: getattr(j, k) for k in ("id", "kind", "status", "created_at", "finished_at", "stage",
                                                "summary", "error")} for j in recent]
    running = next((j for j in job_list if j["status"] == "running"), None)
    return base.page(request, "data.html", biz, "data", counts=counts, first=first, last=last, catalog=catalog,
                     jobs=job_list, running=running, files=FILES, report=report, upload_error=upload_error,
                     demo_sizes=jobs.DEMO_SIZES, max_mb=MAX_BYTES // (1024 * 1024), **base._labels(biz))


@router.get("/data", response_class=HTMLResponse)
def data(request: Request, biz: Business = Depends(current_business)):
    return _data_page(request, biz)


@router.get("/data/templates/{name}.csv", response_class=PlainTextResponse)
def data_template(name: str, biz: Business = Depends(current_business)):
    if name not in FILES:
        raise HTTPException(404, "No such template")
    return PlainTextResponse(template_csv(name), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@router.post("/data/upload", response_class=HTMLResponse)
async def data_upload(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    if form.get("confirm") != "yes":
        return _data_page(request, biz, upload_error="Please tick the box to confirm your current data will be replaced.")
    files: dict[str, bytes] = {}
    for name in FILES:
        f = form.get(name)
        if isinstance(f, UploadFile) or hasattr(f, "read"):
            content = await f.read(MAX_BYTES + 1)
            if content:
                files[name] = content
    with session_scope() as s:
        existing = s.get(Tenant, biz.tenant_id).catalog or {}
    report = await asyncio.to_thread(parse_files, files, base._now(), existing)
    if not report.ok:
        return _data_page(request, biz, report=report)
    job_id = await asyncio.to_thread(jobs.create_job, biz.tenant_id, "upload", base._now())
    if job_id is None:
        return _data_page(request, biz, upload_error="An import is already running. Wait for it to finish.")
    with session_scope() as s:
        s.get(ImportJob, job_id).summary = {"warnings": report.warnings, "checked": report.counts}
    jobs.start(jobs.run_upload, job_id, biz.tenant_id, biz.tenant.slug, report.dataset, report.catalog, base._now)
    request.session["flash"] = {"kind": "ok", "title": "Files look good: importing now",
                                "lines": [f"{report.counts['customers']:,} customers, {report.counts['orders']:,} "
                                          f"orders, {report.counts['categories']} categories."] + report.warnings}
    return RedirectResponse("/app/data", status_code=303)


@router.post("/data/demo")
async def data_demo(request: Request, biz: Business = Depends(current_business)):
    form = await request.form()
    try:
        size = int(form.get("size") or 1000)
    except ValueError:
        size = 1000
    if size not in jobs.DEMO_SIZES:
        raise HTTPException(400, "Unsupported size")
    if form.get("confirm") != "yes":
        return _data_page(request, biz, upload_error="Please tick the box to confirm your current data will be replaced.")
    job_id = await asyncio.to_thread(jobs.create_job, biz.tenant_id, "demo", base._now())
    if job_id is None:
        return _data_page(request, biz, upload_error="An import is already running. Wait for it to finish.")
    jobs.start(jobs.run_demo, job_id, biz.tenant_id, biz.tenant.slug, biz.tenant.prefix, size, base._now)
    return RedirectResponse("/app/data", status_code=303)
