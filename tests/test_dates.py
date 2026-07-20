from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from payment_promotions_monitor.dates import lifecycle_for, parse_date_range


NOW = datetime(2026, 7, 21, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class DateParsingTests(unittest.TestCase):
    def assert_range(self, text: str, start: str, end: str, lifecycle: str) -> None:
        result = parse_date_range(text)
        self.assertEqual(result.start.isoformat() if result.start else None, start)
        self.assertEqual(result.end.isoformat() if result.end else None, end)
        self.assertEqual(lifecycle_for(result.start, result.end, NOW), lifecycle)

    def test_western_date_range(self) -> None:
        self.assert_range("活動期間：2026/07/01～2026/08/31", "2026-07-01", "2026-08-31", "active")

    def test_roc_date_range(self) -> None:
        self.assert_range("活動期間：民國115年7月1日至115年8月31日", "2026-07-01", "2026-08-31", "active")

    def test_cross_month_shorthand(self) -> None:
        self.assert_range("活動期間：2026/07/23～08/05", "2026-07-23", "2026-08-05", "upcoming")

    def test_single_date_does_not_infer_end_date(self) -> None:
        self.assert_range("活動期間：2026年7月16日起，回饋每月重新計算", "2026-07-16", None, "active")

    def test_explicit_single_day_is_active_for_whole_day(self) -> None:
        self.assert_range("活動日期：2026年7月21日，僅限當日有效", "2026-07-21", "2026-07-21", "active")

    def test_expired(self) -> None:
        self.assert_range("活動期間：2026/06/01～2026/07/20", "2026-06-01", "2026-07-20", "ended")

    def test_json_date_fields(self) -> None:
        self.assert_range(
            "activity_start_time: 2026/07/01 00:00\nactivity_end_time: 2026/08/31 23:59",
            "2026-07-01",
            "2026-08-31",
            "active",
        )

    def test_api_date_fields(self) -> None:
        self.assert_range(
            "startDate: 2026-07-23\nendDate: 2026-09-30",
            "2026-07-23",
            "2026-09-30",
            "upcoming",
        )


if __name__ == "__main__":
    unittest.main()
