"""Portal pages for deals (one offer idea for all or some customers) and the activity log."""

from __future__ import annotations

import asyncio
import csv
import io
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import case, func, select

from app.dashboard import queries
from app.db.models import Customer, Deal, OfferDecision, TelegramUser
from app.db.session import get_engine, session_scope
from app.measurement.metrics import measure
from app.offers import deals as deal_engine
from app.offers.deals import APPLIES, TYPES, DealSpec
from app.portal import routes as base
from app.portal.auth import Business, current_business

router = APIRouter(prefix="/app")
DEFAULT_FORM = {"name": "", "type": "percent_off", "value": 15, "applies_to": "favourite", "categories": [],
                "segments": ["all"], "min_order_value": 699, "max_discount_rs": 300, "valid_hours": 168,
                "budget_rs": 20000}


def _deal_or_404(s, biz: Business, deal_id: int) -> Deal:
    d = s.get(Deal, deal_id)
    if d is None or d.tenant_id != biz.tenant_id:
        raise HTTPException(404, "Deal not found")
    return d


def _form_dict(spec: DealSpec | None, raw=None) -> dict:
    if spec is not None:
        return {**spec.__dict__}
    return {**DEFAULT_FORM, **(dict(raw) if raw else {})}


def _form_page(request: Request, biz: Business, form: dict, errors: list[str], deal_id: int | None = None):
    return base.page(request, "deal_form.html", biz, "deals", form=form, errors=errors, deal_id=deal_id,
                     types=TYPES, applies=APPLIES, caps=biz.policy.rules.limits.max_discount_pct,
                     max_per_offer=biz.policy.rules.limits.max_discount_rs_per_offer, **base._labels(biz))


@router.get("/deals", response_class=HTMLResponse)
def deals(request: Request, biz: Business = Depends(current_business)):
    with session_scope() as s:
        rows = s.scalars(select(Deal).where(Deal.tenant_id == biz.tenant_id).order_by(Deal.id.desc())).all()
        claimed = dict(s.execute(select(OfferDecision.deal_id, func.count()).where(
            OfferDecision.tenant_id == biz.tenant_id, OfferDecision.deal_id.is_not(None),
            OfferDecision.response == "claimed").group_by(OfferDecision.deal_id)).all())
        items = [{"d": d, "claimed": claimed.get(d.id, 0), "created_at": d.created_at} for d in rows]
    return base.page(request, "deals.html", biz, "deals", items=items, types=TYPES)


@router.get("/deals/new", response_class=HTMLResponse)
def deal_new(request: Request, biz: Business = Depends(current_business)):
    return _form_page(request, biz, dict(DEFAULT_FORM), [])


async def _save(request: Request, biz: Business, deal_id: int | None):
    form = await request.form()
    spec, errors = DealSpec.from_form(form)
    if spec is not None:
        errors = deal_engine.validate_spec(spec, biz.policy)
    if errors:
        raw = {k: form.get(k) for k in form.keys()}
        raw["categories"], raw["segments"] = form.getlist("categories"), form.getlist("segments") or ["all"]
        return _form_page(request, biz, raw, errors, deal_id)
    with session_scope() as s:
        if deal_id is None:
            d = Deal(tenant_id=biz.tenant_id, created_at=base._now(), created_by=biz.email, status="draft",
                     progress={})
            s.add(d)
        else:
            d = _deal_or_404(s, biz, deal_id)
            if d.status != "draft":
                raise HTTPException(400, "Only drafts can be edited")
        for k, v in spec.__dict__.items():
            setattr(d, k, v)
        s.flush()
        new_id = d.id
    return RedirectResponse(f"/app/deals/{new_id}", status_code=303)


@router.post("/deals", response_class=HTMLResponse)
async def deal_create(request: Request, biz: Business = Depends(current_business)):
    return await _save(request, biz, None)


@router.get("/deals/{deal_id}/edit", response_class=HTMLResponse)
def deal_edit(request: Request, deal_id: int, biz: Business = Depends(current_business)):
    with session_scope() as s:
        d = _deal_or_404(s, biz, deal_id)
        if d.status != "draft":
            return RedirectResponse(f"/app/deals/{deal_id}", status_code=303)
        form = _form_dict(DealSpec.from_deal(d))
    return _form_page(request, biz, form, [], deal_id)


@router.post("/deals/{deal_id}/edit", response_class=HTMLResponse)
async def deal_update(request: Request, deal_id: int, biz: Business = Depends(current_business)):
    return await _save(request, biz, deal_id)


@router.post("/deals/{deal_id}/delete")
def deal_delete(deal_id: int, biz: Business = Depends(current_business)):
    with session_scope() as s:
        d = _deal_or_404(s, biz, deal_id)
        if d.status != "draft":
            raise HTTPException(400, "Only drafts can be deleted")
        s.delete(d)
    return RedirectResponse("/app/deals", status_code=303)


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: int, biz: Business = Depends(current_business)):
    now = base._now()
    policy = biz.policy
    with session_scope() as s:
        d = _deal_or_404(s, biz, deal_id)
        spec = DealSpec.from_deal(d)
        deal = {k: getattr(d, k) for k in ("id", "name", "status", "created_at", "launched_at", "finished_at",
                                           "progress", "error", "budget_rs")}
        preview = sample = None
        stats = report = None
        if d.status == "draft":
            plan = deal_engine.plan_deal(s, biz.tenant_id, biz.tenant.slug, policy, spec, d.id, now)
            preview = plan.summary()
            preview["send_after"] = plan.send_after
            sample = [{"name": i.name, "ref": i.ref, "id": i.customer_id, "segment": i.segment,
                       "headline": i.candidate.headline, "terms": i.candidate.terms,
                       "telegram": bool(i.chat_id) and not i.opted_out} for i in plan.send[:12]]
            preview["headlines"] = plan.headlines.most_common(8)
        else:
            row = s.execute(select(
                func.sum(case((OfferDecision.outcome == "sent", 1), else_=0)),
                func.sum(case((OfferDecision.outcome == "deferred", 1), else_=0)),
                func.sum(case((OfferDecision.outcome == "holdout", 1), else_=0)),
                func.sum(case((OfferDecision.response == "claimed", 1), else_=0)),
                func.sum(case((OfferDecision.response == "dismissed", 1), else_=0)),
                func.coalesce(func.sum(OfferDecision.liability_rs), 0),
            ).where(OfferDecision.deal_id == d.id)).one()
            stats = dict(zip(("sent", "queued", "holdout", "claimed", "dismissed", "liability"),
                             (int(x or 0) for x in row)))
    if deal["status"] != "draft":
        with get_engine().connect() as conn:
            report = measure(conn, biz.tenant_id, deal_id=deal_id)
    return base.page(request, "deal.html", biz, "deals", deal=deal, spec=spec, preview=preview, sample=sample,
                     stats=stats, report=report, types=TYPES, applies=APPLIES, **base._labels(biz))


def _mark_sending(biz: Business, deal_id: int) -> None:
    with session_scope() as s:
        d = _deal_or_404(s, biz, deal_id)
        if d.status != "draft":
            raise HTTPException(400, "This deal was already launched")
        d.status, d.launched_at, d.progress = "sending", base._now(), {"stage": "planning"}


@router.post("/deals/{deal_id}/launch")
async def deal_launch(request: Request, deal_id: int, biz: Business = Depends(current_business)):
    # async so the background job is started on the server's event loop (it outlives this request)
    await asyncio.to_thread(_mark_sending, biz, deal_id)
    deal_engine.start_deal(deal_id, biz.tenant, biz.tenant_id, request.app.state.bots, base._now)
    return RedirectResponse(f"/app/deals/{deal_id}", status_code=303)


@router.get("/deals/{deal_id}/coupons.csv")
def deal_coupons(deal_id: int, biz: Business = Depends(current_business)):
    """Coupons for customers who can't be reached on Telegram, to send through the business's own channels."""
    with session_scope() as s:
        d = _deal_or_404(s, biz, deal_id)
        rows = s.execute(select(Customer.external_ref, Customer.name, Customer.phone, Customer.email,
                                OfferDecision.coupon_code, OfferDecision.headline, OfferDecision.terms,
                                OfferDecision.valid_until, TelegramUser.chat_id)
                         .join(Customer, Customer.id == OfferDecision.customer_id)
                         .outerjoin(TelegramUser, TelegramUser.customer_id == OfferDecision.customer_id)
                         .where(OfferDecision.deal_id == d.id, OfferDecision.outcome.in_(("sent", "deferred")))
                         .order_by(Customer.external_ref)).all()
        name = d.name
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["customer_id", "name", "phone", "email", "coupon", "offer", "terms", "valid_until", "on_telegram"])
    for ref, cname, phone, email, coupon, headline, terms, until, chat in rows:
        w.writerow([ref, cname, phone or "", email or "", coupon, headline, terms,
                    until.strftime("%Y-%m-%d %H:%M") if until else "", "yes" if chat else "no"])
    filename = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "deal"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}-coupons.csv"'})


# ---- results ----

@router.get("/results", response_class=HTMLResponse)
def results(request: Request, biz: Business = Depends(current_business)):
    with session_scope() as s:
        deals = s.scalars(select(Deal).where(Deal.tenant_id == biz.tenant_id, Deal.status == "done")
                          .order_by(Deal.id.desc())).all()
        deal_rows = [{"id": d.id, "name": d.name, "progress": d.progress or {}, "finished_at": d.finished_at}
                     for d in deals]
        claims = dict(s.execute(select(OfferDecision.deal_id, func.count()).where(
            OfferDecision.tenant_id == biz.tenant_id, OfferDecision.response == "claimed",
            OfferDecision.deal_id.is_not(None)).group_by(OfferDecision.deal_id)).all())
    with get_engine().connect() as conn:
        report = measure(conn, biz.tenant_id, include_simulated=False)
        for row in deal_rows:
            row["report"] = measure(conn, biz.tenant_id, deal_id=row["id"]).overall
            row["claimed"] = claims.get(row["id"], 0)
    simulated = any(r["progress"].get("simulated") for r in deal_rows)
    return base.page(request, "results.html", biz, "results", report=report, deals=deal_rows, simulated=simulated,
                     **base._labels(biz))


# ---- activity ----

@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, biz: Business = Depends(current_business), result: str = "", page_no: int = 1):
    page_no = max(1, page_no)
    with session_scope() as s:
        feed = queries.decisions(s, biz.tenant_id, False, result or None, None, 50, (page_no - 1) * 50)

    def qs(**changes) -> str:
        params = {k: v for k, v in {"result": result, "page_no": page_no, **changes}.items() if v}
        return "?" + urlencode(params)

    return base.page(request, "activity.html", biz, "activity", feed=feed, result=result, page_no=page_no,
                     more=len(feed) == 50, qs=qs, **base._labels(biz))
