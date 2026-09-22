"""Offline retrieval replay: will a longer corpus test reasoning or just retrieval?

The v1 seeded corpus is short enough to hit the whole-document shortcut in
``retrieval.select_clauses``, so the published baseline never exercised clause
selection at all. Before writing a longer corpus, this replays the exact query
the engine issues (``f"{rule.name} {rule.guidance}"``, scoring.py:91) against
padded copies of the same documents and reports whether the gold clause is
still in the candidate set — once with the clause headings intact and once with
them stripped, which is what a de-titled corpus looks like to the retriever.

No model calls; keyword mode only (the embedder is not reachable offline).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine import retrieval  # noqa: E402
from backend.engine.parsing import split_clauses  # noqa: E404
from backend.playbooks.registry import get_playbook  # noqa: E402

CASES_PATH = PROJECT_ROOT / "eval_cases" / "seeded_defect_cases.json"
HEAD_LINE = re.compile(r"^第[一二三四五六七八九十百零〇]+条.*$|^\d+(\.\d+)*\s+\S.*$|^[一二三四五六七八九十]+、.*$")

# Contract boilerplate that should carry no signal for any review rule. Any
# non-zero score here is a decoy leak, not padding — it is reported, not hidden.
FILLER = (
    ("标题与解释", "本合同各条款标题仅为查阅方便而设，不影响条款本身的解释，亦不构成合同内容的一部分。"),
    ("通知与送达", "一方按本合同载明的通讯地址向另一方发出书面通知的，交付快递之日起第三个工作日视为送达；地址变更应提前五日书面告知对方。"),
    ("合同份数与文本效力", "本合同一式肆份，甲乙双方各持贰份，具有同等法律效力。合同正本与扫描件效力相同，电子签章与手写签名效力相同。"),
    ("语言版本", "本合同以中文文本为准。如应监管或融资需要另行准备其他语种译本，译本仅供参阅，与中文文本不一致时以中文文本为准。"),
    ("完整协议", "本合同及其附件构成双方就本项目达成的完整合意，取代此前双方就同一事项作出的一切口头或书面沟通。"),
    ("可分割性", "本合同任一条款被认定无效或不可执行的，不影响其余条款的效力，双方应以最接近原意的有效条款替代之。"),
    ("弃权", "一方未行使或迟延行使本合同项下任何权利，不构成对该权利的放弃，亦不妨碍该方今后行使该权利。"),
    ("印鉴与签署管理", "双方各自保管己方印章与签署授权文件，因保管不善造成的后果由该方自行承担；经办人离岗应在三日内书面通知对方。"),
    ("工作日与节假日", "本合同所称工作日指中华人民共和国法定工作日；期限届满日恰逢非工作日的，顺延至其后第一个工作日。"),
    ("账户信息", "双方收付款账户以本合同签署页载明的信息为准，账户变更须经单位盖章的书面函件确认，电话与即时通讯消息不构成有效变更通知。"),
)


def _headings_and_bodies(clauses):
    """(ordinal, heading, body) where body is the clause text minus its heading line."""

    out = []
    for clause in clauses:
        lines = clause.text.split("\n")
        heading = lines[0] if lines and HEAD_LINE.match(lines[0].strip()) else (clause.heading or "")
        body = "\n".join(lines[1:]).strip() if heading and lines[0].strip() == heading.strip() else clause.text
        out.append((clause.ordinal, heading, body))
    return out


def _gold_map(clauses, rule_names):
    """rule → ordinals of clauses whose heading repeats the rule name.

    v1 authored 条款标题 as the rule name, so the heading is a usable ground
    truth for *where the evidence sits* even though it is exactly the shortcut
    under investigation.
    """

    gold = {}
    for ordinal, heading, body in _headings_and_bodies(clauses):
        for name in rule_names:
            if name and name in (heading or ""):
                gold.setdefault(name, []).append(ordinal)
    return gold


def _pad(text: str, target_chars: int) -> tuple[str, int]:
    padded = text
    index = 0
    while sum(len(clause.text) for clause in split_clauses(padded)) < target_chars:
        heading, body = FILLER[index % len(FILLER)]
        padded += f"\n\n{heading}\n{body}"
        index += 1
    return padded, index


def replay(clauses, rule, *, mode="keyword", headless=False):
    """Run the engine's own selection over (optionally) de-headed clauses."""

    from backend.engine.parsing import ParsedClause

    if headless:
        clauses = [
            ParsedClause(ordinal=ordinal, heading=None, text=body or heading)
            for ordinal, heading, body in _headings_and_bodies(clauses)
            if (body or heading).strip()
        ]
    selected = retrieval.select_clauses(clauses, f"{rule.name} {rule.guidance}", mode=mode)
    return [clause.ordinal for clause in selected], clauses


def gold_fate(clauses, rule, gold_ordinals, *, headless=False, char_budget=retrieval.MAX_RULE_CONTEXT_CHARS):
    """Where the gold clause ends up: selected, starved by rank, or no signal at all."""

    from backend.engine.parsing import ParsedClause

    working = clauses
    if headless:
        working = [
            ParsedClause(ordinal=ordinal, heading=None, text=body or heading)
            for ordinal, heading, body in _headings_and_bodies(clauses)
            if (body or heading).strip()
        ]
    keywords = retrieval._keywords(f"{rule.name} {rule.guidance}")
    scored = [(retrieval.score_clause(clause.text, keywords), clause) for clause in working]
    ranked = [clause for score, clause in sorted((p for p in scored if p[0] > 0), key=lambda p: (-p[0], p[1].ordinal))]
    selected = [clause.ordinal for clause in retrieval.select_clauses(working, f"{rule.name} {rule.guidance}", char_budget=char_budget)]
    ranks = {clause.ordinal: index + 1 for index, clause in enumerate(ranked)}
    gold_rank = min((ranks[ordinal] for ordinal in gold_ordinals if ordinal in ranks), default=None)
    ahead = sum(len(clause.text) for clause in ranked[: (gold_rank - 1)]) if gold_rank else None
    hits = [ordinal for ordinal in gold_ordinals if ordinal in selected]
    if hits:
        cause = "OK"
    elif gold_rank is None:
        cause = "NO_SIGNAL"      # 词面零命中，被「任何信号」下限直接丢弃
    else:
        cause = "RANK_STARVED"   # 有信号，但排在前面的弱命中段把预算吃光了
    return {
        "selected": selected,
        "signal_clauses": len(ranked),
        "gold_rank": gold_rank,
        "chars_ahead_of_gold": ahead,
        "precision": round(len(hits) / len(selected), 3) if selected else None,
        "cause": cause,
    }


def filler_leaks(rule_seeds) -> list[str]:
    """Padding that carries keyword signal invalidates the whole measurement."""

    leaks = []
    for heading, body in FILLER:
        for seed in rule_seeds:
            keywords = retrieval._keywords(f"{seed['name']} {seed['guidance']}")
            score = retrieval.score_clause(f"{heading}\n{body}", keywords)
            if score > 0:
                leaks.append(f"填充段「{heading}」对规则「{seed['name']}」有 {score} 分信号")
    return leaks


def audit(cases_path: Path, target_chars: int) -> dict:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    rows, unmapped, leaks = [], [], []
    for case in cases:
        text = (PROJECT_ROOT / case["document"]).read_text(encoding="utf-8")
        spec = get_playbook(case["playbook_id"])
        rule_names = [seed["name"] for seed in spec.rule_seeds]
        rules = {seed["name"]: seed for seed in spec.rule_seeds}
        leaks += [f"{case['id']}: {leak}" for leak in filler_leaks(spec.rule_seeds)]
        base_clauses = split_clauses(text)
        gold = _gold_map(base_clauses, rule_names)
        padded_text, filler_count = _pad(text, target_chars)
        padded_clauses = split_clauses(padded_text)

        for name, ordinals in gold.items():
            rule = type("Rule", (), {"name": name, "guidance": rules[name]["guidance"]})()
            rows.append(
                {
                    "case": case["id"],
                    "rule": name,
                    "gold_ordinals": ordinals,
                    "chars_total": sum(len(c.text) for c in padded_clauses),
                    "clause_count": len(padded_clauses),
                    "with_heading": gold_fate(padded_clauses, rule, ordinals),
                    "heading_stripped": gold_fate(padded_clauses, rule, ordinals, headless=True),
                }
            )
        unmapped.append({"case": case["id"], "rules_without_heading_gold": [n for n in rule_names if n not in gold]})
    return {"rows": rows, "unmapped": unmapped, "filler_leaks": leaks}


def summarise(result: dict) -> list[str]:
    rows = result["rows"]
    if not rows:
        return ["没有任何规则-文档对可重放：金标证据无法从条款标题推出。"]

    def count(key, cause):
        return sum(1 for row in rows if row[key]["cause"] == cause)

    precisions = [row["with_heading"]["precision"] for row in rows if row["with_heading"]["precision"] is not None]
    lost_by_detitle = [
        f"{row['case']}·{row['rule']}（{row['with_heading']['cause']}→{row['heading_stripped']['cause']}）"
        for row in rows
        if row["with_heading"]["cause"] == "OK" and row["heading_stripped"]["cause"] != "OK"
    ]
    lines = [
        f"可重放规则-文档对：{len(rows)}（填充后每份 {rows[0]['chars_total']} 字 / {rows[0]['clause_count']} 段，预算 6000 字）",
        f"标题保留：{count('with_heading', 'OK')} 命中 · {count('with_heading', 'RANK_STARVED')} 排名饿死 · {count('with_heading', 'NO_SIGNAL')} 零词面信号",
        f"剥掉标题：{count('heading_stripped', 'OK')} 命中 · {count('heading_stripped', 'RANK_STARVED')} 排名饿死 · {count('heading_stripped', 'NO_SIGNAL')} 零词面信号",
        f"选中段里真正是证据的比例（精度）均值：{sum(precisions) / len(precisions):.1%}" if precisions else "精度：无命中样本",
        f"仅因去标题而失去证据的规则：{len(lost_by_detitle)} 条",
    ]
    lines += [f"  - {item}" for item in lost_by_detitle]
    if result["filler_leaks"]:
        lines.append(f"填充段自身带信号 {len(result['filler_leaks'])} 处 —— 这些会污染测量，须换措辞：")
        lines += [f"  ! {leak}" for leak in result["filler_leaks"][:12]]
    for entry in result["unmapped"]:
        if entry["rules_without_heading_gold"]:
            lines.append(f"{entry['case']}：{len(entry['rules_without_heading_gold'])} 条规则推不出金标证据条款（多为缺口类，本应无证据）")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="长语料前的检索定位能力预检（零模型调用）")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--pad-to", type=int, default=9000, help="把每份文档的条款文本推到多少字（默认 9000 > 6000 预算）")
    parser.add_argument("--json", type=Path, default=None, help="把逐条结果写到该路径")
    args = parser.parse_args()

    result = audit(args.cases, args.pad_to)
    for line in summarise(result):
        print(line)
    print()
    for row in result["rows"]:
        kept, stripped = row["with_heading"], row["heading_stripped"]
        print(
            f"  {row['case']:<11} {row['rule']:<12} 保留标题={kept['cause']:<12}"
            f"排名{kept['gold_rank']}/有信号{kept['signal_clauses']}段 前置{kept['chars_ahead_of_gold']}字 "
            f"| 剥标题={stripped['cause']:<12} 排名{stripped['gold_rank']}"
        )
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
