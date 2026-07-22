from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from payment_promotions_monitor.adapters import Document, parse_document
from payment_promotions_monitor.discovery import CollectedDocument, Crawler, Task
from payment_promotions_monitor.fetch import FetchResult
from payment_promotions_monitor.models import SourceAttempt


class TaiwanPayConsistencyTests(unittest.TestCase):
    def test_rejects_unrelated_detail_title(self) -> None:
        self.assertFalse(
            Crawler._titles_compatible(
                "停車就用TWQR！",
                "(活動額滿)嘟嘟房停車就用台灣Pay，每筆交易享10元回饋",
            )
        )

    def test_accepts_shortened_detail_title(self) -> None:
        self.assertTrue(
            Crawler._titles_compatible(
                "（活動額滿）光復市場買好料！台灣Pay享20%回饋",
                "（活動額滿）光復市場買好料！",
            )
        )

    def test_transient_post_failure_is_retried(self) -> None:
        now = datetime(2026, 7, 22, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        provider = {
            "id": "taiwanpay",
            "name": "台灣 Pay",
            "official_domains": ["taiwanpay.com.tw"],
        }
        task = Task(
            provider=provider,
            source_name="活動詳情",
            role="activity_detail",
            fetch_url="https://www.taiwanpay.com.tw/api/detail",
            display_url="https://www.taiwanpay.com.tw/event/ABC12345",
            adapter="taiwanpay_detail",
            external_id="ABC12345",
        )
        body = json.dumps(
            {
                "header": {"returnCode": "TF0000"},
                "body": {
                    "campaignDetail": {
                        "systemSeq": "ABC12345",
                        "title": "台灣 Pay 活動",
                        "startDate": "2026-07-01",
                        "endDate": "2026-08-31",
                    }
                },
            }
        ).encode()
        success = FetchResult(task.fetch_url, task.fetch_url, 200, body, body.decode(), "application/json", "hash")
        crawler = Crawler({"providers": [provider]}, now)
        with patch(
            "payment_promotions_monitor.discovery.post_json_url",
            side_effect=[RuntimeError("HTTP 503"), success],
        ) as mocked_post:
            collected = crawler._attempt(task)
        self.assertIsNotNone(collected)
        self.assertEqual(mocked_post.call_count, 2)
        self.assertTrue(crawler.attempts[-1].ok)


class FullpayDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = {
            "id": "fullpay",
            "name": "全支付",
            "official_domains": ["pxpayplus.com"],
        }
        self.now = datetime(2026, 7, 22, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        self.crawler = Crawler({"providers": [self.provider]}, self.now)

    def test_hub_links_are_converted_to_validated_detail_api_tasks(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="官方總覽",
            role="activity_listing",
            fetch_url="https://prod-s3.pxpayplus.com/MKT_Event/100Big2026.json",
            display_url="https://marketing.pxpayplus.com/pxplus_marketing_page/100big_2026",
            max_links=40,
            adapter="fullpay_hub_listing",
        )
        document = Document(
            url=task.fetch_url,
            title="總覽",
            text="",
            links=[
                (
                    "https://marketing.pxpayplus.com/pxplus_marketing_page/activity_content_page?EventId=82",
                    "飲料季",
                ),
                (
                    "https://marketing.pxpayplus.com/pxplus_marketing_page/activity_content_page?EventId=82",
                    "重複",
                ),
                ("https://marketing.pxpayplus.com/pxplus_marketing_page/KoreaEvent", "非詳情頁"),
            ],
            content_hash="hub",
        )
        discovered = self.crawler._fullpay_activity_tasks(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].external_id, "82")
        self.assertEqual(discovered[0].adapter, "fullpay_detail")
        self.assertEqual(
            discovered[0].fetch_url,
            "https://service.pxpayplus.com/px-advertise/web/activity/detail/82",
        )

    def test_news_json_is_split_into_individual_announcement_documents(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="官網最新消息",
            role="announcement_listing",
            fetch_url="https://prod-s3.pxpayplus.com/qaandtutor/pxplus_news.json",
            display_url="https://www.pxpayplus.com/news",
            max_links=120,
            adapter="fullpay_news_listing",
        )
        raw_json = {
            "重要公告": {
                "54": {
                    "title": "活動回饋公告",
                    "date": "2026/07/22",
                    "content": (
                        '<p><a href="https://marketing.pxpayplus.com/pxplus_marketing_page/'
                        'activity_content_page?EventId=82">指定活動</a>已額滿。</p>'
                    ),
                }
            }
        }
        document = Document(task.fetch_url, "最新消息", "", [], "news", raw_json)
        self.crawler.attempts.append(
            SourceAttempt(
                provider_id="fullpay",
                provider_name="全支付",
                source_name=task.source_name,
                role=task.role,
                url=task.fetch_url,
                ok=True,
                fetched_at=self.now.isoformat(),
            )
        )
        self.crawler._collect_fullpay_news(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(self.crawler.announcement_documents), 1)
        announcement = self.crawler.announcement_documents[0]
        self.assertEqual(announcement.document.title, "活動回饋公告")
        self.assertIn("指定活動已額滿", announcement.document.text)
        self.assertIn("EventId=82", announcement.document.text)
        self.assertIn("%E9%87%8D%E8%A6%81%E5%85%AC%E5%91%8A/54", announcement.task.display_url)
        self.assertEqual(self.crawler.attempts[0].discovered_count, 1)

    def test_detail_adapter_combines_public_quota_status(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="活動",
            role="activity_detail",
            fetch_url="https://service.pxpayplus.com/px-advertise/web/activity/detail/82",
            display_url=(
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                "activity_content_page?EventId=82"
            ),
            adapter="fullpay_detail",
            external_id="82",
        )
        detail_body = json.dumps(
            {
                "code": "0000",
                "data": {
                    "title": "飲料季",
                    "activity_start_time": "2026/07/01 00:00",
                    "activity_end_time": "2026/08/31 23:59",
                },
            }
        ).encode()
        quota_body = json.dumps(
            {"code": "0000", "data": {"full_quota_time": "2026/07/22 12:00"}}
        ).encode()
        responses = [
            FetchResult(task.fetch_url, task.fetch_url, 200, detail_body, detail_body.decode(), "application/json", "a"),
            FetchResult(
                "https://service.pxpayplus.com/px-advertise/web/activity/login_info/82",
                "https://service.pxpayplus.com/px-advertise/web/activity/login_info/82",
                200,
                quota_body,
                quota_body.decode(),
                "application/json",
                "b",
            ),
        ]
        with patch("payment_promotions_monitor.discovery.fetch_url", side_effect=responses):
            result = self.crawler._fetch_fullpay_detail(task)
        payload = json.loads(result.text)
        self.assertIn("活動已達上限", payload["public_quota_status"]["notice"])

    def test_null_full_quota_time_is_not_treated_as_sold_out(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="活動",
            role="activity_detail",
            fetch_url="https://service.pxpayplus.com/px-advertise/web/activity/detail/82",
            display_url=(
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                "activity_content_page?EventId=82"
            ),
            adapter="fullpay_detail",
            external_id="82",
        )
        detail_body = json.dumps(
            {
                "code": "0000",
                "data": {
                    "title": "飲料季",
                    "activity_start_time": "2026/07/01 00:00",
                    "activity_end_time": "2026/08/31 23:59",
                },
            }
        ).encode()
        quota_body = json.dumps(
            {"code": "0000", "data": {"full_quota_time": None}}
        ).encode()
        responses = [
            FetchResult(task.fetch_url, task.fetch_url, 200, detail_body, detail_body.decode(), "application/json", "a"),
            FetchResult(
                "https://service.pxpayplus.com/px-advertise/web/activity/login_info/82",
                "https://service.pxpayplus.com/px-advertise/web/activity/login_info/82",
                200,
                quota_body,
                quota_body.decode(),
                "application/json",
                "b",
            ),
        ]
        with patch("payment_promotions_monitor.discovery.fetch_url", side_effect=responses):
            result = self.crawler._fetch_fullpay_detail(task)
        payload = json.loads(result.text)
        self.assertEqual(payload["public_quota_status"]["full_quota_time"], "")
        self.assertEqual(payload["public_quota_status"]["notice"], "")

    def test_quota_failure_keeps_detail_and_records_status_failure(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="活動",
            role="activity_detail",
            fetch_url="https://service.pxpayplus.com/px-advertise/web/activity/detail/82",
            display_url=(
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                "activity_content_page?EventId=82"
            ),
            adapter="fullpay_detail",
            external_id="82",
        )
        detail_body = json.dumps(
            {
                "code": "0000",
                "data": {
                    "title": "飲料季",
                    "activity_start_time": "2026/07/01 00:00",
                    "activity_end_time": "2026/08/31 23:59",
                },
            }
        ).encode()
        detail_result = FetchResult(
            task.fetch_url,
            task.fetch_url,
            200,
            detail_body,
            detail_body.decode(),
            "application/json",
            "a",
        )
        with patch(
            "payment_promotions_monitor.discovery.fetch_url",
            side_effect=[detail_result, RuntimeError("quota timeout")],
        ):
            result = self.crawler._fetch_fullpay_detail(task)
        payload = json.loads(result.text)
        self.assertEqual(payload["data"]["title"], "飲料季")
        self.assertTrue(payload["public_quota_status"]["unavailable"])
        self.assertEqual(len(self.crawler.attempts), 1)
        self.assertFalse(self.crawler.attempts[0].ok)
        self.assertEqual(self.crawler.attempts[0].role, "status_page")
        activity = self.crawler._activity_from_document(
            CollectedDocument(task, parse_document(result), self.now.isoformat())
        )
        self.assertEqual(activity.quota_status, "unknown_source_failure")
        self.assertFalse(activity.quota_evidence_complete)
        self.assertTrue(activity.review_required)

    def test_quota_api_sold_out_evidence_uses_status_endpoint(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="活動",
            role="activity_detail",
            fetch_url="https://service.pxpayplus.com/px-advertise/web/activity/detail/82",
            display_url=(
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                "activity_content_page?EventId=82"
            ),
            adapter="fullpay_detail",
            external_id="82",
        )
        payload = {
            "code": "0000",
            "data": {
                "title": "飲料季",
                "activity_start_time": "2026/07/01 00:00",
                "activity_end_time": "2026/08/31 23:59",
            },
            "public_quota_status": {
                "source_url": "https://service.pxpayplus.com/px-advertise/web/activity/login_info/82",
                "full_quota_time": "2026/07/22 12:00",
                "notice": "活動已達上限（官方公開狀態時間：2026/07/22 12:00）",
                "unavailable": False,
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        result = FetchResult(task.fetch_url, task.fetch_url, 200, body, body.decode(), "application/json", "hash")
        activity = self.crawler._activity_from_document(
            CollectedDocument(task, parse_document(result), self.now.isoformat())
        )
        self.assertEqual(activity.quota_status, "sold_out")
        self.assertEqual(activity.evidence[0].kind, "activity_status_api")
        self.assertTrue(activity.evidence[0].source_url.endswith("/login_info/82"))

    def test_hub_reports_only_when_more_links_exist_than_its_limit(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="官方總覽",
            role="activity_listing",
            fetch_url="https://prod-s3.pxpayplus.com/MKT_Event/hub.json",
            display_url="https://marketing.pxpayplus.com/hub",
            max_links=2,
            adapter="fullpay_hub_listing",
        )
        links = [
            (
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                f"activity_content_page?EventId={event_id}",
                "",
            )
            for event_id in (1, 2, 3)
        ]
        document = Document(task.fetch_url, "總覽", "", links, "hub")
        self.crawler.attempts.append(
            SourceAttempt(
                provider_id="fullpay",
                provider_name="全支付",
                source_name=task.source_name,
                role=task.role,
                url=task.fetch_url,
                ok=True,
                fetched_at=self.now.isoformat(),
            )
        )
        discovered = self.crawler._discovered_tasks(
            CollectedDocument(task, document, self.now.isoformat())
        )
        self.assertEqual(len(discovered), 2)
        self.assertEqual(self.crawler.attempts[0].coverage_issue, "listing_reached_max_links")


class CrawlQueueFairnessTests(unittest.TestCase):
    def test_initial_sources_run_before_discovered_detail_pages_hit_global_cap(self) -> None:
        now = datetime(2026, 7, 22, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        providers = [
            {
                "id": provider_id,
                "name": provider_id,
                "official_domains": ["example.com"],
                "sources": [
                    {
                        "name": f"{provider_id} list",
                        "role": "activity_listing",
                        "url": f"https://example.com/{provider_id}/list",
                    }
                ],
                "seeds": [],
            }
            for provider_id in ("first", "second")
        ]

        class FakeCrawler(Crawler):
            def _attempt(self, task: Task) -> CollectedDocument | None:
                identity = self._task_identity(task)
                if identity in self._seen_fetches:
                    return None
                self._seen_fetches.add(identity)
                self.attempts.append(
                    SourceAttempt(
                        provider_id=task.provider["id"],
                        provider_name=task.provider["name"],
                        source_name=task.source_name,
                        role=task.role,
                        url=task.fetch_url,
                        ok=True,
                        fetched_at=now.isoformat(),
                    )
                )
                return CollectedDocument(
                    task,
                    Document(task.fetch_url, "list", "", [], task.fetch_url),
                    now.isoformat(),
                )

            def _discovered_tasks(self, collected: CollectedDocument) -> list[Task]:
                if collected.task.role != "activity_listing":
                    return []
                provider = collected.task.provider
                return [
                    Task(
                        provider=provider,
                        source_name="detail",
                        role="activity_detail",
                        fetch_url=f"https://example.com/{provider['id']}/detail",
                        display_url=f"https://example.com/{provider['id']}/detail",
                    )
                ]

        crawler = FakeCrawler(
            {
                "providers": providers,
                "max_total_pages": 2,
                "request_delay_seconds": 0,
            },
            now,
        )
        run = crawler.collect("full")
        self.assertEqual([item.provider_id for item in run.attempts], ["first", "second"])
        self.assertEqual(
            {item["provider_id"] for item in run.crawl_limit_pending},
            {"first", "second"},
        )


if __name__ == "__main__":
    unittest.main()
