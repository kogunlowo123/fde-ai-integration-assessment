"""Task 3, PII detection and redaction.

WHAT
    Detects emails, US SSNs and payment card numbers in text and replaces each
    match with ``[REDACTED]``.

WHY
    A model can emit sensitive data it was given, retrieved, or memorised. The
    gateway is the last place that data can be stopped before it reaches a
    client, a log, or a browser cache.

HOW
    One compiled alternation with named groups, scanned left to right. Card
    candidates are normalised (separators stripped), length-checked (13-19
    digits) and Luhn-validated before redaction, so "the year 2024 order
    1234567890123456" is only redacted when the digits really do form a
    plausible card number.

WHEN
    Use ``redact`` for whole strings and
    :class:`~fde_assessment.llm_gateway.guardrails.streaming.StreamingRedactor`
    for token streams, the streaming wrapper adds the chunk-boundary logic.

SECURITY
    This is a detection control, not a proof. Documented limitations:

    * **Recall.** Only three well-known patterns. Names, addresses, phone
      numbers, passport and national-insurance numbers, IBANs, API keys and
      free-text medical detail are *not* detected. A deployment with a real
      confidentiality requirement pairs this with a classifier or a
      customer-specific pattern pack.
    * **Precision.** Luhn is a checksum, not a proof of cardness: a 16-digit
      identifier that happens to satisfy Luhn is redacted. Redaction is the
      safe direction of that error.
    * **Obfuscation.** ``j o h n @ e x a m p l e . c o m``, base64, homoglyphs
      and "at"/"dot" spellings defeat regex matching entirely. Regex guardrails
      raise the cost of accidental disclosure; they do not stop a model that is
      deliberately encoding data.
    * **Locale.** SSN matching is US-specific.

COST
    Pure CPU, no model call, microseconds per kilobyte. Benchmarked in
    ``scripts/benchmark.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

REDACTION: Final = "[REDACTED]"

# Longest span the scanner is expected to handle in one piece. Also the default
# ceiling on the streaming carry buffer (ADR-005).
MAX_MATCH_LENGTH: Final = 128

_EMAIL = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}"
# US SSN: 3-2-4, separated by '-' or a single space. Not \b-anchored on the
# left alone, a 4-digit prefix must not turn 1234-56-7890 into a "match".
_SSN = r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)"
# Card candidate: 13-19 digits, optionally grouped by single spaces or hyphens.
_CARD = r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])"

PATTERN: Final = re.compile(
    rf"(?P<email>{_EMAIL})|(?P<ssn>{_SSN})|(?P<card>{_CARD})",
)

# Suffix patterns: does the tail of a buffer look like the beginning of a
# match that has not arrived yet? Used by the streaming redactor to decide how
# much text it must hold back.
_PARTIAL_PATTERNS: Final = (
    re.compile(r"[A-Za-z0-9._%+\-]+(?:@[A-Za-z0-9.\-]*)?$"),
    re.compile(r"(?:\d[ \-]?)+$"),
)


def luhn_check(digits: str) -> bool:
    """Return True when ``digits`` satisfies the Luhn checksum.

    Rejects empty strings and anything non-numeric. The doubling walk goes
    right to left, doubling every second digit and subtracting 9 when the
    doubled value exceeds 9.
    """
    # `str.isdigit()` is True for full-width and other Unicode digits, whose
    # code points are not `ord(c) - 48`, so the checksum arithmetic below would
    # be meaningless. Require ASCII.
    if not digits or not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def is_card_candidate(raw: str) -> bool:
    """True when ``raw`` normalises to a Luhn-valid 13-19 digit number."""
    digits = raw.replace(" ", "").replace("-", "")
    return 13 <= len(digits) <= 19 and luhn_check(digits)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Redacted text plus per-category counts."""

    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _should_redact(kind: str, matched: str) -> bool:
    """Second-stage validation for a candidate match."""
    if kind == "card":
        return is_card_candidate(matched)
    return True


def scan(text: str, *, allow_trailing_match: bool = True) -> tuple[str, int, dict[str, int]]:
    """Redact ``text``, returning ``(redacted_prefix, consumed, counts)``.

    ``consumed`` is how many characters of ``text`` were fully processed. When
    ``allow_trailing_match`` is False, a match that ends exactly at the end of
    ``text`` is treated as possibly incomplete: scanning stops before it and
    ``consumed`` points at its start. That is what lets the streaming redactor
    avoid redacting ``user@example.co`` a moment before the ``m`` arrives.
    """
    parts: list[str] = []
    counts: dict[str, int] = {}
    cursor = 0

    for match in PATTERN.finditer(text):
        kind = match.lastgroup or "unknown"
        if not allow_trailing_match and match.end() == len(text):
            break
        if not _should_redact(kind, match.group()):
            continue
        parts.append(text[cursor : match.start()])
        parts.append(REDACTION)
        counts[kind] = counts.get(kind, 0) + 1
        cursor = match.end()

    return "".join(parts), cursor, counts


def redact(text: str) -> RedactionResult:
    """Redact every complete match in ``text``."""
    prefix, cursor, counts = scan(text, allow_trailing_match=True)
    return RedactionResult(prefix + text[cursor:], counts)


def partial_match_start(text: str, max_lookback: int = MAX_MATCH_LENGTH) -> int:
    """Index from which ``text`` might still grow into a match.

    Returns ``len(text)`` when the tail cannot begin a match, which is the
    common case and lets the streaming redactor emit immediately, the reason
    the guardrail costs almost nothing in time-to-first-token for ordinary
    prose.
    """
    window_start = max(0, len(text) - max_lookback)
    tail = text[window_start:]
    earliest = len(text)
    for pattern in _PARTIAL_PATTERNS:
        match = pattern.search(tail)
        if match is not None:
            earliest = min(earliest, window_start + match.start())
    return earliest
