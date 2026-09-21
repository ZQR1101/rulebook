"""Seeded-defect evaluation: does the engine catch what a human planted?

Gold set: ``eval_cases/seeded_defect_cases.json``. Every synthetic contract is
annotated rule-by-rule as clean (compliant decoy), defect (violation planted in
the text, with the expected rating) or gap (topic absent entirely). The three
buckets must cover every active rule of the playbook, so a passing run scores
the engine on a corpus with no unlabeled blind spots.

Metrics (per document and aggregated over the corpus):

- defect_recall        : planted violations the engine refused to call green
- defect_exact         : planted violations graded at the exact expected rating
- gap_recall           : missing topics turned red *with* a gap_reason
- false_positive_rate  : compliant clauses the engine flagged red/amber
- citation_genuineness : share of model-emitted quotes that exist in the document
- gate_catch_rate      : unsupported greens the citation gate actually downgraded

The citation metrics need what the model emitted *before* the gate filtered it,
so the real-LLM path wraps the model in :class:`RecordingLLM` and revalidates
every quote against the source document. ``--fake`` runs a scripted model so CI
can exercise the harness (and the metric math) offline — those numbers validate
the measuring stick, not the engine.

Output: ``reports/SEEDED_DEFECT_EVAL_REPORT.md`` + a JSON blob beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CASES_PATH = PROJECT_ROOT / "eval_cases" / "seeded_defect_cases.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "SEEDED_DEFECT_EVAL_REPORT.md"
METRICS_PATH = PROJECT_ROOT / "reports" / "SEEDED_DEFECT_EVAL_METRICS.json"

FLAGGED = ("red", "amber")
_RULE_LINE_RE = re.compile(r"- 规则：(.+)")

# Measured on one fixed corpus: three passes today returned 2, 5 and 6 false
# positives out of 33 clean clauses. A single pass therefore cannot resolve a
# change of that size, so the baseline is several passes plus a reported spread.
# Decoding stays at the online default so the number describes what reviewers
# actually see; pass --temperature 0 for a reproducible greedy comparison.
EVAL_TEMPERATURE = 0.7


# --------------------------------------------------------------------------- gold


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def gold_index(case: dict) -> dict[str, dict]:
    """rule name → {"kind", "expected", "note"} for every annotated rule."""

    index: dict[str, dict] = {}

    def add(rule: str, kind: str, expected: str, note: str) -> None:
        if rule in index:
            raise ValueError(f"{case['id']}: 规则 {rule} 被重复标注")
        index[rule] = {"kind": kind, "expected": expected, "note": note}

    for defect in case.get("defects", []):
        add(defect["rule"], "defect", defect["expected_rating"], defect.get("planted", ""))
    for gap in case.get("gaps", []):
        add(gap["rule"], "gap", "red", gap.get("missing", ""))
    for rule in case.get("clean", []):
        add(rule, "clean", "green", "")
    return index


def validate_gold(case: dict, rule_names: set[str]) -> list[str]:
    """Return human-readable problems: unknown rules, uncovered rules."""

    index = gold_index(case)
    problems = [f"标注了剧本中不存在的规则：{name}" for name in sorted(set(index) - rule_names)]
    missing = sorted(rule_names - set(index))
    if missing:
        problems.append(f"未标注的启用规则：{', '.join(missing)}")
    return problems


# ------------------------------------------------------------------------- scoring


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def score_case(gold: dict, rows: list[dict], emissions: dict[str, dict] | None = None) -> dict:
    """Compare engine verdicts against gold annotations for one document.

    ``rows``: [{"rule_name", "dimension", "rating", "gap_reason",
    "review_state", "citations"}]. ``emissions``: rule name → raw model output
    ({"emitted_rating", "emitted_citations", "valid_citations",
    "invalid_citations"}) captured by :class:`RecordingLLM`.
    """

    emissions = emissions or {}
    expected_rules = len(gold)
    details: list[dict] = []
    counters = {
        "defect": [0, 0, 0],  # flagged, exact, total
        "gap": [0, 0, 0],  # red+gap_reason, flagged, total
        "clean": [0, 0, 0],  # green, total, (unused)
    }
    exact_total = 0
    emitted = valid_emitted = 0
    unsupported_green = caught_green = 0
    by_dimension: dict[str, dict[str, int]] = {}

    for row in rows:
        name = row["rule_name"]
        annotation = gold.get(name)
        if annotation is None:
            continue
        kind, expected = annotation["kind"], annotation["expected"]
        rating = row["rating"]
        raw = emissions.get(name, {})
        emit_count = len(raw.get("emitted_citations", [])) if raw else 0
        valid_count = len(raw.get("valid_citations", [])) if raw else 0
        emitted += emit_count
        valid_emitted += valid_count

        gap_recalled = rating == "red" and bool((row.get("gap_reason") or "").strip())
        flagged = rating in FLAGGED
        exact = rating == expected
        outcome = {
            "rule": name,
            "dimension": row.get("dimension", ""),
            "kind": kind,
            "expected": expected,
            "actual": rating,
            "annotated": annotation["note"],
            "gap_reason": row.get("gap_reason"),
            "rationale": row.get("rationale"),
            "flagged": flagged,
            "exact": exact,
            "gap_recalled": gap_recalled,
            "review_state": row.get("review_state"),
            "citations_stored": len(row.get("citations") or []),
            "citations_emitted": emit_count,
            "citations_invalid": max(emit_count - valid_count, 0),
        }
        details.append(outcome)

        if kind == "defect":
            counters["defect"][0] += int(flagged)
            counters["defect"][1] += int(exact)
            counters["defect"][2] += 1
        elif kind == "gap":
            counters["gap"][0] += int(gap_recalled)
            counters["gap"][1] += int(flagged)
            counters["gap"][2] += 1
        else:
            counters["clean"][0] += int(rating == "green")
            counters["clean"][1] += 1
        exact_total += int(exact)

        # Citation gate: an unsupported green (claimed green with no quote that
        # survives validation) must have been downgraded in the stored verdict.
        if raw and raw.get("emitted_rating") == "green" and valid_count == 0:
            unsupported_green += 1
            caught_green += int(rating != "green")

        bucket = by_dimension.setdefault(
            outcome["dimension"] or "未分类",
            {"defect": 0, "defect_hit": 0, "gap": 0, "gap_hit": 0, "clean": 0, "clean_hit": 0},
        )
        if kind == "defect":
            bucket["defect"] += 1
            bucket["defect_hit"] += int(flagged)
        elif kind == "gap":
            bucket["gap"] += 1
            bucket["gap_hit"] += int(gap_recalled)
        else:
            bucket["clean"] += 1
            bucket["clean_hit"] += int(rating == "green")

    defect_flagged, defect_exact, defect_total = counters["defect"]
    gap_recalled_n, gap_flagged_n, gap_total = counters["gap"]
    clean_green, clean_total, _ = counters["clean"]
    false_positives = clean_total - clean_green
    return {
        "rules_annotated": expected_rules,
        "rules_scored": len(details),
        "coverage_ok": len(details) == expected_rules,
        "defect_total": defect_total,
        "defect_recall": _ratio(defect_flagged, defect_total),
        "defect_exact": _ratio(defect_exact, defect_total),
        "gap_total": gap_total,
        "gap_recall": _ratio(gap_recalled_n, gap_total),
        "gap_flagged": _ratio(gap_flagged_n, gap_total),
        "clean_total": clean_total,
        "clean_pass_rate": _ratio(clean_green, clean_total),
        "false_positive_rate": _ratio(false_positives, clean_total),
        "false_positive_rules": [d["rule"] for d in details if d["kind"] == "clean" and d["actual"] in FLAGGED],
        "missed_defect_rules": [
            d["rule"] for d in details if d["kind"] in ("defect", "gap") and not d["flagged"]
        ],
        "inexact_rules": [d["rule"] for d in details if not d["exact"]],
        "exact_agreement": _ratio(exact_total, len(details)),
        "citations_emitted": emitted,
        "citation_genuineness": _ratio(valid_emitted, emitted),
        "citations_invalid": max(emitted - valid_emitted, 0),
        "unsupported_green_total": unsupported_green,
        "unsupported_green_caught": caught_green,
        "gate_catch_rate": _ratio(caught_green, unsupported_green),
        "by_dimension": by_dimension,
        "details": details,
    }


def _bucket_totals(results: list[dict], bucket: str | None, predicate) -> tuple[int, int]:
    """Pool one predicate over every annotated rule (bucket=None = all kinds)."""

    hit = total = 0
    for result in results:
        for detail in result["metrics"]["details"]:
            if bucket is not None and detail["kind"] != bucket:
                continue
            total += 1
            hit += int(predicate(detail))
    return hit, total


def aggregate(results: list[dict]) -> dict:
    """Pool numerators/denominators across documents (weighted, not averaged)."""

    defect_hit, defect_total = _bucket_totals(results, "defect", lambda d: d["flagged"])
    defect_exact, _ = _bucket_totals(results, "defect", lambda d: d["exact"])
    gap_hit, gap_total = _bucket_totals(results, "gap", lambda d: d["gap_recalled"])
    clean_green, clean_total = _bucket_totals(results, "clean", lambda d: d["actual"] == "green")
    exact_hit, exact_total = _bucket_totals(results, None, lambda d: d["exact"])

    metrics: dict = {
        "documents": len(results),
        "rules_scored": sum(r["metrics"]["rules_scored"] for r in results),
        "defect_total": defect_total,
        "defect_recall": _ratio(defect_hit, defect_total),
        "defect_exact": _ratio(defect_exact, defect_total),
        "gap_total": gap_total,
        "gap_recall": _ratio(gap_hit, gap_total),
        "clean_total": clean_total,
        "clean_pass_rate": _ratio(clean_green, clean_total),
        "false_positive_rate": _ratio(clean_total - clean_green, clean_total),
        "exact_agreement": _ratio(exact_hit, exact_total),
        "citations_emitted": sum(r["metrics"]["citations_emitted"] for r in results),
        "citations_invalid": sum(r["metrics"]["citations_invalid"] for r in results),
        "unsupported_green_total": sum(r["metrics"]["unsupported_green_total"] for r in results),
        "unsupported_green_caught": sum(r["metrics"]["unsupported_green_caught"] for r in results),
        "total_latency_ms": sum(r["latency_ms"] for r in results),
    }
    metrics["citation_genuineness"] = _ratio(
        metrics["citations_emitted"] - metrics["citations_invalid"],
        metrics["citations_emitted"],
    )
    metrics["gate_catch_rate"] = _ratio(
        metrics["unsupported_green_caught"], metrics["unsupported_green_total"]
    )
    usage = [r.get("usage") or {} for r in results]
    cost_total = sum(float(item.get("estimated_cost_usd") or 0) for item in usage)
    metrics["models"] = sorted({str(item["model"]) for item in usage if item.get("model")})
    metrics["llm_calls"] = sum(int(item.get("calls") or 0) for item in usage)
    metrics["input_tokens"] = sum(int(item.get("input_tokens") or 0) for item in usage)
    metrics["output_tokens"] = sum(int(item.get("output_tokens") or 0) for item in usage)
    metrics["total_tokens"] = sum(int(item.get("total_tokens") or 0) for item in usage)
    metrics["token_source"] = _mode({str(item.get("token_source")) for item in usage})
    metrics["estimated_cost_usd"] = round(cost_total, 6) if cost_total else None
    metrics["pricing_source"] = _mode({str(item.get("pricing_source")) for item in usage})
    return metrics


def _mode(values: set[str]) -> str:
    clean = {value for value in values if value and value != "None"}
    if not clean:
        return "—"
    return sorted(clean)[0] if len(clean) == 1 else "mixed"


# --------------------------------------------------------------------------- models


class _Response:
    def __init__(self, content: str):
        self.content = content


class AllRedFakeLLM:
    """Deterministic mechanics pass: every rule red with a gap reason."""

    def invoke(self, prompt: str) -> _Response:
        match = _RULE_LINE_RE.search(prompt)
        rule = match.group(1).strip() if match else "规则"
        return _Response(
            json.dumps(
                {
                    "rating": "red",
                    "rationale": f"机制验证：未找到满足 {rule} 标准的条款。",
                    "citations": [],
                    "gap_reason": "机制验证：文档未约定该事项",
                },
                ensure_ascii=False,
            )
        )


class HallucinatedGreenFakeLLM:
    """Deterministic mechanics pass: unsupported greens with invented quotes."""

    def invoke(self, _prompt: str) -> _Response:
        return _Response(
            json.dumps(
                {
                    "rating": "green",
                    "rationale": "机制验证：看起来满足要求。",
                    "citations": [{"quote": "本合同完全符合公司采购标准第 99 条"}],
                    "gap_reason": "",
                },
                ensure_ascii=False,
            )
        )


def _rule_from_prompt(prompt: str) -> str:
    match = _RULE_LINE_RE.search(prompt)
    return match.group(1).strip() if match else ""


class RecordingLLM:
    """Delegate to the real model, capturing raw citations before the gate.

    The platform stores only gate-validated citations, so genuineness has to be
    measured on what the model actually emitted. Validation uses the full
    document text, matching the strictest reading of the gate.
    """

    def __init__(self, inner, document_text: str):
        self._inner = inner
        self._document_text = document_text
        self.records: dict[str, dict] = {}
        self.calls = 0

    def invoke(self, prompt: str):
        from backend.engine.citation_gate import validate_citations
        from backend.engine.scoring import _extract_json

        self.calls += 1
        response = self._inner.invoke(prompt)
        content = getattr(response, "content", response)
        data = _extract_json(content if isinstance(content, str) else str(content)) or {}
        emitted = data.get("citations") if isinstance(data.get("citations"), list) else []
        valid, invalid = validate_citations(emitted, self._document_text)
        rule = _rule_from_prompt(prompt)
        if rule:
            self.records[rule] = {
                "emitted_rating": str(data.get("rating", "")).lower(),
                "emitted_citations": emitted,
                "valid_citations": valid,
                "invalid_citations": invalid,
            }
        return response


# -------------------------------------------------------------------------- runner


def _resolve_model(model: str | None) -> str:
    """Explicit ``--model`` wins; unknown names are an error, not a silent fallback."""

    from backend.config import SUPPORTED_MODELS, get_config, normalize_model

    if model is None:
        return get_config().model
    if normalize_model(model) != model:
        raise ValueError(f"未知模型 {model}；可选：{', '.join(sorted(SUPPORTED_MODELS))}")
    return model


def _tracked_llm(inner, model: str):
    from backend.llm_service import track_llm_usage

    return track_llm_usage(inner, model)


def _usage_summary(tracker, model: str) -> dict:
    """Tokens and estimated spend for one document's scoring calls."""

    from backend.llm_service import summarize_usage_records

    summary = summarize_usage_records(tracker.usage_records)
    tokens = summary.get("token_usage") or {}
    cost = summary.get("estimated_cost") or {}
    return {
        "model": model,
        "calls": len(summary.get("llm_calls") or []),
        "input_tokens": int(tokens.get("input_tokens") or 0),
        "output_tokens": int(tokens.get("output_tokens") or 0),
        "total_tokens": int(tokens.get("total_tokens") or 0),
        "token_source": tokens.get("source"),
        "estimated_cost_usd": cost.get("total"),
        "currency": cost.get("currency"),
        "pricing_source": cost.get("source"),
    }


def _isolated_env() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="rulebook-seed-eval-"))
    os.environ["PLATFORM_DB_PATH"] = str(tmp / "eval.db")
    os.environ["PLATFORM_DOCS_DIR"] = str(tmp / "docs")
    os.environ["AUTH_SECRET"] = "seed-eval-secret-0123456789abcdef0123456789abcdef"
    return tmp


def _read_verdicts(document_id: str) -> tuple[list[dict], dict]:
    from backend.documents.models import Document, Rule
    from backend.platform_db import platform_session

    session = platform_session()
    try:
        document = session.get(Document, document_id)
        rules = {r.id: r for r in session.query(Rule).filter_by(playbook_id=document.playbook_id).all()}
        rows = []
        for verdict in document.verdicts:
            rule = rules.get(verdict.rule_id)
            rows.append(
                {
                    "rule_name": rule.name if rule else verdict.rule_id,
                    "dimension": rule.dimension if rule else "",
                    "rating": verdict.rating,
                    "rationale": verdict.rationale,
                    "gap_reason": verdict.gap_reason,
                    "review_state": verdict.review_state,
                    "citations": verdict.citations or [],
                }
            )
        return rows, {"scorecard": document.scorecard}
    finally:
        session.close()


def _engine_run_meta(document_id: str) -> dict:
    from backend.documents.models import EngineRun
    from backend.platform_db import platform_session

    session = platform_session()
    try:
        run = (
            session.query(EngineRun)
            .filter(EngineRun.document_id == document_id)
            .order_by(EngineRun.started_at.desc())
            .first()
        )
        if run is None:
            return {}
        stage = next((s for s in (run.stages or []) if s.get("stage") == "score"), {})
        return {
            "run_status": run.status,
            "latency_ms": run.total_latency_ms or 0,
            "retrieval_mode": stage.get("retrieval_mode"),
            "scoring_failures": stage.get("failures"),
        }
    finally:
        session.close()


def _eval_llm(model: str, temperature: float = EVAL_TEMPERATURE):
    """Scoring client for evaluation runs, with the decoding set explicitly."""

    from backend.llm_service import build_llm

    return build_llm(model=model, temperature=temperature)


def run_case(
    case: dict,
    *,
    fake: str | None,
    model: str | None = None,
    temperature: float = EVAL_TEMPERATURE,
) -> dict:
    from backend.documents.models import Rule
    from backend.documents.service import create_document
    from backend.engine.orchestrator import run_review
    from backend.platform_db import platform_session, seed_playbook_rules
    from backend.playbooks import get_playbook

    selected_model = _resolve_model(model)

    document_path = PROJECT_ROOT / case["document"]
    content = document_path.read_bytes()
    document_text = document_path.read_text(encoding="utf-8", errors="ignore")

    session = platform_session()
    try:
        seed_playbook_rules(session, case["playbook_id"])
        rule_names = {
            rule.name
            for rule in session.query(Rule).filter_by(playbook_id=case["playbook_id"], active=True).all()
        }
    finally:
        session.close()

    problems = validate_gold(case, rule_names)
    if problems:
        raise ValueError(f"{case['id']} 金标集不完整：" + "；".join(problems))

    if fake == "all_red":
        inner = AllRedFakeLLM()
    elif fake == "green_hallucinated":
        inner = HallucinatedGreenFakeLLM()
    else:
        inner = _eval_llm(selected_model, temperature)
    # Recorded in every mode: --fake exists to prove the citation metrics work,
    # and they need the pre-gate emissions.
    recorder = RecordingLLM(inner, document_text)
    llm = _tracked_llm(recorder, selected_model)

    session = platform_session()
    try:
        document, created = create_document(
            session,
            playbook_id=case["playbook_id"],
            filename=document_path.name,
            content=content,
            actor="seeded-eval",
        )
        document_id = document.id
        friendly_id = document.friendly_id
    finally:
        session.close()

    if not created:
        raise RuntimeError(
            f"{case['id']}：该文档内容此前已入库（{friendly_id}），引擎不会重跑已签字的文档。"
            "去掉 --live-db 以使用隔离临时数据库，或删除该文档后重试。"
        )

    started = time.perf_counter()
    run_review(document_id, trigger="seeded-eval", custom_llm=llm)
    wall_ms = int((time.perf_counter() - started) * 1000)

    rows, extra = _read_verdicts(document_id)
    metrics = score_case(gold_index(case), rows, recorder.records)
    meta = _engine_run_meta(document_id)
    playbook = get_playbook(case["playbook_id"])
    return {
        "case_id": case["id"],
        "title": case.get("title", ""),
        "playbook_id": case["playbook_id"],
        "playbook_name": playbook.name,
        "document": case["document"],
        "friendly_id": friendly_id,
        "wall_ms": wall_ms,
        "latency_ms": int(meta.get("latency_ms") or wall_ms),
        "retrieval_mode": meta.get("retrieval_mode"),
        "run_temperature": None if fake else temperature,
        "run_status": meta.get("run_status"),
        "scoring_failures": meta.get("scoring_failures"),
        "usage": _usage_summary(llm, selected_model),
        "scorecard": extra["scorecard"],
        "metrics": metrics,
    }


def _describe_error(exc: BaseException) -> str:
    """One line including the cause chain — 'Connection error.' alone is useless."""

    parts = []
    for _ in range(3):
        if exc is None:
            break
        parts.append(f"{type(exc).__name__}: {str(exc).strip() or repr(exc)}")
        exc = exc.__cause__ or exc.__context__
    return " ← ".join(parts)


def _reset_database() -> None:
    from backend.platform_db import init_platform_db, reset_platform_db

    reset_platform_db()
    init_platform_db()


def _fresh_isolated_database() -> None:
    """Move to an empty database, so stored documents really do disappear.

    ``reset_platform_db`` only rebinds the engine: re-initialising the same temp
    file keeps the documents the finished cases ingested, and the next attempt
    then dies on the duplicate-content guard instead of reaching the engine.
    """

    _isolated_env()
    _reset_database()


def _run_case_with_retry(
    case: dict, *, fake: str | None, attempts: int, model: str | None, temperature: float
) -> dict:
    attempt = 1
    while True:
        try:
            return run_case(case, fake=fake, model=model, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 — one flaky API call must not eat the run
            if attempt >= attempts:
                raise
            print(
                f"[WARN] {case['id']} 第 {attempt} 次失败（{_describe_error(exc)}），"
                "重建临时库后重试…",
                flush=True,
            )
            time.sleep(5 * attempt)
            # Finished cases are already materialised metric dicts, so starting
            # over on an empty database is safe — and required, or the retry hits
            # the duplicate-content guard instead of the engine.
            _fresh_isolated_database()
            attempt += 1


# -------------------------------------------------------------------------- report


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def _money(value) -> str:
    return "—" if not value else f"${float(value):.4f}"


def _counts(results: list[dict], bucket: str, predicate) -> str:
    hit, total = _bucket_totals(results, bucket, predicate)
    return f"{hit}/{total}" if total else "—"


def _temperature_line(results: list[dict]) -> list[str]:
    recorded = (result.get("run_temperature") for result in results)
    temperatures = sorted({value for value in recorded if value is not None})
    if not temperatures:
        return []
    shown = " / ".join(f"{value:g}" for value in temperatures)
    note = "（与线上一致）" if temperatures == [0.7] else "（非线上默认，数字不可与基线互换）"
    return [f"判定温度：{shown}{note}"]


_NOISE_METRICS = (
    ("defect_recall", "缺陷召回"),
    ("gap_recall", "缺口召回"),
    ("false_positive_rate", "误报率"),
    ("exact_agreement", "整体等级一致率"),
    ("citation_genuineness", "引用真实率"),
)


def _noise_section(passes: list[list[dict]] | None) -> list[str]:
    """Run-to-run spread, because one pass of a sampling model is not a measurement.

    Observed on this corpus: three passes returned 2, 5 and 6 false positives out
    of 33 clean clauses. Without the spread, a change of that size reads as a
    regression or a win depending on which run was kept.
    """

    if not passes or len(passes) < 2:
        return []
    summary = [aggregate(items) for items in passes]
    lines = [
        f"## 轮间噪声（{len(passes)} 轮独立运行）",
        "",
        "同一份语料、同一套代码、同一模型，逐轮各跑一遍；极差就是本报告的读数误差。",
        "",
        "| 指标 | " + " | ".join(f"第 {index} 轮" for index in range(1, len(passes) + 1)) + " | 均值 | 极差 |",
        "|---|" + "---:|" * (len(passes) + 2),
    ]
    for key, label in _NOISE_METRICS:
        values = [item[key] for item in summary]
        present = [value for value in values if value is not None]
        if not present:
            continue
        spread = max(present) - min(present)
        lines.append(
            f"| {label} | "
            + " | ".join(_pct(value) for value in values)
            + f" | {_pct(sum(present) / len(present))} | {_pct(spread)} |"
        )
    lines.append("")
    return lines


def render_markdown(
    results: list[dict],
    *,
    fake: str | None,
    failed: list[str] | None = None,
    passes: list[list[dict]] | None = None,
) -> str:
    overall = aggregate(results)
    mode = {"all_red": "MechanicsFake · 全红", "green_hallucinated": "MechanicsFake · 幻觉绿色"}.get(
        fake or "", "真实 LLM"
    )
    lines = [
        "# 种入缺陷评测报告（Seeded-Defect Eval）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"运行模式：**{mode}**" + ("（机制验证，不代表引擎能力）" if fake else ""),
        f"语料：{len(max(passes, key=len)) if passes else len(results)} 份合成文档"
        + (f" × {len(passes)} 轮" if passes and len(passes) > 1 else "")
        + f" / {overall['rules_scored']} 条规则判定",
        f"条款检索：{', '.join(sorted({str(r['retrieval_mode']) for r in results}))}",
        *_temperature_line(results),
        f"评分模型：**{', '.join(overall['models']) or '—'}** · {overall['llm_calls']} 次调用 · "
        f"tokens {overall['input_tokens']}/{overall['output_tokens']}（{overall['token_source']}） · "
        f"预估 {_money(overall['estimated_cost_usd'])}（{overall['pricing_source']}）",
        "",
    ]
    if failed:
        lines.append(
            f"**未完成用例：{', '.join(failed)}** — 下列指标只覆盖成功跑完的 "
            f"{len(results)} 份文档，不代表整套语料。"
        )
        lines.append("")
    lines += [
        "## 总体指标",
        "",
        "| 指标 | 数值 | 命中/总数 | 含义 |",
        "|---|---:|---|---|",
        f"| 缺陷召回 | {_pct(overall['defect_recall'])} | {_counts(results, 'defect', lambda d: d['flagged'])} "
        "| 埋入的违规条款被判定为红/黄的比例 |",
        f"| 缺陷等级一致 | {_pct(overall['defect_exact'])} | {_counts(results, 'defect', lambda d: d['exact'])} "
        "| 红/黄等级与人工预期完全一致 |",
        f"| 缺口召回 | {_pct(overall['gap_recall'])} | {_counts(results, 'gap', lambda d: d['gap_recalled'])} "
        "| 整段缺失被正确判红且给出 gap_reason |",
        f"| 干净条款通过率 | {_pct(overall['clean_pass_rate'])} | {_counts(results, 'clean', lambda d: d['actual'] == 'green')} "
        "| 合规诱饵未被误报的比例 |",
        f"| **误报率** | {_pct(overall['false_positive_rate'])} | — | 合规条款被判红/黄（签字负担的直接来源） |",
        f"| 整体等级一致率 | {_pct(overall['exact_agreement'])} | — | 全规则精确匹配 |",
        f"| 引用真实率 | {_pct(overall['citation_genuineness'])} | "
        f"{overall['citations_emitted'] - overall['citations_invalid']}/{overall['citations_emitted']} "
        "| 模型给出的原文引用可逐字回溯到文档 |",
        f"| 引用门拦截率 | {_pct(overall['gate_catch_rate'])} | "
        f"{overall['unsupported_green_caught']}/{overall['unsupported_green_total']} "
        "| 无有效引用的绿色判定被降级为黄的比例 |",
        f"| 时延 | {overall['total_latency_ms']} ms | {len(results)} 次文档评审 | 引擎端到端 |",
        "",
        *_noise_section(passes),
        "## 分文档",
        "",
        "| Case | 剧本 | 缺陷召回 | 缺口召回 | 误报率 | 等级一致 | 引用真实 | 拦截 | 风险指数 | 时延 | 预估 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        m = result["metrics"]
        scorecard = result.get("scorecard") or {}
        usage = result.get("usage") or {}
        lines.append(
            f"| {result['case_id']} | {result['playbook_name']} | {_pct(m['defect_recall'])} "
            f"| {_pct(m['gap_recall'])} | {_pct(m['false_positive_rate'])} | {_pct(m['exact_agreement'])} "
            f"| {_pct(m['citation_genuineness'])} | {_pct(m['gate_catch_rate'])} "
            f"| {scorecard.get('risk_index', '—')} | {result['latency_ms']} ms "
            f"| {_money(usage.get('estimated_cost_usd'))} |"
        )

    lines += ["", "## 未命中清单", ""]
    missed = [
        (result["case_id"], detail)
        for result in results
        for detail in result["metrics"]["details"]
        if detail["kind"] in ("defect", "gap") and not detail["flagged"]
    ]
    false_positives = [
        (result["case_id"], detail)
        for result in results
        for detail in result["metrics"]["details"]
        if detail["kind"] == "clean" and detail["actual"] in FLAGGED
    ]
    if not missed and not false_positives:
        lines.append("无漏报、无误报。")
    for case_id, detail in missed:
        lines.append(
            f"- **漏报** {case_id} · {detail['rule']}：人工预期 {detail['expected']}，"
            f"引擎判定 {detail['actual']}。埋入点：{detail['annotated'] or '（整段缺失）'}"
        )
    for case_id, detail in false_positives:
        lines.append(
            f"- **误报** {case_id} · {detail['rule']}：合规条款被判 {detail['actual']}"
            f"（review_state={detail['review_state']}）："
            f"{(detail.get('rationale') or detail.get('gap_reason') or '')[:100]}"
        )

    lines += ["", "## 等级不一致（判对方向但档位偏移）", ""]
    inexact = [
        (result["case_id"], detail)
        for result in results
        for detail in result["metrics"]["details"]
        if not detail["exact"] and detail["flagged"]
    ]
    if not inexact:
        lines.append("无。")
    for case_id, detail in inexact:
        lines.append(
            f"- {case_id} · {detail['rule']}（{detail['kind']}）：预期 {detail['expected']} → 实际 {detail['actual']}"
        )

    lines += ["", "## 逐条明细", ""]
    for result in results:
        lines += [
            f"### {result['case_id']} · {result['title']}",
            "",
            f"文档：`{result['document']}` → {result['friendly_id']}",
            "",
            "| 规则 | 标注 | 预期 | 实际 | 引用(存储/发出/无效) | 判定理由摘要 |",
            "|---|---|---|---|---|---|",
        ]
        for detail in sorted(result["metrics"]["details"], key=lambda d: (d["kind"], d["rule"])):
            mark = "✅" if detail["exact"] else ("⚠️" if detail["flagged"] else "❌")
            lines.append(
                f"| {detail['rule']} {mark} | {detail['kind']} | {detail['expected']} | {detail['actual']} "
                f"| {detail['citations_stored']}/{detail['citations_emitted']}/{detail['citations_invalid']} "
                f"| {(detail.get('rationale') or detail.get('gap_reason') or '')[:60]} |"
            )
        lines.append("")

    lines += [
        "## 说明与局限",
        "",
        "- 金标集为合成文档：每份都短于条款检索预算（6000 字符），因此本评测衡量的是**评分与引用治理**，"
        "不混入检索召回因素；检索质量的评测见 `scripts/evaluate_retrieval.py`。",
        "- 每条规则都被显式标注为 clean/defect/gap，未标注即视为金标集缺陷（脚本会直接报错），"
        "因此不存在“模型判了什么但没人核对”的盲区。",
        "- 缺陷与缺口的判定标准来自剧本 guidance 的阈值原文，人工可复核；等级一致率低于召回率属正常"
        "（阈值边界处的红/黄偏移对签字流程影响有限）。",
        "- `--fake` 两种模式（全红 / 幻觉绿色）只验证指标计算与治理链路，不构成能力证据。",
        "- “预估”按 `TOKEN_PRICE_*` 环境变量计价；未配置时使用默认估价 "
        "（$0.20 输入 / $0.80 输出 每百万 token），只是量级参考而非账单金额。"
        "token 数优先取 API 返回值，缺失时按字符估算。`--model` 可覆盖评分模型，"
        "默认沿用 `.env` 的 `DEEPSEEK_MODEL`。",
        "- 本脚本强制 `EMAIL_ENABLED=false`，评测运行不会给团队外发通知邮件。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fake",
        nargs="?",
        const="all_red",
        choices=("all_red", "green_hallucinated"),
        help="离线机制验证：不调用真实模型",
    )
    parser.add_argument("--only", help="仅运行指定 case id（逗号分隔）")
    parser.add_argument(
        "--model",
        help="覆盖评分模型；默认沿用 .env 的 DEEPSEEK_MODEL",
    )
    parser.add_argument(
        "--retrieval",
        choices=("keyword", "semantic", "hybrid"),
        help="子句检索模式；默认沿用 RETRIEVAL_MODE（hybrid）",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=EVAL_TEMPERATURE,
        help=f"判定采样温度（默认 {EVAL_TEMPERATURE:g}，与线上一致；0 为可复现的贪心解码）",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="整套语料独立重复几轮并报轮间极差；判定是采样的，单轮不足以当基线（基线建议 3）",
    )
    parser.add_argument("--list", action="store_true", help="列出用例后退出")
    parser.add_argument(
        "--live-db",
        action="store_true",
        help="写入当前平台库（文档会出现在 UI 中）；默认使用隔离临时库",
    )
    args = parser.parse_args()

    cases = load_cases()
    if args.list:
        for case in cases:
            print(
                f"{case['id']:<12} {case['playbook_id']:<20} "
                f"缺陷 {len(case.get('defects', []))} · 缺口 {len(case.get('gaps', []))} · "
                f"合规 {len(case.get('clean', []))}  · {case['title']}"
            )
        return 0
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            print(f"未匹配到用例：{', '.join(sorted(wanted))}")
            return 1

    os.environ["EMAIL_ENABLED"] = "false"  # never mail the team during evaluation
    try:
        selected_model = _resolve_model(args.model)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[INFO] 评分模型：{selected_model} · 判定温度：{args.temperature:g}", flush=True)
    if args.retrieval:
        os.environ["RETRIEVAL_MODE"] = args.retrieval
    elif args.fake:
        # offline pass only validates the harness — don't pay for the embedder
        os.environ["RETRIEVAL_MODE"] = "keyword"
    if not args.live_db:
        _isolated_env()
        _reset_database()

    if args.repeat > 1 and args.live_db:
        print("[FAIL] --repeat 不能与 --live-db 同用：重复轮次会在内容去重闸上被拒。")
        return 1

    # A live pass is ~15 API calls per document; one dropped connection used to
    # discard every case that had already finished. Retry, and keep what works.
    attempts = 1 if (args.fake or args.live_db) else 3
    results: list[dict] = []
    passes: list[list[dict]] = []
    failed: list[str] = []
    for index in range(args.repeat):
        if index:
            # Same fixtures again, so the store has to be empty or pass two dies
            # on the duplicate-content guard instead of measuring anything.
            _fresh_isolated_database()
        if args.repeat > 1:
            print(f"[INFO] 第 {index + 1}/{args.repeat} 轮", flush=True)
        this_pass: list[dict] = []
        for case in cases:
            try:
                this_pass.append(
                    _run_case_with_retry(
                        case,
                        fake=args.fake,
                        attempts=attempts,
                        model=selected_model,
                        temperature=args.temperature,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — reported below, not re-raised
                print(f"[FAIL] {case['id']}：{_describe_error(exc)}", flush=True)
                if case["id"] not in failed:
                    failed.append(case["id"])
        results.extend(this_pass)
        passes.append(this_pass)
    if not results:
        print("所有用例均未完成，报告未写入。")
        return 1

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_markdown(results, fake=args.fake, failed=failed, passes=passes),
        encoding="utf-8",
    )
    METRICS_PATH.write_text(
        json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(), "fake": args.fake,
             "model": selected_model, "temperature": args.temperature,
             "retrieval_mode": os.environ.get("RETRIEVAL_MODE"),
             "failed_cases": failed,
             "overall": aggregate(results),
             "passes": [aggregate(items) for items in passes],
             "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    overall = aggregate(results)
    print(f"Wrote {REPORT_PATH}")
    if len(passes) > 1:
        rates = [
            value
            for value in (aggregate(items)["false_positive_rate"] for items in passes)
            if value is not None
        ]
        if len(rates) > 1:
            print(
                f"[INFO] 误报率逐轮：{' '.join(_pct(value) for value in rates)}"
                f" · 极差 {_pct(max(rates) - min(rates))}（单轮差值小于此数不可解读）"
            )
    print(
        f"缺陷召回 {_pct(overall['defect_recall'])} · 缺口召回 {_pct(overall['gap_recall'])} · "
        f"误报率 {_pct(overall['false_positive_rate'])} · 引用真实率 {_pct(overall['citation_genuineness'])} · "
        f"门禁拦截率 {_pct(overall['gate_catch_rate'])}"
    )
    print(
        f"模型 {', '.join(overall['models']) or '—'} · {overall['llm_calls']} 次调用 · "
        f"tokens {overall['total_tokens']} · 预估 {_money(overall['estimated_cost_usd'])}"
        f"（{overall['pricing_source']}）"
    )
    if failed:
        print(f"[WARN] 未完成用例：{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
