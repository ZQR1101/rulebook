"""Score a blind general-agent review against the seeded-defect gold set.

The competing agent runs in its own workspace (outside this repo) and is given
the same documents and the same rule text our engine's prompt carries, but no
labels. It writes ``out/DOC-*.json``; this script feeds those verdict rows into
the *same* :func:`score_case` / :func:`aggregate` the engine is scored with, so
the two arms share one yardstick, and prints the comparison against the
committed engine baseline.

Read-only apart from the report it writes. No model calls.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MAP = PROJECT_ROOT / "eval_cases" / "blind_arm_map.json"
DEFAULT_CASES = PROJECT_ROOT / "eval_cases" / "seeded_defect_cases.json"
DEFAULT_BASELINE = PROJECT_ROOT / "reports" / "SEEDED_DEFECT_EVAL_METRICS.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "BLIND_ARM_EVAL_REPORT.md"
METRICS_PATH = PROJECT_ROOT / "reports" / "BLIND_ARM_EVAL_METRICS.json"

ARM_ID = {"DOC-A": "通用 agent A", "DOC-B": "通用 agent B", "DOC-C": "通用 agent C", "DOC-D": "通用 agent D"}


def _load_harness():
    """Import scripts/evaluate_seeded_defects.py as a module (scripts/ is not a package)."""

    path = PROJECT_ROOT / "scripts" / "evaluate_seeded_defects.py"
    spec = importlib.util.spec_from_file_location("seeded_defects_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc_label(name: str) -> str:
    return Path(name).stem


def check_coverage(gold: dict, rows: list[dict], label: str) -> list[str]:
    """Report rules the agent dropped or invented.

    ``score_case`` pools its denominators over the rows it is given, so an
    unreported rule would silently shrink the denominator and flatter the arm.
    """

    reported = [row["rule_name"] for row in rows]
    problems = []
    missing = sorted(set(gold) - set(reported))
    extra = sorted(set(reported) - set(gold))
    if missing:
        problems.append(f"{label} 缺少规则判定：{'、'.join(missing)}")
    if extra:
        problems.append(f"{label} 出现了金标集之外的规则名：{'、'.join(extra)}")
    duplicates = {name for name in reported if reported.count(name) > 1}
    if duplicates:
        problems.append(f"{label} 规则重复：{'、'.join(sorted(duplicates))}")
    return problems


def score_document(harness, arm_dir: Path, doc_name: str, case: dict) -> tuple[dict, dict]:
    from backend.engine.citation_gate import validate_citations

    text = (arm_dir / "docs" / f"{doc_name}.txt").read_text(encoding="utf-8")
    raw_rows = json.loads((arm_dir / "out" / f"{doc_name}.json").read_text(encoding="utf-8"))

    rows, emissions = [], {}
    for item in raw_rows:
        name = str(item.get("rule") or "").strip()
        citations = item.get("citations") if isinstance(item.get("citations"), list) else []
        valid, invalid = validate_citations(citations, text)
        rows.append(
            {
                "rule_name": name,
                "dimension": item.get("dimension") or "",
                "rating": str(item.get("rating") or "").strip().lower(),
                "rationale": item.get("rationale") or "",
                "gap_reason": item.get("gap_reason") or "",
                "citations": valid,
                "review_state": "agent_output",
            }
        )
        emissions[name] = {
            "emitted_rating": str(item.get("rating") or "").strip().lower(),
            "emitted_citations": citations,
            "valid_citations": valid,
            "invalid_citations": invalid,
        }

    gold = harness.gold_index(case)
    metrics = harness.score_case(gold, rows, emissions)
    result = {
        "case_id": case["id"],
        "arm": ARM_ID.get(doc_name, doc_name),
        "document": f"docs/{doc_name}.txt",
        "friendly_id": doc_name,
        "playbook_id": case["playbook_id"],
        "latency_ms": 0,
        "usage": {},
        "metrics": metrics,
    }
    return result, check_coverage(gold, rows, doc_name)


def render_markdown(
    results: list[dict], pooled: dict, baseline: dict, problems: list[str], wall_minutes: float | None = None
) -> str:
    engine_passes = baseline.get("passes") or []
    lines = [
        "# 通用 agent 盲测对照报告（Seeded-Defect Blind Arm）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"被测：一个通用 agent 会话（独立工作区，位于本仓库之外），零定制、零标签。",
        f"裁判：`scripts/evaluate_seeded_defects.py` 的 `score_case` / `aggregate`，与引擎基线同一把尺。",
        f"引擎基线：`{baseline.get('generated_at')}`（{len(engine_passes)} 轮 · {baseline.get('model')} · 温度 {baseline.get('temperature')}）。",
        "",
    ]
    if problems:
        lines += ["## 结构性问题（必须先修，否则分数不可用）", ""]
        lines += [f"- {problem}" for problem in problems]
        lines.append("")

    lines += [
        "## 两臂对照",
        "",
        "| 指标 | 引擎基线（3 轮均值 / 极差） | 通用 agent（本轮） |",
        "|---|---|---|",
    ]
    rows = (
        ("defect_recall", "缺陷召回"),
        ("gap_recall", "缺口召回"),
        ("false_positive_rate", "误报率"),
        ("exact_agreement", "整体等级一致率"),
        ("citation_genuineness", "引用真实率"),
        ("defect_exact", "缺陷等级一致"),
    )
    for key, label in rows:
        agent_value = _pct(pooled.get(key))
        spread = _spread(engine_passes, key)
        lines.append(f"| {label} | {spread} | {agent_value} |")
    engine_overall = baseline.get("overall") or {}
    engine_gate_denominator = engine_overall.get("unsupported_green_total") or 0
    lines.append(
        "| 引用门拦截率 | {}（分母 {}） | 不适用：无闸门，无据绿色 {} 条原样留存 |".format(
            _pct(engine_overall.get("gate_catch_rate")),
            engine_gate_denominator,
            pooled.get("unsupported_green_total", 0),
        )
    )
    if wall_minutes:
        engine_pass_minutes = (engine_overall.get("total_latency_ms") or 0) / 1000 / 60 / max(len(engine_passes), 1)
        lines.append(f"| 单轮整批墙钟（4 份文档） | ≈ {engine_pass_minutes:.1f} 分钟 | ≈ {wall_minutes:.1f} 分钟 |")
    lines.append(f"| 成本 | 引擎 3 轮 ${baseline_cost(baseline)} | 不可比：档位与 token 不由评测侧掌握 |")
    lines += ["", f"本轮通用 agent 共判定 {pooled.get('rules_scored')} 条规则，"
                  f"发出引用 {pooled.get('citations_emitted')} 条，其中逐字可回溯 "
                  f"{(pooled.get('citations_emitted') or 0) - (pooled.get('citations_invalid') or 0)} 条。", ""]

    lines += ["## 与引擎不一致的判定", ""]
    misses = _misses(results)
    if not misses:
        lines.append("- 无")
    lines += misses
    lines += [
        "",
        "## 说明与局限",
        "",
        "- 语料改动只有一处：正文首行的「（合成评测样例）」标注已删除，文件名改为 DOC-A…DOC-D。"
        "引用逐字校验因此针对仓库外工作区 `docs/` 下的副本，映射见 `eval_cases/blind_arm_map.json`。",
        "- 尺子是本仓库自标注的金标集：通用 agent 若用另一套同样合理的口径，会被记成漏报或误报。"
        "本对照回答的是「同样的信息，零定制的 agent 能到什么程度」，不是「专家会不会同意」。",
        "- 单轮读数。判定温度与模型档位均未受控，不构成测量；要出结论需 3 轮独立盲测并报极差。",
        "- 引擎臂每份文档得到 15 次「一次一条规则」的独立提问；通用 agent 一次拿到全部规则。"
        "这正是被测变量之一，不要把两者的差距全部归给「模型能力」。",
    ]
    return "\n".join(lines) + "\n"


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _spread(passes: list[dict], key: str) -> str:
    values = [p.get(key) for p in passes if p.get(key) is not None]
    if not values:
        return "—"
    mean = sum(values) / len(values)
    spread = (max(values) - min(values)) * 100
    return f"{_pct(mean)} / 极差 {spread:.1f}%"


def baseline_cost(baseline: dict) -> str:
    overall = baseline.get("overall") or {}
    return f"{overall.get('estimated_cost_usd') or 0:.4f}"


def _misses(results: list[dict]) -> list[str]:
    lines = []
    for result in results:
        for detail in result["metrics"]["details"]:
            kind, expected, actual = detail["kind"], detail["expected"], detail["actual"]
            if kind in ("defect", "gap") and not detail["flagged"]:
                label = "漏报"
            elif kind == "gap" and not detail["gap_recalled"]:
                label = "判红但未说明缺失"
            elif kind == "clean" and detail["flagged"]:
                label = "误报"
            elif not detail["exact"]:
                label = "档位偏移"
            else:
                continue
            lines.append(
                f"- **{label}** {result['friendly_id']} ← {result['case_id']} · {detail['rule']}"
                f"（{kind} 预期 {expected} → 实判 {actual}）"
            )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="用同一把尺给通用 agent 的盲测输出打分")
    parser.add_argument("--arm-dir", type=Path, required=True, help="仓库外的盲测工作区（含 docs/ 与 out/）")
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--metrics", type=Path, default=METRICS_PATH)
    parser.add_argument(
        "--wall-minutes",
        type=float,
        default=None,
        help="盲测会话从发起到最后一次写入的分钟数（该臂无用量记录，只能人工回填）",
    )
    args = parser.parse_args()

    harness = _load_harness()
    mapping = {
        _doc_label(key): value
        for key, value in json.loads(args.map.read_text(encoding="utf-8")).items()
        if not key.startswith("_")
    }
    cases = {case["document"]: case for case in json.loads(args.cases.read_text(encoding="utf-8"))["cases"]}

    import os
    os.environ["EMAIL_ENABLED"] = "false"
    os.chdir(PROJECT_ROOT)

    results, problems = [], []
    for doc_name, fixture in sorted(mapping.items()):
        case = cases.get(fixture)
        if case is None:
            problems.append(f"{doc_name}：映射指向 {fixture}，但金标集里没有这个 case")
            continue
        result, doc_problems = score_document(harness, args.arm_dir, doc_name, case)
        problems += doc_problems
        results.append(result)

    pooled = harness.aggregate(results)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    report = render_markdown(results, pooled, baseline, problems, args.wall_minutes)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8", newline="\n")
    args.metrics.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "engine_baseline": baseline.get("generated_at"),
                "arm": "general-agent-blind",
                "wall_minutes": args.wall_minutes,
                "wall_minutes_source": "operator entry (this arm emits no usage record)",
                "overall": pooled,
                "results": results,
                "problems": problems,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )

    print(f"Wrote {args.report}")
    if problems:
        print("[ERROR] 结构性问题：")
        for problem in problems:
            print("  -", problem)
        return 1
    print(
        "缺陷召回 {dr} · 缺口召回 {gr} · 误报率 {fp} · 引用真实率 {cg} · 等级一致 {ea}".format(
            dr=_pct(pooled["defect_recall"]),
            gr=_pct(pooled["gap_recall"]),
            fp=_pct(pooled["false_positive_rate"]),
            cg=_pct(pooled["citation_genuineness"]),
            ea=_pct(pooled["exact_agreement"]),
        )
    )
    for result in results:
        metrics = result["metrics"]
        flagged = [
            f"{d['rule']}({d['kind']} 预期{d['expected']}/实判{d['actual']})"
            for d in metrics["details"]
            if not d["exact"] or (d["kind"] == "gap" and not d["gap_recalled"])
        ]
        print(f"  {result['friendly_id']} ← {result['case_id']}: " + ("；".join(flagged) or "全部精确命中"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
