"""Offline checks for the CUAD external-gold adapter.

Everything here runs against a synthetic mini-CUAD built in tmp_path: the real
archive is 18 MB and the scoring path costs money, so the CI-runnable contract is
"the adapter reads, maps, scores and renders correctly", not "the numbers are
good". The numbers live in reports/EXTERNAL_GOLD_REPORT.md.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.evaluate_external_gold import (
    BAND_OVER,
    BAND_UNDER,
    RETRIEVAL_BUDGET_CHARS,
    _format_modes,
    aggregate,
    gold_for_contract,
    length_band,
    load_contracts,
    load_mapping,
    render_markdown,
    score_contract,
    score_rule,
    select_contracts,
)

CAP_SPAN = (
    "Neither party's aggregate liability arising out of or relating to this "
    "Agreement shall exceed the total fees paid in the twelve months preceding the claim"
)
AUDIT_SPAN = (
    "Company shall keep accurate and complete books and records relating to the "
    "products licensed hereunder and Manufacturer may audit them once per calendar year"
)
OTHER_SPAN = "The Supplier shall deliver the Products in accordance with each Purchase Order"

MAPPING = {
    "playbook_id": "contract-compliance",
    "rules": {
        "责任上限": {"cuad_categories": ["Cap On Liability", "Uncapped Liability"]},
        "审计权": {"cuad_categories": ["Audit Rights"]},
    },
}


def _contract(**labels: list[str]) -> dict:
    return {
        "title": "Acme Manufacturing Agreement",
        "text": "x" * (RETRIEVAL_BUDGET_CHARS + 1),
        "labels": {**{"Cap On Liability": [], "Uncapped Liability": [], "Audit Rights": []}, **labels},
    }


def _verdict(rating="red", citations=(), gap_reason="") -> dict:
    return {
        "rule_name": "责任上限",
        "dimension": "责任",
        "rating": rating,
        "rationale": "rationale",
        "gap_reason": gap_reason,
        "review_state": "awaiting_review",
        "citations": [{"quote": quote} for quote in citations],
    }


class TestGoldLoading:
    def test_archive_is_read_into_category_spans(self, tmp_path: Path):
        payload = {
            "version": "1",
            "data": [
                {
                    "title": "Acme Manufacturing Agreement",
                    "paragraphs": [
                        {
                            "context": "the full text of the agreement",
                            "qas": [
                                {
                                    "id": "0__Cap On Liability",
                                    "question": "Is there a cap on liability?",
                                    "answers": [{"text": CAP_SPAN, "answer_start": 12}],
                                    "is_impossible": False,
                                },
                                {
                                    "id": "1__Audit Rights",
                                    "question": "Is there an audit right?",
                                    "answers": [],
                                    "is_impossible": True,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        archive = tmp_path / "cuad-data.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("CUADv1.json", json.dumps(payload))

        contracts = load_contracts(archive)

        assert contracts[0]["title"] == "Acme Manufacturing Agreement"
        assert contracts[0]["text"] == "the full text of the agreement"
        assert contracts[0]["labels"]["Cap On Liability"] == [CAP_SPAN]
        assert contracts[0]["labels"]["Audit Rights"] == []

    def test_missing_archive_names_the_fetch_command(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="cuad-data.zip"):
            load_contracts(tmp_path / "nope.zip")

    def test_mapping_must_only_reference_playbook_rules(self, tmp_path: Path):
        bad = json.loads(json.dumps(MAPPING))
        bad["rules"]["不存在的规则"] = {"cuad_categories": ["Audit Rights"]}
        path = tmp_path / "map.json"
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ValueError, match="不存在的规则"):
            load_mapping(path)

    def test_shipped_mapping_matches_the_playbook(self):
        mapping = load_mapping()
        assert mapping["playbook_id"] == "contract-compliance"
        assert len(mapping["rules"]) == 7

    def test_unknown_category_is_reported_not_silently_dropped(self):
        contract = _contract()
        contract["labels"].pop("Uncapped Liability")
        with pytest.raises(ValueError, match="Uncapped Liability"):
            gold_for_contract(contract, MAPPING)

    def test_two_categories_fold_into_one_rule(self):
        contract = _contract(**{"Cap On Liability": [CAP_SPAN], "Uncapped Liability": [OTHER_SPAN]})
        gold = gold_for_contract(contract, MAPPING)
        assert gold["责任上限"]["spans"] == [CAP_SPAN, OTHER_SPAN]
        assert gold["审计权"]["spans"] == []


class TestSelection:
    def _pool(self, sizes: list[int]) -> list[dict]:
        return [
            {
                "title": f"Contract {index}",
                "text": "y" * size,
                "labels": {
                    "Cap On Liability": [CAP_SPAN] if index % 2 == 0 else [],
                    "Uncapped Liability": [],
                    "Audit Rights": [AUDIT_SPAN],
                },
            }
            for index, size in enumerate(sizes)
        ]

    def test_sample_is_split_across_both_length_bands(self):
        pool = self._pool([9000] * 20 + [90000] * 20)
        picked = select_contracts(pool, MAPPING, count=6, min_chars=8000, max_chars=100000)
        bands = [length_band(len(contract["text"])) for contract in picked]
        assert bands.count(BAND_UNDER) == 3
        assert bands.count(BAND_OVER) == 3

    def test_richest_contracts_win_within_a_band(self):
        pool = self._pool([9000, 9000])
        picked = select_contracts(pool, MAPPING, count=1, min_chars=8000, max_chars=100000)
        assert picked[0]["title"] == "Contract 0"  # both rules present beats one

    def test_a_one_sided_band_backfills_from_the_other(self):
        pool = self._pool([90000] * 10)
        picked = select_contracts(pool, MAPPING, count=4, min_chars=8000, max_chars=100000)
        assert len(picked) == 4
        assert {length_band(len(contract["text"])) for contract in picked} == {BAND_OVER}

    def test_length_filter_excludes_contracts(self):
        pool = self._pool([100, 9000])
        picked = select_contracts(pool, MAPPING, count=8, min_chars=8000, max_chars=100000)
        assert [contract["title"] for contract in picked] == ["Contract 1"]

    def test_internal_coverage_key_does_not_leak_into_the_sample(self):
        pool = self._pool([9000])
        picked = select_contracts(pool, MAPPING, count=1, min_chars=8000, max_chars=100000)
        assert set(picked[0]) == {"title", "text", "labels"}


def _details(*, present: bool, hit: int, total: int, agree: bool, over_claim: bool) -> dict:
    return {
        "rule": "责任上限",
        "categories": ["Cap On Liability"],
        "expert_present": present,
        "expert_spans": total,
        "cited_spans_hit": hit,
        "localization_recall": round(hit / total, 4) if total else None,
        "engine_rating": "red",
        "engine_claims_absence": not present,
        "citations_stored": hit,
        "agreement": agree,
        "over_claim": over_claim,
    }


def _result(band: str, *, spans_total: int, spans_hit: int, calls: int, cost: float) -> dict:
    present = _details(present=True, hit=spans_hit, total=spans_total, agree=True, over_claim=False)
    absent = _details(present=False, hit=0, total=0, agree=False, over_claim=True)
    return {
        "title": "Contract",
        "chars": 9000 if band == BAND_UNDER else 90000,
        "length_band": band,
        "retrieval_limited": True,
        "rules": [present, absent],
        "unjudged_rules": [],
        "present_rules": 1,
        "agreed_present": 1,
        "absent_rules": 1,
        "agreed_absent": 0,
        "over_claims": 1,
        "citations_stored": spans_hit,
        "spans_total": spans_total,
        "spans_hit": spans_hit,
        "latency_ms": 1200,
        "retrieval_mode": "keyword",
        "run_status": "completed",
        "verdicts": 2,
        "usage": {"calls": calls, "total_tokens": calls * 900, "token_source": "api",
                  "estimated_cost_usd": cost},
    }


class TestScoring:
    def test_citation_on_an_expert_span_counts_as_localized(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": [CAP_SPAN]},
                            _verdict(rating="amber", citations=[CAP_SPAN]))
        assert detail["expert_present"] is True
        assert detail["localization_recall"] == 1.0
        assert detail["agreement"] is True
        assert detail["over_claim"] is False

    def test_citing_a_different_paragraph_is_a_miss_not_a_disagreement(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": [CAP_SPAN]},
                            _verdict(rating="amber", citations=[OTHER_SPAN]))
        assert detail["localization_recall"] == 0.0
        assert detail["agreement"] is True  # we did not claim absence

    def test_claiming_absence_when_the_expert_found_it_is_a_disagreement(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": [CAP_SPAN]},
                            _verdict(rating="red", gap_reason="文档未约定责任上限"))
        assert detail["engine_claims_absence"] is True
        assert detail["agreement"] is False

    def test_matching_a_gap_is_the_correct_answer(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": []},
                            _verdict(rating="red", gap_reason="文档未约定责任上限"))
        assert detail["expert_present"] is False
        assert detail["agreement"] is True
        assert detail["localization_recall"] is None

    def test_citing_content_the_expert_found_nowhere_is_an_over_claim(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": []},
                            _verdict(rating="green", citations=[OTHER_SPAN]))
        assert detail["over_claim"] is True
        assert detail["agreement"] is False  # citing instead of reporting a gap is not agreement

    def test_a_citation_alongside_a_gap_reason_is_not_claiming_absence(self):
        detail = score_rule("责任上限", {"categories": ["Cap On Liability"], "spans": [CAP_SPAN]},
                            _verdict(citations=[CAP_SPAN], gap_reason="部分约定"))
        assert detail["engine_claims_absence"] is False

    def test_rules_the_engine_never_scored_are_listed_not_guessed(self):
        contract = _contract(**{"Cap On Liability": [CAP_SPAN]})
        scored = score_contract(contract, MAPPING, [_verdict(citations=[CAP_SPAN])])
        assert scored["unjudged_rules"] == ["审计权"]
        assert scored["present_rules"] == 1
        assert scored["length_band"] == BAND_UNDER

    def test_bands_and_cost_are_pooled_not_averaged(self):
        results = [
            _result(BAND_UNDER, spans_total=10, spans_hit=8, calls=7, cost=0.1),
            _result(BAND_UNDER, spans_total=10, spans_hit=2, calls=7, cost=0.1),
            _result(BAND_OVER, spans_total=4, spans_hit=1, calls=9, cost=0.2),
        ]
        overall = aggregate(results)
        assert overall["localization_recall"] == pytest.approx(11 / 24, abs=1e-4)
        bands = overall["localization_recall_by_band"]
        assert bands[BAND_UNDER]["localization_recall"] == pytest.approx(0.5, abs=1e-4)
        assert bands[BAND_OVER]["localization_recall"] == pytest.approx(0.25, abs=1e-4)
        assert bands[BAND_UNDER]["contracts"] == 2
        assert overall["llm_calls"] == 23
        assert overall["tokens"] == 23 * 900
        assert overall["cost_usd"] == pytest.approx(0.4)
        assert overall["spans_hit_text"] == "11/24"
        assert overall["presence_text"] == "3/3"
        assert overall["absence_text"] == "0/3"

    def test_a_single_result_set_still_aggregates(self):
        overall = aggregate([_result(BAND_OVER, spans_total=0, spans_hit=0, calls=1, cost=0.0)])
        assert overall["localization_recall"] is None
        assert overall["spans_hit_text"] == "0/0"


class TestReport:
    def test_markdown_renders_from_aggregate_alone(self):
        # Regression: the renderer used to need keys that only main() injected.
        results = [
            _result(BAND_UNDER, spans_total=10, spans_hit=8, calls=7, cost=0.1),
            _result(BAND_OVER, spans_total=4, spans_hit=1, calls=9, cost=0.2),
        ]
        text = render_markdown(
            results, model="deepseek-flash", failed=["Dropped Contract"]
        )
        assert "## 总体" in text
        assert "Dropped Contract" in text
        assert f"定位召回 · {BAND_UNDER}" in text
        assert f"定位召回 · {BAND_OVER}" in text
        assert "64.3%" in text  # 9 of 14 expert spans
        assert "$0.3000" in text

    def test_report_states_the_severity_limitation(self):
        text = render_markdown(
            [_result(BAND_UNDER, spans_total=4, spans_hit=4, calls=3, cost=0.05)],
            model="deepseek-flash",
            failed=[],
        )
        assert "不评判红黄绿档位" in text
        assert "LICENSE" in text or "license" in text

    def test_a_fallback_from_the_requested_mode_is_called_out(self, monkeypatch):
        monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
        assert "降级" in _format_modes({"keyword": 8})
        assert "降级" not in _format_modes({"hybrid": 8})
        # A mixed run is never a clean hybrid run, even if hybrid is in there.
        assert "降级" in _format_modes({"hybrid": 4, "keyword": 4})
