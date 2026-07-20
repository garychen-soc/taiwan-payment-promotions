from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from payment_promotions_monitor.models import Activity, RunResult, SourceAttempt
from payment_promotions_monitor.report import build_payload


class ReportTests(unittest.TestCase):
    def test_expired_is_excluded_but_active_sold_out_remains(self) -> None:
        now = datetime(2026, 7, 21, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        expired = Activity(
            provider_id="p",
            provider_name="業者",
            title="過期",
            url="https://example.com/old",
            source_url="https://example.com/old",
            start_date="2026-06-01",
            end_date="2026-07-20",
            lifecycle="active",
        )
        sold_out = Activity(
            provider_id="p",
            provider_name="業者",
            title="仍在期間但額滿",
            url="https://example.com/current",
            source_url="https://example.com/current",
            start_date="2026-07-01",
            end_date="2026-08-31",
            lifecycle="active",
            quota_status="sold_out",
        )
        run = RunResult("r", "full", now.isoformat(), now.isoformat(), [expired, sold_out], [])
        payload = build_payload(
            run,
            [expired, sold_out],
            now,
            {"timezone": "Asia/Taipei", "providers": [{"id": "p", "name": "業者"}]},
        )
        self.assertEqual(payload["summary"]["expired_excluded"], 1)
        self.assertEqual(payload["summary"]["included_non_expired"], 1)
        self.assertEqual(payload["sections"]["sold_out"][0]["title"], "仍在期間但額滿")

    def test_listing_truncation_is_reported_as_coverage_gap(self) -> None:
        now = datetime(2026, 7, 21, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempt = SourceAttempt(
            provider_id="p",
            provider_name="業者",
            source_name="活動列表",
            role="activity_listing",
            url="https://example.com/events",
            ok=True,
            fetched_at=now.isoformat(),
            discovered_count=10,
            coverage_issue="listing_reached_max_links",
        )
        run = RunResult("r", "full", now.isoformat(), now.isoformat(), [], [attempt])
        payload = build_payload(
            run,
            [],
            now,
            {
                "timezone": "Asia/Taipei",
                "providers": [
                    {
                        "id": "p",
                        "name": "業者",
                        "sources": [{"role": "activity_listing"}],
                    }
                ],
            },
        )
        self.assertEqual(payload["run"]["coverage"]["status"], "partial")
        self.assertEqual(payload["run"]["coverage"]["discovery_issues"], 1)
        self.assertEqual(payload["coverage_gaps"][0]["issue"], "listing_reached_max_links")


if __name__ == "__main__":
    unittest.main()
