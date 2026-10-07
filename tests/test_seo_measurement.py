from browser_client import browser_client
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard


class Cursor:
    def __init__(self, rows=None, properties=None, duplicate=None):
        self.rows = rows or {}
        self.properties = properties or []
        self.duplicate = duplicate
        self.sql = ""
        self.params = None
        self.calls = []

    def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        self.calls.append((sql, params))

    def fetchone(self):
        if "FROM brands" in self.sql:
            return self.rows.get("brand")
        if "FROM projects" in self.sql:
            return self.rows.get("project")
        if "FROM brand_properties" in self.sql and "domain" in self.sql:
            return self.rows.get("domain")
        if "FROM audits" in self.sql and "audit_type='seo_measurement'" in self.sql:
            return self.rows.get("seo")
        if "FROM audits" in self.sql:
            return self.rows.get("ai")
        if "FROM tasks" in self.sql and "seo_measurement" in self.sql:
            return self.duplicate
        if "FROM tasks" in self.sql and "marketing_audit" in self.sql:
            return self.duplicate
        if "INSERT INTO tasks" in self.sql:
            return {"id": 91}
        return None

    def fetchall(self):
        if "brand_properties" in self.sql:
            return self.properties
        if "competitor_pages" in self.sql:
            return []
        if "FROM audits" in self.sql:
            return [{"id": 1, "audit_type": "defend_audit", "created_at": None,
                     "vis_pct": "12", "confidence": "normal"},
                    {"id": 2, "audit_type": "seo_measurement", "created_at": None,
                     "vis_pct": None, "confidence": None}]
        return []


class Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class AssessmentCursor:
    def __init__(self, assessment, stages):
        self.assessment = assessment
        self.stages = stages
        self.sql = ""

    def execute(self, sql, params=()):
        self.sql = sql

    def fetchone(self):
        return self.assessment if "FROM marketing_assessments" in self.sql else None

    def fetchall(self):
        return self.stages if "marketing_assessment_stages" in self.sql else []


class SeoMeasurementTests(unittest.TestCase):
    def test_assessment_loader_is_best_effort_and_includes_stage_errors(self):
        cursor = AssessmentCursor(
            {"id": 42, "status": "collecting", "source_manifest": '{"crawl": {"status": "ok"}}',
             "report": None, "validation": None, "created_at": "2026-09-14T10:00:00+00:00",
             "updated_at": "2026-09-14T10:05:00+00:00"},
            [{"stage_key": "crawl", "required": True, "status": "queued", "task_status": "failed",
              "task_error": "timeout", "updated_at": "2026-09-14T10:04:00+00:00"}],
        )
        assessment, stages = dashboard._load_latest_marketing_assessment(cursor, 7)
        self.assertEqual(assessment["id"], 42)
        self.assertEqual(assessment["source_manifest"]["crawl"]["status"], "ok")
        self.assertFalse(assessment["report_available"])
        self.assertEqual(stages[0]["status"], "failed")
        self.assertEqual(stages[0]["error"], "timeout")

    def test_assessment_loader_rejects_non_object_report_json(self):
        cursor = AssessmentCursor(
            {"id": 42, "status": "ready", "source_manifest": "[]", "report": "[]", "validation": "[]",
             "created_at": None, "updated_at": None}, [],
        )
        assessment, _stages = dashboard._load_latest_marketing_assessment(cursor, 7)
        self.assertEqual(assessment["report"], {})
        self.assertEqual(assessment["validation"], {})
        self.assertFalse(assessment["report_available"])

    def test_assessment_section_never_claims_ready_without_report(self):
        context = dict(brand={"id": 7, "name": "Brand"}, brand_properties=[], domain="example.test",
                       audit=None, audit_summary={}, audit_sources=[], audit_history=[], audit_date_fmt="",
                       suggestions=[], visibility_rows=[], ch_error=False, capabilities=[], content_items=[],
                       recent_tasks=[], content_by_suggestion={}, task_by_suggestion={}, agent_allowed=False,
                       repo_url=None, project_id=None, summary_json="", seo_audit=None, seo_summary={}, seo_data={},
                       marketing_assessment={"id": 42, "status": "ready", "report": {}, "report_available": False,
                                            "validation": {}, "created_fmt": "2026-09-14 10:00",
                                            "updated_fmt": "2026-09-14 10:05"},
                       marketing_assessment_stages=[{"stage_key": "crawl", "status": "failed", "required": True,
                                                     "updated_fmt": "2026-09-14 10:04", "error": "timeout"}])
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("brand_report.html", **context)
        self.assertIn("Assessment report pending", html)
        self.assertNotIn("Strategy</span>ready", html)
        self.assertIn("timeout", html)

    def test_assessment_section_renders_typed_actions_and_source_availability(self):
        context = dict(brand={"id": 7, "name": "Brand"}, brand_properties=[], domain="example.test",
                       audit=None, audit_summary={}, audit_sources=[], audit_history=[], audit_date_fmt="",
                       suggestions=[], visibility_rows=[], ch_error=False, capabilities=[], content_items=[],
                       recent_tasks=[], content_by_suggestion={}, task_by_suggestion={}, agent_allowed=False,
                       repo_url=None, project_id=None, summary_json="", seo_audit=None, seo_summary={}, seo_data={},
                       marketing_assessment={"id": 42, "status": "partial", "report_available": True,
                                            "validation": {"valid": True}, "created_fmt": "2026-09-14 10:00",
                                            "updated_fmt": "2026-09-14 10:05", "report": {
                                                "actions": [{"title": "Connect Search Console", "priority": "high",
                                                             "mode": "human", "detail": "Grant viewer access.",
                                                             "human_decision_required": True}],
                                                "sources": [{"label": "Search Console", "state": "not_configured",
                                                             "freshness": "unknown", "checked_at": "2026-09-14T10:00:00Z"}],
                                            }}, marketing_assessment_stages=[])
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("brand_report.html", **context)
        self.assertIn("Connect Search Console", html)
        self.assertIn("Grant viewer access.", html)
        self.assertIn("Human decision required before execution", html)
        self.assertIn("Evidence availability and freshness", html)
        self.assertIn("not_configured", html)

    def test_report_separates_ai_and_seo_latest_audits(self):
        initial = Cursor(rows={
            "brand": {"id": 7, "name": "Brand", "project_id": None},
            "ai": {"id": 1, "audit_type": "defend_audit", "summary": {"category": "AI"}, "sources": []},
            "seo": {"id": 2, "audit_type": "seo_measurement", "summary": {"counts": {"pages": 0}}, "raw_data": {"run_id": "r1"}},
        })
        empty = Connection(Cursor())
        with mock.patch.object(dashboard.models, "db", side_effect=[Connection(initial), empty, empty]), \
             mock.patch.object(dashboard.models, "ch_query", return_value=([], [])), \
             mock.patch.object(dashboard, "render_template", return_value="ok") as render:
            response = dashboard.brand_report(7)
        self.assertEqual(response, "ok")
        context = render.call_args.kwargs
        self.assertEqual(context["audit_summary"]["category"], "AI")
        self.assertEqual(context["seo_data"]["run_id"], "r1")

    def test_measurement_template_preserves_zero_and_unavailable(self):
        context = dict(brand={"id": 7, "name": "Brand"}, brand_properties=[], domain="example.test",
                       audit=None, audit_summary={}, audit_sources=[], audit_history=[], audit_date_fmt="",
                       suggestions=[], visibility_rows=[], ch_error=False, capabilities=[], content_items=[],
                       recent_tasks=[], content_by_suggestion={}, task_by_suggestion={}, agent_allowed=False,
                       repo_url=None, project_id=None, summary_json="", seo_audit={"created_at": "now"},
                       seo_summary={"counts": {"pages": 0, "broken_links": 0, "findings": 0}, "comparison": {"finding_ids_added": [], "finding_ids_resolved": []}},
                       seo_data={"run_id": "r0", "sources": {"crawl": {"status": "available"}, "pagespeed": {"status": "unavailable"},
                         "gsc": {"status": "unavailable"}, "ga4": {"status": "unavailable"}}})
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("brand_report.html", **context)
        self.assertIn("Pages</span>0", html)
        self.assertIn("+0 added", html)
        self.assertIn("PageSpeed</span>unavailable", html)
        self.assertIn("GSC</span>unavailable", html)

    def test_enqueue_validates_and_includes_stored_params(self):
        cursor = Cursor(rows={"brand": {"id": 7, "project_id": 3}, "project": {"id": 3, "lifecycle": "active", "state": "live"}},
                        properties=[{"property_type": "domain", "value": "Example.test"},
                                    {"property_type": "gsc_property", "value": "sc-domain:example.test"},
                                    {"property_type": "ga4_property_id", "value": "123"}])
        conn = Connection(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/api/brands/7/seo-measurement")
        self.assertEqual(response.status_code, 200)
        insert = next(params for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        self.assertEqual(json.loads(insert[0]), {"brand_id": 7, "project_id": 3, "url": "https://example.test",
                                                  "gsc_property": "sc-domain:example.test", "ga4_property_id": "123"})

    def test_enqueue_deduplicates_and_rejects_missing_brand(self):
        duplicate = Connection(Cursor(rows={"brand": {"id": 7, "project_id": 3}, "project": {"id": 3, "lifecycle": "active", "state": "live"}},
                                      properties=[{"property_type": "domain", "value": "example.test"}], duplicate={"id": 44}))
        with mock.patch.object(dashboard.models, "db", return_value=duplicate):
            response = browser_client(dashboard.app).post("/api/brands/7/seo-measurement")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

        missing = Connection(Cursor())
        with mock.patch.object(dashboard.models, "db", return_value=missing):
            response = browser_client(dashboard.app).post("/api/brands/999/seo-measurement")
        self.assertEqual(response.status_code, 404)

    def test_measurement_setup_validates_and_saves_non_secret_ids(self):
        cursor = Cursor(rows={"brand": {"id": 7, "project_id": 3},
                              "project": {"id": 3, "lifecycle": "active"}})
        conn = Connection(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post(
                "/api/brands/7/measurement-setup",
                data={"gsc_property": "sc-domain:TrueApply.in",
                      "ga4_property_id": "553391253",
                      "ga4_measurement_id": "g-2gw0337cjv"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["setup"], {
            "gsc_property": "sc-domain:trueapply.in",
            "ga4_property_id": "553391253",
            "ga4_measurement_id": "G-2GW0337CJV",
        })
        self.assertEqual(conn.commits, 1)

    def test_measurement_setup_rejects_non_string_json_values(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = browser_client(dashboard.app).post(
                "/api/brands/7/measurement-setup",
                json={"gsc_property": 123, "ga4_property_id": "553391253", "ga4_measurement_id": "G-2GW0337CJV"},
            )
        self.assertEqual(response.status_code, 400)
        db.assert_not_called()

    def test_full_audit_enqueues_parent_and_deduplicates(self):
        cursor = Cursor(rows={"brand": {"id": 7, "project_id": 3},
                              "project": {"id": 3, "lifecycle": "active", "state": "live"},
                              "domain": {"value": "trueapply.in"}}, duplicate={"id": 77})
        conn = Connection(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/api/brands/7/full-audit")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

    def test_full_audit_requires_public_site_not_repository(self):
        cursor = Cursor(rows={"brand": {"id": 7, "project_id": 3},
                              "project": {"id": 3, "lifecycle": "active", "state": "live"}})
        conn = Connection(cursor)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = browser_client(dashboard.app).post("/api/brands/7/full-audit")
        self.assertEqual(response.status_code, 400)
        self.assertIn("public site URL", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
