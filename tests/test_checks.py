"""Feature checks page: runs every scenario in a separate sandbox process and shows pass/fail."""

import time as clock

from sqlalchemy import func, select

from app.db.models import ImportJob, OfferDecision
from app.db.session import session_scope
from tests.test_hardening import SAME, c, seeded  # noqa: F401


def _wait(timeout=240):
    end = clock.time() + timeout
    while clock.time() < end:
        with session_scope() as s:
            job = s.scalars(select(ImportJob).where(ImportJob.kind == "checks").order_by(ImportJob.id.desc())).first()
            if job and job.status != "running":
                return job.status, job.summary, job.error
        clock.sleep(0.5)
    raise AssertionError("checks did not finish")


def test_all_feature_checks_pass_in_their_sandbox(seeded):
    c, tid = seeded
    with session_scope() as s:
        before = s.scalar(select(func.count()).select_from(OfferDecision))
    assert "No checks run yet" in c.get("/app/checks").text
    c.post("/app/checks/run", headers=SAME)
    status, summary, error = _wait()
    assert status == "done", error
    failed = [x for x in summary["results"] if not x["passed"]]
    assert not failed, failed
    assert summary["passed"] == summary["total"] >= 20 and "copy of your offer rules" in summary["rules"]
    page = c.get("/app/checks").text
    assert f"{summary['total']} / {summary['total']}" in page and "Guardrails" in page
    with session_scope() as s:  # the sandbox never wrote into the business's database
        assert s.scalar(select(func.count()).select_from(OfferDecision)) == before
        # and an import can still start while checks exist (they don't count as a running import)
        assert s.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.kind != "checks")) == 0
