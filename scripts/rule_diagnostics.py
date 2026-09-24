"""Per-rule loss for stored seeded-defect arms — no model calls.

Every engine number so far has been an aggregate over 336 verdicts, which hides
where the loss actually lives: if 73.3% → 95.6% is mostly three rules, the fix
belongs to those three rules and not to the pipeline. This reads a run's own
metrics JSON (the same file paid runs leave behind) and slices it by rule.

Rows are ranked by anchored-defect loss so the first line is the place money
would be spent. Denominators are printed with every rate; a rule with no
annotated defect rows says so rather than showing 0%.

    python scripts/rule_diagnostics.py C:/.../v2_keyword_relabeled.json C:/.../v2_oracle_relabeled.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _pct(hit: int, total: int) -> str:
    return f"{100.0 * hit / total:.1f}%" if total else "—"


def slice_by_rule(results: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(
        lambda: {
            "defect": 0,
            "defect_flagged": 0,
            "defect_exact": 0,
            "groundable": 0,
            "grounded": 0,
            "blocked": 0,
            "clean": 0,
            "clean_fp": 0,
            "gap": 0,
            "gap_recalled": 0,
            "ungrounded_hits": 0,
        }
    )
    for result in results:
        for detail in result["metrics"]["details"]:
            row = out[detail["rule"]]
            if detail["kind"] == "defect":
                row["defect"] += 1
                row["defect_flagged"] += int(detail["flagged"])
                row["defect_exact"] += int(detail["exact"])
                row["blocked"] += int(detail["retrieval_blocked"])
                row["groundable"] += int(detail["defect_groundable"])
                row["grounded"] += int(detail["defect_grounded"])
                row["ungrounded_hits"] += int(detail["flagged"] and not detail["defect_grounded"])
            elif detail["kind"] == "clean":
                row["clean"] += 1
                row["clean_fp"] += int(detail["actual"] in ("red", "amber"))
            else:
                row["gap"] += 1
                row["gap_recalled"] += int(detail["gap_recalled"])
    return dict(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("metrics", type=Path, nargs="+", help="评测 JSON（可多个臂并列）")
    parser.add_argument("--top", type=int, default=15, help="只列锚定损失最大的前 N 条规则")
    args = parser.parse_args()

    arms = [
        (path.stem, slice_by_rule(json.loads(path.read_text(encoding="utf-8"))["results"]))
        for path in args.metrics
    ]
    rules = sorted({rule for _, sliced in arms for rule in sliced})
    keyed = {name: sliced for name, sliced in arms}

    def anchored_gap(rule: str) -> float:
        # Loss is ranked here because this is the single axis that survived the
        # label fix: a flagged defect whose citation is not in the planted clause.
        return sum(
            (keyed[name][rule]["groundable"] - keyed[name][rule]["grounded"])
            for name in keyed
            if rule in keyed[name]
        )

    print(
        f"并列臂：{ ' · '.join(name for name, _ in arms) }"
        f"（每条规则的分子来自各臂自己的 details；— 表示该臂在此规则上没有对应标注行）"
    )
    header = f"{'规则':<16}"
    for name, _ in arms:
        header += f"{name.split('_')[-1] + ' 锚定':>14}{'命中未锚定':>10}{'检索阻断':>10}"
    header += f"{'干净/误报':>12}{'缺口/召回':>12}"
    print(header)
    for rule in sorted(rules, key=anchored_gap, reverse=True)[: args.top]:
        line = f"{rule:<16}"
        for name, _ in arms:
            row = keyed[name].get(rule)
            if row is None:
                line += f"{'—':>14}{'—':>10}{'—':>10}"
                continue
            line += (
                f"{_pct(row['grounded'], row['groundable']):>10}"
                f"({row['grounded']}/{row['groundable']})"[:14].rjust(14)
            )
            line += f"{row['ungrounded_hits']:>10}{row['blocked']:>10}"
        clean = sum(keyed[n][rule]["clean"] for n, _ in arms if rule in keyed[n])
        clean_fp = sum(keyed[n][rule]["clean_fp"] for n, _ in arms if rule in keyed[n])
        gap = sum(keyed[n][rule]["gap"] for n, _ in arms if rule in keyed[n])
        gap_hit = sum(keyed[n][rule]["gap_recalled"] for n, _ in arms if rule in keyed[n])
        line += f"{clean_fp:>6}/{clean:<6}{gap_hit:>6}/{gap}"
        print(line)

    totals = {
        name: {
            key: sum(row[key] for row in sliced.values()) for key in next(iter(sliced.values()))
        }
        for name, sliced in arms
    }
    print("\n全规则合计：")
    for name, _ in arms:
        t = totals[name]
        print(
            f"  {name}: 锚定缺陷召回 {_pct(t['grounded'], t['groundable'])} ({t['grounded']}/{t['groundable']})"
            f" · 命中但未锚定 {t['ungrounded_hits']} · 误报 {t['clean_fp']}/{t['clean']}"
            f" · 缺口召回 {t['gap_recalled']}/{t['gap']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
