"""Layer-2 usefulness analytics: did the expert keep the AI's first pass?

Two halves. Pure math over synthetic :class:`VerdictRecord`s — the overturn /
edit definitions are the claim the product rests on, so they are pinned here
rather than inferred from a demo run. And engine-level checks that the
``initial_rating`` baseline really survives an expert edit, because every
metric above is meaningless if the baseline gets overwritten.
"""

from __future__ import annotations

import pytest

from backend.analytics import (
    VerdictRecord,
    is_comparable,
    is_overturn,
    load_verdict_records,
    render_markdown,
    signoff_report,
    summarize,
)
from test_engine_pipeline import (  # noqa: F401  (fixtures shared via import)
    admin_headers,
    client,
    platform_env,
)
from test_governance import _find_verdict, _reviewed_document


def _record(**overrides) -> VerdictRecord:
    base = {
        "document_id": "doc-1",
        "playbook_id": "contract-compliance",
        "rule_name": "付款账期",
        "dimension": "付款与账期",
        "rating": "amber",
        "review_state": "approved",
        "ai_rating": "amber",
    }
    base.update(overrides)
    return VerdictRecord(**base)


class TestDefinitions:
    def test_approve_keeps_the_ai_judgement(self):
        record = _record(review_state="approved", rating="amber", ai_rating="amber")
        assert is_comparable(record)
        assert not is_overturn(record)

    @pytest.mark.parametrize("state", ["awaiting_review", "drafted"])
    def test_undecided_verdicts_are_neither_overturn_nor_comparable(self, state):
        record = _record(review_state=state, ai_rating=None)
        assert not is_overturn(record)
        assert not is_comparable(record)

    def test_reject_disagrees_even_without_a_baseline(self):
        record = _record(review_state="rejected", ai_rating=None)
        assert is_overturn(record)
        assert is_comparable(record)

    def test_rating_change_against_a_baseline_is_an_overturn(self):
        assert is_overturn(_record(review_state="edited", rating="red", ai_rating="amber"))
        assert not is_overturn(_record(review_state="edited", rating="amber", ai_rating="amber"))


class TestSummarize:
    def test_approve_only_bucket_has_zero_overturns(self):
        stats = summarize([_record() for _ in range(3)])
        assert stats["decided"] == 3
        assert stats["approve_rate"] == 1.0
        assert stats["overturns"] == 0
        assert stats["overturn_rate"] == 0.0
        assert stats["unknown_baseline"] == 0

    def test_edit_direction_and_mean_severity_shift(self):
        stats = summarize(
            [
                _record(rule_name="r1", review_state="edited", rating="red", ai_rating="amber"),
                _record(rule_name="r2", review_state="edited", rating="amber", ai_rating="red"),
            ]
        )
        assert (stats["rating_changes"], stats["overturns"]) == (2, 2)
        assert (stats["tightened"], stats["loosened"]) == (1, 1)
        assert stats["mean_severity_shift"] == 0.0
        assert stats["edit_rate"] == 1.0

    def test_rationale_only_edit_is_human_work_not_disagreement(self):
        stats = summarize(
            [
                _record(rule_name="r1", review_state="edited", rating="amber", ai_rating="amber"),
                _record(),
            ]
        )
        assert stats["overturns"] == 0
        assert stats["rationale_only_edits"] == 1
        assert stats["edit_rate"] == 0.5

    def test_legacy_rows_keep_rejects_but_lose_rating_changes(self):
        stats = summarize(
            [
                _record(rule_name="r1", review_state="approved", ai_rating=None),
                _record(rule_name="r2", review_state="approved", ai_rating=None),
                _record(rule_name="r3", review_state="rejected", rating="red", ai_rating=None),
            ]
        )
        assert stats["decided"] == 3
        assert stats["unknown_baseline"] == 3
        # Only the rejection is judgeable, and it is a disagreement.
        assert stats["overturns"] == 1
        assert stats["overturn_rate"] == 1.0
        assert stats["rating_changes"] == 0

    def test_engine_greens_report_separately_from_the_queue(self):
        stats = summarize([_record(review_state="drafted", rating="green", ai_rating="green")])
        assert stats["auto_passed"] == 1
        assert stats["decided"] == 0
        assert stats["approve_rate"] is None
        assert stats["overturn_rate"] is None

    def test_backlog_rate_tracks_signed_share_of_the_queue(self):
        stats = summarize(
            [
                _record(rule_name="queued", review_state="awaiting_review"),
                _record(rule_name="signed", review_state="approved"),
            ]
        )
        assert stats["review_backlog_rate"] == 0.5

    def test_documents_counted_once(self):
        stats = summarize([_record(rule_name=n) for n in ("r1", "r2", "r3")])
        assert (stats["verdicts"], stats["documents"]) == (3, 1)


class TestGrouping:
    def test_report_splits_playbook_dimension_and_rule(self):
        report = signoff_report(
            [
                _record(playbook_id="contract-compliance", dimension="付款与账期",
                        rule_name="常被推翻", review_state="edited",
                        rating="red", ai_rating="amber"),
                _record(document_id="doc-2", playbook_id="contract-compliance",
                        dimension="责任与赔偿", rule_name="常被驳回", rating="red",
                        review_state="rejected", ai_rating="red"),
                _record(document_id="doc-3", playbook_id="delivery-intake",
                        dimension="交付物", rule_name="照单全收"),
            ]
        )
        assert {row["playbook_id"] for row in report["by_playbook"]} == {
            "contract-compliance",
            "delivery-intake",
        }
        contract = next(
            row for row in report["by_playbook"] if row["playbook_id"] == "contract-compliance"
        )
        assert contract["verdicts"] == 2
        assert contract["overturns"] == 2
        assert report["by_dimension"][0]["dimension"]
        # Worst rule first, and the other playbook never contaminates it.
        assert [row["rule"] for row in report["by_rule"]][:1] == ["常被推翻"]
        assert report["by_rule"][-1]["overturns"] == 0

    def test_markdown_covers_both_empty_and_populated_reports(self):
        empty = render_markdown(signoff_report([]))
        assert empty.startswith("# 签字改判分析报告")
        assert "尚无任何专家签字记录" in empty

        filled = render_markdown(
            signoff_report(
                [
                    _record(review_state="edited", rating="red", ai_rating="amber"),
                    _record(rule_name="r2"),
                ]
            )
        )
        assert "| **判定被推翻率** | 50.0% |" in filled
        assert "## 分维度" in filled
        assert "## 说明与局限" in filled

    def test_fully_adopted_signoffs_are_not_reported_as_rejects(self):
        report = signoff_report([_record(), _record(rule_name="r2", review_state="edited")])
        markdown = render_markdown(report)
        assert "已处置判定无一被推翻" in markdown
        assert "驳回为主" not in markdown


class TestAgainstTheEngine:
    def test_initial_rating_survives_an_expert_edit(self, client, admin_headers, monkeypatch):
        detail = _reviewed_document(client, admin_headers, monkeypatch)
        document_id = detail["document"]["id"]
        verdict = _find_verdict(detail, "付款账期")
        assert verdict["rating"] == "amber"

        response = client.patch(
            f"/documents/{document_id}/verdicts/{verdict['id']}",
            json={"decision": "edit", "new_rating": "red", "expert_note": "账期过长"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

        from backend.platform_db import platform_session

        session = platform_session()
        try:
            records = {record.rule_name: record for record in load_verdict_records(session)}
            stats = summarize(load_verdict_records(session))
        finally:
            session.close()

        edited = records["付款账期"]
        assert (edited.rating, edited.ai_rating) == ("red", "amber")
        assert edited.review_state == "edited"
        assert is_overturn(edited)
        assert stats["decided"] == 1
        assert stats["overturns"] == 1
        assert stats["unknown_baseline"] == 0
        # The engine auto-passed every green rule; they never reach a human.
        assert stats["auto_passed"] == stats["verdicts"] - 1

    def test_review_event_records_the_rating_it_actually_changed(self, client, admin_headers,
                                                                monkeypatch):
        detail = _reviewed_document(client, admin_headers, monkeypatch)
        document_id = detail["document"]["id"]
        verdict = _find_verdict(detail, "付款账期")
        client.patch(
            f"/documents/{document_id}/verdicts/{verdict['id']}",
            json={"decision": "edit", "new_rating": "red"},
            headers=admin_headers,
        )

        from backend.documents.models import ReviewEvent
        from backend.platform_db import platform_session

        session = platform_session()
        try:
            event = (
                session.query(ReviewEvent)
                .filter(ReviewEvent.verdict_id == verdict["id"], ReviewEvent.action == "edit")
                .one()
            )
        finally:
            session.close()

        # Captured before the mutation, so the trail says what really happened.
        assert event.detail["rating_before"] == "amber"
        assert event.detail["rating_after"] == "red"
        assert event.detail["ai_rating"] == "amber"


class TestBaselineColumn:
    def test_legacy_database_gets_the_column_and_keeps_its_rows(self, tmp_path):
        from sqlalchemy import create_engine, inspect, text

        from backend.platform_db import _add_missing_columns

        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE verdicts (id VARCHAR(36) PRIMARY KEY, rating VARCHAR(10))")
            )
            connection.execute(text("INSERT INTO verdicts (id, rating) VALUES ('v1', 'amber')"))
        assert "initial_rating" not in {c["name"] for c in inspect(engine).get_columns("verdicts")}

        _add_missing_columns(engine)

        columns = inspect(engine).get_columns("verdicts")
        assert "initial_rating" in {c["name"] for c in columns}
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT initial_rating FROM verdicts WHERE id = 'v1'")
            ).scalar()
        assert value is None  # old rows = unknown baseline, never a fake agreement

        _add_missing_columns(engine)  # idempotent re-run on startup
        engine.dispose()

    def test_missing_table_is_skipped_not_fatal(self, tmp_path):
        from sqlalchemy import create_engine, inspect

        from backend.platform_db import _add_missing_columns, _backfill_initial_rating

        engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        _add_missing_columns(engine)  # nothing to alter yet, startup must not break
        _backfill_initial_rating(engine)
        assert inspect(engine).get_table_names() == []
        engine.dispose()

    def test_unedited_legacy_verdicts_get_their_baseline_back(self, tmp_path):
        from sqlalchemy import create_engine, text

        from backend.platform_db import _backfill_initial_rating

        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE verdicts (id VARCHAR(36) PRIMARY KEY, "
                    "rating VARCHAR(10), initial_rating VARCHAR(10))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE review_events (id VARCHAR(36) PRIMARY KEY, "
                    "verdict_id VARCHAR(36), action VARCHAR(50))"
                )
            )
            connection.execute(text("INSERT INTO verdicts (id, rating) VALUES ('kept', 'amber')"))
            connection.execute(text("INSERT INTO verdicts (id, rating) VALUES ('moved', 'red')"))
            connection.execute(
                text(
                    "INSERT INTO review_events (id, verdict_id, action) "
                    "VALUES ('e1', 'moved', 'edit')"
                )
            )

        _backfill_initial_rating(engine)

        with engine.connect() as connection:
            baselines = dict(
                connection.execute(text("SELECT id, initial_rating FROM verdicts")).fetchall()
            )
        # Only an untouched verdict still holds the engine's own grade. An edited
        # one has lost it, so it must stay in the "unknown baseline" bucket.
        assert baselines == {"kept": "amber", "moved": None}

        _backfill_initial_rating(engine)  # idempotent re-run on startup
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM verdicts WHERE initial_rating IS NULL")
            ).scalar() == 1
        engine.dispose()
