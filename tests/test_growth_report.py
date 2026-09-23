import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
