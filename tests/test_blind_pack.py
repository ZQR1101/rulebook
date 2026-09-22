"""Blind-pack guarantees: the pack is the corpus minus the answer key, in the right place.

The first blind arm was assembled by hand and scored a tie against the engine.
That result is only worth anything if the competitor could not see the answers,
and "I checked carefully" is not a guarantee — these tests are.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_blind_pack import (  # noqa: E402
    banned_strings,
    doc_name,
    export_pack,
    export_rules_verbatim,
    render_task_md,
    scan_leaks,
    select_cases,
    strip_case_ids,
    strip_markers,
)
from backend.playbooks import get_playbook  # noqa: E402

CASES = json.loads(
    (PROJECT_ROOT / "eval_cases" / "seeded_defect_cases.json").read_text(encoding="utf-8")
)["cases"]
V2_CASES = json.loads(
    (PROJECT_ROOT / "eval_cases" / "seeded_defect_cases_v2.json").read_text(encoding="utf-8")
)["cases"]


def _pack(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    mapping, _ = export_pack(CASES, out_dir=tmp_path / "pack")
    return tmp_path / "pack", mapping


def test_document_names_are_opaque_counters():
    assert doc_name(0) == "DOC-A.txt"
    assert doc_name(2) == "DOC-C.txt"
    assert doc_name(25) == "DOC-Z.txt"
    assert doc_name(26) == "DOC-AA.txt"
    assert doc_name(0) != doc_name(1)


def test_a_subset_keeps_corpus_order_because_that_order_names_the_docs():
    picked = select_cases(V2_CASES, "DI-V2-03,CG-V2-02,CG-V2-05")

    assert [case["id"] for case in picked] == ["CG-V2-02", "CG-V2-05", "DI-V2-03"]
    assert select_cases(V2_CASES, None) == V2_CASES


def test_an_unknown_case_id_is_refused_rather_than_dropped():
    with pytest.raises(ValueError, match="CG-V2-99"):
        select_cases(V2_CASES, "CG-V2-02,CG-V2-99")


def test_pack_contains_docs_rules_and_task_and_nothing_else(tmp_path):
    pack, mapping = _pack(tmp_path)

    files = sorted(path.relative_to(pack).as_posix() for path in pack.rglob("*") if path.is_file())
    assert len(mapping) == len(CASES)
    assert [name for name in files if name.startswith("docs/")] == [
        f"docs/{doc_name(index)}" for index in range(len(CASES))
    ]
    assert set(files) - {f"docs/{name}" for name in mapping} == {
        "TASK.md",
        "rules/供应商合同合规.md",
        "rules/客户交付件风险分析.md",
    }
    assert not any("cases" in name or name.endswith(".json") for name in files), "金标 JSON 不得进入包内"


def test_reviewed_text_is_the_fixture_minus_the_self_annotation(tmp_path):
    pack, mapping = _pack(tmp_path)
    marker = re.compile(r"[（(][^（）()]*评测[^（）()]*[）)]")

    for name, relpath in mapping.items():
        fixture = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
        reviewed = (pack / "docs" / name).read_text(encoding="utf-8")
        annotated = [line for line in fixture.splitlines() if marker.search(line)]
        assert annotated, f"{name}：语料本应带一行自标注"

        for line in reviewed.splitlines():
            if line.strip():
                assert line.strip() in fixture, f"{name}：包内出现原文没有的行 {line[:30]!r}"
        for line in annotated:
            assert marker.search(line)
            assert line.strip() not in reviewed, f"{name}：自标注行未剥除"
            assert line.replace(marker.search(line).group(0), "").strip() in reviewed, (
                f"{name}：剥除时把正文也带走了"
            )
        assert "评测" not in reviewed


def test_rules_file_carries_the_engine_rubric_verbatim():
    exported = export_rules_verbatim("contract-compliance")
    playbook = get_playbook("contract-compliance")

    assert playbook.scoring_instructions in exported, "评审立场必须原样给出，否则两臂口径不等"
    for seed in playbook.rule_seeds:
        assert f"- 判定标准：{seed['guidance']}" in exported


def test_task_sheet_states_the_contract_without_stating_the_answers(tmp_path):
    pack, mapping = _pack(tmp_path)
    task = (pack / "TASK.md").read_text(encoding="utf-8")

    for requirement in ("rating", "gap_reason", "逐字", "硬性口径", "不要联网"):
        assert requirement in task
    for name in mapping:
        assert f"docs/{name}" in task
    assert "缺陷" not in task.replace("被审条款的缺陷", ""), "任务书不得透露语料里埋了什么"


def test_a_finished_pack_passes_the_leak_scan(tmp_path):
    pack, _ = _pack(tmp_path)

    hits = scan_leaks(pack, banned=banned_strings(CASES))
    assert hits == [], f"包内仍有泄漏：{hits}"


def test_strip_case_ids_removes_only_the_catalogue_line():
    text = "某某采购合同\n\n文档编号：CG-V2-02\n甲方（采购方）：某某\n"

    cleaned, removed = strip_case_ids(text, "CG-V2-02")

    assert cleaned == "某某采购合同\n\n甲方（采购方）：某某\n"
    assert removed == ["文档编号：CG-V2-02"]


def test_the_v2_subset_pack_passes_the_leak_scan(tmp_path):
    """v2 语料把自身编号写在正文里——那是一根指向公开仓库答案的线。"""

    subset = select_cases(V2_CASES, "CG-V2-02,CG-V2-05,DI-V2-03,DI-V2-06")
    mapping, _ = export_pack(subset, out_dir=tmp_path / "pack")
    pack = tmp_path / "pack"

    assert list(mapping) == [doc_name(index) for index in range(4)]
    assert scan_leaks(pack, banned=banned_strings(subset)) == []
    body = (pack / "docs" / doc_name(0)).read_text(encoding="utf-8")
    fixture = (PROJECT_ROOT / subset[0]["document"]).read_text(encoding="utf-8")
    assert "CG-V2-02" not in body and "文档编号" not in body
    assert body.splitlines()[0] == fixture.splitlines()[0], "只准删编号行，标题必须原样保留"


def test_leak_scan_catches_an_injected_answer_key(tmp_path):
    pack, mapping = _pack(tmp_path)
    planted = "本条为埋入点：故意删去了审计权条款"
    target = pack / "docs" / str(next(iter(mapping)))
    target.write_text(target.read_text(encoding="utf-8") + "\n" + planted + "\n", encoding="utf-8")

    cases = [{**case, "defects": case.get("defects", []) + [{"rule": "审计权", "planted": planted}]}
             for case in CASES]
    hits = scan_leaks(pack, banned=banned_strings(cases))
    assert any(planted in hit for hit in hits), "扫描必须抓到不在正文里的标注句"


def test_annotations_that_are_already_contract_text_are_not_banned():
    """A note quoting the clause is not an answer key; banning it would be a false alarm."""

    fixture = next(
        case for case in CASES if (PROJECT_ROOT / case["document"]).exists() and case.get("defects")
    )
    text = (PROJECT_ROOT / fixture["document"]).read_text(encoding="utf-8")
    quoted = next(
        line.strip()
        for line in text.splitlines()
        if len(line.strip()) >= 12 and line.strip() not in ("",)
    )
    case = {**fixture, "defects": [{"rule": "付款账期", "planted": quoted}]}

    banned = banned_strings([case])
    assert quoted not in banned
    assert case["id"] in banned


def test_pack_refuses_to_be_built_inside_the_repository(tmp_path):
    with pytest.raises(ValueError, match="仓库之外"):
        export_pack(CASES, out_dir=PROJECT_ROOT / "blind-arm-inside-repo")
    assert not (PROJECT_ROOT / "blind-arm-inside-repo").exists()


def test_strip_markers_keeps_the_title_and_drops_only_the_marker():
    cleaned, removed = strip_markers("高校学生考勤签到系统 工作说明书（合成评测样例）\n\n甲方：某学院\n")

    assert cleaned.splitlines()[0] == "高校学生考勤签到系统 工作说明书"
    assert "甲方：某学院" in cleaned
    assert removed == ["高校学生考勤签到系统 工作说明书（合成评测样例）"]


def test_task_sheet_lists_every_document_and_its_rulebook():
    task = render_task_md([("DOC-A.txt", "供应商合同合规"), ("DOC-B.txt", "客户交付件风险分析")])

    assert "| `docs/DOC-A.txt` | 供应商合同合规 |" in task
    assert "| `docs/DOC-B.txt` | 客户交付件风险分析 |" in task
    assert "out/DOC-A.json" in task
