"""Export a gold corpus into an isolated blind-review pack for another agent.

The first blind arm (reports/BLIND_ARM_EVAL_REPORT.md) was assembled by hand,
and hand-assembling it is how an answer leaks: one forgotten 「（合成评测样例）」
marker, or a fixture named 埋入缺陷合同.txt copied verbatim, and the competitor
is reading the answer key instead of the contract. This script makes the pack
and then checks it.

Three properties, each enforced in code:

- **Nothing outside the pack may enter it.** The gold JSON, the case ids, the
  fixture filenames and every annotation sentence (``planted`` / ``missing``)
  are treated as banned strings and :func:`scan_leaks` greps the finished
  directory for them.
- **The reviewed text is the fixture text**, minus marker lines. A single
  documented difference, so citation genuineness can still be scored against
  the copy the agent actually read.
- **The rubric is the engine's rubric.** The rules file carries the playbook's
  own stance instructions and each rule's guidance verbatim, and TASK.md
  restates the same output contract and absence rule the engine's prompt does.
  Equal information is the only way the comparison means anything.

The pack must be written **outside** the repository: this project's automatic
memory index literally lists the defect counts and the names of missed rules,
and a git worktree carries the tracked answers. A pack inside the repo is not a
blind test.

Usage:
    python scripts/build_blind_pack.py --cases eval_cases/seeded_defect_cases.json \
        --out-dir "C:/Users/you/Documents/blind-arm" --map eval_cases/blind_arm_map.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.playbooks import get_playbook  # noqa: E402

# Strings that would tell the reviewer what it is looking at. Matched case
# insensitively for the latin ones.
GENERIC_MARKERS = (
    "合成评测样例",
    "评测样例",
    "金标",
    "埋入",
    "预期判定",
    "缺陷合同",
    "缺口合同",
    "完全合规",
    "seeded",
    "gold",
    "expected_rating",
)

# The engine's own output contract, restated for a human-shaped agent that has
# no code path forcing it. Wording follows backend/engine/scoring.py::_build_prompt
# so the two arms are held to the same rules.
OUTPUT_CONTRACT = """```json
{
  "rule": "付款账期",
  "dimension": "商业",
  "rating": "red",
  "rationale": "判定理由（中文，150 字以内）",
  "citations": [{ "quote": "文档原文片段" }],
  "gap_reason": "文档缺失该内容时填写，否则留空"
}
```"""

HARD_RULES = (
    "1. **完整性**：对应规则集里的每一条规则都必须出现，且只出现一次；`rule` 与 `dimension` 必须照抄规则集里的原文，不要改写、不要翻译、不要加编号。",
    "2. **rating 只能取三个值之一**：`red` / `amber` / `green`。",
    "3. **引用必须逐字**：`citations[].quote` 必须是该文档正文里**连续逐字出现**的片段，不得改写、拼接、加省略号或翻译。",
    "4. **green 必须有据**：判 `green` 至少要有一条满足第 3 条的引用。",
    "5. **缺失就判红**：文档里找不到该规则对应内容时，`rating` 判 `red` 并在 `gap_reason` 说明缺失了什么，此时**不要**填写任何 `citations`。",
    "6. **不得臆造绿色判定**：没有原文依据时不得给 `green`。",
    "7. **附件边界**：本次只提供文档正文，附件内容不可核实不属于被审条款的缺陷——条款本身清晰合规时判 `green`，并在 `rationale` 末尾注明「附件未提供，需补充核对」；只有条款自身含糊或缺少关键要素时才判 `amber` / `red`。",
)

BOUNDARIES = (
    "- **只在本目录内工作**：只读 `docs/` 与 `rules/`，只写 `out/`。不要读取本目录以外的任何路径。",
    "- **不要联网**：不要搜索、不要访问网页、不要查找任何 Git 仓库或公开资料。这个任务的全部信息都在本目录里。",
    "- **不要修改** `docs/` 与 `rules/` 下的文件。",
    "- 不需要写代码，不需要调用任何模型 API 或第三方审阅工具——判定由你本人完成。",
)

_MARKER_RE = re.compile(r"[（(][^（）()]*评测[^（）()]*[）)]")


def doc_name(index: int) -> str:
    """0 → DOC-A, 25 → DOC-Z, 26 → DOC-AA. Names must not hint at content."""

    letters: list[str] = []
    number = index
    while True:
        letters.append(chr(ord("A") + number % 26))
        number = number // 26 - 1
        if number < 0:
            break
    return "DOC-" + "".join(reversed(letters)) + ".txt"


def strip_markers(text: str) -> tuple[str, list[str]]:
    """Delete the self-annotation span, keeping the rest of the line intact.

    A title like 「××系统 工作说明书（合成评测样例）」 must stay a title: the pack
    may differ from the fixture by the marker and nothing else. A marker written
    as a whole sentence is deliberately left alone, so :func:`scan_leaks` reports
    it instead of this function silently deleting contract prose.
    """

    removed: list[str] = []
    kept: list[str] = []
    for line in text.splitlines():
        cleaned = _MARKER_RE.sub("", line)
        for marker in GENERIC_MARKERS:
            cleaned = cleaned.replace(f"（{marker}）", "").replace(f"({marker})", "")
        cleaned = cleaned.rstrip()
        if cleaned != line.strip() and line.strip():
            removed.append(line.strip())
        kept.append(cleaned)
    return "\n".join(kept).strip() + "\n", removed


def export_rules_verbatim(playbook_id: str) -> str:
    playbook = get_playbook(playbook_id)
    lines = [f"# 规则集：{playbook.name}", "", "## 评审立场与总体要求", playbook.scoring_instructions, "", "## 规则清单"]
    for number, seed in enumerate(playbook.rule_seeds, start=1):
        lines += [
            f"### {number}. {seed['name']}",
            f"- 维度：{seed.get('dimension', '')}",
            f"- 判定标准：{seed['guidance']}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_task_md(assignments: list[tuple[str, str]], *, sample: str | None = None) -> str:
    """`assignments`: (doc filename, playbook name) pairs — no verdicts, no counts."""

    lines = [
        "# 任务：按规则集审阅文档并输出结构化判定",
        "",
        "## 你要做什么",
        "",
        f"`docs/` 下有 {len(assignments)} 份待审文档，`rules/` 下有规则集。"
        "请逐份文档，按对应规则集里的**每一条规则**给出一个判定，并把结果写成 JSON 文件放到 `out/`。",
        "",
        "| 文档 | 适用规则集 |",
        "|---|---|",
    ]
    lines += [f"| `docs/{name}` | {playbook} |" for name, playbook in assignments]
    lines += [
        "",
        "## 交付物",
        "",
        "每个文档一个 `out/<文档名>.json`（例如 `out/" + assignments[0][0].replace(".txt", ".json") + "`），"
        "文件内容是一个 JSON 数组，数组元素形如：",
        "",
        sample or OUTPUT_CONTRACT,
        "",
        "## 硬性口径（不满足会被判为无效输出）",
        "",
        *HARD_RULES,
        "",
        "判定标准（阈值、红黄绿的界线）全部在规则集的「判定标准」里，请严格照它执行；"
        "规则集开头的「评审立场与总体要求」同样适用。",
        "",
        "## 另外交一份过程记录",
        "",
        "`out/PROCESS.md`，写清：",
        "",
        "- 你实际执行的步骤顺序（先读什么后读什么、是否一次性处理整份文档、有没有中途复核）；",
        "- 你认为最没把握的 3 条判定，以及没把握的原因；",
        "- 你是否读取了本目录之外的任何文件或路径（如有，列出）。",
        "",
        "## 边界与禁令",
        "",
        *BOUNDARIES,
        "",
    ]
    return "\n".join(lines)


def banned_strings(cases: list[dict], *, cases_root: Path = PROJECT_ROOT) -> list[str]:
    """Everything whose appearance inside the pack would be an answer leak.

    An annotation that is already verbatim contract text tells the reviewer
    nothing it could not read in the document, so it is not banned; an
    annotation the document does not contain (「删除了审计权条款」) is the answer key.
    """

    banned: list[str] = []
    for case in cases:
        document = ""
        path = cases_root / str(case.get("document", ""))
        if path.exists():
            document = path.read_text(encoding="utf-8", errors="ignore")
        banned.append(str(case.get("id", "")))
        banned.append(Path(str(case.get("document", ""))).name)
        notes = [str(item.get("planted", "")) for item in case.get("defects", [])]
        notes += [str(item.get("missing", "")) for item in case.get("gaps", [])]
        banned += [note for note in notes if note and note not in document]
    return [item for item in banned if len(item) >= 3]


def scan_leaks(out_dir: Path, *, banned: list[str]) -> list[str]:
    """Grep the finished pack for answer-revealing strings. Empty list = clean."""

    probes = [*(marker.lower() for marker in GENERIC_MARKERS), *[item.lower() for item in banned]]
    probes = sorted({probe.strip().lower() for probe in probes if probe and probe.strip()})
    hits: list[str] = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        content = path.read_text(encoding="utf-8", errors="ignore").lower()
        for probe in probes:
            if probe in content:
                offset = content.index(probe)
                hits.append(f"{path.relative_to(out_dir)}: 出现「{probe}」…{content[offset:offset + 40]!r}")
    return hits


def assert_outside_repo(target: Path) -> None:
    resolved = target.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return
    raise ValueError(
        f"盲测包必须建在仓库之外（当前指向 {resolved}）。"
        "仓库内既有金标文件，又有本项目自动记忆索引里的缺陷数与漏报规则名，同目录跑测等于把答案交给对方。"
    )


def export_pack(
    cases: list[dict], *, out_dir: Path, cases_root: Path = PROJECT_ROOT
) -> tuple[dict[str, str], list[str]]:
    """Write docs/, rules/ and TASK.md. Returns (filename → fixture relpath, removed lines)."""

    assert_outside_repo(out_dir)
    (out_dir / "docs").mkdir(parents=True, exist_ok=True)
    (out_dir / "rules").mkdir(parents=True, exist_ok=True)
    (out_dir / "out").mkdir(parents=True, exist_ok=True)

    mapping: dict[str, str] = {}
    removed: list[str] = []
    assignments: list[tuple[str, str]] = []
    playbooks = {case["playbook_id"] for case in cases}
    for index, case in enumerate(cases):
        name = doc_name(index)
        original = Path(cases_root / case["document"]).read_text(encoding="utf-8")
        cleaned, dropped = strip_markers(original)
        (out_dir / "docs" / name).write_text(cleaned, encoding="utf-8")
        removed += [f"{name}: {line}" for line in dropped]
        mapping[name] = case["document"]
        assignments.append((name, get_playbook(case["playbook_id"]).name))
    for playbook_id in sorted(playbooks):
        (out_dir / "rules" / f"{get_playbook(playbook_id).name}.md").write_text(
            export_rules_verbatim(playbook_id), encoding="utf-8"
        )
    (out_dir / "TASK.md").write_text(render_task_md(assignments), encoding="utf-8")
    return mapping, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True, help="金标集 JSON")
    parser.add_argument("--out-dir", type=Path, required=True, help="盲测包目录（须在仓库外）")
    parser.add_argument("--map", type=Path, help="把「DOC-x → 原始语料」映射写到仓库内这个路径")
    parser.add_argument("--check-only", action="store_true", help="只扫描已有目录，不写文件")
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    if args.check_only:
        hits = scan_leaks(args.out_dir, banned=banned_strings(cases))
        for hit in hits:
            print(f"[FAIL] {hit}")
        print(f"[INFO] 泄漏扫描：{len(hits)} 处" if hits else "[OK] 泄漏扫描通过")
        return 1 if hits else 0

    mapping, removed = export_pack(cases, out_dir=args.out_dir)
    hits = scan_leaks(args.out_dir, banned=banned_strings(cases))
    for hit in hits:
        print(f"[FAIL] {hit}")
    for line in removed:
        print(f"[INFO] 剥离标注行 {line}")
    if hits:
        print("[FAIL] 包内仍有泄漏字符串，未写映射文件；请修正语料或标记后重建。")
        return 1

    if args.map:
        payload = {
            "_comment": (
                "盲测包与原始语料的映射，仅供打分使用；不得放入包内。"
                "正文与原始语料的唯一差异是剥掉了自标注行，"
                "因此引用真实性必须对着包内副本校验。"
            ),
            **{name: mapping[name] for name in sorted(mapping)},
        }
        args.map.parent.mkdir(parents=True, exist_ok=True)
        args.map.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 映射写入 {args.map}")
    print(f"[OK] 盲测包写入 {args.out_dir}（{len(mapping)} 份文档，泄漏扫描 0 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
