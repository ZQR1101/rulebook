"""Scoring agent: rule-by-rule verdicts with mandatory citations.

Protocol with the LLM (strict JSON):
  {"rating": "red|amber|green",
   "rationale": "…判定理由…",
   "citations": [{"quote": "…文档原文…"}, …],
   "gap_reason": "…找不到对应内容时说明缺失…"}

Any protocol violation (unparseable JSON, bad rating, empty rationale) is a
first-class failure: the verdict becomes amber + awaiting_review with a
machine-readable failure note — never a silent green.
"""

from __future__ import annotations

import json
import re

from backend.engine.citation_gate import validate_citations
from backend.engine.parsing import ParsedClause
from backend.engine.retrieval import select_clauses

VALID_RATINGS = ("red", "amber", "green")

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_prompt(rule, clauses: list[ParsedClause], playbook_instructions: str) -> str:
    clause_blocks = "\n\n".join(
        f"[条款 {clause.ordinal}]{(' ' + clause.heading) if clause.heading else ''}\n{clause.text}"
        for clause in clauses
    )
    return f"""{playbook_instructions}

## 规则手册条目
- 维度：{rule.dimension}
- 规则：{rule.name}
- 判定标准：{rule.guidance}

## 待审文档条款
{clause_blocks}

## 输出要求
只输出一个 JSON 对象，不要输出其他文字：
{{"rating": "red|amber|green", "rationale": "判定理由（中文，≤150字）", "citations": [{{"quote": "文档原文片段"}}], "gap_reason": "文档缺失该内容时填写，否则留空"}}
注意：quote 必须逐字摘自上方条款原文；green 判定必须至少有一条有效引用；文档中找不到对应内容时 rating=red 并填写 gap_reason。
边界：本次只提供文档正文，附件内容不可核实不属于被审条款的缺陷——条款本身清晰合规时判 green，并在 rationale 末尾注明「附件未提供，需补充核对」；只有条款自身含糊或缺少关键要素时才判 amber/red。"""


def _extract_json(content: str) -> dict | None:
    match = _JSON_BLOCK_RE.search(content or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _default_scoring_llm():
    from backend.llm_service import llm as default_llm

    return default_llm


def _llm_invoke(custom_llm, prompt: str) -> str:
    llm = custom_llm if custom_llm is not None else _default_scoring_llm()
    response = llm.invoke(prompt)
    content = getattr(response, "content", response)
    return content if isinstance(content, str) else str(content)


def score_rule(
    rule,
    clauses: list[ParsedClause],
    document_text: str,
    *,
    playbook_instructions: str,
    custom_llm=None,
    embedder=None,
    retrieval_mode: str = "hybrid",
) -> dict:
    """Produce one verdict dict for one rule against the document."""

    selected = select_clauses(
        clauses,
        f"{rule.name} {rule.guidance}",
        embedder=embedder,
        mode=retrieval_mode,
    )
    prompt = _build_prompt(rule, selected, playbook_instructions)
    raw_content = _llm_invoke(custom_llm, prompt)
    data = _extract_json(raw_content)

    clause_texts = [clause.text for clause in selected]
    failure = None
    if data is None:
        data = {}
        failure = "llm_parse_error"
    rating = str(data.get("rating", "")).lower()
    if rating not in VALID_RATINGS:
        rating = "amber"
        failure = failure or "invalid_rating"

    rationale = str(data.get("rationale") or "").strip()
    gap_reason = str(data.get("gap_reason") or "").strip() or None
    citations = data.get("citations") if isinstance(data.get("citations"), list) else []
    valid_citations, invalid_citations = validate_citations(
        citations, document_text, clause_texts
    )

    if failure is None and not gap_reason and rating == "green" and not valid_citations:
        # Mandatory-citation gate: unsupported greens are downgraded.
        rating = "amber"
        failure = "missing_citation"

    if failure is not None:
        review_state = "awaiting_review"
        if not rationale:
            rationale = {
                "llm_parse_error": "评分模型输出无法解析，需要人工判定",
                "invalid_rating": "评分模型返回了无效等级，需要人工判定",
                "missing_citation": "绿色判定缺少有效原文引用，已降级待人工确认",
            }[failure]
    elif rating == "green":
        review_state = "drafted"
    else:
        review_state = "awaiting_review"

    return {
        "rule_id": rule.id,
        "rule_name": rule.name,
        "dimension": rule.dimension,
        "weight": rule.weight,
        "rating": rating,
        "rationale": rationale,
        "citations": valid_citations,
        "invalid_citations": invalid_citations,
        "gap_reason": gap_reason,
        "review_state": review_state,
        "failure": failure,
    }
