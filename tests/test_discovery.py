from __future__ import annotations

import unittest

from payment_promotions_monitor.discovery import Crawler


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


if __name__ == "__main__":
    unittest.main()
