from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from payment_promotions_monitor.models import Activity, RunResult, SourceAttempt
from payment_promotions_monitor.report import build_payload, render_markdown


class ReportTests(unittest.TestCase):
    def test_registered_sources_are_separate_from_extended_checks(self) -> None:
        now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempts = [
            SourceAttempt(
                "fullpay",
                "全支付",
                "官方活動入口",
                "activity_listing",
                "https://example.com/events",
                True,
                now.isoformat(),
            ),
            SourceAttempt(
                "fullpay",
                "全支付",
                "活動詳情",
                "activity_detail",
                "https://example.com/event/1",
                True,
                now.isoformat(),
            ),
            SourceAttempt(
                "fullpay",
                "全支付",
                "公開額滿狀態",
                "status_page",
                "https://example.com/event/1/status",
                False,
                now.isoformat(),
                error="timeout",
            ),
            SourceAttempt(
                "fullpay",
                "全支付",
                "指定銀行即時名額",
                "quota_listing_detail",
                "https://example.com/quota",
                True,
                now.isoformat(),
            ),
            SourceAttempt(
                "fullpay",
                "全支付",
                "EventId 1",
                "activity_probe",
                "https://example.com/event/1",
                True,
                now.isoformat(),
            ),
            SourceAttempt(
                "fullpay",
                "全支付",
                "EventId 2",
                "activity_probe",
                "https://example.com/event/2",
                True,
                now.isoformat(),
            ),
        ]
        run = RunResult(
            "r",
            "full",
            now.isoformat(),
            now.isoformat(),
            [],
            attempts,
            discovery_scan_summary={"fullpay": {"complete": True}},
        )
        payload = build_payload(
            run,
            [],
            now,
            {
                "timezone": "Asia/Taipei",
                "providers": [
                    {
                        "id": "fullpay",
                        "name": "全支付",
                        "public_status_scope": "partial",
                        "sources": [
                            {
                                "name": "官方活動入口",
                                "role": "activity_listing",
                                "url": "https://example.com/events",
                            },
                            {
                                "name": "官方活動編號探索",
                                "role": "activity_listing",
                                "adapter": "fullpay_id_scan",
                                "coverage_scope": "partial",
                            },
                        ],
                    }
                ],
            },
        )

        coverage = payload["run"]["coverage"]
        self.assertEqual(coverage["expected"], 6)  # legacy request-level counter
        self.assertEqual(
            coverage["registered_sources"],
            {
                "expected": 2,
                "succeeded": 2,
                "failed": 0,
                "rate": 1.0,
                "status": "complete",
            },
        )
        extended = coverage["extended_checks"]
        self.assertEqual((extended["succeeded"], extended["expected"]), (4, 5))
        self.assertEqual(extended["breakdown"]["detail"]["expected"], 1)
        self.assertEqual(extended["breakdown"]["status"]["failed"], 1)
        self.assertEqual(extended["breakdown"]["status"]["expected"], 2)
        self.assertEqual(extended["breakdown"]["numeric_probe"]["expected"], 2)
        self.assertEqual(
            payload["coverage_by_provider"]["fullpay"]["public_status_scope"],
            "partial",
        )
        markdown = render_markdown(payload)
        self.assertIn("官方入口成功：2/2", markdown)
        self.assertIn("延伸檢查成功：4/5", markdown)
        self.assertIn("活動編號探索 2/2", markdown)

    def test_all_failed_dns_run_is_unavailable_not_partial(self) -> None:
        now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempts = [
            SourceAttempt(
                provider_id="p",
                provider_name="業者",
                source_name=f"來源 {index}",
                role="activity_listing",
                url=f"https://example.com/{index}",
                ok=False,
                fetched_at=now.isoformat(),
                error="RuntimeError: <urlopen error [Errno 8] nodename nor servname provided>",
            )
            for index in range(2)
        ]
        run = RunResult("r", "full", now.isoformat(), now.isoformat(), [], attempts)
        payload = build_payload(
            run,
            [],
            now,
            {
                "timezone": "Asia/Taipei",
                "providers": [{"id": "p", "name": "業者", "sources": [{"role": "activity_listing"}]}],
            },
        )
        coverage = payload["run"]["coverage"]
        self.assertEqual(coverage["transport_status"], "unavailable")
        self.assertEqual(coverage["status"], "unavailable")
        self.assertTrue(coverage["systemic_dns_failure"])
        self.assertEqual(payload["coverage_by_provider"]["p"]["discovery_status"], "limited")
        self.assertIn("source_fetch_failed", payload["coverage_by_provider"]["p"]["discovery_gaps"])

    def test_mixed_transport_result_remains_partial(self) -> None:
        now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempts = [
            SourceAttempt("p", "業者", "成功", "activity_listing", "https://example.com/ok", True, now.isoformat()),
            SourceAttempt(
                "p",
                "業者",
                "失敗",
                "announcement_listing",
                "https://example.com/fail",
                False,
                now.isoformat(),
                error="timeout",
            ),
        ]
        coverage = RunResult("r", "full", now.isoformat(), now.isoformat(), [], attempts).coverage
        self.assertEqual(coverage["transport_status"], "partial")
        self.assertFalse(coverage["systemic_dns_failure"])

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

    def test_partial_official_hub_does_not_claim_complete_discovery(self) -> None:
        now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempt = SourceAttempt(
            provider_id="fullpay",
            provider_name="全支付",
            source_name="官方主題總覽",
            role="activity_listing",
            url="https://example.com/hub.json",
            ok=True,
            fetched_at=now.isoformat(),
            discovered_count=20,
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
                        "id": "fullpay",
                        "name": "全支付",
                        "sources": [
                            {
                                "role": "activity_listing",
                                "url": "https://example.com/hub.json",
                                "coverage_scope": "partial",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(payload["coverage_by_provider"]["fullpay"]["discovery_status"], "limited")
        self.assertEqual(payload["coverage_gaps"][0]["issue"], "partial_public_activity_discovery")

    def test_global_crawl_limit_is_reported_per_affected_provider(self) -> None:
        now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        attempt = SourceAttempt(
            provider_id="p",
            provider_name="業者",
            source_name="活動列表",
            role="activity_listing",
            url="https://example.com/events",
            ok=True,
            fetched_at=now.isoformat(),
        )
        run = RunResult(
            "r",
            "full",
            now.isoformat(),
            now.isoformat(),
            [],
            [attempt],
            [
                {
                    "provider_id": "p",
                    "provider_name": "業者",
                    "source_name": "全域擷取上限",
                    "url": "https://example.com/pending",
                    "issue": "crawl_reached_max_total_pages",
                    "discovered_count": 3,
                }
            ],
        )
        payload = build_payload(
            run,
            [],
            now,
            {
                "timezone": "Asia/Taipei",
                "providers": [
                    {"id": "p", "name": "業者", "sources": [{"role": "activity_listing"}]}
                ],
            },
        )
        self.assertEqual(payload["run"]["coverage"]["discovery_issues"], 1)
        self.assertEqual(payload["coverage_gaps"][0]["discovered_count"], 3)
        self.assertEqual(payload["coverage_by_provider"]["p"]["discovery_status"], "limited")


if __name__ == "__main__":
    unittest.main()
