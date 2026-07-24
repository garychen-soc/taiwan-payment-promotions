from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from payment_promotions_monitor.models import Activity, Evidence, RunResult, SourceAttempt
from payment_promotions_monitor.storage import Store


class StorageHistoryTests(unittest.TestCase):
    def test_jkopay_legacy_route_ids_are_migrated_and_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            url = "https://mkt.jkopay.com/zh-TW/campaign/jkodrink"
            legacy = Activity(
                provider_id="jkopay",
                provider_name="街口支付",
                title="週一飲料日",
                url=url,
                source_url=url,
                external_id="campaign-jkodrink",
            )
            current = Activity(
                provider_id="jkopay",
                provider_name="街口支付",
                title="週一飲料日",
                url=url,
                source_url=url,
                external_id="jkodrink",
            )
            with Store(path) as store:
                store.upsert_activity(legacy)
                store.upsert_activity(current)
                store.connection.commit()

            with Store(path) as store:
                rows = store.connection.execute(
                    "SELECT external_id FROM activities WHERE provider_id = 'jkopay'"
                ).fetchall()

            self.assertEqual([row["external_id"] for row in rows], ["jkodrink"])

    def test_distinct_external_ids_can_share_one_official_landing_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            shared_url = "https://mkt.jkopay.com/zh-TW/campaign/newevent"
            activities = [
                Activity(
                    provider_id="jkopay",
                    provider_name="街口支付",
                    title=f"活動 {external_id}",
                    url=shared_url,
                    source_url=shared_url,
                    external_id=external_id,
                    fetched_at="2026-07-24T00:00:00+08:00",
                )
                for external_id in ("one", "two")
            ]
            with Store(path) as store:
                store.save_run(
                    RunResult(
                        "shared-url",
                        "full",
                        activities[0].fetched_at,
                        activities[0].fetched_at,
                        activities,
                        [],
                    )
                )
                stored = store.connection.execute(
                    "SELECT external_id FROM activities ORDER BY external_id"
                ).fetchall()
            self.assertEqual([row["external_id"] for row in stored], ["one", "two"])

    def test_legacy_schema_migration_is_committed_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    run_status TEXT NOT NULL,
                    expected_sources INTEGER NOT NULL,
                    succeeded_sources INTEGER NOT NULL,
                    failed_sources INTEGER NOT NULL,
                    activity_count INTEGER NOT NULL
                );
                INSERT INTO runs VALUES (
                    'legacy-run', 'full', '2026-07-21T00:00:00+08:00',
                    '2026-07-21T00:01:00+08:00', 'complete', 1, 1, 0, 0
                );
                CREATE TABLE source_attempts (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    provider_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    url TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    status_code INTEGER,
                    final_url TEXT,
                    error TEXT,
                    fetched_at TEXT NOT NULL,
                    discovered_count INTEGER NOT NULL
                );
                INSERT INTO source_attempts VALUES (
                    'legacy-run', 'fullpay', '全支付', '舊版活動來源',
                    'activity_listing', 'https://example.test/events', 1, 200,
                    'https://example.test/events', NULL,
                    '2026-07-21T00:00:30+08:00', 3
                );
                CREATE TABLE discovery_state (
                    provider_id TEXT PRIMARY KEY,
                    highest_valid_event_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO discovery_state VALUES (
                    'fullpay', 109, '2026-07-21T00:01:00+08:00'
                );
                """
            )
            connection.close()

            with Store(path) as store:
                self.assertEqual(
                    store.load_discovery_state(),
                    {
                        "fullpay": {
                            "highest_valid_event_id": 109,
                            "scan_frontier_event_id": 109,
                        }
                    },
                )
                migrated_attempt = store.connection.execute(
                    """
                    SELECT source_name, discovered_count, coverage_issue
                    FROM source_attempts
                    WHERE run_id = 'legacy-run'
                    """
                ).fetchone()
                self.assertEqual(migrated_attempt["source_name"], "舊版活動來源")
                self.assertEqual(migrated_attempt["discovered_count"], 3)
                self.assertIsNone(migrated_attempt["coverage_issue"])

            # Reopening without save_run proves the migration itself committed;
            # it must not rely on a later crawl transaction to persist backfill.
            with Store(path) as reopened:
                self.assertEqual(
                    reopened.load_discovery_state()["fullpay"],
                    {
                        "highest_valid_event_id": 109,
                        "scan_frontier_event_id": 109,
                    },
                )
                self.assertEqual(
                    reopened.connection.execute(
                        "SELECT COUNT(*) FROM source_attempts WHERE run_id = 'legacy-run'"
                    ).fetchone()[0],
                    1,
                )

    def test_discovery_watermark_and_coverage_issue_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            attempt = SourceAttempt(
                provider_id="fullpay",
                provider_name="全支付",
                source_name="EventId 109",
                role="activity_probe",
                url="https://service.pxpayplus.com/detail/109",
                ok=False,
                fetched_at="2026-07-22T00:00:30+08:00",
                error="timeout",
                coverage_issue="id_probe_incomplete",
            )
            run = RunResult(
                "r1",
                "full",
                "2026-07-22T00:00:00+08:00",
                "2026-07-22T00:01:00+08:00",
                [],
                [attempt],
                discovery_state_updates={
                    "fullpay": {
                        "highest_valid_event_id": 108,
                        "scan_frontier_event_id": 160,
                    }
                },
            )
            with Store(path) as store:
                store.save_run(run)
                self.assertEqual(
                    store.load_discovery_state(),
                    {
                        "fullpay": {
                            "highest_valid_event_id": 108,
                            "scan_frontier_event_id": 160,
                        }
                    },
                )
                columns = {
                    row["name"]
                    for row in store.connection.execute("PRAGMA table_info(source_attempts)")
                }
                self.assertIn("coverage_issue", columns)
                stored_issue = store.connection.execute(
                    "SELECT coverage_issue FROM source_attempts WHERE run_id = 'r1'"
                ).fetchone()["coverage_issue"]
                self.assertEqual(stored_issue, "id_probe_incomplete")

    def test_source_failure_does_not_downgrade_confirmed_sold_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            previous = Activity(
                provider_id="p",
                provider_name="業者",
                title="活動",
                url="https://example.com/event/1",
                source_url="https://example.com/event/1",
                quota_status="sold_out",
                fetched_at="2026-07-21T00:00:00+08:00",
            )
            current = Activity(
                provider_id="p",
                provider_name="業者",
                title="活動",
                url="https://example.com/event/1",
                source_url="https://example.com/event/1",
                quota_status="unknown_source_failure",
                quota_evidence_complete=False,
                fetched_at="2026-07-22T00:00:00+08:00",
            )
            with Store(path) as store:
                store.save_run(RunResult("r1", "full", previous.fetched_at, previous.fetched_at, [previous], []))
                store.merge_persistent_status([current])
            self.assertEqual(current.quota_status, "sold_out")

    def test_pxpay_shared_page_can_clear_misattributed_fullpay_app_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            url = "https://www.pxmart.com.tw/campaign/pxpay-card/weekend"
            previous = Activity(
                provider_id="pxpay",
                provider_name="PX Pay",
                title="PX Pay 週末活動",
                url=url,
                source_url=url,
                quota_status="unknown_app_only",
                quota_evidence_complete=False,
                evidence=[
                    Evidence(
                        url,
                        "全支付額滿時間請依全支付 APP 公告",
                        "2026-07-21T00:00:00+08:00",
                    )
                ],
                fetched_at="2026-07-21T00:00:00+08:00",
            )
            current = Activity(
                provider_id="pxpay",
                provider_name="PX Pay",
                title="PX Pay 週末活動",
                url=url,
                source_url=url,
                quota_status="not_marked_full",
                quota_evidence_complete=True,
                fetched_at="2026-07-22T00:00:00+08:00",
            )
            with Store(path) as store:
                store.save_run(
                    RunResult(
                        "r1",
                        "full",
                        previous.fetched_at,
                        previous.fetched_at,
                        [previous],
                        [],
                    )
                )
                store.merge_persistent_status([current])
            self.assertEqual(current.quota_status, "not_marked_full")
            self.assertEqual(current.evidence, [])

    def test_sold_out_evidence_persists_and_false_end_date_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            previous = Activity(
                provider_id="p",
                provider_name="業者",
                title="活動",
                url="https://example.com/event/1",
                source_url="https://example.com/event/1",
                start_date="2026-07-16",
                end_date="2026-07-16",
                lifecycle="ended",
                quota_status="sold_out",
                evidence=[
                    Evidence(
                        "https://example.com/news/1",
                        "已額滿",
                        "2026-07-17T00:00:00+08:00",
                        kind="latest_announcement:same_page:1.00",
                    )
                ],
                fetched_at="2026-07-17T00:00:00+08:00",
            )
            current = Activity(
                provider_id="p",
                provider_name="業者",
                title="活動",
                url="https://example.com/event/1",
                source_url="https://example.com/event/1",
                start_date="2026-07-16",
                end_date=None,
                lifecycle="active",
                quota_status="not_marked_full",
                fetched_at="2026-07-21T00:00:00+08:00",
                evidence=[
                    Evidence(
                        "https://example.com/news/1",
                        "已額滿",
                        "2026-07-21T00:00:00+08:00",
                        content_hash="new-hash",
                    )
                ],
            )
            with Store(path) as store:
                store.save_run(RunResult("r1", "full", previous.fetched_at, previous.fetched_at, [previous], []))
                store.merge_persistent_status([current])
                self.assertEqual(current.quota_status, "sold_out")
                self.assertEqual(current.evidence[0].excerpt, "已額滿")
                self.assertEqual(len(current.evidence), 1)
                store.save_run(RunResult("r2", "full", current.fetched_at, current.fetched_at, [current], []))
                loaded = store.load_current()[0]

            self.assertIsNone(loaded.end_date)
            self.assertEqual(loaded.lifecycle, "active")
            self.assertEqual(loaded.quota_status, "sold_out")


if __name__ == "__main__":
    unittest.main()
