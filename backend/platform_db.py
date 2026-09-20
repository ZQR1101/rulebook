"""Platform database for the Rulebook review platform.

A dedicated SQLite database (WAL mode) that is independent of the optional
ENABLE_DB_HISTORY feature. All platform tables (users, documents, rules,
verdicts, audit log, engine runs) live here.

The database path comes from ``PLATFORM_DB_PATH`` (default ``data/rulebook.db``
under the project root). Tests override the env var before the first use and
call :func:`reset_platform_db` between cases.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM_DB_RELPATH = Path("data") / "rulebook.db"
_AUTH_SECRET_FILENAME = "auth_secret.key"


class PlatformBase(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_platform_db_path() -> Path:
    raw = (os.getenv("PLATFORM_DB_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return PROJECT_ROOT / DEFAULT_PLATFORM_DB_RELPATH


def get_auth_secret() -> str:
    """Secret used to sign session tokens.

    Reads ``AUTH_SECRET`` from the environment; otherwise generates one and
    persists it under the platform data directory so sessions survive restarts.
    """

    env_secret = (os.getenv("AUTH_SECRET") or "").strip()
    if env_secret:
        return env_secret

    path = get_platform_db_path().parent / _AUTH_SECRET_FILENAME
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _enable_wal(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_platform_engine() -> Engine:
    global _engine
    if _engine is None:
        db_path = get_platform_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        event.listen(_engine, "connect", _enable_wal)
    return _engine


def get_platform_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_platform_engine(), expire_on_commit=False
        )
    return _session_factory


def platform_session() -> Session:
    """Create a new platform session. Caller is responsible for closing."""

    return get_platform_session_factory()()


def reset_platform_db() -> None:
    """Dispose engine/factory so the next use re-reads PLATFORM_DB_PATH."""

    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_platform_db() -> None:
    """Create tables and seed baseline data. Idempotent."""

    from backend.auth.models import User  # noqa: F401
    from backend.auth.service import hash_password
    from backend.documents.models import (  # noqa: F401
        AuditEntry,
        Clause,
        Document,
        EngineRun,
        ReviewEvent,
        Rule,
        RuleSuggestion,
        Verdict,
    )
    from backend.notifications.models import (  # noqa: F401
        Notification,
        NotificationRead,
    )
    from backend.engine.mail_inbox import MailState  # noqa: F401

    engine = get_platform_engine()
    PlatformBase.metadata.create_all(engine)
    _add_missing_columns(engine)
    _backfill_initial_rating(engine)

    with platform_session() as session:
        _seed_playbook_rules(session)
        _ensure_bootstrap_admin(session)
        session.commit()


# Columns introduced after a table already shipped. ``create_all`` only builds
# missing tables, so existing databases need the explicit ALTER; pre-existing
# rows keep NULL, which analytics report as "unknown baseline".
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("verdicts", "initial_rating", "VARCHAR(10)"),
)


def _add_missing_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, column, ddl in ADDITIVE_COLUMNS:
            if table not in tables:
                continue
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("schema: added %s.%s", table, column)


def _backfill_initial_rating(engine: Engine) -> None:
    """Give legacy verdicts a recoverable AI baseline.

    ``initial_rating`` shipped after thousands of rows existed. If no expert ever
    edited a verdict, the stored rating still *is* the engine's judgement, so the
    baseline can be recovered instead of being reported as unknown forever; an
    edited verdict's original grade is genuinely lost and stays NULL.
    """

    from sqlalchemy import inspect, text

    tables = set(inspect(engine).get_table_names())
    if not {"verdicts", "review_events"} <= tables:
        return
    with engine.begin() as connection:
        result = connection.execute(
            text(
                """
                UPDATE verdicts SET initial_rating = rating
                WHERE initial_rating IS NULL
                  AND id NOT IN (
                      SELECT verdict_id FROM review_events
                      WHERE action = 'edit' AND verdict_id IS NOT NULL
                  )
                """
            )
        )
        if result.rowcount:
            logger.info("schema: recovered AI baseline on %d legacy verdicts", result.rowcount)


def _ensure_bootstrap_admin(session: Session) -> None:
    from backend.auth.models import User
    from backend.auth.service import hash_password

    user_count = session.query(User).count()
    if user_count > 0:
        return

    env_password = (os.getenv("AUTH_ADMIN_PASSWORD") or "").strip()
    if env_password:
        password = env_password
        source = "env AUTH_ADMIN_PASSWORD"
    else:
        password = secrets.token_urlsafe(12)
        source = "generated"

    admin = User(
        username="admin",
        display_name="管理员",
        role="admin",
        password_hash=hash_password(password),
    )
    session.add(admin)
    session.commit()

    if not env_password:
        path = get_platform_db_path().parent / "bootstrap_admin_password.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"username: admin\npassword: {password}\n",
            encoding="utf-8",
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    logger.warning(
        "Platform bootstrap: created admin user 'admin' (password %s). "
        "Password file: %s",
        source,
        get_platform_db_path().parent / "bootstrap_admin_password.txt",
    )


def seed_playbook_rules(session: Session, playbook_id: str) -> None:
    """Insert rulebook seed data for a playbook if it has none yet.

    Public so newly registered playbooks (tests, new verticals) seed lazily
    instead of only at startup.
    """

    from backend.documents.models import Rule
    from backend.playbooks.registry import get_playbook

    spec = get_playbook(playbook_id)
    existing = session.query(Rule).filter(Rule.playbook_id == playbook_id).count()
    if existing > 0:
        return
    for ordinal, seed in enumerate(spec.rule_seeds, start=1):
        session.add(
            Rule(
                playbook_id=playbook_id,
                dimension=seed["dimension"],
                name=seed["name"],
                guidance=seed["guidance"],
                weight=seed.get("weight", 1),
                active=True,
                ordinal=ordinal,
            )
        )
    session.commit()


def _seed_playbook_rules(session: Session) -> None:
    """Load playbook rulebook seed data into the database on first run."""

    from backend.playbooks.registry import list_playbook_ids

    for playbook_id in list_playbook_ids():
        seed_playbook_rules(session, playbook_id)
