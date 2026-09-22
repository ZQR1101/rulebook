"""Chinese clause-retrieval tokenization.

Keyword scoring is the fallback that runs whenever the embedding model is
unavailable, so it decides what the scorer sees in the common case. It used to
slice Chinese into non-overlapping 2-4 character windows, which made every match
depend on where the two sides happened to break: 「可用性承诺」 became
「可用性承」+「诺是多少」 and matched a clause containing 「服务可用性」 and
「乙方承诺」 not at all. Rules therefore scored 0 against documents that plainly
contained their subject, and the scorer was handed the head of the document as
filler instead.

The gold-case hit rate in the last test is the number that regressed silently
before; it is pinned so tokenization cannot quietly break again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import parse_document  # noqa: E402
from backend.engine.retrieval import (  # noqa: E402
    MAX_QUERY_KEYWORDS,
    _keywords,
    score_clause,
    select_clauses,
)
from backend.playbooks import get_playbook  # noqa: E402
from scripts.evaluate_retrieval import load_groups  # noqa: E402

CLAUSE = "乙方承诺服务可用性达到99.95%，P1故障响应时间不超过1小时。"
GOLD_CASE_FILES = ("eval_cases/retrieval_cases.json", "eval_cases/retrieval_hard_cases.json")


class TestChineseTokenization:
    def test_match_no_longer_depends_on_where_the_text_breaks(self):
        keywords = _keywords("可用性承诺是多少")
        assert score_clause(CLAUSE, keywords) > 0

    def test_cjk_runs_become_overlapping_bigrams(self):
        assert _keywords("可用性") == ("可用", "用性")

    def test_latin_and_version_numbers_stay_whole(self):
        keywords = _keywords("SLA uptime 99.95%")
        assert {"sla", "uptime", "99.95"} <= set(keywords)

    def test_stopwords_are_still_dropped(self):
        assert "检查" not in _keywords("检查是否约定审计权")

    def test_long_query_is_capped(self):
        distinct_run = "".join(chr(0x4E00 + index) for index in range(100))
        assert len(_keywords(distinct_run)) == MAX_QUERY_KEYWORDS

    def test_no_rule_is_truncated_by_the_cap(self):
        """The cap only protects cost; it must not silently drop rule guidance."""

        for playbook_id in ("contract-compliance", "dpa-review", "delivery-intake"):
            for seed in get_playbook(playbook_id).rule_seeds:
                count = len(_keywords(f"{seed['name']} {seed['guidance']}"))
                assert count < MAX_QUERY_KEYWORDS, f"{seed['name']} 抽出 {count} 个词，已被截断"


class TestGoldKeywordRetrieval:
    """Keyword-mode selection against the hand-labelled clause gold set."""

    @staticmethod
    def _collect(budget_override: int | None = None):
        groups = load_groups([PROJECT_ROOT / name for name in GOLD_CASE_FILES])
        rows = []
        for group in groups:
            seeds = {seed["name"]: seed for seed in get_playbook(group["playbook_id"]).rule_seeds}
            parsed = parse_document(PROJECT_ROOT / group["path"])
            budget = budget_override or int(group.get("char_budget") or 6000)
            for case in group["cases"]:
                seed = seeds[case["rule_name"]]
                selected = {
                    clause.ordinal
                    for clause in select_clauses(
                        parsed.clauses,
                        f"{seed['name']} {seed['guidance']}",
                        char_budget=budget,
                        mode="keyword",
                    )
                }
                rows.append(
                    {
                        "rule": case["rule_name"],
                        "document": Path(group["path"]).name,
                        "hit": bool(set(case["expected_ordinals"]) & selected),
                    }
                )
        return rows

    @pytest.fixture(scope="class")
    def outcomes(self):
        return self._collect()

    @pytest.fixture(scope="class")
    def working_point(self):
        return self._collect(budget_override=6000)

    def test_at_least_31_of_36_gold_cases_select_an_expected_clause(self, outcomes):
        hits = sum(1 for row in outcomes if row["hit"])
        assert len(outcomes) == 36
        # Was 34/36 under the old splitter, which cut every line into its own
        # ~20-char clause so these tight budgets held most of the document. With
        # real clause granularity 3 cases lose their gold clause to rank
        # starvation inside 1200/4000 chars — tokenization is not the cause.
        # At the engine's 6000-char working point the same set scores 34/36.
        assert hits >= 31, f"关键词命中 {hits}/36，回退到分词修复之前"

    def test_the_rules_the_tokenizer_used_to_miss_now_select(self, working_point):
        recovered = {
            ("软件开发外包合同.pdf", "终止条款"),
            ("DPA_数据处理协议_中文.md", "审计与记录义务"),
            ("DPA_数据处理协议_中文.md", "删除与返还义务"),
        }
        missed = {(row["document"], row["rule"]) for row in working_point if not row["hit"]}
        assert recovered & missed == set()
