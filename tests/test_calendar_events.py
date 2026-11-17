import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from calendar_events import load_calendar, _safe_url, _zone, _obj


class Cursor:
    def __init__(self, data): self.data, self.sql = data, ""
    def execute(self, sql, params=()): self.sql = sql
    def fetchall(self):
        s = self.sql
        if "status='draft'" in s: return self.data.get("drafts", [])
        if "publish_task_id" in s: return self.data.get("pub", [])
        if "FROM content_calendar" in s: return self.data.get("plans", [])
        if "marketing_work_items m" in s: return self.data.get("works", [])
        if "marketing_campaign_runs r" in s: return self.data.get("runs", [])
        return self.data.get("brands", [])


class Conn:
    def __init__(self, data): self.data = data
    def cursor(self): return Cursor(self.data)


def base():
    return {"pub": [], "plans": [], "works": [], "runs": [], "drafts": [], "brands": []}


class CalendarEventsTests(unittest.TestCase):
    def test_timezone_and_month_validation(self):
        self.assertEqual(_zone("UTC").utcoffset(None).total_seconds(), 0)
        with self.assertRaises(ValueError): _zone("Europe/London")
        with self.assertRaises(ValueError): load_calendar(Conn(base()), month="2026-13")

    def test_confirmed_timestamp_and_private_receipt_exclusion(self):
        d = base(); d["pub"] = [
            {"task_id": 1, "finished_at": datetime(2026, 11, 10, 22, 16, tzinfo=timezone.utc), "result_ref": json.dumps({"ok": True, "url": "https://trueapply.in/blog/x", "post_id": "x"}), "content_id": 2, "title": "X", "content_status": "published", "structured": {"content_kind": "article"}, "brand_id": 3, "brand_name": "True"},
            {"task_id": 2, "finished_at": datetime(2026, 11, 10, tzinfo=timezone.utc), "result_ref": json.dumps({"ok": True, "url": "http://ghost.local/private"}), "content_id": 4, "title": "Private", "content_status": "published", "structured": {}, "brand_id": 3, "brand_name": "True"},
        ]
        out = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(len(out["events"]), 1); self.assertEqual(out["events"][0]["time_kind"], "confirmed_at")
        self.assertEqual(out["events"][0]["date"], "2026-11-11"); self.assertEqual(out["events"][0]["public_url"], "https://trueapply.in/blog/x")

    def test_published_plan_is_deduplicated_and_cancelled_visible(self):
        d = base(); d["pub"] = [{"task_id": 1, "finished_at": datetime(2026, 11, 10, tzinfo=timezone.utc), "result_ref": {"status": "published", "url": "https://trueapply.in/blog/x"}, "content_id": 2, "title": "X", "content_status": "published", "structured": {"calendar_id": "9"}, "brand_id": 3, "brand_name": "True"}]
        d["plans"] = [{"plan_id": 9, "brand_id": 3, "brand_name": "True", "title": "X", "planned_date": "2026-11-12", "status": "planned", "content_id": 2, "structured": {"calendar_id": "9"}}, {"plan_id": 10, "brand_id": 3, "brand_name": "True", "title": "Cancelled", "planned_date": "2026-11-13", "status": "cancelled", "content_id": None, "structured": {}}]
        events = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 1, tzinfo=timezone.utc))["events"]
        self.assertEqual([e["status"] for e in events], ["published", "cancelled"])

    def test_campaign_send_time_is_not_actual_sent_time(self):
        d = base(); d["runs"] = [{"id": 7, "brand_id": 3, "brand_name": "True", "item_id": 8, "state": "sent", "send_at": "2026-11-10T01:00:00+00:00", "receipt": {"sent_at": "2026-11-11T02:00:00+00:00"}, "updated_at": "2026-11-12T00:00:00+00:00", "item_kind": "email_campaign", "channel": "email", "item_title": "Mail"}]
        e = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 1, tzinfo=timezone.utc))["events"][0]
        self.assertEqual(e["status"], "sent"); self.assertEqual(e["time_kind"], "sent_at"); self.assertEqual(e["date"], "2026-11-11")

    def test_no_pii_and_coverage_shape(self):
        d = base(); d["works"] = [{"id": 1, "brand_id": 3, "brand_name": "True", "kind": "social_post", "channel": "linkedin", "title": "Post", "planned_at": None, "state": "ready"}]
        out = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(out["unscheduled"][0]["reason"], "no planned date"); self.assertNotIn("recipient", str(out)); self.assertIn("sources", out["coverage"])

    def test_projection_uses_explicit_config_weekday_and_date_only_plan_has_no_time(self):
        d = base(); d["brands"] = [{"id": 3, "name": "True", "project_id": 30}]
        d["plans"] = [{"plan_id": 12, "brand_id": 3, "brand_name": "True", "title": "Planned", "planned_date": "2026-11-18", "status": "planned", "content_id": None, "structured": {"content_kind": "article"}}]
        config = {"brands": {"3": {"enabled": True, "project_id": 30, "calendar": {"projection_days": 8, "slots": [{"kind": "article", "weekday": 1, "time_utc": "07:15"}], "weekly_limits": {"article": 2}, "max_per_day": 1}}}}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump(config, f); f.flush()
            out = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 16, 0, tzinfo=timezone.utc), config_path=f.name)
        self.assertEqual(next(e for e in out["events"] if e["id"] == "plan-12")["at"], None)
        self.assertTrue(any(e["status"] == "projected" and e["date"] == "2026-11-17" for e in out["events"]))

    def test_malformed_json_url_limits_and_undated_cancelled_run_are_safe(self):
        self.assertEqual(_obj("[]"), {})
        self.assertIsNone(_safe_url("https://[bad"))
        d = base(); d["runs"] = [{"id": 8, "brand_id": 3, "brand_name": "True", "item_id": None, "state": "cancelled", "send_at": None, "receipt": "[]", "updated_at": None, "item_kind": "email_campaign", "channel": "email", "item_title": "Cancelled"}]
        d["brands"] = [{"id": 3, "name": "True", "project_id": 30}]
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump({"brands": {"3": {"enabled": True, "project_id": 30, "calendar": {"projection_days": "bad", "slots": [{"kind": "article", "weekday": 1}], "weekly_limits": {"article": 2}, "max_per_day": 1}}}}, f); f.flush()
            out = load_calendar(Conn(d), month="2026-11", config_path=f.name)
        self.assertEqual(out["unscheduled"][0]["status"], "cancelled"); self.assertFalse(any(e["status"] == "projected" for e in out["events"]))

    def test_rolling_limit_boundary_uses_exact_expiry_and_never_three(self):
        d = base(); d["brands"] = [{"id": 3, "name": "True", "project_id": 30}]
        d["pub"] = [
            {"task_id": 13, "finished_at": "2026-11-13T07:17:32+00:00", "result_ref": {"status": "published", "url": "https://trueapply.in/blog/a", "post_id": "a"}, "content_id": 13, "title": "A", "content_status": "published", "structured": {"content_kind": "article"}, "brand_id": 3, "brand_name": "True"},
            {"task_id": 17, "finished_at": "2026-11-17T07:23:56+00:00", "result_ref": {"status": "published", "url": "https://trueapply.in/blog/b", "post_id": "b"}, "content_id": 17, "title": "B", "content_status": "published", "structured": {"content_kind": "article"}, "brand_id": 3, "brand_name": "True"},
        ]
        config = {"brands": {"3": {"enabled": True, "project_id": 30, "calendar": {"projection_days": 35, "slots": [{"kind": "article", "weekday": 4, "time_utc": "07:15"}, {"kind": "article", "weekday": 1, "time_utc": "07:15"}], "weekly_limits": {"article": 2}, "max_per_day": 1}}}}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump(config, f); f.flush()
            out = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 1, tzinfo=timezone.utc), config_path=f.name)
        projected = [e for e in out["events"] if e["status"] == "projected" and e["date"] in ("2026-11-20", "2026-11-24")]
        self.assertEqual([e["date"] for e in projected], ["2026-11-20", "2026-11-24"])
        self.assertEqual([datetime.fromisoformat(e["at"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z") for e in projected], ["2026-11-20T07:17:32+0000", "2026-11-24T07:23:56+0000"])
        for e in projected:
            self.assertLessEqual(sum(1 for x in [*out["events"]] if x["kind"] == "article" and x["brand_id"] == 3 and x.get("at") and datetime.fromisoformat(x["at"]) > datetime.fromisoformat(e["at"]) - timedelta(days=7) and datetime.fromisoformat(x["at"]) <= datetime.fromisoformat(e["at"])), 2)

    def test_forecast_is_independent_of_viewed_month_and_stale_outline_is_ignored(self):
        d = base(); d["brands"] = [{"id": 3, "name": "True", "project_id": 30}]
        d["drafts"] = [{"id": 26, "brand_id": 3, "title": "Help 26", "status": "draft", "structured": {"content_kind": "help"}, "publish_task_id": None}, {"id": 27, "brand_id": 3, "title": "Stale", "status": "outline", "structured": {"content_kind": "help"}, "publish_task_id": None}]
        config = {"brands": {"3": {"enabled": True, "project_id": 30, "calendar": {"projection_days": 35, "slots": [{"kind": "help", "weekday": 1, "time_utc": "07:15"}], "weekly_limits": {"help": 1}, "max_per_day": 1}}}}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump(config, f); f.flush()
            nov = load_calendar(Conn(d), month="2026-11", now=datetime(2026, 11, 25, tzinfo=timezone.utc), config_path=f.name)
            dec = load_calendar(Conn(d), month="2026-12", now=datetime(2026, 11, 25, tzinfo=timezone.utc), config_path=f.name)
        n = {e["id"]: e for e in nov["events"] if e["status"] == "projected" and e["date"].startswith("2026-12-")}
        q = {e["id"]: e for e in dec["events"] if e["status"] == "projected" and e["date"].startswith("2026-12-")}
        self.assertTrue(set(n).issubset(set(q))); self.assertIn("Help 26", {e["title"] for e in nov["events"]}); self.assertNotIn("Stale", {e["title"] for e in nov["events"]})


if __name__ == "__main__": unittest.main()
