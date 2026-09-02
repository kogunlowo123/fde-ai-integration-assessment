"""Deterministic chunking (Production Enhancement).

WHAT
    Splits document text into overlapping, size-bounded chunks on paragraph
    and sentence boundaries where it can, and hard-splits only when a single
    span exceeds the size.

WHY chunk size and overlap matter
    * **Too large** -> each hit spends a lot of the context budget, so fewer
      hits fit; irrelevant text rides along with the relevant sentence,
      diluting the signal and inflating cost and latency; and the
      prompt-injection surface grows with every extra character of untrusted
      text.
    * **Too small** -> the passage loses the context that made it answer the
      question ("it must be returned within 30 days" without saying what
      "it" is), and recall drops because the embedding of a fragment is
      noisier.
    * **Overlap** stops an answer that straddles a boundary from being lost by
      both chunks. It costs storage and embedding time proportional to the
      overlap fraction, and it makes near-duplicate hits more likely, which is
      why the retriever de-duplicates by document.

    512 characters with 64 of overlap (12.5%) is a defensible default for
    policy and support documentation: roughly a paragraph, which is the unit
    such documents are actually written in.

HOW
    Split on blank lines, accumulate paragraphs until the size limit, then
    carry the last ``overlap`` characters into the next chunk. Deterministic:
    the same input always produces the same chunks, which is what makes
    content-hash-based re-ingestion skipping correct.

WHEN
    Called by the ingestion pipeline. Tune per corpus, not per request.
"""

from __future__ import annotations

import re

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    """Collapse horizontal whitespace and normalise line endings.

    Normalisation happens before hashing so that a document reformatted with
    different line endings is recognised as unchanged and not re-embedded.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _split_oversized(span: str, size: int) -> list[str]:
    """Break a span that is itself larger than ``size``."""
    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(span):
        if len(sentence) <= size:
            pieces.append(sentence)
            continue
        pieces.extend(sentence[i : i + size] for i in range(0, len(sentence), size))
    return [p for p in pieces if p]


def chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split ``text`` into overlapping chunks of at most ``chunk_size``."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    cleaned = normalize(text)
    if not cleaned:
        return []

    spans: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(cleaned):
        stripped = paragraph.strip()
        if not stripped:
            continue
        spans.extend(
            [stripped] if len(stripped) <= chunk_size else _split_oversized(stripped, chunk_size)
        )

    chunks: list[str] = []
    current = ""
    for span in spans:
        candidate = f"{current}\n\n{span}" if current else span
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{span}".strip() if tail else span
            # An overlap tail plus a large span can itself exceed the size;
            # in that case the span starts a fresh chunk without the tail.
            if len(current) > chunk_size:
                current = span
        else:
            current = span

    if current:
        chunks.append(current)
    return chunks
