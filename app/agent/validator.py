"""Checks every AI choice against the rules before anything is sent. The validator always wins.

The AI may only (1) pick an offer from the rule-approved shortlist and (2) write a short intro. What is offered
(amount, terms, validity, coupon) is rendered by code from the Candidate, so the intro must not state or
imply any of it. Anything suspicious is rejected and the rules engine's own pick + template is used instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.rules.engine import Candidate, CatalogInfo

MAX_TITLE = 60
MAX_BODY = 240

PATTERNS: list[tuple[str, str]] = [
    (r"\d", "states a number (amounts, % and dates come only from the offer box)"),
    (r"[₹%$€£]", "mentions money or a percentage"),
    (r"\b(rs\.?|inr|rupees?|paise|percent|per cent)\b", "mentions money or a percentage"),
    (r"\b(half|double|triple|twice|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty"
     r"|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|crore)\b", "states an amount in words"),
    (r"\b(today|tonight|tomorrow|hours?|minutes?|midnight|ends soon|last chance|expires?|expiring|deadline)\b",
     "makes a timing claim (validity comes from the offer box)"),
    (r"\b(guarantee[ds]?|unlimited|lifetime|cash\s?back|refund|no questions asked|forever|gift|bogo)\b",
     "promises something the offer doesn't include"),
    (r"https?://|www\.|\.com\b|t\.me|@\w", "contains a link or handle"),
    (r"<[^>]*>|&[a-z]+;", "contains HTML"),
    (r"\b(ignore|instructions?|system prompt|prompt|as an ai|language model|jailbreak)\b",
     "looks like prompt-injection text"),
]
TYPE_WORDS = {
    "free": ("free_delivery", "says 'free' but the offer is not free delivery"),
    "points": ("bonus_points", "mentions points but the offer is not bonus points"),
    "delivery": ("free_delivery", "mentions delivery but the offer is not free delivery"),
    "shipping": ("free_delivery", "mentions shipping but the offer is not free delivery"),
}


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    candidate: Candidate | None = None
    title: str = ""
    body: str = ""


def _clean(v) -> str:
    return " ".join(str(v).split()) if isinstance(v, str) else ""


def validate_ai_choice(raw: dict, shortlist: list[Candidate], catalog: CatalogInfo) -> Validation:
    problems: list[str] = []
    if not isinstance(raw, dict):
        return Validation(False, ["response is not a JSON object"])
    key = raw.get("offer_key")
    cand = next((c for c in shortlist if c.offer_key == key), None)
    if cand is None:
        problems.append(f"chose '{key}', which is not in the rule-approved shortlist "
                        f"({', '.join(c.offer_key for c in shortlist)})")
    title, body = _clean(raw.get("title")), _clean(raw.get("body"))
    if not title or not body:
        problems.append("title or body is missing")
    if len(title) > MAX_TITLE:
        problems.append(f"title is {len(title)} characters (max {MAX_TITLE})")
    if len(body) > MAX_BODY:
        problems.append(f"body is {len(body)} characters (max {MAX_BODY})")

    text = f"{title}\n{body}".lower()
    for pattern, why in PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            problems.append(why)
    if cand is not None:
        for word, (allowed_type, why) in TYPE_WORDS.items():
            if re.search(rf"\b{word}\b", text) and cand.type != allowed_type:
                problems.append(why)
        allowed = {cand.category} if cand.category != "any" else set(catalog.labels) - set(cand.excluded_categories)
        for cat, label in catalog.labels.items():
            names = {cat.replace("_", " "), label.lower()} | {p for p in cat.split("_") if len(p) > 4}
            if cat not in allowed and any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
                problems.append(f"mentions {label}, which this offer does not cover")
                break
    return Validation(not problems, problems, cand, title, body)
