from __future__ import annotations

import unittest
from datetime import date

from payment_promotions_monitor.insights import analyze_activity


class RewardInsightTests(unittest.TestCase):
    def test_extracts_high_percentage_without_treating_caps_as_fixed_reward(self) -> None:
        item = {
            "title": "生活圈 Pay你遊壢享20%回饋",
            "conditions_summary": (
                "活動期間2026/7/1至2026/9/30，單筆消費不限金額享20%現金回饋，"
                "每一帳戶最高回饋新臺幣1,500元為限，活動總回饋上限50萬元。"
            ),
        }
        result = analyze_activity(item, date(2026, 7, 21))
        self.assertEqual(result["max_reward_percent"], 20)
        self.assertIsNone(result["fixed_reward_amount"])
        self.assertTrue(result["is_high_return"])
        self.assertIn("高回饋", result["insight_tags"])

    def test_fixed_voucher_uses_reward_not_spending_threshold(self) -> None:
        result = analyze_activity(
            {"conditions_summary": "使用行動支付累積消費滿150元送50元優惠券。"},
            date(2026, 7, 21),
        )
        self.assertEqual(result["fixed_reward_amount"], 50)
        self.assertFalse(result["is_high_return"])
        self.assertNotIn("高額回饋", result["insight_tags"])

    def test_fixed_reward_at_threshold_is_high_value(self) -> None:
        result = analyze_activity(
            {"title": "新戶完成首筆付款贈100元現金回饋"},
            date(2026, 7, 21),
        )
        self.assertEqual(result["fixed_reward_amount"], 100)
        self.assertTrue(result["is_high_return"])
        self.assertIn("高額回饋", result["insight_tags"])

    def test_reward_caps_and_point_caps_are_not_fixed_rewards(self) -> None:
        result = analyze_activity(
            {
                "conditions_summary": (
                    "消費總額享3.5%回饋，每人每月回饋上限150點，"
                    "單筆交易回饋金上限300元，活動總金額47萬元為上限。"
                )
            },
            date(2026, 7, 21),
        )
        self.assertEqual(result["max_reward_percent"], 3.5)
        self.assertIsNone(result["fixed_reward_amount"])
        self.assertFalse(result["is_high_return"])

    def test_non_reward_percentages_are_ignored(self) -> None:
        result = analyze_activity(
            {"conditions_summary": "海外交易另收1.5%手續費，分期年利率6%。"},
            date(2026, 7, 21),
        )
        self.assertIsNone(result["max_reward_percent"])

    def test_uses_largest_explicit_reward(self) -> None:
        result = analyze_activity(
            {"conditions_summary": "基本享3.5%回饋，首次付款再加碼最高享10%回饋。"},
            date(2026, 7, 21),
        )
        self.assertEqual(result["max_reward_percent"], 10)


class TimingInsightTests(unittest.TestCase):
    def test_upcoming_within_fourteen_days(self) -> None:
        result = analyze_activity(
            {"start_date": "2026-07-23", "end_date": "2026-09-30"},
            date(2026, 7, 21),
        )
        self.assertEqual(result["starts_in_days"], 2)
        self.assertTrue(result["is_upcoming"])
        self.assertIn("即將開始", result["insight_tags"])
        self.assertIn("2 天後開始", result["human_summary"])

    def test_distant_future_has_day_count_but_is_not_upcoming(self) -> None:
        result = analyze_activity(
            {"start_date": "2026-08-21"},
            date(2026, 7, 21),
        )
        self.assertEqual(result["starts_in_days"], 31)
        self.assertFalse(result["is_upcoming"])

    def test_expiring_today_and_seven_day_boundary(self) -> None:
        today_result = analyze_activity({"end_date": "2026-07-21"}, date(2026, 7, 21))
        boundary_result = analyze_activity({"end_date": "2026-07-28"}, date(2026, 7, 21))
        self.assertTrue(today_result["is_expiring_soon"])
        self.assertIn("今天截止", today_result["human_summary"])
        self.assertTrue(boundary_result["is_expiring_soon"])
        self.assertEqual(boundary_result["ends_in_days"], 7)

    def test_past_or_invalid_end_date_is_not_expiring_soon(self) -> None:
        past = analyze_activity({"end_date": "2026-07-20"}, date(2026, 7, 21))
        invalid = analyze_activity({"end_date": "民國115年7月"}, date(2026, 7, 21))
        self.assertEqual(past["ends_in_days"], -1)
        self.assertFalse(past["is_expiring_soon"])
        self.assertIsNone(invalid["ends_in_days"])
        self.assertFalse(invalid["is_expiring_soon"])

    def test_quota_status_tags_and_summary_are_human_readable(self) -> None:
        result = analyze_activity(
            {"quota_status": "partial_sold_out", "conditions_summary": "享15%回饋"},
            date(2026, 7, 21),
        )
        self.assertIn("高回饋", result["insight_tags"])
        self.assertIn("部分額滿", result["insight_tags"])
        self.assertIn("部分期別或子活動已額滿", result["human_summary"])
        self.assertTrue(result["human_summary"].endswith("。"))


if __name__ == "__main__":
    unittest.main()
