"""The v2 composer must produce a corpus whose labels survive inspection.

v1 was too easy (a general agent with no customization matched the engine), so v2
is long, de-titled and dense with boilerplate. Length alone would make the corpus
*worse* than v1 if the labels drifted, and these properties are what keep it
honest. All of them are checked without a model call.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import split_clauses  # noqa: E402
from scripts import build_seeded_corpus as builder  # noqa: E402
from scripts import compose_corpus_v2 as composer  # noqa: E402
from scripts.corpus_v2_library import GAP_SIGNATURES  # noqa: E402

SEED = 20260922
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@pytest.fixture(scope="module")
def manifest() -> dict:
    specs, _ = composer.compose_all(SEED)
    return {
        "defaults": {"out_dir": "eval_cases/seeded_defects_v2"},
        "documents": [composer.spec_to_manifest(spec) for spec in specs],
    }


@pytest.fixture(scope="module")
def derived(manifest, tmp_path_factory) -> tuple[list[dict], dict]:
    path = tmp_path_factory.mktemp("corpus") / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    cases, documents, _ = builder.build(path)
    return cases, documents


def test_document_count_and_volume_band(derived):
    """B 档约定：16 份合同 + 8 份交付件，每份 8k~14k 字，且必须超出检索预算。"""
    cases, documents = derived
    assert len(cases) == 24
    assert sum(1 for case in cases if case["playbook_id"] == "contract-compliance") == 16
    assert sum(1 for case in cases if case["playbook_id"] == "delivery-intake") == 8
    for case in cases:
        text = documents[case["id"]][1]
        assert 8000 <= len(text) <= 14000, f"{case['id']} 正文 {len(text)} 字越界"
        clause_chars = sum(len(clause.text) for clause in split_clauses(text))
        assert clause_chars > builder.RETRIEVAL_BUDGET_CHARS, f"{case['id']} 短于检索预算，考不到检索"


def test_preflight_reports_no_errors(derived):
    cases, documents = derived
    errors, _ = builder.preflight(cases, documents)
    assert errors == []


def test_each_document_carries_enough_anchors(derived):
    """考卷规格：每份文档至少 3 个埋入缺陷、2 个缺口，否则这份文档考不出东西。"""
    cases, _ = derived
    for case in cases:
        assert len(case["defects"]) >= 3, f"{case['id']} 只有 {len(case['defects'])} 个缺陷"
        assert len(case["gaps"]) >= 2, f"{case['id']} 只有 {len(case['gaps'])} 个缺口"


def test_every_gap_is_really_absent_from_the_document(derived):
    """「文档未约定」 must be a statement about the text, not about our labelling."""
    cases, documents = derived
    for case in cases:
        text = documents[case["id"]][1]
        for gap in case["gaps"]:
            hits = [term for term in GAP_SIGNATURES[gap["rule"]] if term in text]
            assert not hits, f"{case['id']}·{gap['rule']}：正文别处出现签名词 {hits}，缺口不成立"


def test_no_rule_is_labelled_twice(derived):
    cases, _ = derived
    for case in cases:
        labels = (
            [item["rule"] for item in case["defects"]]
            + [item["rule"] for item in case["gaps"]]
            + list(case["clean"])
        )
        assert len(labels) == len(set(labels))


@pytest.mark.parametrize(
    ("rule", "rating", "band"),
    [
        ("付款账期", "green", (1, 60)),
        ("付款账期", "amber", (61, 90)),
        ("数据泄露通知", "green", (1, 72)),
        ("数据泄露通知", "amber", (73, 168)),
    ],
)
def test_threshold_bearing_clause_sits_in_its_guidance_band(derived, rule, rating, band):
    """The number the corpus plants must fall in the band the guidance names."""
    cases, documents = derived
    checked = 0
    for case in cases:
        gold = case["retrieval_gold"].get(rule)
        if not gold:
            continue
        if _labelled_as(case, rule) != rating:
            continue
        clauses = {clause.ordinal: clause.text for clause in split_clauses(documents[case["id"]][1])}
        numbers = [float(value.replace(",", "")) for value in NUMBER.findall(clauses[gold[0]])]
        in_band = [value for value in numbers if band[0] <= value <= band[1]]
        assert in_band, f"{case['id']}·{rule} 期望 {rating}（{band[0]}~{band[1]}），条款数值只有 {numbers}"
        checked += 1
    assert checked, f"语料中没有 {rule}={rating} 的样本可核对"


def _labelled_as(case: dict, rule: str) -> str:
    """The colour this document's gold assigns to ``rule`` (gap when unanswered)."""
    for defect in case["defects"]:
        if defect["rule"] == rule:
            return defect["expected_rating"]
    return "green" if rule in case["clean"] else "gap"


def test_composition_is_reproducible():
    first, _ = composer.compose_all(SEED)
    second, _ = composer.compose_all(SEED)
    assert [builder.render_document(a) for a in first] == [builder.render_document(b) for b in second]


def test_manifest_round_trips_through_the_loader(manifest, tmp_path):
    """The manifest is the durable artifact; it must load back as the same corpus."""
    specs, _ = composer.compose_all(SEED)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _, loaded = builder.load_manifest(path)
    assert [builder.render_document(a) for a in loaded] == [builder.render_document(b) for b in specs]


def test_every_rule_is_attacked_at_least_once(derived):
    """A rule that is never violated and never missing is not being examined."""
    cases, _ = derived
    attacked = {item["rule"] for case in cases for item in case["defects"]}
    missing = {item["rule"] for case in cases for item in case["gaps"]}
    assert attacked >= set(composer.LIBRARIES["contract-compliance"])
    assert attacked >= set(composer.LIBRARIES["delivery-intake"])
    assert missing, "整套语料里没有任何缺口，缺失判定这一路没被考到"
