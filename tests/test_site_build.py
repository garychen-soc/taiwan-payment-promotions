from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scripts.build_site import PUBLIC_STATUS_SCOPES, _google_calendar_url, _public_status_scope, build


class SiteBuildTests(unittest.TestCase):
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
            calendar_url = payload["activities"][0]["google_calendar_url"]
            calendar_query = parse_qs(urlsplit(calendar_url).query)
            self.assertEqual(calendar_query["dates"], ["20260701/20260901"])
            self.assertIn(activity_url, calendar_query["details"][0])
            self.assertIn("calendar-link", built_html)

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

    def test_google_calendar_url_encodes_all_day_event_and_official_details(self) -> None:
        activity = {
            "provider_name": "台灣 Pay",
            "title": "週末回饋 & 加碼",
            "start_date": "2026-12-31",
            "end_date": "2027-01-01",
            "editorial_summary": "最高 20% 回饋，留意每人上限。",
            "url": "https://www.taiwanpay.com.tw/event/example?a=1&b=2",
        }

        calendar_url = _google_calendar_url(activity)
        parsed = urlsplit(calendar_url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "calendar.google.com")
        self.assertEqual(parsed.path, "/calendar/render")
        self.assertEqual(query["action"], ["TEMPLATE"])
        self.assertEqual(query["text"], ["週末回饋 & 加碼"])
        self.assertEqual(query["dates"], ["20261231/20270102"])
        self.assertEqual(query["ctz"], ["Asia/Taipei"])
        self.assertIn("支付業者：台灣 Pay", query["details"][0])
        self.assertIn(activity["url"], query["details"][0])

    def test_google_calendar_url_uses_next_day_for_single_day_event(self) -> None:
        calendar_url = _google_calendar_url(
            {
                "title": "單日活動",
                "start_date": "2026-07-31",
                "end_date": "2026-07-31",
            }
        )
        self.assertEqual(parse_qs(urlsplit(calendar_url).query)["dates"], ["20260731/20260801"])

    def test_google_calendar_url_rejects_incomplete_or_reversed_dates(self) -> None:
        invalid_values = (
            {"title": "缺少結束日", "start_date": "2026-07-01", "end_date": None},
            {"title": "缺少開始日", "start_date": None, "end_date": "2026-07-02"},
            {"title": "日期顛倒", "start_date": "2026-07-02", "end_date": "2026-07-01"},
            {"title": "日期無效", "start_date": "2026-02-30", "end_date": "2026-03-01"},
        )
        for activity in invalid_values:
            with self.subTest(activity=activity["title"]):
                self.assertEqual(_google_calendar_url(activity), "")


if __name__ == "__main__":
    unittest.main()
