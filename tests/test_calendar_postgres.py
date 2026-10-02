"""Opt-in real SQL contract test. All fixtures and queued tasks are temporary."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg2.extras

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as dashboard


@unittest.skipUnless(os.environ.get("AGENCY_CALENDAR_DB_TEST") == "1", "opt-in temporary PostgreSQL integration")
class CalendarPostgresTests(unittest.TestCase):
    def test_plan_research_and_deduplication(self):
        core = ROOT.parent / "agency-os"
        sys.path.insert(0, str(core / "scripts"))
        import worker
        conn = worker.get_conn()

        class Session:
            def cursor(self):
                return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            def commit(self):
                pass  # Keep the real connection in one rollback-only transaction.
            def rollback(self):
                pass
            def close(self):
                pass

        try:
            cur = conn.cursor()
            cur.execute("""SET LOCAL search_path=pg_temp;
                CREATE TEMP TABLE projects(id bigint PRIMARY KEY,lifecycle text,state text);
                CREATE TEMP TABLE brands(id integer PRIMARY KEY,name text,project_id bigint);
                CREATE TEMP TABLE audits(id integer PRIMARY KEY,brand_id integer);
                CREATE TEMP TABLE tasks(id serial PRIMARY KEY,type text,status text,params jsonb,triggered_by text,result_ref text);
            """)
            migration = (core / "infra/migrations/015_content_calendar.sql").read_text()
            cur.execute(migration.replace("BEGIN;", "").replace("COMMIT;", ""))
            cur.execute("""INSERT INTO projects VALUES(1,'active','live');
                INSERT INTO brands VALUES(1,'Temporary test brand',1);
                INSERT INTO audits VALUES(1,1);""")
            payload = dict(brand_id=1, title="Planning test", target_keyword="test keyword",
                           audience="test audience", hypothesis="test hypothesis", success_metric="gsc_clicks",
                           planned_date="2026-10-01", competitor_urls=["https://example.com/article"], evidence_audit_id=1)
            with patch("models.db", return_value=Session()), patch("models.get_alert_nav_count", return_value=0):
                client = dashboard.app.test_client()
                response = client.post("/content/calendar", json=payload)
                self.assertEqual(response.status_code, 201)
                plan = response.json["id"]
                self.assertEqual(client.get("/content/calendar?brand_id=1").status_code, 200)
                response = client.post(f"/content/calendar/{plan}/research")
                self.assertEqual(response.status_code, 201)
                task = response.json["task_id"]
                repeated = client.post(f"/content/calendar/{plan}/research")
                self.assertTrue(repeated.json["deduplicated"])
                self.assertEqual(repeated.json["task_id"], task)
                self.assertEqual(client.post(f"/content/calendar/{plan}/cancel").status_code, 409)
                cur.execute("SELECT count(*) FROM tasks")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.rollback()
            conn.close()
