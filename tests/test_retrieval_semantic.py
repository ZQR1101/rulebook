"""Semantic/hybrid clause retrieval tests with a deterministic fake embedder.

No network, no real model: the fake embedder maps clause text to vectors by
simple token hashing so relevance can be scripted exactly.
"""

from __future__ import annotations

import pytest

from backend.engine.parsing import ParsedClause
from backend.engine.retrieval import (
    ClauseEmbedder,
    get_clause_embedder,
    select_clauses,
)
from test_engine_pipeline import (  # noqa: F401  (fixtures shared via import)
    SAMPLE_CONTRACT,
    admin_headers,
    client,
    fake_llm,
    platform_env,
)


class FakeEmbedder(ClauseEmbedder):
    """Deterministic embedder: vectors come from a scripted per-clause map."""

    def __init__(self, vectors: dict[str, list[float]]):
        super().__init__(model=None)
        self._vectors = vectors

    def ensure_ready(self, clauses: list[ParsedClause]) -> bool:
        try:
            self._clause_matrix = [self._vectors[c.text] for c in clauses]
            self._clause_ordinals = [c.ordinal for c in clauses]
            return True
        except (KeyError, AttributeError):
            self._clause_matrix = None
            return False

    def semantic_scores(self, rule_text: str) -> list[float] | None:
        if self._clause_matrix is None:
            return None
        query = self._vectors[rule_text]
        return [
            sum(a * b for a, b in zip(query, row))
            for row in self._clause_matrix
        ]


CLAUSES = [
    ParsedClause(ordinal=1, heading=None, text="付款账期为验收合格后四十五个工作日内支付。"),
    ParsedClause(ordinal=2, heading=None, text="甲方在收到发票后四十五日内支付服务费用。"),  # 同义改写，无关键词重合
    ParsedClause(ordinal=3, heading=None, text="乙方提供7x24小时热线支持服务。"),
]

# 语义空间：规则向量与条款2最近（同义付款条款），关键词只命中条款1
VECTORS = {
    "付款账期 账期不超过 60 天为绿；61–90 天为黄；超过 90 天为红。": [1.0, 0.9, 0.0],
    "付款账期为验收合格后四十五个工作日内支付。": [0.9, 0.2, 0.1],
    "甲方在收到发票后四十五日内支付服务费用。": [0.95, 0.85, 0.0],
    "乙方提供7x24小时热线支持服务。": [0.0, 0.1, 1.0],
}
RULE = "付款账期 账期不超过 60 天为绿；61–90 天为黄；超过 90 天为红。"


class TestSemanticRetrieval:
    """select_clauses returns document order; ranking decides WHO fits the
    char budget. Use a tight budget (40 chars, min_top=2) so selection
    actually discriminates between modes."""

    BUDGET = dict(char_budget=40, min_top=2)

    def test_keyword_mode_misses_paraphrased_clause(self):
        selected = select_clauses(CLAUSES, RULE, **self.BUDGET)
        ordinals = {c.ordinal for c in selected}
        # 关键词只命中条款1；条款2同义改写落选，条款3（热线支持）与本规则零相关，
        # 不再为凑满 40 字预算被当填充项拉进来（旧行为 {1,3} 正是那个缺陷）
        assert ordinals == {1}

    def test_semantic_mode_selects_paraphrased_clause(self):
        embedder = FakeEmbedder(VECTORS)
        assert embedder.ensure_ready(CLAUSES) is True
        selected = select_clauses(CLAUSES, RULE, embedder=embedder, mode="semantic", **self.BUDGET)
        ordinals = {c.ordinal for c in selected}
        assert ordinals == {2, 3}  # 语义把同义改写的条款2选进来，热线条款3让位

    def test_hybrid_keeps_keyword_anchor_over_pure_semantic(self):
        embedder = FakeEmbedder(VECTORS)
        embedder.ensure_ready(CLAUSES)
        selected = select_clauses(CLAUSES, RULE, embedder=embedder, mode="hybrid", **self.BUDGET)
        ordinals = {c.ordinal for c in selected}
        assert ordinals == {1, 3}  # 混合权重下关键词锚点条款1 险胜纯语义条款2

    def test_embedder_failure_degrades_to_keyword(self):
        class BrokenEmbedder(FakeEmbedder):
            def semantic_scores(self, rule_text):
                return None  # 模拟推理失败

        embedder = BrokenEmbedder(VECTORS)
        embedder.ensure_ready(CLAUSES)
        selected = select_clauses(CLAUSES, RULE, embedder=embedder, mode="hybrid", **self.BUDGET)
        assert {c.ordinal for c in selected} == {1}  # 自动降级关键词，不抛异常

    def test_document_order_preserved_in_result(self):
        embedder = FakeEmbedder(VECTORS)
        embedder.ensure_ready(CLAUSES)
        selected = select_clauses(CLAUSES, RULE, embedder=embedder, mode="semantic")
        assert [c.ordinal for c in selected] == sorted(c.ordinal for c in selected)

    def test_ensure_ready_failure_clears_state(self):
        embedder = FakeEmbedder(VECTORS)
        assert embedder.ensure_ready([ParsedClause(ordinal=1, heading=None, text="未知文本")]) is False
        assert embedder.semantic_scores(RULE) is None


class TestAbsenceIsReportable:
    """A rule that matches nothing must be allowed to come back empty.

    Padding to ``MIN_TOP`` handed the scorer zero-relevance text to cite for
    content the document did not contain, so an honest "文档未约定该项" was
    structurally unreachable — in the CUAD run the absence-agreement rate could
    only ever be 0%.
    """

    ENGLISH_CONTRACT = [
        ParsedClause(ordinal=1, heading=None, text="The Supplier shall deliver the Products in each month."),
        ParsedClause(ordinal=2, heading=None, text="Manufacturer grants Distributor a non-exclusive license."),
        ParsedClause(ordinal=3, heading=None, text="Prices are payable within forty-five days of invoice."),
    ]

    # 三段正文约 184 字；预算给 120 才构成"预算紧张"，才会触发证据筛选
    SCARCE = dict(char_budget=120, mode="keyword")

    def test_no_candidate_survives_a_rule_that_matches_nothing(self):
        # 中文规则 × 英文正文：词面重叠为 0，检索必须给出空集而非凑三段填充
        assert select_clauses(self.ENGLISH_CONTRACT, "审计权 检查是否约定审计对方账簿记录", **self.SCARCE) == []

    def test_min_top_does_not_resurrect_filler(self):
        selected = select_clauses(
            self.ENGLISH_CONTRACT,
            "审计权 检查是否约定审计对方账簿记录",
            min_top=3,
            **self.SCARCE,
        )
        assert selected == []

    def test_single_relevant_clause_survives_alone(self):
        selected = select_clauses(self.ENGLISH_CONTRACT, "invoice payment 付款", **self.SCARCE)
        assert [clause.ordinal for clause in selected] == [3]  # min_top=3 不会补进 1、2

    def test_a_document_that_fits_the_budget_is_returned_whole(self):
        """预算不紧张时什么都不筛：该看的全文都得看到。"""
        selected = select_clauses(
            self.ENGLISH_CONTRACT, "审计权 检查是否约定审计对方账簿记录", char_budget=6000, mode="keyword"
        )
        assert [clause.ordinal for clause in selected] == [1, 2, 3]

    def test_empty_candidates_become_an_explicit_absence_instruction(self):
        from types import SimpleNamespace

        from backend.engine.scoring import _build_prompt

        rule = SimpleNamespace(dimension="监管", name="审计权", guidance="检查是否约定审计权")
        prompt = _build_prompt(rule, [], "审阅指令")
        assert "未检索到与本规则相关的条款" in prompt
        assert "不得引用任何原文" in prompt
        # 有候选时这句不能出现，否则会教模型把正常条款误报成缺失
        assert "未检索到" not in _build_prompt(rule, self.ENGLISH_CONTRACT, "审阅指令")


class TestEmbedderFactory:
    def test_factory_returns_none_when_model_unavailable(self, monkeypatch):
        def broken():
            raise RuntimeError("model not available")

        monkeypatch.setattr("backend.rag_store.get_embedding_model", broken)
        assert get_clause_embedder() is None

    def test_factory_returns_none_when_model_none(self, monkeypatch):
        monkeypatch.setattr("backend.rag_store.get_embedding_model", lambda: None)
        assert get_clause_embedder() is None


class TestRetrievalModeConfig:
    def test_default_is_hybrid(self, monkeypatch):
        monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
        from backend.config import read_retrieval_mode

        assert read_retrieval_mode() == "hybrid"

    @pytest.mark.parametrize("value,expected", [
        ("keyword", "keyword"),
        ("semantic", "semantic"),
        ("HYBRID", "hybrid"),
        ("bogus", "hybrid"),
    ])
    def test_mode_reading(self, monkeypatch, value, expected):
        monkeypatch.setenv("RETRIEVAL_MODE", value)
        from backend.config import read_retrieval_mode

        assert read_retrieval_mode() == expected


class TestOrchestratorIntegration:
    def test_fallback_audited_when_embedder_unavailable(
        self, client, admin_headers, fake_llm, monkeypatch
    ):
        """评分阶段在 embedder 不可用时应审计回退事件并继续 keyword 评审。"""

        from test_engine_pipeline import _upload

        monkeypatch.setenv("RETRIEVAL_MODE", "semantic")
        monkeypatch.setattr(
            "backend.engine.retrieval.get_clause_embedder", lambda: None
        )
        upload = _upload(
            client, admin_headers, "fb.txt", SAMPLE_CONTRACT.encode("utf-8")
        )
        document_id = upload.json()["document_id"]
        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        assert detail["document"]["status"] == "awaiting_review"
        audit = client.get(f"/documents/{document_id}/audit", headers=admin_headers).json()["audit"]
        events = [entry["event"] for entry in audit]
        assert "review.retrieval_fallback" in events
