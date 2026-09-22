"""Corpus builder guarantees: labels come from the rendered text, preflight is strict.

The v2 corpus exists because v1 could not tell our engine apart from a general
agent (see reports/BLIND_ARM_EVAL_REPORT.md). Two things have to be true or the
new corpus is worse than the old one, and both are checked here without an LLM:
a gold annotation must be *findable* in the real splitter output, and a clause
title must not hand its rule name to keyword retrieval.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import split_clauses  # noqa: E402
from scripts import build_seeded_corpus as builder  # noqa: E402
from scripts.evaluate_seeded_defects import gold_index, load_cases, validate_gold  # noqa: E402

PLAYBOOK = "contract-compliance"
RULES = list(builder.playbook_rules(PLAYBOOK))


def _manifest(answers: dict[str, str], *, decoys: int = 3) -> dict:
    """One clause per answered rule, plus unlabelled decoy prose."""

    clauses = []
    for index, (rule, sentence) in enumerate(answers.items(), start=1):
        clauses.append(
            {
                "heading": f"商务约定{index}",  # never contains the rule name
                "sentences": [sentence],
                "answers": rule,
                "kind": "defect" if sentence.startswith("红：") else "clean",
                "expected_rating": "red" if sentence.startswith("红：") else None,
                "note": sentence,
            }
        )
        clauses.append(
            {
                "heading": f"一般约定{index}",
                "sentences": [f"本条为第 {index} 项一般性约定，双方按诚实信用原则履行各自义务。"],
            }
        )
    for extra in range(decoys):
        clauses.append(
            {
                "heading": f"附则{extra + 1}",
                "sentences": [f"附件 {extra + 1} 由双方另行签署，与本合同具有同等效力。"],
            }
        )
    return {
        "defaults": {"playbook_id": PLAYBOOK, "min_chars": 0, "max_chars": 10**6},
        "documents": [
            {
                "id": "TEST-V2-01",
                "title": "测试合同",
                "part_titles": ["商务条款", "技术条款"],
                "preamble": ["甲方：测试大学信息中心", "合同编号：TEST-2026-001"],
                "parts": [
                    {"clauses": clauses[: max(len(clauses) // 2, 1)]},
                    {"clauses": clauses[max(len(clauses) // 2, 1) :]},
                ],
            }
        ],
    }


def _write(tmp_path: Path, manifest: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def _build(tmp_path: Path, manifest: dict, hint: dict | None = None):
    cases, documents, meta = builder.build(_write(tmp_path, manifest), hint)
    return cases, documents


@pytest.fixture
def small_budget(monkeypatch):
    """Test manifests are short; the budget guard is about the real corpus shape."""

    monkeypatch.setattr(builder, "RETRIEVAL_BUDGET_CHARS", 40)


def test_every_declared_clause_is_locatable_in_the_split_output(tmp_path, small_budget):
    answers = {rule: f"双方确认第 {index} 项，结算于验收合格后 30 日完成。" for index, rule in enumerate(RULES[:6])}
    cases, documents = _build(tmp_path, _manifest(answers))
    spec, text = documents["TEST-V2-01"]
    parsed_ordinals = {clause.ordinal for clause in split_clauses(text)}

    for rule, ordinals in cases[0]["retrieval_gold"].items():
        assert set(ordinals) <= parsed_ordinals, f"{rule} 的金标序号不在切分结果里"
        assert len(ordinals) == 1, "每条规则在本语料里只对应一段条款"


def test_gold_buckets_cover_every_rule_of_the_playbook(tmp_path, small_budget):
    answers = {rule: f"第 {index} 项约定，指标为 99.9%。" for index, rule in enumerate(RULES[:4])}
    cases, documents = _build(tmp_path, _manifest(answers))
    case = cases[0]

    assert len(case["defects"]) + len(case["clean"]) == 4
    assert {gap["rule"] for gap in case["gaps"]} == set(RULES) - set(RULES[:4])
    rules = set(builder.playbook_rules(PLAYBOOK))
    assert validate_gold(case, rules) == [], "导出的金标必须能被评测脚本原样接受"
    assert set(gold_index(case)) == rules


def test_ambiguous_first_sentence_is_refused(tmp_path, small_budget):
    """Two clauses with the same opening sentence cannot both be gold for anything."""

    manifest = _manifest({"付款账期": "重复的开头句。", "责任上限": "重复的开头句。"})
    with pytest.raises(ValueError, match="命中 2 段"):
        builder.build(_write(tmp_path, manifest))


def test_shared_boilerplate_opening_is_resolved_by_a_longer_probe(tmp_path, small_budget):
    """Several clauses legitimately open with the same sentence; that is not fatal."""

    manifest = _manifest({"付款账期": "双方本着诚实信用原则履行本合同。", "责任上限": "双方本着诚实信用原则履行本合同。"})
    for part in manifest["documents"][0]["parts"]:
        for clause in part["clauses"]:
            if clause.get("answers") == "付款账期":
                clause["sentences"].append("尾款在最终验收后 120 日支付。")
            elif clause.get("answers") == "责任上限":
                clause["sentences"].append("赔偿总额以合同年度总额为限。")
    cases, documents = _build(tmp_path, manifest)

    gold = cases[0]["retrieval_gold"]
    assert gold["付款账期"] != gold["责任上限"], "两段共用开头必须落在不同序号上"
    assert set(gold["付款账期"] + gold["责任上限"]) <= {
        clause.ordinal for clause in split_clauses(documents["TEST-V2-01"][1])
    }


def test_duplicate_rule_annotation_is_refused(tmp_path, small_budget):
    manifest = _manifest({"付款账期": "账期 120 天。"})
    manifest["documents"][0]["parts"][1]["clauses"].append(
        {
            "heading": "补充支付约定",
            "sentences": ["尾款于最终验收后 60 日内结清。"],
            "answers": "付款账期",
            "kind": "clean",
        }
    )
    with pytest.raises(ValueError, match="金标必须唯一"):
        builder.build(_write(tmp_path, manifest))


def test_heading_that_spells_its_rule_name_fails_preflight(tmp_path, small_budget):
    manifest = _manifest({"付款账期": "账期 120 天，超期部分按日 1% 计。"})
    manifest["documents"][0]["parts"][0]["clauses"][0]["heading"] = "付款账期与结算"
    cases, documents = _build(tmp_path, manifest)
    errors, _ = builder.preflight(cases, documents)
    assert any("写出规则名" in error for error in errors), errors


def test_decoy_clause_naming_a_rule_fails_preflight(tmp_path, small_budget):
    manifest = _manifest({"付款账期": "账期 120 天。"})
    decoy = manifest["documents"][0]["parts"][0]["clauses"][1]
    decoy["sentences"] = ["分包披露由乙方自行决定，无需甲方同意。"]
    cases, documents = _build(tmp_path, manifest)
    errors, _ = builder.preflight(cases, documents)
    assert any("诱饵条款" in error for error in errors), errors


def test_unknown_rule_label_fails_preflight(tmp_path, small_budget):
    manifest = _manifest({"不存在的规则": "随便写点什么内容。"})
    cases, documents = _build(tmp_path, manifest)
    errors, _ = builder.preflight(cases, documents)
    assert any("剧本外的规则" in error for error in errors), errors


def test_document_inside_the_retrieval_budget_is_rejected(tmp_path, monkeypatch):
    """A short document is answered from the whole text; it measures nothing about retrieval."""

    monkeypatch.setattr(builder, "RETRIEVAL_BUDGET_CHARS", 6000)
    manifest = _manifest({"付款账期": "账期 120 天。"})
    cases, documents = _build(tmp_path, manifest)
    errors, _ = builder.preflight(cases, documents)
    assert any("考不到检索" in error for error in errors), errors


def test_clause_that_ignores_its_guidance_vocabulary_is_warned(tmp_path, small_budget):
    """The measured v2 risk: gold prose and guidance wording barely overlap."""

    rule = "审计权"  # guidance states no numeric threshold, so only vocabulary can warn
    sentence = "红：乙方每年度向甲方提交一份自查说明，甲方不得进入乙方场所核查。"
    guidance = builder.playbook_rules(PLAYBOOK)[rule]["guidance"]
    shared, _ = builder.shared_vocabulary(sentence, guidance)
    assert shared < builder.MIN_SHARED_GRAMS, "用例前提：这段条款确实与 guidance 词汇错配"

    cases, documents = _build(tmp_path, _manifest({rule: sentence}))
    _, warnings = builder.preflight(cases, documents)
    assert any(rule in warning and "共享实词二元组" in warning for warning in warnings), warnings


def test_quantitative_guidance_without_a_number_in_the_clause_is_warned(tmp_path, small_budget):
    manifest = _manifest({"可用性承诺": "红：乙方承诺平台保持高可用，具体指标见附件。"})
    cases, documents = _build(tmp_path, manifest)
    _, warnings = builder.preflight(cases, documents)
    assert any("无" in warning and "量化数值" in warning for warning in warnings), warnings


def test_written_corpus_loads_back_through_the_eval_harness(tmp_path, small_budget):
    answers = {rule: f"第 {index} 项，违约金的计算基数为合同年度总额。" for index, rule in enumerate(RULES[:5])}
    cases, documents = _build(tmp_path, _manifest(answers), {"out_dir": tmp_path / "corpus"})
    cases_path = tmp_path / "cases.json"
    builder.write_corpus(cases, documents, cases_path=cases_path)

    reloaded = load_cases(cases_path)
    assert [item["id"] for item in reloaded] == ["TEST-V2-01"]
    written = Path(reloaded[0]["document"])
    assert written == tmp_path / "corpus" / "TEST-V2-01.txt"
    assert written.read_text(encoding="utf-8") == documents["TEST-V2-01"][1]
    assert validate_gold(reloaded[0], set(builder.playbook_rules(PLAYBOOK))) == []


def test_keyword_replay_flags_gold_the_selector_cannot_reach(tmp_path):
    """The replay is the zero-cost preview of the block rate a paid run would pay for.

    Only meaningful once the document outgrows the retrieval budget: inside it,
    the engine is handed the whole text and every gold clause reads as reachable.
    """

    answers = {
        rule: f"第 {index} 项：双方同意以电子方式交换的文件视为有效文件，纸本不再另行出具。"
        for index, rule in enumerate(RULES[:3])
    }
    manifest = _manifest(answers, decoys=8)
    for part in manifest["documents"][0]["parts"]:
        for clause in part["clauses"]:
            if not clause.get("answers"):
                clause["sentences"] = [
                    clause["sentences"][0] + f"（第 {index} 次重申，编号 {index:03d}）"
                    for index in range(30)
                ]
    cases, documents = _build(tmp_path, manifest)
    text = documents["TEST-V2-01"][1]
    clause_chars = sum(len(clause.text) for clause in split_clauses(text))
    assert clause_chars > 6000, f"用例前提：文档须超出检索预算，实际条款正文 {clause_chars} 字"

    rows = builder.retrieval_replay(cases, documents)
    assert len(rows) == 3
    assert all(row["gold"] for row in rows)
    assert any(row["reachable"] is False for row in rows), f"无检索词的条款应当出现不可达金标：{rows}"
