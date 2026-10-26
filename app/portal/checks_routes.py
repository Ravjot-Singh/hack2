"""Portal "Feature checks": run every feature scenario on a throwaway sandbox (a separate process with its own
temporary database, a fake Telegram and the AI off) and show expected vs actual with a pass/fail per feature."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.config import ROOT_DIR
from app.db.models import ImportJob
from app.db.session import session_scope
from app.ingest import jobs
from app.portal import routes as base
from app.portal.auth import Business, current_business

router = APIRouter(prefix="/app/checks")
TIMEOUT_S = 600


def run_checks(job_id: int, rules: dict, now_fn) -> None:
    """Background job: the checks run in a child process so they can never touch this process's database."""
    def work() -> dict:
        jobs._stage(job_id, "Building a sandbox store and running every check")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules), encoding="utf-8")
            env = {**os.environ, "DATABASE_URL": f"sqlite:///{Path(tmp) / 'checks.db'}", "PUBLIC_BASE_URL": "",
                   "LLM_ENABLED": "false", "GROQ_API_KEY": "", "SHOPKART_BOT_TOKEN": "", "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run([sys.executable, "-m", "app.checks", "--rules", str(path)], cwd=ROOT_DIR, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S)
        lines = [x for x in proc.stdout.strip().splitlines() if x.startswith("{")]
        if proc.returncode != 0 or not lines:
            raise RuntimeError((proc.stderr or "The checks didn't finish.").strip().splitlines()[-1][:280])
        return json.loads(lines[-1])
    jobs._finish(job_id, work, now_fn)


def _latest(biz: Business) -> dict | None:
    with session_scope() as s:
        j = s.scalars(select(ImportJob).where(ImportJob.tenant_id == biz.tenant_id, ImportJob.kind == "checks")
                      .order_by(ImportJob.id.desc()).limit(1)).first()
        return None if j is None else {k: getattr(j, k) for k in ("id", "status", "created_at", "finished_at",
                                                                  "stage", "summary", "error")}


@router.get("", response_class=HTMLResponse)
def checks(request: Request, biz: Business = Depends(current_business)):
    job = _latest(biz)
    results = (job or {}).get("summary") or {}
    groups: dict[str, list] = {}
    for x in results.get("results", []):
        groups.setdefault(x["feature"], []).append(x)
    return base.page(request, "checks.html", biz, "checks", job=job, results=results, groups=groups)


@router.post("/run")
async def run(request: Request, biz: Business = Depends(current_business)):
    with session_scope() as s:
        busy = s.scalar(select(ImportJob.id).where(ImportJob.tenant_id == biz.tenant_id, ImportJob.kind == "checks",
                                                   ImportJob.status == "running"))
        if busy is None:
            job = ImportJob(tenant_id=biz.tenant_id, kind="checks", status="running", created_at=base._now(),
                            stage="Starting", summary={})
            s.add(job)
            s.flush()
            job_id = job.id
    if busy is None:
        jobs.start(run_checks, job_id, biz.policy.rules.model_dump(mode="json"), base._now)
    return RedirectResponse("/app/checks", status_code=303)
