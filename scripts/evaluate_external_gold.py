"""External gold: score the engine against CUAD's lawyer-made annotations.

Why this exists: the seeded-defect corpus is synthetic and labelled by us, which
makes it self-referential, and every one of its documents is short enough to fit
the clause-retrieval budget — so it never measured retrieval either. CUAD is 510
real commercial agreements whose relevant clauses were highlighted by legal
professionals (Hendrycks et al., NeurIPS 2021), and 462 of them are *longer* than
our retrieval budget. That makes it an independent, adversarial check of the two
things we cannot check ourselves.

What it can and cannot say. CUAD records presence and location, never severity,
so this run measures:

- localization_recall : when experts highlighted the clause, did our citation on
                        the mapped rule land on one of their spans?
- presence_agreement  : when experts found it, did we wrongly claim absence?
- absence_agreement   : when experts found nothing, did we correctly report a gap?
- over_claim          : citations we produced for content experts say is absent

Severity agreement (our red/amber/green cut) is deliberately out of scope.

Data: ``data/external/cuad-data.zip`` (CUADv1.json). The CUAD repository carries
no license file, so this corpus is for internal evaluation only — never
redistributed, never bundled into a release.

Usage: ``python scripts/evaluate_external_gold.py --count 8``
Output: ``reports/EXTERNAL_GOLD_REPORT.md`` (+ metrics JSON beside it).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_seeded_defects import (  # noqa: E402  (shared harness helpers)
    AllRedFakeLLM,
    HallucinatedGreenFakeLLM,
    _describe_error,
    _engine_run_meta,
    _isolated_env,
    _money,
    _pct,
    _read_verdicts,
    _reset_database,
    _resolve_model,
    _tracked_llm,
    _usage_summary,
)
from backend.config import read_retrieval_mode  # noqa: E402

CUAD_ZIP = PROJECT_ROOT / "data" / "external" / "cuad-data.zip"
CUAD_MEMBER = "CUADv1.json"
MAPPING_PATH = PROJECT_ROOT / "eval_cases" / "cuad_rule_map.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "EXTERNAL_GOLD_REPORT.md"
METRICS_PATH = PROJECT_ROOT / "reports" / "EXTERNAL_GOLD_METRICS.json"
MECHANICS_REPORT_PATH = PROJECT_ROOT / "reports" / "EXTERNAL_GOLD_MECHANICS.md"
MECHANICS_METRICS_PATH = PROJECT_ROOT / "reports" / "EXTERNAL_GOLD_MECHANICS.json"

# What one rule gets to see; imported so this can never drift from the engine.
from backend.engine.retrieval import MAX_RULE_CONTEXT_CHARS, MIN_TOP  # noqa: E402

RETRIEVAL_BUDGET_CHARS = MAX_RULE_CONTEXT_CHARS

# CUAD has no short-but-rich contracts: only 48 of 510 fit one retrieval budget,
# and none of those covers more than 2 of our mapped rules. So the honest control
# is not "under budget vs over budget" (everything interesting is over) but
# moderately long vs very long, split at four budgets' worth of text.
BAND_EDGE_CHARS = RETRIEVAL_BUDGET_CHARS * 4
BAND_UNDER = f"≤{BAND_EDGE_CHARS // 1000}k 字符"
BAND_OVER = f">{BAND_EDGE_CHARS // 1000}k 字符"


def length_band(chars: int) -> str:
    return BAND_OVER if chars > BAND_EDGE_CHARS else BAND_UNDER


# ---------------------------------------------------------------------------- gold


def load_mapping(path: Path = MAPPING_PATH) -> dict:
    mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    playbook_id = mapping["playbook_id"]

    from backend.playbooks import get_playbook

    rule_names = {seed["name"] for seed in get_playbook(playbook_id).rule_seeds}
    unknown = sorted(set(mapping["rules"]) - rule_names)
    if unknown:
        raise ValueError(f"映射表引用了剧本 {playbook_id} 中不存在的规则：{', '.join(unknown)}")
    return mapping


def load_contracts(zip_path: Path = CUAD_ZIP) -> list[dict]:
    """CUAD contracts with their expert-highlighted spans, keyed by category."""

    if not Path(zip_path).is_file():
        raise FileNotFoundError(
            f"缺少 {zip_path}。取数：curl -o data/external/cuad-data.zip "
            "https://raw.githubusercontent.com/The-Atticus-Project/cuad/main/data.zip"
        )
    with zipfile.ZipFile(zip_path) as archive:
        payload = json.loads(archive.read(CUAD_MEMBER))
    contracts = []
    for item in payload["data"]:
        paragraph = item["paragraphs"][0]
        labels: dict[str, list[str]] = {}
        for qa in paragraph["qas"]:
            category = qa["id"].split("__", 1)[1]
            labels[category] = [answer["text"] for answer in qa["answers"]]
        contracts.append({"title": item["title"], "text": paragraph["context"], "labels": labels})
    return contracts


def gold_for_contract(contract: dict, mapping: dict) -> dict[str, dict]:
    """rule name → {"spans", "categories"} for the categories mapped to it."""

    gold = {}
    for rule, spec in mapping["rules"].items():
        categories = spec["cuad_categories"]
        missing = [c for c in categories if c not in contract["labels"]]
        if missing:
            raise ValueError(
                f"{contract['title']}：CUAD 标注里没有类别 {', '.join(missing)}（映射表写错了？）"
            )
        spans = [span for category in categories for span in contract["labels"][category]]
        gold[rule] = {"categories": categories, "spans": spans}
    return gold


def select_contracts(
    contracts: list[dict],
    mapping: dict,
    *,
    count: int,
    min_chars: int,
    max_chars: int,
) -> list[dict]:
    """Deterministic sample: half the rich contracts under BAND_EDGE_CHARS, half over.

    Length-bounded on purpose — a 300k-character agreement is a legitimate CUAD
    document but turns this into an embedding-throughput test rather than a
    review-quality test. Stratifying over the band edge is what makes the
    localization number interpretable: one recall figure for every contract is
    mostly a statement about how much text a single rule gets to read.
    """

    bands: dict[str, list[dict]] = {}
    for contract in contracts:
        size = len(contract["text"])
        if not min_chars <= size <= max_chars:
            continue
        gold = gold_for_contract(contract, mapping)
        present_rules = sum(1 for entry in gold.values() if entry["spans"])
        present_spans = sum(len(entry["spans"]) for entry in gold.values())
        contract = {**contract, "_coverage": (present_rules, present_spans)}
        bands.setdefault(length_band(size), []).append(contract)

    for rows in bands.values():
        rows.sort(key=lambda row: (-row["_coverage"][0], -row["_coverage"][1], row["title"]))

    picked: list[dict] = []
    quota = {BAND_UNDER: count - count // 2, BAND_OVER: count // 2}
    for band in (BAND_UNDER, BAND_OVER):
        picked.extend(bands.get(band, [])[: quota[band]])
    # Backfill whatever a thin band could not supply from the other band.
    if len(picked) < count:
        taken = {row["title"] for row in picked}
        remaining = [
            row
            for band in (BAND_UNDER, BAND_OVER)
            for row in bands.get(band, [])
            if row["title"] not in taken
        ]
        picked.extend(remaining[: count - len(picked)])
    return [{k: v for k, v in row.items() if k != "_coverage"} for row in picked]


# ------------------------------------------------------------------------ scoring


def score_rule(name: str, gold: dict, verdict: dict) -> dict:
    """One rule's agreement with the expert annotation for one contract."""

    from backend.gold_metrics import span_recall

    spans = gold["spans"]
    citations = [citation.get("quote", "") for citation in (verdict["citations"] or [])]
    claims_absence = bool((verdict["gap_reason"] or "").strip()) and not citations
    hits, total = span_recall(spans, citations)
    present = total > 0
    return {
        "rule": name,
        "categories": gold["categories"],
        "expert_present": present,
        "expert_spans": total,
        "cited_spans_hit": hits,
        "localization_recall": round(hits / total, 4) if total else None,
        "engine_rating": verdict["rating"],
        "engine_claims_absence": claims_absence,
        "citations_stored": len(citations),
        # present  → we must not claim absence; ideally we land on an expert span
        # absent   → claiming absence is the correct answer; citing something is over-claim
        "agreement": (not claims_absence) if present else claims_absence,
        "over_claim": (not present) and bool(citations),
    }


def score_contract(contract: dict, mapping: dict, rows: list[dict]) -> dict:
    gold = gold_for_contract(contract, mapping)
    by_name = {row["rule_name"]: row for row in rows}
    details = [
        score_rule(name, gold[name], by_name[name]) for name in sorted(gold) if name in by_name
    ]
    unjudged = sorted(set(gold) - set(by_name))
    present = [d for d in details if d["expert_present"]]
    absent = [d for d in details if not d["expert_present"]]
    spans_total = sum(d["expert_spans"] for d in present)
    spans_hit = sum(d["cited_spans_hit"] for d in present)
    return {
        "title": contract["title"],
        "chars": len(contract["text"]),
        "length_band": length_band(len(contract["text"])),
        "retrieval_limited": len(contract["text"]) > RETRIEVAL_BUDGET_CHARS,
        "rules": details,
        "unjudged_rules": unjudged,
        "present_rules": len(present),
        "agreed_present": sum(1 for d in present if d["agreement"]),
        "absent_rules": len(absent),
        "agreed_absent": sum(1 for d in absent if d["agreement"]),
        "over_claims": sum(1 for d in details if d["over_claim"]),
        "spans_total": spans_total,
        "spans_hit": spans_hit,
        "citations_stored": sum(d["citations_stored"] for d in details),
    }


def aggregate(results: list[dict]) -> dict:
    present = sum(r["present_rules"] for r in results)
    absent = sum(r["absent_rules"] for r in results)
    spans_total = sum(r["spans_total"] for r in results)
    spans_hit = sum(r["spans_hit"] for r in results)
    usages = [r["usage"] for r in results]

    bands: dict[str, dict] = {}
    for row in results:
        band = bands.setdefault(
            row["length_band"], {"contracts": 0, "spans_total": 0, "spans_hit": 0}
        )
        band["contracts"] += 1
        band["spans_total"] += row["spans_total"]
        band["spans_hit"] += row["spans_hit"]
    for band in bands.values():
        band["localization_recall"] = _ratio(band["spans_hit"], band["spans_total"])

    return {
        "contracts": len(results),
        "rules_scored": present + absent,
        "expert_spans": spans_total,
        "spans_hit": spans_hit,
        "localization_recall": _ratio(spans_hit, spans_total),
        "presence_agreement": _ratio(sum(r["agreed_present"] for r in results), present),
        "absence_agreement": _ratio(sum(r["agreed_absent"] for r in results), absent),
        "over_claim_rules": sum(r["over_claims"] for r in results),
        "citations_stored": sum(r["citations_stored"] for r in results),
        "retrieval_limited_contracts": sum(1 for r in results if r["retrieval_limited"]),
        "retrieval_modes": {
            mode: sum(1 for r in results if r["retrieval_mode"] == mode)
            for mode in sorted({r["retrieval_mode"] for r in results})
        },
        "localization_recall_by_band": bands,
        "total_latency_ms": sum(r["latency_ms"] for r in results),
        "spans_hit_text": f"{spans_hit}/{spans_total}",
        "presence_text": f"{sum(r['agreed_present'] for r in results)}/{present}",
        "absence_text": f"{sum(r['agreed_absent'] for r in results)}/{absent}",
        "llm_calls": sum(u.get("calls", 0) for u in usages),
        "tokens": sum(int(u.get("total_tokens") or 0) for u in usages),
        "token_source": usages[0].get("token_source") if usages else None,
        "cost_usd": round(sum(float(u.get("estimated_cost_usd") or 0) for u in usages), 6),
    }


# ------------------------------------------------------------------------- engine


def run_contract(contract: dict, *, model: str, fake: str | None = None) -> dict:
    from backend.documents.service import create_document
    from backend.engine.orchestrator import run_review
    from backend.platform_db import platform_session, seed_playbook_rules

    mapping = load_mapping()
    playbook_id = mapping["playbook_id"]
    workdir = Path(tempfile.mkdtemp(prefix="rulebook-cuad-"))
    source = workdir / f"{contract['title'][:60].replace('/', '_')}.txt"
    source.write_text(contract["text"], encoding="utf-8")

    session = platform_session()
    try:
        seed_playbook_rules(session, playbook_id)
        document, created = create_document(
            session,
            playbook_id=playbook_id,
            filename=source.name,
            content=source.read_bytes(),
            actor="external-gold",
        )
        document_id = document.id
        friendly_id = document.friendly_id
    finally:
        session.close()
    if not created:
        raise RuntimeError(f"{contract['title']}：内容已存在于隔离库，未重跑")

    if fake == "all_red":
        inner = AllRedFakeLLM()
    elif fake == "green_hallucinated":
        inner = HallucinatedGreenFakeLLM()
    else:
        from backend.llm_service import build_llm

        inner = build_llm(model=model)
    llm = _tracked_llm(inner, model)
    started = time.perf_counter()
    run_review(document_id, trigger="external-gold", custom_llm=llm)
    wall_ms = int((time.perf_counter() - started) * 1000)

    rows, _ = _read_verdicts(document_id)
    meta = _engine_run_meta(document_id)
    scored = score_contract(contract, mapping, rows)
    scored.update(
        {
            "friendly_id": friendly_id,
            "latency_ms": int(meta.get("latency_ms") or wall_ms),
            "retrieval_mode": meta.get("retrieval_mode") or "unknown",
            "run_status": meta.get("run_status"),
            "usage": _usage_summary(llm, model),
            "verdicts": len(rows),
        }
    )
    return scored


# ------------------------------------------------------------------------- report


def render_markdown(results: list[dict], *, model: str, failed: list[str], fake: str | None = None) -> str:
    overall = aggregate(results)
    scored_by = f"MechanicsFake · {fake}" if fake else f"**{model}**"
    lines = [
        "# 外部专家金标对照（CUAD）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"评分模型：{scored_by}",
        f"条款检索：{ _format_modes(overall['retrieval_modes']) }"
        f"（单条规则最多可见 {RETRIEVAL_BUDGET_CHARS} 字符）",
        f"语料：CUAD v1（510 份真实商业协议，律师标注）中取 {overall['contracts']} 份",
        f"映射规则：{len(load_mapping()['rules'])} 条 contract-compliance 规则；"
        "CUAD 只标「有没有、在哪一段」，不含严重度，因此本报告不评判红黄绿档位",
        "",
    ]
    if failed:
        lines += [f"**未完成用例：{', '.join(failed)}** — 下列指标只覆盖跑完的文档。", ""]
    lines += [
        "## 总体",
        "",
        "| 指标 | 数值 | 命中/总数 | 含义 |",
        "|---|---:|---|---|",
        f"| 定位召回 | {_pct(overall['localization_recall'])} | "
        f"{overall['spans_hit_text']} | 专家标出的片段中，被我们引用命中的比例 |",
        f"| 存在即不称缺 | {_pct(overall['presence_agreement'])} | {overall['presence_text']} "
        "| 专家找到该条款时，我们没有声称文档缺失 |",
        f"| 缺失判定一致 | {_pct(overall['absence_agreement'])} | {overall['absence_text']} "
        "| 专家没找到时，我们也如实报告缺失 |",
        f"| 无中生有引用 | {overall['over_claim_rules']} | — "
        "| 专家认定不存在，我们却引用了内容（幻觉/过度报警信号） |",
        f"| 检索受限的文档 | {overall['retrieval_limited_contracts']}/{overall['contracts']} "
        f"| 正文 > {RETRIEVAL_BUDGET_CHARS} 字符 | 单条规则看不全整份合同 |",
    ]
    for band_name, band in sorted(overall["localization_recall_by_band"].items()):
        lines.append(
            f"| 定位召回 · {band_name} | {_pct(band['localization_recall'])} | "
            f"{band['spans_hit']}/{band['spans_total']}（{band['contracts']} 份） "
            "| 长度对定位能力的直接影响 |"
        )
    lines += [
        f"| 时延 | {overall['total_latency_ms']} ms | {overall['contracts']} docs | 引擎端到端 |",
        f"| 成本 | {_money(overall['cost_usd'])} | {overall['llm_calls']} 次调用 | tokens "
        f"{overall['tokens']}（{overall['token_source']}） |",
        "",
        "## 逐份",
        "",
        "| 合同 | 字符 | 规则 | 定位召回 | 存在一致 | 缺失一致 | 无中生有 | 引用 | 判定数 | 时延 | 预估 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['title'][:34]} | {result['chars']} | {len(result['rules'])} "
            f"| {_pct(_ratio(result['spans_hit'], result['spans_total']))} "
            f"| {result['agreed_present']}/{result['present_rules']} "
            f"| {result['agreed_absent']}/{result['absent_rules']} "
            f"| {result['over_claims']} | {result['citations_stored']} | {result['verdicts']} "
            f"| {result['latency_ms']} ms | {_money(result['usage'].get('estimated_cost_usd'))} |"
        )

    lines += ["", "## 逐条不一致明细", ""]
    misses = [
        (result["title"], detail)
        for result in results
        for detail in result["rules"]
        if not detail["agreement"] or detail["over_claim"]
    ]
    if not misses:
        lines.append("全部一致：专家找到时我们没称缺失，专家没找到时我们也如实报告。")
    for title, detail in misses:
        kind = "无中生有" if detail["over_claim"] else ("谎称缺失" if detail["expert_present"] else "该缺未报")
        lines.append(
            f"- **{kind}** {title[:40]} · {detail['rule']}"
            f"（{'/'.join(detail['categories'])}）：引擎判 {detail['engine_rating']}、"
            f"引用 {detail['citations_stored']} 条、"
            f"命中专家片段 {detail['cited_spans_hit']}/{detail['expert_spans']}"
        )
    lines += [
        "",
        "## 说明与局限",
        "",
        "- **语言与剧本不匹配**：我们的规则是中文合同规则，CUAD 全是英文 SEC 协议。keyword 检索的"
        "中英跨语言重叠接近随机，因此本报告的定位召回衡量的是「这套规则配这份语料」，不能直接读成"
        "「引擎在真实评审中定位能力差」。要得到可用结论，需换用语料同语言的剧本，或让语义检索真正可用。",
        f"- **检索兜底会制造引用**：`select_clauses` 无论如何都至少返回 {MIN_TOP} 段条款（哪怕相关度为 0），"
        "模型只能从这些填充段落里挑，所以「专家没找到但引擎引用了」是结构性结果，"
        "缺失判定一致率为 0 首先指向这个下限设计，其次才是模型幻觉。",
        "- CUAD 的类别与我们的规则不是同一套语义，映射表见 `eval_cases/cuad_rule_map.json`，"
        "其中「定价清晰度」「违约补偿条款」是近似映射，读数时应降权看待。",
        "- 定位命中按归一化后的最长公共片段判定（≥60 字符），因为 CUAD 的偏移基于另一条文本提取链。",
        "- 一份合同里同一规则可能对应多个 CUAD 类别（例如责任上限既有 Cap 也有 Uncapped），"
        "命中任一专家片段即算该规则定位成功。",
        "- 时延包含嵌入模型联网重试的等待，网络不通时该列会显著虚高，不要用于性能结论。",
        "- CUAD 仓库未附 LICENSE 文件，本语料仅限内部评测，不得再分发或并入发布物。",
        "- 本脚本强制 `EMAIL_ENABLED=false`，评测运行不会给团队外发通知邮件。",
        "",
    ]
    return "\n".join(lines)


def _ratio(numerator: int, denominator: int):
    return round(numerator / denominator, 4) if denominator else None


def _format_modes(modes: dict[str, int]) -> str:
    """Describe what retrieval actually ran, flagging any silent fallback."""

    rendered = " + ".join(f"{mode}×{count}" for mode, count in modes.items()) or "未知"
    requested = read_retrieval_mode()
    if len(modes) > 1 or (modes and requested not in modes):
        return f"{rendered}（请求 {requested}，实际已降级——嵌入模型不可达时引擎回落到 keyword）"
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=8, help="评阅的 CUAD 合同份数（默认 8）")
    parser.add_argument("--model", help="评分模型；默认沿用 .env 的 DEEPSEEK_MODEL")
    parser.add_argument(
        "--retrieval",
        choices=("keyword", "semantic", "hybrid"),
        help="条款检索模式；默认沿用 RETRIEVAL_MODE",
    )
    parser.add_argument("--min-chars", type=int, default=8000, help="入选合同的最小正文字符")
    parser.add_argument("--max-chars", type=int, default=60000, help="入选合同的最大正文字符")
    parser.add_argument(
        "--fake",
        choices=("all_red", "green_hallucinated"),
        help="用确定性假模型跑通链路（零 API 调用、零花费），只验机制不出结论",
    )
    parser.add_argument("--list", action="store_true", help="打印选中的合同后退出")
    args = parser.parse_args()

    os.environ["EMAIL_ENABLED"] = "false"
    if args.retrieval:
        os.environ["RETRIEVAL_MODE"] = args.retrieval

    report_path, metrics_path = (
        (MECHANICS_REPORT_PATH, MECHANICS_METRICS_PATH)
        if args.fake
        else (REPORT_PATH, METRICS_PATH)
    )

    mapping = load_mapping()
    contracts = load_contracts()
    picked = select_contracts(
        contracts, mapping, count=args.count, min_chars=args.min_chars, max_chars=args.max_chars
    )
    if args.list:
        for contract in picked:
            print(f"{len(contract['text']):>7}  {contract['title']}")
        return 0
    if not picked:
        print(f"没有合同落在 {args.min_chars}-{args.max_chars} 字符区间内。")
        return 1

    try:
        model = _resolve_model(args.model)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[INFO] 评分模型：{model}；候选 {len(picked)} 份 CUAD 合同", flush=True)
    if not args.fake:
        # Spell out the spend before the first paid call, not after the report.
        from backend.playbooks import get_playbook

        rules = len(get_playbook(mapping["playbook_id"]).rule_seeds)
        print(
            f"[INFO] 预计约 {len(picked)} × {rules} ≈ {len(picked) * rules} 次 {model} 调用；"
            f"如需零花费的先跑 --fake all_red",
            flush=True,
        )

    _isolated_env()
    _reset_database()

    results: list[dict] = []
    failed: list[str] = []
    for contract in picked:
        try:
            results.append(run_contract(contract, model=model, fake=args.fake))
            print(f"[OK] {contract['title'][:50]} — {results[-1]['verdicts']} 条判定", flush=True)
        except Exception as exc:  # noqa: BLE001 — one bad contract must not eat the run
            print(f"[FAIL] {contract['title'][:50]}：{_describe_error(exc)}", flush=True)
            failed.append(contract["title"])
    if not results:
        print("所有合同均未完成，报告未写入。")
        return 1

    overall = aggregate(results)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_markdown(results, model=model, failed=failed, fake=args.fake), encoding="utf-8"
    )
    metrics_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "fake": args.fake,
                "failed_contracts": failed,
                "overall": overall,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {report_path}")
    cost_note = "（估算，未实际调用）" if overall["token_source"] != "api" else ""
    print(
        f"定位召回 {_pct(overall['localization_recall'])} · 存在一致 {_pct(overall['presence_agreement'])} · "
        f"缺失一致 {_pct(overall['absence_agreement'])} · 无中生有 {overall['over_claim_rules']} 条 · "
        f"成本 {_money(overall['cost_usd'])}{cost_note}"
    )
    band_text = " · ".join(
        f"{name} 定位召回 {_pct(band['localization_recall'])}"
        for name, band in sorted(overall["localization_recall_by_band"].items())
    )
    print(
        f"检索受限 {overall['retrieval_limited_contracts']}/{overall['contracts']} 份："
        f"{band_text}"
    )
    if failed:
        print(f"[WARN] 未完成：{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
