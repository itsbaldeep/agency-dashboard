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
                CREATE TEMP TABLE growth_recommendations(
                    id integer PRIMARY KEY, brand_id integer, audit_id integer, kind text,
                    title text, target_keyword text, rank integer, rationale text,
                    evidence jsonb, status text, created_at timestamptz DEFAULT now()
                );
                CREATE TEMP TABLE capabilities(project_id bigint, capability text, status text, evidence jsonb, checked_at timestamptz);
                CREATE TEMP TABLE suggestions(
                    id integer PRIMARY KEY, brand_id integer, title text, rationale text,
                    impact integer, effort integer, action_type text, status text,
                    compliance_flags jsonb, sources jsonb, audit_id integer, created_at timestamptz
                );
            """)
            migration = (core / "infra/migrations/015_content_calendar.sql").read_text()
            cur.execute(migration.replace("BEGIN;", "").replace("COMMIT;", ""))
            cur.execute("""CREATE TEMP TABLE content_items(
                id integer PRIMARY KEY, brand_id integer, title text, content_type text,
                status text, structured jsonb, updated_at timestamptz DEFAULT now()
            );""")
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

                cur.execute("INSERT INTO content_items(id,brand_id,title,content_type,status,structured) VALUES (27,1,'Existing outline','article','outline','{\"target_keyword\":\"existing topic\",\"content_kind\":\"article\"}'::jsonb)")
                scheduled = client.post("/content/calendar/content/27/schedule", data={"planned_date": "2026-10-08"})
                self.assertEqual(scheduled.status_code, 201)
                calendar_id = scheduled.json["calendar_id"]
                cur.execute("SELECT structured->>'calendar_id' FROM content_items WHERE id=27")
                self.assertEqual(cur.fetchone()[0], str(calendar_id))
                blocked_research = client.post(f"/content/calendar/{calendar_id}/research")
                self.assertEqual(blocked_research.status_code, 409)
                repeated_schedule = client.post("/content/calendar/content/27/schedule", data={"planned_date": "2026-10-09"})
                self.assertEqual(repeated_schedule.status_code, 200)
                self.assertTrue(repeated_schedule.json["deduplicated"])
                cur.execute("SELECT count(*) FROM content_calendar WHERE id=%s", (calendar_id,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.rollback()
            conn.close()
