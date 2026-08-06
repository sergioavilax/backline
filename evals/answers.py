"""Answer extraction: the output contracts questions impose (`ANSWER:` / `FLAG:` lines).

Every suite prompt states its exact final-line contract; extraction is therefore
mechanical, and an answer that ignores the contract is a scoring failure with an
explicit ``extraction`` detail — never a fuzzy match. Money parsing returns
``Decimal`` (never float, invariant 1).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_ANSWER_LINE = re.compile(r"^\s*ANSWER:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_FLAG_LINE = re.compile(
    r"^\s*FLAG:\s*(?P<kind>[a-z_]+)\s+(?P<source>label|staged):(?P<line_id>\d+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_MONEY = re.compile(r"-?[\d,]+(?:\.\d+)?")


def extract_answer(text: str) -> str | None:
    """The LAST `ANSWER:` line wins (models sometimes restate while reasoning)."""
    matches = _ANSWER_LINE.findall(text)
    return matches[-1] if matches else None


def extract_flags(text: str) -> set[tuple[str, str, int]]:
    """All `FLAG: <kind> <source>:<line_id>` lines as (kind, source, line_id)."""
    return {
        (m.group("kind").lower(), m.group("source").lower(), int(m.group("line_id")))
        for m in _FLAG_LINE.finditer(text)
    }


def parse_money(raw: str) -> Decimal | None:
    """'$1,234.56', '1234.56 USD', '-12.00' → Decimal. None when nothing parses."""
    cleaned = raw.replace("USD", "").replace("usd", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    match = _MONEY.search(cleaned)
    if match is None:
        return None
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    return -value if negative and value > 0 else value


def parse_count(raw: str) -> int | None:
    match = _MONEY.search(raw)
    if match is None or "." in match.group(0):
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_percent(raw: str) -> Decimal | None:
    """Royalty points: '30%' → 30; '0.30' (rate form) → 30; '30' → 30."""
    cleaned = raw.strip()
    had_pct = cleaned.endswith("%")
    value = parse_money(cleaned.rstrip("%"))
    if value is None:
        return None
    if not had_pct and value < 1:
        value = value * 100
    return value.normalize()


def parse_bool(raw: str) -> str | None:
    token = raw.strip().strip(".").casefold()
    if token in {"yes", "y", "true"}:
        return "YES"
    if token in {"no", "n", "false"}:
        return "NO"
    return None


def parse_period(raw: str) -> str | None:
    match = re.search(r"\b(\d{4}-\d{2})\b", raw)
    return match.group(1) if match else None


def parse_set(raw: str) -> set[str]:
    """Semicolon-separated names (comma fallback), whitespace/case-normalized."""
    separator = ";" if ";" in raw else ","
    return {" ".join(part.split()).casefold() for part in raw.split(separator) if part.strip()}


def normalize_value(raw: str) -> str:
    return " ".join(raw.split()).strip().strip(".").casefold()
