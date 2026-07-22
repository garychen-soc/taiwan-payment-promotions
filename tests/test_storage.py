from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from payment_promotions_monitor.models import Activity, Evidence, RunResult
from payment_promotions_monitor.storage import Store


class StorageHistoryTests(unittest.TestCase):
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
