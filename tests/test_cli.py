from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from payment_promotions_monitor.cli import main
from payment_promotions_monitor.models import RunResult, SourceAttempt


class CliFailureGateTests(unittest.TestCase):
    def test_all_sources_failed_returns_nonzero_after_writing_report(self) -> None:
        now = datetime(2026, 7, 22, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        failed_run = RunResult(
            "run",
            "full",
            now.isoformat(),
            now.isoformat(),
            [],
            [
                SourceAttempt(
                    provider_id="p",
                    provider_name="業者",
                    source_name="活動列表",
                    role="activity_listing",
                    url="https://example.com/events",
                    ok=False,
                    fetched_at=now.isoformat(),
                    error="RuntimeError: <urlopen error [Errno 8] nodename nor servname provided>",
                )
            ],
        )

        class FakeCrawler:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def collect(self, *args, **kwargs) -> RunResult:
                return failed_run

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "timezone": "Asia/Taipei",
                        "providers": [
                            {
                                "id": "p",
                                "name": "業者",
                                "official_domains": ["example.com"],
                                "sources": [{"role": "activity_listing"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("payment_promotions_monitor.cli.Crawler", FakeCrawler):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "--db",
                        str(root / "monitor.sqlite3"),
                        "--output-dir",
                        str(root / "reports"),
                        "--now",
                        now.isoformat(),
                    ]
                )
            self.assertEqual(exit_code, 4)
            self.assertTrue((root / "reports" / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
