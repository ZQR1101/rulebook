"""Which contract party is 我方 has to be declared, not inferred.

Several rules key their grade off 我方 versus 对方 — 适用法律 (whose venue),
知识产权归属 (who owns the deliverable), 审计权 (who may audit). Neither the
playbook nor a contract states which party the reviewer represents, so the
scoring model has been inferring it per document: on two fixtures whose
governing-law clauses are near-identical (「适用中华人民共和国法律…提交甲方所在地
法院」) it called 甲方-venue green in one and amber in the other, and greedy
decoding reproduced the flip. These tests pin the declaration at the surface
that matters — the prompt actually sent to the model.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.engine.parsing import ParsedClause
from backend.engine.scoring import _build_prompt
from backend.playbooks import get_playbook

_GOVERNING_LAW_CLAUSE = ParsedClause(
    ordinal=1,
    heading="适用法律与争议解决",
    text="本合同适用中华人民共和国法律。因本合同产生的争议，提交甲方所在地有管辖权的人民法院解决。",
)


def _prompt_for(playbook_id: str, rule_name: str) -> str:
    playbook = get_playbook(playbook_id)
    seed = next(item for item in playbook.rule_seeds if item["name"] == rule_name)
    rule = SimpleNamespace(name=seed["name"], dimension=seed["dimension"], guidance=seed["guidance"])
    return _build_prompt(rule, [_GOVERNING_LAW_CLAUSE], playbook.scoring_instructions)


def test_the_review_party_is_declared_instead_of_guessed():
    prompt = _prompt_for("contract-compliance", "适用法律")

    assert "我方＝采购方" in prompt
    assert "不得从条款行文推测立场" in prompt


def test_the_two_playbooks_take_opposite_sides():
    """Supplier contracts are read for the buyer; SOWs are read for the vendor.

    A shared boilerplate instruction would silently hand one vertical the other
    vertical's stance, which is the exact ambiguity that flipped the venue grade.
    """

    assert "我方＝交付方" in _prompt_for("delivery-intake", "责任与赔偿")
    assert "我方＝采购方" in _prompt_for("contract-compliance", "责任上限")


def test_a_rule_that_never_names_a_party_still_gets_the_declaration():
    """交付与验收条款 has no 我方 in its guidance; the stance must not be
    smuggled in as a reason to downgrade a clause for a missing element."""

    prompt = _prompt_for("contract-compliance", "交付与验收条款")

    assert "我方＝采购方" in prompt
