import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
import marketing_connections


class Cursor:
    def __init__(self, brand, config=None, task_id=41):
        self.brand = brand
        self.config = config
        self.task_id = task_id
        self.calls = []
        self.last = None

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "FROM brands b" in sql:
            self.last = self.brand
        elif "FROM brand_properties" in sql:
            self.last = {"value": json.dumps(self.config or {})} if self.config is not None else None
        elif "RETURNING id" in sql:
            self.last = {"id": self.task_id}
        else:
            self.last = None

    def fetchone(self):
        value, self.last = self.last, None
        return value


class Connection:
    def __init__(self, brand, config=None):
        self.cursor_value = Cursor(brand, config)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): pass


class MarketingConnectionTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()
        self.brand = {"id": 7, "project_id": 12, "local_path": "/home/agency/engagements/example", "classification": "engagement", "lifecycle": "active"}

    def test_get_preloads_only_safe_config_and_digest(self):
        config = {"base_url": "http://127.0.0.1:3100", "endpoint": "/marketing/summary", "credential_ref": "/home/agency/engagements/example/.env", "credential_name": "MARKETING_READ_TOKEN"}
        conn = Connection(self.brand, config)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.get("/api/brands/7/marketing-connection")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["config"], config)
        self.assertNotIn("secret-value", response.get_data(as_text=True).lower())

    def test_cross_origin_write_and_stale_digest_are_rejected(self):
        conn = Connection(self.brand, {})
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/brands/7/marketing-connection", json={"digest": "bad"}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/brands/7/marketing-connection", json={"digest": "bad"}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_localhost_validation_handles_ipv6_bad_ports_and_body_limit(self):
        self.assertEqual(marketing_connections._localhost("http://[::1]:3100"), "http://[::1]:3100")
        self.assertIsNone(marketing_connections._localhost("http://[::1]:not-a-port"))
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.post("/api/brands/7/marketing-connection", data=b"x" * 100001,
                                        content_type="application/json", headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 413)
        db.assert_not_called()

    def test_malformed_and_cross_root_references_are_rejected(self):
        conn = Connection(self.brand, {})
        base = {"digest": marketing_connections._digest({}), "base_url": "http://127.0.0.1:3100", "endpoint": "/marketing/summary", "credential_name": "MARKETING_READ_TOKEN"}
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/brands/7/marketing-connection", json={**base, "credential_ref": "/etc/passwd"}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 400)
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.post("/api/brands/7/marketing-connection", json={**base, "credential_ref": "/home/agency/engagements/example/.env", "credential_name": "bad-name"}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 400)

    def test_save_persists_safe_config_and_completed_setup_task(self):
        with tempfile.TemporaryDirectory(dir="/home/agency/engagements") as root:
            brand = {**self.brand, "local_path": root}
            conn = Connection(brand, {})
            payload = {"digest": marketing_connections._digest({}), "base_url": "http://127.0.0.1:3100", "endpoint": "/marketing/summary", "credential_ref": str(Path(root) / ".env"), "credential_name": "MARKETING_READ_TOKEN"}
            with mock.patch.object(dashboard.models, "db", return_value=conn):
                response = self.client.post("/api/brands/7/marketing-connection", json=payload, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(conn.commits, 1)
        sql = " ".join(call[0] for call in conn.cursor_value.calls)
        self.assertIn("FOR UPDATE OF b", sql)
        self.assertIn("activation_config", sql)
        self.assertIn("marketing_connection_setup", sql)
        self.assertNotIn("secret", response.get_data(as_text=True).lower())

    def test_paused_project_is_rejected(self):
        conn = Connection({**self.brand, "lifecycle": "soft_parked"}, {})
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.get("/api/brands/7/marketing-connection")
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
