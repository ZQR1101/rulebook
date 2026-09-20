"""Live-library audit tests: the flags must fire on real failure shapes.

Pure-data tests — no database, no model. Each fixture mimics a shape actually
seen in the platform library, including the parse-stub documents that produced
a full all-red scorecard from a few dozen characters of text. The intake flag is
the engine's own refusal reason, so these tests also pin the two together.
"""

from __future__ import annotations

from backend.engine.parsing import MIN_REVIEW_CLAUSES
from scripts.evaluate_live_library import audit_document, render_markdown, summarise


def _verdict(rule, rating, *, citations=(), gap_reason="", state="awaiting_review"):
    return {
        "rule_name": rule,
        "dimension": "商业",
        "rating": rating,
        "review_state": state,
        "rationale": "",
        "gap_reason": gap_reason,
        "citations": [{"quote": q} for q in citations],
    }


def _doc(friendly_id="CG-0001", clauses=("甲方应在收到发票后30日内付款。",), verdicts=()):
    return {
        "friendly_id": friendly_id,
        "title": "样例合同",
        "playbook_id": "contract-compliance",
        "status": "awaiting_review",
        "clauses": list(clauses),
        "verdicts": list(verdicts),
    }


def _reviewable(prefix: str = "甲") -> list[str]:
    return [prefix * 300] * MIN_REVIEW_CLAUSES


def test_a_stub_document_is_flagged_with_the_engines_refusal():
    stub = _doc(
        friendly_id="CG-0005",
        clauses=("软件开发合同 本合同约定开发一套演示系统。",),
        verdicts=[_verdict(f"规则{i}", "red", gap_reason="文档未约定") for i in range(15)],
    )

    audit = audit_document(stub)

    assert audit["flags"] == [
        "只解析出 1 段条款 / 21 字符正文，低于可评审下限 3 段："
        "逐条核对无从进行，疑为条款未能切分（解析失败）或文档类型不符"
    ]


def test_a_split_document_carries_no_intake_flag():
    real = _doc(
        clauses=_reviewable(),
        verdicts=[_verdict("付款账期", "green", citations=["甲" * 300])],
    )

    assert audit_document(real)["flags"] == []


def test_flagged_verdicts_need_a_traceable_quote_or_a_gap_reason():
    doc = _doc(
        clauses=("甲" * 900,),
        verdicts=[
            _verdict("责任上限", "red"),
            _verdict("审计权", "amber", citations=["不存在的话"]),
            _verdict("付款账期", "green", citations=["甲" * 900]),
            _verdict("适用法律", "red", gap_reason="全文未约定适用法律"),
        ],
    )

    audit = audit_document(doc)

    assert audit["unsubstantiated"] == 2  # the quote-less red + the amber's fake quote
    assert audit["green_without_citation"] == 0  # the green has a real quote
    assert audit["citations_emitted"] == 2
    assert audit["citations_invalid"] == 1


def test_queue_pollution_is_measured_against_pending_verdicts():
    stub = _doc(
        friendly_id="CG-0004",
        clauses=("短。",),
        verdicts=[_verdict("责任上限", "red", gap_reason="未约定")] * 15,
    )
    real = _doc(
        friendly_id="CG-0002",
        clauses=_reviewable("乙"),
        verdicts=[_verdict("付款账期", "green", citations=["乙" * 300])] * 5,
    )

    audits = [audit_document(stub), audit_document(real)]

    assert audit_document(real)["flags"] == []
    assert summarise(audits)["queue_pollution"] == 0.75  # 15 of 20 pending
    assert summarise(audits)["flagged_documents"] == 1


def test_report_names_the_refused_corpus_and_the_adjudicated_share():
    audits = [
        audit_document(
            _doc(
                friendly_id="CG-0007",
                clauses=("Receipt",),
                verdicts=[_verdict("责任上限", "red", gap_reason="未约定")] * 15,
            )
        )
    ]

    report = render_markdown(audits)

    assert "低于评审下限的文档 | 1 份 / 15 条判定" in report
    assert "已处置占比 | 0.0%" in report


def test_the_report_never_carries_an_uploaded_filename():
    """Titles are real filenames — contract numbers, invoice ids, client SOW names.

    The per-document row used to append the upload filename to the friendly id,
    which puts client-identifying text into a committed artifact.
    """

    doc = _doc(
        verdicts=[_verdict("付款账期", "green", citations=["甲方应在收到发票后30日内付款。"])],
    )
    doc["title"] = "软件开发合同（SAMPLE-NOT-A-REAL-ID）.docx"

    audit = audit_document(doc)
    report = render_markdown([audit])

    assert "title" not in audit
    assert "SAMPLE-NOT-A-REAL-ID" not in report
    assert "软件开发合同" not in report
    assert "CG-0001" in report  # the row is still locatable in the UI
