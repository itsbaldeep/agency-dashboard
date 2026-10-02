"""Bounded, approval-free content planning and research kickoff routes."""

import ipaddress
import json
from datetime import date
from urllib.parse import urlparse

from flask import Blueprint, jsonify, redirect, render_template, request
from psycopg2 import errors

import models


content_calendar = Blueprint("content_calendar", __name__)
SUCCESS_METRICS = {"gsc_clicks", "ga4_users", "ga4_key_events"}
ACTIVE_CONTENT_STATUSES = ("outline", "draft", "approved", "needs_publish_input", "publish_failed")


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


def _calendar_success(payload, status, brand_id):
    if request.accept_mimetypes.accept_html and not request.accept_mimetypes.accept_json:
        return redirect("/content/calendar?brand_id=%s" % brand_id)
    return jsonify(payload), status


def _active_content_count(cur, brand_id, kind=None):
    """Count active plans without counting a calendar plan and its linked outline twice."""
    calendar_kind = "" if kind not in {"help", "article"} else (" AND " + ("cc.evidence_note ~ '\"kind\"\\s*:\\s*\"help\"'" if kind == "help" else "cc.evidence_note !~ '\"kind\"\\s*:\\s*\"help\"'"))
    item_kind = "" if kind not in {"help", "article"} else (" AND COALESCE(ci.structured->>'content_kind', ci.content_type, 'article')=%s")
    params = [brand_id]
    if calendar_kind and kind == "help": pass
    params.append(brand_id)
    if item_kind: params.append(kind)
    params.append(brand_id)
    cur.execute(("""SELECT count(*) AS total FROM (
        SELECT cc.id FROM content_calendar cc WHERE cc.brand_id=%s AND cc.status IN ('planned','research_queued')""" + calendar_kind + """
          AND NOT EXISTS (
            SELECT 1 FROM content_items linked
            WHERE linked.brand_id=cc.brand_id
              AND NULLIF(linked.structured->>'calendar_id','')::int=cc.id
              AND linked.status NOT IN ('outline','draft','approved','needs_publish_input','publish_failed')
          )
        UNION ALL SELECT ci.id FROM content_items ci WHERE ci.brand_id=%s AND ci.status IN ('outline','draft','approved','needs_publish_input','publish_failed')""" + item_kind + """
          AND NOT EXISTS (
            SELECT 1 FROM content_calendar cc
            WHERE cc.id=NULLIF(ci.structured->>'calendar_id','')::int
              AND cc.brand_id=%s AND cc.status IN ('planned','research_queued')
          )
    ) active"""), tuple(params))
    row = cur.fetchone() or {}
    return int(row.get("total") or 0)


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
    candidates, help_candidates, analysis_task, existing_content = [], [], None, []
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
            if brand_filter:
                # Load the complete linked set for publication status derivation.
                # The selected brand's content panel is intentionally brand-only.
                cur.execute("SELECT id, title, status, content_type, structured, updated_at FROM content_items WHERE brand_id=%s AND status IN ('outline','draft','approved','needs_publish_input','publish_failed','published') ORDER BY updated_at DESC NULLS LAST, id DESC", (brand_filter,))
                existing_content = cur.fetchall()
            published_calendar_ids = set()
            for item in existing_content:
                structured = item.get("structured") if isinstance(item.get("structured"), dict) else {}
                calendar_id = structured.get("calendar_id")
                item["calendar_id"] = calendar_id
                item["content_kind"] = structured.get("content_kind") or item.get("content_type") or "article"
                if item.get("status") == "published" and calendar_id is not None:
                    published_calendar_ids.add(str(calendar_id))
            for plan in plans:
                plan["display_status"] = "published" if str(plan.get("id")) in published_calendar_ids else plan.get("status")
            cur.execute(
                "SELECT b.id, b.name FROM brands b JOIN projects p ON p.id=b.project_id "
                "WHERE p.lifecycle='active' ORDER BY b.name"
            )
            brands = cur.fetchall()
            available = True
            candidates, help_candidates, analysis_task = [], [], None
            if brand_filter:
                cur.execute("SELECT id, audit_id, kind, title, target_keyword, rank, rationale, evidence, status, created_at FROM growth_recommendations WHERE brand_id=%s AND status NOT IN ('dismissed','published') ORDER BY kind, rank, id", (brand_filter,))
                recommendations = cur.fetchall()
                for item in recommendations:
                    evidence = item.get("evidence") or {}
                    if isinstance(evidence, str):
                        try: evidence = json.loads(evidence)
                        except json.JSONDecodeError: evidence = {}
                    row = {"id": item.get("id"), "title": item.get("title"), "target_keyword": item.get("target_keyword") or "", "reason": item.get("rationale") or "", "priority": "high" if item.get("rank") == 1 else "medium", "evidence_at": item.get("created_at"), "audit_id": item.get("audit_id"), "evidence": evidence, "kind": item.get("kind")}
                    (help_candidates if item.get("kind") == "help" else candidates).append(row)
                cur.execute("SELECT id, status FROM tasks WHERE type='growth_plan' AND params->>'brand_id'=%s ORDER BY id DESC LIMIT 1", (str(brand_filter),))
                analysis_task = cur.fetchone()
        except errors.UndefinedTable:
            plans, brands, available = [], [], False
            candidates, help_candidates, analysis_task = [], [], None
    finally:
        conn.close()
    return render_template("content_calendar.html", plans=plans, brands=brands, available=available, brand_filter=brand_filter, candidates=candidates, help_candidates=help_candidates, analysis_task=analysis_task, existing_content=existing_content)


@content_calendar.post("/content/calendar/generate")
def generate_next_outline():
    """Force a fresh SEO measurement, then let growth_generate choose and research the next bounded topic."""
    try:
        brand_id = int(request.form.get("brand_id"))
    except (TypeError, ValueError):
        return _error("A brand is required", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            brand = _eligible_brand(cur, brand_id, lock=True)
            cur.execute("SELECT id, params FROM tasks WHERE type='seo_measurement' AND status IN ('queued','running') AND params->>'brand_id'=%s ORDER BY id DESC LIMIT 1", (str(brand_id),))
            existing = cur.fetchone()
            if existing:
                existing_params = existing.get("params") if isinstance(existing, dict) else None
                if isinstance(existing_params, str):
                    try:
                        existing_params = json.loads(existing_params)
                    except (TypeError, ValueError):
                        existing_params = {}
                if isinstance(existing_params, dict) and existing_params.get("followup") == "growth_generate" and existing_params.get("queue_research") is True and existing_params.get("operator_authorized") is True:
                    conn.rollback()
                    return jsonify({"ok": True, "task_id": existing["id"], "deduplicated": True})
                conn.rollback()
                return _error("A regular SEO refresh is already running. Retry Generate fresh outline after it completes", 409)
            cur.execute("SELECT property_type, value FROM brand_properties WHERE brand_id=%s", (brand_id,))
            properties = {row.get("property_type"): row.get("value") for row in cur.fetchall()}
            domain = str(properties.get("domain") or "").strip()
            if not domain:
                conn.rollback()
                return _error("A domain is required before generating an outline", 409)
            url = domain if domain.startswith(("http://", "https://")) else "https://" + domain
            params = {"brand_id": brand_id, "project_id": brand.get("project_id"), "url": url,
                      "source": "content-calendar", "followup": "growth_generate",
                      "queue_research": True, "operator_authorized": True,
                      "requires_review": True}
            cur.execute("INSERT INTO tasks (type,status,params,triggered_by) VALUES ('seo_measurement','queued',%s,'content-calendar') RETURNING id", (json.dumps(params),))
            task_id = cur.fetchone()["id"]
            conn.commit()
            return jsonify({"ok": True, "task_id": task_id, "deduplicated": False, "pipeline": "fresh-seo→growth_generate→research→outline"}), 201
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Growth recommendation storage is not installed", 503)
        except (LookupError, PermissionError) as exc:
            conn.rollback()
            return _error(str(exc), 404 if isinstance(exc, LookupError) else 409)
    finally:
        conn.close()


@content_calendar.post("/content/calendar/analyze")
def analyze_next_content():
    """Queue the worker's bounded article/help recommendation refresh."""
    try:
        brand_id = int(request.form.get("brand_id"))
    except (TypeError, ValueError):
        return _error("A brand is required", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            brand = _eligible_brand(cur, brand_id, lock=True)
        except LookupError:
            return _error("Brand not found", 404)
        except PermissionError as exc:
            return _error(str(exc), 409)
        cur.execute("SELECT id FROM tasks WHERE type='seo_measurement' AND status IN ('queued','running') AND params->>'brand_id'=%s ORDER BY id DESC LIMIT 1", (str(brand_id),))
        existing = cur.fetchone()
        if existing:
            return jsonify({"ok": True, "task_id": existing["id"], "deduplicated": True})
        cur.execute("SELECT property_type, value FROM brand_properties WHERE brand_id=%s", (brand_id,))
        properties = {row.get("property_type"): row.get("value") for row in cur.fetchall()}
        domain = str(properties.get("domain") or "").strip()
        if not domain:
            return _error("A domain is required before refreshing SEO evidence", 409)
        url = domain if domain.startswith("http") else "https://" + domain
        params = {"brand_id": brand_id, "project_id": brand.get("project_id"), "url": url, "source": "content-calendar", "followup": "growth_plan", "requires_review": True}
        cur.execute("INSERT INTO tasks (type,status,params,triggered_by) VALUES ('seo_measurement','queued',%s,'content-calendar') RETURNING id", (json.dumps(params),))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id, "deduplicated": False}), 201
    except errors.UndefinedTable:
        conn.rollback()
        return _error("Growth recommendation storage is not installed", 503)
    finally:
        conn.close()


@content_calendar.post("/content/calendar/recommendation/<int:recommendation_id>/plan")
def plan_recommendation(recommendation_id):
    """Convert one current recommendation into a manually scheduled plan."""
    payload = _payload()
    try:
        planned_date = date.fromisoformat(str(payload.get("planned_date")))
    except (TypeError, ValueError):
        return _error("planned_date must be YYYY-MM-DD", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, brand_id, audit_id, kind, title, target_keyword, rationale, evidence, status FROM growth_recommendations WHERE id=%s FOR UPDATE", (recommendation_id,))
        recommendation = cur.fetchone()
        if not recommendation or recommendation.get("status") in {"dismissed", "published", "scheduled"}:
            return _error("Recommendation is stale or unavailable", 409)
        try:
            brand = _eligible_brand(cur, recommendation["brand_id"], lock=True)
        except LookupError:
            return _error("Brand not found", 404)
        except PermissionError as exc:
            return _error(str(exc), 409)
        cur.execute("SELECT id FROM audits WHERE brand_id=%s AND audit_type='seo_measurement' ORDER BY created_at DESC LIMIT 1", (recommendation["brand_id"],))
        latest_audit = cur.fetchone()
        if recommendation.get("audit_id") and (not latest_audit or recommendation.get("audit_id") != latest_audit.get("id")):
            return _error("Recommendation is based on stale SEO evidence; refresh the queue first", 409)
        lane = recommendation.get("kind") if recommendation.get("kind") in {"help", "article"} else "article"
        if _active_content_count(cur, recommendation["brand_id"], lane) >= 3:
            return _error("The active %s queue already has three items" % lane, 409)
        evidence = recommendation.get("evidence") or {}
        if isinstance(evidence, str):
            try: evidence = json.loads(evidence)
            except json.JSONDecodeError: evidence = {}
        candidate_urls = evidence.get("competitor_urls") or evidence.get("sources") or [] if isinstance(evidence, dict) else []
        try:
            candidate_urls = _urls(candidate_urls)
        except ValueError:
            candidate_urls = []
        note = json.dumps({"recommendation_id": recommendation_id, "kind": recommendation.get("kind"), "evidence": evidence}, separators=(",", ":"))
        cur.execute("""INSERT INTO content_calendar (brand_id,title,target_keyword,audience,hypothesis,success_metric,planned_date,competitor_urls,evidence_audit_id,evidence_note)
            VALUES (%s,%s,%s,%s,%s,'gsc_clicks',%s,%s::jsonb,%s,%s) RETURNING id""", (recommendation["brand_id"], recommendation["title"], recommendation.get("target_keyword") or recommendation["title"], "TrueApply users", recommendation.get("rationale") or "Evidence-backed recommendation", planned_date, json.dumps(candidate_urls), recommendation.get("audit_id"), note))
        plan = cur.fetchone()
        cur.execute("UPDATE growth_recommendations SET status='scheduled', human_snapshot=%s, updated_at=now() WHERE id=%s", (json.dumps({"calendar_id": plan["id"], "planned_date": planned_date.isoformat()}), recommendation_id))
        conn.commit()
        return jsonify({"ok": True, "calendar_id": plan["id"], "recommendation_id": recommendation_id}), 201
    except errors.UndefinedTable:
        conn.rollback()
        return _error("Growth recommendation storage is not installed", 503)
    finally:
        conn.close()


@content_calendar.post("/content/calendar/content/<int:content_item_id>/schedule")
def schedule_existing_content(content_item_id):
    """Attach one existing reviewed outline/draft to a planning date without research or publication."""
    payload = _payload()
    try:
        planned_date = date.fromisoformat(str(payload.get("planned_date")))
    except (TypeError, ValueError):
        return _error("planned_date must be YYYY-MM-DD", 400)
    conn = models.db()
    try:
        cur = conn.cursor()
        try:
            cur.execute("""SELECT ci.id, ci.brand_id, ci.title, ci.content_type, ci.status, ci.structured,
                                  b.project_id, p.lifecycle, p.state
                           FROM content_items ci JOIN brands b ON b.id=ci.brand_id
                           JOIN projects p ON p.id=b.project_id
                           WHERE ci.id=%s FOR UPDATE""", (content_item_id,))
            item = cur.fetchone()
            if not item:
                return _error("Content item not found", 404)
            if item.get("status") not in {"outline", "draft", "approved", "needs_publish_input", "publish_failed"}:
                return _error("Only an outline or draft can be scheduled", 409)
            _eligible_brand(cur, item["brand_id"], lock=True)
            structured = item.get("structured") or {}
            if isinstance(structured, str):
                try:
                    structured = json.loads(structured)
                except (TypeError, ValueError):
                    structured = {}
            if not isinstance(structured, dict):
                structured = {}
            kind = structured.get("content_kind") or item.get("content_type") or "article"
            calendar_id = structured.get("calendar_id")
            if calendar_id:
                try:
                    calendar_id = int(calendar_id)
                except (TypeError, ValueError):
                    calendar_id = None
            if calendar_id:
                cur.execute("SELECT id, brand_id, status, task_id FROM content_calendar WHERE id=%s FOR UPDATE", (calendar_id,))
                calendar = cur.fetchone()
                if calendar and calendar.get("brand_id") == item["brand_id"]:
                    if calendar.get("status") == "cancelled" and calendar.get("task_id"):
                        return _error("The linked calendar item already has research; schedule its existing workflow instead", 409)
                    if calendar.get("task_id"):
                        if calendar.get("status") != "research_queued":
                            return _error("The linked calendar item is not in a schedulable state", 409)
                        cur.execute("UPDATE content_calendar SET planned_date=%s, updated_at=now() WHERE id=%s AND status='research_queued'", (planned_date, calendar_id))
                    else:
                        if calendar.get("status") not in {"planned", "cancelled"}:
                            return _error("The linked calendar item is not in a schedulable state", 409)
                        cur.execute("UPDATE content_calendar SET planned_date=%s, status='planned', updated_at=now() WHERE id=%s AND status IN ('planned','cancelled')", (planned_date, calendar_id))
                    if getattr(cur, "rowcount", 1) != 1:
                        conn.rollback()
                        return _error("The linked calendar item could not be updated", 409)
                    conn.commit()
                    return _calendar_success({"ok": True, "calendar_id": calendar_id, "content_item_id": content_item_id, "deduplicated": True}, 200, item["brand_id"])
            planning = structured.get("planning_context") if isinstance(structured.get("planning_context"), dict) else {}
            audience = str(planning.get("audience") or "TrueApply users")[:1000]
            hypothesis = str(planning.get("hypothesis") or "Schedule the existing reviewed content for a later editorial decision")[:2000]
            metric = planning.get("success_metric") if planning.get("success_metric") in SUCCESS_METRICS else "gsc_clicks"
            target = str(structured.get("target_keyword") or item.get("title") or "content")[:200]
            evidence_audit_id = structured.get("evidence_audit_id")
            if not evidence_audit_id and structured.get("research_id"):
                cur.execute("""SELECT t.params->>'audit_id' AS audit_id
                               FROM content_research cr JOIN tasks t ON t.id=cr.task_id
                               WHERE cr.id=%s""", (structured.get("research_id"),))
                research_source = cur.fetchone()
                if research_source and research_source.get("audit_id"):
                    try:
                        evidence_audit_id = int(research_source["audit_id"])
                    except (TypeError, ValueError):
                        evidence_audit_id = None
            note = json.dumps({"content_item_id": content_item_id, "kind": kind, "source": "existing-content-schedule"}, separators=(",", ":"))
            cur.execute("""INSERT INTO content_calendar
                (brand_id,title,target_keyword,audience,hypothesis,success_metric,planned_date,competitor_urls,evidence_audit_id,evidence_note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb,%s,%s) RETURNING id""",
                        (item["brand_id"], item["title"], target, audience, hypothesis, metric, planned_date, evidence_audit_id, note))
            calendar_id = cur.fetchone()["id"]
            structured["calendar_id"] = calendar_id
            cur.execute("UPDATE content_items SET structured=%s::jsonb, updated_at=now() WHERE id=%s", (json.dumps(structured), content_item_id))
            conn.commit()
            return _calendar_success({"ok": True, "calendar_id": calendar_id, "content_item_id": content_item_id, "deduplicated": False}, 201, item["brand_id"])
        except errors.UndefinedTable:
            conn.rollback()
            return _error("Content calendar is not installed", 503)
        except (LookupError, PermissionError) as exc:
            conn.rollback()
            return _error(str(exc), 404 if isinstance(exc, LookupError) else 409)
    finally:
        conn.close()


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
            if _active_content_count(cur, plan["brand_id"], "article") >= 3:
                raise ValueError("The active article queue already has three items")
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
            cur.execute("""SELECT id FROM content_items
                           WHERE brand_id=%s
                             AND NULLIF(structured->>'calendar_id','')::int=%s
                           LIMIT 1""", (plan["brand_id"], calendar_id))
            if cur.fetchone():
                conn.rollback()
                return _error("This calendar item is already linked to existing content; research is not required", 409)
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
            planning_context = {"audience": plan.get("audience") or "", "hypothesis": plan.get("hypothesis") or "", "success_metric": plan.get("success_metric") or "", "evidence_audit_id": plan.get("evidence_audit_id"), "evidence_note": plan.get("evidence_note") or ""}
            try:
                note = json.loads(planning_context["evidence_note"] or "{}")
            except (TypeError, ValueError):
                note = {}
            params = {"brand_id": plan["brand_id"], "project_id": brand["project_id"], "title": plan["title"], "target_keyword": plan["target_keyword"], "competitor_urls": urls, "calendar_id": calendar_id, "source": "content-calendar", "content_kind": note.get("kind") if note.get("kind") in {"article", "help"} else "article", "planning_context": planning_context}
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
