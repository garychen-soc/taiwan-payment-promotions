from __future__ import annotations

import json
import unittest

from payment_promotions_monitor.adapters import parse_document
from payment_promotions_monitor.fetch import FetchResult


class JsonAdapterTests(unittest.TestCase):
    def test_taiwanpay_detail_ignores_related_campaigns(self) -> None:
        payload = {
            "body": {
                "campaignDetail": {
                    "systemSeq": "CURRENT",
                    "title": "停車就用TWQR！",
                    "content": "活動期間享回饋。",
                },
                "recommendedCampaigns": [
                    {
                        "systemSeq": "RELATED",
                        "title": "（活動額滿）另一檔推薦活動",
                    }
                ],
            }
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        result = FetchResult(
            requested_url="https://www.taiwanpay.com.tw/detail",
            final_url="https://www.taiwanpay.com.tw/detail",
            status_code=200,
            body=body,
            text=body.decode(),
            content_type="application/json",
            content_hash="hash",
        )

        document = parse_document(result)

        self.assertEqual(document.title, "停車就用TWQR！")
        self.assertNotIn("另一檔推薦活動", document.text)
        self.assertNotIn("活動額滿", document.text)


if __name__ == "__main__":
    unittest.main()
