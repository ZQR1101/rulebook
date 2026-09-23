"""Attribution: a verdict must be able to say which clause it rests on.

The citation gate proves a quote exists somewhere in the document; it cannot
prove the quote belongs to the clause the rule asks about. That gap is how a
green for 可用性承诺 was supported by the deduction clause's 99.95% figure — a
verbatim quote from a clause that answers a *different* rule.

These helpers only read what the model claimed. A claim about a clause the
scorer never sent is impossible, so it is evidence the verdict is not
trustworthy; no claim at all is merely uninformative and is left standing.
"""

from __future__ import annotations

import re

_ORDINAL_RE = re.compile(r"(\d+)")
_SECTION_NUMBER_RE = re.compile(r"\d+\.\d+")


def parse_ordinal(value) -> int | None:
    """Model-shape tolerance: 17, "17", "条款 17", "第17条" all mean clause 17.

    A dotted number is the document's own section numbering (「2.4」), not one of
    the ordinals the prompt labels clauses with. Guessing a clause from it would
    attribute a verdict to the wrong text, so it is treated as no claim.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        if _SECTION_NUMBER_RE.search(value):
            return None
        match = _ORDINAL_RE.search(value)
        if match:
            ordinal = int(match.group(1))
            return ordinal if ordinal > 0 else None
    return None


def split_unserved(
    citations: list, served_ordinals: set[int]
) -> tuple[list, list]:
    """Split raw citations into (usable, claiming-a-clause-that-was-never-sent).

    Only a dict carrying a parseable ``clause`` can be disproved; a bare quote
    or an unparseable claim stays with the citation gate, which is the existing
    behaviour and the one the answer key depends on.
    """

    usable: list = []
    unserved: list = []
    for citation in citations or []:
        if isinstance(citation, dict):
            claimed = parse_ordinal(citation.get("clause"))
            if claimed is not None and claimed not in served_ordinals:
                unserved.append(citation)
                continue
        usable.append(citation)
    return usable, unserved
