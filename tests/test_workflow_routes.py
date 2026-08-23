import json
import sys
import unittest
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
        with mock.patch.object(dashboard.models, "get_alert_state", return_value={
            "stale": True,
            "summary": {"open_count": 1, "critical_count": 1, "clear_count": 0},
            "error": "snapshot unavailable",
        }):
            response = self.client.get("/alerts/data")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Snapshot unavailable", response.data)

    def test_alert_credential_button_uses_safe_data_attribute(self):
        snapshot = {
            "summary": {"open_count": 1, "critical_count": 0, "clear_count": 4},
            "backup": {"status": "clear", "offsite": {"overdue": False}},
            "credentials": [{
                "id": "core.env:DEEPSEEK_API_KEY", "name": "DEEPSEEK_API_KEY",
                "source_path": "/home/agency/.config/agency/core.env",
                "placeholder_like": False, "human_rotated_at": None,
                "next_action": "Acknowledge after human rotation.",
            }],
            "credential_summary": {"open": 1},
            "maintenance": {"status": "clear", "upgradable_count": 0,
                             "reboot_required": False, "commands": []},
            "root_recovery": {"status": "clear", "detail": "clear"},
            "failed_units": {"status": "clear", "units": []},
            "generated_at": "2026-08-23T00:00:00+00:00",
        }
        with mock.patch.object(dashboard.models, "get_alert_state", return_value=snapshot):
            response = self.client.get("/alerts/data")
        html = response.data.decode()
        self.assertIn('data-credential-id="core.env:DEEPSEEK_API_KEY"', html)
        self.assertNotIn('credential_id:"core.env:DEEPSEEK_API_KEY"', html)

    def test_alert_action_is_whitelisted_and_silent(self):
        conn = FakeConnection([None, {"id": 77}])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/alerts/actions", json={"action": "recheck_system"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["task_id"], 77)
        insert = next(params for sql, params in conn.cursor_value.calls if sql.startswith("INSERT INTO tasks"))
        self.assertEqual(json.loads(insert[0]), {"action": "recheck_system", "silent": True})
        self.assertEqual(conn.commits, 1)

    def test_mark_credential_action_queues_the_safe_identifier(self):
        conn = FakeConnection([None, {"id": 78}])
        snapshot = {"credentials": [{"id": "core.env:DEEPSEEK_API_KEY"}]}
        with mock.patch.object(dashboard.models, "db", return_value=conn), \
             mock.patch.object(dashboard.models, "get_alert_state", return_value=snapshot):
            response = self.client.post(
                "/api/alerts/actions",
                json={"action": "mark_credential", "credential_id": "core.env:DEEPSEEK_API_KEY"},
            )
        self.assertEqual(response.status_code, 200)
        insert = next(params for sql, params in conn.cursor_value.calls if sql.startswith("INSERT INTO tasks"))
        self.assertEqual(json.loads(insert[0]), {
            "action": "mark_credential", "credential_id": "core.env:DEEPSEEK_API_KEY", "silent": True,
        })

    def test_alert_action_rejects_shell_and_unconfirmed_backup(self):
        shell = self.client.post("/api/alerts/actions", json={"action": "run_shell"})
        backup = self.client.post("/api/alerts/actions", json={"action": "mark_offsite"})
        self.assertEqual(shell.status_code, 400)
        self.assertEqual(backup.status_code, 400)


if __name__ == "__main__":
    unittest.main()
