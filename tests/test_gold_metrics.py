"""Gold-alignment tests: extraction noise must not break span matching.

The external gold sets quote contract text from a different extraction than ours
(.docx/PDF/OCR plus our own clause splitter), so these tests pin the tolerance:
whitespace, markdown and case differences still hit, while coincidental overlap
does not.
"""

from __future__ import annotations

from backend.gold_metrics import (
    longest_overlap,
    normalise,
    span_hit,
    span_recall,
)

SPAN = (
    "Licensor shall have no obligation to maintain or continue to provide any "
    "Product or service, or any portion or functionality thereof."
)


def test_normalise_folds_extraction_noise():
    messy = "  Clause **12.3**\t| No &nbsp;warranty &nbsp;for &| updates |\n"

    cleaned = normalise(messy)

    assert cleaned == "clause 12.3 no warranty for updates"


def test_a_verbatim_quote_hits_even_when_whitespace_differs():
    citation = SPAN.replace(" or any ", "\n  or any")

    assert span_hit(SPAN, [citation]) is True


def test_coincidental_short_overlap_does_not_hit():
    unrelated = (
        "The Customer represents that it has full power and authority to enter "
        "into this Agreement and to perform its obligations under this Agreement."
    )

    assert span_hit(SPAN, [unrelated]) is False


def test_a_short_span_needs_its_own_text_not_a_fragment():
    short = "No warranty is provided."

    assert span_hit(short, [short]) is True
    assert span_hit(short, ["No warranty"]) is False


def test_span_recall_counts_hits_over_expert_spans():
    other = "Termination for convenience requires sixty (60) days prior written notice."

    hits, total = span_recall([SPAN, other], citations=[SPAN])

    assert (hits, total) == (1, 2)
    assert span_recall([], [SPAN]) == (0, 0)


def test_longest_overlap_is_the_shared_run_regardless_of_order():
    left = "abcdefghijklmnopqrstuvwxyz"
    right = "00000stuvwxyzabc"

    assert longest_overlap(normalise(left), normalise(right)) == 8  # "stuvwxyz"
