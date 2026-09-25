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
from collections import Counter, defaultdict
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


def rows_by_case(payload: dict) -> dict[tuple[str, str], dict]:
    """(case_id, rule) → the detail row, so two arms can be compared annotation by annotation."""

    out = {}
    for result in payload["results"]:
        for detail in result["metrics"]["details"]:
            out[(result["case_id"], detail["rule"])] = detail
    return out


def _row_score(detail: dict) -> int:
    """How much of this annotation the verdict earned: band right, and for a
    defect also grounded in the planted clause."""

    score = int(bool(detail["exact"]))
    if detail["kind"] == "defect":
        score += int(bool(detail["defect_grounded"]))
    return score


def print_diff(first: dict, last: dict, first_name: str, last_name: str) -> None:
    keys = sorted(set(first) & set(last))
    print(f"\n=== 逐条差异：{first_name} → {last_name}（分母 {len(keys)} 条标注）")
    tally: dict[tuple[str, str], int] = Counter()
    for key in keys:
        before, after = first[key], last[key]
        if (
            before["actual"] == after["actual"]
            and before["defect_grounded"] == after["defect_grounded"]
        ):
            continue
        delta = _row_score(after) - _row_score(before)
        direction = "变好" if delta > 0 else "变差" if delta < 0 else "平移"
        tally[(before["kind"], direction)] += 1
        extra = (
            f"｜锚定 {int(before['defect_grounded'])}→{int(after['defect_grounded'])}"
            if before["kind"] == "defect"
            else f"｜缺口达标 {int(before['gap_recalled'])}→{int(after['gap_recalled'])}"
            if before["kind"] == "gap"
            else ""
        )
        print(
            f"  [{direction}] {key[0]} · {key[1]}（{before['kind']} 期望 {before['expected']}）："
            f"{before['actual']} → {after['actual']}{extra}"
        )
    print("\n变化方向统计（按标注类型）：")
    for kind in ("defect", "gap", "clean"):
        parts = [f"{word} {tally[(kind, word)]}" for word in ("变好", "变差", "平移") if tally[(kind, word)]]
        print(f"  {kind:<7} {' · '.join(parts) or '无变化'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("metrics", type=Path, nargs="+", help="评测 JSON（可多个臂并列）")
    parser.add_argument("--top", type=int, default=15, help="只列锚定损失最大的前 N 条规则")
    parser.add_argument(
        "--diff", action="store_true", help="对比第一个与最后一个臂，逐条列出结果变化的标注"
    )
    args = parser.parse_args()

    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in args.metrics]
    arms = [(path.stem, slice_by_rule(payload["results"])) for path, payload in zip(args.metrics, payloads)]
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
        short = name.removeprefix("v2_").replace("_relabeled", "")[:10]
        header += f"{short + ' 锚定':>14}{'命中未锚定':>10}{'检索阻断':>10}"
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
    if args.diff and len(payloads) >= 2:
        print_diff(
            rows_by_case(payloads[0]),
            rows_by_case(payloads[-1]),
            args.metrics[0].stem,
            args.metrics[-1].stem,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
