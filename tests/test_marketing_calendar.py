import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard


class Cursor:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []
    def execute(self, sql, params=()): self.calls.append((" ".join(sql.split()), params))
    def fetchall(self): return self.batches.pop(0) if self.batches else []


class Conn:
    def __init__(self, batches): self.cursor_value = Cursor(batches)
    def cursor(self, *args, **kwargs): return self.cursor_value
    def close(self): pass


class MarketingCalendarTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()

    def test_brand_filter_binds_plans_and_runs_and_keeps_sections_separate(self):
        conn = Conn([
            [{"id": 4, "name": "Deployden"}],
            [{"id": 10, "brand_id": 4, "brand_name": "Deployden", "title": "Launch", "kind": "social_post", "channel": "linkedin", "status": "draft", "planned_at": None, "url": "/brands/4/work/10"}],
            [{"id": 20, "brand_id": 4, "brand_name": "Deployden", "item_id": 10, "title": "Launch", "revision": 2, "send_at": None, "state": "cancelled"}],
        ])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.get("/calendar?brand_id=4")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Planning dates", body)
        self.assertIn("Approved delivery runs", body)
        self.assertIn("cancelled", body)
        plan_call = next(call for call in conn.cursor_value.calls if "marketing_work_items" in call[0])
        run_call = next(call for call in conn.cursor_value.calls if "marketing_campaign_runs" in call[0])
        self.assertEqual(plan_call[1], (4, 4))
        self.assertEqual(run_call[1], (4,))

    def test_default_all_brands_and_bad_filter(self):
        conn = Conn([[{"id": 4, "name": "Deployden"}], [], []])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.get("/calendar")
            bad = self.client.get("/calendar?brand_id=0")
            text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bad.status_code, 400)
        for invalid in ("²", "99999999999", "2147483648", "-1", "abc"):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.get("/calendar", query_string={"brand_id": invalid}).status_code, 400)
        self.assertIn("All brands", text)
        plan_call = next(call for call in conn.cursor_value.calls if "marketing_work_items" in call[0])
        run_call = next(call for call in conn.cursor_value.calls if "marketing_campaign_runs" in call[0])
        self.assertEqual(plan_call[1], ())
        self.assertEqual(run_call[1], ())

    def test_calendar_does_not_render_contract_or_recipient_fields(self):
        conn = Conn([[{"id": 4, "name": "Brand"}], [], [{"id": 1, "brand_id": 4, "brand_name": "Brand", "item_id": 2, "title": "Email", "revision": 1, "send_at": None, "state": "approved"}]])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            body = self.client.get("/calendar").get_data(as_text=True)
        self.assertNotIn("contract", body.lower())
        self.assertNotIn("recipient", body.lower())


if __name__ == "__main__": unittest.main()
