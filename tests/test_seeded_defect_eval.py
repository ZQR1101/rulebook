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
    OracleRetrieval,
    RetrievalProbe,
    aggregate,
    gold_index,
    load_cases,
    render_markdown,
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


def test_retry_after_a_dropped_call_reaches_the_engine(offline_env, monkeypatch):
    """A connection drop must cost one case, not the rest of the run.

    Observed on a real pass: attempt two raised 「该文档内容此前已入库」 instead
    of re-scoring, because the retry only rebound the engine to the *same* temp
    file — which still held the documents the earlier cases ingested.
    """

    import scripts.evaluate_seeded_defects as harness

    real_run_case = harness.run_case
    attempts: list[str] = []

    def dropped_after_ingesting(case, *, fake=None, model=None, temperature=None, retrieval=None):
        result = real_run_case(
            case, fake=fake, model=model, temperature=temperature, retrieval=retrieval
        )
        attempts.append(case["id"])
        if len(attempts) == 1:
            raise ConnectionError("APIConnectionError: Connection error")
        return result

    monkeypatch.setattr(harness, "run_case", dropped_after_ingesting)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)

    result = harness._run_case_with_retry(
        CASE_BY_ID["CG-SEED-02"],
        fake="all_red",
        attempts=3,
        model=None,
        temperature=harness.EVAL_TEMPERATURE,
    )

    assert len(attempts) == 2
    assert result["run_status"] == "succeeded"


def test_evaluation_decoding_reaches_the_client_not_just_the_flag(monkeypatch):
    """A pinned constant that never reaches the client fixes nothing."""

    import backend.llm_service as llm_service
    from scripts.evaluate_seeded_defects import EVAL_TEMPERATURE, _eval_llm

    captured = {}

    def fake_build_llm(model=None, temperature=None, max_tokens=None):  # noqa: ARG001
        captured["model"] = model
        captured["temperature"] = temperature
        return object()

    monkeypatch.setattr(llm_service, "build_llm", fake_build_llm)

    _eval_llm("deepseek-flash", EVAL_TEMPERATURE)

    assert captured == {"model": "deepseek-flash", "temperature": EVAL_TEMPERATURE}


def test_report_names_the_decoding_temperature(offline_env, monkeypatch):
    """The run conditions belong in the artifact, not only in the invocation."""

    import scripts.evaluate_seeded_defects as harness
    from scripts.evaluate_seeded_defects import AllRedFakeLLM

    # Canned oracle, real-mode bookkeeping: the header must reflect the
    # temperature the run was *told* to use without calling anybody.
    monkeypatch.setattr(harness, "_eval_llm", lambda model, temperature: AllRedFakeLLM())

    result = harness.run_case(
        CASE_BY_ID["CG-SEED-02"], fake=None, model="deepseek-flash", temperature=0.3
    )
    assert "判定温度：0.3" in harness.render_markdown([result], fake=None)

    # A canned oracle does not sample, so it must not claim a decoding setting.
    fake_result = harness.run_case(CASE_BY_ID["CG-SEED-03"], fake="all_red")
    assert "判定温度" not in harness.render_markdown([fake_result], fake="all_red")


def test_noise_section_needs_two_passes_to_exist(offline_env):
    """The spread is a property of repeated runs, so one run must not print one."""

    import copy

    from scripts.evaluate_seeded_defects import _noise_section, run_case

    first = run_case(CASE_BY_ID["CG-SEED-02"], fake="all_red")

    assert _noise_section([first]) == []
    assert _noise_section(None) == []

    second = copy.deepcopy(first)
    for detail in second["metrics"]["details"]:
        if detail["kind"] == "clean":
            detail["actual"] = "green"  # a cleaner second pass, without paying for one

    section = "\n".join(_noise_section([[first], [second]]))
    assert "## 轮间噪声（2 轮独立运行）" in section
    assert "| 误报率 |" in section and "| 均值 | 极差 |" in section


# ------------------------------------------------------------------ gap rubric split


def test_gap_found_but_explained_in_the_wrong_field_is_counted_apart():
    """Both arms lost this point on 单点依赖: red verdict, empty gap_reason, real gap."""

    gold = {"单点依赖": {"kind": "gap", "expected": "red", "note": "全文未约定"}}
    metrics = score_case(gold, [_row("单点依赖", "red")])

    assert metrics["gap_recall"] == 0.0  # the shared rubric still requires gap_reason
    assert metrics["gap_red_without_reason"] == 1
    assert metrics["gap_red_without_reason_rules"] == ["单点依赖"]
    assert metrics["gap_not_red_rules"] == []


def test_a_gap_never_spotted_is_a_different_failure():
    gold = {"单点依赖": {"kind": "gap", "expected": "red", "note": "全文未约定"}}
    metrics = score_case(gold, [_row("单点依赖", "green")])

    assert metrics["gap_red_without_reason"] == 0
    assert metrics["gap_not_red_rules"] == ["单点依赖"]


def test_aggregate_pools_the_gap_reason_loss_across_documents():
    gold = {"单点依赖": {"kind": "gap", "expected": "red", "note": ""}}

    def result(**kwargs):
        return {
            "metrics": score_case(gold, [_row("单点依赖", **kwargs)]),
            "latency_ms": 1,
        }

    overall = aggregate([result(rating="red"), result(rating="red", gap_reason="未约定"), result(rating="amber")])

    assert overall["gap_total"] == 3
    assert overall["gap_recall"] == 0.3333  # one verdict carried a gap_reason
    assert overall["gap_red_without_reason"] == 1  # and one of the misses was field placement


# ---------------------------------------------------------------- oracle retrieval


def _clause(ordinal: int, text: str):
    from backend.engine.parsing import ParsedClause

    return ParsedClause(ordinal=ordinal, heading=text.split("\n", 1)[0], text=text)


def test_oracle_hands_over_exactly_the_gold_clause():
    clauses = [_clause(index, f"第{index}条 常规条款内容") for index in range(1, 6)]
    oracle = OracleRetrieval({"付款账期": [4]})

    picked = oracle.select(clauses, "付款账期 检查付款账期条款。账期不超过 60 天为绿。")

    assert [clause.ordinal for clause in picked] == [4]
    assert oracle.forced == ["付款账期"]
    assert oracle.fell_back == []


def test_oracle_falls_back_for_rules_that_have_no_gold_clause():
    """整段缺失本来就没有金标条款，走真实检索才是诚实的对照。"""

    clauses = [_clause(index, f"第{index}条 常规条款内容") for index in range(1, 6)]
    oracle = OracleRetrieval({"付款账期": [4]})

    picked = oracle.select(clauses, "数据存储地 检查数据存储与处理地域要求。")

    assert oracle.fell_back == ["数据存储地"]
    # The fixture is far under the 6,000-char budget, so the real selector
    # legitimately returns everything; the point is it did not return *only* 4.
    assert [clause.ordinal for clause in picked] != [4]


def test_oracle_respects_the_same_character_budget_as_the_live_selector():
    clauses = [_clause(1, "甲" * 3000), _clause(2, "乙" * 3000), _clause(3, "丙" * 100)]
    oracle = OracleRetrieval({"付款账期": [1, 2, 3]})

    picked = oracle.select(clauses, "付款账期 账期")

    assert sum(len(clause.text) for clause in picked) <= oracle.char_budget
    assert [clause.ordinal for clause in picked] == [1, 2]


def test_oracle_patch_is_reverted_after_the_block():
    from backend.engine import scoring

    original = scoring.select_clauses
    with OracleRetrieval({"付款账期": [1]}):
        assert scoring.select_clauses is not original
    assert scoring.select_clauses is original


def test_oracle_arm_runs_end_to_end_and_reports_which_rules_it_forced(offline_env):
    case = {**CASE_BY_ID["CG-SEED-01"], "retrieval_gold": {"付款账期": [4], "责任上限": [5]}}

    result = run_case(case, fake="all_red", retrieval="oracle")

    assert result["retrieval_arm"] == "oracle"
    assert result["oracle_forced_rules"] == ["付款账期", "责任上限"]
    assert len(result["oracle_fallback_rules"]) == 13  # 15 rules minus the two forced
    assert result["metrics"]["coverage_ok"] is True


def test_oracle_arm_refuses_a_case_without_gold_ordinals(offline_env):
    with pytest.raises(ValueError, match="retrieval_gold"):
        run_case(CASE_BY_ID["CG-SEED-02"], fake="all_red", retrieval="oracle")


def test_report_says_the_oracle_numbers_are_not_live_capability(offline_env):
    case = {**CASE_BY_ID["CG-SEED-03"], "retrieval_gold": {"付款账期": [4]}}
    result = run_case(case, fake="all_red", retrieval="oracle")

    oracle_report = render_markdown([result], fake="all_red", retrieval_arm="oracle")
    plain_report = render_markdown([result], fake="all_red")

    assert "oracle 臂" in oracle_report
    assert "oracle 臂" not in plain_report


def test_limitations_admit_retrieval_only_when_the_corpus_outgrows_the_budget(offline_env):
    short = run_case(CASE_BY_ID["CG-SEED-02"], fake="all_red")

    assert "不混入检索召回因素" in render_markdown([short], fake="all_red")

    long_like = dict(short, clause_chars=9000)
    report = render_markdown([long_like], fake="all_red")
    assert "混入检索召回因素" in report
    assert "不混入检索召回因素" not in report


# ------------------------------------------------------------- retrieval blocking


def test_a_missed_defect_is_attributed_to_retrieval_when_the_clause_was_not_served():
    gold = {"付款账期": {"kind": "defect", "expected": "red", "note": "账期 120 天"}}
    rows = [_row("付款账期", "green")]

    blocked = score_case(gold, rows, retrieval_gold={"付款账期": [7]}, selections={"付款账期": [1, 2, 3]})
    served = score_case(gold, rows, retrieval_gold={"付款账期": [7]}, selections={"付款账期": [7, 2]})
    unmeasured = score_case(gold, rows)

    assert blocked["defect_recall"] == 0.0  # the headline never gets flattered
    assert blocked["retrieval_blocked_total"] == 1
    assert blocked["retrieval_blocked_rules"] == ["付款账期"]
    assert blocked["defect_total_unblocked"] == 0
    assert blocked["defect_recall_unblocked"] is None  # nothing judgeable left

    assert served["retrieval_blocked_total"] == 0
    assert served["defect_total_unblocked"] == 1
    assert served["defect_recall_unblocked"] == 0.0

    assert unmeasured["retrieval_blocking_measured"] is False
    assert unmeasured["retrieval_blocked_total"] == 0
    assert unmeasured["defect_total_unblocked"] == 1  # not measured = not excluded


def test_probe_records_selections_without_changing_them():
    from backend.engine.retrieval import select_clauses

    clauses = [
        _clause(1, "第1条 双方一般约定"),
        _clause(2, "第2条 账期 120 天，逾期部分按日 1% 计"),
    ]
    query = "付款账期 检查付款账期条款。账期不超过 60 天为绿。"
    probe = RetrievalProbe()

    picked = probe.select(clauses, query, mode="keyword")
    reference = select_clauses(clauses, query, mode="keyword")

    assert [clause.ordinal for clause in picked] == [clause.ordinal for clause in reference]
    assert probe.selections["付款账期"] == [clause.ordinal for clause in reference]


def test_blocking_rows_appear_only_for_a_corpus_that_can_measure_them(offline_env):
    measured = run_case(
        {**CASE_BY_ID["CG-SEED-03"], "retrieval_gold": {"付款账期": [4]}}, fake="all_red"
    )
    baseline = run_case(CASE_BY_ID["CG-SEED-02"], fake="all_red")

    assert "检索阻断率" in render_markdown([measured], fake="all_red")
    assert "检索阻断率" not in render_markdown([baseline], fake="all_red")
    assert measured["retrieval_selections"]["付款账期"]
