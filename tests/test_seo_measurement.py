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
        if "FROM audits" in self.sql and "audit_type='seo_measurement'" in self.sql:
            return self.rows.get("seo")
        if "FROM audits" in self.sql:
            return self.rows.get("ai")
        if "FROM tasks" in self.sql and "seo_measurement" in self.sql:
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


class SeoMeasurementTests(unittest.TestCase):
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
            response = dashboard.app.test_client().post("/api/brands/7/seo-measurement")
        self.assertEqual(response.status_code, 200)
        insert = next(params for sql, params in cursor.calls if "INSERT INTO tasks" in sql)
        self.assertEqual(json.loads(insert[0]), {"brand_id": 7, "project_id": 3, "url": "https://example.test",
                                                  "gsc_property": "sc-domain:example.test", "ga4_property_id": "123"})

    def test_enqueue_deduplicates_and_rejects_missing_brand(self):
        duplicate = Connection(Cursor(rows={"brand": {"id": 7, "project_id": 3}, "project": {"id": 3, "lifecycle": "active", "state": "live"}},
                                      properties=[{"property_type": "domain", "value": "example.test"}], duplicate={"id": 44}))
        with mock.patch.object(dashboard.models, "db", return_value=duplicate):
            response = dashboard.app.test_client().post("/api/brands/7/seo-measurement")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deduplicated"])

        missing = Connection(Cursor())
        with mock.patch.object(dashboard.models, "db", return_value=missing):
            response = dashboard.app.test_client().post("/api/brands/999/seo-measurement")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
