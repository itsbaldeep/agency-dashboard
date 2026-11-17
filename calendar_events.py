"""Deterministic, read-only event feed for the marketing calendar."""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
MAX_ROWS = 2000
DEFAULT_CONFIG = "/agency-config/owned_marketing.json"
FALLBACK_CONFIG = "/home/agency/agency-os/config/owned_marketing.json"
DEFAULT_CALENDAR = {
    "timezone": "Asia/Kolkata", "projection_days": 35,
    "slots": [{"kind": "article", "weekday": 1, "time_utc": "07:15"},
              {"kind": "help", "weekday": 2, "time_utc": "07:15"},
              {"kind": "article", "weekday": 4, "time_utc": "07:15"}],
    "weekly_limits": {"article": 2, "help": 1}, "max_per_day": 1,
}
KIND_MAP = {"social_post": "social", "video_brief": "video", "email_campaign": "email", "channel_setup": "setup"}


def _zone(name):
    if name == "UTC": return timezone.utc
    if name == "Asia/Kolkata": return timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
    raise ValueError("timezone must be Asia/Kolkata or UTC")


def _month(value):
    m = MONTH_RE.match(value or "")
    if not m or not 1 <= int(m.group(2)) <= 12 or not 2000 <= int(m.group(1)) <= 2100: raise ValueError("month must be YYYY-MM")
    return value


def _obj(value):
    if isinstance(value, dict): return value
    try:
        value = json.loads(value or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError): return {}


def _dt(value, tz=timezone.utc):
    if not value: return None
    if isinstance(value, date) and not isinstance(value, datetime): return datetime.combine(value, datetime.min.time(), tz)
    if not isinstance(value, datetime):
        try: value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError): return None
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _iso(value): return value.isoformat() if value else None


def _safe_url(value):
    if not isinstance(value, str): return None
    try: p = urlsplit(value)
    except ValueError: return None
    return value if p.scheme == "https" and p.netloc and not p.username and not p.password else None


def _base(id_, title, brand_id, brand_name, kind, channel, status, when, time_kind, url, public_url, source, detail):
    return {"id": str(id_), "title": title or "Untitled", "brand_id": brand_id, "brand_name": brand_name or "",
            "kind": kind, "channel": channel, "status": status, "date": when.date().isoformat() if when else None,
            "at": _iso(when), "time_kind": time_kind, "url": url, "public_url": _safe_url(public_url),
            "source": source, "detail": detail}


def _config(path):
    path = path or os.environ.get("AGENCY_OWNED_MARKETING_CONFIG") or DEFAULT_CONFIG
    if not Path(path).exists() and path == DEFAULT_CONFIG: path = FALLBACK_CONFIG
    try: return _obj(Path(path).read_text())
    except (OSError, ValueError): return {}


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    rows = cur.fetchall()
    return rows[:MAX_ROWS + 1], len(rows) > MAX_ROWS


def load_calendar(conn, brand_id=None, month=None, timezone_name="Asia/Kolkata", now=None, config_path=None):
    """Return JSON-safe actual, planned, and explicitly-labelled projected events."""
    month = _month(month or datetime.now(timezone.utc).strftime("%Y-%m")); tz = _zone(timezone_name)
    now = _dt(now or datetime.now(timezone.utc), tz); today = now.date()
    year, mon = map(int, month.split("-")); first, last = date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])
    grid_start = first - timedelta(days=first.weekday()); grid_end = grid_start + timedelta(days=41)
    cur = conn.cursor(); filt = (brand_id,) if brand_id is not None else ()
    pub, cap_pub = _rows(cur, """SELECT t.id task_id,t.finished_at,t.result_ref,ci.id content_id,ci.title,ci.status content_status,ci.structured,b.id brand_id,b.name brand_name FROM tasks t JOIN content_items ci ON ci.publish_task_id=t.id JOIN brands b ON b.id=ci.brand_id WHERE t.type='publish_content' AND t.status='done' AND ci.status IN ('published','withdrawn') AND (%s IS NULL OR ci.brand_id=%s) ORDER BY t.id DESC LIMIT 2001""", (brand_id, brand_id) if brand_id is not None else (None, None))
    plans, cap_plan = _rows(cur, """SELECT c.id plan_id,c.brand_id,b.name brand_name,c.title,c.planned_date,c.status,c.updated_at,ci.id content_id,ci.status content_status,ci.structured FROM content_calendar c JOIN brands b ON b.id=c.brand_id LEFT JOIN content_items ci ON ci.structured->>'calendar_id'=c.id::text WHERE (%s IS NULL OR c.brand_id=%s) ORDER BY c.id DESC LIMIT 2001""", (brand_id, brand_id) if brand_id is not None else (None, None))
    works, cap_work = _rows(cur, """SELECT m.id,m.brand_id,b.name brand_name,m.kind,m.channel,m.title,m.planned_at,m.state,m.updated_at FROM marketing_work_items m JOIN brands b ON b.id=m.brand_id WHERE m.state<>'archived' AND m.kind<>'strategy' AND (%s IS NULL OR m.brand_id=%s) ORDER BY m.id DESC LIMIT 2001""", (brand_id, brand_id) if brand_id is not None else (None, None))
    runs, cap_run = _rows(cur, """SELECT r.id,r.brand_id,b.name brand_name,r.item_id,r.state,r.send_at,r.receipt,r.updated_at,wi.kind item_kind,wi.channel,wi.title item_title FROM marketing_campaign_runs r JOIN brands b ON b.id=r.brand_id LEFT JOIN marketing_work_items wi ON wi.id=r.item_id WHERE (%s IS NULL OR r.brand_id=%s) ORDER BY r.id DESC LIMIT 2001""", (brand_id, brand_id) if brand_id is not None else (None, None))
    drafts, _ = _rows(cur, """SELECT id,brand_id,title,status,structured,publish_task_id FROM content_items WHERE status='draft' AND publish_task_id IS NULL AND (%s IS NULL OR brand_id=%s) ORDER BY updated_at,id LIMIT 2000""", (brand_id, brand_id) if brand_id is not None else (None, None))
    brands, _ = _rows(cur, "SELECT id,name,project_id FROM brands WHERE (%s IS NULL OR id=%s) LIMIT 500", (brand_id, brand_id) if brand_id is not None else (None, None))
    events, unscheduled, seen, accounting = [], [], set(), []; actual_keys, linked_published = set(), set()
    def add(e):
        accounting.append(e)
        if e.get("date") and grid_start.isoformat() <= e["date"] <= grid_end.isoformat(): events.append(e)
    for r in pub:
        rec = _obj(r.get("result_ref")); url = _safe_url(rec.get("public_url") or rec.get("url")); ok = rec.get("status") == "published" or (rec.get("ok") is True and url)
        if not ok: continue
        key = (r.get("brand_id"), rec.get("post_id")) if rec.get("post_id") else (r.get("brand_id"), url)
        if key in actual_keys: continue
        actual_keys.add(key); linked_published.add(r.get("content_id")); linked_published.add(str(_obj(r.get("structured")).get("calendar_id")))
        pubat = _dt(rec.get("published_at"), tz); finished = _dt(r.get("finished_at"), tz); when = pubat or finished
        add(_base("publication-"+str(r.get("task_id")), rec.get("title") or r.get("title"), r.get("brand_id"), r.get("brand_name"), "help" if _obj(r.get("structured")).get("content_kind") == "help" else "article", "blog", "published", when, "published_at" if pubat else "confirmed_at", "/content/"+str(r.get("content_id"))+"/preview", url, "publish_content", "Published receipt" if pubat else "Publication confirmed by task completion"))
    # A calendar plan can join multiple content rows. Keep one deterministic row,
    # preferring a currently published link over draft/outline links.
    unique_plans = {}
    for r in plans:
        old = unique_plans.get(r.get("plan_id"))
        if old is None or (r.get("content_status") == "published" and old.get("content_status") != "published"): unique_plans[r.get("plan_id")] = r
    for r in unique_plans.values():
        if r.get("content_id") in linked_published or str(r.get("plan_id")) in linked_published: continue
        when = _dt(r.get("planned_date"), tz); kind = "help" if _obj(r.get("structured")).get("content_kind") == "help" else "article"; cancelled = str(r.get("status")) == "cancelled"; overdue = when and when.date() < today and not cancelled
        e = _base("plan-"+str(r.get("plan_id")), r.get("title"), r.get("brand_id"), r.get("brand_name"), kind, "blog", "cancelled" if cancelled else ("overdue" if overdue else "planned"), when, "planned", "/content/calendar?brand_id="+str(r.get("brand_id")), None, "content_calendar", "Cancelled plan" if cancelled else ("Past planned date" if overdue else "Planned publication")); add(e); seen.add(("work", r.get("content_id")))
        if e.get("date") and not isinstance(r.get("planned_date"), datetime) and not (isinstance(r.get("planned_date"), str) and "T" in r.get("planned_date")): e["at"] = None
        if not when or overdue: unscheduled.append({**e, "reason": "overdue" if overdue else "no planned date"})
    run_item_ids = {r.get("item_id") for r in runs if r.get("item_id") is not None}
    for r in works:
        if r.get("id") in run_item_ids: continue
        kind = KIND_MAP.get(r.get("kind"));
        if not kind: continue
        when = _dt(r.get("planned_at"), tz); e = _base("work-"+str(r.get("id")), r.get("title"), r.get("brand_id"), r.get("brand_name"), kind, r.get("channel"), "planned", when, "planned", "/brands/%s/work/%s" % (r.get("brand_id"), r.get("id")), None, "marketing_work_items", "Planned work item")
        if when: add(e)
        else: unscheduled.append({**e, "reason": "no planned date"})
    for r in runs:
        state = str(r.get("state") or ""); rec = _obj(r.get("receipt")); actual = None; actual_kind = None
        if state == "sent":
            for k in ("sent_at", "delivered_at", "completed_at"):
                actual = _dt(rec.get(k), tz)
                if actual: actual_kind = k; break
            if not actual: actual, actual_kind = _dt(r.get("updated_at"), tz), "confirmed_at"
        when = actual or _dt(r.get("send_at"), tz); kind = KIND_MAP.get(r.get("item_kind"), "social")
        status = "sent" if state == "sent" else state if state in ("failed", "cancelled") else "scheduled"
        e = _base("run-"+str(r.get("id")), r.get("item_title") or "Campaign", r.get("brand_id"), r.get("brand_name"), kind, r.get("channel"), status, when, actual_kind if actual else "planned", "/brands/%s/work/%s" % (r.get("brand_id"), r.get("item_id")) if r.get("item_id") else None, None, "marketing_campaign_runs", "Campaign receipt" if actual else "Scheduled campaign")
        if when: add(e)
        else: unscheduled.append({**e, "reason": "no planned date"})
    # Projections are opt-in, active-brand configuration only.
    cfg = _config(config_path); configured = _obj(cfg.get("brands")); projected = []
    for b in brands:
        bc = configured.get(str(b.get("id")), configured.get(b.get("id"), {})); raw_cal = bc.get("calendar") if isinstance(bc, dict) else None
        if not isinstance(bc, dict) or not bc.get("enabled") or not isinstance(raw_cal, dict) or bc.get("project_id") != b.get("project_id"): continue
        cal = {**DEFAULT_CALENDAR, **raw_cal}
        if not isinstance(cal.get("slots"), list) or not cal.get("slots"): continue
        try: maxday = int(cal.get("max_per_day", 1)); projection_days = int(cal.get("projection_days", 35))
        except (TypeError, ValueError): continue
        if maxday < 1 or not 1 <= projection_days <= 90: continue
        raw_limits = cal.get("weekly_limits")
        if not isinstance(raw_limits, dict): continue
        try: limits = {**DEFAULT_CALENDAR["weekly_limits"], **{k: int(v) for k, v in raw_limits.items()}}
        except (TypeError, ValueError): continue
        if any(v < 0 for v in limits.values()): continue
        pending = {"article": [], "help": []}
        for d in drafts:
            if d.get("brand_id") == b.get("id") and d.get("status") == "draft" and not d.get("publish_task_id"):
                k = "help" if _obj(d.get("structured")).get("content_kind") == "help" else "article"; pending[k].append(d)
        for n in range(projection_days):
            day = today + timedelta(days=n); day_count = sum(1 for x in accounting + projected if x.get("brand_id") == b.get("id") and x.get("kind") in ("article", "help") and x.get("status") in ("published", "planned", "scheduled", "projected") and x.get("date") == day.isoformat())
            for slot in cal.get("slots", []):
                if not isinstance(slot, dict): continue
                kind = slot.get("kind");
                try: slot_weekday = int(slot.get("weekday"))
                except (TypeError, ValueError): continue
                if kind not in pending or day.weekday() != slot_weekday or day_count >= maxday: continue
                try: hh, mm = map(int, str(slot.get("time_utc", "07:15")).split(":")[:2])
                except (TypeError, ValueError): continue
                if not 0 <= hh <= 23 or not 0 <= mm <= 59: continue
                at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc).astimezone(tz)
                if at <= now: continue
                limit = limits.get(kind, 0)
                if limit <= 0: continue
                def counts_at(moment):
                    prior = []
                    for x in accounting + projected:
                        if x.get("brand_id") != b.get("id") or x.get("kind") != kind or x.get("status") not in ("published", "planned", "scheduled", "projected"): continue
                        # Date-only plans conservatively occupy their entire day.
                        timestamp = _dt(x.get("at"), tz)
                        if timestamp is None and x.get("date"):
                            timestamp = datetime.combine(date.fromisoformat(x["date"]), datetime.max.time(), tz)
                        if timestamp and moment - timedelta(days=7) < timestamp <= moment:
                            prior.append(timestamp)
                    return prior
                prior = counts_at(at)
                if len(prior) >= limit:
                    expiry = sorted(prior)[len(prior)-limit] + timedelta(days=7)
                    if expiry > at + timedelta(hours=1) or len(counts_at(expiry)) >= limit: continue
                    at = expiry
                    shifted_count = sum(1 for x in accounting + projected if x.get("brand_id") == b.get("id") and x.get("kind") in ("article", "help") and x.get("status") in ("published", "planned", "scheduled", "projected") and x.get("date") == at.date().isoformat())
                    if shifted_count >= maxday: continue
                candidate = pending[kind].pop(0) if pending[kind] else None; title = (candidate or {}).get("title") or ("Help slot: topic to be selected" if kind == "help" else "Article slot: topic to be selected")
                projected.append(_base("projection-%s-%s-%s" % (b.get("id"), day.isoformat(), kind), title, b.get("id"), b.get("name"), kind, "blog", "projected", at, "projected", "/content/"+str(candidate["id"])+"/preview" if candidate else "/content/calendar?brand_id="+str(b.get("id")), None, "owned_marketing_projection", "Publication window, pending quality checks; not a scheduled publish")); day_count += 1
    for e in projected: add(e)
    coverage = {"complete": not any((cap_pub, cap_plan, cap_work, cap_run)), "grid_start": grid_start.isoformat(), "grid_end": grid_end.isoformat(), "sources": {"publish_content": {"capped": cap_pub}, "content_calendar": {"capped": cap_plan}, "marketing_work_items": {"capped": cap_work}, "marketing_campaign_runs": {"capped": cap_run}}}
    return {"month": month, "timezone": timezone_name, "today": today.isoformat(), "brands": [{"id": b.get("id"), "name": b.get("name") or ""} for b in brands], "events": events, "unscheduled": unscheduled, "coverage": coverage}
