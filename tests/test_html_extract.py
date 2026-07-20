from __future__ import annotations

import unittest

from payment_promotions_monitor.html_extract import best_title, parse_html


class HtmlTitleTests(unittest.TestCase):
    def test_generic_page_title_uses_first_heading(self) -> None:
        parsed = parse_html(
            """
            <html><head><title>活動訊息 - icash Pay</title></head>
            <body><nav><a>icash2.0</a></nav><h1>暑假遊韓最速PAY TWQR最高回饋52%</h1></body></html>
            """,
            "https://www.icashpay.com.tw/advertMessage/view/id/2372",
        )

        self.assertEqual(best_title(parsed), "暑假遊韓最速PAY TWQR最高回饋52%")


if __name__ == "__main__":
    unittest.main()
