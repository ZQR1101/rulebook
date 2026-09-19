"""ORM models for the Rulebook review platform."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.platform_db import PlatformBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(PlatformBase):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    friendly_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    playbook_id: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str]
    source_filename: Mapped[str]
    source_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # uploaded | parsing | scoring | awaiting_review | finalized | failed
    status: Mapped[str] = mapped_column(String(30), default="uploaded", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scorecard: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    clauses: Mapped[list[Clause]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="Clause.ordinal",
    )
    verdicts: Mapped[list[Verdict]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )


class Clause(PlatformBase):
    __tablename__ = "clauses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    heading: Mapped[str | None] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="clauses")


class Rule(PlatformBase):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    playbook_id: Mapped[str] = mapped_column(String(50), index=True)
    dimension: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    guidance: Mapped[str] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(default=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Verdict(PlatformBase):
    __tablename__ = "verdicts"
    __table_args__ = (
        UniqueConstraint("document_id", "rule_id", name="uq_verdict_doc_rule"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"), index=True)
    clause_id: Mapped[str | None] = mapped_column(
        ForeignKey("clauses.id", ondelete="SET NULL"), nullable=True
    )
    # red | amber | green
    rating: Mapped[str] = mapped_column(String(10))
    # The engine's first judgement, never touched by expert edits. Null for
    # verdicts scored before this column existed → "unknown baseline" bucket.
    initial_rating: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    gap_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # drafted | awaiting_review | approved | edited | rejected
    review_state: Mapped[str] = mapped_column(String(30), default="drafted", index=True)
    expert_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    document: Mapped[Document] = relationship(back_populates="verdicts")
    rule: Mapped[Rule] = relationship()


class ReviewEvent(PlatformBase):
    __tablename__ = "review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    verdict_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str]
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEntry(PlatformBase):
    """Append-only audit trail. Only inserts and reads are allowed by convention."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_correlation_created", "correlation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(40), index=True)
    event: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(50), default="system")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuleSuggestion(PlatformBase):
    """LLM-drafted rulebook addition; takes effect only after admin acceptance."""

    __tablename__ = "rule_suggestions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    playbook_id: Mapped[str] = mapped_column(String(50), index=True)
    dimension: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    guidance: Mapped[str] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    rationale: Mapped[str] = mapped_column(Text, default="")
    source_document_id: Mapped[str] = mapped_column(String(36), index=True)
    # proposed | accepted | dismissed
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EngineRun(PlatformBase):
    __tablename__ = "engine_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    playbook_id: Mapped[str] = mapped_column(String(50))
    # running | succeeded | failed
    status: Mapped[str] = mapped_column(String(20), default="running")
    trigger: Mapped[str] = mapped_column(String(30), default="upload")
    stages: Mapped[list] = mapped_column(JSON, default=list)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
