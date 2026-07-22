from __future__ import annotations

import unittest
from datetime import date

from payment_promotions_monitor.status import analyze_quota, match_announcement


class QuotaStatusTests(unittest.TestCase):
    def test_limited_is_not_sold_out(self) -> None:
        result = analyze_quota("活動總回饋上限40萬點，送完為止；若額滿將另行公告。")
        self.assertEqual(result.status, "not_marked_full")

    def test_app_announcement_rule_is_not_sold_out(self) -> None:
        result = analyze_quota("名額額滿將於全支付APP公告，惟額滿公告之系統時間並不等同活動實際額滿之時間。")
        self.assertEqual(result.status, "unknown_app_only")

    def test_explicit_sold_out(self) -> None:
        result = analyze_quota("【額滿公告】本活動回饋已於2026/7/18全數額滿。", event_start=date(2026, 7, 1))
        self.assertEqual(result.status, "sold_out")
        self.assertEqual(result.sold_out_at, "2026-07-18")

    def test_conditional_phrases_do_not_trigger(self) -> None:
        for text in (
            "若額滿則停止回饋",
            "回饋額滿為止",
            "如達活動上限將提前結束",
            "請至 APP 留意當月是否已額滿",
            "舉例如下：小通消費1300元，10%回饋已達每人每月回饋上限100點。",
            "除已達各項活動回饋總金額上限外，可一併獲得所有優惠。",
            "TWQR花火狂歡Pay享10%回饋【每月額滿詳活動提醒】",
            "當活動頁面出現「已額滿」即代表已發送完畢。",
        ):
            with self.subTest(text=text):
                self.assertEqual(analyze_quota(text).status, "not_marked_full")

    def test_official_title_marker_is_sold_out(self) -> None:
        self.assertEqual(analyze_quota("title: （活動額滿）市場回饋活動").status, "sold_out")

    def test_app_sold_out_notice_is_app_only(self) -> None:
        result = analyze_quota("暑假遊韓20%回饋【電支機構APP額滿公告詳如活動提醒】")
        self.assertEqual(result.status, "unknown_app_only")

    def test_partial_month_and_component(self) -> None:
        result = analyze_quota("活動二7月份回饋已額滿，8、9月仍可參加。", event_start=date(2026, 7, 1))
        self.assertEqual(result.status, "partial_sold_out")
        self.assertEqual(result.components[0]["period"], "07月")
        self.assertEqual(result.components[0]["component"], "活動二")

    def test_month_total_cap_is_partial_not_whole_campaign(self) -> None:
        result = analyze_quota("9 月因活動總回饋上限已額滿，故 9 月消費不符合回饋資格。")
        self.assertEqual(result.status, "partial_sold_out")
        self.assertEqual(result.components[0]["period"], "09月")

    def test_weekly_quota_is_partial_not_whole_campaign(self) -> None:
        result = analyze_quota("(首週回饋已額滿)，次週活動仍將繼續。")
        self.assertEqual(result.status, "partial_sold_out")
        self.assertEqual(result.components[0]["period"], "首週")

    def test_app_only_is_unknown(self) -> None:
        result = analyze_quota("回饋額滿資訊僅公告於悠遊付App，本網站不另行公告。")
        self.assertEqual(result.status, "unknown_app_only")
        self.assertFalse(result.evidence_complete)


class AnnouncementMatchingTests(unittest.TestCase):
    def test_generic_page_titles_do_not_match(self) -> None:
        matched, method, _ = match_announcement(
            activity_url="https://example.com/event/1",
            activity_title="活動訊息",
            activity_text="2026/7/1-8/31",
            announcement_url="https://example.com/event/2",
            announcement_title="活動訊息",
            announcement_text="另一活動已額滿",
        )
        self.assertFalse(matched)
        self.assertEqual(method, "insufficient")

    def test_event_id_match(self) -> None:
        matched, method, score = match_announcement(
            activity_url="https://example.com/event?EventId=89",
            activity_title="延三夜市回饋",
            activity_text="",
            announcement_url="https://example.com/news/1",
            announcement_title="全支付夜市活動額滿通知",
            announcement_text="EventId=89 已額滿",
        )
        self.assertTrue(matched)
        self.assertEqual(method, "event_id_or_url")
        self.assertEqual(score, 1.0)

    def test_event_id_prefix_does_not_match_longer_id(self) -> None:
        matched, _, _ = match_announcement(
            activity_url="https://example.com/event?EventId=8",
            activity_title="活動八",
            activity_text="",
            announcement_url="https://example.com/news/1",
            announcement_title="另一個活動公告",
            announcement_text="official_link: https://example.com/event?EventId=82 指定活動已額滿",
        )
        self.assertFalse(matched)

    def test_exact_link_without_event_id_matches(self) -> None:
        matched, method, _ = match_announcement(
            activity_url="https://example.com/events/summer?b=2&a=1",
            activity_title="夏日活動",
            activity_text="",
            announcement_url="https://example.com/news/1",
            announcement_title="公告",
            announcement_text="official_link: https://example.com/events/summer?a=1&b=2 回饋已額滿",
        )
        self.assertTrue(matched)
        self.assertEqual(method, "event_id_or_url")

    def test_normalized_title_with_matching_period(self) -> None:
        matched, method, _ = match_announcement(
            activity_url="https://example.com/event/2",
            activity_title="悠遊付「迺夜市最高35%回饋」",
            activity_text="活動期間：2026/7/1～2026/8/31",
            announcement_url="https://example.com/news/2",
            announcement_title="【額滿公告】悠遊付 迺夜市 7月週六加碼回饋已額滿",
            announcement_text="7月份週六加碼已額滿",
        )
        self.assertTrue(matched)
        self.assertEqual(method, "normalized_title_period")

    def test_different_period_does_not_match(self) -> None:
        matched, method, _ = match_announcement(
            activity_url="https://example.com/event/aug",
            activity_title="台灣Pay×OK超商",
            activity_text="活動期間：2026/8/1～2026/8/31",
            announcement_url="https://example.com/news/jul",
            announcement_title="台灣Pay×OK超商 7月回饋已額滿",
            announcement_text="2026年7月份已額滿",
        )
        self.assertFalse(matched)
        self.assertEqual(method, "period_conflict")


if __name__ == "__main__":
    unittest.main()
