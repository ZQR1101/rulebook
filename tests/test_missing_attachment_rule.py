"""Missing attachments must not be charged to the clause under review.

The seeded-defect run's dominant false positive was a compliant clause downgraded
because it pointed at 附件一/附件二 that were not part of the provided text — the
single largest source of needless sign-off work. The guard lives in the scoring
prompt, so these tests assert the boundary actually reaches the model and does
not displace the citation requirement it sits next to.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_engine_pipeline import (  # noqa: F401
    SAMPLE_CONTRACT,
    _upload,
    admin_headers,
    client,
    fake_llm,
    platform_env,
)

from backend.engine.parsing import ParsedClause
from backend.engine.scoring import _build_prompt
from backend.playbooks import get_playbook


def _prompt_for(rule_name: str) -> str:
    playbook = get_playbook("contract-compliance")
    seed = next(item for item in playbook.rule_seeds if item["name"] == rule_name)
    rule = SimpleNamespace(name=seed["name"], dimension=seed["dimension"], guidance=seed["guidance"])
    clause = ParsedClause(ordinal=1, heading="交付与验收", text="验收标准详见附件一《验收标准》。")
    return _build_prompt(rule, [clause], playbook.scoring_instructions)


def test_attachment_boundary_reaches_the_model_on_every_call(fake_llm, client, admin_headers):
    upload = _upload(client, admin_headers, "合同.txt", SAMPLE_CONTRACT.encode("utf-8"))
    assert upload.status_code == 202, upload.text

    assert fake_llm.calls, "engine never prompted the scoring model"
    for prompt in fake_llm.calls:
        assert "附件内容不可核实不属于被审条款的缺陷" in prompt


def test_boundary_names_the_note_instead_of_a_downgrade():
    prompt = _prompt_for("交付与验收条款")

    assert "附件未提供，需补充核对" in prompt
    assert "判 green" in prompt


def test_boundary_keeps_the_citation_requirement_intact():
    prompt = _prompt_for("交付与验收条款")

    assert "green 判定必须至少有一条有效引用" in prompt
    assert "文档中找不到对应内容时 rating=red" in prompt
