"""Engine pipeline tests: end-to-end review with a scripted fake LLM.

Covers the three mandated paths — happy, duplicate, failure — plus the toy
third playbook proving the engine is playbook-agnostic (adding a vertical =
data only). No network, no real LLM.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


class FakeLLM:
    """Scripted LLM: returns rule-specific or default JSON payloads."""

    def __init__(self, *, default: dict | None = None, script: dict[str, dict] | None = None, junk: bool = False):
        self.default = default or {
            "rating": "green",
            "rationale": "条款内容满足规则要求",
            "citations": [{"quote": "SAMPLE QUOTE"}],
            "gap_reason": "",
        }
        self.script = script or {}
        self.junk = junk
        self.calls: list[str] = []

    def invoke(self, prompt: str):
        self.calls.append(prompt)

        class Response:
            def __init__(self, content: str):
                self.content = content

        if self.junk:
            return Response("抱歉，我无法输出 JSON。")
        for name, payload in self.script.items():
            if name in prompt:
                return Response(json.dumps(payload, ensure_ascii=False))
        return Response(json.dumps(self.default, ensure_ascii=False))


@pytest.fixture()
def fake_llm(monkeypatch):
    """Inject a controllable fake LLM into the scoring agent."""

    instance = FakeLLM()
    monkeypatch.setattr(
        "backend.engine.scoring._default_scoring_llm", lambda: instance
    )
    return instance


SAMPLE_CONTRACT = """服务采购合同

第1条 付款方式
本合同服务费为每月人民币10万元，乙方应于每月服务完成后60日内开具发票，甲方在收到发票后45日内付款。SAMPLE QUOTE

第2条 责任限制
乙方在本合同项下的累计赔偿责任总额不超过本合同年度总服务费的100%。SAMPLE QUOTE

第3条 保密与数据保护
乙方应对甲方数据采取加密与访问控制措施，保密义务在合同终止后存续。SAMPLE QUOTE

第4条 终止
任何一方均可提前30日书面通知对方终止本合同；违约方被终止时立即生效。SAMPLE QUOTE

第5条 服务水平
乙方承诺服务可用性达到99.95%，P1故障响应时间不超过1小时，未达标按月度服务费5%支付服务抵扣。SAMPLE QUOTE
"""


@pytest.fixture()
def platform_env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform" / "rulebook.db"
    monkeypatch.setenv("PLATFORM_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTH_SECRET", "test-secret-for-platform-tests-0123456789abcdef")
    monkeypatch.setenv("PLATFORM_DOCS_DIR", str(tmp_path / "data" / "review_documents"))

    from backend.platform_db import init_platform_db, reset_platform_db

    reset_platform_db()
    init_platform_db()
    yield {"db_path": db_path}
    reset_platform_db()


@pytest.fixture()
def client(platform_env):
    from backend.server import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(client, platform_env):
    from pathlib import Path

    pwd_file = Path(platform_env["db_path"]).parent / "bootstrap_admin_password.txt"
    password = ""
    for line in pwd_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("password:"):
            password = line.removeprefix("password:").strip()
    login = client.post("/auth/login", json={"username": "admin", "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _upload(client, headers, name: str, content: bytes, playbook: str = "contract-compliance"):
    return client.post(
        "/documents/upload",
        files={"file": (name, content, "text/plain")},
        data={"playbook_id": playbook},
        headers=headers,
    )


class TestHappyPath:
    def test_end_to_end(self, client, admin_headers, fake_llm):
        upload = _upload(client, admin_headers, "合同.txt", SAMPLE_CONTRACT.encode("utf-8"))
        assert upload.status_code == 202, upload.text
        document_id = upload.json()["document_id"]

        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        document = detail["document"]
        assert document["status"] == "awaiting_review"
        assert document["friendly_id"].startswith("CG-")

        scorecard = document["scorecard"]
        assert scorecard["total_rules"] == 15
        assert scorecard["counts"]["green"] == 15
        assert scorecard["coverage_pct"] == 100

        verdicts = detail["verdicts"]
        assert len(verdicts) == 15
        assert all(v["review_state"] == "drafted" for v in verdicts)
        assert all(v["citations"] for v in verdicts)

        runs = client.get(f"/documents/{document_id}/runs", headers=admin_headers).json()["runs"]
        assert runs[0]["status"] == "succeeded"
        assert {stage["stage"] for stage in runs[0]["stages"]} == {"parse", "score", "scorecard"}
        assert len(fake_llm.calls) == 15

    def test_audit_trail_recorded(self, client, admin_headers, fake_llm):
        upload = _upload(client, admin_headers, "合同.txt", SAMPLE_CONTRACT.encode("utf-8"))
        document_id = upload.json()["document_id"]
        audit = client.get(f"/documents/{document_id}/audit", headers=admin_headers).json()["audit"]
        events = [entry["event"] for entry in audit]
        assert "document.created" in events
        assert "review.started" in events
        assert "review.parsed" in events
        assert "review.scored" in events
        assert "review.scorecard_ready" in events

    def test_second_playbook_on_same_engine(self, client, admin_headers, fake_llm):
        upload = _upload(client, admin_headers, "sow.txt", SAMPLE_CONTRACT.encode("utf-8"), playbook="delivery-intake")
        document_id = upload.json()["document_id"]
        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        assert detail["document"]["status"] == "awaiting_review"
        assert len(detail["verdicts"]) == 12
        assert detail["document"]["friendly_id"].startswith("DI-")


class TestDuplicatePath:
    def test_same_content_rejected_with_pointer(self, client, admin_headers, fake_llm):
        first = _upload(client, admin_headers, "a.txt", SAMPLE_CONTRACT.encode("utf-8"))
        assert first.status_code == 202
        second = _upload(client, admin_headers, "b.txt", SAMPLE_CONTRACT.encode("utf-8"))
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert detail["friendly_id"] == first.json()["friendly_id"]

    def test_duplicate_rejection_audited(self, client, admin_headers, fake_llm):
        _upload(client, admin_headers, "a.txt", SAMPLE_CONTRACT.encode("utf-8"))
        _upload(client, admin_headers, "b.txt", SAMPLE_CONTRACT.encode("utf-8"))
        recent = client.get("/documents/audit/recent", headers=admin_headers).json()["audit"]
        assert any(entry["event"] == "document.duplicate_rejected" for entry in recent)


class TestFailurePath:
    def test_llm_junk_yields_amber_awaiting_review(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(
            "backend.engine.scoring._default_scoring_llm", lambda: FakeLLM(junk=True)
        )
        upload = _upload(client, admin_headers, "c.txt", SAMPLE_CONTRACT.encode("utf-8"))
        document_id = upload.json()["document_id"]
        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        assert detail["document"]["status"] == "awaiting_review"
        for verdict in detail["verdicts"]:
            assert verdict["rating"] == "amber"
            assert verdict["review_state"] == "awaiting_review"
            assert "无法解析" in verdict["rationale"]

    def test_gap_rules_turn_red_with_reason(self, client, admin_headers, monkeypatch):
        thin_doc = (
            "项目服务合同\n\n第1条 服务内容\n乙方提供现场培训服务。\n\n"
            "第2条 合同期限\n本合同自签署之日起一年内有效。\n\n"
            "第3条 争议解决\n双方协商不成时提交签署地人民法院。\n"
        )
        monkeypatch.setattr(
            "backend.engine.scoring._default_scoring_llm",
            lambda: FakeLLM(
                default={
                    "rating": "red",
                    "rationale": "文档中未找到对应内容",
                    "citations": [],
                    "gap_reason": "文档未约定该事项",
                }
            ),
        )
        upload = _upload(client, admin_headers, "empty.txt", thin_doc.encode("utf-8"))
        document_id = upload.json()["document_id"]
        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        reds = [v for v in detail["verdicts"] if v["rating"] == "red"]
        assert reds
        assert all(v["gap_reason"] for v in reds)
        assert detail["document"]["scorecard"]["counts"]["red"] == len(reds)

    def test_intake_gate_refuses_a_stub_document(self, client, admin_headers, fake_llm):
        """A 21-character stub must not become 15 red verdicts in someone's queue."""

        stub = "软件开发合同 本合同约定开发一套演示系统。"
        upload = _upload(client, admin_headers, "stub.txt", stub.encode("utf-8"))
        assert upload.status_code == 202, upload.text
        document_id = upload.json()["document_id"]

        detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
        assert detail["document"]["status"] == "failed"
        assert "可评审下限" in detail["document"]["status_reason"]
        assert detail["verdicts"] == []
        assert fake_llm.calls == []  # refused before any model spend

    def test_green_without_citation_downgraded(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(
            "backend.engine.scoring._default_scoring_llm",
            lambda: FakeLLM(
                default={
                    "rating": "green",
                    "rationale": "看起来符合",
                    "citations": [{"quote": "这段话并不存在于文档中"}],
                    "gap_reason": "",
                }
            ),
        )
        upload = _upload(client, admin_headers, "d.txt", SAMPLE_CONTRACT.encode("utf-8"))
        document_id = upload.json()["document_id"]
        verdicts = client.get(f"/documents/{document_id}", headers=admin_headers).json()["verdicts"]
        assert verdicts
        for verdict in verdicts:
            assert verdict["rating"] == "amber"
            assert verdict["review_state"] == "awaiting_review"

    def test_invalid_format_rejected(self, client, admin_headers):
        response = client.post(
            "/documents/upload",
            files={"file": ("x.exe", b"MZ...", "application/octet-stream")},
            data={"playbook_id": "contract-compliance"},
            headers=admin_headers,
        )
        assert response.status_code == 415

    def test_unknown_playbook_rejected(self, client, admin_headers):
        response = _upload(client, admin_headers, "a.txt", b"content", playbook="nope")
        assert response.status_code in (400, 404, 500)


class TestToyThirdPlaybook:
    def test_new_vertical_is_data_only(self, client, admin_headers, fake_llm):
        from backend.playbooks import get_playbook, list_playbooks, register_playbook
        from backend.playbooks import registry
        from backend.playbooks.base import PlaybookSpec

        before = len(list_playbooks())
        toy = PlaybookSpec(
            id="toy-review",
            name="玩具剧本",
            description="证明引擎与剧本解耦",
            friendly_id_prefix="TY",
            dimensions=("维度甲",),
            rule_seeds=(
                {"dimension": "维度甲", "name": "甲规则", "guidance": "找到关键内容为绿"},
            ),
            scoring_instructions="玩具评分说明",
        )
        register_playbook(toy)
        try:
            assert len(list_playbooks()) == before + 1
            assert get_playbook("toy-review").name == "玩具剧本"

            upload = _upload(client, admin_headers, "toy.txt", SAMPLE_CONTRACT.encode("utf-8"), playbook="toy-review")
            document_id = upload.json()["document_id"]
            detail = client.get(f"/documents/{document_id}", headers=admin_headers).json()
            assert detail["document"]["status"] == "awaiting_review"
            assert len(detail["verdicts"]) == 1
            assert detail["verdicts"][0]["dimension"] == "维度甲"
            assert detail["document"]["friendly_id"].startswith("TY-")
        finally:
            registry._PLAYBOOKS.pop("toy-review", None)


class TestAccessControl:
    def test_requires_auth(self, client):
        assert client.get("/documents").status_code == 401
        assert client.get("/documents/playbooks").status_code == 401

    def test_playbooks_endpoint_lists_both(self, client, admin_headers):
        response = client.get("/documents/playbooks", headers=admin_headers)
        ids = {p["id"] for p in response.json()["playbooks"]}
        assert ids == {"contract-compliance", "delivery-intake", "dpa-review"}
