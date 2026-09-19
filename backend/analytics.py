"""Sign-off analytics: how often the expert overturned the AI, and where.

The platform's usefulness claim is "the AI does the first pass, a human only
signs". That claim only holds if the human *keeps* most of the AI's judgements,
so every verdict carries two ratings: ``initial_rating`` (what the engine
emitted, never rewritten) and ``rating`` (what the expert signed). This module
pools the gap between them.

Definitions, deliberately conservative:

- decided     : review_state in approved | edited | rejected (a human touched it)
- overturn    : rejected, or the signed rating differs from ``initial_rating``
- auto passed : review_state drafted — engine greens that never enter the queue

Judging a disagreement needs the baseline only when it must be inferred from a
changed rating. A rejection disagrees whatever the engine said, so legacy rows
(``initial_rating`` was not recorded yet) keep counting as overturns, and the
ones whose rating change we cannot see are reported separately as
``unknown_baseline`` rather than silently treated as agreements.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

DECIDED_STATES = ("approved", "edited", "rejected")
SEVERITY = {"green": 0, "amber": 1, "red": 2}


@dataclass(frozen=True)
class VerdictRecord:
    document_id: str
    playbook_id: str
    rule_name: str
    dimension: str
    rating: str
    review_state: str
    ai_rating: str | None = None


def load_verdict_records(session) -> list[VerdictRecord]:
    """Every verdict in the platform library joined to its rule and playbook."""

    from backend.documents.models import Document, Rule, Verdict

    rows = (
        session.query(Verdict, Rule, Document.playbook_id)
        .join(Rule, Rule.id == Verdict.rule_id)
        .join(Document, Document.id == Verdict.document_id)
        .all()
    )
    return [
        VerdictRecord(
            document_id=verdict.document_id,
            playbook_id=playbook_id,
            rule_name=rule.name,
            dimension=rule.dimension,
            rating=verdict.rating,
            review_state=verdict.review_state,
            ai_rating=verdict.initial_rating,
        )
        for verdict, rule, playbook_id in rows
    ]


def _severity(rating: str | None) -> int:
    return SEVERITY.get(rating or "", 0)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def is_overturn(record: VerdictRecord) -> bool:
    """Did the expert end up disagreeing with the engine on this rule?"""

    if record.review_state not in DECIDED_STATES:
        return False
    if record.review_state == "rejected":
        return True
    return record.ai_rating is not None and record.rating != record.ai_rating


def is_comparable(record: VerdictRecord) -> bool:
    """Can we tell at all whether the expert disagreed?"""

    if record.review_state not in DECIDED_STATES:
        return False
    return record.review_state == "rejected" or record.ai_rating is not None


def summarize(records: Iterable[VerdictRecord]) -> dict:
    """Aggregate one bucket of verdicts into decision / overturn metrics."""

    records = list(records)
    states = Counter(record.review_state for record in records)
    decided = [r for r in records if r.review_state in DECIDED_STATES]
    baselined = [r for r in decided if r.ai_rating is not None]
    comparable = [r for r in decided if is_comparable(r)]
    changes = [r for r in baselined if r.rating != r.ai_rating]
    shifts = [_severity(r.rating) - _severity(r.ai_rating) for r in changes]
    overturns = sum(1 for r in comparable if is_overturn(r))

    return {
        "verdicts": len(records),
        "documents": len({r.document_id for r in records}),
        "approves": states["approved"],
        "edits": states["edited"],
        "rejects": states["rejected"],
        "awaiting_review": states["awaiting_review"],
        "auto_passed": states["drafted"],
        "decided": len(decided),
        "approve_rate": _rate(states["approved"], len(decided)),
        "edit_rate": _rate(states["edited"], len(decided)),
        "reject_rate": _rate(states["rejected"], len(decided)),
        "unknown_baseline": len(decided) - len(baselined),
        "overturns": overturns,
        "overturn_rate": _rate(overturns, len(comparable)),
        "rating_changes": len(changes),
        "tightened": sum(1 for shift in shifts if shift > 0),
        "loosened": sum(1 for shift in shifts if shift < 0),
        "mean_severity_shift": round(sum(shifts) / len(shifts), 3) if shifts else None,
        # An edit that only rewrites the rationale is human work, not disagreement.
        "rationale_only_edits": sum(
            1
            for r in decided
            if r.review_state == "edited" and r.ai_rating is not None and r.rating == r.ai_rating
        ),
        # Share of everything the engine queued that no expert has signed yet.
        "review_backlog_rate": _rate(
            states["awaiting_review"], states["awaiting_review"] + len(decided)
        ),
    }


def _grouped_rows(
    records: list[VerdictRecord], keys: tuple[str, ...], extract
) -> list[dict]:
    buckets: dict[tuple, list[VerdictRecord]] = {}
    for record in records:
        buckets.setdefault(extract(record), []).append(record)
    rows = []
    for group_keys, group in buckets.items():
        row = dict(zip(keys, group_keys))
        row.update(summarize(group))
        rows.append(row)
    rows.sort(key=lambda row: (-row["overturns"], -row["decided"], row.get("rule", "") or ""))
    return rows


def signoff_report(records: Iterable[VerdictRecord]) -> dict:
    """Full analytics payload: overall, per playbook, per dimension, per rule."""

    records = list(records)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": summarize(records),
        "by_playbook": _grouped_rows(records, ("playbook_id",), lambda r: (r.playbook_id,)),
        "by_dimension": _grouped_rows(
            records, ("playbook_id", "dimension"), lambda r: (r.playbook_id, r.dimension)
        ),
        "by_rule": _grouped_rows(
            records, ("playbook_id", "rule"), lambda r: (r.playbook_id, r.rule_name)
        ),
    }


def build_report(session) -> dict:
    return signoff_report(load_verdict_records(session))


def render_markdown(report: dict) -> str:
    """Human-readable sign-off report for ``rulebook analytics`` and reports/."""

    from backend.playbooks import get_playbook

    def pct(value) -> str:
        return "—" if value is None else f"{value * 100:.1f}%"

    def num(value) -> str:
        return "—" if value is None else str(value)

    def name_of(playbook_id: str) -> str:
        try:
            return get_playbook(playbook_id).name
        except Exception:
            return playbook_id

    overall = report["overall"]
    lines = [
        "# 签字改判分析报告（Sign-off Analytics）",
        "",
        f"生成时间：{report['generated_at']}",
        f"样本：{overall['verdicts']} 条判定 / {overall['documents']} 份文档，"
        f"其中 {overall['decided']} 条已由专家处置",
        "",
        "## 总体",
        "",
        "| 指标 | 数值 | 含义 |",
        "|---|---:|---|",
        f"| 照单采纳（approve） | {pct(overall['approve_rate'])} | 专家不改判直接签字 |",
        f"| 改判（edit） | {pct(overall['edit_rate'])} | 专家调整了等级或理由 |",
        f"| 驳回（reject） | {pct(overall['reject_rate'])} | 专家拒绝该判定 |",
        f"| **判定被推翻率** | {pct(overall['overturn_rate'])} | 驳回 + 等级实际变化 |",
        f"| 调严 / 调松 | {overall['tightened']} / {overall['loosened']} | "
        f"平均严重度偏移 {num(overall['mean_severity_shift'])} |",
        f"| 仅改理由不改等级 | {overall['rationale_only_edits']} | 有人工成本但不构成不信任 |",
        f"| 待签字积压 | {pct(overall['review_backlog_rate'])} | 引擎已标记但尚无专家结论 |",
        f"| 无 AI 基线 | {overall['unknown_baseline']} | 早期数据看不出等级是否被改动，仅驳回可判定 |",
        "",
        "## 分剧本",
        "",
        "| 剧本 | 判定数 | 已处置 | 采纳率 | 改判率 | 驳回率 | 推翻率 | 调严/调松 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["by_playbook"]:
        lines.append(
            f"| {name_of(row['playbook_id'])} | {row['verdicts']} | {row['decided']} | "
            f"{pct(row['approve_rate'])} | {pct(row['edit_rate'])} | {pct(row['reject_rate'])} | "
            f"{pct(row['overturn_rate'])} | {row['tightened']}/{row['loosened']} |"
        )

    lines += [
        "",
        "## 最常被推翻的规则（前 15）",
        "",
        "| 剧本 | 规则 | 已处置 | 推翻 | 推翻率 | 方向 |",
        "|---|---|---:|---:|---:|---|",
    ]
    overturned = [row for row in report["by_rule"] if row["overturns"]][:15]
    if not overturned:
        lines.append(
            "| — | 尚无任何专家签字记录 | 0 | 0 | — | — |"
            if not overall["decided"]
            else "| — | 已处置判定无一被推翻 | 0 | 0 | 0.0% | 全部采纳 |"
        )
    for row in overturned:
        if row["loosened"] > row["tightened"]:
            direction = "偏严被调松"
        elif row["tightened"]:
            direction = "偏松被调严"
        else:
            direction = "驳回为主"
        lines.append(
            f"| {name_of(row['playbook_id'])} | {row['rule']} | {row['decided']} | "
            f"{row['overturns']} | {pct(row['overturn_rate'])} | {direction} |"
        )

    lines += [
        "",
        "## 分维度",
        "",
        "| 剧本 | 维度 | 已处置 | 采纳率 | 推翻率 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in report["by_dimension"]:
        if not row["decided"]:
            continue
        lines.append(
            f"| {name_of(row['playbook_id'])} | {row['dimension']} | {row['decided']} | "
            f"{pct(row['approve_rate'])} | {pct(row['overturn_rate'])} |"
        )

    lines += [
        "",
        "## 说明与局限",
        "",
        "- 推翻率的分母是**可判定的已处置判定**（有 AI 基线，或已被驳回）。`initial_rating` 上线前的"
        "历史数据没有基线，仍会统计驳回，只是看不出“等级是否被改动”，这类条数列在“无 AI 基线”中。",
        "- 绿灯判定由引擎自动通过（`drafted`），不进入签字队列；采纳率只反映被人工复核的那部分风险。",
        "- 样本量小时比例抖动很大，请连同“判定数 / 已处置”一起解读。",
        "- 数据源为平台库 `verdicts` + `rules`，本文件由 `rulebook analytics` 生成。",
    ]
    return "\n".join(lines)
