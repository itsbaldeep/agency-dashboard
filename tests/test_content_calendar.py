from browser_client import browser_client
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
import content_calendar as calendar_routes
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
        if "FROM brand_properties" in self.sql:
            return [{"property_type": "domain", "value": "https://trueapply.in"}]
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
    def test_growth_plan_refresh_queues_and_deduplicates(self):
        cursor = Cursor(brand=BRAND, task=None)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/analyze", data={"brand_id": "1"})
        self.assertEqual(response.status_code, 201)
        task_sql, task_params = next((sql, params) for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        self.assertEqual(json.loads(task_params[0])["source"], "content-calendar")
        self.assertEqual(conn.commits, 1)

        duplicate_cursor = Cursor(brand=BRAND, task={"id": 77})
        duplicate_conn = Conn(duplicate_cursor)
        with mock.patch.object(dashboard.models, "db", return_value=duplicate_conn):
            duplicate = browser_client(dashboard.app).post("/content/calendar/analyze", data={"brand_id": "1"})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["deduplicated"])

    def test_generate_fresh_outline_queues_measurement_with_growth_operator_contract(self):
        cursor = Cursor(brand=BRAND, task=None)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/generate", data={"brand_id": "1"})
        self.assertEqual(response.status_code, 201)
        task_sql, task_params = next((sql, params) for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        queued = json.loads(task_params[0])
        self.assertIn("seo_measurement", task_sql)
        self.assertEqual(queued["followup"], "growth_generate")
        self.assertTrue(queued["queue_research"])
        self.assertTrue(queued["operator_authorized"])
        self.assertTrue(queued["requires_review"])
        self.assertIn("growth_generate", response.get_json()["pipeline"])

    def test_generate_deduplicates_only_same_operator_contract(self):
        same = {"id": 81, "params": {"followup": "growth_generate", "queue_research": True, "operator_authorized": True}}
        with mock.patch.object(dashboard.models, "db", return_value=Conn(Cursor(brand=BRAND, task=same))):
            response = browser_client(dashboard.app).post("/content/calendar/generate", data={"brand_id": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

        ordinary = {"id": 82, "params": {"followup": "growth_plan"}}
        with mock.patch.object(dashboard.models, "db", return_value=Conn(Cursor(brand=BRAND, task=ordinary))):
            response = browser_client(dashboard.app).post("/content/calendar/generate", data={"brand_id": "1"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("regular SEO refresh", response.get_json()["error"])

    def test_calendar_renders_existing_help_content_and_published_link_status(self):
        class CalendarCursor(Cursor):
            def fetchall(self):
                if "content_items" in self.sql:
                    return [
                        {"id": 24, "title": "How to use the kit", "status": "draft", "content_type": "article", "structured": {"content_kind": "help"}, "updated_at": "2026-10-02"},
                        {"id": 21, "title": "Acquisition guide", "status": "outline", "content_type": "article", "structured": {"content_kind": "article", "calendar_id": 1}, "updated_at": "2026-10-01"},
                        {"id": 22, "title": "Published guide", "status": "published", "content_type": "article", "structured": {"calendar_id": 2}, "updated_at": "2026-09-30"},
                    ]
                return super().fetchall()
        plan = {**PLAN, "id": 2, "status": "research_queued"}
        cursor = CalendarCursor(brand=BRAND, plan=plan)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            with dashboard.app.test_request_context("/content/calendar?brand_id=1"):
                html = dashboard.render_template("content_calendar.html", plans=[plan], brands=[], available=True, brand_filter=1,
                                                  candidates=[], help_candidates=[], analysis_task=None,
                                                  existing_content=[
                                                      {"id": 24, "title": "How to use the kit", "status": "draft", "content_kind": "help", "updated_at": "2026-10-02"},
                                                      {"id": 21, "title": "Acquisition guide", "status": "outline", "content_kind": "article", "updated_at": "2026-10-01"},
                                                  ])
        self.assertIn("How to use the kit", html)
        self.assertIn("/content/24", html)
        self.assertIn("Acquisition guide", html)

    def test_calendar_route_keeps_existing_content_and_derives_published_status(self):
        class CalendarCursor(Cursor):
            def fetchall(self):
                if "content_items" in self.sql:
                    return [
                        {"id": 24, "title": "How to use the kit", "status": "draft", "content_type": "article", "structured": {"content_kind": "help"}, "updated_at": "2026-10-02"},
                        {"id": 22, "title": "Published guide", "status": "published", "content_type": "article", "structured": {"calendar_id": 2}, "updated_at": "2026-09-30"},
                    ]
                return super().fetchall()
        plan = {**PLAN, "id": 2, "status": "research_queued"}
        cursor = CalendarCursor(brand=BRAND, plan=plan)
        captured = {}
        def capture(template, **context):
            captured.update(context)
            return "rendered"
        with mock.patch.object(dashboard.models, "db", return_value=Conn(cursor)), mock.patch.object(calendar_routes, "render_template", side_effect=capture):
            response = browser_client(dashboard.app).get("/content/calendar?brand_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["existing_content"][0]["content_kind"], "help")
        self.assertEqual(captured["plans"][0]["display_status"], "published")

    def test_schedule_existing_outline_creates_one_linked_calendar_without_research(self):
        class ExistingCursor:
            def __init__(self): self.sql = ""; self.calls = []
            def execute(self, sql, params=()): self.sql, self.params = sql, params; self.calls.append((sql, params))
            def fetchone(self):
                if "FROM content_items" in self.sql:
                    return {"id": 27, "brand_id": 1, "title": "Existing outline", "content_type": "article", "status": "outline", "structured": {"target_keyword": "existing topic", "content_kind": "article", "research_id": 9}}
                if "FROM brands" in self.sql:
                    return BRAND
                if "FROM content_research" in self.sql:
                    return {"audit_id": "57"}
                if "INSERT INTO content_calendar" in self.sql:
                    return {"id": 70}
                return None
            def fetchall(self): return []
        cursor = ExistingCursor()
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/content/27/schedule", data={"planned_date": "2026-10-20"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()["deduplicated"])
        self.assertFalse(any("INSERT INTO tasks" in sql for sql, _ in cursor.calls))
        self.assertTrue(any("UPDATE content_items SET structured" in sql for sql, _ in cursor.calls))
        insert_params = next(params for sql, params in cursor.calls if "INSERT INTO content_calendar" in sql)
        self.assertEqual(insert_params[7], 57)

    def test_schedule_existing_linked_outline_updates_date_without_duplicate(self):
        class LinkedCursor:
            def __init__(self): self.sql = ""; self.calls = []
            def execute(self, sql, params=()): self.sql, self.params = sql, params; self.calls.append((sql, params))
            def fetchone(self):
                if "FROM content_items" in self.sql:
                    return {"id": 27, "brand_id": 1, "title": "Existing outline", "content_type": "article", "status": "outline", "structured": {"calendar_id": 70, "content_kind": "article"}}
                if "FROM brands" in self.sql:
                    return BRAND
                if "FROM content_calendar" in self.sql:
                    return {"id": 70, "brand_id": 1, "status": "planned", "task_id": None}
                return None
            def fetchall(self): return []
        cursor = LinkedCursor()
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/content/27/schedule", data={"planned_date": "2026-10-21"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])
        self.assertFalse(any("INSERT INTO content_calendar" in sql for sql, _ in cursor.calls))

    def test_schedule_existing_outline_redirects_for_normal_browser_navigation(self):
        class ExistingCursor:
            def __init__(self): self.sql = ""; self.calls = []
            def execute(self, sql, params=()): self.sql, self.params = sql, params; self.calls.append((sql, params))
            def fetchone(self):
                if "FROM content_items" in self.sql:
                    return {"id": 27, "brand_id": 1, "title": "Existing outline", "content_type": "article", "status": "outline", "structured": {"target_keyword": "existing topic", "content_kind": "article"}}
                if "FROM brands" in self.sql: return BRAND
                if "INSERT INTO content_calendar" in self.sql: return {"id": 70}
                return None
            def fetchall(self): return []
        cursor = ExistingCursor()
        with mock.patch.object(dashboard.models, "db", return_value=Conn(cursor)):
            response = browser_client(dashboard.app).post("/content/calendar/content/27/schedule", data={"planned_date": "2026-10-08"}, headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/content/calendar?brand_id=1")

    def test_research_rejects_calendar_item_already_linked_to_existing_content(self):
        class LinkedResearchCursor:
            def __init__(self): self.sql = ""; self.calls = []
            def execute(self, sql, params=()): self.sql, self.params = sql, params; self.calls.append((sql, params))
            def fetchone(self):
                if "FROM content_calendar" in self.sql:
                    return {"id": 70, "brand_id": 1, "status": "planned", "task_id": None, "title": "Existing outline", "target_keyword": "existing topic", "competitor_urls": ["https://example.com"]}
                if "FROM content_items" in self.sql:
                    return {"id": 27}
                if "FROM brands" in self.sql: return BRAND
                return None
            def fetchall(self): return []
        cursor = LinkedResearchCursor()
        with mock.patch.object(dashboard.models, "db", return_value=Conn(cursor)):
            response = browser_client(dashboard.app).post("/content/calendar/70/research")
        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", response.get_json()["error"])
        self.assertFalse(any("INSERT INTO tasks" in sql for sql, _ in cursor.calls))

    def test_recommendation_schedule_rejects_stale_entry(self):
        class RecommendationCursor:
            def execute(self, sql, params=()): self.sql = sql
            def fetchone(self):
                if "growth_recommendations" in self.sql:
                    return {"id": 5, "brand_id": 1, "status": "published"}
                return None
        conn = Conn(RecommendationCursor())
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/recommendation/5/plan", data={"planned_date": "2026-10-10"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_recommendation_schedule_rejects_three_active_items(self):
        class RecommendationCursor:
            def __init__(self): self.sql = ""
            def execute(self, sql, params=()): self.sql = sql
            def fetchone(self):
                if "growth_recommendations" in self.sql:
                    return {"id": 5, "brand_id": 1, "audit_id": 7, "kind": "article", "title": "Fresh topic", "target_keyword": "fresh topic", "rationale": "Recent evidence", "evidence": {}, "status": "suggested"}
                if "FROM brands" in self.sql: return {"id": 1, "project_id": 2, "lifecycle": "active", "state": "live"}
                if "FROM audits" in self.sql: return {"id": 7}
                if "count(*) AS total" in self.sql: return {"total": 3}
                return None
        conn = Conn(RecommendationCursor())
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/recommendation/5/plan", data={"planned_date": "2026-10-10"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("three items", response.get_json()["error"])
    def test_create_rejects_invalid_fields_without_database(self):
        response = browser_client(dashboard.app).post("/content/calendar", data={"brand_id": "1", "title": "", "success_metric": "bad"})
        self.assertEqual(response.status_code, 400)

    def test_create_validates_evidence_audit_and_escapes_when_rendered(self):
        cursor = Cursor(brand=BRAND)
        conn = Conn(cursor)
        data = {"brand_id": "1", "title": "<Guide>", "target_keyword": "keyword", "audience": "Readers", "hypothesis": "A useful test", "success_metric": "gsc_clicks", "planned_date": "2026-09-30", "evidence_audit_id": "7", "evidence_note": "<note>"}
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar", data=data)
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
            response = browser_client(dashboard.app).post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 400)
        plan = {**PLAN, "task_id": 44, "competitor_urls": ["https://example.com"]}
        conn = Conn(Cursor(brand=BRAND, plan=plan))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

    def test_research_rejects_cancelled_and_unknown_or_inactive_brand(self):
        cancelled = {**PLAN, "status": "cancelled", "competitor_urls": ["https://example.com"]}
        conn = Conn(Cursor(brand=BRAND, plan=cancelled))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            self.assertEqual(browser_client(dashboard.app).post("/content/calendar/19/research").status_code, 409)
        conn = Conn(Cursor(brand=None))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            self.assertEqual(browser_client(dashboard.app).post("/content/calendar", data={"brand_id": "99", "title": "x", "target_keyword": "x", "audience": "x", "hypothesis": "x", "success_metric": "gsc_clicks", "planned_date": "2026-09-30"}).status_code, 404)

    def test_research_queue_and_cancel_commit(self):
        plan = {**PLAN, "competitor_urls": ["https://example.com"]}
        cursor = Cursor(brand=BRAND, plan=plan)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/19/research")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(conn.commits, 1)
        task_params = next(params[0] for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        self.assertEqual(json.loads(task_params)["source"], "content-calendar")

        conn = Conn(Cursor(brand=BRAND, plan=PLAN))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/19/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conn.commits, 1)

    def test_sources_update_is_bounded_to_planned_active_plan(self):
        plan = {**PLAN, "status": "planned", "competitor_urls": []}
        cursor = Cursor(brand=BRAND, plan=plan)
        conn = Conn(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/content/calendar/19/sources", json={"competitor_urls": ["https://one.example/path", "https://two.example"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conn.commits, 1)
        update = next(params for sql, params in cursor.calls if "UPDATE content_calendar SET competitor_urls" in sql)
        self.assertEqual(json.loads(update[0]), ["https://one.example/path", "https://two.example"])

    def test_sources_update_rejects_invalid_url_and_queued_or_cancelled_plan(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = browser_client(dashboard.app).post("/content/calendar/19/sources", json={"competitor_urls": ["http://example.com"]})
        self.assertEqual(response.status_code, 400)
        for status in ("research_queued", "cancelled"):
            conn = Conn(Cursor(brand=BRAND, plan={**PLAN, "status": status}))
            with mock.patch.object(dashboard.models, "db", return_value=conn):
                response = browser_client(dashboard.app).post("/content/calendar/19/sources", json={"competitor_urls": ["https://example.com"]})
            self.assertEqual(response.status_code, 409)
    def test_missing_migration_is_graceful(self):
        conn = Conn(Cursor(raise_table=True))
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).get("/content/calendar")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not installed yet", response.data)


if __name__ == "__main__":
    unittest.main()
