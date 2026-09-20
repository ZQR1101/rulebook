"""Clause retrieval for rule-vs-document scoring.

Three modes, selected by ``RETRIEVAL_MODE`` (default ``hybrid``):

- keyword  : deterministic token overlap (v1 behaviour, zero dependencies)
- semantic : embedding cosine similarity between rule text and clause text
- hybrid   : 0.6 * normalized semantic + 0.4 * normalized keyword

Semantic mode reuses the local sentence-transformers model from
``rag_store.get_embedding_model``. The embedder is created once per document
run and pre-encodes all clauses; any failure degrades explicitly to keyword
retrieval (audited by the orchestrator) — never silently loses recall.
"""

from __future__ import annotations

import re
from functools import lru_cache

from backend.engine.parsing import ParsedClause

MAX_RULE_CONTEXT_CHARS = 6000
MIN_TOP = 3
SEMANTIC_WEIGHT = 0.6
KEYWORD_WEIGHT = 0.4
# CJK bigrams are roughly one token per character, so the old 24-token ceiling
# would have truncated a rule's guidance before its distinctive tail.
MAX_QUERY_KEYWORDS = 64
_TOKEN_RE = re.compile("[A-Za-z]+|\\d+(?:\\.\\d+)+|[\\u4e00-\\u9fff]+")
_CJK_RUN = re.compile("[\\u4e00-\\u9fff]+")


STOPWORDS = {
    "的", "了", "和", "是", "在", "有", "与", "及", "或", "对", "为", "不", "按",
    "检查", "应当", "必须", "条款", "内容", "相关", "情况", "要求", "是否",
    "the", "and", "for", "with", "that", "this", "are", "not", "shall", "must",
}


@lru_cache(maxsize=512)
def _keywords(text: str) -> tuple[str, ...]:
    """Query keywords: latin/number runs whole, CJK runs as overlapping bigrams.

    Slicing Chinese into non-overlapping windows made every match boundary
    dependent: 「可用性承诺」 cut as 「可用性承」+「诺是多少」 matched neither
    「服务可用性」 nor 「乙方承诺」, so a rule scored 0 against a document that
    plainly contained its subject. Overlapping bigrams match wherever the two
    sides break.
    """

    seen: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        chunk = match.group(0)
        if _CJK_RUN.fullmatch(chunk):
            tokens = [chunk[index : index + 2] for index in range(len(chunk) - 1)]
        else:
            tokens = [chunk]
        for token in tokens:
            if token in STOPWORDS or len(token) < 2:
                continue
            if token not in seen:
                seen.append(token)
    return tuple(seen[:MAX_QUERY_KEYWORDS])


def score_clause(clause_text: str, query_keywords: tuple[str, ...]) -> int:
    haystack = clause_text.lower()
    score = 0
    for keyword in query_keywords:
        if keyword in haystack:
            score += 2 if len(keyword) >= 3 else 1
    return score


class ClauseEmbedder:
    """Per-document embedder: encodes the clause set once, scores rules cheaply."""

    def __init__(self, model):
        self._model = model
        self._clause_matrix = None

    def ensure_ready(self, clauses: list[ParsedClause]) -> bool:
        """Encode all clauses once. Returns False (and clears state) on failure."""
        try:
            texts = [clause.text for clause in clauses]
            if not texts:
                return False
            self._clause_matrix = self._model.encode(texts, normalize_embeddings=True)
            return True
        except Exception:
            self._clause_matrix = None
            return False

    def semantic_scores(self, rule_text: str) -> list[float] | None:
        """Cosine similarity of rule text against the pre-encoded clause matrix."""
        if self._clause_matrix is None:
            return None
        try:
            import numpy as np

            query = self._model.encode([rule_text], normalize_embeddings=True)[0]
            return [float(np.dot(query, row)) for row in self._clause_matrix]
        except Exception:
            return None


def get_clause_embedder() -> ClauseEmbedder | None:
    """Build a ClauseEmbedder from the local embedding model, or None on failure."""

    try:
        from backend.rag_store import get_embedding_model

        model = get_embedding_model()
        if model is None:
            return None
        return ClauseEmbedder(model)
    except Exception:
        return None


def select_clauses(
    clauses: list[ParsedClause],
    rule_text: str,
    *,
    char_budget: int = MAX_RULE_CONTEXT_CHARS,
    min_top: int = MIN_TOP,
    embedder: ClauseEmbedder | None = None,
    mode: str = "hybrid",
) -> list[ParsedClause]:
    """Return clauses ranked by relevance, bounded by a char budget.

    Without an embedder (or in keyword mode) this is the v1 token-overlap
    ranking. With semantic/hybrid mode and a ready embedder, cosine similarity
    participates; any embedder failure degrades to keyword automatically.

    A clause with no keyword signal at all carries no evidence for this rule, so
    it is never selected. The result may legitimately be empty, and empty is the
    scorer's evidence that the document lacks the content: padding up to
    ``min_top`` instead handed it filler to cite, which made an honest
    "文档未约定该项" unreachable. ``min_top`` is a floor among candidates that
    do carry signal. A document that fits the budget entirely is returned whole —
    nothing is scarce, so nothing is filtered.
    """

    if clauses and sum(len(clause.text) for clause in clauses) <= char_budget:
        # Nothing has to be thrown away, so don't narrow anything: relevance
        # ranking only earns its keep when the budget is scarce. Dropping
        # non-matching clauses here would hide text the scorer could have read
        # for free and turn a retrieval choice into a false "文档缺失该项".
        return sorted(clauses, key=lambda clause: clause.ordinal)

    keywords = _keywords(rule_text)
    keyword_scores = {clause.ordinal: score_clause(clause.text, keywords) for clause in clauses}

    semantic_scores: list[float] | None = None
    if embedder is not None and mode in ("semantic", "hybrid"):
        semantic_scores = embedder.semantic_scores(rule_text)

    def combined_score(clause: ParsedClause) -> float:
        keyword = float(keyword_scores[clause.ordinal])
        if semantic_scores is None:
            return keyword
        max_keyword = max(keyword_scores.values()) or 1
        keyword_norm = keyword / max_keyword
        semantic_norm = (semantic_scores[clause.ordinal - 1] + 1) / 2  # cosine [-1,1] → [0,1]
        if mode == "semantic":
            return semantic_norm
        return SEMANTIC_WEIGHT * semantic_norm + KEYWORD_WEIGHT * keyword_norm

    # Floor is "any signal at all", deliberately not a tuned threshold: a cutoff
    # of 2 caught 9/10 labelled gaps but dropped 6 of the 34 gold retrieval
    # cases, because a clause matched by a single bigram is often the right
    # clause. Lexical score cannot tell "weak but correct" from "stray word".
    ranked = sorted(
        (pair for pair in enumerate(clauses) if combined_score(pair[1]) > 0),
        key=lambda pair: (-combined_score(pair[1]), pair[0]),
    )

    selected: list[ParsedClause] = []
    used = 0
    for _, clause in ranked:
        cost = len(clause.text)
        if selected and used + cost > char_budget:
            continue
        if not selected and cost > char_budget:
            selected.append(
                ParsedClause(
                    ordinal=clause.ordinal,
                    heading=clause.heading,
                    text=clause.text[:char_budget],
                )
            )
            used += char_budget
            break
        selected.append(clause)
        used += cost
        if len(selected) >= min_top and used >= char_budget:
            break
    if len(selected) < min_top:
        chosen_ordinals = {clause.ordinal for clause in selected}
        for _, clause in ranked:
            if clause.ordinal in chosen_ordinals:
                continue
            selected.append(clause)
            chosen_ordinals.add(clause.ordinal)
            if len(selected) >= min_top:
                break
    return sorted(selected, key=lambda clause: clause.ordinal)
