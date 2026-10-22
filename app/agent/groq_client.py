"""Groq access with hard guards so the free tier is never exceeded.

A call only happens if: LLM_ENABLED is true, a key is set, fewer than LLM_MAX_CALLS_PER_MINUTE calls ran in the
last minute (in-process), and fewer than LLM_MAX_CALLS_PER_DAY ran today (counted in the database, so restarts
don't reset it). Every attempt is logged in llm_calls. Any failure returns None and the caller falls back.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, time as dtime
from typing import Protocol

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import LLMCall
from app.db.session import session_scope

log = logging.getLogger(__name__)


class JsonLLM(Protocol):
    def __call__(self, tenant_id: int, purpose: str, system: str, user: str, now: datetime) -> dict | None: ...


_lock = threading.Lock()
_recent: deque[float] = deque()


def _minute_slot_free(limit: int) -> bool:
    with _lock:
        cutoff = time.monotonic() - 60
        while _recent and _recent[0] < cutoff:
            _recent.popleft()
        if len(_recent) >= limit:
            return False
        _recent.append(time.monotonic())
        return True


def calls_today(now: datetime) -> int:
    midnight = datetime.combine(now.date(), dtime(0), now.tzinfo)
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.created_at >= midnight)) or 0


def llm_status(now: datetime) -> str | None:
    """None if a call may be made now, otherwise why not."""
    st = get_settings()
    if not st.llm_enabled:
        return "LLM is switched off (LLM_ENABLED=false)"
    if not st.groq_api_key:
        return "no GROQ_API_KEY set"
    if calls_today(now) >= st.llm_max_calls_per_day:
        return f"daily LLM limit reached ({st.llm_max_calls_per_day})"
    return None


def _log(tenant_id: int, purpose: str, now: datetime, ok: bool, started: float, usage=None, error: str | None = None):
    with session_scope() as s:
        s.add(LLMCall(tenant_id=tenant_id, created_at=now, purpose=purpose, ok=ok,
                      latency_ms=int((time.monotonic() - started) * 1000),
                      prompt_tokens=getattr(usage, "prompt_tokens", None),
                      completion_tokens=getattr(usage, "completion_tokens", None),
                      error=(error or "")[:300] or None))


def groq_json(tenant_id: int, purpose: str, system: str, user: str, now: datetime) -> dict | None:
    """Ask Groq for a JSON object. Returns None when not allowed or on any error."""
    st = get_settings()
    blocked = llm_status(now)
    if blocked:
        log.info("LLM skipped: %s", blocked)
        return None
    if not _minute_slot_free(st.llm_max_calls_per_minute):
        log.info("LLM skipped: per-minute limit reached")
        return None

    from groq import Groq  # imported lazily so tests and LLM-off runs never need the network

    started = time.monotonic()
    try:
        client = Groq(api_key=st.groq_api_key, timeout=10, max_retries=0)
        resp = client.chat.completions.create(
            model=st.groq_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=1000,  # reasoning models spend part of this thinking before the JSON
            extra_body={"reasoning_effort": "low"} if "gpt-oss" in st.groq_model else None,
        )
        data = json.loads(resp.choices[0].message.content or "")
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        _log(tenant_id, purpose, now, True, started, resp.usage)
        return data
    except Exception as exc:  # network, rate limit, bad JSON: all mean "fall back to rules"
        log.warning("LLM call failed: %s", exc)
        _log(tenant_id, purpose, now, False, started, error=f"{type(exc).__name__}: {exc}")
        return None
