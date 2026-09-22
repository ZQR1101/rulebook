"""Build a seeded-defect corpus from a manifest, and derive its gold set.

The v1 corpus proved too easy: four short documents, clause titles that
duplicated their rule name, and all of them inside the 6,000-character
retrieval budget, so a general-purpose agent with no customization matched the
engine (see reports/BLIND_ARM_EVAL_REPORT.md). This builder exists to make a
corpus that cannot be passed that way, and to keep the labels honest while
doing it:

- **Gold comes from the rendered text, not from a second hand-written list.**
  A manifest clause declares which rule it answers; its ordinal is then looked
  up by running the *real* splitter over the *rendered* document. Labels cannot
  drift away from the corpus the way the v1 ordinal labels did when the splitter
  changed.
- **A clause title may not name its rule.** v1 headings were the retrieval
  signal: de-titling 「付款账期」 pushed that gold clause from rank 1 to rank 6
  and made another one unreachable outright. Enforced here, so a v2 heading
  cannot quietly hand the answer to keyword retrieval.
- **Preflight before spend.** Every check is LLM-free. A document that only
  *looks* like it plants a violation — because the clause never touches the
  threshold its own guidance states — is rejected here instead of turning into
  an unexplained miss after a paid run.

Manifest shape (JSON):

    {"defaults": {"playbook_id": "contract-compliance"},
     "documents": [{"id": "CG-V2-01", "title": "…", "part_titles": ["商务条款", …],
                    "preamble": ["甲方：…", "合同编号：…"],
                    "gap_notes": {"数据存储地": "全文未约定存储与处理地域"},
                    "parts": [{"clauses": [
                        {"heading": "价款与支付", "sentences": ["…", "…"],
                         "answers": "付款账期", "kind": "defect",
                         "expected_rating": "red", "note": "账期 120 天，超过 90 天红线"},
                        {"heading": "结算账户", "sentences": ["…"]}]}]}]}

Clauses without ``answers`` are decoy prose: they must read like contract
language and must not name any rule.

Usage:
    python scripts/build_seeded_corpus.py --manifest eval_cases/corpus_v2_manifest.json
    python scripts/build_seeded_corpus.py --manifest … --check-only
    python scripts/build_seeded_corpus.py --manifest … --report-retrieval
Then evaluate the corpus it wrote:
    python scripts/evaluate_seeded_defects.py --cases eval_cases/seeded_defect_cases_v2.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import MAX_CLAUSE_CHARS, split_clauses  # noqa: E402
from backend.playbooks import get_playbook  # noqa: E402

RETRIEVAL_BUDGET_CHARS = 6000
DEFAULT_MIN_CHARS = 8000
DEFAULT_MAX_CHARS = 14000
RATINGS = ("red", "amber", "green")

# Guidance is written as 「账期不超过 60 天为绿；61–90 天为黄；超过 90 天或未约定为红」,
# so the thresholds and the colour bands are parseable without an LLM.
_THRESHOLD_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|天|日|小时|分钟|个月|年|万元|元|次)")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_STOPWORD_CHARS = set("的为与或不在于经向其按照根据本对其进行了和如有所未任")


# --------------------------------------------------------------------------- spec


@dataclass
class ClauseSpec:
    """One contract clause: a title, the sentences under it, and what it answers."""

    heading: str
    sentences: list[str]
    answers: str | None = None
    kind: str | None = None  # clean | defect
    expected_rating: str | None = None
    note: str = ""

    @property
    def body(self) -> str:
        return "\n".join(self.sentences)

    def probe_candidates(self) -> list[str]:
        """Prefixes of this clause's sentences, shortest first.

        Real contracts open several clauses with the same boilerplate, so the
        first sentence is a convenient probe rather than a valid one; the
        shortest prefix that resolves uniquely is what identifies the clause.
        """

        return ["\n".join(self.sentences[: size + 1]) for size in range(len(self.sentences))]


@dataclass
class DocumentSpec:
    id: str
    title: str
    playbook_id: str
    parts: list[list[ClauseSpec]]
    part_titles: list[str] = field(default_factory=list)
    preamble: list[str] = field(default_factory=list)
    gap_notes: dict[str, str] = field(default_factory=dict)

    @property
    def clauses(self) -> list[ClauseSpec]:
        return [clause for part in self.parts for clause in part]


_CN_DIGITS = "零一二三四五六七八九十"


def _cn(numeral: int) -> str:
    if numeral < 10:
        return _CN_DIGITS[numeral]
    tens, ones = divmod(numeral, 10)
    prefix = "" if tens == 1 else _CN_DIGITS[tens]
    suffix = "" if ones == 0 else _CN_DIGITS[ones]
    return f"{prefix}十{suffix}"


def load_manifest(path: Path) -> tuple[dict, list[DocumentSpec]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    documents: list[DocumentSpec] = []
    for entry in raw["documents"]:
        parts = [
            [
                ClauseSpec(
                    heading=clause["heading"].strip(),
                    sentences=[sentence.strip() for sentence in clause["sentences"] if sentence.strip()],
                    answers=clause.get("answers"),
                    kind=clause.get("kind"),
                    expected_rating=clause.get("expected_rating"),
                    note=clause.get("note", ""),
                )
                for clause in part["clauses"]
            ]
            for part in entry["parts"]
        ]
        documents.append(
            DocumentSpec(
                id=entry["id"],
                title=entry["title"],
                playbook_id=entry.get("playbook_id") or defaults.get("playbook_id"),
                parts=parts,
                part_titles=entry.get("part_titles", []),
                preamble=entry.get("preamble", []),
                gap_notes=entry.get("gap_notes", {}),
            )
        )
    return defaults, documents


def render_document(spec: DocumentSpec) -> str:
    """Number every clause ``<part>.<index>`` — the form the splitter keys on."""

    lines = [spec.title, ""]
    if spec.preamble:
        lines.extend(spec.preamble)
        lines.append("")
    for part_number, clauses in enumerate(spec.parts, start=1):
        title = spec.part_titles[part_number - 1] if len(spec.part_titles) >= part_number else "正文"
        lines.append(f"第{_cn(part_number)}部分 {title}")
        for clause_number, clause in enumerate(clauses, start=1):
            lines.append(f"{part_number}.{clause_number} {clause.heading}")
            lines.extend(clause.sentences)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- rule vocabulary


def playbook_rules(playbook_id: str) -> dict[str, dict]:
    playbook = get_playbook(playbook_id)
    return {
        seed["name"]: {"dimension": seed.get("dimension", ""), "guidance": seed.get("guidance", "")}
        for seed in playbook.rule_seeds
    }


def _content_bigrams(text: str) -> set[str]:
    """CJK bigrams minus stopword pairs — the shared-vocabulary signal."""

    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        grams.update(first + second for first, second in zip(run, run[1:]))
    return {gram for gram in grams if gram[0] not in _STOPWORD_CHARS and gram[1] not in _STOPWORD_CHARS}


def guidance_elements(guidance: str) -> dict:
    return {
        "thresholds": [match.group(0).strip() for match in _THRESHOLD_RE.finditer(guidance)],
        "grams": _content_bigrams(guidance),
    }


def shared_vocabulary(clause_text: str, guidance: str) -> tuple[int, list[str]]:
    grams = _content_bigrams(clause_text) & guidance_elements(guidance)["grams"]
    return len(grams), sorted(grams)


# ------------------------------------------------------------------------- gold


def locate_clauses(text: str, spec: DocumentSpec) -> dict[int, int]:
    """Map clause index → parsed ordinal by re-finding each clause in the splitter output.

    This is the whole point of the builder: the gold ordinal is whatever the
    engine's own parser says it is, not what the manifest hoped for.
    """

    parsed = split_clauses(text)
    positions: dict[int, int] = {}
    for index, clause in enumerate(spec.clauses):
        hits: list[int] = []
        for probe in clause.probe_candidates():
            hits = [item.ordinal for item in parsed if probe in item.text]
            if len(hits) == 1:
                break
        if len(hits) != 1:
            raise ValueError(
                f"{spec.id}：条款「{clause.heading}」在切分结果中命中 {len(hits)} 段"
                "（须恰好 1 段；整段正文仍重复或找不到，说明这条标注无法定位）"
            )
        positions[index] = hits[0]
    return positions


def derive_gold(spec: DocumentSpec, *, document_relpath: str, defaults: dict) -> dict:
    rules = playbook_rules(spec.playbook_id)
    text = render_document(spec)
    positions = locate_clauses(text, spec)

    defects: list[dict] = []
    clean: list[str] = []
    retrieval_gold: dict[str, list[int]] = {}
    answered: set[str] = set()

    for index, clause in enumerate(spec.clauses):
        if not clause.answers:
            continue
        rule = clause.answers
        ordinal = positions[index]
        retrieval_gold.setdefault(rule, []).append(ordinal)
        if rule in answered:
            raise ValueError(f"{spec.id}：规则 {rule} 被多条条款标注，金标必须唯一")
        answered.add(rule)
        kind = clause.kind or "clean"
        if kind == "defect":
            defects.append(
                {
                    "rule": rule,
                    "expected_rating": clause.expected_rating or "red",
                    "planted": clause.note or clause.heading,
                }
            )
        elif kind == "clean":
            clean.append(rule)
        else:
            raise ValueError(f"{spec.id}：规则 {rule} 的 kind={kind} 不支持（clean|defect）")

    gaps = [
        {"rule": rule, "missing": spec.gap_notes.get(rule, "全文未约定该事项")}
        for rule in rules
        if rule not in answered
    ]
    return {
        "id": spec.id,
        "title": spec.title,
        "playbook_id": spec.playbook_id,
        "document": document_relpath,
        "min_chars": int(defaults.get("min_chars", DEFAULT_MIN_CHARS)),
        "max_chars": int(defaults.get("max_chars", DEFAULT_MAX_CHARS)),
        "defects": defects,
        "gaps": gaps,
        "clean": clean,
        "retrieval_gold": {rule: sorted(set(ordinals)) for rule, ordinals in retrieval_gold.items()},
    }


# ---------------------------------------------------------------------- preflight


def preflight(cases: list[dict], documents: dict[str, tuple[DocumentSpec, str]]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors must be fixed before any paid run."""

    errors: list[str] = []
    warnings: list[str] = []

    for case in cases:
        spec, text = documents[case["id"]]
        rules = playbook_rules(case["playbook_id"])
        parsed = split_clauses(text)
        clause_chars = sum(len(clause.text) for clause in parsed)
        label = case["id"]

        labelled = (
            [item["rule"] for item in case["defects"]]
            + [item["rule"] for item in case["gaps"]]
            + list(case["clean"])
        )
        unknown = sorted({name for name in labelled if name not in rules})
        if unknown:
            errors.append(f"{label}：标注了剧本外的规则 {unknown}")
        uncovered = sorted(set(rules) - set(labelled))
        if uncovered:
            errors.append(f"{label}：剧本规则未被标注 {uncovered}")
        duplicated = sorted({name for name in labelled if labelled.count(name) > 1})
        if duplicated:
            errors.append(f"{label}：同一规则被标注多次 {duplicated}")

        if not case["min_chars"] <= len(text) <= case["max_chars"]:
            errors.append(f"{label}：正文 {len(text)} 字不在 [{case['min_chars']}, {case['max_chars']}] 区间")
        if clause_chars <= RETRIEVAL_BUDGET_CHARS:
            errors.append(
                f"{label}：条款正文仅 {clause_chars} 字，不超过检索预算 {RETRIEVAL_BUDGET_CHARS} 字，"
                "引擎会直投全文，这份文档考不到检索"
            )

        for offending in _rule_named_headings(spec, rules):
            errors.append(f"{label}：条款标题「{offending}」写出规则名，标题会替检索给出答案")
        for hint in _rule_hinting_headings(spec, rules):
            warnings.append(f"{label}：条款标题「{hint}」与规则名共享多个实词，检索仍可走捷径")

        for clause in spec.clauses:
            if not clause.answers:
                leaked = sorted(name for name in rules if name in clause.body)
                if leaked:
                    errors.append(f"{label}：诱饵条款「{clause.heading}」写出规则名 {leaked}")
                continue
            guidance = rules.get(clause.answers) or {}
            if not guidance:
                continue  # already reported as an out-of-playbook label
            thresholds = guidance_elements(guidance["guidance"])["thresholds"]
            shared, _ = shared_vocabulary(clause.body, guidance["guidance"])
            if thresholds and not _THRESHOLD_RE.search(clause.body):
                warnings.append(
                    f"{label}·{clause.answers}：条款无任何量化数值，"
                    f"而 guidance 阈值为 {thresholds}，红黄绿无从复核"
                )
            elif shared < MIN_SHARED_GRAMS:
                warnings.append(
                    f"{label}·{clause.answers}：条款与 guidance 共享实词二元组 {shared} 个"
                    f"（<{MIN_SHARED_GRAMS}），关键词检索可能找不到金标条款"
                )
            if len(clause.body) > MAX_CLAUSE_CHARS:
                warnings.append(
                    f"{label}：条款「{clause.heading}」正文 {len(clause.body)} 字，"
                    f"切分器会拆到 {MAX_CLAUSE_CHARS} 字以内，金标只覆盖首段"
                )

        for defect in case["defects"]:
            if defect["expected_rating"] not in ("red", "amber"):
                errors.append(f"{label}·{defect['rule']}：defect 的 expected_rating 须为 red/amber")
    return errors, warnings


# Rough floor: below this, the guidance and the clause barely speak the same language.
MIN_SHARED_GRAMS = 2


def _answered_headings(spec: DocumentSpec, rules: dict[str, dict]):
    for clause in spec.clauses:
        if clause.answers and clause.answers in rules:
            yield clause.heading, clause.answers


def _rule_named_headings(spec: DocumentSpec, rules: dict[str, dict]) -> list[str]:
    return [heading for heading, rule in _answered_headings(spec, rules) if rule in heading]


def _rule_hinting_headings(spec: DocumentSpec, rules: dict[str, dict]) -> list[str]:
    """Headings that never spell the rule name but share most of its content words."""

    hinted = []
    for heading, rule in _answered_headings(spec, rules):
        if rule in heading:
            continue
        rule_grams = _content_bigrams(rule)
        if len(_content_bigrams(heading) & rule_grams) >= max(2, len(rule_grams) - 1):
            hinted.append(heading)
    return hinted


# ------------------------------------------------------------------------ output


def write_corpus(cases: list[dict], documents: dict[str, tuple[DocumentSpec, str]], *, cases_path: Path) -> None:
    """Write the rendered text and the derived gold; paths come from the gold."""

    for case in cases:
        target = PROJECT_ROOT / case["document"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(documents[case["id"]][1], encoding="utf-8")
    cases_path = Path(cases_path)
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")


def retrieval_replay(cases: list[dict], documents: dict[str, tuple[DocumentSpec, str]]) -> list[dict]:
    """Zero-cost keyword replay: would the live selector even find the gold clause?

    The engine builds its query as ``f"{rule.name} {rule.guidance}"``, so the
    replay uses exactly that string instead of inventing a nicer one.
    """

    from backend.engine.retrieval import select_clauses

    rows: list[dict] = []
    for case in cases:
        spec, text = documents[case["id"]]
        clauses = split_clauses(text)
        rules = playbook_rules(case["playbook_id"])
        for rule, ordinals in sorted(case["retrieval_gold"].items()):
            guidance = rules[rule]["guidance"]
            selected = select_clauses(clauses, f"{rule} {guidance}", mode="keyword")
            chosen = [clause.ordinal for clause in selected]
            gold = set(ordinals)
            ranks = [index + 1 for index, ordinal in enumerate(chosen) if ordinal in gold]
            rows.append(
                {
                    "case": case["id"],
                    "rule": rule,
                    "gold": sorted(gold),
                    "selected": chosen,
                    "gold_rank": min(ranks) if ranks else None,
                    "reachable": bool(ranks),
                }
            )
    return rows


def build(manifest: Path, defaults_hint: dict | None = None) -> tuple[list[dict], dict, dict]:
    defaults, specs = load_manifest(manifest)
    if defaults_hint:
        defaults = {**defaults, **defaults_hint}
    documents: dict[str, tuple[DocumentSpec, str]] = {}
    cases: list[dict] = []
    out_dir = Path(defaults.get("out_dir", "eval_cases/seeded_defects_v2"))
    for spec in specs:
        text = render_document(spec)
        documents[spec.id] = (spec, text)
        relpath = out_dir / f"{spec.id}.txt"
        cases.append(derive_gold(spec, document_relpath=str(relpath), defaults=defaults))
    return cases, documents, {"out_dir": out_dir, "cases": defaults.get("cases_path")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, help="语料目录（默认取 manifest.defaults.out_dir）")
    parser.add_argument("--cases", type=Path, help="金标 JSON 输出路径")
    parser.add_argument("--check-only", action="store_true", help="只跑预检，不写文件")
    parser.add_argument("--report-retrieval", action="store_true", help="零花费关键词重放，报告金标可达性")
    args = parser.parse_args()

    hint = {}
    if args.out_dir:
        hint["out_dir"] = args.out_dir
    if args.cases:
        hint["cases_path"] = str(args.cases)
    try:
        cases, documents, meta = build(args.manifest, hint)
    except ValueError as exc:
        print(f"[FAIL] 金标无法从语料导出：{exc}")
        return 1
    errors, warnings = preflight(cases, documents)

    for warning in warnings:
        print(f"[WARN] {warning}")
    for error in errors:
        print(f"[FAIL] {error}")
    print(
        f"[INFO] {len(cases)} 份文档 · {sum(len(c['defects']) for c in cases)} 个埋入缺陷 · "
        f"{sum(len(c['gaps']) for c in cases)} 个缺口 · {sum(len(c['clean']) for c in cases)} 条合规诱饵"
    )

    if args.report_retrieval:
        rows = retrieval_replay(cases, documents)
        blocked = [row for row in rows if not row["reachable"]]
        print(
            f"[INFO] 关键词重放：{len(rows)} 条金标规则，"
            f"不可达 {len(blocked)} 条（阻断率 {len(blocked) / max(len(rows), 1):.1%}）"
        )
        for row in blocked:
            print(f"       不可达：{row['case']} · {row['rule']} 金标 {row['gold']}")
        replay_path = PROJECT_ROOT / "reports" / "seeded_corpus_replay.json"
        replay_path.parent.mkdir(parents=True, exist_ok=True)
        replay_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] 明细写入 {replay_path}（本地诊断件：含规则名与金标 ordinal，不要提交入库）")

    if errors:
        print("[FAIL] 预检未通过，语料未写入。")
        return 1
    if args.check_only:
        print("[OK] 预检通过（--check-only，未写文件）")
        return 0

    cases_path = Path(meta["cases"]) if meta["cases"] else PROJECT_ROOT / "eval_cases" / "seeded_defect_cases_v2.json"
    write_corpus(cases, documents, cases_path=cases_path)
    print(f"[OK] 语料写入 {meta['out_dir']}，金标写入 {cases_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
