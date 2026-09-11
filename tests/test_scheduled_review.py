"""Offline regressions for scheduled promotion processing."""
import email
from datetime import datetime, date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
NOW = datetime(2026, 9, 11, 12, tzinfo=ZoneInfo("Asia/Taipei"))
import importlib.util
from payment_promotions_monitor import dates, html_extract, models
from payment_promotions_monitor.storage import Store
spec = importlib.util.spec_from_file_location("review_build_site", Path(__file__).resolve().parents[1] / "scripts/build_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

class PaymentReview(unittest.TestCase):
    def test_P01_reversed_api_period_not_high_confidence(self):
        result = dates.parse_date_range('startDate: 2026-12-01\nendDate: 2026-08-01')
        self.assertNotEqual(result.confidence, 'high')

    def test_P02_malformed_link_does_not_abort_text(self):
        parsed = html_extract.parse_html('<p>Valid terms</p><a href="https://[">bad</a>', 'https://example.com')
        self.assertIn('Valid terms', parsed.text)

    def test_P03_new_period_does_not_inherit_old_sold_out(self):
        with tempfile.TemporaryDirectory() as d, Store(Path(d)/'synthetic.sqlite') as store:
            old = models.Activity('demo','Demo','Monthly','https://example.com/monthly','https://example.com/monthly',
                external_id='monthly', start_date='2026-08-01',end_date='2026-08-31',quota_status='sold_out',
                date_confidence='high', evidence=[models.Evidence('https://example.com','本期名額已額滿',NOW.isoformat())])
            store.upsert_activity(old)
            fresh = models.Activity('demo','Demo','Monthly','https://example.com/monthly','https://example.com/monthly',
                external_id='monthly',start_date='2026-09-01',end_date='2026-09-30',date_confidence='high')
            store.merge_persistent_status([fresh])
            self.assertEqual(fresh.quota_status, 'not_marked_full')
            self.assertEqual(fresh.evidence, [])

    def test_P04_no_requests_not_complete(self):
        run = models.RunResult('demo','full',NOW.isoformat(),NOW.isoformat(),[],[])
        self.assertEqual(run.coverage['transport_status'], 'unavailable')

    def test_P05_unproven_supplement_sold_out_rejected(self):
        config = {'providers':[{'id':'demo','name':'Demo','official_domains':['example.com']}]}
        supplement = {'supplemental_activities':[{'provider_id':'demo','title':'Demo','url':'https://example.com',
            'start_date':'2026-09-01','end_date':'2026-09-30','quota_status':'sold_out','evidence':[]}]}
        result = site._supplemental_activities(supplement, config, [], NOW.date())
        self.assertFalse(any(r['quota_status']=='sold_out' for r in result))
