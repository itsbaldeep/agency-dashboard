import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class WorkflowRouteTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()

    def test_resume_merges_named_suggestion_inputs_into_same_task(self):
        conn = FakeConnection([{
            "id": 31,
            "type": "execute_suggestion",
            "status": "needs_input",
            "params": {"suggestion_id": 7},
        }])
        with mock.patch.object(dashboard.models, "db", return_value=conn), \
             mock.patch.object(dashboard.models, "ch_trace"):
            response = self.client.post(
                "/api/tasks/31/resume",
                json={"target_keyword": "resume automation", "competitor_urls": "https://example.com/a"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["task_id"], 31)
        update = next(params for sql, params in conn.cursor_value.calls if sql.startswith("UPDATE tasks SET params="))
        merged = json.loads(update[0])
        self.assertEqual(merged["suggestion_id"], 7)
        self.assertEqual(merged["target_keyword"], "resume automation")
        self.assertEqual(conn.commits, 1)

    def test_project_audit_uses_public_site_and_queues_full_workflow(self):
        conn = FakeConnection([
            {"id": 30, "name": "TrueApply", "lifecycle": "active", "state": "live",
             "repo_url": "https://github.com/itsbaldeep/trueapply"},
            {"id": 31},
            None,
            {"id": 501},
        ])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post(
                "/projects/30/audit",
                data={"website_url": "https://trueapply.in"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/engagements/brand/31/report", response.location)
        insert = next((params for sql, params in conn.cursor_value.calls
                       if sql.startswith("INSERT INTO tasks")), None)
        self.assertIsNotNone(insert)
        self.assertIn("marketing_audit", conn.cursor_value.calls[-1][0])
        self.assertNotIn("github.com", json.dumps(insert))

    def test_project_audit_does_not_use_repo_url_without_public_site(self):
        conn = FakeConnection([
            {"id": 30, "name": "TrueApply", "lifecycle": "active", "state": "live",
             "repo_url": "https://github.com/itsbaldeep/trueapply"},
            None,
        ])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/projects/30/audit", data={})
        self.assertEqual(response.status_code, 302)
        self.assertIn("public+site+URL", response.location)
        self.assertFalse(any("INSERT INTO tasks" in sql for sql, _ in conn.cursor_value.calls))

    def test_project_engagement_resolves_latest_linked_brand(self):
        class ModelCursor:
            def __init__(self):
                self.sql = ""
            def execute(self, sql, params=()):
                self.sql = sql
            def fetchone(self):
                if "FROM projects p" in self.sql:
                    return {"id": 30, "name": "TrueApply", "state": "live",
                            "brand_id": 31, "brand_name": "TrueApply",
                            "brand_access_tier": "0", "project_id": 30}
                return None
            def fetchall(self):
                if "brand_properties" in self.sql:
                    return [{"property_type": "domain", "value": "trueapply.in"}]
                return []
        class ModelConnection:
            def __init__(self): self.cursor_value = ModelCursor()
            def cursor(self): return self.cursor_value
            def close(self): pass
        with mock.patch.object(dashboard.models, "db", return_value=ModelConnection()), \
             mock.patch.object(dashboard.models, "get_docker_stats", return_value={}), \
             mock.patch.object(dashboard.models, "_caddy_sites", return_value={}):
            engagement = dashboard.models.get_engagement_detail("project", 30)
        self.assertEqual(engagement["brand_id"], 31)
        self.assertEqual(engagement["brand_properties"][0]["value"], "trueapply.in")

    def test_resume_refuses_unmapped_side_effect_task(self):
        conn = FakeConnection([{
            "id": 32, "type": "propose_fix", "status": "needs_input", "params": {}
        }])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/tasks/32/resume", json={"instructions": "retry"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_content_approval_resumes_existing_input_task_without_duplicate(self):
        conn = FakeConnection([
            {"id": 8, "status": "needs_publish_input", "publish_task_id": 44},
            {"id": 44, "type": "publish_content", "status": "needs_input",
             "params": {"content_item_id": 8, "destination": {}}},
        ])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post(
                "/content/8/approve",
                json={"destination_type": "wordpress", "base_url": "https://example.com",
                      "username": "publisher", "credential_ref": "WP_APP_PASSWORD"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["resumed"])
        inserts = [sql for sql, _ in conn.cursor_value.calls if sql.startswith("INSERT INTO tasks")]
        self.assertEqual(inserts, [])

    def test_alert_fragment_renders_when_snapshot_is_unavailable(self):
        with mock.patch.object(dashboard.models, "get_combined_alert_state", return_value={
            "stale": True,
            "summary": {"open_count": 1, "critical_count": 1, "clear_count": 0},
            "error": "snapshot unavailable",
        }):
            response = self.client.get("/alerts/data")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Snapshot unavailable", response.data)

    def test_alert_credential_health_has_no_rotation_action(self):
        snapshot = {
            "summary": {"open_count": 1, "critical_count": 0, "clear_count": 4},
            "backup": {"status": "clear", "offsite": {"overdue": False}},
            "credentials": [{
                "id": "core.env:DEEPSEEK_API_KEY", "name": "DEEPSEEK_API_KEY",
                "source_path": "/home/agency/.config/agency/core.env",
                "placeholder_like": False,
                "next_action": "No weak, placeholder, or unhealthy evidence detected.",
            }],
            "credential_summary": {"open": 1},
            "maintenance": {"status": "clear", "upgradable_count": 0,
                             "reboot_required": False, "commands": []},
            "root_recovery": {"status": "clear", "detail": "clear"},
            "failed_units": {"status": "clear", "units": []},
            "generated_at": "2026-08-23T00:00:00+00:00",
        }
        with mock.patch.object(dashboard.models, "get_combined_alert_state", return_value=snapshot):
            response = self.client.get("/alerts/data")
        html = response.data.decode()
        self.assertIn("No weak markers", html)
        self.assertNotIn("Mark rotated", html)

    def test_alert_credential_health_shows_known_compromise(self):
        snapshot = {
            "summary": {"open_count": 1, "critical_count": 1, "clear_count": 4},
            "backup": {"status": "clear", "offsite": {"overdue": False}},
            "credentials": [{
                "id": "core.env:POSTGRES_PASSWORD",
                "name": "POSTGRES_PASSWORD",
                "source_path": "/home/agency/.config/agency/core.env",
                "placeholder_like": False,
                "compromised_at": "2026-08-28T18:01:03+00:00",
                "next_action": "Run the controlled maintenance command.",
            }],
            "credential_summary": {"open": 1, "compromised": 1},
            "maintenance": {"status": "clear", "upgradable_count": 0,
                             "reboot_required": False, "commands": []},
            "root_recovery": {"status": "clear", "detail": "clear"},
            "failed_units": {"status": "clear", "units": []},
            "generated_at": "2026-08-28T18:01:03+00:00",
        }
        with mock.patch.object(dashboard.models, "get_combined_alert_state", return_value=snapshot):
            response = self.client.get("/alerts/data")
        self.assertIn(b"Known compromised", response.data)

    def test_alert_action_is_whitelisted_and_silent(self):
        conn = FakeConnection([None, {"id": 77}])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/alerts/actions", json={"action": "recheck_system"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["task_id"], 77)
        insert = next(params for sql, params in conn.cursor_value.calls if sql.startswith("INSERT INTO tasks"))
        self.assertEqual(json.loads(insert[0]), {"action": "recheck_system", "silent": True})
        self.assertEqual(conn.commits, 1)

    def test_mark_credential_action_is_retired(self):
        response = self.client.post(
            "/api/alerts/actions",
            json={"action": "mark_credential", "credential_id": "core.env:DEEPSEEK_API_KEY"},
        )
        self.assertEqual(response.status_code, 400)

    def test_alert_action_rejects_shell_and_unconfirmed_backup(self):
        shell = self.client.post("/api/alerts/actions", json={"action": "run_shell"})
        backup = self.client.post("/api/alerts/actions", json={"action": "mark_offsite"})
        self.assertEqual(shell.status_code, 400)
        self.assertEqual(backup.status_code, 400)

    def test_agent_trace_fragment_renders_only_redacted_summary(self):
        data = {
            "working": 1,
            "needs_human": 1,
            "worker_count": 2,
            "generated_at": "2026-08-24T00:00:00+00:00",
            "traces": [{
                "trace_id": "atr_test",
                "updated_at": "2026-08-24T00:00:00Z",
                "status": "needs_human",
                "severity": "urgent",
                "model": "gpt-5.6-sol",
                "cwd": "/home/agency",
                "workers": 2,
                "tools": 4,
                "summary": "Choose the deployment window",
                "refs": ["/home/agency/core/agency-os/ROADMAP.md"],
            }],
        }
        with mock.patch.object(dashboard.models, "get_agent_trace_view", return_value=data):
            response = self.client.get("/operations/agent-traces")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose the deployment window", response.data)
        self.assertIn(b"atr_test", response.data)

    def test_agent_alert_is_cleared_by_later_verified_result(self):
        with tempfile.TemporaryDirectory() as root:
            current_day = datetime.now(timezone.utc).date().isoformat()
            path = Path(root) / f"{current_day}.jsonl"
            rows = [
                {"ts": f"{current_day}T00:00:00Z", "trace_id": "atr_one",
                 "kind": "alert", "status": "needs_human", "severity": "urgent",
                 "summary": "Choose a window"},
                {"ts": f"{current_day}T00:01:00Z", "trace_id": "atr_one",
                 "kind": "result", "status": "verified", "severity": "info",
                 "summary": "Window confirmed"},
                {"ts": f"{current_day}T00:02:00Z", "trace_id": "atr_two",
                 "kind": "alert", "status": "needs_human", "severity": "warning",
                 "summary": "Grant property access"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with mock.patch.object(dashboard.models, "AGENT_TRACE_DIR", root):
                alerts = dashboard.models.get_agent_alerts()
        self.assertEqual([item["trace_id"] for item in alerts], ["atr_two"])

    def test_agent_alert_reads_durable_attention_index(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "attention.json"
            path.write_text(json.dumps({
                "v": 1,
                "updated_at": "2026-08-24T00:00:00.000Z",
                "alerts": {
                    "atr_old": {
                        "ts": "2020-01-01T00:00:00.000Z",
                        "trace_id": "atr_old",
                        "status": "needs_human",
                        "severity": "warning",
                        "summary": "Still needs a human after the raw trace window",
                        "refs": ["dashboard:/alerts"],
                    }
                },
            }))
            with mock.patch.object(dashboard.models, "AGENT_TRACE_DIR", root):
                alerts = dashboard.models.get_agent_alerts()
        self.assertEqual([item["trace_id"] for item in alerts], ["atr_old"])
        self.assertEqual(alerts[0]["status"], "needs_human")

    def test_invalid_attention_shape_falls_back_to_recent_traces(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "attention.json").write_text("null\n")
            with mock.patch.object(dashboard.models, "AGENT_TRACE_DIR", root), \
                 mock.patch.object(dashboard.models, "get_agent_trace_view", return_value={
                     "traces": [{"trace_id": "atr_fallback", "status": "needs_human"}]
                 }):
                alerts = dashboard.models.get_agent_alerts()
        self.assertEqual([item["trace_id"] for item in alerts], ["atr_fallback"])


if __name__ == "__main__":
    unittest.main()
