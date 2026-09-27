"""Pool documents from several runs of the *same* arm into one baseline.

A paid run writes its report only at the end, so a dropped call or an exhausted
balance destroys the verdicts already paid for. This re-assembles them: run the
missing documents again with ``--only`` and pool the parts here.

It refuses to pool runs that are not the same measurement (corpus, model,
temperature, retrieval arm, clause budget, fake mode), keeps the newest file for
any document two runs both contain, and records which part every document came
from — a pooled baseline has to be auditable or it is just a nicer-looking number.

    python scripts/pool_run_parts.py part1.json part2.json \
        --metrics-out pooled.json --report-out pooled.md

    # Parts collected before the budget was recorded, after checking each part's
    # own report line:
    python scripts/pool_run_parts.py part1.json --clause-budget 12000 ...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_seeded_defects import (  # noqa: E402
    aggregate,
    render_markdown,
)

SAME_MEASUREMENT_FIELDS = ("model", "temperature", "retrieval_arm", "fake")
# The whole-document arm and the shipping arm report the same retrieval_arm and
# differ *only* by this knob, so pooling across it would produce a baseline that
# answers no question at all.
BUDGET_FIELD = "clause_budget_chars"


def _portable(path: str) -> str:
    return Path(str(path).replace("\\", "/")).as_posix()


def _recorded_budgets(payloads: list[dict]) -> list[int]:
    """Clause budgets the parts actually recorded. Artifacts predating the field record none."""

    return sorted({
        payload[BUDGET_FIELD]
        for payload in payloads
        if payload.get(BUDGET_FIELD) is not None
    })


def _conflict(payloads: list[dict]) -> str | None:
    """Name the field on which two inputs disagree, or None if they are one measurement."""

    for field in SAME_MEASUREMENT_FIELDS:
        values = {json.dumps(payload.get(field), ensure_ascii=False) for payload in payloads}
        if len(values) > 1:
            return f"{field} 不一致：{' vs '.join(sorted(values))}"
    corpora = {_portable(payload.get("cases_file") or "") for payload in payloads}
    if len(corpora) > 1:
        return f"金标集不一致：{' vs '.join(sorted(corpora))}"
    # A part that predates the field is unknown, not a contradiction: refusing it
    # would throw away paid verdicts over a label that did not exist when they ran.
    budgets = _recorded_budgets(payloads)
    if len(budgets) > 1:
        return f"条款预算不一致：{' vs '.join(str(budget) for budget in budgets)} 字/规则"
    return None


def merge(parts: list[tuple[str, list[dict]]]) -> tuple[dict[str, dict], dict[str, str]]:
    """Case id → verdict block, plus where it came from. A later part wins."""

    merged: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for path, results in parts:
        for result in results:
            merged[result["case_id"]] = result
            origin[result["case_id"]] = path
    return merged, origin


def _pooled_from(paths: list[str], origin: dict[str, str]) -> dict[str, list[str]]:
    return {path: sorted(case for case, source in origin.items() if source == path) for path in paths}


def _missing_cases(cases_file: str, covered: set[str]) -> list[str]:
    path = PROJECT_ROOT / cases_file
    if not path.is_file():
        return []
    cases = json.loads(path.read_text(encoding="utf-8")).get("cases", [])
    return sorted(case["id"] for case in cases if case["id"] not in covered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("metrics", type=Path, nargs="+", help="同一臂的多次运行 metrics JSON")
    parser.add_argument("--metrics-out", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    parser.add_argument(
        "--clause-budget",
        type=int,
        default=None,
        help="各份运行自己没记条款预算时，人工核对后显式填入（字/规则）",
    )
    args = parser.parse_args()

    paths = [str(path) for path in args.metrics]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in args.metrics]

    conflict = _conflict(payloads)
    if conflict:
        print(f"[FAIL] 这些运行不是同一个测量，不能拼成一份基线：{conflict}", file=sys.stderr)
        return 1
    for path, payload in zip(paths, payloads):
        listed = [result["case_id"] for result in payload["results"]]
        repeated = sorted(case for case, count in Counter(listed).items() if count > 1)
        if repeated:
            print(
                f"[FAIL] {path} 里同一文档出现多次（{', '.join(repeated)}）：那是 --repeat 的多轮采样，"
                "按文档去重会把轮间噪声抹成一轮。请对缺的文档单轮补跑后再拼。",
                file=sys.stderr,
            )
            return 1

    first = payloads[0]
    budgets = _recorded_budgets(payloads)
    unlabelled = sum(1 for payload in payloads if payload.get(BUDGET_FIELD) is None)
    if args.clause_budget is not None and budgets and args.clause_budget not in budgets:
        print(
            f"[FAIL] --clause-budget {args.clause_budget} 与运行自己记录的预算 {budgets} 矛盾。",
            file=sys.stderr,
        )
        return 1
    runs_agree = len(budgets) == 1 and not unlabelled
    budget = budgets[0] if runs_agree else args.clause_budget
    budget_source = (
        None if budget is None else ("运行自记录" if runs_agree else "人工核对填入（各份运行早于该字段）")
    )
    merged, origin = merge(list(zip(paths, [payload["results"] for payload in payloads])))
    results = list(merged.values())
    covered = set(merged)
    failed = sorted(
        {case for payload in payloads for case in payload.get("failed_cases") or ()} - covered
    )
    overall = aggregate(results)
    pooled_from = _pooled_from(paths, origin)

    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fake": first.get("fake"),
        "model": first.get("model"),
        "temperature": first.get("temperature"),
        "retrieval_mode": first.get("retrieval_mode"),
        "retrieval_arm": first.get("retrieval_arm"),
        BUDGET_FIELD: budget,
        "clause_budget_source": budget_source,
        "cases_file": first.get("cases_file"),
        "pooled_from": pooled_from,
        "failed_cases": failed,
        "overall": overall,
        "passes": [overall],
        "results": results,
    }
    report = render_markdown(
        results,
        fake=first.get("fake"),
        failed=failed,
        passes=[results],
        retrieval_arm=first.get("retrieval_arm"),
        clause_budget=document[BUDGET_FIELD],
    )
    lines = ["", "## 本基线由多次运行拼合", ""]
    lines += [
        f"- `{path}`：贡献 {len(cases)} 份（{', '.join(cases) or '无'}）"
        for path, cases in pooled_from.items()
    ]
    lines.append("- 同名文档以更靠后的运行为准；每份文档的判定本身仍是单次采样。")
    if budget is not None and not runs_agree:
        lines.append(
            f"- 条款预算 {budget} 字/规则由人工核对各轮报告后填入："
            f"{unlabelled}/{len(paths)} 份运行早于该字段，自身没有记录。"
        )
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(report + "\n".join(lines) + "\n", encoding="utf-8")
    args.metrics_out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"[OK] 合并 {len(covered)} 份文档 / {overall['rules_scored']} 条判定 → {args.metrics_out}")
    print(f"[OK] 报告 → {args.report_out}")
    if budget is None:
        print(
            f"[WARN] {unlabelled}/{len(paths)} 份运行未记录条款预算，拼合基线只能记为未知；已记录的部分为 "
            f"{budgets[0] if budgets else '—'} 字/规则。两臂混拼无法从文件本身排除，请人工核对各轮报告"
            "（报告里的「本轮检索工作点」行）后用 --clause-budget 填入。"
        )
    elif runs_agree:
        print(f"[OK] 条款预算同为 {budget} 字/规则（各份运行自记录）")
    else:
        print(f"[OK] 条款预算 {budget} 字/规则（人工核对填入，{unlabelled} 份运行本身未记录）")
    if failed:
        print(f"[WARN] 仍标记为未完成：{', '.join(failed)}")
    gaps = _missing_cases(str(document.get("cases_file") or ""), covered)
    if gaps:
        print(f"[WARN] 金标集里有 {len(gaps)} 份文档没有任何判定：{', '.join(gaps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
