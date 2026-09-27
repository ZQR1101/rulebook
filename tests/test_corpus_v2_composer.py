"""The v2 composer must produce a corpus whose labels survive inspection.

v1 was too easy (a general agent with no customization matched the engine), so v2
is long, de-titled and dense with boilerplate. Length alone would make the corpus
*worse* than v1 if the labels drifted, and these properties are what keep it
honest. All of them are checked without a model call.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import split_clauses  # noqa: E402
from scripts import build_seeded_corpus as builder  # noqa: E402
from scripts import compose_corpus_v2 as composer  # noqa: E402
from scripts.corpus_v2_library import GAP_SIGNATURES  # noqa: E402

SEED = 20260922
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@pytest.fixture(scope="module")
def manifest() -> dict:
    specs, _ = composer.compose_all(SEED)
    return {
        "defaults": {"out_dir": "eval_cases/seeded_defects_v2"},
        "documents": [composer.spec_to_manifest(spec) for spec in specs],
    }


@pytest.fixture(scope="module")
def derived(manifest, tmp_path_factory) -> tuple[list[dict], dict]:
    path = tmp_path_factory.mktemp("corpus") / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    cases, documents, _ = builder.build(path)
    return cases, documents


def test_document_count_and_volume_band(derived):
    """B 档约定：16 份合同 + 8 份交付件，每份 8k~14k 字，且必须超出检索预算。"""
    cases, documents = derived
    assert len(cases) == 24
    assert sum(1 for case in cases if case["playbook_id"] == "contract-compliance") == 16
    assert sum(1 for case in cases if case["playbook_id"] == "delivery-intake") == 8
    for case in cases:
        text = documents[case["id"]][1]
        assert 8000 <= len(text) <= 14000, f"{case['id']} 正文 {len(text)} 字越界"
        clause_chars = sum(len(clause.text) for clause in split_clauses(text))
        assert clause_chars > builder.RETRIEVAL_BUDGET_CHARS, f"{case['id']} 短于检索预算，考不到检索"


def test_preflight_reports_no_errors(derived):
    cases, documents = derived
    errors, _ = builder.preflight(cases, documents)
    assert errors == []


def test_each_document_carries_enough_anchors(derived):
    """考卷规格：每份文档至少 3 个埋入缺陷、2 个缺口，否则这份文档考不出东西。"""
    cases, _ = derived
    for case in cases:
        assert len(case["defects"]) >= 3, f"{case['id']} 只有 {len(case['defects'])} 个缺陷"
        assert len(case["gaps"]) >= 2, f"{case['id']} 只有 {len(case['gaps'])} 个缺口"


def test_every_gap_is_really_absent_from_the_document(derived):
    """「文档未约定」 must be a statement about the text, not about our labelling."""
    cases, documents = derived
    for case in cases:
        text = documents[case["id"]][1]
        for gap in case["gaps"]:
            hits = [term for term in GAP_SIGNATURES[gap["rule"]] if term in text]
            assert not hits, f"{case['id']}·{gap['rule']}：正文别处出现签名词 {hits}，缺口不成立"


def test_no_rule_is_labelled_twice(derived):
    cases, _ = derived
    for case in cases:
        labels = (
            [item["rule"] for item in case["defects"]]
            + [item["rule"] for item in case["gaps"]]
            + list(case["clean"])
        )
        assert len(labels) == len(set(labels))


@pytest.mark.parametrize(
    ("rule", "rating", "band"),
    [
        ("付款账期", "green", (1, 60)),
        ("付款账期", "amber", (61, 90)),
        ("数据泄露通知", "green", (1, 72)),
        ("数据泄露通知", "amber", (73, 168)),
    ],
)
def test_threshold_bearing_clause_sits_in_its_guidance_band(derived, rule, rating, band):
    """The number the corpus plants must fall in the band the guidance names."""
    cases, documents = derived
    checked = 0
    for case in cases:
        gold = case["retrieval_gold"].get(rule)
        if not gold:
            continue
        if _labelled_as(case, rule) != rating:
            continue
        clauses = {clause.ordinal: clause.text for clause in split_clauses(documents[case["id"]][1])}
        numbers = [float(value.replace(",", "")) for value in NUMBER.findall(clauses[gold[0]])]
        in_band = [value for value in numbers if band[0] <= value <= band[1]]
        assert in_band, f"{case['id']}·{rule} 期望 {rating}（{band[0]}~{band[1]}），条款数值只有 {numbers}"
        checked += 1
    assert checked, f"语料中没有 {rule}={rating} 的样本可核对"


def _labelled_as(case: dict, rule: str) -> str:
    """The colour this document's gold assigns to ``rule`` (gap when unanswered)."""
    for defect in case["defects"]:
        if defect["rule"] == rule:
            return defect["expected_rating"]
    return "green" if rule in case["clean"] else "gap"


def test_composition_is_reproducible():
    first, _ = composer.compose_all(SEED)
    second, _ = composer.compose_all(SEED)
    assert [builder.render_document(a) for a in first] == [builder.render_document(b) for b in second]


# String hash order is fixed inside one process, so the test above cannot see a
# plan that depends on it: the colouring loop used to iterate a *set* while drawing
# from the rng, which meant PYTHONHASHSEED decided what the corpus looked like.
_COMPOSE_SEVERAL = '''
import hashlib, random, sys
sys.path.insert(0, sys.argv[1])
from scripts.build_seeded_corpus import render_document
from scripts.compose_corpus_v2 import compose_document
from scripts.corpus_v2_library import LIBRARIES

usage = {
    rule: {"gap": 0, "defect": 0, "red": 0, "amber": 0}
    for library in LIBRARIES.values()
    for rule in library
}
digest = hashlib.sha256()
for playbook_id in LIBRARIES:
    rng = random.Random(f"{sys.argv[2]}:{playbook_id}")
    for index in range(3):
        spec = compose_document(playbook_id, f"{playbook_id}-{index}", rng, usage)
        digest.update(render_document(spec).encode("utf-8"))
print(digest.hexdigest())
'''


def _digest_under_hash_seed(hash_seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    finished = subprocess.run(
        [sys.executable, "-c", _COMPOSE_SEVERAL, str(PROJECT_ROOT), str(SEED)],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    return finished.stdout.strip()


def test_composition_survives_a_different_string_hash_order():
    digests = {_digest_under_hash_seed(seed) for seed in ("0", "999")}
    assert len(digests) == 1, f"同一个 seed 在不同 PYTHONHASHSEED 下组出不同语料：{digests}"


def test_manifest_round_trips_through_the_loader(manifest, tmp_path):
    """The manifest is the durable artifact; it must load back as the same corpus."""
    specs, _ = composer.compose_all(SEED)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _, loaded = builder.load_manifest(path)
    assert [builder.render_document(a) for a in loaded] == [builder.render_document(b) for b in specs]


def test_every_rule_is_attacked_at_least_once(derived):
    """A rule that is never violated and never missing is not being examined."""
    cases, _ = derived
    attacked = {item["rule"] for case in cases for item in case["defects"]}
    missing = {item["rule"] for case in cases for item in case["gaps"]}
    assert attacked >= set(composer.LIBRARIES["contract-compliance"])
    assert attacked >= set(composer.LIBRARIES["delivery-intake"])
    assert missing, "整套语料里没有任何缺口，缺失判定这一路没被考到"


def test_committed_gold_references_documents_portably():
    """金标在 Linux/CI 上也要读得回来：反斜杠会被当成一个文件名而不是目录分隔。"""
    cases = json.loads(
        (PROJECT_ROOT / "eval_cases" / "seeded_defect_cases_v2.json").read_text(encoding="utf-8")
    )["cases"]
    for case in cases:
        document = case["document"]
        assert "\\" not in document, f"{case['id']} 的 document 含反斜杠：{document}"
        assert not Path(document).is_absolute(), f"{case['id']} 的 document 必须是相对路径"
        assert (PROJECT_ROOT / document).exists(), f"{case['id']} 指向的语料文件不存在：{document}"


def test_an_ungradeable_absence_never_buys_a_gap_slot():
    """「不切实际的承诺」的红档要先存在一个可判不可达的承诺；条款缺席不构成结论。

    把这种规则标成缺口，等于先答错再问模型：keyword 与 oracle 两臂 3/3 判 green。
    """

    for playbook_id, library in composer.LIBRARIES.items():
        rules = tuple(library)
        for seed in range(60):
            usage = {rule: {"gap": 0, "defect": 0, "red": 0, "amber": 0} for rule in rules}
            plan = composer._plan(random.Random(seed), rules, library, usage)
            if plan is None:
                continue
            gaps, _ = plan
            assert not set(gaps) & composer.ABSENCE_NOT_GRADED, (
                f"{playbook_id} seed={seed} 把不可判的缺席当缺口"
            )


def test_every_gap_declares_the_band_its_absence_actually_earns(derived):
    """金标必须自带预期档位，打分器不再靠"缺口就是红"这个假设。"""
    cases, _ = derived
    seen = set()
    for case in cases:
        for gap in case["gaps"]:
            seen.add(gap["rule"])
            declared = composer.GAP_BAND_OVERRIDES.get(
                (case["id"], gap["rule"]), composer.ABSENCE_BAND.get(gap["rule"], "red")
            )
            assert gap["expected_rating"] == declared, (
                f"{case['id']} 的缺口「{gap['rule']}」档位与声明不符：{gap}"
            )
    assert seen & set(composer.ABSENCE_BAND), "整套语料里没有任何黄档缺口，这条口径没被考到"


def test_band_overrides_are_the_only_way_to_leave_the_per_rule_default(derived):
    """按文档改档位是裁定，不是调参：偏离默认的缺口行必须逐条记在覆盖表里。"""

    cases, _ = derived
    for (doc, rule), band in composer.GAP_BAND_OVERRIDES.items():
        assert band in ("red", "amber"), f"{doc}·{rule} 的覆盖档位非法：{band}"
        gaps = {gap["rule"] for gap in next(c for c in cases if c["id"] == doc)["gaps"]}
        assert rule in gaps, f"{doc}·{rule} 写档位在未作缺口的规则上，等于悄悄失效"

    deviating = {
        (case["id"], gap["rule"])
        for case in cases
        for gap in case["gaps"]
        if gap["expected_rating"] != composer.ABSENCE_BAND.get(gap["rule"], "red")
    }
    assert deviating == set(composer.GAP_BAND_OVERRIDES), (
        f"有缺口档位偏离默认却不在覆盖表里：{sorted(deviating ^ set(composer.GAP_BAND_OVERRIDES))}"
    )


def test_a_compliant_clause_never_cancels_its_own_limit():
    """绿色模板曾一边设上限、一边把超限部分推回责任方，16 份合同里 8 份因此判红。"""

    unlimited = re.compile(r"超出[^。]{0,20}限额|不受[^。]{0,12}限额约束|不以[^。]{0,12}为限|无限责任")
    for playbook_id, library in composer.LIBRARIES.items():
        for rule, variants in library.items():
            green = "\n".join(variants["green"].sentences)
            assert not unlimited.search(green), f"{playbook_id}/{rule} 的绿色条款自我取消了限额：{green}"


# 知情未修：付款账期 amber「81 日内…顺延合计不超过 81 日」把同一个带档位的槽位叠了两次，
# 最长 162 日而 guidance 写「超过 90 天为红」⇒ 该档位在数学上不可达。keyword／整份直送／
# oracle 三臂在 CG-V2-05、CG-V2-11 上都照字面判红，理由里写出了这条算式。
BAND_DOUBLED_SLOTS = {("付款账期", "amber")}


def banded_slot_doublings() -> set[tuple[str, str]]:
    """(rule, band) pairs whose variant stacks a slot that carries a declared band range."""

    doubled = set()
    for library in composer.LIBRARIES.values():
        for rule, variants in library.items():
            for band, template in variants.items():
                text = " ".join(template.sentences)
                for slot in composer.BANDS.get((rule, band), {}):
                    if text.count("{" + slot + "}") > 1:
                        doubled.add((rule, band))
    return doubled


def test_a_banded_slot_is_not_stacked_without_being_accounted_for():
    """自我抵消的档位模板要么不存在，要么必须登记在案，不许悄悄多出一条。"""

    doubled = banded_slot_doublings()
    assert doubled == BAND_DOUBLED_SLOTS, (
        f"档位模板的自我抵消清单变了：新增 {sorted(doubled - BAND_DOUBLED_SLOTS)}，"
        f"已消除 {sorted(BAND_DOUBLED_SLOTS - doubled)}"
    )


def test_a_self_cancelling_band_keeps_its_label_until_the_text_moves(derived):
    """文本没改就不许单独把标签改成红——那是把语料缺陷洗成模型失误。"""

    cases, documents = derived
    stacked = re.compile(r"顺延合计不超过 \{?\d+\}? 日")
    for case in cases:
        row = next((d for d in case["defects"] if d["rule"] == "付款账期"), None)
        if row is None or row["expected_rating"] != "amber":
            continue
        text = documents[case["id"]][1]
        assert stacked.search(text), (
            f"{case['id']} 的付款账期仍标 amber，但正文里的叠算句已不在：改模板必须同时重述标签"
        )
