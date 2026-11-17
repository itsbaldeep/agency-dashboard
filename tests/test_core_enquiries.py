import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
import core_enquiries


class _Cursor:
    def __init__(self, brand):
        self.brand = brand

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self.brand


class _Connection:
    def __init__(self, brand):
        self.brand = brand

    def cursor(self):
        return _Cursor(self.brand)

    def close(self):
        return None


class CoreEnquiryTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.brand_id = 7
        self.db_dir = self.root / str(self.brand_id)
        self.db_dir.mkdir()
        self.db = self.db_dir / "leads.db"
        self.brand = {"id": 7, "project_id": 12, "classification": "core", "lifecycle": "active"}
        self.env = mock.patch.dict(os.environ, {"AGENCY_CORE_ENQUIRY_OWNERS": "7:12"}, clear=False)
        self.env.start()
        self.root_patch = mock.patch.object(core_enquiries, "ENQUIRY_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _create_db(self, message="Hello"):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT, email TEXT, company TEXT, message TEXT, created_at TEXT, notified INTEGER)")
            db.execute("INSERT INTO leads VALUES (1, '<img>', 'x@example.test', 'Acme', ?, '2026-10-06T00:00:00Z', 1)", (message,))
            db.commit()

    def _get(self):
        return self.client.get("/brands/7/enquiries")

    def test_bounded_rows_render_escaped_and_no_store(self):
        self._create_db("<script>alert(1)</script>")
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            response = self._get()
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("sent", html)

    def test_unconfigured_and_wrong_ownership_are_unavailable(self):
        with mock.patch.dict(os.environ, {"AGENCY_CORE_ENQUIRY_OWNERS": ""}, clear=False):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)
        with mock.patch.object(dashboard.models, "db", return_value=_Connection({**self.brand, "project_id": 99})):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)

    def test_inactive_and_non_core_are_unavailable(self):
        for changes in ({"lifecycle": "soft_parked"}, {"classification": "engagement"}):
            with mock.patch.object(dashboard.models, "db", return_value=_Connection({**self.brand, **changes})):
                response = self._get()
            self.assertIn(b"Enquiries are unavailable.", response.data)

    def test_missing_corrupt_and_symlink_sources_do_not_create_or_expose_path(self):
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)
        self.assertFalse(self.db.exists())
        self.db.write_text("not sqlite")
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)
        self.db.unlink()
        self.db.symlink_to(self.root / "missing-target")
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)
        self.assertNotIn(str(self.root).encode(), response.data)

    def test_mapping_rejects_malformed_duplicate_and_non_ascii_values(self):
        for value in ("7:12,7:13", "7:0", "7 :12", "é:12", "07:12", "7:12" + ",8:13" * 64):
            with mock.patch.dict(os.environ, {"AGENCY_CORE_ENQUIRY_OWNERS": value}, clear=False):
                self.assertFalse(core_enquiries.configured_for_brand(7, 12))
        self.assertFalse(core_enquiries.configured_for_brand("7", 12))
        self.assertFalse(core_enquiries.configured_for_brand(7, 12.0))

    def test_bad_timestamp_shape_is_unavailable(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT, email TEXT, company TEXT, message TEXT, created_at TEXT, notified INTEGER)")
            db.execute("INSERT INTO leads VALUES (1, 'n', 'e', 'c', 'm', ?, 0)", ("x" * 65,))
            db.commit()
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            response = self._get()
        self.assertIn(b"Enquiries are unavailable.", response.data)

    def test_database_is_read_only(self):
        self._create_db()
        captured = {}
        original_connect = sqlite3.connect

        def capture_connect(*args, **kwargs):
            class CaptureConnection(sqlite3.Connection):
                def close(self):
                    captured["connection"] = self
            kwargs["factory"] = CaptureConnection
            return original_connect(*args, **kwargs)

        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)), \
                mock.patch.object(core_enquiries.sqlite3, "connect", side_effect=capture_connect) as connect:
            response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("mode=ro", connect.call_args.args[0])
        with self.assertRaises(sqlite3.OperationalError):
            captured["connection"].execute("INSERT INTO leads VALUES (2, 'n', 'e', 'c', 'm', 't', 0)")
        sqlite3.Connection.close(captured["connection"])

    def test_source_timestamp_is_validated_and_displayed_in_utc(self):
        self._create_db()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE leads SET created_at='2026-10-06T09:00:00+09:00'")
            db.commit()
        rows, _ = core_enquiries._read_rows(self.db)
        self.assertEqual(rows[0]['created_at'], '2026-10-06 00:00:00')
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE leads SET created_at='not a date'")
            db.commit()
        self.assertIsNone(core_enquiries._read_rows(self.db))

    def test_cursor_paginates_fifty_rows_without_overlap(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT, email TEXT, company TEXT, message TEXT, created_at TEXT, notified INTEGER)")
            db.executemany("INSERT INTO leads VALUES (?, 'n', 'e', 'c', 'm', '2026-10-06T00:00:00Z', 0)", ((i,) for i in range(1, 56)))
            db.commit()
        with mock.patch.object(dashboard.models, "db", return_value=_Connection(self.brand)):
            first = self._get()
            second = self.client.get("/brands/7/enquiries?before=6")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows, next_before = core_enquiries._read_rows(self.db)
        self.assertEqual(len(rows), 50)
        self.assertEqual(next_before, 6)
        older, older_next = core_enquiries._read_rows(self.db, before=6)
        self.assertEqual([row["id"] for row in older], [5, 4, 3, 2, 1])
        self.assertIsNone(older_next)
        for value in ("0", "01", "1.0", "-1", "999999999999999999999999999999999999999999999999999999999999"):
            response = self.client.get("/brands/7/enquiries?before=" + value)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/brands/7/enquiries?before=6&before=5").status_code, 400)
        self.assertEqual(self.client.get("/brands/7/enquiries?other=6").status_code, 400)


if __name__ == "__main__":
    unittest.main()
