"""Choose a candidate cap from true relevance ranks — spends nothing.

The paid keyword run showed the engine losing defects it was shown the answer
to. The candidate explanation is the shape of the context, not its length: on a
10,000-character document the 6,000-character budget never binds, so every
clause sharing one stray bigram reaches the model next to the clause that
actually answers the rule.

A cap can only be chosen on the *relevance* order, which is why this reads
``rank_clauses`` rather than ``select_clauses``: the live selector re-sorts by
document ordinal before returning, so the ordinals in ``retrieval_selections``
and in the replay below are positions in a document-ordered list. "Gold is third
in the context" is not "gold is third-ranked", and a cap placed on the ranking
would drop a different set than the name suggests.

Two variants are swept for every k, because the planned substance filter moves
what sits at rank ≤ k:

- ``cap``        : top-k of the relevance ranking.
- ``filter+cap`` : top-k after candidates whose body (text minus its heading)
                   is shorter than --min-substance-chars are dropped.

Every rate carries its denominator. ``--cases`` documents are read from disk, so
this replays exactly the text the paid arms saw.

    python scripts/sweep_retrieval_cap.py --focus CG-V2-02/可用性承诺
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import parse_document, split_clauses  # noqa: E402
from backend.engine.retrieval import (  # noqa: E402
    MAX_RULE_CONTEXT_CHARS,
    rank_clauses,
    select_clauses,
)
from scripts.build_seeded_corpus import playbook_rules  # noqa: E402
from scripts.evaluate_retrieval import load_groups  # noqa: E402

# A CJK contract is roughly 1.5 characters per token in the pricing tokenizer;
# this only ever sizes the cost, never the decision.
CHARS_PER_TOKEN = 1.5
HARD_CASE_FILES = ("eval_cases/retrieval_cases.json", "eval_cases/retrieval_hard_cases.json")
_WHITESPACE = re.compile(r"\s+")


def _body(text: str, heading: str) -> str:
    """Clause text without its heading line — what substance a candidate carries."""

    if heading:
        first_line = text.splitlines()[0] if text else ""
        if heading in first_line:
            return text[len(first_line):].strip()
    return text.strip()


def _class_index(case_ids: list[str], clause_lists: dict[str, list]) -> tuple[dict, int]:
    """Mark clauses that repeat across documents as boilerplate (attachment-level prose)."""

    documents_by_text: dict[str, set[str]] = defaultdict(set)
    for case_id, clauses in clause_lists.items():
        for clause in clauses:
            documents_by_text[_WHITESPACE.sub("", clause.text)].add(case_id)
    threshold = max(2, len(case_ids) // 3)
    return {
        (case_id, clause.ordinal): "样板"
        for case_id, clauses in clause_lists.items()
        for clause in clauses
        if len(documents_by_text[_WHITESPACE.sub("", clause.text)]) >= threshold
    }, threshold


def _classify(
    clause,
    *,
    gold: set[int],
    annotated: set[int],
    boilerplate: str | None,
    min_substance_chars: int,
    heading: str,
) -> str:
    if clause.ordinal in gold:
        return "金标"
    if clause.ordinal in annotated:
        return "他规则标注段"
    if boilerplate:
        return "样板"
    if len(_body(clause.text, heading)) < min_substance_chars:
        return "裸标题/碎片"
    return "其他正文"


def sweep(
    cases: list[dict],
    documents: dict[str, list],
    ks: list[int],
    *,
    min_substance_chars: int,
) -> tuple[dict, dict, list[dict]]:
    """Return {variant: {k: stats}}, gold-rank histogram, and per-row detail."""

    boilerplate, boilerplate_threshold = _class_index(list(documents), documents)
    stats = {variant: {} for variant in ("cap", "filter+cap")}
    detail: list[dict] = []
    rank_hist: Counter[int] = Counter()
    unreachable = 0
    denominated = 0

    for case in cases:
        clauses = documents[case["id"]]
        rules = playbook_rules(case["playbook_id"])
        annotated = {ordinal for ordinals in case["retrieval_gold"].values() for ordinal in ordinals}
        for rule, ordinals in sorted(case["retrieval_gold"].items()):
            gold = set(ordinals)
            query = f"{rule} {rules[rule]['guidance']}"
            ranked = rank_clauses(clauses, query, mode="keyword")
            body_lengths = {clause.ordinal: len(_body(clause.text, clause.heading)) for clause in clauses}
            best_rank = next(
                (index + 1 for index, clause in enumerate(ranked) if clause.ordinal in gold), None
            )
            denominated += 1
            if best_rank is None:
                unreachable += 1
            else:
                rank_hist[min(best_rank, 10)] += 1
            sent_now = select_clauses(clauses, query, mode="keyword")
            row = {
                "case": case["id"],
                "rule": rule,
                "gold": sorted(gold),
                "gold_rank": best_rank,
                "candidate_count": len(ranked),
                "sent_now_count": len(sent_now),
                "sent_now_chars": sum(len(c.text) for c in sent_now),
                "reached_now": bool(gold & {c.ordinal for c in sent_now}),
            }
            detail.append(row)

            for variant, pool in (
                ("cap", ranked),
                (
                    "filter+cap",
                    [c for c in ranked if body_lengths[c.ordinal] >= min_substance_chars],
                ),
            ):
                for k in ks:
                    bucket = stats[variant].setdefault(
                        k,
                        {
                            "survived": 0,
                            "total": 0,
                            "decoy": Counter(),
                            "decoy_clauses": 0,
                            "sent_clauses": 0,
                            "chars": [],
                            "budget_cut": 0,
                        },
                    )
                    bucket["total"] += 1
                    taken = pool[:k]
                    chars = sum(len(c.text) for c in taken)
                    if chars > MAX_RULE_CONTEXT_CHARS:
                        bucket["budget_cut"] += 1
                    bucket["chars"].append(chars)
                    bucket["sent_clauses"] += len(taken)
                    if any(c.ordinal in gold for c in taken):
                        bucket["survived"] += 1
                    for clause in taken:
                        label = _classify(
                            clause,
                            gold=gold,
                            annotated=annotated,
                            boilerplate=boilerplate.get((case["id"], clause.ordinal)),
                            min_substance_chars=min_substance_chars,
                            heading=clause.heading,
                        )
                        if label != "金标":
                            bucket["decoy"][label] += 1
                            bucket["decoy_clauses"] += 1
    return stats, {"hist": rank_hist, "unreachable": unreachable, "total": denominated,
                   "boilerplate_threshold": boilerplate_threshold}, detail


def _print_table(stats: dict, meta: dict, ks: list[int], detail: list[dict]) -> None:
    gold_slots = meta["total"]
    hist = " ".join(f"r{rank}={count}" for rank, count in sorted(meta["hist"].items()))
    print(f"金标槽位：{gold_slots} 条 (case, rule)；排序后仍不可达 {meta['unreachable']} 条")
    print(f"跨文档样板判定阈值：出现在 ≥ {meta['boilerplate_threshold']} 份文档")
    print(f"金标真相关度名次分布：{hist}（r10 含名次 ≥10 的全部；分母 {gold_slots}）\n")

    for variant, buckets in stats.items():
        print(f"=== {variant} ===")
        print(
            "k    金标存活            每规则段数  每规则字数(中位/最大)      "
            "诱饵段数（裸标题/样板/他规则/其他）   超预算"
        )
        for k in ks:
            b = buckets[k]
            rate = b["survived"] / b["total"] if b["total"] else 0.0
            decoy = b["decoy"]
            chars = b["chars"]
            median_chars = statistics.median(chars)
            print(
                f"{k:<4} {b['survived']}/{b['total']} = {rate:>5.1%}    "
                f"{b['sent_clauses'] / b['total']:>7.1f}     "
                f"{median_chars:>6.0f}/{max(chars):<6.0f}"
                f"（≈{median_chars / CHARS_PER_TOKEN:>4.0f}tok）  "
                f"{sum(decoy.values()):>5}：裸标题 {decoy['裸标题/碎片']}、样板 {decoy['样板']}、"
                f"他规则 {decoy['他规则标注段']}、其他 {decoy['其他正文']}   {b['budget_cut']} 例"
            )
        print()


def sweep_hard_cases(ks: list[int], *, min_substance_chars: int) -> dict:
    """The pinned 36-case gold set under the same cap — the test we must not break quietly."""

    results = {}
    rows = results.setdefault("rows", [])
    for group in load_groups([PROJECT_ROOT / name for name in HARD_CASE_FILES]):
        rules = playbook_rules(group["playbook_id"])
        clauses = parse_document(PROJECT_ROOT / group["path"]).clauses
        budget = int(group.get("char_budget") or MAX_RULE_CONTEXT_CHARS)
        total_chars = sum(len(c.text) for c in clauses)
        for case in group["cases"]:
            query = f"{case['rule_name']} {rules[case['rule_name']]['guidance']}"
            gold = set(case["expected_ordinals"])
            ranked = rank_clauses(clauses, query, mode="keyword")
            pool = [
                c for c in ranked if len(_body(c.text, c.heading)) >= min_substance_chars
            ]
            rows.append(
                {
                    "document": Path(group["path"]).name,
                    "rule": case["rule_name"],
                    "budget": budget,
                    "early_exit": total_chars <= budget,
                    "rank": next(
                        (index + 1 for index, c in enumerate(ranked) if c.ordinal in gold), None
                    ),
                    "hit_now": bool(
                        gold
                        & {
                            c.ordinal
                            for c in select_clauses(clauses, query, char_budget=budget, mode="keyword")
                        }
                    ),
                    "hit_pool": {k: bool(gold & {c.ordinal for c in pool[:k]}) for k in ks},
                }
            )
    rows = results["rows"]
    return {
        "total": len(rows),
        "hit_now": sum(1 for r in rows if r["hit_now"]),
        "early_exit": sum(1 for r in rows if r["early_exit"]),
        "working_points": sorted({r["budget"] for r in rows}),
        "by_k": {
            k: {
                "plumbed": sum(1 for r in rows if r["early_exit"] or r["hit_pool"][k]),
                "pool": sum(1 for r in rows if r["hit_pool"][k]),
                "misses": [
                    f"{r['document']}/{r['rule']}"
                    for r in rows
                    if not r["early_exit"] and not r["hit_pool"][k]
                ],
            }
            for k in ks
        },
    }


def print_focus(cases: list[dict], documents: dict[str, list], focus: str, min_substance_chars: int) -> None:
    case_id, rule = focus.split("/", 1)
    case = next((c for c in cases if c["id"] == case_id), None)
    if case is None:
        print(f"[focus] 找不到文档 {case_id}")
        return
    clauses = documents[case_id]
    rules = playbook_rules(case["playbook_id"])
    gold = set(case["retrieval_gold"].get(rule) or [])
    annotated = {o for os_ in case["retrieval_gold"].values() for o in os_}
    boilerplate, threshold = _class_index(list(documents), documents)
    ranked = rank_clauses(clauses, f"{rule} {rules[rule]['guidance']}", mode="keyword")
    print(f"[focus] {case_id} · {rule} · 金标 {sorted(gold)} · 候选 {len(ranked)} 段")
    for index, clause in enumerate(ranked, start=1):
        label = _classify(
            clause,
            gold=gold,
            annotated=annotated,
            boilerplate=boilerplate.get((case_id, clause.ordinal)),
            min_substance_chars=min_substance_chars,
            heading=clause.heading,
        )
        print(
            f"  rank {index:>2}  条款 {clause.ordinal:>2}  {len(clause.text):>4} 字  "
            f"正文 {len(_body(clause.text, clause.heading)):>4} 字  {label:<8} {clause.heading or '（无标题）'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "eval_cases/seeded_defect_cases_v2.json")
    parser.add_argument("--ks", default="2,3,4,5,6,8,10,12")
    parser.add_argument("--min-substance-chars", type=int, default=24)
    parser.add_argument("--focus", help="case/rule，逐名次打印排序明细")
    parser.add_argument("--skip-hard-cases", action="store_true")
    args = parser.parse_args()

    ks = [int(k) for k in args.ks.split(",")]
    payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = payload["cases"]
    documents = {}
    for case in cases:
        text = (PROJECT_ROOT / case["document"]).read_text(encoding="utf-8")
        documents[case["id"]] = split_clauses(text)

    print(f"语料：{args.cases.name} · {len(cases)} 份文档 · 切出 "
          f"{sum(len(c) for c in documents.values())} 段 · "
          f"每份 {min(len(c) for c in documents.values())}~{max(len(c) for c in documents.values())} 段")
    stats, meta, detail = sweep(cases, documents, ks, min_substance_chars=args.min_substance_chars)
    reached_now = sum(1 for r in detail if r["reached_now"])
    print(
        f"现管线（只有 {MAX_RULE_CONTEXT_CHARS} 字预算）：每规则送入 "
        f"{statistics.mean([r['sent_now_count'] for r in detail]):.1f} 段 / "
        f"{statistics.median([r['sent_now_chars'] for r in detail]):.0f} 字（中位），"
        f"金标在其中 {reached_now}/{len(detail)} = {reached_now / max(len(detail), 1):.1%}"
    )
    print()
    _print_table(stats, meta, ks, detail)

    if not args.skip_hard_cases:
        hard = sweep_hard_cases(ks, min_substance_chars=args.min_substance_chars)
        print(
            f"=== 固定短文档金标集 {hard['total']} 例"
            f"（tests/test_clause_tokenization.py 钉住；工作点 {hard['working_points']} 字，"
            f"其中 {hard['early_exit']}/{hard['total']} 例走整份直送早退分支）==="
        )
        print(f"现管线命中 {hard['hit_now']}/{hard['total']}")
        for k, row in sorted(hard["by_k"].items()):
            print(
                f"  k={k:<3} 按落地口径 {row['plumbed']}/{hard['total']}，"
                f"纯 top-{k} 池 {row['pool']}/{hard['total']}（差值即早退分支救回）  "
                f"未命中：{'、'.join(row['misses']) or '无'}"
            )
        print()

    if args.focus:
        print_focus(cases, documents, args.focus, args.min_substance_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
