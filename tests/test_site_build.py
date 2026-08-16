from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.build_site import (
    CALENDAR_DESCRIPTION_LIMIT,
    PUBLIC_STATUS_SCOPES,
    _flatten_report,
    _fold_ical_line,
    _ical_escape,
    _public_status_scope,
    build,
    build_subscription_calendar,
)


class SiteBuildTests(unittest.TestCase):
    def test_flatten_keeps_distinct_external_ids_that_share_an_official_landing_page(self) -> None:
        shared_url = "https://mkt.jkopay.com/zh-TW/campaign/newevent"
        base = {
            "provider_id": "jkopay",
            "provider_name": "街口支付",
            "title": "活動一",
            "url": shared_url,
            "external_id": "campaign-one",
            "lifecycle": "active",
            "quota_status": "not_marked_full",
            "conditions_summary": "",
        }
        report = {
            "sections": {
                "active_public": [
                    base,
                    dict(base, title="活動二", external_id="campaign-two"),
                ]
            }
        }
        activities = _flatten_report(report, date(2026, 7, 24))
        self.assertEqual(len(activities), 2)

    def test_build_excludes_ended_and_applies_valid_official_highlight(self) -> None:
        activity_url = "https://www.taiwanpay.com.tw/fisc-tpay/news/event/example"
        base_item = {
            "provider_id": "taiwanpay",
            "provider_name": "台灣 Pay",
            "title": "指定通路享 20% 回饋",
            "url": activity_url,
            "source_url": activity_url,
            "external_id": "example",
            "start_date": "2026-07-01",
            "end_date": "2026-08-31",
            "lifecycle": "active",
            "quota_status": "not_marked_full",
            "quota_evidence_complete": True,
            "review_required": False,
            "date_confidence": "high",
            "conditions_summary": "指定通路付款享 20% 現金回饋。",
            "fetched_at": "2026-07-21T08:00:00+08:00",
            "content_hash": "example",
            "evidence": [],
            "components": [],
        }
        ended_item = dict(base_item, title="已結束活動", url=f"{activity_url}-ended", lifecycle="ended")
        report = {
            "generated_at": "2026-07-21T08:00:00+08:00",
            "timezone": "Asia/Taipei",
            "run": {
                "coverage": {
                    "expected": 2,
                    "succeeded": 2,
                    "registered_sources": {"expected": 1, "succeeded": 1, "failed": 0},
                    "extended_checks": {"expected": 1, "succeeded": 1, "failed": 0},
                }
            },
            "summary": {"included_non_expired": 1},
            "source_failures": [],
            "coverage_gaps": [
                {
                    "provider_id": "taiwanpay",
                    "provider_name": "台灣 Pay",
                    "source_name": "活動列表",
                    "url": activity_url,
                    "issue": "listing_zero_discovery",
                    "discovered_count": 0,
                }
            ],
            "coverage_by_provider": {
                "taiwanpay": {
                    "discovery_status": "limited",
                    "public_status_scope": "partial",
                    "public_status_coverage": "文字即使改寫，也不應影響結構化狀態。",
                    "registered_sources": {"expected": 1, "succeeded": 1, "failed": 0},
                    "extended_checks": {"expected": 1, "succeeded": 1, "failed": 0},
                }
            },
            "sections": {
                "active_public": [base_item, ended_item],
                "sold_out": [],
                "upcoming": [],
                "app_only_unknown": [],
                "review_required": [],
            },
        }
        supplement = {
            "schema_version": 1,
            "generated_at": "2026-07-21T08:05:00+08:00",
            "headline": "今天的 AI 重點",
            "highlights": [
                {
                    "kind": "high_return",
                    "provider_id": "taiwanpay",
                    "provider_name": "台灣 Pay",
                    "title": "20% 回饋重點",
                    "summary": "回饋高，但仍須留意個人上限。",
                    "url": activity_url,
                },
                {
                    "kind": "high_return",
                    "provider_id": "taiwanpay",
                    "provider_name": "台灣 Pay",
                    "title": "不可信連結",
                    "summary": "這筆必須被排除。",
                    "url": "https://example.com/not-official",
                },
            ],
            "supplemental_activities": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            supplement_path = root / "supplement.json"
            output_dir = root / "site"
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            supplement_path.write_text(json.dumps(supplement, ensure_ascii=False), encoding="utf-8")

            data_path = build(report_path, output_dir, supplement_path)
            payload = json.loads(data_path.read_text(encoding="utf-8"))

            self.assertTrue((output_dir / ".nojekyll").exists())
            self.assertTrue((output_dir / "index.html").exists())
            built_html = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("__ASSET_VERSION__", built_html)
            self.assertIn("./assets/app.js?v=", built_html)
            repository_root = Path(__file__).resolve().parents[1]
            self.assertEqual(
                built_html,
                (repository_root / "docs" / "index.html").read_text(encoding="utf-8"),
            )
            built_assets = {
                path.name: path.read_bytes()
                for path in (output_dir / "assets").iterdir()
                if path.is_file()
            }
            committed_assets = {
                path.name: path.read_bytes()
                for path in (repository_root / "docs" / "assets").iterdir()
                if path.is_file()
            }
            self.assertEqual(built_assets, committed_assets)
            self.assertEqual(payload["headline"], "今天的 AI 重點")
            self.assertEqual(payload["analysis_method"], "local_rules_and_codex_review")
            self.assertEqual(len(payload["activities"]), 1)
            self.assertEqual(len(payload["highlights"]), 1)
            self.assertEqual(payload["activities"][0]["editorial_summary"], "回饋高，但仍須留意個人上限。")
            self.assertTrue(payload["activities"][0]["insights"]["is_high_return"])
            self.assertEqual(payload["activities"][0]["conditions_display"], ["指定通路付款享 20% 現金回饋。"])
            taiwanpay_coverage = next(
                item for item in payload["provider_coverage"] if item["provider_id"] == "taiwanpay"
            )
            self.assertEqual(taiwanpay_coverage["activity_count"], 1)
            self.assertEqual(taiwanpay_coverage["public_status_coverage"], "partial")
            self.assertEqual(taiwanpay_coverage["official_sources"]["expected"], 1)
            self.assertEqual(taiwanpay_coverage["extended_checks"]["expected"], 1)
            self.assertTrue(taiwanpay_coverage["coverage_note"])
            self.assertEqual(payload["source_health"]["review_label"], "1 個官網列表待補強")
            self.assertNotIn("AI", payload["source_health"]["review_label"])
            self.assertNotIn("google_calendar_url", payload["activities"][0])
            calendar_text = (output_dir / "calendar.ics").read_text(encoding="utf-8")
            self.assertIn("SUMMARY:1 檔優惠今日截止", calendar_text)
            self.assertIn("DTSTART;VALUE=DATE:20260831", calendar_text)
            self.assertIn("DTEND;VALUE=DATE:20260901", calendar_text)
            self.assertIn("webcal://garychen-soc.github.io", built_html)

    def test_public_status_scope_is_structured_not_inferred_from_copy(self) -> None:
        provider = {
            "public_status_scope": "public",
            "public_status_coverage": "部分文字僅是說明，不應改變 enum",
        }
        self.assertEqual(_public_status_scope(provider, {}), "public")
        self.assertEqual(
            _public_status_scope(provider, {"public_status_scope": "partial"}),
            "partial",
        )
        self.assertEqual(
            _public_status_scope({"public_status_scope": "unexpected"}, {}),
            "unknown",
        )

    def test_source_registry_declares_public_status_scope_for_every_provider(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        config = json.loads((repository_root / "config" / "sources.json").read_text(encoding="utf-8"))
        self.assertTrue(config["providers"])
        for provider in config["providers"]:
            with self.subTest(provider=provider["id"]):
                self.assertIn(provider.get("public_status_scope"), PUBLIC_STATUS_SCOPES - {"unknown"})

    def test_subscription_calendar_groups_dates_and_excludes_past_ended_and_sold_out(self) -> None:
        base = {
            "provider_id": "taiwanpay",
            "provider_name": "台灣 Pay",
            "url": "https://www.taiwanpay.com.tw/event/example",
            "start_date": "2026-08-16",
            "end_date": "2026-08-31",
            "lifecycle": "active",
            "quota_status": "not_marked_full",
        }
        activities = [
            dict(base, external_id="one", title="活動一"),
            dict(base, external_id="two", title="活動二"),
            dict(base, external_id="past", title="過去開跑", start_date="2026-08-15"),
            dict(base, external_id="ended", title="已結束", lifecycle="ended"),
            dict(base, external_id="full", title="已額滿", quota_status="sold_out"),
            dict(base, external_id="incomplete", title="日期不完整", end_date=None),
        ]
        generated_at = datetime(2026, 8, 16, 8, tzinfo=ZoneInfo("Asia/Taipei"))

        calendar = build_subscription_calendar(activities, generated_at)

        self.assertEqual(calendar.count("SUMMARY:2 檔優惠今日開跑"), 1)
        self.assertEqual(calendar.count("SUMMARY:3 檔優惠今日截止"), 1)
        self.assertNotIn("UID:start-2026-08-15", calendar)
        self.assertNotIn("已結束", calendar)
        self.assertNotIn("已額滿", calendar)
        self.assertIn("DTSTART;VALUE=DATE:20260816", calendar)
        self.assertIn("DTEND;VALUE=DATE:20260817", calendar)
        self.assertIn("TRANSP:TRANSPARENT", calendar)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT12H", calendar)
        self.assertIn("X-PUBLISHED-TTL:PT12H", calendar)

    def test_subscription_calendar_caps_large_same_day_description(self) -> None:
        activities = [
            {
                "provider_id": "provider",
                "provider_name": "支付業者",
                "external_id": str(index),
                "title": f"不應逐筆列出的活動 {index}",
                "url": f"https://example.com/{index}",
                "start_date": "2026-08-01",
                "end_date": "2026-12-31",
                "lifecycle": "active",
                "quota_status": "not_marked_full",
            }
            for index in range(CALENDAR_DESCRIPTION_LIMIT + 1)
        ]
        generated_at = datetime(2026, 8, 16, 8, tzinfo=ZoneInfo("Asia/Taipei"))

        calendar = build_subscription_calendar(activities, generated_at)
        unfolded = calendar.replace("\r\n ", "")

        self.assertIn(f"SUMMARY:{CALENDAR_DESCRIPTION_LIMIT + 1} 檔優惠今日截止", unfolded)
        self.assertIn(f"共 {CALENDAR_DESCRIPTION_LIMIT + 1} 檔優惠今日截止", unfolded)
        self.assertNotIn("不應逐筆列出的活動", unfolded)

    def test_ical_text_escaping_and_utf8_octet_folding(self) -> None:
        escaped = _ical_escape("反斜線\\、逗號,、分號;\n第二行")
        self.assertEqual(escaped, "反斜線\\\\、逗號\\,、分號\\;\\n第二行")

        folded = _fold_ical_line("DESCRIPTION:" + "中文活動，" * 20)
        physical_lines = folded.split("\r\n")
        self.assertGreater(len(physical_lines), 1)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in physical_lines))
        self.assertTrue(all(line.startswith(" ") for line in physical_lines[1:]))

    def test_subscription_calendar_is_deterministic_for_same_generated_at(self) -> None:
        activity = {
            "provider_id": "taiwanpay",
            "provider_name": "台灣 Pay",
            "external_id": "stable",
            "title": "穩定活動",
            "url": "https://www.taiwanpay.com.tw/event/stable",
            "start_date": "2026-08-20",
            "end_date": "2026-08-31",
            "lifecycle": "upcoming",
            "quota_status": "not_marked_full",
        }
        generated_at = datetime(2026, 8, 16, 8, tzinfo=ZoneInfo("Asia/Taipei"))
        first = build_subscription_calendar([activity], generated_at)
        second = build_subscription_calendar([dict(activity)], generated_at)
        self.assertEqual(first, second)
        self.assertIn("DTSTAMP:20260816T000000Z", first)


if __name__ == "__main__":
    unittest.main()
