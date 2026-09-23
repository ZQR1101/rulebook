"""Clause identity and attribution: a verdict must be able to name its clause.

Two failures measured on the v2 corpus are fixed here. The splitter hands a
numbered heading and a later chunk of the same article to the model as separate
clauses, so the body arrives with no title at all — and the citation gate proves
only that a quote exists somewhere, so a green supported by the *deduction*
clause's 99.95% figure passed while answering a different rule.

Attribution is checked only against what this call actually received, and only
when the model made a claim: an absent claim is uninformative, not false, so
the existing citation gate stays the sole authority over it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.attribution import parse_ordinal, split_unserved  # noqa: E402
from backend.engine.citation_gate import validate_citations  # noqa: E402
from backend.engine.parsing import clause_identities, split_clauses  # noqa: E402
from backend.engine.scoring import _build_prompt, score_rule  # noqa: E402

# 「月度可用性低于」 re-triggers the SLA block rule inside the splitter, which is
# what detaches a body paragraph from its own heading in a real contract.
CONTRACT = """第二部分 运行保障与服务安排

2.2 服务连续性指标
乙方将尽力保证视频监控系统的正常运转。

2.4 服务未达标的抵扣
月度可用性低于 99.95% 的，乙方按当月服务费的 10% 向甲方出具服务抵扣金。
"""

CLAUSES = split_clauses(CONTRACT)
BODY_ORDINAL = next(c.ordinal for c in CLAUSES if "99.95" in c.text)
PROMISE_ORDINAL = next(c.ordinal for c in CLAUSES if "尽力保证" in c.text)
DEDUCTION_CLAUSE = next(c for c in CLAUSES if c.ordinal == BODY_ORDINAL)
PROMISE_CLAUSE = next(c for c in CLAUSES if c.ordinal == PROMISE_ORDINAL)

RULE = SimpleNamespace(
    id="rule-availability",
    name="可用性承诺",
    dimension="服务",
    weight=3,
    guidance="检查服务可用性 SLA。不低于 99.9% 为绿。",
)


class FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _score(payload: dict):
    fake = FakeLLM(payload)
    row = score_rule(
        RULE,
        CLAUSES,
        CONTRACT,
        playbook_instructions="按规则判定。",
        custom_llm=fake,
        retrieval_mode="keyword",
    )
    return row, fake


class TestClauseIdentity:
    def test_a_body_chunk_recovers_the_heading_it_was_split_from(self):
        identities = clause_identities(CLAUSES)

        title, section = identities[BODY_ORDINAL]
        assert title == "2.4 服务未达标的抵扣"
        assert section == "第二部分 运行保障与服务安排"

    def test_a_clause_with_its_own_heading_keeps_it(self):
        title, _ = clause_identities(CLAUSES)[PROMISE_ORDINAL]

        assert title == "2.2 服务连续性指标"

    def test_identity_is_annotation_only_so_ordinals_stay_put(self):
        before = [clause.ordinal for clause in CLAUSES]

        clause_identities(CLAUSES)

        assert [clause.ordinal for clause in CLAUSES] == before
        assert len(clause_identities(CLAUSES)) == len(CLAUSES)


class TestPromptShowsWhatEachClauseIs:
    def test_the_deduction_clause_is_labelled_as_one(self):
        prompt = _build_prompt(
            RULE, [DEDUCTION_CLAUSE], "按规则判定。", identities=clause_identities(CLAUSES)
        )

        assert f"[条款 {BODY_ORDINAL}] 2.4 服务未达标的抵扣（所属：第二部分 运行保障与服务安排）" in prompt

    def test_a_heading_the_clause_already_prints_is_not_duplicated(self):
        prompt = _build_prompt(RULE, [PROMISE_CLAUSE], "按规则判定。")

        assert prompt.count("2.2 服务连续性指标") == 1


class TestAttributionParsing:
    def test_ordinal_claims_survive_model_shape_variants(self):
        assert [parse_ordinal(v) for v in (17, "17", "条款 17", "第17条")] == [17, 17, 17, 17]

    def test_a_dotted_section_number_is_not_a_clause_ordinal(self):
        assert parse_ordinal(" 2.4 ") is None

    def test_a_claim_that_is_not_a_clause_number_is_no_claim(self):
        assert parse_ordinal(None) is None
        assert parse_ordinal(True) is None
        assert parse_ordinal(0) is None
        assert parse_ordinal("抵扣条款") is None

    def test_only_a_claim_about_an_unseen_clause_is_disprovable(self):
        usable, unserved = split_unserved(
            [{"quote": "甲", "clause": 4}, {"quote": "乙", "clause": 77}, "丙"],
            {1, 2, 3, 4},
        )

        assert [len(usable), len(unserved)] == [2, 1]
        assert unserved[0]["clause"] == 77


class TestOperativeClauseGate:
    def test_a_green_citing_a_clause_it_did_not_name_is_downgraded(self):
        row, _ = _score(
            {
                "rating": "green",
                "rationale": "已有可用性指标。",
                "citations": [{"quote": PROMISE_CLAUSE.text[:20], "clause": PROMISE_ORDINAL}],
                "operative_clause": BODY_ORDINAL,
            }
        )

        assert row["rating"] == "amber"
        assert row["failure"] == "operative_quote_mismatch"
        assert row["review_state"] == "awaiting_review"
        assert row["operative_clause"] == BODY_ORDINAL

    def test_a_green_naming_a_clause_that_was_never_sent_is_downgraded(self):
        row, _ = _score(
            {
                "rating": "green",
                "rationale": "已承诺。",
                "citations": [{"quote": DEDUCTION_CLAUSE.text[:20], "clause": BODY_ORDINAL}],
                "operative_clause": 999,
            }
        )

        assert row["rating"] == "amber"
        assert row["failure"] == "operative_clause_unserved"

    def test_a_green_whose_quote_sits_inside_its_operative_clause_stands(self):
        row, _ = _score(
            {
                "rating": "green",
                "rationale": "写明 99.95% 未达标即抵扣，视为已有指标。",
                "citations": [{"quote": DEDUCTION_CLAUSE.text[:20], "clause": BODY_ORDINAL}],
                "operative_clause": BODY_ORDINAL,
            }
        )

        assert row["rating"] == "green"
        assert row["failure"] is None
        assert row["citations"][0]["clause"] == BODY_ORDINAL

    def test_a_citation_from_a_clause_never_sent_is_not_a_citation(self):
        row, _ = _score(
            {
                "rating": "green",
                "rationale": "附件另有承诺。",
                "citations": [{"quote": "乙方保证全年可用性不低于 99.99%", "clause": 999}],
            }
        )

        assert row["citations"] == []
        assert len(row["invalid_citations"]) == 1
        assert row["failure"] == "missing_citation"

    def test_no_claim_leaves_the_citation_gate_as_the_only_authority(self):
        row, _ = _score(
            {
                "rating": "green",
                "rationale": "已写明指标。",
                "citations": [{"quote": DEDUCTION_CLAUSE.text[:20]}],
            }
        )

        assert row["rating"] == "green"
        assert row["failure"] is None
        assert row["operative_clause"] is None


class TestAnswerKeyUnchanged:
    def test_a_clause_claim_cannot_change_what_the_gate_accepts(self):
        quote = DEDUCTION_CLAUSE.text[:20]

        plain, _ = validate_citations([{"quote": quote}], CONTRACT, [DEDUCTION_CLAUSE.text])
        claiming, dropped = validate_citations(
            [{"quote": quote, "clause": 77}], CONTRACT, [DEDUCTION_CLAUSE.text]
        )

        assert plain == claiming
        assert dropped == []
