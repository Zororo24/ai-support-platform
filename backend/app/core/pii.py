"""Mask personal data in text before it is stored.

Used when an agent's privacy setting `redact_pii` is on. The model still sees the real text during
the conversation (it needs it to help); what is *kept* afterwards (transcripts, run previews, trace
steps) has email addresses, card numbers and phone numbers replaced. This is pattern matching, not
a guarantee: it will miss names and addresses, and it can mask a long number that is not a phone
number. Use `store_transcript: none` when nothing at all should be kept.
"""

import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
PHONE_RES = (
    re.compile(r"(?<![\w-])\+\d[\d\s().\-]{7,16}\d(?![\w-])"),  # +44 20 7946 0958
    re.compile(r"(?<![\w-])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\w-])"),  # (415) 555-0132
    re.compile(r"(?<![\w-])\d{4}[\s.\-]\d{3}[\s.\-]\d{3}(?![\w-])"),  # 0412 345 678
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[card]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group()


def mask_text(text: str) -> str:
    text = EMAIL_RE.sub("[email]", text)
    text = CARD_RE.sub(_mask_card, text)
    for pattern in PHONE_RES:
        text = pattern.sub("[phone]", text)
    return text


def mask_deep(value: Any, *, depth: int = 0) -> Any:
    """A copy of `value` with every string masked."""
    if depth > 8:
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_deep(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_deep(v, depth=depth + 1) for v in value]
    return value
