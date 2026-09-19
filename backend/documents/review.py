"""Signature gate: verdict review state machine and document finalization.

States: drafted → awaiting_review → approved | edited | rejected.
Rules encoded here are the product's governance core:
  - Red/amber verdicts must be expert-approved (or rejected) before export.
  - Every decision writes a ReviewEvent AND an audit entry.
  - Illegal transitions are rejected loudly, never coerced.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.documents.audit import record_audit
from backend.documents.models import Document, ReviewEvent, Rule, Verdict
from backend.engine.scorecard import build_scorecard

VALID_DECISIONS = ("approve", "edit", "reject")

# decision → allowed source states
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "approve": ("awaiting_review", "rejected"),
    "edit": ("awaiting_review", "drafted", "approved", "rejected"),
    "reject": ("awaiting_review", "drafted", "approved"),
}

DECISION_TO_STATE = {"approve": "approved", "edit": "edited", "reject": "rejected"}


@dataclass
class DecisionResult:
    verdict: Verdict
    document: Document


class IllegalTransition(ValueError):
    pass


def _refresh_scorecard(session: Session, document: Document) -> None:
    """Recompute the scorecard from current verdicts (expert edits count)."""

    rules = {r.id: r for r in session.query(Rule).filter(Rule.playbook_id == document.playbook_id).all()}
    verdicts = session.query(Verdict).filter(Verdict.document_id == document.id).all()
    rows = []
    for verdict in verdicts:
        rule = rules.get(verdict.rule_id)
        rows.append(
            {
                "rule_id": verdict.rule_id,
                "dimension": rule.dimension if rule else "未分类",
                "name": rule.name if rule else verdict.rule_id,
                "rating": verdict.rating,
                "weight": rule.weight if rule else 1,
                "review_state": verdict.review_state,
            }
        )
    from backend.playbooks import get_playbook

    playbook = get_playbook(document.playbook_id)
    document.scorecard = build_scorecard(
        rows, playbook_name=playbook.name, dimensions=list(playbook.dimensions)
    )


def apply_decision(
    session: Session,
    *,
    document: Document,
    verdict: Verdict,
    decision: str,
    actor: str,
    expert_note: str | None = None,
    new_rating: str | None = None,
    rationale: str | None = None,
) -> DecisionResult:
    decision = decision.lower().strip()
    if decision not in VALID_DECISIONS:
        raise IllegalTransition(f"未知操作: {decision}")
    rating_before = verdict.rating
    if verdict.review_state not in _ALLOWED_TRANSITIONS[decision]:
        raise IllegalTransition(
            f"不允许的状态迁移: {verdict.review_state} → {DECISION_TO_STATE[decision]}"
        )
    if decision == "reject" and not (expert_note or "").strip():
        raise IllegalTransition("驳回必须填写专家意见")

    if new_rating is not None and new_rating not in ("red", "amber", "green"):
        raise IllegalTransition(f"无效的改判等级: {new_rating}")

    if decision == "edit":
        if new_rating is None and rationale is None:
            raise IllegalTransition("改判必须提供新等级或修改理由")
        if new_rating is not None:
            verdict.rating = new_rating
        if rationale is not None:
            verdict.rationale = rationale
        verdict.review_state = "edited"
        verdict.expert_note = expert_note
    else:
        verdict.review_state = DECISION_TO_STATE[decision]
        verdict.expert_note = expert_note

    verdict.reviewed_by = actor
    verdict.reviewed_at = datetime.now(timezone.utc)

    session.add(
        ReviewEvent(
            id=str(uuid.uuid4()),
            document_id=document.id,
            verdict_id=verdict.id,
            actor=actor,
            action=decision,
            detail={
                "rating_before": rating_before,
                "rating_after": verdict.rating,
                "ai_rating": verdict.initial_rating,
                "state": verdict.review_state,
                "note": expert_note,
            },
        )
    )
    record_audit(
        session,
        correlation_id=document.friendly_id,
        event=f"verdict.{decision}",
        actor=actor,
        payload={
            "verdict_id": verdict.id,
            "rule_id": verdict.rule_id,
            "rating_before": rating_before,
            "rating": verdict.rating,
            "review_state": verdict.review_state,
        },
    )
    _refresh_scorecard(session, document)
    session.commit()
    return DecisionResult(verdict=verdict, document=document)


def pending_review_count(session: Session, document: Document) -> int:
    return (
        session.query(Verdict)
        .filter(Verdict.document_id == document.id, Verdict.review_state == "awaiting_review")
        .count()
    )


def finalize_document(session: Session, *, document: Document, actor: str) -> Document:
    """Sign-off gate: nothing exports while any verdict awaits review."""

    if document.status == "finalized":
        return document  # idempotent re-finalize
    if document.status != "awaiting_review":
        raise IllegalTransition(f"当前状态不可定稿: {document.status}")
    pending = pending_review_count(session, document)
    if pending:
        raise IllegalTransition(f"还有 {pending} 条判定待专家签字")
    document.status = "finalized"
    record_audit(
        session,
        correlation_id=document.friendly_id,
        event="document.finalized",
        actor=actor,
        payload={"scorecard": document.scorecard},
    )
    from backend.notifications.service import create_notification_and_email as create_notification

    create_notification(
        session,
        type="finalized",
        title=f"{document.friendly_id} 已定稿",
        body=f"{document.title} 完成签字定稿，报告可导出。",
        payload={"document_id": document.id},
        correlation_id=document.friendly_id,
    )
    session.commit()
    return document
