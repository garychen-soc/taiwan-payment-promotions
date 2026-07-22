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

    def test_numeric_probe_bootstrap_is_deferred_and_bounded(self) -> None:
        source = {
            "name": "官方活動 API 編號探索",
            "role": "activity_listing",
            "url": "https://service.pxpayplus.com/px-advertise/web/activity/detail/",
            "adapter": "fullpay_id_scan",
            "bootstrap_end_id": 3,
            "maximum_probe_ids": 3,
        }
        provider = dict(self.provider, sources=[source], seeds=[])
        other = {
            "id": "other",
            "name": "其他業者",
            "official_domains": ["example.com"],
            "sources": [
                {
                    "name": "其他列表",
                    "role": "activity_listing",
                    "url": "https://example.com/events",
                }
            ],
            "seeds": [],
        }
        crawler = Crawler({"providers": [provider, other]}, self.now)
        tasks = crawler._initial_tasks("full", [])
        self.assertEqual(tasks[0].provider["id"], "other")
        self.assertEqual([task.external_id for task in tasks[1:]], ["1", "2", "3"])
        self.assertTrue(all(task.adapter == "fullpay_id_probe" for task in tasks[1:]))

    def test_numeric_probe_treats_2001_as_a_successful_empty_id(self) -> None:
        tasks = self.crawler._fullpay_probe_tasks(
            self.provider,
            {
                "name": "官方活動 API 編號探索",
                "bootstrap_end_id": 1,
                "maximum_probe_ids": 1,
            },
        )
        body = json.dumps({"code": "2001", "message": "查無活動", "data": None}).encode()
        response = FetchResult(
            tasks[0].fetch_url,
            tasks[0].fetch_url,
            200,
            body,
            body.decode(),
            "application/json",
            "empty",
        )
        with patch("payment_promotions_monitor.discovery.fetch_url", return_value=response):
            collected = self.crawler._attempt(tasks[0])
        self.assertIsNone(collected)
        self.assertTrue(self.crawler.attempts[-1].ok)
        self.assertIsNone(self.crawler.attempts[-1].coverage_issue)
        self.assertEqual(self.crawler._fullpay_scan_plans["fullpay"]["outcomes"][1], "not_found")

    def test_numeric_probe_failure_does_not_advance_high_water_mark(self) -> None:
        crawler = Crawler(
            {"providers": [self.provider]},
            self.now,
            discovery_state={"fullpay": 5},
        )
        tasks = crawler._fullpay_probe_tasks(
            self.provider,
            {
                "name": "官方活動 API 編號探索",
                "rescan_window": 1,
                "frontier_buffer": 2,
                "maximum_probe_ids": 3,
            },
        )
        crawler._record_fullpay_probe_outcome(tasks[0], "valid")
        crawler._record_fullpay_probe_outcome(tasks[1], "valid")
        crawler._record_fullpay_probe_outcome(tasks[2], "failure")
        self.assertEqual(crawler._fullpay_discovery_state_updates(), {})

    def test_numeric_probe_transport_failure_is_not_an_empty_id(self) -> None:
        tasks = self.crawler._fullpay_probe_tasks(
            self.provider,
            {
                "name": "官方活動 API 編號探索",
                "bootstrap_end_id": 1,
                "maximum_probe_ids": 1,
            },
        )
        with patch(
            "payment_promotions_monitor.discovery.fetch_url",
            side_effect=RuntimeError("temporary DNS failure"),
        ):
            collected = self.crawler._attempt(tasks[0])
        self.assertIsNone(collected)
        self.assertFalse(self.crawler.attempts[-1].ok)
        self.assertEqual(self.crawler.attempts[-1].coverage_issue, "id_probe_incomplete")
        self.assertEqual(self.crawler._fullpay_scan_plans["fullpay"]["outcomes"][1], "failure")
        self.assertEqual(self.crawler._fullpay_discovery_state_updates(), {})

    def test_complete_numeric_probe_advances_to_highest_valid_id(self) -> None:
        crawler = Crawler(
            {"providers": [self.provider]},
            self.now,
            discovery_state={"fullpay": 5},
        )
        tasks = crawler._fullpay_probe_tasks(
            self.provider,
            {
                "name": "官方活動 API 編號探索",
                "rescan_window": 1,
                "frontier_buffer": 2,
                "maximum_probe_ids": 3,
            },
        )
        crawler._record_fullpay_probe_outcome(tasks[0], "valid")
        crawler._record_fullpay_probe_outcome(tasks[1], "not_found")
        crawler._record_fullpay_probe_outcome(tasks[2], "valid")
        self.assertEqual(
            crawler._fullpay_discovery_state_updates(),
            {
                "fullpay": {
                    "highest_valid_event_id": 7,
                    "scan_frontier_event_id": 7,
                }
            },
        )

    def test_complete_empty_frontier_still_moves_forward(self) -> None:
        crawler = Crawler(
            {"providers": [self.provider]},
            self.now,
            discovery_state={
                "fullpay": {
                    "highest_valid_event_id": 5,
                    "scan_frontier_event_id": 7,
                }
            },
        )
        tasks = crawler._fullpay_probe_tasks(
            self.provider,
            {
                "name": "官方活動 API 編號探索",
                "rescan_window": 1,
                "frontier_buffer": 2,
                "maximum_probe_ids": 3,
            },
        )
        for task in tasks:
            crawler._record_fullpay_probe_outcome(task, "not_found")
        self.assertEqual(
            crawler._fullpay_discovery_state_updates(),
            {
                "fullpay": {
                    "highest_valid_event_id": 5,
                    "scan_frontier_event_id": 9,
                }
            },
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

    def test_expired_numeric_probe_skips_quota_endpoint(self) -> None:
        task = Task(
            provider=self.provider,
            source_name="活動探索",
            role="activity_probe",
            fetch_url="https://service.pxpayplus.com/px-advertise/web/activity/detail/81",
            display_url=(
                "https://marketing.pxpayplus.com/pxplus_marketing_page/"
                "activity_content_page?EventId=81"
            ),
            adapter="fullpay_id_probe",
            external_id="81",
        )
        body = json.dumps(
            {
                "code": "0000",
                "data": {
                    "title": "已結束活動",
                    "activity_start_time": "2026/07/01 00:00",
                    "activity_end_time": "2026/07/21 23:59",
                },
            }
        ).encode()
        detail = FetchResult(task.fetch_url, task.fetch_url, 200, body, body.decode(), "application/json", "a")
        with patch("payment_promotions_monitor.discovery.fetch_url") as mocked_fetch:
            result = self.crawler._fetch_fullpay_detail(
                task,
                initial_result=detail,
                skip_expired_quota=True,
            )
        mocked_fetch.assert_not_called()
        payload = json.loads(result.text)
        self.assertEqual(payload["public_quota_status"]["skipped"], "expired_activity")

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


class OfficialProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 22, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    @staticmethod
    def _attempt(provider: dict[str, object], task: Task, now: datetime) -> SourceAttempt:
        return SourceAttempt(
            provider_id=str(provider["id"]),
            provider_name=str(provider["name"]),
            source_name=task.source_name,
            role=task.role,
            url=task.fetch_url,
            ok=True,
            fetched_at=now.isoformat(),
        )

    def test_pi_wordpress_listing_decodes_title_and_summary(self) -> None:
        provider = {
            "id": "piwallet",
            "name": "Pi 拍錢包",
            "official_domains": ["piapp.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        task = Task(
            provider=provider,
            source_name="Pi 官方活動 API",
            role="activity_listing",
            fetch_url="https://web.piapp.com.tw/wp-json/wp/v2/posts?categories=3",
            display_url="https://web.piapp.com.tw/events/",
            adapter="pi_wordpress_listing",
            max_links=10,
        )
        crawler.attempts.append(self._attempt(provider, task, self.now))
        document = Document(
            task.fetch_url,
            "posts",
            "",
            [],
            "pi",
            raw_json=[
                {
                    "id": 7,
                    "link": "https://web.piapp.com.tw/event/summer/",
                    "title": {"rendered": "夏日 &#038; 回饋"},
                    "excerpt": {"rendered": "<p>2026/7/1-8/31 最高&nbsp;10% 回饋</p>"},
                },
                {
                    "id": 8,
                    "link": "https://example.com/not-official",
                    "title": {"rendered": "外部文章"},
                },
            ],
        )
        tasks = crawler._discovered_tasks(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title_hint, "夏日 & 回饋")
        self.assertEqual(tasks[0].summary_hint, "2026/7/1-8/31 最高 10% 回饋")
        self.assertEqual(tasks[0].adapter, "pi_post_detail")
        self.assertEqual(tasks[0].external_id, "7")
        self.assertEqual(tasks[0].start_date, "2026-07-01")
        self.assertEqual(tasks[0].end_date, "2026-08-31")

        announcement_task = Task(
            provider=provider,
            source_name="Pi 官方公告 API",
            role="announcement_listing",
            fetch_url="https://web.piapp.com.tw/wp-json/wp/v2/posts?categories=55",
            display_url="https://web.piapp.com.tw/",
            adapter="pi_wordpress_listing",
            max_links=10,
        )
        crawler.attempts.append(self._attempt(provider, announcement_task, self.now))
        announcement_tasks = crawler._discovered_tasks(
            CollectedDocument(announcement_task, document, self.now.isoformat())
        )
        self.assertEqual(announcement_tasks[0].role, "announcement_detail")

    def test_pxmart_campaign_listing_requires_exact_px_pay_label(self) -> None:
        provider = {
            "id": "pxpay",
            "name": "PX Pay",
            "official_domains": ["pxmart.com.tw", "pxmartevent.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        task = Task(
            provider=provider,
            source_name="全聯官方活動卡",
            role="activity_listing",
            fetch_url="https://www.pxmart.com.tw/campaign/pxpay-card",
            display_url="https://www.pxmart.com.tw/campaign/pxpay-card",
            adapter="pxmart_campaign_listing",
            max_links=10,
        )
        crawler.attempts.append(self._attempt(provider, task, self.now))
        campaigns = [
            {
                "id": 84,
                "attributes": {
                    "title": "PX Pay 週末最高3%",
                    "label": "PX Pay",
                    "openDate": "2026-06-26",
                    "closeDate": "2026-08-06",
                },
            },
            {
                "id": 85,
                "attributes": {
                    "title": "實體信用卡最高3%",
                    "label": "信用卡",
                    "openDate": "2026-06-26",
                    "closeDate": "2026-08-06",
                },
            },
        ]
        document = Document(
            task.fetch_url,
            "PX Pay 與信用卡",
            "",
            [],
            "px",
            raw_json={"props": {"pageProps": {"campaigns": campaigns}}},
        )
        tasks = crawler._discovered_tasks(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title_hint, "PX Pay 週末最高3%")
        self.assertNotIn("實體信用卡", tasks[0].display_url)
        self.assertEqual(tasks[0].start_date, "2026-06-26")

    def test_pxmart_campaign_does_not_apply_fullpay_app_notice_to_pxpay(self) -> None:
        provider = {
            "id": "pxpay",
            "name": "PX Pay",
            "official_domains": ["pxmart.com.tw", "pxmartevent.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        task = Task(
            provider=provider,
            source_name="PX Pay 活動詳情",
            role="activity_detail",
            fetch_url="https://www.pxmart.com.tw/campaign/pxpay-card/weekend",
            display_url="https://www.pxmart.com.tw/campaign/pxpay-card/weekend",
            title_hint="PX Pay 週末活動",
            adapter="pxmart_campaign_detail",
            start_date="2026-06-26",
            end_date="2026-08-06",
        )
        document = Document(
            task.fetch_url,
            "PX Pay 週末活動",
            "台新銀行全支付活動額滿時間請依全支付官網／APP公告為主。",
            [],
            "campaign",
        )
        activity = crawler._activity_from_document(
            CollectedDocument(task, document, self.now.isoformat())
        )
        self.assertEqual(activity.quota_status, "not_marked_full")
        self.assertEqual(activity.evidence, [])

    def test_pxmart_campaign_recheck_keeps_brand_specific_adapter(self) -> None:
        provider = {
            "id": "pxpay",
            "name": "PX Pay",
            "official_domains": ["pxmart.com.tw", "pxmartevent.com.tw"],
            "sources": [],
            "seeds": [],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        url = "https://www.pxmart.com.tw/campaign/pxpay-card/weekend"
        tasks = crawler._initial_tasks(
            "full",
            [
                {
                    "provider_id": "pxpay",
                    "url": url,
                    "source_url": url,
                    "external_id": "pxmart-campaign-84",
                }
            ],
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].adapter, "pxmart_campaign_detail")

    def test_status_mode_refreshes_famipay_registered_card_source(self) -> None:
        provider = {
            "id": "famipay",
            "name": "My FamiPay",
            "official_domains": ["family.com.tw"],
            "sources": [
                {
                    "name": "全家官方 FamiPay 活動卡",
                    "role": "activity_listing",
                    "url": "https://www.family.com.tw/Marketing/zh/Event",
                    "adapter": "famipay_listing",
                }
            ],
            "seeds": [],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        tasks = crawler._initial_tasks("status", [])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].adapter, "famipay_listing")

    def test_pxmart_quota_combines_sold_out_and_available_bank_evidence(self) -> None:
        provider = {
            "id": "pxpay",
            "name": "PX Pay",
            "official_domains": ["pxmart.com.tw", "pxmartevent.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        task = Task(
            provider=provider,
            source_name="福利點即時名額 → 2026-06-26~2026-08-06",
            role="quota_listing_detail",
            fetch_url="https://cardpoint.pxmartevent.com.tw/event/GetEventList?event_type=1",
            display_url="https://cardpoint.pxmartevent.com.tw/",
            title_hint="PX Pay 指定銀行週末滿額福利點活動",
            adapter="pxmart_quota_detail",
            external_id="pxpay-quota-2026-06-26~2026-08-06",
            start_date="2026-06-26",
            end_date="2026-08-06",
        )
        crawler.attempts.append(self._attempt(provider, task, self.now))
        records = [
            {
                "bank_name": "國泰世華銀行",
                "pay_type": "[1,2]",
                "left_count": None,
                "disbursed_time": "2026-06-27 17:55:13",
                "type_text": "週六日單筆消費滿1,200元，贈360福利點",
            },
            {
                "bank_name": "台新銀行",
                "pay_type": "[1]",
                "left_count": 526,
                "disbursed_time": None,
                "type_text": "週六日單筆消費滿1,200元，贈360福利點",
            },
            {
                "bank_name": "僅全支付銀行",
                "pay_type": "[2]",
                "left_count": 100,
                "disbursed_time": None,
                "type_text": "不應納入",
            },
        ]
        document = Document(
            task.fetch_url,
            "quota",
            "",
            [],
            "quota",
            raw_json={"success": True, "data": records},
        )
        crawler._discovered_tasks(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(crawler.activity_documents), 1)
        activity_document = crawler.activity_documents[0]
        self.assertIn("國泰世華銀行 本期名額已額滿", activity_document.document.text)
        self.assertIn("台新銀行 官方即時狀態尚有 526 份名額", activity_document.document.text)
        self.assertNotIn("僅全支付銀行", activity_document.document.text)
        activity = crawler._activity_from_document(activity_document)
        self.assertEqual(activity.quota_status, "partial_sold_out")
        self.assertEqual(activity.evidence[0].kind, "activity_status_api")
        self.assertIn("GetEventList", activity.evidence[0].source_url)
        self.assertEqual(len(activity.evidence), 2)
        self.assertIn("尚有 526 份名額", activity.evidence[1].excerpt)

    def test_pxmart_quota_activity_urls_are_unique_per_period(self) -> None:
        provider = {
            "id": "pxpay",
            "name": "PX Pay",
            "official_domains": ["pxmart.com.tw", "pxmartevent.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        record = {
            "bank_name": "台新銀行",
            "pay_type": "[1]",
            "left_count": 526,
            "disbursed_time": None,
            "type_text": "消費滿額贈福利點",
        }
        for period in ("2026-06-26~2026-08-06", "2026-07-01~2026-09-30"):
            start_date, end_date = period.split("~")
            fetch_url = (
                "https://cardpoint.pxmartevent.com.tw/event/GetEventList?"
                f"event_type=1&period={period}&pay_type=%5B%221%22%5D"
            )
            task = Task(
                provider=provider,
                source_name=f"quota {period}",
                role="quota_listing_detail",
                fetch_url=fetch_url,
                display_url="https://cardpoint.pxmartevent.com.tw/",
                title_hint="PX Pay 指定銀行週末滿額福利點活動",
                adapter="pxmart_quota_detail",
                external_id=f"pxpay-quota-{period}",
                start_date=start_date,
                end_date=end_date,
            )
            crawler.attempts.append(self._attempt(provider, task, self.now))
            document = Document(
                fetch_url,
                "quota",
                "",
                [],
                period,
                raw_json={"success": True, "data": [record]},
            )
            crawler._collect_pxmart_quota(CollectedDocument(task, document, self.now.isoformat()))
        urls = [item.task.display_url for item in crawler.activity_documents]
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(set(urls)), 2)
        self.assertTrue(all("GetEventList" in url and "period=" in url for url in urls))
        activities = [crawler._activity_from_document(item) for item in crawler.activity_documents]
        self.assertTrue(all(item.quota_status == "confirmed_available" for item in activities))
        self.assertTrue(all(item.evidence for item in activities))

    def test_famipay_uses_official_card_without_following_external_detail(self) -> None:
        provider = {
            "id": "famipay",
            "name": "My FamiPay",
            "official_domains": ["family.com.tw"],
        }
        crawler = Crawler({"providers": [provider]}, self.now)
        task = Task(
            provider=provider,
            source_name="全家官方 FamiPay 活動卡",
            role="activity_listing",
            fetch_url="https://www.family.com.tw/Marketing/zh/Event",
            display_url="https://www.family.com.tw/Marketing/zh/Event",
            adapter="famipay_listing",
            max_links=10,
        )
        crawler.attempts.append(self._attempt(provider, task, self.now))
        source = """
        <div class="card card--event">
          <a class="card__cover-link" href="https://bank.example/promo"></a>
          <p class="card__date">2026/05/27 - 2026/08/31</p>
          <h6 class="card__title">全家FamiPay綁卡付</h6>
          <p class="card__text line-clamp">小樹點一鍵折抵</p>
        </div>
        <div class="card card--event">
          <a class="card__cover-link" href="https://bank.example/card"></a>
          <p class="card__date">2026/05/27 - 2026/08/31</p>
          <h6 class="card__title">一般信用卡優惠</h6>
        </div>
        <div class="card card--event">
          <a class="card__cover-link" href="https://another-bank.example/promo"></a>
          <p class="card__date">長期活動</p>
          <h6 class="card__title">My FamiPay支付優惠</h6>
          <p class="card__text line-clamp">結帳妙招</p>
        </div>
        """
        document = Document(
            task.fetch_url,
            "全家活動",
            "",
            [],
            "family",
            raw_json={"html": source},
        )
        crawler._discovered_tasks(CollectedDocument(task, document, self.now.isoformat()))
        self.assertEqual(len(crawler.activity_documents), 2)
        urls = [item.task.display_url for item in crawler.activity_documents]
        self.assertEqual(len(set(urls)), 2)
        self.assertTrue(all("monitor_card=" in url for url in urls))
        collected = crawler.activity_documents[0]
        self.assertEqual(collected.document.url, task.fetch_url)
        self.assertEqual(collected.document.links, [])
        self.assertNotIn("bank.example", collected.document.text)
        activity = crawler._activity_from_document(collected)
        self.assertEqual(activity.title, "全家FamiPay綁卡付")
        self.assertEqual(activity.end_date, "2026-08-31")


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
