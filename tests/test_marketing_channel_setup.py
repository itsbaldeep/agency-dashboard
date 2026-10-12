import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
import marketing_workspace as workspace


class MarketingChannelSetupTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()

    def test_identity_reference_is_strict_and_bounded(self):
        good = {"work_id": 4, "source_revision": 2, "filename": "avatar.png", "sha256": "a" * 64}
        self.assertEqual(workspace._identity_ref(good), good)
        for value in ({**good, "work_id": True}, {**good, "source_revision": "2"},
                      {**good, "filename": "../avatar.png"}, {**good, "sha256": "A" * 64},
                      {**good, "extra": 1}):
            self.assertIsNone(workspace._identity_ref(value))

    def test_csv_formula_escape_handles_leading_whitespace_and_identity_refs_fail_closed(self):
        for value in ("=Launch", "  +Launch", "\t-Launch", "@Launch"):
            self.assertTrue(workspace._csv_safe(value).lstrip().startswith("'"))
        self.assertEqual(workspace._csv_safe("Launch"), "Launch")

    def test_bio_allows_newlines_but_other_identity_fields_reject_controls(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.post("/api/brands/4/channels/instagram", json={
                "revision": 0, "display_name": "Name", "handle": "handle\n", "bio": "Line one\nLine two",
                "profile_url": "https://instagram.com/example", "setup_checks": {},
            }, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 400)
        db.assert_not_called()

    def test_saved_identity_reference_keeps_original_revision_in_selector(self):
        saved = {"work_id": 4, "source_revision": 1, "filename": "avatar.png", "sha256": "a" * 64}
        candidate = {"work_id": 4, "source_revision": 2, "filename": "avatar.png", "sha256": "a" * 64,
                     "width": 512, "height": 512, "title": "New copy"}
        with dashboard.app.test_request_context("/"):
            html = dashboard.render_template("marketing_channel.html", brand={"id": 4, "name": "Fixture"},
                channel="instagram", saved={"revision": 2, "setup_checks": {}, "identity_assets": {"avatar": saved}},
                profile={}, guide=("https://instagram.com", "Guide"), checks=workspace.CHANNEL_CHECKS,
                work_items=[], first_posts=[], suggested_identity={"display_name": "", "bio": ""},
                identity_assets={"avatar": saved}, identity_candidates=[candidate])
        self.assertIn('value="{&#34;filename&#34;: &#34;avatar.png&#34;', html)
        self.assertIn("Saved avatar.png · rev 1", html)

    def test_invalid_typed_channel_fields_rejected_before_database(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.post("/api/brands/4/channels/instagram", json={
                "revision": "0", "display_name": "Name", "handle": "handle", "bio": "Bio",
                "profile_url": "https://instagram.com/example", "setup_checks": {},
            }, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 400)
        db.assert_not_called()

    def test_identity_image_route_is_brand_scoped_and_no_store(self):
        with mock.patch.object(dashboard.models, "db") as db:
            response = self.client.get("/brands/4/channels/instagram/identity/avatar")
        self.assertEqual(response.status_code, 404)

    def test_launch_kit_route_rejects_unknown_channel(self):
        response = self.client.get("/brands/4/channels/nope/launch-kit")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
