"""Document parsing: raw text extraction and clause splitting.

Extracts text via the existing OCR/pypdf pathway and splits it into numbered
clauses (heading + body) for the engine to score. Deliberately deterministic
and LLM-free: parsing failures must be reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.ocr_service import extract_text_from_document

MAX_CLAUSE_CHARS = 2000
MIN_CLAUSE_CHARS = 2

# Numbered clause starts: "## 标题", "第3条", "3.1", "一、", "(a)", "Article 5"
_HEADING_RE = re.compile(
    r"""^\s*(
        #{1,6}\s+\S
      | 第[一二三四五六七八九十百\d]+[条款章节部分]
      | \d+(\.\d+)*[\.、\)]?\s+\S
      | [(（]?[a-zA-Z\d][)）]\s+\S
      | [（(][一二三四五六七八九十]+[)）]
      | [一二三四五六七八九十]+、\s*\S
      | (Article|Section|Clause)\s+\d+
    )""",
    re.VERBOSE,
)
_SLA_KEYWORDS = ("服务级别", "服务水平", "SLA", "uptime", "可用性", "响应时间")


@dataclass
class ParsedClause:
    ordinal: int
    heading: str | None
    text: str


@dataclass
class ParsedDocument:
    text: str
    clauses: list[ParsedClause]
    parse_method: str
    page_count: int | None = None
    warnings: list[str] | None = None


class DocumentParseError(Exception):
    """Raised when the document cannot be parsed into usable text."""


def _extract_raw_text(path, suffix: str) -> tuple[str, str, int | None, list[str]]:
    """Return (text, method, page_count, warnings)."""

    if suffix in {".md", ".txt"}:
        try:
            return path.read_text(encoding="utf-8"), "text", 1, []
        except (OSError, UnicodeError) as exc:
            raise DocumentParseError(f"无法读取文档内容: {exc}") from exc

    if suffix == ".pdf":
        result = extract_text_from_document(path)
        warnings = list(result.get("warnings") or [])
        text = (result.get("text") or "").strip()
        if not text and result.get("need_ocr"):
            warnings.append("PDF 无内嵌文本（可能为扫描件），OCR 评分不在本期支持范围")
        if not text:
            raise DocumentParseError("PDF 未提取到文本内容")
        return text, result.get("method", "pypdf"), result.get("page_count"), warnings

    if suffix == ".docx":
        return _extract_docx(path)

    raise DocumentParseError(f"不支持的文件格式: {suffix}")


def _extract_docx(path) -> tuple[str, str, int | None, list[str]]:
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError("服务器未安装 python-docx，无法解析 Word 文档") from exc
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocumentParseError(f"Word 文档解析失败: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    if not parts:
        raise DocumentParseError("Word 文档内容为空")
    return "\n".join(parts), "docx", None, []


def _is_clause_start(line: str) -> bool:
    return bool(_HEADING_RE.match(line))


def _is_sla_block(line: str) -> bool:
    return any(keyword in line for keyword in _SLA_KEYWORDS)


def split_clauses(text: str) -> list[ParsedClause]:
    """Split document text into clauses.

    Primary strategy: numbered clause headings. Fallback: paragraph blocks
    (double-newline or single newline groups), so unstructured documents still
    produce scorable units instead of failing.
    """

    lines = [line.rstrip() for line in text.splitlines()]
    blocks: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_clause_start(stripped) or (_is_sla_block(stripped) and current_heading):
            if current_lines:
                blocks.append((current_heading, current_lines))
            current_heading = stripped if _is_clause_start(stripped) else None
            current_lines = [stripped]
        else:
            if not current_lines:
                current_heading = None
            current_lines.append(stripped)
    if current_lines:
        blocks.append((current_heading, current_lines))

    clauses: list[ParsedClause] = []
    buffer_text: list[str] = []
    buffer_heading: str | None = None

    def flush_buffer() -> None:
        if buffer_text:
            joined = "\n".join(buffer_text).strip()
            if joined:
                clauses.append(
                    ParsedClause(
                        ordinal=len(clauses) + 1,
                        heading=buffer_heading,
                        text=joined[:MAX_CLAUSE_CHARS],
                    )
                )
            buffer_text.clear()

    for heading, block_lines in blocks:
        block_text = "\n".join(block_lines).strip()
        if len(block_text) < MIN_CLAUSE_CHARS:
            continue
        if heading is not None:
            flush_buffer()
            buffer_heading = heading
            buffer_text.append(block_text[:MAX_CLAUSE_CHARS])
            # A complete numbered block becomes its own clause immediately.
            flush_buffer()
            buffer_heading = None
        else:
            buffer_text.append(block_text[:MAX_CLAUSE_CHARS])
            if sum(len(t) for t in buffer_text) >= MAX_CLAUSE_CHARS // 2:
                flush_buffer()
                buffer_heading = None
    flush_buffer()

    if not clauses:
        # Last resort: whole document as a single clause.
        trimmed = text.strip()[:MAX_CLAUSE_CHARS]
        if trimmed:
            clauses.append(ParsedClause(ordinal=1, heading=None, text=trimmed))
    return clauses


def parse_document(path) -> ParsedDocument:
    path_obj = __import__("pathlib").Path(path)
    suffix = path_obj.suffix.lower()
    text, method, page_count, warnings = _extract_raw_text(path_obj, suffix)
    clauses = split_clauses(text)
    if not clauses:
        raise DocumentParseError("文档切分后没有可评分的条款")
    return ParsedDocument(
        text=text,
        clauses=clauses,
        parse_method=method,
        page_count=page_count,
        warnings=warnings,
    )
