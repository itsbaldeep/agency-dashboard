import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
import content_asset_routes as routes
import content_visuals


def draft(status="draft", brand_id=4):
    return {
        "id": 22,
        "brand_id": brand_id,
        "title": "Example",
        "status": status,
        "lifecycle": "active",
        "content_blocks": [{"type": "prose", "markdown": "A complete article."}],
        "body": "A complete article.",
        "structured": {"facts": []},
        "updated_at": datetime(2026, 9, 26, tzinfo=timezone.utc),
    }


class FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return copy.deepcopy(self.rows.pop(0)) if self.rows else None

    def fetchall(self):
        return []

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class ContentAssetRouteTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()

    def test_upload_and_attach_require_current_revision(self):
        conn = FakeConnection([draft()])
        with mock.patch.object(routes.models, "db", return_value=conn):
            response = self.client.post(
                "/content/22/assets/upload",
                data={"revision": "stale", "description": "A useful diagram", "rights": "owned", "creator": "Agency"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Draft changed", response.get_json()["error"])
        self.assertEqual(conn.commits, 0)

    def test_cross_site_asset_mutations_are_rejected_before_storage(self):
        response = self.client.post(
            "/content/22/assets/upload",
            data={"revision": "anything", "description": "A useful diagram", "rights": "owned", "creator": "Agency"},
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Cross-site", response.get_json()["error"])

    def test_cross_brand_asset_cannot_be_attached(self):
        item = draft(brand_id=4)
        conn = FakeConnection([item, None])
        with mock.patch.object(routes.models, "db", return_value=conn):
            response = self.client.post(
                "/content/22/assets/attach",
                json={"revision": content_visuals.revision(item), "reviewed": True,
                      "asset_id": 88, "alt": "A sufficiently descriptive diagram", "index": 1},
            )
        self.assertEqual(response.status_code, 404)
        self.assertIn("Asset not found", response.get_json()["error"])
        self.assertEqual(conn.commits, 0)

    def test_published_content_cannot_use_asset_editor(self):
        conn = FakeConnection([draft(status="published")])
        with mock.patch.object(routes.models, "db", return_value=conn):
            response = self.client.get("/content/22/assets")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Only composed drafts", response.get_json()["error"])

    def test_search_requires_bounded_query_and_keeps_external_search_mocked(self):
        conn = FakeConnection([draft(), draft()])
        with mock.patch.object(routes.models, "db", return_value=conn), \
             mock.patch.object(routes, "module") as asset_module:
            asset_module.return_value.search_assets.return_value = []
            short = self.client.get("/content/22/assets/search?q=ab")
            self.assertEqual(short.status_code, 400)
            good = self.client.get("/content/22/assets/search?q=resume%20diagram")
        self.assertEqual(good.status_code, 200)
        asset_module.return_value.search_assets.assert_called_once_with("resume diagram")

    def test_suggestion_get_is_read_only(self):
        item = draft()
        conn = FakeConnection([item, None])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.get('/content/22/assets/suggestions')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['slots'], [])
        self.assertEqual(conn.commits, 0)
        self.assertTrue(all(sql.lstrip().startswith('SELECT') for sql, _ in conn.calls))

    def test_suggestion_refresh_is_revision_bound(self):
        conn = FakeConnection([draft(), None])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.post('/content/22/assets/suggestions', json={'revision': 'stale'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_suggestion_refresh_deduplicates_running_task(self):
        item = draft()
        conn = FakeConnection([item, {'id': 405, 'status': 'running'}])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.post('/content/22/assets/suggestions', json={'revision': content_visuals.revision(item)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['task_id'], 405)
        self.assertTrue(all('INSERT' not in sql for sql, _ in conn.calls))

    def test_suggestion_refresh_records_expected_fingerprint(self):
        from content_asset_workflow import fingerprint
        item = draft()
        conn = FakeConnection([item, None, {'id': 406, 'status': 'queued'}])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.post('/content/22/assets/suggestions', json={'revision': content_visuals.revision(item)})
        self.assertEqual(response.status_code, 200)
        inserts = [(sql, params) for sql, params in conn.calls if 'INSERT' in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(json.loads(inserts[0][1][0])['expected_fingerprint'], fingerprint(item))

    def test_stale_suggestions_are_not_presented_as_current(self):
        item = draft()
        item['structured']['asset_suggestions'] = {'fingerprint': 'old', 'slots': [{'index': 0}]}
        conn = FakeConnection([item, {'id': 406, 'status': 'done'}])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.get('/content/22/assets/suggestions')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['stale'])
        self.assertEqual(response.get_json()['slots'], [])

    def test_replace_image_keeps_body_and_blocks_in_sync(self):
        item = draft()
        old = {'type': 'image_slot', 'alt': 'Old descriptive alt text',
               'url': 'https://assets.example/old.png', 'caption': 'Old caption.'}
        item['content_blocks'].append(old)
        item['body'] += '\n\n![Old descriptive alt text](https://assets.example/old.png)\n\nOld caption.'
        metadata = {'url': 'https://assets.example/new.png', 'sha256': 'a' * 64,
                    'provenance': {'kind': 'owned'}}
        conn = FakeConnection([item, {'id': 8, 'metadata': metadata}, {'id': 410}])
        with mock.patch.object(routes.models, 'db', return_value=conn):
            response = self.client.post('/content/22/assets/attach', json={
                'revision': content_visuals.revision(item), 'asset_id': 8, 'index': 1,
                'alt': 'New descriptive alt text', 'caption': 'New caption.', 'reviewed': True})
        self.assertEqual(response.status_code, 200)
        updated = next(params for sql, params in conn.calls if sql.startswith('UPDATE content_items'))
        self.assertIn('![New descriptive alt text](https://assets.example/new.png)', updated[1])
        self.assertNotIn('Old caption', updated[1])
        self.assertIn('New caption.', updated[1])
        self.assertEqual(updated[1].count('!['), 1)


if __name__ == "__main__":
    unittest.main()
