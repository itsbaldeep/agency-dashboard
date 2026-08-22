#!/usr/bin/env python3
"""Agency OS Dashboard — rewritten from scratch with unified engagement model."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, send_file, make_response, send_from_directory
import markdown
import psycopg2.extras

import models

app = Flask(__name__)
TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"
app.jinja_loader.searchpath = [str(TEMPLATES)]

@app.template_filter('jsonloads')
def jsonloads_filter(val, key=None):
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return ''
    if isinstance(val, dict) and key:
        return val.get(key, '')
    if isinstance(val, dict):
        return val
    return ''


def dec_to_num(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def fmt_ts(v):
    if not v:
        return ""
    try:
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def fmt_dur(started, finished):
    if not started or not finished:
        return ""
    try:
        if isinstance(started, str):
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if isinstance(finished, str):
            finished = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except Exception:
        return ""
    s = int((finished - started).total_seconds())
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ── Dashboard / Overview ────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/dashboard/data")
def dashboard_data():
    data = models.get_overview()
    return render_template("fragments/overview.html", data=data)


# ── Engagements (unified projects + clients + brands) ───────────

@app.route("/engagements")
def engagements():
    return render_template("engagements.html")


@app.route("/engagements/data")
def engagements_data():
    engs = models.get_engagements()
    return render_template("fragments/engagement_list.html", engagements=engs)


@app.route("/engagements/<ref_type>/<int:ref_id>")
def engagement_detail(ref_type, ref_id):
    if ref_type not in ("client", "project", "brand"):
        return redirect("/engagements")
    eng = models.get_engagement_detail(ref_type, ref_id)
    if not eng:
        return redirect("/engagements")
    return render_template("engagement.html", eng=eng)


@app.route("/engagements/<ref_type>/<int:ref_id>/code")
def engagement_code(ref_type, ref_id):
    eng = models.get_engagement_detail(ref_type, ref_id)
    if not eng:
        return "", 404
    return render_template("fragments/engagement_code.html", eng=eng)


@app.route("/engagements/<ref_type>/<int:ref_id>/marketing")
def engagement_marketing(ref_type, ref_id):
    eng = models.get_engagement_detail(ref_type, ref_id)
    if not eng:
        return "", 404
    return render_template("fragments/engagement_marketing.html", eng=eng)


@app.route("/engagements/<ref_type>/<int:ref_id>/activity")
def engagement_activity(ref_type, ref_id):
    eng = models.get_engagement_detail(ref_type, ref_id)
    if not eng:
        return "", 404
    name = eng.get("client_name") or eng.get("name") or ""
    data = models.get_activity(ref_type, ref_id, name)
    return render_template("fragments/engagement_activity.html", **data)


def _parse_jsonb(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return {}
    return v or {}


@app.route("/engagements/brand/<int:brand_id>/report")
def brand_report(brand_id):
    conn = models.db()
    project_id = None
    agent_allowed = False
    repo_url = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id=%s", (brand_id,))
        brand = cur.fetchone()
        if not brand:
            return redirect("/engagements")
        project_id = brand.get("project_id")
        cur.execute("SELECT property_type, value FROM brand_properties WHERE brand_id=%s", (brand_id,))
        brand_properties = cur.fetchall()

        if project_id:
            cur.execute("SELECT agent_allowed, repo_url FROM projects WHERE id=%s", (project_id,))
            prow = cur.fetchone()
            if prow:
                agent_allowed = bool(prow.get("agent_allowed"))
                repo_url = prow.get("repo_url")

        cur.execute(
            "SELECT id, domain, name, scan_enabled, last_scanned_at, sitemap_url, "
            "(SELECT count(*) FROM competitor_pages cp WHERE cp.competitor_id=c.id) as page_count, "
            "(SELECT max(lastmod) FROM competitor_pages cp WHERE cp.competitor_id=c.id) as most_recent_page "
            "FROM competitors c WHERE c.brand_id=%s ORDER BY c.domain", (brand_id,))
        competitors = cur.fetchall()

        cur.execute("SELECT * FROM audits WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1", (brand_id,))
        audit = cur.fetchone()

        cur.execute(
            "SELECT id, audit_type, created_at, summary->>'brand_share_of_voice_pct' as vis_pct, "
            "summary->>'confidence' as confidence FROM audits WHERE brand_id=%s ORDER BY created_at DESC LIMIT 10",
            (brand_id,))
        audit_history = cur.fetchall()
    finally:
        conn.close()

    audit_summary = {}
    audit_sources = []
    if audit:
        audit_summary = _parse_jsonb(audit.get("summary"))
        audit_sources = _parse_jsonb(audit.get("sources")) or []
        if not isinstance(audit_sources, list):
            audit_sources = []

    capabilities = []
    if project_id:
        conn = models.db()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT capability, status, evidence, checked_at FROM capabilities "
                "WHERE project_id=%s ORDER BY capability", (project_id,))
            for row in cur.fetchall():
                row = dict(row)
                row["evidence"] = _parse_jsonb(row.get("evidence"))
                row["checked_fmt"] = fmt_ts(row.get("checked_at"))
                capabilities.append(row)
        except Exception:
            capabilities = []
        finally:
            conn.close()

    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, rationale, impact, effort, action_type, status, "
            "compliance_flags, sources, audit_id, brand_id, created_at "
            "FROM suggestions WHERE brand_id=%s ORDER BY impact DESC, created_at DESC", (brand_id,))
        suggestions = cur.fetchall()
        for sg in suggestions:
            sg["compliance_flags"] = _parse_jsonb(sg.get("compliance_flags")) or []
            if not isinstance(sg["compliance_flags"], list):
                sg["compliance_flags"] = []
            sg["sources"] = _parse_jsonb(sg.get("sources")) or []
        cur.execute(
            "SELECT id, title, status, content_type, created_at, updated_at, suggestion_id "
            "FROM content_items WHERE brand_id=%s ORDER BY updated_at DESC", (brand_id,))
        content_items = cur.fetchall()
        cur.execute(
            "SELECT id, type, status, created_at, finished_at, left(error,120) as error "
            "FROM tasks WHERE (params->>'brand_id')::text = %s "
            "ORDER BY created_at DESC LIMIT 10", (str(brand_id),))
        recent_tasks = cur.fetchall()
    except Exception:
        content_items = []
        recent_tasks = []
    finally:
        conn.close()

    sug_ids = [s["id"] for s in suggestions]
    content_by_suggestion = {}
    for ci in content_items:
        sid = ci.get("suggestion_id")
        if sid and sid not in content_by_suggestion:
            content_by_suggestion[sid] = ci

    task_by_suggestion = {}
    if sug_ids:
        conn = models.db()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, type, status, params->>'suggestion_id' as sug_id "
                "FROM tasks WHERE params->>'suggestion_id' = ANY(%s) "
                "ORDER BY id DESC", (list(map(str, sug_ids)),))
            for t in cur.fetchall():
                sid = t.get("sug_id")
                try:
                    sid = int(sid) if sid else None
                except (ValueError, TypeError):
                    sid = None
                if sid and sid not in task_by_suggestion:
                    task_by_suggestion[sid] = t
        except Exception:
            pass
        finally:
            conn.close()

    visibility_rows = []
    ch_error = False
    try:
        cols, rows = models.ch_query(
            "SELECT prompt, cited, position, competitors_cited, detail, ts "
            "FROM default.ai_visibility_checks "
            f"WHERE brand_id = {int(brand_id)} "
            "ORDER BY ts DESC LIMIT 50 "
            "FORMAT TabSeparatedWithNames"
        )
        visibility_rows = [dict(zip(cols, row)) for row in rows] if cols else []
    except Exception:
        ch_error = True

    domain = audit_summary.get("domain") or ""
    if not domain:
        for p in brand_properties:
            if p.get("property_type") == "domain" and p.get("value"):
                domain = p["value"]
                break

    audit_date_fmt = ""
    if audit and audit.get("created_at"):
        try:
            d = audit["created_at"]
            if isinstance(d, str):
                d = datetime.fromisoformat(d.replace("Z", "+00:00"))
            audit_date_fmt = d.strftime("%b %d, %Y %H:%M")
        except Exception:
            audit_date_fmt = str(audit["created_at"])

    for a in audit_history:
        a["created_fmt"] = fmt_ts(a.get("created_at"))

    for c in competitors:
        c["last_scanned_fmt"] = fmt_ts(c.get("last_scanned_at"))
        if c.get("most_recent_page"):
            try:
                d = c["most_recent_page"]
                if isinstance(d, str):
                    d = datetime.fromisoformat(d.replace("Z", "+00:00"))
                c["most_recent_page_fmt"] = d.strftime("%b %d, %Y")
            except Exception:
                c["most_recent_page_fmt"] = str(c.get("most_recent_page"))

    for t in recent_tasks:
        t["created_fmt"] = fmt_ts(t.get("created_at"))

    for ci in content_items:
        ci["updated_fmt"] = fmt_ts(ci.get("updated_at"))

    return render_template("brand_report.html", active='engagements',
                           brand=brand, brand_properties=brand_properties,
                           domain=domain, competitors=competitors, audit=audit,
                           audit_summary=audit_summary, audit_sources=audit_sources,
                           audit_history=audit_history, audit_date_fmt=audit_date_fmt,
                           suggestions=suggestions, visibility_rows=visibility_rows,
                           ch_error=ch_error, capabilities=capabilities,
                           content_items=content_items, recent_tasks=recent_tasks,
                           content_by_suggestion=content_by_suggestion,
                           task_by_suggestion=task_by_suggestion,
                           agent_allowed=agent_allowed, repo_url=repo_url,
                           project_id=project_id,
                           summary_json=json.dumps(audit_summary, indent=2, default=str) if audit_summary else "")


# ── Client onboard (create engagement) ──────────────────────────

@app.route("/onboard", methods=["POST"])
def onboard_client():
    ctype = request.form.get("type", "").strip()
    user_input = request.form.get("input", "").strip()
    client_name = request.form.get("name", "").strip() or (
        user_input.split(".")[0].title() if ctype == "black_box" else user_input[:50])

    if not ctype or ctype not in ("marketing_only", "existing_code_marketing", "clean_slate"):
        return jsonify({"ok": False, "error": "valid type required"}), 400
    if not user_input:
        return jsonify({"ok": False, "error": "input required"}), 400

    # Map to legacy types
    legacy_map = {"marketing_only": "black_box", "existing_code_marketing": "import_repo", "clean_slate": "new_project"}
    legacy_type = legacy_map[ctype]

    # Optional enrichment fields (name + URL is enough; these are gravy)
    f_industry = request.form.get("industry", "").strip()
    f_target_market = request.form.get("target_market", "").strip()
    f_target_audience = request.form.get("target_audience", "").strip()
    f_business_stage = request.form.get("business_stage", "").strip()
    f_sales_channel = request.form.get("primary_sales_channel", "").strip()
    f_description = request.form.get("description", "").strip()
    f_competitors_raw = request.form.get("competitors", "").strip()

    def _add_brand_property(cur, bid, ptype, value):
        if not value:
            return
        cur.execute(
            "INSERT INTO brand_properties (brand_id, property_type, value, accessible) "
            "VALUES (%s, %s, %s, true) ON CONFLICT DO NOTHING",
            (bid, ptype, value))

    def _insert_competitors(cur, bid, raw):
        if not raw:
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Parse "domain" or "name,domain"
            if "," in line:
                cname, cdomain = line.split(",", 1)
                cname, cdomain = cname.strip(), cdomain.strip().lower()
            else:
                cname, cdomain = "", line.strip().lower()
            cdomain = (cdomain.removeprefix("https://").removeprefix("http://")
                              .removeprefix("www."))
            if not cdomain or not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", cdomain):
                continue
            if not cname:
                cname = cdomain.split(".")[0].title()
            cur.execute(
                "INSERT INTO competitors (brand_id, domain, name, scan_enabled) "
                "VALUES (%s, %s, %s, false) ON CONFLICT DO NOTHING",
                (bid, cdomain, cname))

    def _enrich_brand(cur, bid):
        _add_brand_property(cur, bid, "category", f_industry)
        _add_brand_property(cur, bid, "target_market", f_target_market)
        _add_brand_property(cur, bid, "target_audience", f_target_audience)
        _add_brand_property(cur, bid, "business_stage", f_business_stage)
        _add_brand_property(cur, bid, "primary_sales_channel", f_sales_channel)
        if f_description:
            _add_brand_property(cur, bid, "description", f_description)
        _insert_competitors(cur, bid, f_competitors_raw)

    try:
        conn = models.db()
        cur = conn.cursor()
        brand_id = None

        if ctype == "marketing_only":
            domain = user_input
            slug = re.sub(r'[^a-z0-9]+', '-', domain.split(".")[0].lower()).strip('-')
            cur.execute("INSERT INTO brands (name, slug, access_tier) VALUES (%s, %s, '0') ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name RETURNING id", (client_name, slug))
            brand_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO brand_properties (brand_id, property_type, value, accessible) VALUES (%s, 'domain', %s, true) ON CONFLICT DO NOTHING", (brand_id, domain))
            _enrich_brand(cur, brand_id)
            intake = json.dumps({"domain": domain})
            cur.execute("INSERT INTO clients (name, type, status, brand_id, intake_params) VALUES (%s, 'black_box', 'queued', %s, %s) RETURNING id", (client_name, brand_id, intake))
            client_id = cur.fetchone()["id"]
            task_params = json.dumps({"domain": domain, "brand_id": brand_id})
            cur.execute("INSERT INTO tasks (type, params) VALUES ('run_brand_audit', %s) RETURNING id", (task_params,))
            task_id = cur.fetchone()["id"]
            conn.commit()
            models.ch_trace({"project": "clients", "actor": "human", "action": "client_black_box_created",
                             "detail": f"Client {client_id}, brand {brand_id}, task {task_id} for {domain}",
                             "gate": "green", "decision": "proceed", "ok": 1})
            return jsonify({"ok": True, "client_id": client_id, "brand_id": brand_id, "task_id": task_id,
                            "domain": domain, "type": "marketing_only", "status": "queued"})

        elif ctype == "existing_code_marketing":
            intake = json.dumps({"repo_url": user_input})
            cur.execute("INSERT INTO clients (name, type, status, intake_params) VALUES (%s, 'import_repo', 'queued', %s) RETURNING id", (client_name, intake))
            client_id = cur.fetchone()["id"]
            task_params = json.dumps({"client_id": client_id, "repo_url": user_input})
            cur.execute("INSERT INTO tasks (type, params) VALUES ('client_import_repo', %s) RETURNING id", (task_params,))
            task_id = cur.fetchone()["id"]
            conn.commit()
            models.ch_trace({"project": "clients", "actor": "human", "action": "client_import_repo_created",
                             "detail": f"Client {client_id}, task {task_id} for repo {user_input}",
                             "gate": "green", "decision": "proceed", "ok": 1})
            return jsonify({"ok": True, "client_id": client_id, "task_id": task_id,
                            "type": "existing_code_marketing", "status": "queued"})

        elif ctype == "clean_slate":
            brief = user_input[:500]
            intake = json.dumps({"brief": brief})
            cur.execute("INSERT INTO clients (name, type, status, intake_params) VALUES (%s, 'new_project', 'queued', %s) RETURNING id", (client_name, intake))
            client_id = cur.fetchone()["id"]
            task_params = json.dumps({"client_id": client_id, "brief": brief})
            cur.execute("INSERT INTO tasks (type, params) VALUES ('client_new_project', %s) RETURNING id", (task_params,))
            task_id = cur.fetchone()["id"]
            conn.commit()
            models.ch_trace({"project": "clients", "actor": "human", "action": "client_new_project_created",
                             "detail": f"Client {client_id}, task {task_id}: {brief[:80]}",
                             "gate": "green", "decision": "proceed", "ok": 1})
            return jsonify({"ok": True, "client_id": client_id, "task_id": task_id,
                            "type": "clean_slate", "status": "queued"})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


# ── Projects (onboard + list) ───────────────────────────────────

@app.route("/projects")
def projects():
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, repo_name, base_branch, agent_allowed FROM projects ORDER BY id DESC")
        rows = cur.fetchall()
    finally:
        conn.close()
    return render_template("projects.html", projects=rows)


@app.route("/projects/<int:project_id>")
def project_detail(project_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM projects WHERE id=%s", (project_id,))
        project = cur.fetchone()
        if not project:
            return redirect("/projects")

        caps = None
        try:
            cur.execute("SELECT capability, status, checked_at, evidence FROM capabilities WHERE project_id=%s ORDER BY capability", (project_id,))
            caps = cur.fetchall()
        except Exception:
            caps = None

        latest_audit = None
        try:
            cur.execute(
                "SELECT result_ref FROM tasks WHERE type='defend_audit' AND status='done' "
                "AND (params->>'project_id')::int=%s ORDER BY id DESC LIMIT 1", (project_id,))
            latest_audit = cur.fetchone()
        except Exception:
            latest_audit = None

        content_items = None
        try:
            cur.execute(
                "SELECT ci.title, ci.content_type AS type, ci.status, ci.created_at, ci.id "
                "FROM content_items ci JOIN brands b ON b.project_id=%s WHERE ci.brand_id=b.id",
                (project_id,))
            content_items = cur.fetchall()
        except Exception:
            content_items = None

        # Multi-stage content pipeline: awaiting composition = outlines not yet
        # composed, so the human can inspect the plan before the spend gate.
        outlines = []
        try:
            cur.execute(
                "SELECT ci.id, ci.title, ci.status, ci.structured, ci.created_at, "
                "       t.id AS compose_task_id, t.status AS compose_status "
                "FROM content_items ci JOIN brands b ON b.project_id=%s AND ci.brand_id=b.id "
                "LEFT JOIN tasks t ON t.type='content_compose' AND (t.params->>'content_item_id')::int=ci.id "
                "WHERE ci.status='outline' ORDER BY ci.id DESC",
                (project_id,))
            for o in cur.fetchall():
                o["blocks"] = (o.get("structured") or {}).get("blocks") or []
                outlines.append(o)
        except Exception:
            outlines = []

        recent_research = None
        try:
            cur.execute(
                "SELECT cr.id, cr.target_keyword, cr.gaps, cr.strongest, cr.elements, cr.created_at, "
                "       (SELECT t.id FROM tasks t WHERE t.type='content_outline' "
                "        AND (t.params->>'research_id')::int=cr.id ORDER BY t.id DESC LIMIT 1) AS outline_task_id "
                "FROM content_research cr ORDER BY cr.id DESC LIMIT 5")
            recent_research = cur.fetchall()
        except Exception:
            recent_research = None

        dev_activity = None
        try:
            cur.execute(
                "SELECT id, status, COALESCE(params->>'spec', params->>'description', "
                "params->>'prompt', '') AS gist FROM tasks "
                "WHERE type IN ('propose_fix','agent_task') AND params->>'repo'=%s "
                "ORDER BY id DESC LIMIT 10", (project.get("repo_name"),))
            dev_activity = cur.fetchall()
        except Exception:
            dev_activity = None

        pending = None
        try:
            cur.execute(
                "SELECT s.id, s.title, s.status FROM suggestions s "
                "WHERE s.status='pending' AND s.brand_id IN (SELECT id FROM brands WHERE project_id=%s)",
                (project_id,))
            pending = cur.fetchall()
        except Exception:
            pending = None
    finally:
        conn.close()
    return render_template("project_detail.html", project=project, caps=caps,
                           latest_audit=latest_audit, content_items=content_items,
                           dev_activity=dev_activity, pending=pending,
                           outlines=outlines, recent_research=recent_research)


@app.route("/projects/onboard", methods=["POST"])
def project_onboard():
    repo_name = request.form.get("repo_name", "").strip()
    if not re.fullmatch(r"[a-z0-9-]+", repo_name):
        return jsonify({"ok": False, "error": "repo_name must match ^[a-z0-9-]+$"}), 400
    params = json.dumps({
        "repo_name": repo_name,
        "git_url": request.form.get("git_url", "").strip(),
        "github_owner": request.form.get("github_owner", "itsbaldeep").strip() or "itsbaldeep",
        "base_branch": request.form.get("base_branch", "main").strip() or "main",
    })
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('onboard_project', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


# ── Per-project actions (audit / draft / fix) ──────────────────

@app.route("/projects/<int:project_id>/audit", methods=["POST"])
def project_audit(project_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT repo_url FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or not (row["repo_url"] or "").startswith("http"):
            return redirect("/projects/%d" % project_id)
        params = json.dumps({"project_id": project_id, "url": row["repo_url"]})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('defend_audit', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/projects/<int:project_id>/draft", methods=["POST"])
def project_draft(project_id):
    keyword = request.form.get("keyword", "").strip()
    brief = request.form.get("brief", "").strip()
    if not keyword or not brief:
        return redirect("/projects/%d" % project_id)
    def w(field, default):
        v = request.form.get(field, "").strip()
        return int(v) if v.isdigit() else default
    model = request.form.get("model", "").strip()
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM projects WHERE id=%s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return redirect("/projects")
        cur.execute("SELECT id FROM brands WHERE project_id=%s", (project_id,))
        brand = cur.fetchone()
        if not brand:
            cur.execute("INSERT INTO brands (name, project_id) VALUES (%s, %s) RETURNING id", (proj["name"], project_id))
            brand = cur.fetchone()
        params = {"content_type": "blog_post", "brand_id": brand["id"],
                  "suggestion": brief, "suggestion_title": brief[:80],
                  "target_keyword": keyword, "word_count_min": w("words_min", 900),
                  "word_count_max": w("words_max", 1400), "source": "dashboard"}
        if model:
            params["model"] = model
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('generate_draft', 'queued', %s, 'dashboard') RETURNING id", (json.dumps(params),))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/projects/<int:project_id>/content-pipeline", methods=["POST"])
def project_content_pipeline(project_id):
    """Queue the multi-stage content pipeline for a project. Research auto-chains
    to outline (both cheap); compose is a separate deliberate gate."""
    keyword = request.form.get("target_keyword", "").strip()
    urls_raw = request.form.get("competitor_urls", "").strip()
    title = request.form.get("title", "").strip()
    if not keyword:
        return redirect("/projects/%d" % project_id)
    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
    if not urls:
        return redirect("/projects/%d" % project_id)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM projects WHERE id=%s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return redirect("/projects")
        cur.execute("SELECT id FROM brands WHERE project_id=%s", (project_id,))
        brand = cur.fetchone()
        if not brand:
            cur.execute("INSERT INTO brands (name, project_id) VALUES (%s, %s) RETURNING id", (proj["name"], project_id))
            brand = cur.fetchone()
        params = {
            "target_keyword": keyword, "competitor_urls": urls,
            "brand_id": brand["id"], "source": "dashboard",
        }
        if title:
            params["title"] = title
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('content_research', 'queued', %s, 'dashboard') RETURNING id", (json.dumps(params),))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/projects/<int:project_id>/content-compose", methods=["POST"])
def project_content_compose(project_id):
    """Deliberate gate: queue compose ONLY after the human inspects the outline."""
    ci_id = request.form.get("content_item_id", "").strip()
    target_keyword = request.form.get("target_keyword", "").strip()
    if not ci_id or not target_keyword:
        return redirect("/projects/%d" % project_id)
    conn = models.db()
    try:
        cur = conn.cursor()
        # only allow composing outlines for this project's brands
        cur.execute(
            "SELECT ci.id FROM content_items ci JOIN brands b ON b.id=ci.brand_id "
            "WHERE ci.id=%s AND ci.status='outline' AND b.project_id=%s",
            (int(ci_id), project_id))
        if not cur.fetchone():
            return redirect("/projects/%d" % project_id)
        params = json.dumps({"content_item_id": int(ci_id), "target_keyword": target_keyword, "source": "dashboard"})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('content_compose', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/projects/<int:project_id>/fix", methods=["POST"])
def project_fix(project_id):
    description = request.form.get("description", "").strip()
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT repo_name FROM projects WHERE id=%s", (project_id,))
        row = cur.fetchone()
        if not row or not description:
            return redirect("/projects/%d" % project_id)
        params = json.dumps({"repo": row["repo_name"], "description": description, "source": "dashboard"})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('propose_fix', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


# ── Content ─────────────────────────────────────────────────────

@app.route("/content")
def content():
    return render_template("content.html")


@app.route("/content/new", methods=["GET"])
def content_new():
    """Content drafting wizard: pick project/brand, run research+outline, inspect, gate compose."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT p.id, p.name, b.id AS brand_id, b.name AS brand_name
                       FROM projects p JOIN brands b ON b.project_id=p.id
                       ORDER BY p.name""")
        projects = cur.fetchall()
        # Check for outline-status items to show in step 2/3
        outline_item_id = request.args.get("outline", type=int)
        outline_item = None
        if outline_item_id:
            cur.execute("""SELECT ci.id, ci.title, ci.status, ci.structured, ci.content_blocks,
                                  ci.created_at, ci.task_id
                           FROM content_items ci WHERE ci.id=%s""", (outline_item_id,))
            outline_item = cur.fetchone()
            if outline_item:
                # Parse structured for template use
                s = outline_item.get("structured")
                if isinstance(s, str):
                    try:
                        s = json.loads(s)
                    except (json.JSONDecodeError, TypeError):
                        s = {}
                outline_item["structured_obj"] = s or {}
        return render_template("content_new.html", projects=projects, outline_item=outline_item,
                               prefill_url=request.args.get("url", ""))
    finally:
        conn.close()


@app.route("/content/new/outlines")
def content_new_outlines():
    """HTMX fragment: list outline-status items pending inspection."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT ci.id, ci.title, ci.status, ci.created_at
                       FROM content_items ci WHERE ci.status='outline'
                       ORDER BY ci.created_at DESC LIMIT 10""")
        outlines = cur.fetchall()
        return render_template("fragments/pending_outlines.html", outlines=outlines)
    finally:
        conn.close()


@app.route("/content/new", methods=["POST"])
def content_new_submit():
    """Queue content_research for the selected brand. Redirect to the task page."""
    keyword = request.form.get("target_keyword", "").strip()
    urls_raw = request.form.get("competitor_urls", "").strip()
    title = request.form.get("title", "").strip()
    brand_id = request.form.get("brand_id", "").strip()
    if not keyword or not urls_raw or not brand_id:
        return redirect("/content/new")
    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
    if not urls:
        return redirect("/content/new")
    conn = models.db()
    try:
        cur = conn.cursor()
        params = {
            "target_keyword": keyword, "competitor_urls": urls,
            "brand_id": int(brand_id), "source": "dashboard",
        }
        if title:
            params["title"] = title
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('content_research', 'queued', %s, 'dashboard') RETURNING id",
            (json.dumps(params),))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/content/<int:item_id>/compose", methods=["POST"])
def content_compose_gate(item_id):
    """Wizard compose gate: queue content_compose after human inspects the outline."""
    target_keyword = request.form.get("target_keyword", "").strip()
    if not target_keyword:
        return redirect("/content/new?outline=%d" % item_id)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM content_items WHERE id=%s", (item_id,))
        row = cur.fetchone()
        if not row or row["status"] != "outline":
            return redirect("/content/new?outline=%d" % item_id)
        params = json.dumps({"content_item_id": item_id, "target_keyword": target_keyword, "source": "dashboard"})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('content_compose', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return redirect("/tasks/%d" % task_id)


@app.route("/content/data")
def content_data():
    data = models.get_content()
    return render_template("fragments/content_list.html", **data)


# ── Competitors ──────────────────────────────────────────────────

# TLDs that make a domain "look real" for competitors auto-proposed by audits.
REAL_TLD_RE = re.compile(r"\.(?:com|in|co|net|org|io|ai|dev|store|info)$", re.IGNORECASE)


def _norm_domain(d):
    return (d or "").strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")


@app.route("/competitors")
def competitors():
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.brand_id, c.domain, c.name, c.scan_enabled,
                   c.sitemap_url, c.last_scanned_at, b.name AS brand_name,
                   (SELECT count(*) FROM competitor_pages p WHERE p.competitor_id = c.id) AS total_pages,
                   (SELECT max(p.lastmod) FROM competitor_pages p WHERE p.competitor_id = c.id) AS most_recent_page,
                   (SELECT count(*) FROM competitor_pages p WHERE p.competitor_id = c.id
                    AND p.first_seen_at > now() - interval '30 days'
                    AND p.first_seen_at > (SELECT min(first_seen_at) + interval '1 hour'
                                           FROM competitor_pages p2 WHERE p2.competitor_id = p.competitor_id)
                   ) AS recent_pages
            FROM competitors c
            LEFT JOIN brands b ON b.id = c.brand_id
            ORDER BY b.name, c.domain
        """)
        comps = cur.fetchall()
        # Projects available for the add-competitor form (scaffolded/onboarded apps).
        # The brand for a project is resolved/created on add.
        cur.execute("""SELECT p.id, p.name,
                              b.id AS brand_id, b.name AS brand_name
                       FROM projects p
                       LEFT JOIN brands b ON b.project_id = p.id
                       WHERE p.name NOT IN ('dashboard', 'agency-os', 'agency-dashboard', 'system')
                       ORDER BY p.name""")
        projects = cur.fetchall()
        # Domain validation is computed here (no DB flag): a domain counts as
        # validated if it has a sitemap, has been scanned, or matches a real-TLD
        # pattern. Auto-proposed LLM competitors usually have none of these.
        why_by_domain = {}
        fetched_brands = set()
        for comp in comps:
            domain = _norm_domain(comp["domain"])
            comp["domain_validated"] = bool(
                comp.get("sitemap_url") or comp.get("last_scanned_at") or REAL_TLD_RE.search(domain))
            brand_id = comp["brand_id"]
            if brand_id and brand_id not in fetched_brands:
                fetched_brands.add(brand_id)
                cur.execute(
                    "SELECT summary->'competitors' AS competitors FROM audits "
                    "WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1", (brand_id,))
                row = cur.fetchone()
                items = _parse_jsonb(row["competitors"]) if row else []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("domain"):
                            why_by_domain.setdefault(_norm_domain(item["domain"]), item.get("why"))
            comp["why"] = why_by_domain.get(domain)
        # Fetch recent non-baseline pages for enabled competitors
        recent_pages = {}
        for comp in comps:
            if not comp["scan_enabled"]:
                continue
            cur.execute("""
                SELECT p.url, p.title, p.lastmod, p.first_seen_at
                FROM competitor_pages p
                WHERE p.competitor_id = %s
                  AND p.first_seen_at > (SELECT min(first_seen_at) + interval '1 hour'
                                         FROM competitor_pages p2 WHERE p2.competitor_id = p.competitor_id)
                ORDER BY p.first_seen_at DESC
                LIMIT 25
            """, (comp["id"],))
            recent_pages[comp["id"]] = cur.fetchall()
        return render_template("competitors.html", competitors=comps, recent_pages=recent_pages, projects=projects)
    finally:
        conn.close()


@app.route("/competitors/add", methods=["POST"])
def competitor_add():
    project_id = request.form.get("project_id", type=int)
    name = request.form.get("name", "").strip()
    domain = request.form.get("domain", "").strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    sitemap_url = request.form.get("sitemap_url", "").strip()
    if not project_id:
        return jsonify({"ok": False, "error": "project_id is required"}), 400
    if not domain:
        return jsonify({"ok": False, "error": "domain is required"}), 400
    if not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", domain):
        return jsonify({"ok": False, "error": "invalid domain"}), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM projects WHERE id=%s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return jsonify({"ok": False, "error": "project not found"}), 404
        # Resolve (or create) the brand for this project
        cur.execute("SELECT id FROM brands WHERE project_id=%s", (project_id,))
        brand = cur.fetchone()
        if not brand:
            slug = re.sub(r'[^a-z0-9]+', '-', (name or domain.split(".")[0]).lower()).strip('-')[:40]
            cur.execute(
                "INSERT INTO brands (name, slug, access_tier, project_id) VALUES (%s, %s, '0', %s) "
                "ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name, project_id=EXCLUDED.project_id RETURNING id",
                (proj["name"], slug, project_id))
            brand = cur.fetchone()
        cur.execute(
            "INSERT INTO competitors (brand_id, domain, name, scan_enabled, sitemap_url) "
            "VALUES (%s, %s, %s, true, %s) RETURNING id",
            (brand["id"], domain, name or domain.split(".")[0].title(), sitemap_url or None))
        comp_id = cur.fetchone()["id"]
        params = json.dumps({"competitor_id": comp_id})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('competitor_scan', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, "id": comp_id})


@app.route("/competitors/<int:cid>/scan", methods=["POST"])
def competitor_scan_now(cid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, scan_enabled FROM competitors WHERE id=%s", (cid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        # Scans are explicit opt-in; scanning now implies opting in.
        if not row["scan_enabled"]:
            cur.execute("UPDATE competitors SET scan_enabled=true WHERE id=%s", (cid,))
        params = json.dumps({"competitor_id": cid})
        cur.execute(
            "INSERT INTO tasks (type, status, params, triggered_by) "
            "VALUES ('competitor_scan', 'queued', %s, 'dashboard') RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/competitors/<int:cid>/toggle", methods=["POST"])
def competitor_toggle(cid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE competitors SET scan_enabled = NOT scan_enabled WHERE id=%s RETURNING scan_enabled", (cid,))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "scan_enabled": row["scan_enabled"]})
    finally:
        conn.close()


def _render_content_body(ci, item_id, conn, cur):
    """Render a content item's body as HTML (content_blocks → structured → body)."""
    banner = ('<div style="background:#fff7d6;border:1px solid #e6cc66;'
              'padding:10px 14px;margin-bottom:16px;border-radius:6px;font-size:14px;">'
              '&#9888; Preview only &#8212; final appearance depends on the target project\'s own site.'
              '</div>')
    blocks = ci.get('content_blocks')
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except json.JSONDecodeError:
            blocks = None
    if isinstance(blocks, list) and blocks:
        sys.path.insert(0, "/home/agency/agency-os/scripts")
        import importlib
        cp = importlib.import_module("content_pipeline")
        return banner + cp.render_pipeline_css() + \
            f"<div class='pipeline-article'>{cp.render_content_blocks(blocks, ci['title'])}</div>"
    structured = ci.get('structured')
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            structured = None
    if structured:
        parts = [f"<h1>{structured.get('title', ci['title'])}</h1>"]
        if structured.get('meta_description'):
            parts.append(f"<p><i>{structured['meta_description']}</i></p>")
        for s in structured.get('sections', []):
            parts.append(f"<h2>{s.get('title', s.get('heading', ''))}</h2>")
            parts.append(markdown.markdown(s.get('body_markdown') or ''))
        faqs = structured.get('faqs', structured.get('faq', []))
        if faqs:
            parts.append("<h2>FAQs</h2>")
            for f in faqs:
                q = f.get('q', f.get('question', ''))
                a = f.get('a', f.get('answer', ''))
                parts.append(f"<p><b>{q}</b><br>{markdown.markdown(a or '')}</p>")
        return banner + "".join(parts)
    html = markdown.markdown(ci.get('body') or '*No content generated yet.*')
    return banner + f"<h1>{ci['title']}</h1>{html}"


@app.route("/content/<int:item_id>/preview")
def content_preview(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ci.*, b.name AS brand_name "
            "FROM content_items ci LEFT JOIN brands b ON b.id=ci.brand_id "
            "WHERE ci.id=%s", (item_id,))
        ci = cur.fetchone()
        if not ci:
            return "Not found", 404
        body_html = _render_content_body(ci, item_id, conn, cur)
        target_keyword = ""
        s = ci.get("structured")
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                s = None
        if isinstance(s, dict):
            target_keyword = s.get("target_keyword", "") or ""
        blocks = _parse_jsonb(ci.get("content_blocks"))
        has_image_slots = isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "image_slot" for block in blocks
        )
        updated_fmt = fmt_ts(ci.get("updated_at") or ci.get("created_at"))
        return render_template("content_preview.html", active='content',
                               ci=ci, body_html=body_html,
                               target_keyword=target_keyword,
                               has_image_slots=has_image_slots,
                               updated_fmt=updated_fmt)
    finally:
        conn.close()


@app.route("/content/<int:item_id>/images", methods=["POST"])
def content_images(item_id):
    """Generate + store slot images for a content item's image_slots (MinIO)."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT content_blocks FROM content_items WHERE id=%s", (item_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        blocks = row.get("content_blocks")
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except json.JSONDecodeError:
                blocks = None
        if not isinstance(blocks, list):
            return jsonify({"ok": False, "error": "no content_blocks"}), 400
        sys.path.insert(0, "/home/agency/agency-os/scripts")
        import importlib
        cp = importlib.import_module("content_pipeline")
        blocks = cp.source_slot_images(item_id, blocks)
        blocks = cp.ensure_slot_images(item_id, blocks)
        cur.execute("UPDATE content_items SET content_blocks=%s, updated_at=now() WHERE id=%s",
                    (json.dumps(blocks), item_id))
        conn.commit()
        made = [b for b in blocks if b.get("type") == "image_slot" and b.get("url")]
        return jsonify({"ok": True, "slots": len(made),
                        "urls": [b["url"] for b in made]})
    finally:
        conn.close()


@app.route("/content/<int:ci_id>/download")
def content_download(ci_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, body, status FROM content_items WHERE id=%s", (ci_id,))
        ci = cur.fetchone()
        if not ci:
            return "Not found", 404
        md = f"# {ci['title']}\n\n{ci.get('body') or '*No content generated yet.*'}"
        resp = make_response(md)
        resp.headers["Content-Type"] = "text/markdown; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{ci["title"][:50].replace(" ","-")}.md"'
        return resp
    finally:
        conn.close()


@app.route("/content/<int:ci_id>/approve", methods=["POST"])
def content_approve(ci_id):
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,status,publish_task_id FROM content_items WHERE id=%s FOR UPDATE", (ci_id,))
        item = cur.fetchone()
        if not item:
            return jsonify({"ok": False, "error": "not found"}), 404
        if item["status"] not in ("draft", "approved", "needs_publish_input", "publish_failed"):
            return jsonify({"ok": False, "error": f"content is {item['status']}, not publishable"}), 409
        destination = {
            "type": (payload.get("destination_type") or "").strip(),
            "base_url": (payload.get("base_url") or "").strip(),
            "username": (payload.get("username") or "").strip(),
            "credential_ref": (payload.get("credential_ref") or "").strip(),
        }
        if item.get("publish_task_id"):
            cur.execute("SELECT id,type,status,params FROM tasks WHERE id=%s FOR UPDATE",
                        (item["publish_task_id"],))
            existing = cur.fetchone()
            if existing and existing["type"] == "publish_content":
                if existing["status"] in ("queued", "running"):
                    conn.commit()
                    return jsonify({"ok": True, "task_id": existing["id"], "existing": True})
                if existing["status"] == "needs_input":
                    params = _resume_workflow_params("publish_content", _parse_jsonb(existing.get("params")), payload)
                    cur.execute(
                        "UPDATE tasks SET params=%s,status='queued',error=NULL,result_ref=NULL,"
                        "started_at=NULL,finished_at=NULL,progress=0,progress_text='resumed with publication input' "
                        "WHERE id=%s",
                        (json.dumps(params), existing["id"]),
                    )
                    cur.execute("UPDATE content_items SET status='publishing',updated_at=now() WHERE id=%s", (ci_id,))
                    conn.commit()
                    return jsonify({"ok": True, "task_id": existing["id"], "resumed": True})
                if existing["status"] == "failed":
                    return jsonify({"ok": False, "error": "publication may have partially completed; inspect the linked failed task before an explicit re-run"}), 409
        params = json.dumps({"content_item_id": ci_id, "destination": destination,
                             "instructions": (payload.get("instructions") or "")[:2000]})
        cur.execute(
            "INSERT INTO tasks (type,status,params,triggered_by) "
            "VALUES ('publish_content','queued',%s,'dashboard-content-approval') RETURNING id",
            (params,),
        )
        task_id = cur.fetchone()["id"]
        cur.execute("UPDATE content_items SET status='publishing', publish_task_id=%s, updated_at=now() WHERE id=%s",
                    (task_id, ci_id))
        conn.commit()
        models.ch_trace({"project": "system", "actor": "agent", "action": "content_approved",
                         "detail": f"Content item {ci_id} approved for publication task {task_id}",
                         "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": task_id})
    finally:
        conn.close()


@app.route("/content/<int:ci_id>/regenerate", methods=["POST"])
def content_regenerate(ci_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE content_items SET status='draft', body=NULL WHERE id=%s", (ci_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ── Operations (Jobs + Approvals) ──────────────────────────────

@app.route("/operations")
def operations():
    return render_template("operations.html")


@app.route("/operations/jobs")
def operations_jobs():
    jobs = models.get_jobs()
    return render_template("fragments/jobs_list.html", jobs=jobs)


@app.route("/operations/approvals")
def operations_approvals():
    pending = models.get_approvals(pending_only=True)
    history = models.get_approvals(pending_only=False)
    return render_template("fragments/approvals_list.html", approvals=pending, all_approvals=history)


@app.route("/operations/job-runs")
def job_runs():
    job_id = request.args.get("job_id", type=int)
    runs = models.get_job_runs(limit=50, job_id=job_id)
    return render_template("fragments/job_runs.html", runs=runs)


@app.route("/operations/jobs/<int:job_id>/toggle", methods=["POST"])
def job_toggle(job_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE background_jobs SET enabled = NOT enabled, updated_at=now() WHERE id=%s", (job_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/operations/jobs/<int:job_id>/run", methods=["POST"])
def job_run(job_id):
    subprocess.Popen(
        ["bash", "/home/agency/agency-os/scripts/run-job.sh", str(job_id), "manual"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return jsonify({"ok": True})


@app.route("/operations/approvals/<int:approval_id>/<decision>", methods=["POST"])
def approval_act(approval_id, decision):
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    ok, msg = models.run_approval(approval_id, decision, payload.get("note", ""))
    if ok:
        return jsonify({"ok": True, "task_id": int(msg) if msg.isdigit() else None})
    return jsonify({"ok": False, "error": msg}), 400


# ── Health ──────────────────────────────────────────────────────

@app.route("/health")
def health():
    return render_template("health.html")


@app.route("/health/data")
def health_data():
    data = models.get_health()
    cols, rows = models.ch_query(
        "SELECT ts, project, actor, action, detail, gate, decision, ok "
        "FROM default.events WHERE ok=0 OR gate NOT IN ('','green') OR decision='resolved' "
        "OR action NOT IN ('health_check','job_completed','memory_sweep','security_scan','docker_prune') "
        "ORDER BY ts DESC LIMIT 20 FORMAT TabSeparatedWithNames"
    )
    events = [dict(zip(cols, row)) for row in rows] if cols else []
    return render_template("fragments/health.html", health=data, events=events)


# ── Spend ───────────────────────────────────────────────────────

@app.route("/spend")
def spend():
    return render_template("spend.html")


@app.route("/spend/data")
def spend_data():
    data = models.get_spend()
    return render_template("fragments/spend.html", spend=data)


# ── Resources ───────────────────────────────────────────────────

@app.route("/resources")
def resources():
    return render_template("resources.html")


@app.route("/resources/data")
def resources_data():
    data = models.get_resources()
    return render_template("fragments/resources.html", resources=data)


# ── System Map ──────────────────────────────────────────────────

@app.route("/system-map")
def system_map():
    data = models.get_system_data()
    return render_template("system_map.html", system=data)


# ── Task status polling ─────────────────────────────────────────

@app.route("/api/tasks/<int:tid>")
def task_status(tid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, type, status, progress, progress_text, prompt_tokens, completion_tokens, cost, result_ref, error, finished_at FROM tasks WHERE id=%s", (tid,))
        t = cur.fetchone()
        if not t:
            return jsonify({"ok": False, "error": "not found"}), 404
        resp = {k: dec_to_num(v) for k, v in dict(t).items()}
        return jsonify(resp)
    finally:
        conn.close()


@app.route("/tasks/<int:task_id>/monitor")
def task_monitor(task_id):
    """Live progress fragment for the task detail page (self-polling)."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status, progress, progress_text, prompt_tokens, "
                    "completion_tokens, cost, finished_at, error FROM tasks WHERE id=%s",
                    (task_id,))
        t = cur.fetchone()
        if t:
            t = {k: dec_to_num(v) for k, v in dict(t).items()}
    finally:
        conn.close()
    if not t:
        return "", 404
    return render_template("_task_monitor.html", t=t)


@app.route("/api/dev-tasks/<int:tid>/progress")
def dev_task_progress(tid):
    """Live progress fragment for a dev-tasks table row (self-polling)."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status, progress, progress_text, finished_at, error "
                    "FROM tasks WHERE id=%s", (tid,))
        t = cur.fetchone()
        if t:
            t = {k: dec_to_num(v) for k, v in dict(t).items()}
    finally:
        conn.close()
    if not t:
        return "", 404
    return render_template("_dev_task_progress.html", t=t)


NUMERIC_PARAMS = {"timeout", "rounds", "word_count_min", "word_count_max", "limit"}
RESUMABLE_WORKFLOW_TASKS = {"execute_suggestion", "publish_content", "execute_approval"}


def _resume_workflow_params(task_type, params, payload):
    """Merge only known non-secret workflow inputs into an interrupted task."""
    params = dict(params) if isinstance(params, dict) else {}
    if task_type == "execute_suggestion":
        for key, limit in (("instructions", 2000), ("target_keyword", 300),
                           ("competitor_urls", 5000)):
            if key in payload:
                params[key] = str(payload.get(key) or "").strip()[:limit]
        return params

    destination = params.get("destination")
    destination = dict(destination) if isinstance(destination, dict) else {}
    for source, target, limit in (
        ("destination_type", "type", 40),
        ("base_url", "base_url", 500),
        ("username", "username", 300),
        ("credential_ref", "credential_ref", 200),
    ):
        if source in payload:
            destination[target] = str(payload.get(source) or "").strip()[:limit]
    params["destination"] = destination
    note = str(payload.get("instructions") or payload.get("operator_input") or "").strip()[:2000]
    if note:
        params["operator_input" if task_type == "execute_approval" else "instructions"] = note
    return params


@app.route("/api/tasks/<int:tid>/resume", methods=["POST"])
def task_resume(tid):
    """Resume a linked workflow after supplying its explicitly requested inputs."""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,type,status,params FROM tasks WHERE id=%s FOR UPDATE", (tid,))
        task = cur.fetchone()
        if not task:
            return jsonify({"ok": False, "error": "not found"}), 404
        if task["status"] != "needs_input":
            return jsonify({"ok": False, "error": f"task is {task['status']}, not waiting for input"}), 409
        if task["type"] not in RESUMABLE_WORKFLOW_TASKS:
            return jsonify({"ok": False, "error": "this side-effect task must be inspected and re-run as a new task"}), 409
        params = _parse_jsonb(task.get("params"))
        params = _resume_workflow_params(task["type"], params, payload)
        cur.execute(
            "UPDATE tasks SET params=%s,status='queued',error=NULL,result_ref=NULL,"
            "started_at=NULL,finished_at=NULL,progress=0,progress_text='resumed with operator input' "
            "WHERE id=%s",
            (json.dumps(params), tid),
        )
        if task["type"] == "execute_suggestion" and params.get("suggestion_id"):
            cur.execute("UPDATE suggestions SET status='executing',updated_at=now() WHERE id=%s",
                        (params["suggestion_id"],))
        elif task["type"] == "publish_content" and params.get("content_item_id"):
            cur.execute("UPDATE content_items SET status='publishing',updated_at=now() WHERE id=%s",
                        (params["content_item_id"],))
        conn.commit()
        models.ch_trace({"project": "system", "actor": "human", "action": "workflow_task_resumed",
                         "detail": f"Task {tid} resumed with named operator inputs", "gate": "green",
                         "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": tid})
    finally:
        conn.close()


@app.route("/api/tasks/<int:tid>/rerun", methods=["POST"])
def task_rerun(tid):
    """Duplicate a task with editable params. Returns the new task id."""
    source_repo = request.args.get("repo", "")
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT type, params FROM tasks WHERE id=%s", (tid,))
        src = cur.fetchone()
        if not src:
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            base_params = json.loads(src["params"]) if isinstance(src["params"], str) else (src["params"] or {})
            base_params = dict(base_params) if isinstance(base_params, dict) else {}
        except Exception:
            base_params = {}
        # Copy editable params that were submitted (param__<key>). Prefilled by the
        # form with the source task's values, so only touched ones matter.
        for k, v in request.form.items():
            if k.startswith("param__"):
                key = k[len("param__"):]
                if not v:
                    base_params.pop(key, None)
                    continue
                if key in NUMERIC_PARAMS:
                    try:
                        base_params[key] = int(v)
                    except ValueError:
                        return jsonify({"ok": False, "error": f"{key} must be a number"}), 400
                else:
                    base_params[key] = v.strip()
        # Optional free-form extra params as JSON.
        extra = request.form.get("extra_json", "").strip()
        if extra:
            try:
                ex = json.loads(extra)
                if isinstance(ex, dict):
                    base_params.update(ex)
                else:
                    return jsonify({"ok": False, "error": "extra_json must be a JSON object"}), 400
            except (json.JSONDecodeError, TypeError):
                return jsonify({"ok": False, "error": "extra_json is not valid JSON"}), 400
        if source_repo and not base_params.get("repo"):
            base_params["repo"] = source_repo
        base_params["source"] = "dashboard-rerun"
        base_params["rerun_from"] = tid
        cur.execute("INSERT INTO tasks (type, status, params, triggered_by) "
                    "VALUES (%s,'queued',%s,'dashboard-rerun') RETURNING id",
                    (src["type"], json.dumps(base_params)))
        new_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


# ── Brand audit / suggestion actions (kept from original) ──────

@app.route("/api/brands/audit", methods=["POST"])
def brand_audit_create():
    domain = request.form.get("domain", "").strip()
    brand_id = request.form.get("brand_id", "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "domain is required"}), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"domain": domain, "brand_id": int(brand_id) if brand_id else None})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('run_brand_audit', %s) RETURNING id", (params,))
        tid = cur.fetchone()["id"]
        conn.commit()
        event_project = f"brand:{int(brand_id)}" if brand_id else domain
        models.ch_trace({"project": event_project, "actor": "human", "action": "brand_audit_created",
                         "detail": f"Brand audit task {tid} for {domain}", "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/suggestions/<int:sid>/approve", methods=["POST"])
def suggestion_approve(sid):
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,status,execution_task_id FROM suggestions WHERE id=%s FOR UPDATE", (sid,))
        suggestion = cur.fetchone()
        if not suggestion:
            return jsonify({"ok": False, "error": "not found"}), 404
        if suggestion.get("execution_task_id"):
            cur.execute("SELECT id,type,status,params FROM tasks WHERE id=%s FOR UPDATE",
                        (suggestion["execution_task_id"],))
            existing = cur.fetchone()
            if existing and existing["type"] == "execute_suggestion":
                if existing["status"] in ("queued", "running"):
                    return jsonify({"ok": True, "task_id": existing["id"], "existing": True})
                if existing["status"] == "needs_input":
                    params = _resume_workflow_params("execute_suggestion", _parse_jsonb(existing.get("params")), payload)
                    cur.execute(
                        "UPDATE tasks SET params=%s,status='queued',error=NULL,result_ref=NULL,"
                        "started_at=NULL,finished_at=NULL,progress=0,progress_text='resumed with suggestion input' "
                        "WHERE id=%s",
                        (json.dumps(params), existing["id"]),
                    )
                    cur.execute("UPDATE suggestions SET status='executing',updated_at=now() WHERE id=%s", (sid,))
                    conn.commit()
                    return jsonify({"ok": True, "task_id": existing["id"], "resumed": True})
                if existing["status"] == "failed":
                    return jsonify({"ok": False, "error": "inspect the linked failed task before an explicit re-run"}), 409
            # A dangling link should not block repair; create a replacement below.
        if suggestion["status"] not in ("pending", "approved", "failed", "needs_input"):
            return jsonify({"ok": False, "error": f"suggestion is already {suggestion['status']}"}), 409
        task_params = json.dumps({
            "suggestion_id": sid,
            "instructions": (payload.get("instructions") or "")[:2000],
            "target_keyword": (payload.get("target_keyword") or "")[:300],
            "competitor_urls": (payload.get("competitor_urls") or "")[:5000],
        })
        cur.execute(
            "INSERT INTO tasks (type,status,params,triggered_by) "
            "VALUES ('execute_suggestion','queued',%s,'dashboard-suggestion-approval') RETURNING id",
            (task_params,),
        )
        task_id = cur.fetchone()["id"]
        cur.execute("UPDATE suggestions SET status='executing', execution_task_id=%s, updated_at=now() WHERE id=%s",
                    (task_id, sid))
        conn.commit()
        models.ch_trace({"project": "system", "actor": "agent", "action": "suggestion_approved",
                         "detail": f"Suggestion {sid} → execution task {task_id}",
                         "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": task_id})
    finally:
        conn.close()


@app.route("/api/suggestions/<int:sid>/reject", methods=["POST"])
def suggestion_reject(sid):
    reason = request.form.get("reason", "")
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE suggestions SET status='rejected', rejection_reason=%s, updated_at=now() WHERE id=%s AND status='pending'", (reason, sid))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/suggestions/<int:sid>/generate", methods=["POST"])
def suggestion_generate(sid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, brand_id, title, rationale, action_type FROM suggestions WHERE id=%s", (sid,))
        sug = cur.fetchone()
        if not sug:
            return jsonify({"ok": False, "error": "not found"}), 404

        is_blog = sug.get("action_type") == "create" or any(kw in (sug["title"] or "").lower()[:20] for kw in ["create ", "write ", "develop ", "launch "])
        content_type = "blog_post" if is_blog else "article"

        context_parts = [f"Brand: {sug['brand_id']}"]
        cur.execute("SELECT property_type, value FROM brand_properties WHERE brand_id=%s", (sug["brand_id"],))
        for p in cur.fetchall():
            context_parts.append(f"{p['property_type']}: {p['value']}")

        cur.execute("SELECT crawl_text, created_at FROM audits WHERE brand_id=%s AND crawl_text IS NOT NULL ORDER BY created_at DESC LIMIT 1", (sug["brand_id"],))
        audit_row = cur.fetchone()
        if audit_row and audit_row["crawl_text"]:
            age = (datetime.now(timezone.utc) - audit_row["created_at"]).days if audit_row["created_at"] else 99
            if age <= 7:
                context_parts.append(f"\n--- Homepage content ---\n{audit_row['crawl_text'][:2000]}")

        params = json.dumps({
            "suggestion": f"{sug['title']} - {sug.get('rationale', '')}",
            "context": "\n".join(context_parts),
            "content_type": content_type,
            "suggestion_id": sid,
            "brand_id": sug["brand_id"],
            "suggestion_title": sug["title"],
        })
        cur.execute("INSERT INTO tasks (type, params) VALUES ('generate_draft', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id, "suggestion_id": sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/suggestions/<int:sid>/task")
def suggestion_task(sid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.status, t.prompt_tokens, t.completion_tokens, t.cost, t.error, ci.body
            FROM content_items ci
            JOIN tasks t ON t.id = ci.task_id
            WHERE ci.suggestion_id=%s
            ORDER BY ci.id DESC LIMIT 1
        """, (sid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "status": "not_found"})
        resp = {k: dec_to_num(v) for k, v in dict(row).items()}
        resp["tokens"] = (resp.get("prompt_tokens") or 0) + (resp.get("completion_tokens") or 0)
        if resp.get("cost"):
            resp["cost"] = f"{resp['cost']:.6f}"
        return jsonify(resp)
    finally:
        conn.close()


# ── Design page routes ─────────────────────────────────────────

@app.route("/api/design/<int:project_id>/concepts", methods=["POST"])
def design_concepts(project_id):
    brief = request.form.get("brief", "").strip()
    n = int(request.form.get("variations", "3"))
    if not brief:
        return jsonify({"ok": False, "error": "brief is required"}), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"project_id": project_id, "brief": brief, "variations": max(1, min(n, 5)), "stage": "concepts"})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('design_page', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/design/variations/<int:vid>/approve", methods=["POST"])
def design_variation_approve(vid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM concept_variations WHERE id=%s", (vid,))
        var = cur.fetchone()
        if not var:
            return jsonify({"ok": False, "error": "not found"}), 404
        if var["status"] != "pending":
            return jsonify({"ok": False, "error": f"status is '{var['status']}', expected 'pending'"}), 400
        cur.execute("UPDATE concept_variations SET status='approved' WHERE id=%s", (vid,))
        params = json.dumps({"variation_id": vid, "stage": "render"})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('design_page', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id, "variation_id": vid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/design/<path:project>/file/<int:vid>")
def design_file(project, vid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT cv.file_path, p.local_path FROM concept_variations cv JOIN projects p ON p.id=cv.project_id WHERE cv.id=%s", (vid,))
        row = cur.fetchone()
        if row and row[0] and row[1]:
            fpath = os.path.join(row[1], row[0])
            try:
                return send_file(fpath)
            except Exception:
                pass
    finally:
        conn.close()
    return "<p>File not found</p>", 404


@app.route("/design/<int:project_id>/variations")
def design_variations(project_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT cv.*, p.name AS project_slug FROM concept_variations cv JOIN projects p ON p.id=cv.project_id WHERE cv.project_id=%s AND cv.skill='design_page' ORDER BY cv.spec_index", (project_id,))
        variations = cur.fetchall()
        for v in variations:
            if isinstance(v.get("spec_json"), str):
                try:
                    v["spec_json"] = json.loads(v["spec_json"])
                except (json.JSONDecodeError, TypeError):
                    v["spec_json"] = {}
        cur.execute("SELECT name FROM projects WHERE id=%s", (project_id,))
        proj = cur.fetchone()
        return render_template("design_variations.html", variations=variations, project_id=project_id, project_slug=proj["name"] if proj else "?")
    finally:
        conn.close()


# ── Dev tasks (propose_fix) ───────────────────────────────────

def dev_task_repos(cur):
    """Authorized fixable repos straight from Postgres (projects table)."""
    cur.execute(
        "SELECT repo_name, name, base_branch, github_owner "
        "FROM projects WHERE agent_allowed=true "
        "AND repo_name IS NOT NULL AND repo_name<>'' ORDER BY name")
    return cur.fetchall()


@app.route("/dev-tasks")
def dev_tasks():
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.*, COALESCE(p.name, 'system') AS project_name
            FROM tasks t LEFT JOIN projects p ON false
            WHERE t.type = 'propose_fix'
            ORDER BY t.created_at DESC LIMIT 50
        """)
        tasks_list = cur.fetchall()
        for t in tasks_list:
            for k, v in list(t.items()):
                if isinstance(v, Decimal):
                    t[k] = float(v)
                elif isinstance(v, datetime):
                    t[k] = v.isoformat()
            if t.get("result_ref"):
                try:
                    t["pr_data"] = json.loads(t["result_ref"])
                except (json.JSONDecodeError, TypeError):
                    t["pr_data"] = None
            else:
                t["pr_data"] = None
            t["created_fmt"] = fmt_ts(t.get("created_at"))
            t["duration"] = fmt_dur(t.get("started_at"), t.get("finished_at"))
        repos = dev_task_repos(cur)
        return render_template("dev_tasks.html", tasks=tasks_list, repos=repos)
    finally:
        conn.close()


@app.route("/api/dev-tasks/create", methods=["POST"])
def dev_task_create():
    repo = request.form.get("repo", "")
    desc = request.form.get("description", "")
    base = request.form.get("base", "main")
    if not desc.strip():
        return jsonify({"ok": False, "error": "description required"}), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        repos = dev_task_repos(cur)
        match = next((r for r in repos if r["repo_name"] == repo), None)
        if not match:
            return jsonify({"ok": False, "error": f"Repo not authorized: {repo}"}), 400
        base = base or str(match["base_branch"] or "main")
        params = json.dumps({"repo": repo, "description": desc, "base": base})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('propose_fix', %s) RETURNING id", (params,))
        tid = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/dev-tasks/<int:tid>/merge", methods=["POST"])
def dev_task_merge(tid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, type, status, params, result_ref FROM tasks WHERE id=%s", (tid,))
        task = cur.fetchone()
        if not task:
            return jsonify({"ok": False, "error": "not found"}), 404
        if task["type"] != "propose_fix" or task["status"] != "done":
            return jsonify({"ok": False, "error": "task not ready"}), 400
        pr_data = json.loads(task["result_ref"])
        pr_url = pr_data.get("pr_url", "")
        if not pr_url:
            return jsonify({"ok": False, "error": "no PR url"}), 400
        m = re.search(r'/pull/(\d+)', pr_url)
        if not m:
            return jsonify({"ok": False, "error": "cannot parse PR number"}), 400
        pr_num = m.group(1)
        repo = json.loads(task["params"]).get("repo", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        import urllib.request
        merge_payload = json.dumps({"merge_method": "merge"}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/itsbaldeep/{repo}/pulls/{pr_num}/merge",
            data=merge_payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "AgencyOS/1.0"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            merge_data = json.loads(resp.read())
            cur.execute("UPDATE tasks SET status='merged', finished_at=now() WHERE id=%s", (tid,))
            conn.commit()
            return jsonify({"ok": True, "merge": merge_data})
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            return jsonify({"ok": False, "error": f"GitHub merge failed {e.code}: {body}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


# ── Tasks ───────────────────────────────────────────────────────

@app.route("/tasks")
def tasks():
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, type, status, cost, triggered_by, created_at, started_at, finished_at,
                   prompt_tokens, completion_tokens,
                   COALESCE(params->>'spec', params->>'description', params->>'prompt', params->>'question', '') AS gist
            FROM tasks ORDER BY id DESC LIMIT 50
        """)
        tasks_list = cur.fetchall()
    finally:
        conn.close()
    for t in tasks_list:
        t["created_fmt"] = fmt_ts(t.get("created_at"))
        t["duration"] = fmt_dur(t.get("started_at"), t.get("finished_at"))
        t["tokens"] = (t.get("prompt_tokens") or 0) + (t.get("completion_tokens") or 0)
    return render_template("tasks.html", tasks=tasks_list)


@app.route("/tasks/<int:task_id>")
def task_detail(task_id):
    conn = models.db()
    workflow_links = {"suggestion": None, "content": None, "approval": None, "children": []}
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        t = cur.fetchone()
        if not t:
            return redirect("/tasks")
        d = {k: dec_to_num(v) for k, v in dict(t).items()}
        p = d.get("params")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except (json.JSONDecodeError, TypeError):
                p = None
        d["params_pretty"] = json.dumps(p, indent=2, default=str) if p else ""
        d["params_obj"] = p if isinstance(p, dict) else {}
        d["created_fmt"] = fmt_ts(d.get("created_at"))
        d["started_fmt"] = fmt_ts(d.get("started_at"))
        d["finished_fmt"] = fmt_ts(d.get("finished_at"))
        d["duration"] = fmt_dur(d.get("started_at"), d.get("finished_at"))
        d["tokens"] = (d.get("prompt_tokens") or 0) + (d.get("completion_tokens") or 0)
        d["source"] = d["params_obj"].get("source") if d["params_obj"] else None
        d["model"] = d["params_obj"].get("model") if d["params_obj"] else None
        d["result_pretty"] = ""
        rr = d.get("result_ref")
        if rr:
            try:
                d["result_pretty"] = json.dumps(json.loads(rr), indent=2)
            except (json.JSONDecodeError, TypeError):
                d["result_pretty"] = rr
        ci = None
        outline_ci = None
        if d.get("type") == "generate_draft" and d.get("status") == "done":
            cur.execute(
                "SELECT id, title FROM content_items WHERE task_id=%s ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            ci = cur.fetchone()
        elif d.get("type") == "content_outline" and d.get("status") == "done":
            cur.execute(
                "SELECT id, title, status FROM content_items WHERE task_id=%s ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            outline_ci = cur.fetchone()
        elif d.get("type") == "content_compose" and d.get("status") == "done":
            ci_id = d.get("params_obj", {}).get("content_item_id")
            if ci_id:
                cur.execute("SELECT id, title FROM content_items WHERE id=%s", (ci_id,))
                ci = cur.fetchone()
        cur.execute("SELECT id,title,status,brand_id FROM suggestions WHERE execution_task_id=%s LIMIT 1", (task_id,))
        workflow_links["suggestion"] = cur.fetchone()
        cur.execute("SELECT id,title,status FROM content_items WHERE publish_task_id=%s LIMIT 1", (task_id,))
        workflow_links["content"] = cur.fetchone()
        cur.execute("SELECT id,type::text,status::text FROM approvals WHERE task_id=%s LIMIT 1", (task_id,))
        workflow_links["approval"] = cur.fetchone()
        cur.execute("SELECT id,type,status FROM tasks WHERE parent_task_id=%s ORDER BY id", (task_id,))
        workflow_links["children"] = cur.fetchall()
    finally:
        conn.close()
    return render_template("task_detail.html", t=d, ci=ci, outline_ci=outline_ci,
                           workflow_links=workflow_links,
                           can_resume=d.get("status") == "needs_input" and d.get("type") in RESUMABLE_WORKFLOW_TASKS)


# ── Static files ───────────────────────────────────────────────

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC), filename)


# ── Error handlers ──────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template("base.html", content="<p>Page not found</p>"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("base.html", content="<p>Server error</p>"), 500


# ── Main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    bind_host = os.environ.get("BIND_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("BIND_PORT", "80"))
    app.run(host=bind_host, port=bind_port, debug=False)
