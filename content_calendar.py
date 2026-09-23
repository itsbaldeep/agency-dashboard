"""Bounded, approval-free content planning and research kickoff routes."""

import ipaddress
import json
from datetime import date
from urllib.parse import urlparse

from flask import Blueprint, jsonify, render_template, request
from psycopg2 import errors

import models


content_calendar = Blueprint("content_calendar", __name__)
SUCCESS_METRICS = {"gsc_clicks", "ga4_users", "ga4_key_events"}


def _text(value, field, maximum):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = " ".join(value.split()).strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{field} must be 1-{maximum} characters")
    return value


def _optional_text(value, field, maximum):
    if value in (None, ""):
        return ""
    return _text(value, field, maximum)


def _public_https_url(value):
    if not isinstance(value, str):
        raise ValueError("competitor URLs must be text")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("competitor URLs must be public HTTPS URLs")
    if parsed.port is not None or parsed.fragment or parsed.query:
        raise ValueError("competitor URLs cannot contain credentials, ports, queries, or fragments")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or "." not in host:
        raise ValueError("competitor URL host must be public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified or address.is_multicast):
        raise ValueError("competitor URL host must be public")
    return parsed.geturl()


def _urls(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("competitor URLs must contain at most 5 URLs")
    urls = [_public_https_url(item) for item in value]
    if len(set(urls)) != len(urls):
        raise ValueError("competitor URLs must be unique")
    return urls


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else request.form.to_dict(flat=True)


def _plan_payload(data):
    try:
        brand_id = int(data.get("brand_id"))
    except (TypeError, ValueError):
        raise ValueError("brand_id is required")
    if brand_id <= 0:
        raise ValueError("brand_id is required")
    planned_date = data.get("planned_date")
    try:
        planned_date = date.fromisoformat(str(planned_date)) if planned_date else None
    except ValueError:
        raise ValueError("planned_date must be YYYY-MM-DD")
    if planned_date is None:
        raise ValueError("planned_date is required")
    metric = _text(data.get("success_metric"), "success_metric", 32)
    if metric not in SUCCESS_METRICS:
        raise ValueError("success_metric is unsupported")
    evidence_audit_id = data.get("evidence_audit_id")
    if evidence_audit_id in (None, ""):
        evidence_audit_id = None
    else:
        try:
            evidence_audit_id = int(evidence_audit_id)
        except (TypeError, ValueError):
            raise ValueError("evidence_audit_id must be an integer")
        if evidence_audit_id <= 0:
            raise ValueError("evidence_audit_id must be an integer")
    return {
        "brand_id": brand_id,
        "title": _text(data.get("title"), "title", 200),
        "target_keyword": _text(data.get("target_keyword"), "target_keyword", 200),
        "audience": _text(data.get("audience"), "audience", 200),
        "hypothesis": _text(data.get("hypothesis"), "hypothesis", 1000),
        "success_metric": metric,
        "planned_date": planned_date,
        "competitor_urls": _urls(data.get("competitor_urls")),
        "evidence_audit_id": evidence_audit_id,
        "evidence_note": _optional_text(data.get("evidence_note"), "evidence_note", 2000),
    }


def _eligible_brand(cur, brand_id, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    cur.execute(
        "SELECT b.id, b.name, b.project_id, p.lifecycle, p.state "
        "FROM brands b JOIN projects p ON p.id=b.project_id "
        "WHERE b.id=%s%s" % ("%s", suffix), (brand_id,)
    )
    row = cur.fetchone()
    if not row:
        raise LookupError("Brand not found")
    if row.get("lifecycle") != "active":
        raise PermissionError("Brand project lifecycle is not active")
    if row.get("state") not in {"scaffolded", "building", "preview", "staged", "live"}:
        raise PermissionError("Brand project is not eligible")
    return row


def _error(message, status):
    return jsonify({"ok": False, "error": message}), status


@content_calendar.get("/content/calendar")
def calendar_page():
    brand_filter = request.args.get("brand_id")
    if brand_filter == "":
        brand_filter = None
    if brand_filter not in (None, ""):
        try:
            brand_filter = int(brand_filter)
            if brand_filter <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error("brand_id must be an integer", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT cc.*, b.name AS brand_name, t.status AS task_status, t.result_ref "
                "FROM content_calendar cc JOIN brands b ON b.id=cc.brand_id "
                "LEFT JOIN tasks t ON t.id=cc.task_id "
                "WHERE (%s IS NULL OR cc.brand_id=%s) ORDER BY cc.planned_date NULLS LAST, cc.id DESC LIMIT 200",
                (brand_filter, brand_filter),
            )
            plans = cur.fetchall()
            cur.execute(
                "SELECT b.id, b.name FROM brands b JOIN projects p ON p.id=b.project_id "
                "WHERE p.lifecycle='active' ORDER BY b.name"
            )
            brands = cur.fetchall()
            available = True
        except errors.UndefinedTable:
            plans, brands, available = [], [], False
    finally:
        conn.close()
    return render_template("content_calendar.html", plans=plans, brands=brands, available=available, brand_filter=brand_filter)


@content_calendar.post("/content/calendar")
def create_calendar_plan():
    try:
        plan = _plan_payload(_payload())
    except ValueError as exc:
        return _error(str(exc), 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            brand = _eligible_brand(cur, plan["brand_id"])
            if plan["evidence_audit_id"]:
                cur.execute("SELECT id FROM audits WHERE id=%s AND brand_id=%s", (plan["evidence_audit_id"], plan["brand_id"]))
                if not cur.fetchone():
                    raise ValueError("evidence_audit_id does not belong to brand")
            cur.execute(
                "INSERT INTO content_calendar "
                "(brand_id,title,target_keyword,audience,hypothesis,success_metric,planned_date,competitor_urls,evidence_audit_id,evidence_note) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
                (plan["brand_id"], plan["title"], plan["target_keyword"], plan["audience"], plan["hypothesis"], plan["success_metric"], plan["planned_date"], json.dumps(plan["competitor_urls"]), plan["evidence_audit_id"], plan["evidence_note"]),
            )
            row = cur.fetchone()
            conn.commit()
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Content calendar is not installed", 503)
        except (LookupError, PermissionError, ValueError) as exc:
            conn.rollback()
            return _error(str(exc), 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, PermissionError) else 400)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"ok": True, "id": row["id"], "brand_id": brand["id"]}), 201


@content_calendar.post("/content/calendar/<int:calendar_id>/research")
def queue_calendar_research(calendar_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM content_calendar WHERE id=%s FOR UPDATE", (calendar_id,))
            plan = cur.fetchone()
            if not plan:
                return _error("Calendar plan not found", 404)
            if plan.get("status") == "cancelled":
                return _error("Cancelled plans cannot start research", 409)
            if plan.get("task_id"):
                conn.rollback()
                return jsonify({"ok": True, "task_id": plan["task_id"], "deduplicated": True})
            if plan.get("status") != "planned":
                conn.rollback()
                return _error("Only planned calendar items can start research", 409)
            brand = _eligible_brand(cur, plan["brand_id"], lock=True)
            urls = _urls(plan.get("competitor_urls") or [])
            if not 1 <= len(urls) <= 5:
                conn.rollback()
                return _error("Research requires 1-5 public HTTPS competitor URLs", 400)
            params = {"brand_id": plan["brand_id"], "project_id": brand["project_id"], "title": plan["title"], "target_keyword": plan["target_keyword"], "competitor_urls": urls, "calendar_id": calendar_id, "source": "content-calendar", "planning_context": {"audience": plan.get("audience") or "", "hypothesis": plan.get("hypothesis") or "", "success_metric": plan.get("success_metric") or "", "evidence_audit_id": plan.get("evidence_audit_id"), "evidence_note": plan.get("evidence_note") or ""}}
            cur.execute("INSERT INTO tasks (type,status,params,triggered_by) VALUES ('content_research','queued',%s,'content-calendar') RETURNING id", (json.dumps(params),))
            task = cur.fetchone()
            cur.execute("UPDATE content_calendar SET task_id=%s,status='research_queued',updated_at=now() WHERE id=%s", (task["id"], calendar_id))
            conn.commit()
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Content calendar is not installed", 503)
        except (LookupError, PermissionError, ValueError) as exc:
            conn.rollback()
            return _error(str(exc), 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, PermissionError) else 400)
    finally:
        conn.close()
    return jsonify({"ok": True, "task_id": task["id"], "deduplicated": False}), 201


@content_calendar.post("/content/calendar/<int:calendar_id>/sources")
def update_calendar_sources(calendar_id):
    """Update only competitor sources while a plan remains safely editable."""
    data = _payload()
    try:
        urls = _urls(data.get("competitor_urls"))
    except ValueError as exc:
        return _error(str(exc), 400)
    if not 1 <= len(urls) <= 5:
        return _error("Sources require 1-5 public HTTPS competitor URLs", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, brand_id, status FROM content_calendar WHERE id=%s FOR UPDATE", (calendar_id,))
            plan = cur.fetchone()
            if not plan:
                conn.rollback()
                return _error("Calendar plan not found", 404)
            if plan.get("status") != "planned":
                conn.rollback()
                return _error("Only planned calendar items can update sources", 409)
            _eligible_brand(cur, plan["brand_id"], lock=True)
            cur.execute("UPDATE content_calendar SET competitor_urls=%s::jsonb, updated_at=now() WHERE id=%s AND status='planned'", (json.dumps(urls), calendar_id))
            conn.commit()
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Content calendar is not installed", 503)
        except (LookupError, PermissionError) as exc:
            conn.rollback()
            return _error(str(exc), 404 if isinstance(exc, LookupError) else 409)
    finally:
        conn.close()
    return jsonify({"ok": True, "id": calendar_id, "competitor_urls": urls})


@content_calendar.post("/content/calendar/<int:calendar_id>/cancel")
def cancel_calendar_plan(calendar_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            cur.execute("UPDATE content_calendar SET status='cancelled',updated_at=now() WHERE id=%s AND status='planned' RETURNING id", (calendar_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return _error("Only planned calendar items can be cancelled", 409)
            conn.commit()
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Content calendar is not installed", 503)
    finally:
        conn.close()
    return jsonify({"ok": True, "id": row["id"]})
