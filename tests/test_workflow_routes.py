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


if __name__ == "__main__":
    unittest.main()
