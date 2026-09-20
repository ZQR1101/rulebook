"""Fuzzy alignment between engine citations and externally annotated spans.

Public legal gold sets (CUAD, ContractNLI) mark the clauses a professional
considered relevant, but their text rarely matches ours byte-for-byte: the same
contract goes through .docx, PDF, OCR and our own clause splitter, so offsets
and whitespace differ. Exact offset arithmetic therefore fails; containment by
longest common run is what survives that pipeline.

A span counts as *hit* when some citation shares a contiguous run of at least
``MIN_OVERLAP_CHARS`` with it. That threshold is deliberately blunt: it accepts
genuinely quoted text and rejects coincidental phrase overlap ("the foregoing"
style boilerplate cannot reach 60 characters).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

MIN_OVERLAP_CHARS = 60

_MD_ARTIFACTS = re.compile(r"[#*_`|>]+")
_WS = re.compile(r"\s+")
_HTML_ENTITY = re.compile(r"&[a-z0-9#]*;?", re.IGNORECASE)


def normalise(text: str) -> str:
    """Fold away the differences that extraction introduces."""

    text = _HTML_ENTITY.sub(" ", text or "")
    text = _MD_ARTIFACTS.sub(" ", text)
    return _WS.sub(" ", text).strip().lower()


def longest_overlap(left: str, right: str) -> int:
    """Length of the longest contiguous run shared by two normalised strings."""

    if not left or not right:
        return 0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    return matcher.find_longest_match(0, len(left), 0, len(right)).size


def span_hit(span: str, citations: list[str], *, min_chars: int = MIN_OVERLAP_CHARS) -> bool:
    target = normalise(span)
    if not target:
        return False
    if len(target) < min_chars:  # a short span can only be matched exactly-ish
        min_chars = max(20, len(target))
    return any(longest_overlap(normalise(quote), target) >= min_chars for quote in citations)


def span_recall(spans: list[str], citations: list[str]) -> tuple[int, int]:
    """(hit, total) over expert spans — the localisation half of the score."""

    return (sum(1 for span in spans if span_hit(span, citations)), len(spans))
