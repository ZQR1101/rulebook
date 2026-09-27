"""Pooling parts of one arm into a baseline must not invent a measurement.

The dangerous failure here is silent: a pooled file looks exactly like a run, so
anything that distinguishes the parts has to survive — same corpus, same model,
same arm — and so does the record of which document came from which run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import pool_run_parts as pool  # noqa: E402

COMMITTED_RUN = PROJECT_ROOT / "reports" / "SEEDED_DEFECT_EVAL_METRICS.json"


def _payload(results, **overrides) -> dict:
    document = {
        "model": "deepseek-flash",
        "temperature": 0.7,
        "retrieval_arm": "keyword",
        "fake": None,
        "cases_file": "eval_cases/seeded_defect_cases_v2.json",
        "failed_cases": [],
        "results": results,
    }
    return {**document, **overrides}


def _result(case_id: str, marker: str) -> dict:
    return {"case_id": case_id, "title": marker}


def test_a_later_run_replaces_the_document_it_redid():
    first = _payload([_result("CG-A", "首轮"), _result("CG-B", "首轮")])
    second = _payload([_result("CG-B", "重跑"), _result("CG-C", "补跑")])

    merged, origin = pool.merge([("first.json", first["results"]), ("second.json", second["results"])])

    assert sorted(merged) == ["CG-A", "CG-B", "CG-C"]
    assert merged["CG-B"]["title"] == "重跑"
    assert pool._pooled_from(["first.json", "second.json"], origin) == {
        "first.json": ["CG-A"],
        "second.json": ["CG-B", "CG-C"],
    }


def test_two_arms_of_the_same_corpus_are_not_one_measurement():
    keyword = _payload([])
    oracle = _payload([], retrieval_arm="oracle")
    assert pool._conflict([keyword, oracle]) == "retrieval_arm 不一致：\"keyword\" vs \"oracle\""
    assert pool._conflict([keyword, _payload([])]) is None

    other_corpus = _payload([], cases_file="eval_cases\\seeded_defect_cases.json")
    assert pool._conflict([keyword, other_corpus]).startswith("金标集不一致")


def test_arms_that_differ_only_by_clause_budget_are_not_one_measurement():
    """The whole-document arm reports retrieval_arm=keyword, so the budget is the only handle."""

    shipping = _payload([], clause_budget_chars=6000)
    whole = _payload([], clause_budget_chars=12000)
    assert pool._conflict([shipping, whole]) == "条款预算不一致：6000 vs 12000 字/规则"
    assert pool._conflict([shipping, _payload([], clause_budget_chars=6000)]) is None
    # A part predating the field is unknown, not a contradiction: refusing it would
    # throw away paid verdicts over a label that did not exist when they ran.
    assert pool._conflict([shipping, _payload([])]) is None


def _run_pool(inputs: list[Path], tmp_path: Path, monkeypatch, extra: tuple = ()) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pool_run_parts.py",
            *(str(path) for path in inputs),
            *extra,
            "--metrics-out",
            str(tmp_path / "pooled.json"),
            "--report-out",
            str(tmp_path / "pooled.md"),
        ],
    )
    return pool.main()


def _single_pass_copy(source: dict, path: Path) -> Path:
    """The committed v1 run holds three passes; keep the first visit of each document."""

    seen: set[str] = set()
    results = []
    for result in source["results"]:
        if result["case_id"] not in seen:
            results.append(result)
            seen.add(result["case_id"])
    payload = {key: value for key, value in source.items() if key != "passes"}
    payload["results"] = results
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_pooling_a_multi_pass_run_is_refused(tmp_path, monkeypatch, capsys):
    assert _run_pool([COMMITTED_RUN], tmp_path, monkeypatch) == 1
    assert "多轮采样" in capsys.readouterr().err


def test_pooling_one_pass_reproduces_that_pass_numbers(tmp_path, monkeypatch):
    """Re-aggregating stored verdicts must not move the numbers the run itself recorded."""

    source = json.loads(COMMITTED_RUN.read_text(encoding="utf-8"))
    single = _single_pass_copy(source, tmp_path / "one_pass.json")

    assert _run_pool([single], tmp_path, monkeypatch) == 0

    pooled = json.loads((tmp_path / "pooled.json").read_text(encoding="utf-8"))
    shared = set(pooled["overall"]) & set(source["passes"][0])
    assert shared, "两轮聚合没有任何共同指标，断言等于没断"
    assert {key: pooled["overall"][key] for key in shared} == {
        key: source["passes"][0][key] for key in shared
    }
    assert [r["case_id"] for r in pooled["results"]] == [r["case_id"] for r in source["results"][:4]]
    report = (tmp_path / "pooled.md").read_text(encoding="utf-8")
    assert "本基线由多次运行拼合" in report
    assert str(single) in report


def _labelled_copy(tmp_path: Path, name: str, budget: int | None) -> Path:
    """One-pass copy of the committed v1 run, optionally carrying a clause budget."""

    source = json.loads(COMMITTED_RUN.read_text(encoding="utf-8"))
    path = _single_pass_copy(source, tmp_path / name)
    if budget is None:
        return path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["clause_budget_chars"] = budget
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_fully_labelled_pool_certifies_its_working_point(tmp_path, monkeypatch, capsys):
    parts = [_labelled_copy(tmp_path, f"part_{index}.json", 6000) for index in (1, 2)]

    assert _run_pool(parts, tmp_path, monkeypatch) == 0

    pooled = json.loads((tmp_path / "pooled.json").read_text(encoding="utf-8"))
    assert pooled["clause_budget_chars"] == 6000
    assert "条款预算同为 6000 字/规则" in capsys.readouterr().out


def test_a_part_without_a_recorded_budget_pools_but_does_not_certify(tmp_path, monkeypatch, capsys):
    legacy = _labelled_copy(tmp_path, "legacy.json", None)
    labelled = _labelled_copy(tmp_path, "labelled.json", 6000)

    assert _run_pool([legacy, labelled], tmp_path, monkeypatch) == 0

    pooled = json.loads((tmp_path / "pooled.json").read_text(encoding="utf-8"))
    assert pooled["clause_budget_chars"] is None, "有一份没记录预算，拼合结果不该自称核验过工作点"
    out = capsys.readouterr().out
    assert "1/2 份运行未记录条款预算" in out
    assert "条款预算同为" not in out


def test_an_operator_verified_budget_is_recorded_as_verified(tmp_path, monkeypatch):
    """The paid whole-document parts predate the field, but each part's own report states 12000."""

    legacy = _labelled_copy(tmp_path, "legacy.json", None)

    assert _run_pool([legacy], tmp_path, monkeypatch, extra=["--clause-budget", "12000"]) == 0

    pooled = json.loads((tmp_path / "pooled.json").read_text(encoding="utf-8"))
    assert pooled["clause_budget_chars"] == 12000
    assert "人工核对" in pooled["clause_budget_source"]
    report = (tmp_path / "pooled.md").read_text(encoding="utf-8")
    assert "由人工核对各轮报告后填入" in report
    assert "每条规则 12000 字上限" in report


def test_a_supplied_budget_contradicting_a_run_is_refused(tmp_path, monkeypatch, capsys):
    labelled = _labelled_copy(tmp_path, "labelled.json", 6000)

    assert _run_pool([labelled], tmp_path, monkeypatch, extra=["--clause-budget", "12000"]) == 1
    assert "矛盾" in capsys.readouterr().err
    assert not (tmp_path / "pooled.json").exists()
