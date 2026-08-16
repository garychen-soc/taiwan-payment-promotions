from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts.notify_webhook import build_message, post_webhook, provider_counts


class NotifyWebhookTests(unittest.TestCase):
    def test_message_contains_operational_summary_and_deduplicated_provider_counts(self) -> None:
        activity = {
            "provider_id": "taiwanpay",
            "provider_name": "台灣 Pay",
            "external_id": "one",
            "title": "活動一",
            "url": "https://www.taiwanpay.com.tw/event/one",
        }
        report = {
            "run": {"coverage": {"failed": 1, "transport_status": "partial"}},
            "summary": {"included_non_expired": 2, "sold_out_or_partial": 1},
            "sections": {
                "active_public": [activity],
                "sold_out": [activity, dict(activity, external_id="two", title="活動二")],
            },
            "coverage_gaps": [{"issue": "listing_zero_discovery"}],
            "changes": [{"kind": "updated"}],
            "cache": {"refetched": 8, "reused": 12},
        }

        self.assertEqual(provider_counts(report), [("台灣 Pay", 2)])
        message = build_message(
            report,
            stage="網站已建置",
            error="detail timeout",
            commit="abc1234",
        )

        self.assertIn("部分成功", message)
        self.assertIn("活動：2 筆；額滿／部分額滿 1 筆", message)
        self.assertIn("台灣 Pay 2", message)
        self.assertIn("重抓 8、沿用 12", message)
        self.assertIn("Coverage gaps：1", message)
        self.assertIn("Commit：abc1234", message)
        self.assertIn("進度：網站已建置", message)
        self.assertIn("錯誤：detail timeout", message)

    @patch("scripts.notify_webhook.subprocess.run")
    def test_post_webhook_uses_curl_json_without_logging_url(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "ok"
        run.return_value.stderr = ""

        post_webhook("https://hooks.example.test/secret", "成功，2 筆")

        args = run.call_args.args[0]
        self.assertEqual(args[0], "curl")
        self.assertEqual(args[-1], "https://hooks.example.test/secret")
        payload = json.loads(args[args.index("--data-binary") + 1])
        self.assertEqual(payload, {"text": "成功，2 筆"})
        self.assertTrue(run.call_args.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
