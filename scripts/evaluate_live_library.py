"""Live-library audit: what the platform's own stored verdicts say about itself.

Complements the seeded-defect gold set, which can only measure synthetic
documents. Here there are no planted answers — instead we interrogate what the
engine already produced on real uploads:

- intake sanity  : documents too thin to review (parse stubs, wrong file type)
                   that still generated a full all-red scorecard
- citation truth : stored citations re-validated verbatim against the clauses
                   the engine actually saw
- governance     : greens without a citation, red/ambers with neither a
                   traceable quote nor a gap reason
- sign-off truth : how much of the queue a human has actually adjudicated
                   (rating drift lives in `rulebook analytics`)

Read-only against the platform database; makes no LLM calls, so it is free to
run any time. Output: reports/LIVE_LIBRARY_AUDIT.md (+ --json).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REPORT_PATH = PROJECT_ROOT / "reports" / "LIVE_LIBRARY_AUDIT.md"


def audit_document(doc: dict) -> dict:
    """Attach intake / citation / governance flags to one document row bundle.

    ``doc``: {"friendly_id", "playbook_id", "status", "clauses": [text],
    "verdicts": [{"rule_name","dimension","rating","review_state","rationale",
    "gap_reason","citations"}]}.

    No document title on purpose: titles are real uploaded filenames (contract
    numbers, invoice ids, customer SOW names) and this report is committed.
    ``friendly_id`` locates the row in the UI.
    """

    from backend.engine.citation_gate import validate_citations
    from backend.engine.parsing import reviewability_reason

    clause_text = "\n".join(doc["clauses"])
    verdicts = doc["verdicts"]
    red = sum(1 for v in verdicts if v["rating"] == "red")
    amber = sum(1 for v in verdicts if v["rating"] == "amber")
    green = sum(1 for v in verdicts if v["rating"] == "green")
    decided = [v for v in verdicts if v["review_state"] in ("approved", "edited", "rejected")]
    pending = [v for v in verdicts if v["review_state"] == "awaiting_review"]

    emitted = invalid = 0
    green_without_citation = 0
    unsubstantiated = 0
    for verdict in verdicts:
        citations = verdict["citations"] or []
        valid, bad = validate_citations(citations, clause_text)
        emitted += len(citations)
        invalid += len(bad)
        if verdict["rating"] == "green" and not valid:
            green_without_citation += 1
        if verdict["rating"] in ("red", "amber") and not valid \
                and not (verdict["gap_reason"] or "").strip():
            unsubstantiated += 1

    chars = sum(len(text) for text in doc["clauses"])
    blocked = reviewability_reason(len(doc["clauses"]), chars)
    flags = [blocked] if blocked else []
    if not verdicts:
        flags.append("无判定记录")

    return {
        "friendly_id": doc["friendly_id"],
        "playbook_id": doc["playbook_id"],
        "status": doc["status"],
        "clause_count": len(doc["clauses"]),
        "chars": chars,
        "verdicts": len(verdicts),
        "red": red,
        "amber": amber,
        "green": green,
        "decided": len(decided),
        "pending": len(pending),
        "citations_emitted": emitted,
        "citations_invalid": invalid,
        "green_without_citation": green_without_citation,
        "unsubstantiated": unsubstantiated,
        "flags": flags,
    }


def load_library(session) -> list[dict]:
    """Every document with its clauses and verdicts, as plain dicts."""

    from backend.documents.models import Clause, Document, Rule, Verdict

    rules = {rule.id: rule for rule in session.query(Rule).all()}
    bundles = []
    for document in session.query(Document).order_by(Document.friendly_id).all():
        clauses = [
            clause.text
            for clause in session.query(Clause)
            .filter(Clause.document_id == document.id)
            .order_by(Clause.ordinal)
            .all()
        ]
        verdicts = []
        for verdict in session.query(Verdict).filter(Verdict.document_id == document.id).all():
            rule = rules.get(verdict.rule_id)
            verdicts.append(
                {
                    "rule_name": rule.name if rule else verdict.rule_id,
                    "dimension": rule.dimension if rule else "",
                    "rating": verdict.rating,
                    "review_state": verdict.review_state,
                    "rationale": verdict.rationale or "",
                    "gap_reason": verdict.gap_reason or "",
                    "citations": verdict.citations or [],
                }
            )
        bundles.append(
            {
                "friendly_id": document.friendly_id,
                "playbook_id": document.playbook_id,
                "status": document.status,
                "clauses": clauses,
                "verdicts": verdicts,
            }
        )
    return bundles


def summarise(audits: list[dict]) -> dict:
    verdicts = sum(a["verdicts"] for a in audits)
    emitted = sum(a["citations_emitted"] for a in audits)
    decided = sum(a["decided"] for a in audits)
    flagged = [a for a in audits if a["flags"]]
    return {
        "documents": len(audits),
        "verdicts": verdicts,
        "pending": sum(a["pending"] for a in audits),
        "decided": decided,
        "decided_share": round(decided / verdicts, 4) if verdicts else None,
        "citations_emitted": emitted,
        "citations_invalid": sum(a["citations_invalid"] for a in audits),
        "citation_genuineness": (
            round((emitted - sum(a["citations_invalid"] for a in audits)) / emitted, 4)
            if emitted
            else None
        ),
        "green_without_citation": sum(a["green_without_citation"] for a in audits),
        "unsubstantiated": sum(a["unsubstantiated"] for a in audits),
        "flagged_documents": len(flagged),
        "flagged_verdicts": sum(a["verdicts"] for a in flagged),
        "queue_pollution": (
            round(sum(a["pending"] for a in flagged) / sum(a["pending"] for a in audits), 4)
            if sum(a["pending"] for a in audits)
            else None
        ),
    }


def render_markdown(audits: list[dict]) -> str:
    overall = summarise(audits)
    lines = [
        "# 平台库存量体检（Live Library Audit）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"数据源：`data/rulebook.db`（只读，未调用模型）",
        f"范围：{overall['documents']} 份文档 / {overall['verdicts']} 条判定",
        "",
        "## 总体",
        "",
        "| 指标 | 数值 | 含义 |",
        "|---|---:|---|",
        f"| 已处置占比 | {_pct(overall['decided_share'])}（{overall['decided']}/{overall['verdicts']}） "
        "| 专家真的看过并给出结论的判定比例，其余无对照价值 |",
        f"| 存量引用真实率 | {_pct(overall['citation_genuineness'])} "
        f"（{overall['citations_emitted'] - overall['citations_invalid']}/{overall['citations_emitted']}） "
        "| 入库引用逐字回查子句原文的成功率 |",
        f"| 绿灯无有效引用 | {overall['green_without_citation']} | 引用治理漏洞（应为 0） |",
        f"| 红/黄判定无凭据 | {overall['unsubstantiated']} | 既无可回溯引用，也无缺口理由 |",
        f"| 低于评审下限的文档 | {overall['flagged_documents']} 份 / {overall['flagged_verdicts']} 条判定 "
        "| 条款数不足却已产出整套判定 |",
        f"| 这类文档占用的签字队列 | {_pct(overall['queue_pollution'])} | 本不该占用专家时间 |",
        "",
        "## 逐份",
        "",
        "| 文档 | 剧本 | 判定 | 红/黄/绿 | 已处置 | 待签 | 引用(发出/无效) | 正文字符 | 标记 |",
        "|---|---|---:|---|---:|---:|---|---:|---|",
    ]
    for audit in audits:
        lines.append(
            f"| {audit['friendly_id']} | {audit['playbook_id']} "
            f"| {audit['verdicts']} | {audit['red']}/{audit['amber']}/{audit['green']} "
            f"| {audit['decided']} | {audit['pending']} "
            f"| {audit['citations_emitted']}/{audit['citations_invalid']} | {audit['chars']} "
            f"| {'；'.join(audit['flags']) or '—'} |"
        )
    lines += [
        "",
        "## 怎么读这份报告",
        "",
        "- 被标记的文档就是引擎现在会在入库时拒绝的那一类（条款数低于 "
        "`MIN_REVIEW_CLAUSES`）：任何剧本都不可能在其中找到全部条款，整套红判定是引擎在解读"
        "“没解析出内容”，不是在评估合同风险。这些是历史积压，重新上传不会再产生。",
        "- 引用真实率是对**已入库**判定的事后回查，与种入缺陷评测里的“发出前后对比”不同："
        "这里看不到被门禁过滤掉的引用，只关心库里现存引用是否可回溯。",
        "- 已处置占比低的文档不具备“引擎 vs 专家”对照价值：没有人工结论可当标签。",
        "",
    ]
    return "\n".join(lines)


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument("--output", help="写入文件路径（默认写 reports/LIVE_LIBRARY_AUDIT.md）")
    args = parser.parse_args()

    from backend.platform_db import platform_session

    session = platform_session()
    try:
        audits = [audit_document(doc) for doc in load_library(session)]
    finally:
        session.close()
    if not audits:
        print("平台库为空：先 `rulebook serve` 并上传文档。")
        return 1

    payload = (
        json.dumps({"audits": audits, "overall": summarise(audits)}, ensure_ascii=False, indent=2)
        if args.json
        else render_markdown(audits)
    )
    target = Path(args.output).expanduser().resolve() if args.output else REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.rstrip() + "\n", encoding="utf-8")
    overall = summarise(audits)
    print(f"Wrote {target}")
    print(
        f"{overall['documents']} 份文档 · 已处置 {_pct(overall['decided_share'])} · "
        f"引用真实率 {_pct(overall['citation_genuineness'])} · "
        f"低于评审下限 {overall['flagged_documents']} 份（占用签字队列 {_pct(overall['queue_pollution'])}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
