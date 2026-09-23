import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
from psycopg2 import errors


class Cursor:
    def __init__(self, brand=None, plan=None, audit=True, task=None, raise_table=False):
        self.brand, self.plan, self.audit, self.task = brand, plan, audit, task
        self.raise_table = raise_table
        self.sql = ""
        self.params = None
        self.calls = []

    def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        self.calls.append((sql, params))
        if self.raise_table:
            raise errors.UndefinedTable("content_calendar")

    def fetchone(self):
        if "INSERT INTO content_calendar" in self.sql:
            return {"id": 19}
        if "INSERT INTO tasks" in self.sql:
            return {"id": 44}
        if "SELECT id FROM audits" in self.sql:
            return {"id": 7} if self.audit else None
        if "FROM audits" in self.sql:
            return {"id": 7} if self.audit else None
        if "FROM content_calendar" in self.sql:
            return self.plan
        if "UPDATE content_calendar" in self.sql:
            return {"id": 19}
        if "FROM brands" in self.sql:
            return self.brand
        return self.task

    def fetchall(self):
        if "FROM content_calendar" in self.sql:
            return [self.plan] if self.plan else []
        if "FROM brands" in self.sql:
            return [{"id": 1, "name": "Brand"}]
        return []


class Conn:
    def __init__(self, cursor):
        self.cur = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


BRAND = {"id": 1, "name": "Brand", "project_id": 2, "lifecycle": "active", "state": "live"}
PLAN = {"id": 19, "brand_id": 1, "title": "Useful guide", "target_keyword": "test keyword", "status": "planned", "task_id": None, "competitor_urls": ["https://example.com"]}


class ContentCalendarTests(unittest.TestCase):
    def test_create_rejects_invalid_fields_without_database(self):
        response = dashboard.app.test_client().post("/content/calendar", data={"brand_id": "1", "title": "", "success_metric": "bad"})
        self.assertEqual(response.status_code, 400)

    def test_create_validates_evidence_audit_and_escapes_when_rendered(self):
        cursor = Cursor(brand=BRAND)
        conn = Conn(cursor)
        data = {"brand_id": "1", "title": "<Guide>", "target_keyword": "keyword", "audience": "Readers", "hypothesis": "A useful test", "success_metric": "gsc_clicks", "planned_date": "2026-09-30", "evidence_audit_id": "7", "evidence_note": "<note>"}
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar", data=data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(conn.commits, 1)
        insert = next(params for sql, params in cursor.calls if "INSERT INTO content_calendar" in sql)
        self.assertEqual(json.loads(insert[7]), [])
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("content_calendar.html", plans=[{**PLAN, "title": "<Guide>", "evidence_note": "<note>"}], brands=[], available=True, brand_filter=None)
        self.assertIn("&lt;Guide&gt;", html)
        self.assertNotIn("<Guide>", html)

    def test_research_requires_public_competitor_and_deduplicates(self):
        plan = {**PLAN, "competitor_urls": []}
        conn = Conn(Cursor(brand=BRAND, plan=plan))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 400)
        plan = {**PLAN, "task_id": 44, "competitor_urls": ["https://example.com"]}
        conn = Conn(Cursor(brand=BRAND, plan=plan))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

    def test_research_rejects_cancelled_and_unknown_or_inactive_brand(self):
        cancelled = {**PLAN, "status": "cancelled", "competitor_urls": ["https://example.com"]}
        conn = Conn(Cursor(brand=BRAND, plan=cancelled))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            self.assertEqual(dashboard.app.test_client().post("/content/calendar/19/research").status_code, 409)
        conn = Conn(Cursor(brand=None))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            self.assertEqual(dashboard.app.test_client().post("/content/calendar", data={"brand_id": "99", "title": "x", "target_keyword": "x", "audience": "x", "hypothesis": "x", "success_metric": "gsc_clicks", "planned_date": "2026-09-30"}).status_code, 404)

    def test_research_queue_and_cancel_commit(self):
        plan = {**PLAN, "competitor_urls": ["https://example.com"]}
        cursor = Cursor(brand=BRAND, plan=plan)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(conn.commits, 1)
        task_params = next(params[0] for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        self.assertEqual(json.loads(task_params)["source"], "content-calendar")

        conn = Conn(Cursor(brand=BRAND, plan=PLAN))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar/19/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conn.commits, 1)

    def test_sources_update_is_bounded_to_planned_active_plan(self):
        plan = {**PLAN, "status": "planned", "competitor_urls": []}
        cursor = Cursor(brand=BRAND, plan=plan)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().post("/content/calendar/19/sources", json={"competitor_urls": ["https://one.example/path", "https://two.example"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conn.commits, 1)
        update = next(params for sql, params in cursor.calls if "UPDATE content_calendar SET competitor_urls" in sql)
        self.assertEqual(json.loads(update[0]), ["https://one.example/path", "https://two.example"])

    def test_sources_update_rejects_invalid_url_and_queued_or_cancelled_plan(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = dashboard.app.test_client().post("/content/calendar/19/sources", json={"competitor_urls": ["http://example.com"]})
        self.assertEqual(response.status_code, 400)
        for status in ("research_queued", "cancelled"):
            conn = Conn(Cursor(brand=BRAND, plan={**PLAN, "status": status}))
            with mock.patch.object(dashboard.models, "db", return_value=conn):
                response = dashboard.app.test_client().post("/content/calendar/19/sources", json={"competitor_urls": ["https://example.com"]})
            self.assertEqual(response.status_code, 409)
    def test_missing_migration_is_graceful(self):
        conn = Conn(Cursor(raise_table=True))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = dashboard.app.test_client().get("/content/calendar")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not installed yet", response.data)


if __name__ == "__main__":
    unittest.main()
