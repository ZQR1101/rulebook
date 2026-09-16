from pathlib import Path
import asyncio
import codecs
import hashlib
import http.client
import ipaddress
import logging
import os
import secrets
import socket
from datetime import datetime, timezone
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import ParseResult, quote, urljoin, urlparse
from typing import Any, Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import get_config
from backend.database import get_database_url, get_db_session, init_db, is_db_history_enabled
from backend.evaluation_store import (
    list_recent_judge_results,
    save_judge_result,
    update_judge_feedback,
)
from backend.judge_service import is_judge_persistence_enabled, is_llm_judge_enabled, judge_answer
from backend.schemas import ChatRequest, ChatResponse, JudgeFeedbackRequest
from backend.run_metadata import build_run_metadata
from backend.run_repository import Run, get_run_repository
from backend.pending_actions import (
    PendingAction,
    get_pending_action_repository,
)
from backend.pdf_validation import (
    PDFPageLimitExceeded,
    PDFValidationError,
    PDFValidationTimeout,
    validate_pdf_file,
)
from backend.ocr_service import extract_text_from_document, safe_document_parse_result
from backend.resource_limits import (
    ConcurrencyGate,
    TokenBucketRateLimiter,
    UploadBodyLimitMiddleware,
    UploadQuotaReservations,
)
from backend.session_store import (
    create_or_get_session,
    get_recent_messages,
    get_session_messages,
    list_sessions,
    save_message,
)
from backend.tool_registry import InvalidConfirmation, ToolConfirmationRequired
from backend.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)


DOCS_PATH = get_config().docs_path
SUPPORTED_DOC_EXTENSIONS = {".md", ".txt", ".pdf"}
IMAGE_PROXY_MAX_REDIRECTS = 3
IMAGE_PROXY_MAX_RESOLVED_ADDRESSES = 8
UPLOAD_CHUNK_SIZE = 64 * 1024
UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024
ALLOWED_UPLOAD_CONTENT_TYPES = {
    ".pdf": {"application/pdf"},
    ".md": {"text/markdown", "text/plain"},
    ".txt": {"text/plain"},
}
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_UPLOAD_CONCURRENCY_GATE = ConcurrencyGate()
_UPLOAD_RATE_LIMITER = TokenBucketRateLimiter()
_UPLOAD_QUOTA = UploadQuotaReservations()
_IMAGE_PROXY_CONCURRENCY_GATE = ConcurrencyGate()
_IMAGE_PROXY_RATE_LIMITER = TokenBucketRateLimiter()

app = FastAPI(
    title="AI 学习助手 API",
    openapi_tags=[
        {"name": "Chat", "description": "统一聊天与 Agent 入口。"},
        {"name": "Tools", "description": "Tool Registry 查询、调用和审计。"},
        {"name": "Runs", "description": "统一执行记录，供历史、回放、比较和导出使用。"},
        {"name": "Knowledge Base", "description": "知识文件与 RAG 状态管理。"},
        {"name": "Sessions", "description": "会话历史。"},
        {"name": "Evaluation", "description": "LLM-as-Judge 结果与反馈。"},
        {"name": "System", "description": "服务状态。"},
    ],
)


app.add_middleware(
    UploadBodyLimitMiddleware,
    config_provider=lambda: get_config(),
    concurrency_gate=_UPLOAD_CONCURRENCY_GATE,
    rate_limiter=_UPLOAD_RATE_LIMITER,
    multipart_overhead_bytes=UPLOAD_MULTIPART_OVERHEAD_BYTES,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_config().cors_allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-Requested-With",
        "X-Tool-Approval-Key",
        "X-Tool-Approver-Key",
    ],
)

from backend.auth.routes import router as auth_router  # noqa: E402
from backend.documents.routes import router as documents_router  # noqa: E402
from backend.documents.review_routes import router as document_review_router  # noqa: E402
from backend.documents.rules_routes import router as rules_router  # noqa: E402
from backend.documents.suggestions_routes import router as suggestions_router  # noqa: E402
from backend.notifications.routes import router as notifications_router  # noqa: E402

app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(document_review_router)
app.include_router(rules_router)
app.include_router(suggestions_router)
app.include_router(notifications_router)


@app.on_event("startup")
def _startup_init_db():
    """Initialize database tables on startup when a database is configured."""
    if get_database_url() and (is_db_history_enabled() or is_judge_persistence_enabled()):
        try:
            init_db()
            logger.info("Database tables initialized")
        except Exception as exc:
            logger.warning("Failed to initialize database: %s", exc)

    try:
        from backend.platform_db import init_platform_db

        init_platform_db()
        logger.info("Platform database initialized")
    except Exception as exc:
        logger.warning("Failed to initialize platform database: %s", exc)

    config = get_config()
    if config.enable_rag_warmup:
        from backend.rag_warmup import start_rag_warmup

        result = start_rag_warmup(load_index=config.rag_warmup_load_index)
        logger.info("RAG warmup startup trigger: started=%s", result["started"])


class TextRequest(BaseModel):
    text: str


class RagRequest(BaseModel):
    text: str
    top_k: int = 3
    retrieval_mode: Literal["vector", "bm25", "hybrid"] = "vector"
    reranker_enabled: bool = False


class DebugRagRequest(BaseModel):
    text: str
    top_k: int = 5
    retrieval_mode: Literal["vector", "bm25", "hybrid"] = "vector"
    reranker_enabled: bool = False


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmation_token: str | None = None
    actor: str = "api"


class PendingActionDecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _resolve_public_image_url(url: str) -> tuple[ParseResult, tuple[str, ...]] | None:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None

    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None

    resolved_addresses: list[str] = []
    for item in addresses:
        host = item[4][0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not address.is_global:
            return None
        normalized_host = str(address)
        if normalized_host not in resolved_addresses:
            resolved_addresses.append(normalized_host)

    if not resolved_addresses:
        return None

    return parsed, tuple(resolved_addresses[:IMAGE_PROXY_MAX_RESOLVED_ADDRESSES])


def _is_public_image_url(url: str) -> bool:
    return _resolve_public_image_url(url) is not None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = self._create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = self._create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedImageResponse:
    def __init__(self, connection, response):
        self._connection = connection
        self._response = response
        self.headers = response.headers
        self.status = response.status

    def read(self, amount: int = -1):
        return self._response.read(amount)

    def set_timeout(self, timeout: float) -> None:
        if self._connection.sock is not None:
            self._connection.sock.settimeout(timeout)

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _image_request_target(parsed: ParseResult) -> str:
    target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        encoded_query = quote(parsed.query, safe="=&%:@!$'()*+,;/?-._~")
        target = f"{target}?{encoded_query}"
    return target


def _image_host_header(hostname: str, port: int, scheme: str) -> str:
    try:
        is_ipv6 = ipaddress.ip_address(hostname).version == 6
    except ValueError:
        is_ipv6 = False
    host = f"[{hostname}]" if is_ipv6 else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _open_pinned_image_response(
    parsed: ParseResult,
    pinned_ip: str,
    *,
    timeout: float,
):
    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_class = (
        _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    )
    connection = connection_class(hostname, pinned_ip, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            _image_request_target(parsed),
            headers={
                "Host": _image_host_header(hostname, port, parsed.scheme),
                "User-Agent": "AI-Study-Assistant/1.0",
                "Connection": "close",
            },
        )
        return _PinnedImageResponse(connection, connection.getresponse())
    except Exception:
        connection.close()
        raise


def _open_public_image(url: str, *, deadline: float | None = None):
    deadline = deadline or (perf_counter() + 20)
    current_url = url

    for redirect_count in range(IMAGE_PROXY_MAX_REDIRECTS + 1):
        resolved = _resolve_public_image_url(current_url)
        if resolved is None:
            raise HTTPException(status_code=400, detail="Unsupported image URL")

        parsed, addresses = resolved
        last_error: OSError | None = None
        response = None
        for pinned_ip in addresses:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise URLError("Image fetch timed out")
            try:
                response = _open_pinned_image_response(
                    parsed,
                    pinned_ip,
                    timeout=remaining,
                )
                break
            except OSError as exc:
                last_error = exc

        if response is None:
            raise URLError(last_error or "Unable to connect to image host")

        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location") if response.headers else None
            response.close()
            if not location or redirect_count >= IMAGE_PROXY_MAX_REDIRECTS:
                raise HTTPException(
                    status_code=400,
                    detail="Image redirect is invalid or exceeds the redirect limit",
                )
            current_url = urljoin(current_url, location)
            continue

        if response.status >= 400:
            status = response.status
            headers = response.headers
            response.close()
            raise HTTPError(current_url, status, "Image fetch failed", headers, None)

        return response

    raise HTTPException(status_code=400, detail="Image redirect limit exceeded")


def _is_safe_proxy_image(content_type: str, content: bytes) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type in {"image/jpeg", "image/pjpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
        )
    return False


def _safe_doc_path(filename: str) -> Path:
    clean_name = Path(filename).name

    if clean_name != filename:
        raise HTTPException(status_code=400, detail="Invalid file name")

    file_path = (DOCS_PATH / clean_name).resolve()
    docs_root = DOCS_PATH.resolve()

    if docs_root not in file_path.parents or file_path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return file_path


_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", tags=["System"])
def home():
    index_file = _STATIC_DIR / "index.html"
    if index_file.is_file():
        # index.html references hashed assets; never cache it, or users keep
        # running a stale bundle after a rebuild.
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
    return {
        "message": "Rulebook 后端启动成功",
        "hint": (
            "Frontend static files are not bundled here. Run `npm run build` to emit "
            "backend/static, or start the Vite dev server with `npm run dev`."
        ),
    }


@app.get("/health", tags=["System"])
def health_check():
    from backend.rag_store import get_rag_index_status
    from backend.rag_warmup import get_rag_warmup_status

    config = get_config()
    rag_warmup_status = get_rag_warmup_status()
    return {
        "status": "ok",
        "config": {
            "model": config.model,
            "base_url": config.base_url,
            "has_api_key": config.has_api_key,
            "api_key_source": config.api_key_source,
            "embedding_model": config.embedding_model,
            "embedding_model_local_only": config.embedding_model_local_only,
            "rag_auto_build_enabled": config.enable_rag_auto_build,
        },
        "rag_index": get_rag_index_status(),
        "rag_warmup": {
            "enabled": config.enable_rag_warmup,
            "status": rag_warmup_status["status"],
        },
        "db_history_enabled": is_db_history_enabled(),
        "database_configured": get_database_url() is not None,
        "llm_judge_enabled": is_llm_judge_enabled(),
        "judge_persistence_enabled": is_judge_persistence_enabled(),
    }


@app.get("/tools", tags=["Tools"])
def list_tools_api():
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "category": spec.category.value,
                "requires_confirmation": spec.requires_confirmation,
                "agent_visible": spec.agent_visible,
            }
            for spec in TOOL_REGISTRY.values()
        ]
    }


def _dangerous_tool_keys(tool_name: str) -> tuple[str, str] | None:
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None or not spec.requires_confirmation:
        return None

    config = get_config()
    request_key = config.tool_approval_key
    approver_key = config.tool_approver_key
    if not request_key or not approver_key:
        raise HTTPException(
            status_code=503,
            detail="Dangerous tool requester and approver keys are not configured",
        )
    if secrets.compare_digest(request_key, approver_key):
        raise HTTPException(
            status_code=503,
            detail="Dangerous tool requester and approver keys must be different",
        )
    return request_key, approver_key


def _key_identity(role: str, key: str) -> str:
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{role}:{fingerprint}"


def _require_tool_requester(tool_name: str, approval_key: str | None) -> str | None:
    configured_keys = _dangerous_tool_keys(tool_name)
    if configured_keys is None:
        return None
    request_key, _ = configured_keys
    if not approval_key or not secrets.compare_digest(approval_key, request_key):
        raise HTTPException(status_code=403, detail="Dangerous tool approval denied")
    return _key_identity("requester", request_key)


def _require_tool_approver(tool_name: str, approver_key: str | None) -> str:
    configured_keys = _dangerous_tool_keys(tool_name)
    if configured_keys is None:
        raise HTTPException(status_code=400, detail="Tool does not require approval")
    _, configured_approver_key = configured_keys
    if not approver_key or not secrets.compare_digest(
        approver_key,
        configured_approver_key,
    ):
        raise HTTPException(status_code=403, detail="Dangerous tool approval denied")
    return _key_identity("approver", configured_approver_key)


@app.post(
    "/tools/{tool_name}/invoke",
    tags=["Tools"],
    responses={
        409: {"description": "Dangerous tool requires confirmation"},
        400: {"description": "Confirmation token is invalid or expired"},
        403: {"description": "Dangerous tool approval denied"},
        404: {"description": "Tool not found"},
        503: {"description": "Dangerous tool approval is not configured"},
    },
)
def invoke_tool_api(
    tool_name: str,
    request: ToolInvokeRequest,
    approval_key: str | None = Header(default=None, alias="X-Tool-Approval-Key"),
):
    arguments = dict(request.arguments)
    for reserved in ("actor", "confirmed", "confirmation_token"):
        arguments.pop(reserved, None)
    confirmation_subject = _require_tool_requester(tool_name, approval_key)
    try:
        return TOOL_REGISTRY.execute(
            tool_name,
            confirmation_token=request.confirmation_token,
            actor=confirmation_subject or request.actor,
            confirmation_subject=confirmation_subject,
            **arguments,
        )
    except ToolConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
    except InvalidConfirmation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/tools/{tool_name}/approvals/{approval_request_id}",
    tags=["Tools"],
    responses={
        400: {"description": "Approval request is invalid or expired"},
        403: {"description": "Dangerous tool approval denied"},
        404: {"description": "Tool not found"},
        503: {"description": "Dangerous tool approval is not configured safely"},
    },
)
def approve_tool_api(
    tool_name: str,
    approval_request_id: str,
    approver_key: str | None = Header(default=None, alias="X-Tool-Approver-Key"),
):
    approver = _require_tool_approver(tool_name, approver_key)
    try:
        token = TOOL_REGISTRY.approve_confirmation(
            approval_request_id,
            tool_name,
            approver=approver,
        )
    except InvalidConfirmation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "confirmation_token": token,
        "expires_in_seconds": TOOL_REGISTRY.confirmations.ttl_seconds,
    }


@app.get("/tools/audit/recent", tags=["Tools"])
def recent_tool_audit_api(limit: int = Query(100, ge=1, le=1000)):
    return {"events": TOOL_REGISTRY.audit_log.recent(limit)}


def _run_payload(run: Run) -> dict:
    return run.model_dump(mode="json") if hasattr(run, "model_dump") else run.dict()


def _finish_status_from_summary(run_id: str, summary_status: str | None) -> str:
    if summary_status in {"succeeded", "completed"}:
        return "completed"
    if summary_status in {"failed", "partial"}:
        return str(summary_status)
    logger.warning(
        "Unknown run summary status %r for run %s; marking it partial",
        summary_status,
        run_id,
    )
    return "partial"


def _best_effort_mark_run_failed(run_repository, run_id: str, error: BaseException) -> bool:
    try:
        run_repository.finish_run(run_id, status="failed", error=str(error))
        return True
    except Exception as fallback_error:
        logger.error(
            "Unable to mark run %s failed after %s: %s",
            run_id,
            error,
            fallback_error,
            exc_info=True,
        )
        return False


@app.get("/runs", tags=["Runs"])
def list_runs_api(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = None,
    session_id: str | None = None,
    include_deleted: bool = False,
):
    runs = get_run_repository().list_runs(
        limit=limit,
        status=status,
        session_id=session_id,
        include_deleted=include_deleted,
    )
    return {"runs": [_run_payload(run) for run in runs], "count": len(runs)}


@app.get("/runs/{run_id}", tags=["Runs"])
def get_run_api(run_id: str):
    try:
        run = get_run_repository().get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_payload(run)


def _pending_action_payload(action: PendingAction) -> dict:
    if hasattr(action, "model_dump"):
        return action.model_dump(mode="json")
    return action.dict()


def _require_in_app_action_request(x_requested_with: str | None) -> None:
    if x_requested_with != "AI-Study-Assistant":
        raise HTTPException(status_code=403, detail="Pending action request source denied")


def _finalize_action_run(
    action: PendingAction,
    *,
    status: Literal["completed", "partial", "failed"],
    event_status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    run_repository = get_run_repository()
    run = run_repository.get_run(action.run_id)
    if run is None or run.status != "awaiting_action":
        return
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "pending_action",
        "action_id": action.id,
        "tool": action.tool_name,
        "status": event_status,
        "actor": "user",
        "run_id": action.run_id,
    }
    try:
        run_repository.append_audit(action.run_id, event)
        run_repository.finish_run(
            action.run_id,
            status=status,
            output={
                "pending_actions": [_pending_action_payload(action)],
                **({"pending_action_result": result} if result is not None else {}),
            },
            error=error,
        )
    except (KeyError, ValueError):
        logger.warning("Unable to finalize Run %s for Pending Action %s", action.run_id, action.id)


@app.get("/pending-actions", tags=["Tools"])
def list_pending_actions_api(
    status: str | None = None,
    run_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    actions = get_pending_action_repository().list(
        status=status,
        run_id=run_id,
        limit=limit,
    )
    for action in actions:
        if action.status == "expired":
            _finalize_action_run(action, status="partial", event_status="expired")
    return {
        "actions": [_pending_action_payload(action) for action in actions],
        "count": len(actions),
    }


@app.get("/pending-actions/{action_id}", tags=["Tools"])
def get_pending_action_api(action_id: str):
    try:
        action = get_pending_action_repository().get(action_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if action is None:
        raise HTTPException(status_code=404, detail="Pending action not found")
    if action.status == "expired":
        _finalize_action_run(action, status="partial", event_status="expired")
    return _pending_action_payload(action)


@app.post("/pending-actions/{action_id}/approve", tags=["Tools"])
def approve_pending_action_api(
    action_id: str,
    request: PendingActionDecisionRequest,
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    _require_in_app_action_request(x_requested_with)
    repository = get_pending_action_repository()
    try:
        action = repository.get(action_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if action is None:
        raise HTTPException(status_code=404, detail="Pending action not found")
    if action.status == "expired":
        _finalize_action_run(action, status="partial", event_status="expired")
        raise HTTPException(status_code=409, detail="Pending action expired")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Pending action is already {action.status}")

    now = datetime.now(timezone.utc).isoformat()
    try:
        action = repository.transition(
            action.id,
            expected={"pending"},
            status="approved",
            decision_reason=request.reason,
            decided_at=now,
        )
        action = repository.transition(
            action.id,
            expected={"approved"},
            status="executing",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    spec = TOOL_REGISTRY.get(action.tool_name)
    if spec is None or not spec.requires_confirmation or spec.category.value != "dangerous":
        failed = repository.transition(
            action.id,
            expected={"executing"},
            status="failed",
            error="Pending action tool is no longer available as a dangerous tool",
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        _finalize_action_run(
            failed,
            status="failed",
            event_status="failed",
            error=failed.error,
        )
        raise HTTPException(status_code=409, detail=failed.error)

    subject = f"pending-action:{action.id}"
    try:
        try:
            TOOL_REGISTRY.execute(
                action.tool_name,
                actor=subject,
                confirmation_subject=subject,
                **action.arguments,
            )
        except ToolConfirmationRequired as confirmation:
            token = TOOL_REGISTRY.approve_confirmation(
                confirmation.request_id,
                action.tool_name,
                approver="user:pending-action-card",
            )
        else:
            raise RuntimeError("Dangerous tool executed without confirmation")

        result = TOOL_REGISTRY.execute(
            action.tool_name,
            actor=subject,
            confirmation_subject=subject,
            confirmation_token=token,
            **action.arguments,
        )
    except Exception as exc:
        failed = repository.transition(
            action.id,
            expected={"executing"},
            status="failed",
            error=str(exc),
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        _finalize_action_run(
            failed,
            status="failed",
            event_status="failed",
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=f"Pending action execution failed: {exc}") from exc

    executed = repository.transition(
        action.id,
        expected={"executing"},
        status="executed",
        result=result,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )
    _finalize_action_run(
        executed,
        status="completed",
        event_status="executed",
        result=result,
    )
    return {"action": _pending_action_payload(executed), "result": result}


@app.post("/pending-actions/{action_id}/reject", tags=["Tools"])
def reject_pending_action_api(
    action_id: str,
    request: PendingActionDecisionRequest,
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    _require_in_app_action_request(x_requested_with)
    repository = get_pending_action_repository()
    try:
        rejected = repository.transition(
            action_id,
            expected={"pending"},
            status="rejected",
            decision_reason=request.reason,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Pending action not found") from exc
    except ValueError as exc:
        action = repository.get(action_id)
        if action and action.status == "expired":
            _finalize_action_run(action, status="partial", event_status="expired")
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _finalize_action_run(rejected, status="completed", event_status="rejected")
    return {"action": _pending_action_payload(rejected)}


@app.get("/rag/status", tags=["Knowledge Base"])
def rag_status_api():
    from backend.rag_warmup import get_rag_warmup_status

    return get_rag_warmup_status()


@app.post("/rag/warmup", tags=["Knowledge Base"])
def rag_warmup_api():
    from backend.rag_warmup import start_rag_warmup

    config = get_config()
    return start_rag_warmup(load_index=config.rag_warmup_load_index)


@app.post("/echo", include_in_schema=False)
def echo_api(request: TextRequest):
    return {"echo": request.text}


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat_api(request: ChatRequest):
    from backend.ai_core import run_chat_request

    request_started_at = perf_counter()
    session_id = request.session_id
    db_history_error = None
    judge_error = None
    judge_persistence_error = None
    run_repository = get_run_repository()

    # --- DB history: load context ---
    if is_db_history_enabled():
        try:
            with get_db_session() as db:
                session_id = create_or_get_session(
                    db, request.session_id, title=request.message
                )
                db_messages = get_recent_messages(db, session_id, limit=10)
                if db_messages and not request.history:
                    request = request.model_copy(update={"history": db_messages, "session_id": session_id})
                elif not request.history:
                    request = request.model_copy(update={"session_id": session_id})
                else:
                    request = request.model_copy(update={"session_id": session_id})
        except Exception as exc:
            db_history_error = f"db_load_error: {exc}"
            logger.warning("DB history load failed: %s", exc)

    request_payload = (
        request.model_dump(mode="json", exclude={"run_id"})
        if hasattr(request, "model_dump")
        else request.dict(exclude={"run_id"})
    )
    run = run_repository.create_run(
        request=request_payload,
        session_id=session_id,
        metadata={"entrypoint": "/chat"},
    )
    run_id = run.id
    request = request.model_copy(update={"run_id": run_id, "session_id": session_id})

    # --- Execute chat ---
    try:
        result = run_chat_request(request)
    except Exception as exc:
        _best_effort_mark_run_failed(run_repository, run_id, exc)
        raise
    result["run_id"] = run_id

    # --- LLM-as-Judge evaluation ---
    if (
        is_llm_judge_enabled()
        and result.get("answer")
        and not result.get("pending_actions")
    ):
        try:
            evaluation = judge_answer(
                request.message,
                result["answer"],
                sources=result.get("sources", []),
                trace=result.get("trace", []),
                runtime_info=result.get("runtime_info", {}),
                model=result.get("model") or request.model,
            )
            result["judge_evaluation"] = {
                **evaluation,
                "session_id": session_id,
                "run_id": run_id,
                "question": request.message,
                "answer": result["answer"],
            }
        except Exception as exc:
            judge_error = f"judge_error: {exc}"
            logger.warning("LLM judge evaluation failed: %s", exc)

    # --- Judge evaluation persistence ---
    if result.get("judge_evaluation") and get_database_url() and is_judge_persistence_enabled():
        try:
            with get_db_session() as db:
                persisted_evaluation = save_judge_result(
                    db,
                    session_id=session_id,
                    question=request.message,
                    answer=result["answer"],
                    evaluation=result["judge_evaluation"],
                    run_id=run_id,
                )
                result["judge_evaluation"] = {
                    **persisted_evaluation,
                    "run_id": run_id,
                }
        except Exception as exc:
            judge_persistence_error = f"judge_db_save_error: {exc}"
            logger.warning("Judge evaluation save failed: %s", exc)

    # --- Attach session_id and db error to response ---
    if session_id:
        result["session_id"] = session_id
    if db_history_error:
        runtime_info = result.get("runtime_info", {})
        runtime_info["db_history_error"] = db_history_error
        result["runtime_info"] = runtime_info
    if judge_error or judge_persistence_error:
        runtime_info = result.get("runtime_info", {})
        if judge_error:
            runtime_info["judge_error"] = judge_error
        if judge_persistence_error:
            runtime_info["judge_persistence_error"] = judge_persistence_error
        result["runtime_info"] = runtime_info

    run_summary, run_details = build_run_metadata(
        result,
        duration_ms=max(0, round((perf_counter() - request_started_at) * 1000)),
    )
    result["run_summary"] = run_summary
    result["run_details"] = run_details
    if result.get("pending_actions"):
        result["run_summary"] = {**run_summary, "status": "awaiting_action"}

    # --- DB history: save messages and the complete assistant response ---
    if is_db_history_enabled() and session_id:
        try:
            response_snapshot = ChatResponse.model_validate(result).model_dump(mode="json")
            with get_db_session() as db:
                save_message(db, session_id, "user", request.message)
                if result.get("answer"):
                    save_message(
                        db,
                        session_id,
                        "assistant",
                        result["answer"],
                        response=response_snapshot,
                    )
        except Exception as exc:
            db_history_error = f"db_save_error: {exc}"
            runtime_info = dict(result.get("runtime_info", {}))
            runtime_info["db_history_error"] = db_history_error
            result["runtime_info"] = runtime_info
            logger.warning("DB history save failed: %s", exc)

    try:
        run_repository.update_run(
            run_id,
            session_id=session_id,
            plan=result.get("plan", []),
            tools=result.get("runtime_info", {}).get("tool_calls", []),
            artifacts={
                "answer": result.get("answer", ""),
                "sources": result.get("sources", []),
                "flashcards": result.get("flashcards", []),
                "trace": result.get("trace", []),
                "pending_actions": result.get("pending_actions", []),
            },
            metadata={"run_summary": result.get("run_summary", {})},
        )
        response_payload = ChatResponse.model_validate(result).model_dump(mode="json")
        if result.get("pending_actions"):
            run_repository.update_run(
                run_id,
                status="awaiting_action",
                output=response_payload,
                error=None,
                finished_at=None,
            )
        else:
            finish_status = _finish_status_from_summary(
                run_id,
                run_summary.get("status"),
            )
            run_repository.finish_run(
                run_id,
                status=finish_status,
                output=response_payload,
                error=result.get("runtime_info", {}).get("error"),
            )
    except Exception as exc:
        logger.warning("Failed to finalize run %s: %s", run_id, exc)
        runtime_info = dict(result.get("runtime_info", {}))
        runtime_info["run_persistence_error"] = str(exc)
        result["runtime_info"] = runtime_info
        _best_effort_mark_run_failed(run_repository, run_id, exc)
        return result

    return result


def _recent_judge_results_payload(limit: int) -> dict:
    """Return recent LLM-as-Judge scores, newest first."""
    if not get_database_url():
        return {
            "results": [],
            "evaluations": [],
            "message": "Database is not configured; judge results are not persisted",
        }
    if not is_judge_persistence_enabled():
        return {
            "results": [],
            "evaluations": [],
            "message": "Judge persistence is disabled (ENABLE_JUDGE_PERSISTENCE=false)",
        }

    try:
        with get_db_session() as db:
            results = list_recent_judge_results(db, limit=limit)
        return {"results": results, "evaluations": results}
    except Exception as exc:
        logger.warning("Failed to list judge results: %s", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/judge-results/recent", tags=["Evaluation"])
def recent_judge_results_api(limit: int = Query(20, ge=1, le=100)):
    """Return recent LLM-as-Judge results, newest first."""
    return _recent_judge_results_payload(limit)


@app.get("/judge-evaluations/recent", include_in_schema=False, deprecated=True)
def recent_judge_evaluations_api(limit: int = Query(20, ge=1, le=100)):
    """Backward-compatible alias for recent judge results."""
    return _recent_judge_results_payload(limit)


@app.post("/judge-results/{result_id}/feedback", tags=["Evaluation"])
def judge_result_feedback_api(result_id: int, request: JudgeFeedbackRequest):
    """Persist human feedback about whether a judge result was reasonable."""
    if not get_database_url():
        raise HTTPException(status_code=503, detail="Database is not configured")
    if not is_judge_persistence_enabled():
        raise HTTPException(status_code=503, detail="Judge persistence is disabled")

    try:
        with get_db_session() as db:
            updated = update_judge_feedback(
                db,
                result_id=result_id,
                judge_feedback=request.judge_feedback,
                reason=request.reason,
            )
        if updated is None:
            raise HTTPException(status_code=404, detail="Judge result not found")
        return {"result": updated}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to save judge feedback: %s", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/image-proxy", include_in_schema=False)
def image_proxy(request: Request, url: str = Query(..., min_length=1)):
    config = get_config()
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site image proxy request denied")

    source_origin = request.headers.get("origin", "").rstrip("/")
    referer = request.headers.get("referer", "")
    if not source_origin and referer:
        parsed_referer = urlparse(referer)
        if parsed_referer.scheme and parsed_referer.netloc:
            source_origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
        else:
            source_origin = "invalid"
    if source_origin and source_origin not in set(config.cors_allowed_origins):
        raise HTTPException(status_code=403, detail="Image proxy request source denied")

    client_host = request.client.host if request.client else "unknown"
    allowed, retry_after = _IMAGE_PROXY_RATE_LIMITER.allow(
        client_host,
        getattr(config, "image_proxy_rate_limit", 30),
        getattr(config, "image_proxy_rate_window_seconds", 60),
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Image proxy rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    if not _IMAGE_PROXY_CONCURRENCY_GATE.try_acquire(
        getattr(config, "image_proxy_max_concurrency", 4)
    ):
        raise HTTPException(
            status_code=503,
            detail="Image proxy capacity is busy; retry later",
            headers={"Retry-After": "1"},
        )

    try:
        max_response_bytes = getattr(
            config,
            "image_proxy_max_response_bytes",
            20 * 1024 * 1024,
        )
        deadline = perf_counter() + getattr(
            config,
            "image_proxy_timeout_seconds",
            20,
        )
        try:
            with _open_public_image(url, deadline=deadline) as response:
                content_type = response.headers.get("content-type", "image/png").split(
                    ";"
                )[0].strip()
                chunks = []
                total_size = 0
                while total_size <= max_response_bytes:
                    remaining_timeout = deadline - perf_counter()
                    if remaining_timeout <= 0:
                        raise HTTPException(status_code=504, detail="Image fetch timed out")
                    if hasattr(response, "set_timeout"):
                        response.set_timeout(remaining_timeout)
                    chunk = response.read(
                        min(UPLOAD_CHUNK_SIZE, max_response_bytes + 1 - total_size)
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total_size += len(chunk)
                content = b"".join(chunks)
        except HTTPException:
            raise
        except HTTPError as exc:
            raise HTTPException(status_code=exc.code, detail="Image fetch failed") from exc
        except URLError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Image fetch failed: {exc.reason}",
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Image fetch timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise HTTPException(status_code=502, detail="Image fetch failed") from exc

        if len(content) > max_response_bytes:
            raise HTTPException(status_code=413, detail="Image is too large")
        if not _is_safe_proxy_image(content_type, content):
            raise HTTPException(
                status_code=400,
                detail="URL did not return a supported image",
            )

        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )
    finally:
        _IMAGE_PROXY_CONCURRENCY_GATE.release()


@app.post("/debug-langgraph", include_in_schema=False)
def debug_langgraph(request: ChatRequest):
    try:
        from backend.langgraph_runtime import LangGraphRuntimeUnavailableError, run_langgraph_workflow

        return run_langgraph_workflow(
            request.message,
            top_k=request.top_k,
            retrieval_mode=request.retrieval_mode,
            reranker_enabled=request.reranker_enabled,
            use_rag=request.use_rag,
            model=request.model,
            temperature=request.temperature,
            history=request.history,
        )
    except LangGraphRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/explain", include_in_schema=False, deprecated=True)
def explain_api(request: TextRequest):
    result = TOOL_REGISTRY.execute(
        "study", step_input=request.text, operation="explain", actor="api"
    )
    return {"result": result["answer"]}


@app.post("/summarize", include_in_schema=False, deprecated=True)
def summarize_api(request: TextRequest):
    result = TOOL_REGISTRY.execute(
        "study", step_input=request.text, operation="summarize", actor="api"
    )
    return {"result": result["answer"]}


@app.post("/quiz", include_in_schema=False, deprecated=True)
def quiz_api(request: TextRequest):
    result = TOOL_REGISTRY.execute(
        "study", step_input=request.text, operation="quiz", actor="api"
    )
    return {"result": result["answer"]}


@app.post("/rag", include_in_schema=False, deprecated=True)
def rag_api(request: RagRequest):
    return TOOL_REGISTRY.execute(
        "rag_search",
        step_input=request.text,
        top_k=request.top_k,
        shared_context={
            "retrieval_mode": request.retrieval_mode,
            "reranker_enabled": request.reranker_enabled,
        },
        actor="api",
    )


@app.post("/agent", include_in_schema=False, deprecated=True)
def agent_api(request: TextRequest):
    from backend.ai_core import agent_router

    return {"result": agent_router(request.text)}


@app.post("/upload", tags=["Knowledge Base"])
async def upload_file(file: UploadFile = File(...)):
    original_filename = file.filename or ""
    filename = Path(original_filename).name
    filename_stem = Path(filename).stem.upper()
    if (
        not filename
        or filename != original_filename
        or len(filename) > 128
        or filename != filename.rstrip(" .")
        or any(ord(character) < 32 for character in filename)
        or filename_stem in WINDOWS_RESERVED_FILENAMES
    ):
        raise HTTPException(status_code=400, detail="Invalid file name")

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_DOC_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported file extension")

    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_UPLOAD_CONTENT_TYPES[suffix]:
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type {content_type or '<missing>'} is not allowed for {suffix}",
        )

    DOCS_PATH.mkdir(parents=True, exist_ok=True)
    save_path = DOCS_PATH / filename
    config = get_config()
    max_size = config.max_upload_size_bytes
    max_total_size = getattr(
        config,
        "max_upload_total_bytes",
        max_size * 10,
    )
    declared_size = file.size if isinstance(file.size, int) and file.size >= 0 else 0
    if declared_size > max_size:
        await file.close()
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {max_size} byte upload limit",
        )

    total_size = 0
    pdf_pages = None
    pdf_validation_status = "not_applicable"
    pdf_validation_warnings: list[str] = []
    bypass_upload_parse = False
    header = b""
    reserved_bytes = 0
    temp_path = DOCS_PATH / f".upload-{secrets.token_hex(16)}.tmp"
    text_decoder = (
        codecs.getincrementaldecoder("utf-8")()
        if suffix in {".md", ".txt"}
        else None
    )

    try:
        reservation = _UPLOAD_QUOTA.reserve(
            DOCS_PATH,
            declared_size,
            max_total_size,
        )
        if reservation is None:
            raise HTTPException(
                status_code=507,
                detail="Knowledge file storage quota is full",
            )
        reserved_bytes = reservation

        with temp_path.open("xb") as handle:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_size:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {max_size} byte upload limit",
                    )
                grown_reservation = _UPLOAD_QUOTA.grow(
                    DOCS_PATH,
                    reserved_bytes,
                    total_size,
                    max_total_size,
                )
                if grown_reservation is None:
                    raise HTTPException(
                        status_code=507,
                        detail="Knowledge file storage quota is full",
                    )
                reserved_bytes = grown_reservation
                if len(header) < 5:
                    header = (header + chunk)[:5]
                if text_decoder is not None:
                    try:
                        text_decoder.decode(chunk, final=False)
                    except UnicodeDecodeError as exc:
                        raise HTTPException(
                            status_code=415,
                            detail="Text uploads must be valid UTF-8",
                        ) from exc
                handle.write(chunk)

            if text_decoder is not None:
                try:
                    text_decoder.decode(b"", final=True)
                except UnicodeDecodeError as exc:
                    raise HTTPException(
                        status_code=415,
                        detail="Text uploads must be valid UTF-8",
                    ) from exc
            if suffix == ".pdf" and not header.startswith(b"%PDF-"):
                raise HTTPException(
                    status_code=415,
                    detail="Uploaded PDF does not have a valid PDF signature",
                )
        if suffix == ".pdf":
            try:
                pdf_pages = await asyncio.to_thread(
                    validate_pdf_file,
                    temp_path,
                    max_pages=getattr(config, "max_pdf_pages", 500),
                    timeout_seconds=getattr(
                        config,
                        "pdf_validation_timeout_seconds",
                        5,
                    ),
                    max_memory_bytes=getattr(
                        config,
                        "pdf_validation_max_memory_bytes",
                        256 * 1024 * 1024,
                    ),
                )
                pdf_validation_status = "valid"
            except PDFPageLimitExceeded as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except PDFValidationTimeout as exc:
                pdf_validation_status = "timeout"
                bypass_upload_parse = True
                pdf_validation_warnings.append(
                    f"{exc}. The PDF was stored safely, but parsing was bypassed."
                )
            except PDFValidationError as exc:
                pdf_validation_status = "warning"
                pdf_validation_warnings.append(
                    f"{exc}. The PDF was stored safely and fallback parsing was attempted."
                )
        os.link(temp_path, save_path)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="A file with this name already exists") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to store uploaded file") from exc
    finally:
        temp_path.unlink(missing_ok=True)
        _UPLOAD_QUOTA.release(reserved_bytes)
        await file.close()

    if bypass_upload_parse:
        parse_result = safe_document_parse_result(pdf_validation_warnings[0])
    else:
        try:
            parse_result = await asyncio.to_thread(extract_text_from_document, save_path)
        except Exception as exc:
            parse_result = safe_document_parse_result(
                f"Document parsing failed unexpectedly: {exc}. The file remains safely stored.",
                corrupted_pdf=suffix == ".pdf",
            )

    parse_result = {**parse_result}
    if pdf_validation_warnings:
        parse_result["corrupted_pdf"] = True
        parse_result["warnings"] = list(
            dict.fromkeys([*pdf_validation_warnings, *parse_result["warnings"]])
        )

    if parse_result.get("safe_fallback"):
        message = "文件已安全上传，但文档解析和 OCR 均未获得有效文本；文件已保留，请查看 warnings"
    elif parse_result.get("corrupted_pdf"):
        message = f"{filename} 已上传；检测到 PDF 结构异常，并已通过 fallback 完成解析"
    elif parse_result["need_ocr"] and not parse_result["ocr_used"]:
        message = "文件已上传，但可能是扫描版 PDF，当前 OCR 未启用或不可用，未能提取有效文本"
    else:
        message = f"{filename} 上传成功；重建知识库索引后生效"

    return {
        "message": message,
        "file": filename,
        "parse_method": parse_result["method"],
        "need_ocr": parse_result["need_ocr"],
        "ocr_used": parse_result["ocr_used"],
        "warnings": parse_result["warnings"],
        "corrupted_pdf": parse_result.get("corrupted_pdf", False),
        "safe_fallback": parse_result.get("safe_fallback", False),
        "pdf_parser": parse_result.get("pdf_parser"),
        "pdf_validation_status": pdf_validation_status,
        "rebuild_required": True,
        "size": total_size,
        **({"pages": pdf_pages} if pdf_pages is not None else {}),
    }


@app.get("/knowledge-files", tags=["Knowledge Base"])
def knowledge_files_api():
    DOCS_PATH.mkdir(exist_ok=True)
    files = []

    for file_path in sorted(DOCS_PATH.iterdir(), key=lambda item: item.name.lower()):
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS:
            continue

        files.append(
            {
                "name": file_path.name,
                "type": file_path.suffix.lower().lstrip("."),
                "size": file_path.stat().st_size,
                "url": f"/knowledge-files/{quote(file_path.name)}",
            }
        )

    return {"count": len(files), "files": files}


@app.get("/knowledge-files/{filename}/parse-status", tags=["Knowledge Base"])
def knowledge_file_parse_status_api(filename: str):
    file_path = _safe_doc_path(filename)
    try:
        parse_result = extract_text_from_document(file_path)
    except Exception as exc:
        parse_result = safe_document_parse_result(
            f"Document parsing failed unexpectedly: {exc}",
            corrupted_pdf=file_path.suffix.lower() == ".pdf",
        )
    return {
        "name": file_path.name,
        "type": file_path.suffix.lower().lstrip("."),
        "parse_method": parse_result["method"],
        "need_ocr": parse_result["need_ocr"],
        "ocr_used": parse_result["ocr_used"],
        "page_count": parse_result["page_count"],
        "text_char_count": parse_result["text_char_count"],
        "ocr_error": parse_result["ocr_error"],
        "warnings": parse_result["warnings"],
        "corrupted_pdf": parse_result.get("corrupted_pdf", False),
        "safe_fallback": parse_result.get("safe_fallback", False),
        "pdf_parser": parse_result.get("pdf_parser"),
    }


@app.get("/knowledge-files/{filename}", tags=["Knowledge Base"])
def open_knowledge_file_api(filename: str):
    file_path = _safe_doc_path(filename)
    encoded_name = quote(file_path.name)
    media_types = {
        ".md": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".pdf": "application/pdf",
    }
    return FileResponse(
        file_path,
        media_type=media_types.get(file_path.suffix.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": f"inline; filename*=utf-8''{encoded_name}",
        },
    )


@app.get("/knowledge-files/{filename}/content", tags=["Knowledge Base"])
def read_knowledge_file_content_api(filename: str):
    file_path = _safe_doc_path(filename)

    if file_path.suffix.lower() not in {".md", ".txt"}:
        raise HTTPException(status_code=400, detail="Only md/txt files support text preview")

    return {
        "name": file_path.name,
        "type": file_path.suffix.lower().lstrip("."),
        "content": file_path.read_text(encoding="utf-8"),
    }


@app.post("/learn", include_in_schema=False, deprecated=True)
def learn_api(request: TextRequest):
    from backend.ai_core import learning_workflow

    return learning_workflow(request.text)


@app.post("/rebuild-index", include_in_schema=False, deprecated=True)
def rebuild_index_api(
    request: ToolInvokeRequest | None = None,
    approval_key: str | None = Header(default=None, alias="X-Tool-Approval-Key"),
):
    request = request or ToolInvokeRequest()
    confirmation_subject = _require_tool_requester(
        "rebuild_rag_index",
        approval_key,
    )
    try:
        TOOL_REGISTRY.execute(
            "rebuild_rag_index",
            confirmation_token=request.confirmation_token,
            actor=confirmation_subject or request.actor,
            confirmation_subject=confirmation_subject,
        )
    except ToolConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
    except InvalidConfirmation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "RAG 索引已重建"}


@app.get("/debug-index-sources", include_in_schema=False)
def debug_index_sources_api():
    from backend.rag_store import list_index_sources, list_index_source_statuses

    sources = list_index_sources()
    return {
        "count": len(sources),
        "sources": sources,
        "source_statuses": list_index_source_statuses(),
    }


@app.post("/debug-rag", include_in_schema=False)
def debug_rag_api(request: DebugRagRequest):
    from backend.rag_store import search_relevant_chunks

    result = search_relevant_chunks(
        request.text,
        top_k=request.top_k,
        retrieval_mode=request.retrieval_mode,
        reranker_enabled=request.reranker_enabled,
        include_metadata=True,
    )
    chunks = result["chunks"]

    return {
        "question": request.text,
        "retrieval_mode": result.get("retrieval_mode", request.retrieval_mode),
        "reranker_enabled": result.get("reranker_enabled", request.reranker_enabled),
        "reranker_used": result.get("reranker_used", False),
        "reranker_model": result.get("reranker_model"),
        "reranker_top_n": result.get("reranker_top_n"),
        "reranker_error": result.get("reranker_error"),
        "count": len(chunks),
        "chunks": chunks,
    }


# --- Session history APIs ---


@app.get("/sessions", tags=["Sessions"])
def list_sessions_api(limit: int = Query(50, ge=1, le=200)):
    """List recent chat sessions."""
    if not is_db_history_enabled():
        return {"sessions": [], "message": "DB history is disabled (ENABLE_DB_HISTORY=false)"}

    try:
        with get_db_session() as db:
            sessions = list_sessions(db, limit=limit)
        return {"sessions": sessions}
    except Exception as exc:
        logger.warning("Failed to list sessions: %s", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/sessions/{session_id}/messages", tags=["Sessions"])
def get_session_messages_api(session_id: str, limit: int = Query(50, ge=1, le=500)):
    """Get messages for a specific session."""
    if not is_db_history_enabled():
        return {
            "session_id": session_id,
            "messages": [],
            "message": "DB history is disabled (ENABLE_DB_HISTORY=false)",
        }

    try:
        with get_db_session() as db:
            messages = get_session_messages(db, session_id, limit=limit)
        return {"session_id": session_id, "messages": messages}
    except Exception as exc:
        logger.warning("Failed to get session messages: %s", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


# --- Frontend static serving (pip install / Docker single-container deployments) ---
# Mounted last, so every registered API route takes precedence. The root route
# above serves index.html when static files are bundled.
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
