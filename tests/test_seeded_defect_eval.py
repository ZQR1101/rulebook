"""Seeded-defect harness tests: prove the measuring stick before trusting the number.

Nothing here calls a real model. The fake passes assert the two degenerate
oracles behave exactly as designed — an always-red model flags everything, an
always-green hallucinating model gets caught by the citation gate — so a real
run's numbers can be read as engine ability rather than harness artefacts.
"""

from __future__ import annotations

import pytest

from test_engine_pipeline import platform_env  # noqa: F401  (fixture shared via import)

from scripts.evaluate_seeded_defects import (
    aggregate,
    gold_index,
    load_cases,
    run_case,
    score_case,
    validate_gold,
)


CASES = load_cases()
CASE_BY_ID = {case["id"]: case for case in CASES}


def _case(playbook_id: str, defects=(), gaps=(), clean=()) -> dict:
    return {
        "id": "synthetic",
        "playbook_id": playbook_id,
        "defects": list(defects),
        "gaps": list(gaps),
        "clean": list(clean),
    }


def _row(name: str, rating: str, *, dimension: str = "商业", gap_reason: str = "") -> dict:
    return {
        "rule_name": name,
        "dimension": dimension,
        "rating": rating,
        "rationale": "",
        "gap_reason": gap_reason,
        "review_state": "awaiting_review",
        "citations": [],
    }


# --------------------------------------------------------------------- gold set


@pytest.mark.parametrize("case_id", [case["id"] for case in CASES])
def test_gold_set_labels_every_active_rule(case_id):
    """No rule may go unlabeled — that is the blind spot this corpus removes."""

    from backend.playbooks import get_playbook

    case = CASE_BY_ID[case_id]
    playbook = get_playbook(case["playbook_id"])
    rule_names = {seed["name"] for seed in playbook.rule_seeds}

    assert validate_gold(case, rule_names) == []


def test_unlabeled_rule_is_reported_as_a_gold_defect():
    case = _case("contract-compliance", clean=["付款账期"])
    rules = {"付款账期", "责任上限"}

    problems = validate_gold(case, rules)

    assert problems == ["未标注的启用规则：责任上限"]


def test_unknown_rule_annotation_is_rejected():
    case = _case("contract-compliance", clean=["付款账期", "隔壁剧本的规则"])

    problems = validate_gold(case, {"付款账期"})

    assert problems == ["标注了剧本中不存在的规则：隔壁剧本的规则"]


def test_duplicate_annotation_raises():
    case = _case("contract-compliance", clean=["付款账期"], defects=[{"rule": "付款账期", "expected_rating": "red"}])

    with pytest.raises(ValueError, match="重复标注"):
        gold_index(case)


def test_seeded_documents_exist_and_are_named_after_their_case():
    from pathlib import Path

    from scripts.evaluate_seeded_defects import PROJECT_ROOT

    for case in CASES:
        path = PROJECT_ROOT / case["document"]
        assert path.is_file(), path
        assert path.stem.startswith(case["id"]), path


# ------------------------------------------------------------------ metric math


def test_recall_and_false_positive_are_computed_per_bucket():
    case = _case(
        "contract-compliance",
        defects=[
            {"rule": "付款账期", "expected_rating": "red"},
            {"rule": "责任上限", "expected_rating": "amber"},
        ],
        gaps=[{"rule": "审计权", "missing": "未约定审计"}],
        clean=["定价清晰度", "终止条款"],
    )
    rows = [
        _row("付款账期", "red", gap_reason="未约定"),
        _row("责任上限", "amber"),
        _row("审计权", "red", gap_reason="文档未约定审计权"),
        _row("定价清晰度", "green"),
        _row("终止条款", "amber"),
    ]

    metrics = score_case(gold_index(case), rows)

    assert metrics["defect_recall"] == 1.0  # red and amber both count as caught
    assert metrics["defect_exact"] == 1.0
    assert metrics["gap_recall"] == 1.0
    assert metrics["clean_pass_rate"] == 0.5
    assert metrics["false_positive_rate"] == 0.5
    assert metrics["false_positive_rules"] == ["终止条款"]
    assert metrics["exact_agreement"] == 0.8
    assert metrics["coverage_ok"] is True


def test_a_flagged_defect_at_the_wrong_level_still_counts_as_recalled():
    case = _case("contract-compliance", defects=[{"rule": "付款账期", "expected_rating": "red"}])
    metrics = score_case(gold_index(case), [_row("付款账期", "amber")])

    assert metrics["defect_recall"] == 1.0
    assert metrics["defect_exact"] == 0.0
    assert metrics["missed_defect_rules"] == []


def test_a_missed_defect_shows_up_in_the_miss_list():
    case = _case(
        "contract-compliance",
        defects=[{"rule": "付款账期", "expected_rating": "red"}],
        clean=["定价清晰度"],
    )
    rows = [_row("付款账期", "green"), _row("定价清晰度", "green")]

    metrics = score_case(gold_index(case), rows)

    assert metrics["defect_recall"] == 0.0
    assert metrics["false_positive_rate"] == 0.0
    assert metrics["missed_defect_rules"] == ["付款账期"]


def test_gap_needs_red_and_a_reason_not_just_red():
    """A red verdict with no gap_reason is a violation, not a missing clause."""

    case = _case("contract-compliance", gaps=[{"rule": "审计权", "missing": "未约定审计"}])

    silent = score_case(gold_index(case), [_row("审计权", "red")])
    explained = score_case(gold_index(case), [_row("审计权", "red", gap_reason="全文无审计条款")])

    assert silent["gap_recall"] == 0.0
    assert silent["gap_flagged"] == 1.0
    assert explained["gap_recall"] == 1.0


def test_unlabeled_verdicts_do_not_dilute_the_denominators():
    case = _case("contract-compliance", clean=["定价清晰度"])
    rows = [_row("定价清晰度", "green"), _row("隔壁剧本的规则", "red")]

    metrics = score_case(gold_index(case), rows)

    assert metrics["rules_scored"] == 1
    assert metrics["rules_annotated"] == 1


def test_citation_genuineness_uses_pre_gate_emissions():
    case = _case("contract-compliance", clean=["定价清晰度"])
    emissions = {
        "定价清晰度": {
            "emitted_rating": "green",
            "emitted_citations": [{"quote": "真实引用"}, {"quote": "编造引用"}],
            "valid_citations": [{"quote": "真实引用"}],
            "invalid_citations": [{"quote": "编造引用"}],
        }
    }

    metrics = score_case(gold_index(case), [_row("定价清晰度", "green")], emissions)

    assert metrics["citations_emitted"] == 2
    assert metrics["citations_invalid"] == 1
    assert metrics["citation_genuineness"] == 0.5


def test_gate_catch_rate_tracks_unsupported_green_downgrades():
    case = _case(
        "contract-compliance",
        clean=["定价清晰度", "终止条款"],
    )
    caught = {
        "emitted_rating": "green",
        "emitted_citations": [{"quote": "编造"}],
        "valid_citations": [],
        "invalid_citations": [{"quote": "编造"}],
    }
    emissions = {"定价清晰度": caught, "终止条款": dict(caught)}

    downgraded = score_case(
        gold_index(case),
        [_row("定价清晰度", "amber"), _row("终止条款", "amber")],
        emissions,
    )
    escaped = score_case(
        gold_index(case),
        [_row("定价清晰度", "green"), _row("终止条款", "green")],
        emissions,
    )

    assert downgraded["unsupported_green_total"] == 2
    assert downgraded["gate_catch_rate"] == 1.0
    assert escaped["unsupported_green_caught"] == 0
    assert escaped["gate_catch_rate"] == 0.0


def test_supported_green_is_not_counted_against_the_gate():
    case = _case("contract-compliance", clean=["定价清晰度"])
    emissions = {
        "定价清晰度": {
            "emitted_rating": "green",
            "emitted_citations": [{"quote": "真实"}],
            "valid_citations": [{"quote": "真实"}],
            "invalid_citations": [],
        }
    }

    metrics = score_case(gold_index(case), [_row("定价清晰度", "green")], emissions)

    assert metrics["unsupported_green_total"] == 0
    assert metrics["gate_catch_rate"] is None


def test_by_dimension_splits_the_buckets():
    case = _case(
        "contract-compliance",
        defects=[{"rule": "付款账期", "expected_rating": "red"}],
        clean=["定价清晰度"],
    )
    rows = [_row("付款账期", "red", dimension="商业"), _row("定价清晰度", "green", dimension="数据隐私")]

    by_dimension = score_case(gold_index(case), rows)["by_dimension"]

    assert by_dimension["商业"] == {
        "defect": 1, "defect_hit": 1, "gap": 0, "gap_hit": 0, "clean": 0, "clean_hit": 0,
    }
    assert by_dimension["数据隐私"]["clean"] == 1
    assert by_dimension["数据隐私"]["clean_hit"] == 1


def test_aggregate_pools_rule_counts_not_document_averages():
    """One 10-rule document must not weigh the same as a 2-rule document."""

    big = _case("contract-compliance", clean=_playbook_rules("contract-compliance")[:10])
    small = _case("contract-compliance", clean=["付款账期", "责任上限"])
    results = [
        {
            "latency_ms": 10,
            "metrics": score_case(
                gold_index(big),
                [_row(name, "green") for name in big["clean"]],
            ),
        },
        {
            "latency_ms": 5,
            "metrics": score_case(
                gold_index(small),
                [_row("付款账期", "red"), _row("责任上限", "green")],
            ),
        },
    ]

    overall = aggregate(results)

    assert overall["rules_scored"] == 12
    assert overall["clean_total"] == 12
    assert overall["false_positive_rate"] == round(1 / 12, 4)
    assert overall["total_latency_ms"] == 15
    assert overall["citation_genuineness"] is None  # nothing emitted offline
    assert overall["models"] == []  # results without a usage block stay empty
    assert overall["llm_calls"] == 0


def test_explicit_model_wins_and_typos_are_rejected(monkeypatch):
    """A mistyped --model must not silently score the corpus on the premium tier."""

    from scripts.evaluate_seeded_defects import _resolve_model

    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")

    assert _resolve_model(None) == "deepseek-flash"
    assert _resolve_model("qwen3.7-max") == "qwen3.7-max"
    with pytest.raises(ValueError, match="未知模型"):
        _resolve_model("deepseek-cheapo")


def test_run_case_reports_what_the_run_cost(offline_env):
    result = run_case(CASE_BY_ID["CG-SEED-03"], fake="all_red", model="deepseek-flash")
    usage = result["usage"]

    assert usage["model"] == "deepseek-flash"
    assert usage["calls"] == 15
    assert usage["total_tokens"] > 0
    assert usage["estimated_cost_usd"] > 0

    overall = aggregate([result])
    assert overall["models"] == ["deepseek-flash"]
    assert overall["llm_calls"] == 15
    assert overall["total_tokens"] == usage["total_tokens"]
    assert overall["estimated_cost_usd"] == pytest.approx(usage["estimated_cost_usd"], abs=1e-6)


def test_report_names_the_model_and_spend(offline_env):
    from scripts.evaluate_seeded_defects import render_markdown

    result = run_case(CASE_BY_ID["CG-SEED-03"], fake="all_red", model="deepseek-flash")
    report = render_markdown([result], fake="all_red")

    assert "评分模型：**deepseek-flash**" in report
    assert "15 次调用" in report
    assert "$" in report


def _playbook_rules(playbook_id: str):
    from backend.playbooks import get_playbook

    return [seed["name"] for seed in get_playbook(playbook_id).rule_seeds]


# ------------------------------------------------------------- offline engines


@pytest.fixture()
def offline_env(platform_env, monkeypatch):  # noqa: ARG001 - platform_env is the DB
    monkeypatch.setenv("RETRIEVAL_MODE", "keyword")
    monkeypatch.setenv("EMAIL_ENABLED", "false")


def test_all_red_oracle_hits_every_ceiling(offline_env):
    result = run_case(CASE_BY_ID["CG-SEED-02"], fake="all_red")
    metrics = result["metrics"]

    assert metrics["coverage_ok"] is True
    assert metrics["defect_recall"] == 1.0
    assert metrics["gap_recall"] == 1.0
    assert metrics["false_positive_rate"] == 1.0
    assert metrics["clean_pass_rate"] == 0.0
    assert result["run_status"] == "succeeded"
    assert result["scorecard"]["total_rules"] == 15


def test_hallucinating_green_oracle_is_caught_by_the_gate(offline_env):
    """An invented quote must never reach the report as a clean green."""

    result = run_case(CASE_BY_ID["CG-SEED-02"], fake="green_hallucinated")
    metrics = result["metrics"]

    assert metrics["citation_genuineness"] == 0.0
    assert metrics["gate_catch_rate"] == 1.0
    assert metrics["clean_pass_rate"] == 0.0
    assert metrics["gap_recall"] == 0.0  # no red, so no gap was named
    assert {detail["actual"] for detail in metrics["details"]} == {"amber"}
    assert all(detail["citations_stored"] == 0 for detail in metrics["details"])


def test_every_seeded_case_is_engine_compatible(offline_env):
    """Covers clause parsing for all four corpora: one verdict per gold rule."""

    for case in CASES:
        result = run_case(case, fake="all_red")
        metrics = result["metrics"]

        assert metrics["coverage_ok"] is True, case["id"]
        assert metrics["rules_scored"] == metrics["rules_annotated"], case["id"]
        assert result["scoring_failures"] in (0, None), case["id"]
