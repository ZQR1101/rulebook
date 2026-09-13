"""Clause retrieval evaluation: keyword vs semantic vs hybrid selection.

Supports two case-file shapes:
- single-document (legacy ``retrieval_cases.json``: "document" + top-level "cases")
- multi-document (``retrieval_hard_cases.json``: "documents": [{path, playbook_id, cases}])

Metric per case: selection hit rate — the expected clause ordinal appears in
the selected set under the production character budget. Uses the real local
embedding model (CI never runs this script).

Output: ``reports/RETRIEVAL_EVAL_REPORT.md``
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REPORT_PATH = PROJECT_ROOT / "reports" / "RETRIEVAL_EVAL_REPORT.md"
MODES = ("keyword", "semantic", "hybrid")


def load_groups(case_files: list[Path]) -> list[dict]:
    """Normalize both case-file shapes into a list of document groups."""

    groups: list[dict] = []
    for path in case_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "documents" in data:
            for group in data["documents"]:
                groups.append({**group, "case_file": path.name})
        else:
            groups.append(
                {
                    "path": data["document"],
                    "playbook_id": data["playbook_id"],
                    "cases": data["cases"],
                    "case_file": path.name,
                    "note": data.get("note", ""),
                }
            )
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        nargs="*",
        default=["eval_cases/retrieval_cases.json", "eval_cases/retrieval_hard_cases.json"],
        help="Case files to evaluate",
    )
    args = parser.parse_args()

    from backend.config import get_config
    from backend.engine.parsing import parse_document
    from backend.engine.retrieval import get_clause_embedder, select_clauses
    from backend.playbooks import get_playbook

    config = get_config()
    groups = load_groups([PROJECT_ROOT / f for f in args.cases])

    embedder = get_clause_embedder()
    if embedder is None:
        print("[ERROR] 本地 embedding 模型不可用")
        return 1

    totals = {mode: {"hit": 0, "total": 0} for mode in MODES}
    report_sections: list[tuple[dict, list[dict]]] = []

    for group in groups:
        document_path = PROJECT_ROOT / group["path"]
        playbook = get_playbook(group["playbook_id"])
        seed_by_name = {seed["name"]: seed for seed in playbook.rule_seeds}
        parsed = parse_document(document_path)
        ready = embedder.ensure_ready(parsed.clauses)
        group_budget = int(group.get("char_budget") or 6000)

        rows = []
        for case in group["cases"]:
            seed = seed_by_name[case["rule_name"]]
            rule_text = f"{seed['name']} {seed['guidance']}"
            expected = set(case["expected_ordinals"])
            row = {
                "rule": case["rule_name"],
                "expected": sorted(expected),
                "clauses": len(parsed.clauses),
            }
            for mode in MODES:
                selected = select_clauses(
                    parsed.clauses,
                    rule_text,
                    char_budget=group_budget,
                    embedder=embedder if (ready and mode != "keyword") else None,
                    mode=mode,
                )
                selected_ordinals = {clause.ordinal for clause in selected}
                hit = bool(expected & selected_ordinals)
                totals[mode]["total"] += 1
                totals[mode]["hit"] += 1 if hit else 0
                row[mode] = sorted(selected_ordinals)
                row[f"{mode}_hit"] = hit
            rows.append(row)
        report_sections.append((group, rows))
        print(f"[OK] {group['path']} — {len(rows)} cases (embedder ready: {ready})")

    lines = [
        "# 条款检索硬案例评测报告（keyword vs semantic vs hybrid）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"embedding：{config.embedding_model} · 预算：按组设定（6000 默认）",
        "",
    ]
    for group, rows in report_sections:
        lines.append(f"## {group['path']}")
        if group.get("note"):
            lines.append(f"\n> {group['note']}")
        lines.append("")
        lines.append("| 规则 | 期望条款 | K 选中 | S 选中 | H 选中 | K | S | H |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in rows:
            lines.append(
                f"| {row['rule']} | {row['expected']} | {row['keyword']} | {row['semantic']} "
                f"| {row['hybrid']} | {'✅' if row['keyword_hit'] else '❌'} "
                f"| {'✅' if row['semantic_hit'] else '❌'} | {'✅' if row['hybrid_hit'] else '❌'} |"
            )
        lines.append("")

    lines.append("## 总体选段命中率")
    lines.append("")
    lines.append("| 模式 | 命中 |")
    lines.append("|---|---|")
    for mode in MODES:
        stats = totals[mode]
        rate = stats["hit"] / stats["total"] if stats["total"] else 0
        lines.append(f"| {mode} | {stats['hit']}/{stats['total']} = {rate:.0%} |")
    lines.append("")
    lines.append(
        "> 判读：命中 = 期望证据条款进入选中集合（不要求排第一）。"
        "期望为多条款时选中其一即算命中；缺失类规则（无证据可标注）不在本指标内。"
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REPORT_PATH}")
    for mode in MODES:
        stats = totals[mode]
        print(f"  {mode:8} hit {stats['hit']}/{stats['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
