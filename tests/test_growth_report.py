from browser_client import browser_client
import os
import sys
import unittest
import importlib.util
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard


def growth_fixture():
    return {
        "schema_version": 1,
        "status": "available",
        "captured_at": "2026-09-19T10:00:00+00:00",
        "windows": {
            "current": {"start_date": "2026-08-19", "end_date": "2026-09-15", "days": 28},
            "previous": {"start_date": "2026-07-22", "end_date": "2026-08-18", "days": 28},
        },
        "sources": {
            "gsc": {
                "state": "available",
                "status": "available",
                "windows": {
                    "current": {"status": "available", "metrics": {"impressions": 120, "clicks": 6}},
                    "previous": {"status": "available", "metrics": {"impressions": 100, "clicks": 4}},
                },
                "comparison": {"impressions": {"trend": "up", "percent": 20.0}},
            },
            "ga4": {
                "state": "available",
                "status": "available",
                "windows": {
                    "current": {"status": "available", "metrics": {"totalUsers": 12, "sessions": 15, "keyEvents": 0}},
                    "previous": {"status": "available", "metrics": {"totalUsers": 8, "sessions": 10, "keyEvents": 0}},
                },
                "comparison": {"totalUsers": {"trend": "up", "percent": 50.0}},
            },
        },
        "confidence": "available",
        "recommendations": [{"id": "growth-next", "reason": "Observed users remain sparse", "action": "Run a distribution test"}],
    }


class GrowthReportTests(unittest.TestCase):
    def render(self, data):
        with dashboard.app.test_request_context("/"):
            return dashboard.render_template("fragments/growth_report.html", growth_report=data)

    def test_contract_renders_windows_metrics_trend_followup_and_freshness(self):
        report = dashboard._normalise_growth_report({"captured_at": "fallback", "growth": growth_fixture()})
        html = self.render(report)
        for value in ("2026-08-19", "2026-09-15", "120", "6", "12", "15", "Observed users remain sparse", "2026-09-19T10:00:00+00:00"):
            self.assertIn(value, html)
        self.assertIn("GSC Impressions", html)
        self.assertIn("up (20.0%)", html)
        self.assertIn("not confirmed signups", html)
        self.assertIn("not necessarily leads", html)

    def test_zero_is_displayed_and_unavailable_is_not_zero(self):
        data = growth_fixture()
        data["sources"]["gsc"]["windows"]["current"]["metrics"] = {"impressions": 0, "clicks": 0}
        data["sources"]["ga4"]["windows"]["current"] = {"status": "source_unavailable", "metrics": {"totalUsers": 99}}
        report = dashboard._normalise_growth_report({"growth": data})
        html = self.render(report)
        self.assertIn("<td>0</td>", html)
        self.assertIn("<span class=\"subtle\">unavailable</span>", html)

    def test_overlapping_or_short_windows_do_not_claim_a_trend(self):
        data = growth_fixture()
        data["windows"]["current"]["start_date"] = "2026-08-18"
        report = dashboard._normalise_growth_report({"growth": data})
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("valid 28-day", report["reason"])

    def test_missing_or_malformed_growth_is_explicitly_unavailable(self):
        for raw in ({}, {"growth": []}, {"growth": {"schema_version": 2}}):
            report = dashboard._normalise_growth_report(raw)
            html = self.render(report)
            self.assertEqual(report["status"], "unavailable")
            self.assertIn("Run SEO measurement to collect comparable growth data", html)

    def test_escaped_contract_text_is_not_interpreted_as_markup(self):
        data = growth_fixture()
        data["recommendations"] = [{"reason": "<script>alert(1)</script>", "action": "<b>safe text</b>"}]
        report = dashboard._normalise_growth_report({"growth": data})
        html = self.render(report)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_malformed_source_cannot_crash_report(self):
        data = growth_fixture()
        data["sources"]["ga4"] = ["invalid"]
        report = dashboard._normalise_growth_report({"growth": data})
        self.assertEqual(report["comparison"]["status"], "partial")
        self.assertIsNone(report["current"]["ga4"]["users"])
        self.render(report)

    def test_deltas_are_recomputed_not_trusted(self):
        data = growth_fixture()
        data["sources"]["gsc"]["comparison"] = {"impressions": {"trend": "down", "percent": "NaN"}}
        report = dashboard._normalise_growth_report({"growth": data})
        delta = report["comparison"]["deltas"]["gsc"]["impressions"]
        self.assertEqual(delta, {"trend": "up", "percent": 20.0})

    def test_gap_between_windows_is_rejected(self):
        data = growth_fixture()
        data["windows"]["previous"] = {"start_date": "2026-06-01", "end_date": "2026-06-28"}
        self.assertEqual(dashboard._normalise_growth_report({"growth": data})["status"], "unavailable")

    def test_missing_history_is_not_an_access_failure(self):
        data = growth_fixture()
        data["sources"]["ga4"]["state"] = "historical_unavailable"
        data["sources"]["ga4"]["windows"]["previous"] = {"status": "source_unavailable"}
        report = dashboard._normalise_growth_report({"growth": data})
        self.assertEqual(report["current"]["ga4"]["users"], 12)
        self.assertEqual(report["comparison"]["status"], "partial")
        self.assertIn("historical coverage is incomplete", self.render(report))

    def test_activation_contract_preserves_zeroes_and_attribution_coverage(self):
        raw = {"captured_at": "2026-10-01T10:00:00Z", "activation": {
            "schema_version": 1, "status": "available",
            "window": {"days": 28, "start": "2026-09-01", "end": "2026-09-28"},
            "totals": {"signups": 2, "resume_processed": 1, "profile_confirmed": 1, "job_selected": 0, "kit_completed": 0, "kit_evidence_only": 0, "download_served": 0},
            "signup_cohort_totals": {"signups": 2, "resume_processed": 1, "profile_confirmed": 1, "kit_started": 1, "kit_completed": 0, "kit_evidence_only": 0, "download_served": 0},
            "cohorts": [{"source": "google", "medium": "organic", "campaign": "", "landing_path": "/blog/a", "signups": 1, "resume_processed": 1, "profile_confirmed": 1, "kit_started": 1, "kit_completed": 0, "kit_evidence_only": 0, "download_served": 0}],
            "coverage": {"consented_signups": 2, "unattributed_signups": 1},
            "health": {"status": "healthy", "last_event_at": "2026-10-01T09:00:00Z"},
        }}
        report = dashboard._normalise_activation_report(raw)
        self.assertEqual(report["totals"]["job_selected"], 0)
        self.assertEqual(report["cohorts"][0]["landing_path"], "/blog/a")
        self.assertEqual(report["coverage"]["unattributed_signups"], 1)
        self.assertEqual(report["signup_cohort_totals"]["kit_started"], 1)
        self.assertEqual(report["cohorts"][0]["profile_confirmed"], 1)

    def test_activation_malformed_available_snapshot_is_unavailable(self):
        report = dashboard._normalise_activation_report({"activation": {"schema_version": 1, "status": "available", "totals": {}}})
        self.assertEqual(report["status"], "unavailable")

    def test_activation_retention_counts_survive_normalisation_without_payloads(self):
        raw = {"activation": {
            "schema_version": 1, "status": "available",
            "totals": {key: 0 for key in ["signups", "resume_processed", "profile_confirmed", "job_selected", "kit_completed", "kit_evidence_only", "download_served"]},
            "retention": {"status": "available", "counts": {"notifications_generated": 4, "notifications_read": 2, "notifications_clicked": 1, "unread_notifications": 2, "active_watchlist_jobs": 3, "active_saved_searches": 1, "digest_previews": 2, "email_blocked": 5, "email_failed": 0, "recipients": ["private@example.test"]}, "email_status": "not_connected"},
        }}
        report = dashboard._normalise_activation_report(raw)
        self.assertEqual(report["retention"]["counts"]["notifications_generated"], 4)
        self.assertEqual(report["retention"]["email_status"], "not_connected")
        self.assertNotIn("recipients", report["retention"])

    def test_activation_missing_retention_remains_backward_compatible(self):
        raw = {"activation": {"schema_version": 1, "status": "available", "totals": {key: 0 for key in ["signups", "resume_processed", "profile_confirmed", "job_selected", "kit_completed", "kit_evidence_only", "download_served"]}}}
        report = dashboard._normalise_activation_report(raw)
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["retention"]["status"], "unavailable")

    def test_activation_schema2_preserves_project_stages_and_aggregate_counts(self):
        raw = {"captured_at": "2026-10-05T10:00:00Z", "activation": {
            "schema_version": 2, "status": "available",
            "stage_totals": {"enquiry_received": 0},
            "stage_labels": {"enquiry_received": "Enquiries received"},
            "aggregate_counts": {"internal_alerts_confirmed": 0},
            "window": {"days": 28, "start": "2026-09-08", "end": "2026-10-05"},
            "coverage": {}, "health": {"status": "available", "last_event_at": "2026-10-05T09:00:00Z"},
        }}
        report = dashboard._normalise_activation_report(raw)
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["stage_totals"], {"enquiry_received": 0})
        self.assertEqual(report["aggregate_counts"], {"internal_alerts_confirmed": 0})
        self.assertEqual(report["totals"], {"enquiry_received": 0})
        self.assertNotIn("signups", report["totals"])
        self.assertEqual(report["coverage"], {})

    def test_activation_schema2_accepts_aggregate_only_and_rejects_malformed_values(self):
        base = {"schema_version": 2, "status": "available", "stage_totals": {}, "stage_labels": {},
                "aggregate_counts": {"orders": 3}}
        report = dashboard._normalise_activation_report({"activation": base})
        self.assertEqual(report["aggregate_counts"], {"orders": 3})
        for invalid in (
            {**base, "aggregate_counts": {"orders": -1}},
            {**base, "aggregate_counts": {"orders": True}},
            {**base, "stage_labels": {"unknown": "bad\x7flabel"}},
            {**base, "status": "mystery"},
            {**base, "status": []},
            {**base, "health": {"status": "available\n"}},
            {**base, "health": {"status": "bad\x7fstatus"}},
            {**base, "health": {"last_event_at": "bad\x7ftime"}},
            {**base, "window": {"days": 0}},
            {**base, "window": {"start": "bad\x7fdate"}},
        ):
            self.assertEqual(dashboard._normalise_activation_report({"activation": invalid})["status"], "unavailable")
        compatible = dashboard._normalise_activation_report({"activation": {
            **base, "stage_labels": {"unknown": "Future source label"}}})
        self.assertEqual(compatible["status"], "available")

    def test_schema2_brand_report_uses_generic_labels_without_signup_claims(self):
        raw = {"captured_at": "2026-10-05T10:00:00Z", "activation": {
            "schema_version": 2, "status": "available", "stage_totals": {"enquiry_received": 1},
            "stage_labels": {"enquiry_received": "Enquiries received"},
            "aggregate_counts": {"internal_alerts_confirmed": 1}, "coverage": {},
            "health": {"status": "available", "last_event_at": "2026-10-05T09:00:00Z"},
            "window": {"days": 28, "start": "2026-09-08", "end": "2026-10-05"}}}
        report = dashboard._normalise_activation_report(raw)
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template(
                "brand_report.html", brand={"id": 31, "name": "Synthetic"}, activation_report=report,
                audit_summary={}, audit_history=[], domain="", audit_date_fmt="", capabilities=[],
                full_audit_run=None, full_audit_children=[], marketing_assessment=None,
                marketing_assessment_stages=[], brand_properties=[], competitors=[], audit=None,
                seo_audit=None, seo_summary={}, seo_data={}, suggestions=[], visibility_rows=[],
                ch_error=False, content_items=[], recent_tasks=[], content_by_suggestion={},
                task_by_suggestion={}, agent_allowed=False, repo_url=None, project_id=None,
                measurement_setup={}, seo_cleanup_groups=[], seo_cleanup_batch=None,
            )
        section = html.split('id="acquisition-activation-report"', 1)[1]
        self.assertIn("Enquiries received", section)
        self.assertIn("Internal Alerts Confirmed", html)
        self.assertNotIn("aggregate, consented", section)
        self.assertNotIn("Signups", section)

    def test_schema2_lifecycle_view_uses_project_counts_without_funnel_language(self):
        report = dashboard._normalise_activation_report({"activation": {
            "schema_version": 2, "status": "available", "stage_totals": {"enquiry_received": 1},
            "stage_labels": {"enquiry_received": "Enquiries received"},
            "aggregate_counts": {"internal_alerts_confirmed": 1}}})
        context = {"brand": {"id": 31, "name": "Synthetic"}, "profile": {}, "profile_revision": 0,
                   "properties": {}, "tab": "lifecycle", "tabs": (), "items": [], "audits": [],
                   "content_items": [], "suggestions": [], "tasks": [], "seo": None, "evidence": {},
                   "sources": {}, "growth_report": {}, "activation_report": report, "plays": {},
                   "channels": {}, "schedule_enabled": False, "enquiry_source_configured": False}
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("marketing_workspace.html", **context)
        self.assertIn("Enquiries received", html)
        self.assertIn("Internal Alerts Confirmed", html)
        self.assertIn("do not establish signups", html)

    def test_core_collector_contract_reaches_dashboard_without_evidence_loss(self):
        scripts = Path(os.environ.get("AGENCY_SCRIPT_DIR", "/home/agency/core/agency-os/scripts"))
        sys.path.insert(0, str(scripts))
        try:
            spec = importlib.util.spec_from_file_location("seo_measurement_contract", scripts / "seo_measurement.py")
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            collected = module.normalize_activation({
                "totals": {key: 0 for key in ["signups", "resume_processed", "profile_confirmed", "job_selected", "kit_completed", "kit_evidence_only", "download_served"]},
                "retention": {"notifications_generated": 7, "notifications_read": 3, "email_status": "not_connected"},
            })
        finally:
            sys.path.pop(0)
        report = dashboard._normalise_activation_report({"activation": collected})
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["totals"]["kit_evidence_only"], 0)
        self.assertEqual(report["retention"]["counts"]["notifications_generated"], 7)
        self.assertEqual(report["retention"]["email_status"], "not_connected")

    def test_seo_cleanup_groups_same_rule_and_url(self):
        groups = dashboard._seo_cleanup_groups([
            {"evidence_id": "a", "rule": "missing_description", "url": "https://x.test/a"},
            {"evidence_id": "b", "rule": "missing_description", "url": "https://x.test/a"},
            {"evidence_id": "c", "rule": "canonical_mismatch", "url": "https://x.test/a"},
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["finding_ids"], ["a", "b"])

    def test_populated_cleanup_groups_render_urls_evidence_and_before_after(self):
        groups = [{
            "rule": "missing_description",
            "items": [{
                "url": "https://trueapply.in/guide",
                "evidence_id": "seo-123",
                "precondition": {"observed": "missing"},
                "expected": "Add a unique meta description",
            }],
        }]
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template(
                "brand_report.html",
                brand={"id": 31, "name": "TrueApply"},
                audit_summary={}, audit_history=[], domain="", audit_date_fmt="", capabilities=[],
                full_audit_run=None, full_audit_children=[], marketing_assessment=None,
                marketing_assessment_stages=[], brand_properties=[], competitors=[], audit=None,
                seo_audit=None, seo_summary={}, seo_data={}, suggestions=[], visibility_rows=[],
                ch_error=False, content_items=[], recent_tasks=[], content_by_suggestion={},
                task_by_suggestion={}, agent_allowed=False, repo_url=None, project_id=None,
                measurement_setup={},
                seo_cleanup_groups=groups,
                seo_cleanup_batch={"id": 4, "status": "proposed", "plan_hash": "hash-1", "summary": {}},
            )
        self.assertIn("https://trueapply.in/guide", html)
        self.assertIn("seo-123", html)
        self.assertIn("missing / Add a unique meta description", html)

    def test_cleanup_approval_requires_current_batch_and_queues_apply_task(self):
        class Cursor:
            def __init__(self, stale=False): self.sql = ''; self.calls = []; self.stale = stale
            def execute(self, sql, params=()): self.sql = sql; self.calls.append((sql, params))
            def fetchone(self):
                if 'FROM brands' in self.sql: return {'id': 31, 'project_id': 30}
                if 'FROM audits' in self.sql: return {'id': 9} if self.stale else {'id': 7}
                if 'FROM seo_cleanup_batches' in self.sql: return {'id': 4, 'audit_id': 7, 'plan_hash': 'hash-1', 'status': 'proposed', 'revision': 'run-7', 'summary': {}}
                if "type='seo_cleanup'" in self.sql: return None
                if 'INSERT INTO tasks' in self.sql: return {'id': 88}
                return None
        class Conn:
            def __init__(self, cursor): self.cursor_value = cursor; self.commits = 0
            def cursor(self): return self.cursor_value
            def commit(self): self.commits += 1
            def rollback(self): pass
            def close(self): pass
        import types
        publication = types.ModuleType('publication_settings')
        publication.project_destination = lambda project_id: {'type': 'ghost', 'credential_path': '/run/trueapply/ghost.json', 'base_url': 'https://trueapply.in/blog'}
        with mock.patch.dict(sys.modules, {'publication_settings': publication}):
            cursor, conn = Cursor(), None
            conn = Conn(cursor)
            with mock.patch.object(dashboard.models, 'db', return_value=conn):
                response = browser_client(dashboard.app).post('/api/brands/31/seo-cleanup/approve', json={'batch_id': 4, 'plan_hash': 'hash-1'})
            self.assertEqual(response.status_code, 201)
            body = response.get_json()
            self.assertEqual(body['task_id'], 88)
            task_params = next(params[0] for sql, params in cursor.calls if 'INSERT INTO tasks' in sql)
            self.assertEqual(__import__('json').loads(task_params)['phase'], 'apply')
            self.assertEqual(conn.commits, 1)

            stale_cursor, stale_conn = Cursor(stale=True), None
            stale_conn = Conn(stale_cursor)
            with mock.patch.object(dashboard.models, 'db', return_value=stale_conn):
                stale = browser_client(dashboard.app).post('/api/brands/31/seo-cleanup/approve', json={'batch_id': 4, 'plan_hash': 'hash-1'})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale_conn.commits, 0)

    def test_legacy_direct_generation_is_retired(self):
        response = browser_client(dashboard.app).post('/api/suggestions/17/generate')
        self.assertEqual(response.status_code, 410)
        self.assertIn('research plan', response.get_json()['error'])


if __name__ == "__main__":
    unittest.main()
