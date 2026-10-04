import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self, rows=()):
        self.cursor_value = FakeCursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args, **kwargs):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class Studio:
    GTM_PLAYBOOKS = []

    @staticmethod
    def validate_profile(profile):
        if not isinstance(profile, dict):
            raise ValueError("profile must be an object")
        return profile

    @staticmethod
    def validate_work_item(payload):
        if not isinstance(payload, dict):
            raise ValueError("work item must be an object")
        return payload


class MarketingWorkspaceRouteTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()

    def patch_studio(self):
        return mock.patch("marketing_workspace.domain_module", return_value=Studio)

    def post(self, *args, **kwargs):
        headers = {"Origin": "http://localhost"}
        headers.update(kwargs.pop("headers", {}))
        kwargs["headers"] = headers
        return self.client.post(*args, **kwargs)

    def test_cross_origin_write_is_rejected_before_database_access(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.post(
                "/api/brands/4/profile",
                json={"revision": 0, "profile": {}},
                headers={"Origin": "https://attacker.example"},
            )
        self.assertEqual(response.status_code, 403)
        db.assert_not_called()

    def test_missing_origin_is_rejected_before_database_access(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.post(
                "/api/brands/4/profile",
                json={"revision": 0, "profile": {}},
            )
        self.assertEqual(response.status_code, 403)
        db.assert_not_called()

    def test_retired_code_onboarding_mode_is_rejected_before_database_access(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.post(
                "/onboard",
                data={"type": "existing_code_marketing", "input": "https://github.com/example/app"},
            )
        self.assertEqual(response.status_code, 410)
        db.assert_not_called()

    def test_workspace_exposes_exactly_the_seven_supported_tabs(self):
        import marketing_workspace
        self.assertEqual(
            marketing_workspace.TABS,
            ('overview', 'strategy', 'measurement', 'editorial', 'social', 'lifecycle', 'setup'),
        )
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.get("/brands/4/not-a-tab")
        self.assertEqual(response.status_code, 404)
        db.assert_not_called()

    def test_work_id_from_another_brand_is_not_visible_or_mutable(self):
        conn = FakeConnection([None])
        with self.patch_studio(), mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.post(
                "/api/brands/4/work-items/99",
                json={"revision": 1, "action": "archive"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(conn.commits, 0)
        self.assertIn("brand_id=%s", conn.cursor_value.calls[0][0])

    def test_stale_work_revision_returns_conflict_without_update(self):
        conn = FakeConnection([{
            "id": 99, "brand_id": 4, "revision": 3, "state": "draft",
            "body": "Existing copy", "brief": {},
        }])
        with self.patch_studio(), mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.post(
                "/api/brands/4/work-items/99",
                json={"revision": 2, "action": "archive"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(sql.startswith("UPDATE marketing_work_items") for sql, _ in conn.cursor_value.calls))

    def test_ready_rejects_empty_or_needs_input_draft(self):
        conn = FakeConnection([{
            "id": 99, "brand_id": 4, "revision": 1, "state": "draft",
            "body": "", "brief": json.dumps({"needs_input": ["source"]}),
        }])
        with self.patch_studio(), mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.post(
                "/api/brands/4/work-items/99",
                json={"revision": 1, "action": "ready"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any(sql.startswith("UPDATE marketing_work_items") for sql, _ in conn.cursor_value.calls))

    def test_profile_stale_revision_returns_conflict(self):
        conn = FakeConnection([{"id": 4}, {"revision": 2}])
        with self.patch_studio(), mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.post(
                "/api/brands/4/profile",
                json={"revision": 1, "profile": {"audience": "builders"}},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_malformed_json_objects_are_currently_rejected_before_database_access(self):
        with self.patch_studio():
            response = self.post("/api/brands/4/profile", json=["bad"])
            self.assertEqual(response.status_code, 400)
            response = self.post("/api/brands/4/work-items", json=["bad"])
            self.assertEqual(response.status_code, 400)
            response = self.post("/api/brands/4/work-items/99", json=["bad"])
            self.assertEqual(response.status_code, 400)

    def test_work_detail_escapes_user_content_and_keeps_brand_scoped_links(self):
        item = {
            "id": 99, "brand_id": 4, "kind": "social_post", "channel": "instagram",
            "title": "<script>alert(1)</script>", "body": "<img src=x onerror=alert(1)>",
            "brief": json.dumps({}), "state": "draft", "revision": 1,
            "task_id": None, "planned_at": None,
            "updated_at": datetime.now(timezone.utc),
        }
        conn = FakeConnection([item, {"id": 4, "name": "Brand <One>"}])
        with mock.patch.object(dashboard.models, "db", return_value=conn):
            response = self.client.get("/brands/4/work/99")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("/api/brands/4/work-items/99/export", body)
        self.assertNotIn("/api/brands/5/", body)

    def test_draft_regeneration_updates_only_rendered_fields_and_preserves_identity(self):
        scripts = str(Path(__file__).parents[2] / "agency-os" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import marketing_studio_workflow as workflow

        original = {
            "id": 99, "brand_id": 4, "kind": "social_post", "channel": "instagram",
            "title": "Launch note", "brief": {"approved_facts": "Fact"},
            "body": "Old copy", "state": "draft", "revision": 1,
            "planned_at": None,
        }
        conn = FakeConnection([original, {"id": 4, "name": "Brand"}, {"profile": {"voice": "plain"}}])
        rendered = {"body": "New copy", "brief": {"needs_input": []}}
        with mock.patch.object(workflow.marketing_studio, "render_work_item", return_value=rendered):
            result = workflow.handle(
                {"params": {"brand_id": 4, "item_id": 99, "revision": 1}},
                lambda: conn,
            )
        self.assertTrue(result["ok"])
        update_sql, update_params = next((sql, params) for sql, params in conn.cursor_value.calls if sql.startswith("UPDATE marketing_work_items"))
        self.assertIn("body=%s,brief=%s,state='draft'", update_sql)
        self.assertNotIn("title=%s", update_sql)
        self.assertEqual(update_params[0], "New copy")
        self.assertEqual(json.loads(update_params[1]), {"needs_input": []})
        self.assertEqual(conn.commits, 1)


if __name__ == "__main__":
    unittest.main()

class UploadStorageTests(unittest.TestCase):
    def test_repeat_safe_upload_and_symlink_leaf_rejection(self):
        import tempfile, os
        import marketing_workspace as workspace
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)/'brands'
            directory,fds=workspace._upload_directory_fd(base,7,9)
            try:
                self.assertTrue(workspace._store_upload_bytes(directory,'image.png',b'first'))
                self.assertFalse(workspace._store_upload_bytes(directory,'image.png',b'first'))
                with self.assertRaises(ValueError):workspace._store_upload_bytes(directory,'image.png',b'other')
                target=Path(temporary)/'outside';target.write_bytes(b'unchanged')
                (base/'7/inputs/9/link.png').symlink_to(target)
                with self.assertRaises(ValueError):workspace._store_upload_bytes(directory,'link.png',b'replace')
                self.assertEqual(target.read_bytes(),b'unchanged')
            finally:
                for fd in reversed(fds):os.close(fd)
            with mock.patch.object(workspace.os,'fchmod') as chmod:
                directory,fds=workspace._upload_directory_fd(base,7,9)
                for fd in reversed(fds):os.close(fd)
                chmod.assert_not_called()

    def test_symlink_parent_is_rejected(self):
        import tempfile
        import marketing_workspace as workspace
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)/'brands';base.mkdir()
            outside=Path(temporary)/'outside';outside.mkdir()
            (base/'7').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(OSError):workspace._upload_directory_fd(base,7,9)
