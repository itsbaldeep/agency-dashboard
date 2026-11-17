import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))
from browser_client import browser_client
import app as dashboard
import content_text_edits as edits
import content_visuals


def draft(status="draft"):
    return {"id": 23, "brand_id": 4, "title": "Example", "status": status,
            "lifecycle": "active", "content_blocks": [{"type": "prose", "markdown": "Original prose", "fact_ids": []}],
            "body": "Original prose", "structured": {"facts": [], "link_report": {"old": True}},
            "updated_at": datetime(2026, 10, 2, tzinfo=timezone.utc)}


class FakeConnection:
    def __init__(self, item, active=None):
        self.item, self.active = copy.deepcopy(item), active
        self.rows, self.calls, self.tasks = [], [], {}
        self.commits = self.rollbacks = 0

    def cursor(self): return self
    def close(self): pass
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("SELECT ci."):
            self.rows.append(copy.deepcopy(self.item))
        elif sql.startswith("SELECT id,type,status"):
            self.rows.append(copy.deepcopy(self.active))
        elif sql.startswith("UPDATE content_items"):
            if "content_blocks=%s" in sql:
                self.item.update(title=params[0], content_blocks=json.loads(params[1]), body=params[2], structured=json.loads(params[3]))
            else:
                self.item.update(title=params[0], structured=json.loads(params[1]))
            self.item["updated_at"] += timedelta(seconds=1)
            self.rows.append({"updated_at": self.item["updated_at"]})
        elif sql.startswith("INSERT INTO tasks"):
            task_id = 900 + len(self.tasks)
            self.tasks[task_id] = {"params": json.loads(params[0]), "result_ref": json.loads(params[1])}
            self.rows.append({"id": task_id})

    def fetchone(self): return self.rows.pop(0) if self.rows else None


class ContentTextEditRouteTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config["TESTING"] = True
        self.client = browser_client(dashboard.app)
        self.db = FakeConnection(draft())
        self.patch = mock.patch.object(edits.models, "db", return_value=self.db)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def revision(self): return content_visuals.revision(self.db.item)

    def payload(self, **extra):
        return {"revision": self.revision(), "blocks": copy.deepcopy(self.db.item["content_blocks"]),
                "review_note": "Routine editorial correction", **extra}

    def test_get_uses_canonical_revision_and_preview_does_not_write(self):
        response = self.client.get("/content/23/edit-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["revision"], self.revision())
        before = copy.deepcopy(self.db.item)
        preview = self.client.post("/content/23/edit/preview", json=self.payload())
        self.assertEqual(preview.status_code, 200, preview.get_json())
        self.assertEqual(self.db.item, before)
        self.assertEqual(self.db.commits, 0)

    def test_stale_revision_and_digest_mismatch_are_rejected(self):
        self.assertEqual(self.client.post("/content/23/edit/preview", json=self.payload(revision="stale")).status_code, 409)
        preview = self.client.post("/content/23/edit/preview", json=self.payload(title="Changed"))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.client.post("/content/23/edit", json=self.payload(title="Changed", preview_digest="wrong")).status_code, 409)
        self.assertEqual(self.db.commits, 0)

    def test_status_lifecycle_and_active_task_guards(self):
        for status in ("approved", "publishing", "published", "needs_input"):
            db = FakeConnection(draft(status))
            with mock.patch.object(edits.models, "db", return_value=db):
                self.assertEqual(self.client.get("/content/23/edit-data").status_code, 409)
        db = FakeConnection(draft(), {"id": 4, "type": "publish_content", "status": "running"})
        with mock.patch.object(edits.models, "db", return_value=db):
            self.assertEqual(self.client.get("/content/23/edit-data").status_code, 409)
        db = FakeConnection(draft()); db.item["lifecycle"] = "parked"
        with mock.patch.object(edits.models, "db", return_value=db):
            self.assertEqual(self.client.get("/content/23/edit-data").status_code, 409)

    def test_unknown_metadata_invalid_blocks_and_asset_changes_fail(self):
        self.assertEqual(self.client.post("/content/23/edit/preview", json=self.payload(metadata={"private": "x"})).status_code, 400)
        self.assertEqual(self.client.post("/content/23/edit/preview", json=self.payload(blocks=[{"type": "unknown", "markdown": "x"}])).status_code, 400)
        self.db.item["structured"]["facts"] = [{"id": "f1"}]
        original = self.db.item["content_blocks"] + [{"type": "chart", "chart_type": "bar", "fact_ids": ["f1"]}]
        self.db.item["content_blocks"] = original
        changed = copy.deepcopy(original); changed[-1]["chart_type"] = "line"
        self.assertEqual(self.client.post("/content/23/edit/preview", json=self.payload(blocks=changed)).status_code, 400)

    def test_save_keeps_markdown_clears_link_report_and_records_before_snapshot(self):
        blocks = [{"type": "prose", "markdown": "Updated **copy**", "fact_ids": []}]
        payload = self.payload(blocks=blocks, review_note="Reviewed by owner")
        preview = self.client.post("/content/23/edit/preview", json=payload).get_json()
        saved = self.client.post("/content/23/edit", json={**payload, "preview_digest": preview["preview_digest"]})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(self.db.item["body"], "Updated **copy**")
        self.assertNotIn("link_report", self.db.item["structured"])
        audit = next(iter(self.db.tasks.values()))["result_ref"]
        self.assertEqual(audit["before"]["structured"]["link_report"], {"old": True})
        self.assertEqual(audit.get("review_note"), "Reviewed by owner")
        self.assertFalse(audit["publication"])

    def test_outline_checkpoint_and_fact_orphan_are_rejected(self):
        item = draft("outline")
        item["content_blocks"] = [{"type": "prose", "brief": "composed"}]
        item["structured"] = {"blocks": [{"type": "intro", "brief": "opening", "fact_ids": []}], "facts": []}
        db = FakeConnection(item)
        with mock.patch.object(edits.models, "db", return_value=db):
            response = self.client.post("/content/23/edit/preview", json={"revision": content_visuals.revision(item), "blocks": item["structured"]["blocks"]})
        self.assertEqual(response.status_code, 409)
        item["content_blocks"] = []
        item["structured"]["blocks"] = [{"type": "intro", "brief": "opening", "fact_ids": ["f1"]}, {"type": "prose", "brief": "body", "fact_ids": []}]
        item["structured"]["facts"] = [{"id": "f1"}]
        db = FakeConnection(item)
        with mock.patch.object(edits.models, "db", return_value=db):
            response = self.client.post("/content/23/edit/preview", json={"revision": content_visuals.revision(item), "blocks": item["structured"]["blocks"], "remove_fact_ids": ["f1"]})
        self.assertEqual(response.status_code, 400)


    def test_outline_save_removes_unreferenced_fact_and_keeps_status(self):
        item = draft("outline"); item["content_blocks"] = []; item["body"] = None
        item["structured"] = {"blocks":[{"type":"intro","brief":"Open","keyword_target":True},{"type":"prose","brief":"Explain","keyword_target":True}],"facts":[{"id":"old","claim":"Unsuitable"}],"target_keyword":"example"}
        db = FakeConnection(item)
        payload = {"revision":content_visuals.revision(item),"blocks":item["structured"]["blocks"],"metadata":{"meta_title":"A reviewed outline"},"remove_fact_ids":["old"],"review_note":"Remove unsuitable unused evidence"}
        with mock.patch.object(edits.models,"db",return_value=db):
            preview = self.client.post("/content/23/edit/preview",json=payload)
            self.assertEqual(preview.status_code,200,preview.get_json())
            saved = self.client.post("/content/23/edit",json={**payload,"preview_digest":preview.get_json()["preview_digest"]})
        self.assertEqual(saved.status_code,200,saved.get_json())
        self.assertEqual(db.item["status"],"outline")
        self.assertEqual(db.item["structured"]["facts"],[])
        self.assertEqual(db.item["structured"]["meta_title"],"A reviewed outline")
        self.assertIsNone(db.item["body"])

    def test_existing_image_preserved_during_text_edit(self):
        image={"type":"image_slot","alt":"A useful owned diagram","url":"https://example.com/image.png","reviewed":True,"asset":{"sha256":"abc","provenance":{"kind":"owned"}}}
        self.db.item["content_blocks"].append(image)
        blocks=copy.deepcopy(self.db.item["content_blocks"]); blocks[0]["markdown"]="New introduction"
        data=self.payload(blocks=blocks)
        preview=self.client.post("/content/23/edit/preview",json=data)
        self.assertEqual(preview.status_code,200,preview.get_json())
        saved=self.client.post("/content/23/edit",json={**data,"preview_digest":preview.get_json()["preview_digest"]})
        self.assertEqual(saved.status_code,200,saved.get_json())
        self.assertEqual(self.db.item["content_blocks"][1],image)
        self.assertIn('![A useful owned diagram](https://example.com/image.png)',self.db.item["body"])
        self.assertNotIn('<html',self.db.item["body"])
        self.assertIn('quality_report',self.db.item["structured"])

    def test_rejects_cross_origin_and_large_payload(self):
        self.assertEqual(self.client.post('/content/23/edit/preview',json=self.payload(),headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.post('/content/23/edit/preview',data='x'*200001,content_type='application/json').status_code,413)

if __name__ == "__main__":
    unittest.main()
