"""Blind-arm scoring guarantees: same denominators, or say so loudly.

The blind arm is only evidence while it is measured with the engine's own
ruler on the *same* documents. These tests guard the two ways that quietly
breaks: an agent dropping rules (which would shrink the denominator and flatter
it) and a subset pack being compared against a whole-corpus engine aggregate.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.score_blind_arm import _misses, check_coverage, reslice_baseline  # noqa: E402


class _StubHarness:
    """Aggregate that just counts documents, so the slicing is observable."""

    def aggregate(self, results):
        return {"documents": len(results), "rules_scored": sum(len(r["metrics"]) for r in results)}


def _engine_result(case_id: str, rules: int) -> dict:
    return {"case_id": case_id, "metrics": [{"rule": f"r{i}"} for i in range(rules)]}


def _row(rule_name: str) -> dict:
    return {"rule_name": rule_name, "dimension": "", "rating": "green", "citations": []}


def test_a_dropped_rule_is_a_structural_problem_not_a_smaller_denominator():
    gold = {"付款账期": {}, "责任上限": {}, "审计权": {}}

    problems = check_coverage(gold, [_row("付款账期")], "DOC-A")

    assert len(problems) == 1 and "缺少规则判定" in problems[0] and "责任上限" in problems[0]


def test_a_subset_pack_repools_the_engine_baseline_over_the_same_documents():
    baseline = {
        "results": [_engine_result("CG-V2-02", 15), _engine_result("CG-V2-03", 15)],
        "overall": {"documents": 2},
        "passes": [{"documents": 2}],
    }

    sliced, mismatch = reslice_baseline(baseline, _StubHarness(), {"CG-V2-02"})

    assert mismatch is None
    assert [row["case_id"] for row in sliced["results"]] == ["CG-V2-02"]
    assert sliced["overall"]["documents"] == 1
    assert [entry["documents"] for entry in sliced["passes"]] == [1]


def test_a_multi_pass_baseline_refuses_to_be_sliced():
    baseline = {
        "results": [_engine_result("CG-V2-02", 15), _engine_result("CG-V2-03", 15)],
        "overall": {"documents": 2},
        "passes": [{"documents": 2}, {"documents": 2}, {"documents": 2}],
    }

    untouched, mismatch = reslice_baseline(baseline, _StubHarness(), {"CG-V2-02"})

    assert untouched is baseline
    assert mismatch and "无法重新池化" in mismatch


def test_a_full_corpus_pack_leaves_the_baseline_alone():
    baseline = {
        "results": [_engine_result("CG-V2-02", 15)],
        "overall": {"documents": 1},
        "passes": [{"documents": 1}],
    }

    untouched, mismatch = reslice_baseline(baseline, _StubHarness(), {"CG-V2-02"})

    assert untouched is baseline and mismatch is None


def _mismatch_of(kind: str, expected: str, actual: str, gap_recalled: bool) -> str:
    detail = {"kind": kind, "expected": expected, "actual": actual, "rule": "单点依赖",
              "flagged": actual != "green", "exact": actual == expected, "gap_recalled": gap_recalled}
    lines = _misses([{"friendly_id": "DOC-C", "case_id": "CG-V2-02", "metrics": {"details": [detail]}}])
    assert len(lines) == 1
    return lines[0]


def test_an_under_called_gap_is_a_severity_miss_not_a_silent_one():
    """「判红但未说明缺失」 claims the reviewer called red and said nothing — amber must not borrow it."""

    amber = _mismatch_of("gap", "red", "amber", gap_recalled=False)
    assert "档位偏移" in amber and "未说明缺失" not in amber

    red_and_silent = _mismatch_of("gap", "red", "red", gap_recalled=False)
    assert "判红但未说明缺失" in red_and_silent
