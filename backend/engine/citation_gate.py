"""Citation gate: every verdict must point at real document text.

A citation is a verbatim quote from the document. Validation normalizes
whitespace and checks containment; unanchored verdicts are downgraded to
human review instead of being trusted.
"""

from __future__ import annotations

import re

WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub("", text or "")


def quote_in_document(quote: str, document_text: str) -> bool:
    normalized_quote = normalize_text(quote)
    if len(normalized_quote) < 6:
        return False
    return normalized_quote in normalize_text(document_text)


def validate_citations(
    citations: list[dict],
    document_text: str,
    clause_texts: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split citations into (valid, invalid).

    A citation is valid when its ``quote`` appears in the full document text.
    ``clause_texts`` narrows matching to the clauses the scorer actually saw
    when provided. Model-shape tolerance: bare strings and {"quote": …}
    objects are both accepted; anything else is invalid, never an exception —
    a malformed citation must not fail the whole document.
    """

    valid: list[dict] = []
    invalid: list[dict] = []
    haystacks = [document_text]
    if clause_texts:
        haystacks.extend(clause_texts)
    for citation in citations or []:
        if isinstance(citation, str):
            normalized = {"quote": citation.strip()}
        elif isinstance(citation, dict):
            normalized = {"quote": str(citation.get("quote") or "").strip()}
        else:
            normalized = None
        quote = normalized["quote"] if normalized else ""
        if normalized and quote and any(quote_in_document(quote, hay) for hay in haystacks):
            valid.append(normalized)
        else:
            invalid.append(citation if isinstance(citation, dict) else {"quote": str(citation)})
    return valid, invalid
