"""Review orchestrator: the six-stage pipeline entry.

entry → parse → score → scorecard → awaiting_review → (delivery happens at
finalize time, phase 3). Runs synchronously; the API layer may offload it to
BackgroundTasks. Every stage appends an audit event under the document's
friendly correlation ID and records latency on the EngineRun.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.documents.audit import record_audit
from backend.documents.models import Clause, Document, EngineRun, Rule, Verdict
from backend.documents.service import source_dir
from backend.engine.parsing import DocumentParseError, parse_document, reviewability
from backend.engine.scorecard import build_scorecard
from backend.engine.scoring import score_rule
from backend.playbooks import get_playbook
from backend.platform_db import platform_session

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("parsing", "scoring")


class ReviewConflict(RuntimeError):
    """Document is already processing or already reviewed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_review(document_id: str, *, trigger: str = "upload", custom_llm=None) -> EngineRun:
    """Run the full review pipeline for one document. Synchronous."""

    session: Session = platform_session()
    run_id = str(uuid.uuid4())
    try:
        document = session.get(Document, document_id)
        if document is None:
            raise ValueError(f"document not found: {document_id}")
        if document.status in ACTIVE_STATUSES:
            raise ReviewConflict(f"document {document.friendly_id} is already processing")
        if document.status in ("awaiting_review", "finalized"):
            raise ReviewConflict(
                f"document {document.friendly_id} already has verdicts"
            )

        playbook = get_playbook(document.playbook_id)
        engine_run = EngineRun(
            id=run_id,
            document_id=document.id,
            playbook_id=playbook.id,
            status="running",
            trigger=trigger,
            stages=[],
        )
        session.add(engine_run)
        document.status = "parsing"
        document.status_reason = None
        record_audit(
            session,
            correlation_id=document.friendly_id,
            event="review.started",
            payload={"run_id": run_id, "trigger": trigger},
        )
        session.commit()

        started = time.perf_counter()
        stages: list[dict] = []

        # —— Stage: parse ——
        stage_start = time.perf_counter()
        try:
            source_files = list(source_dir(document.id).glob("source.*"))
            if not source_files:
                raise DocumentParseError("找不到原始文档文件")
            parsed = parse_document(source_files[0])
            blocked = reviewability(parsed)
            if blocked:
                raise DocumentParseError(blocked)
        except DocumentParseError as exc:
            _fail(session, document, engine_run, "parse", str(exc), stages, started)
            raise
        document.status = "scoring"
        session.query(Clause).filter(Clause.document_id == document.id).delete()
        for clause in parsed.clauses:
            session.add(
                Clause(
                    id=str(uuid.uuid4()),
                    document_id=document.id,
                    ordinal=clause.ordinal,
                    heading=clause.heading,
                    text=clause.text,
                )
            )
        stages.append(
            {
                "stage": "parse",
                "status": "succeeded",
                "latency_ms": int((time.perf_counter() - stage_start) * 1000),
                "clause_count": len(parsed.clauses),
                "method": parsed.parse_method,
            }
        )
        record_audit(
            session,
            correlation_id=document.friendly_id,
            event="review.parsed",
            payload={"clauses": len(parsed.clauses), "method": parsed.parse_method},
        )
        session.commit()

        # —— Stage: score ——
        stage_start = time.perf_counter()
        rules = (
            session.query(Rule)
            .filter(Rule.playbook_id == playbook.id, Rule.active.is_(True))
            .order_by(Rule.ordinal)
            .all()
        )
        if not rules:
            _fail(session, document, engine_run, "score", "规则手册为空", stages, started)
            raise ReviewConflict("规则手册为空")

        from backend.config import get_config

        retrieval_mode = get_config().retrieval_mode
        embedder = None
        if retrieval_mode in ("semantic", "hybrid"):
            from backend.engine.retrieval import get_clause_embedder

            embedder = get_clause_embedder()
            if embedder is None or not embedder.ensure_ready(parsed.clauses):
                embedder = None
                record_audit(
                    session,
                    correlation_id=document.friendly_id,
                    event="review.retrieval_fallback",
                    payload={"requested": retrieval_mode, "fell_back_to": "keyword"},
                )
                retrieval_mode = "keyword"

        verdict_rows: list[dict] = []
        try:
            for rule in rules:
                verdict_rows.append(
                    score_rule(
                        rule,
                        parsed.clauses,
                        parsed.text,
                        playbook_instructions=playbook.scoring_instructions,
                        custom_llm=custom_llm,
                        embedder=embedder,
                        retrieval_mode=retrieval_mode,
                    )
                )
        except Exception as exc:  # LLM infrastructure failure
            logger.exception("scoring failed for %s", document.friendly_id)
            _fail(
                session,
                document,
                engine_run,
                "score",
                f"评分模型调用失败: {exc}",
                stages,
                started,
            )
            raise

        for row in verdict_rows:
            session.add(
                Verdict(
                    id=str(uuid.uuid4()),
                    document_id=document.id,
                    rule_id=row["rule_id"],
                    rating=row["rating"],
                    initial_rating=row["rating"],
                    rationale=row["rationale"],
                    citations=row["citations"],
                    gap_reason=row["gap_reason"],
                    review_state=row["review_state"],
                    confidence=1.0 if row["failure"] is None else 0.4,
                )
            )
        stages.append(
            {
                "stage": "score",
                "status": "succeeded",
                "latency_ms": int((time.perf_counter() - stage_start) * 1000),
                "verdicts": len(verdict_rows),
                "failures": sum(1 for row in verdict_rows if row["failure"]),
                "retrieval_mode": retrieval_mode,
            }
        )
        record_audit(
            session,
            correlation_id=document.friendly_id,
            event="review.scored",
            payload={
                "verdicts": len(verdict_rows),
                "awaiting_review": sum(1 for r in verdict_rows if r["review_state"] == "awaiting_review"),
            },
        )
        session.commit()

        # —— Stage: scorecard ——
        stage_start = time.perf_counter()
        scorecard = build_scorecard(
            verdict_rows,
            playbook_name=playbook.name,
            dimensions=list(playbook.dimensions),
        )
        document.scorecard = scorecard
        document.status = "awaiting_review"
        stages.append(
            {
                "stage": "scorecard",
                "status": "succeeded",
                "latency_ms": int((time.perf_counter() - stage_start) * 1000),
            }
        )
        record_audit(
            session,
            correlation_id=document.friendly_id,
            event="review.scorecard_ready",
            payload={
                "red": scorecard["counts"]["red"],
                "amber": scorecard["counts"]["amber"],
                "green": scorecard["counts"]["green"],
                "risk_index": scorecard["risk_index"],
            },
        )

        from backend.notifications.service import create_notification_and_email as create_notification

        pending = scorecard["counts"]["red"] + scorecard["counts"]["amber"]
        create_notification(
            session,
            type="scored",
            title=f"{document.friendly_id} 评分完成",
            body=(
                f"{document.title}：红 {scorecard['counts']['red']} · "
                f"黄 {scorecard['counts']['amber']} · 绿 {scorecard['counts']['green']}，"
                f"待签字 {pending} 项"
            ),
            payload={"document_id": document.id, "pending": pending},
            correlation_id=document.friendly_id,
        )

        engine_run.status = "succeeded"
        engine_run.stages = stages
        engine_run.total_latency_ms = int((time.perf_counter() - started) * 1000)
        engine_run.finished_at = datetime.now(timezone.utc)
        session.commit()
        return engine_run
    except (ReviewConflict, ValueError):
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _fail(
    session: Session,
    document: Document,
    engine_run: EngineRun,
    stage: str,
    error: str,
    stages: list[dict],
    started: float,
) -> None:
    """Mark run + document as failed with an explicit, auditable reason."""

    stages.append({"stage": stage, "status": "failed", "error": error})
    engine_run.status = "failed"
    engine_run.error = error
    engine_run.stages = stages
    engine_run.total_latency_ms = int((time.perf_counter() - started) * 1000)
    engine_run.finished_at = datetime.now(timezone.utc)
    document.status = "failed"
    document.status_reason = error
    record_audit(
        session,
        correlation_id=document.friendly_id,
        event="review.failed",
        payload={"stage": stage, "error": error},
    )
    from backend.notifications.service import create_notification_and_email as create_notification

    create_notification(
        session,
        type="failed",
        title=f"{document.friendly_id} 处理失败",
        body=error,
        payload={"stage": stage},
        correlation_id=document.friendly_id,
    )
    session.commit()
