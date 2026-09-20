"""Inbox watcher tests: auto-intake, dedup, and dead-letter paths."""

from __future__ import annotations

import pytest

from test_engine_pipeline import (
    SAMPLE_CONTRACT,
    FakeLLM,
    admin_headers,
    client,
    fake_llm,
    platform_env,
)


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    watched = tmp_path / "inbox"
    archive = tmp_path / "inbox_archive"
    watched.mkdir()
    monkeypatch.setenv("INBOX_DIR", str(watched))
    monkeypatch.setenv("INBOX_ARCHIVE_DIR", str(archive))
    return {"dir": watched, "archive": archive, "dead": archive / "dead_letter"}


@pytest.fixture(autouse=True)
def _reset_seen_state():
    from backend.engine import inbox_watcher

    inbox_watcher._SEEN_SIZES.clear()
    yield
    inbox_watcher._SEEN_SIZES.clear()


def _poll(playbook: str = "contract-compliance", settle: float = 0.0):
    from backend.engine.inbox_watcher import poll_once

    return poll_once(playbook_id=playbook, settle_interval=settle)


class TestInboxIntake:
    def test_happy_path_document_ingested_and_reviewed(self, client, inbox, fake_llm, admin_headers):
        (inbox["dir"] / "合同A.txt").write_text(SAMPLE_CONTRACT, encoding="utf-8")
        stats = _poll()
        assert stats.ingested == 1
        assert stats.duplicates == 0
        assert stats.dead_lettered == 0
        assert not (inbox["dir"] / "合同A.txt").exists()
        assert (inbox["archive"] / "合同A.txt").exists()

        documents = client.get("/documents", headers=admin_headers).json()["documents"]
        assert len(documents) == 1
        assert documents[0]["status"] == "awaiting_review"

    def test_duplicate_file_archived_not_reingested(self, client, inbox, fake_llm, admin_headers):
        (inbox["dir"] / "a.txt").write_text(SAMPLE_CONTRACT, encoding="utf-8")
        first = _poll()
        assert first.ingested == 1

        (inbox["dir"] / "b.txt").write_text(SAMPLE_CONTRACT, encoding="utf-8")
        second = _poll()
        assert second.ingested == 0
        assert second.duplicates == 1
        assert (inbox["archive"] / "b.txt").exists()

        documents = client.get("/documents", headers=admin_headers).json()["documents"]
        assert len(documents) == 1

    def test_unsupported_suffix_ignored(self, client, inbox, fake_llm):
        (inbox["dir"] / "virus.exe").write_bytes(b"MZ...")
        stats = _poll()
        assert stats.ingested == 0
        assert (inbox["dir"] / "virus.exe").exists()

    def test_unsettled_file_waits(self, client, inbox, fake_llm):
        path = inbox["dir"] / "growing.txt"
        # 内容必须可评审：入库闸会拒绝切不出 3 段条款的正文，本测试只验 settle 等待
        path.write_text(SAMPLE_CONTRACT, encoding="utf-8")
        stats = _poll(settle=2.0)
        assert stats.pending == 1
        assert stats.ingested == 0
        # stable second pass ingests
        second = _poll(settle=2.0)
        assert second.ingested == 1


class TestDeadLetter:
    def test_unreadable_file_dead_lettered(self, client, inbox, fake_llm, monkeypatch):
        path = inbox["dir"] / "locked.txt"
        path.write_text(SAMPLE_CONTRACT, encoding="utf-8")

        def broken_read_bytes(self, *args, **kwargs):
            raise OSError("file locked")

        monkeypatch.setattr("pathlib.Path.read_bytes", broken_read_bytes)
        stats = _poll()
        assert stats.dead_lettered == 1
        monkeypatch.undo()
        assert (inbox["dead"] / "locked.txt").exists()
        assert not path.exists()


class TestWatcherLoop:
    def test_once_mode_returns_stats(self, client, inbox, fake_llm):
        from backend.engine.inbox_watcher import run_watcher

        (inbox["dir"] / "合同B.txt").write_text(SAMPLE_CONTRACT, encoding="utf-8")
        stats = run_watcher(playbook_id="contract-compliance", once=True, settle_interval=0.0)
        assert stats.ingested == 1
