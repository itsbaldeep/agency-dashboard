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


@app.route("/engagements/brand/<int:brand_id>/report")
def brand_report(brand_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, slug, access_tier FROM brands WHERE id=%s", (brand_id,))
        brand = cur.fetchone()
        if not brand:
            return redirect("/engagements")
        cur.execute("SELECT property_type, value FROM brand_properties WHERE brand_id=%s", (brand_id,))
        brand_properties = cur.fetchall()
        cur.execute("SELECT domain, name FROM competitors WHERE brand_id=%s ORDER BY domain", (brand_id,))
        competitors = cur.fetchall()
        cur.execute("SELECT * FROM audits WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1", (brand_id,))
        audit = cur.fetchone()
    finally:
        conn.close()

    audit_summary = {}
    audit_sources = []
    suggestions = []
    if audit:
        s = audit.get("summary")
        if isinstance(s, str):
            try:
                audit_summary = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                audit_summary = {}
        else:
            audit_summary = s or {}
        src = audit.get("sources")
        if isinstance(src, str):
            try:
                audit_sources = json.loads(src)
            except (json.JSONDecodeError, TypeError):
                audit_sources = []
        else:
            audit_sources = src or []
        conn = models.db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM suggestions WHERE audit_id=%s ORDER BY impact, created_at", (audit["id"],))
            suggestions = cur.fetchall()
            for sg in suggestions:
                cf = sg.get("compliance_flags")
                if isinstance(cf, str):
                    try:
                        sg["compliance_flags"] = json.loads(cf)
                    except (json.JSONDecodeError, TypeError):
                        sg["compliance_flags"] = []
                elif cf is None:
                    sg["compliance_flags"] = []
        finally:
            conn.close()

    visibility_rows = []
    if audit:
        cols, rows = models.ch_query(
            "SELECT prompt, cited, position, competitors_cited, detail "
            "FROM default.ai_visibility_checks "
            f"WHERE brand_id = {int(brand_id)} "
            "ORDER BY ts "
            "FORMAT TabSeparatedWithNames"
        )
        visibility_rows = [dict(zip(cols, row)) for row in rows] if cols else []

    return render_template("brand_report.html", active='engagements',
                           brand=brand, brand_properties=brand_properties,
                           competitors=competitors, audit=audit,
                           audit_summary=audit_summary, audit_sources=audit_sources,
                           suggestions=suggestions, visibility_rows=visibility_rows,
                           summary_json=json.dumps(audit_summary, indent=2, default=str) if audit_summary else "")


# ── Client onboard (create engagement) ──────────────────────────

@app.route("/onboard", methods=["POST"])
def onboard_client():
    ctype = request.form.get("type", "").strip()
    user_input = request.form.get("input", "").strip()
    client_name = request.form.get("name", "").strip() or user_input.split(".")[0].title() if ctype == "black_box" else user_input[:50]

    if not ctype or ctype not in ("marketing_only", "existing_code_marketing", "clean_slate"):
        return jsonify({"ok": False, "error": "valid type required"}), 400
    if not user_input:
        return jsonify({"ok": False, "error": "input required"}), 400

    # Map to legacy types
    legacy_map = {"marketing_only": "black_box", "existing_code_marketing": "import_repo", "clean_slate": "new_project"}
    legacy_type = legacy_map[ctype]

    try:
        conn = models.db()
        cur = conn.cursor()
        brand_id = None
        project_id_task = None

        if ctype == "marketing_only":
            domain = user_input
            slug = re.sub(r'[^a-z0-9]+', '-', domain.split(".")[0].lower()).strip('-')
            cur.execute("INSERT INTO brands (name, slug, access_tier) VALUES (%s, %s, '0') ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name RETURNING id", (client_name, slug))
            brand_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO brand_properties (brand_id, property_type, value, accessible) VALUES (%s, 'domain', %s, true) ON CONFLICT DO NOTHING", (brand_id, domain))
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

@app.route("/competitors")
def competitors():
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.brand_id, c.domain, c.name, c.scan_enabled,
                   c.sitemap_url, c.last_scanned_at, b.name AS brand_name,
                   (SELECT count(*) FROM competitor_pages p WHERE p.competitor_id = c.id) AS total_pages,
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


@app.route("/content/<int:item_id>/preview")
def content_preview(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, body, structured, content_blocks FROM content_items WHERE id=%s", (item_id,))
        ci = cur.fetchone()
        if not ci:
            return "Not found", 404
        banner = ('<div style="background:#fff7d6;border:1px solid #e6cc66;'
                  'padding:10px 14px;margin-bottom:16px;border-radius:6px;font-size:14px;">'
                  '&#9888; Preview only &#8212; final appearance depends on the target project\'s own site.'
                  '</div>')
        # New pipeline output: render typed content_blocks.
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
            blocks = cp.ensure_slot_images(item_id, blocks)
            if any(b.get("type") == "image_slot" for b in blocks):
                cur.execute("UPDATE content_items SET content_blocks=%s, updated_at=now() WHERE id=%s",
                            (json.dumps(blocks), item_id))
                conn.commit()
            content = banner + cp.render_pipeline_css() + \
                f"<div class='pipeline-article'>{cp.render_content_blocks(blocks, ci['title'])}</div>"
            return render_template("base.html", title=ci['title'], content=content)
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
            content = banner + "".join(parts)
        else:
            html = markdown.markdown(ci['body'] or '*No content generated yet.*')
            content = banner + f"<h1>{ci['title']}</h1>{html}"
        return render_template("base.html", title=ci['title'], content=content)
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
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE content_items SET status='approved', updated_at=now() WHERE id=%s", (ci_id,))
        conn.commit()
        models.ch_trace({"project": "system", "actor": "agent", "action": "content_approved",
                         "detail": f"Content item {ci_id} approved", "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True})
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
    ok, msg = models.run_approval(approval_id, decision)
    if ok:
        return "", 200
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
        "FROM default.events ORDER BY ts DESC LIMIT 20 FORMAT TabSeparatedWithNames"
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


# ── Consoles ─────────────────────────────────────────────────────

@app.route("/consoles/pg")
def console_pg():
    ap = os.environ.get("ADMINER_PORT", "")
    if ap:
        return redirect(f"http://100.64.0.1:{ap}")
    return "<p>Adminer not configured</p>"


@app.route("/consoles/ch")
def console_ch():
    return redirect("http://100.64.0.1:8123/play")


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
        models.ch_trace({"project": "brands", "actor": "human", "action": "brand_audit_created",
                         "detail": f"Brand audit task {tid} for {domain}", "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/suggestions/<int:sid>/approve", methods=["POST"])
def suggestion_approve(sid):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE suggestions SET status='approved', updated_at=now() WHERE id=%s AND status='pending'", (sid,))
        conn.commit()
        models.ch_trace({"project": "system", "actor": "agent", "action": "suggestion_approved",
                         "detail": f"Suggestion {sid} → approved", "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True})
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
        cur.execute("SELECT cv.file_path, p.name AS slug FROM concept_variations cv JOIN projects p ON p.id=cv.project_id WHERE cv.id=%s", (vid,))
        row = cur.fetchone()
        if row and row[0]:
            fpath = f"/home/agency/projects/{row[1]}/{row[0]}"
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
    finally:
        conn.close()
    return render_template("task_detail.html", t=d, ci=ci, outline_ci=outline_ci)


# ── Job Search Routes ──────────────────────────────────────────

@app.route("/jobs")
def jobs():
    data = models.get_job_campaigns()
    stats = models.get_job_stats()
    return render_template("jobs.html", campaigns=data, stats=stats)


@app.route("/jobs/<int:campaign_id>")
def job_campaign_detail(campaign_id):
    camp = models.get_job_campaign(campaign_id)
    if not camp:
        return redirect("/jobs")
    listings = models.get_job_listings(campaign_id)
    applications = models.get_job_applications(campaign_id)
    run_history = models.get_job_run_history(campaign_id)
    email_threads = models.get_email_threads(campaign_id)
    return render_template("job_detail.html", campaign=camp, listings=listings,
                           applications=applications, run_history=run_history,
                           email_threads=email_threads)


@app.route("/jobs/<int:campaign_id>/listings")
def job_listings_fragment(campaign_id):
    status = request.args.get("status")
    listings = models.get_job_listings(campaign_id, status)
    return render_template("fragments/job_listings.html", listings=listings, campaign_id=campaign_id)


@app.route("/jobs/<int:campaign_id>/applications")
def job_applications_fragment(campaign_id):
    apps = models.get_job_applications(campaign_id)
    return render_template("fragments/job_applications.html", applications=apps)


@app.route("/jobs/<int:campaign_id>/run", methods=["POST"])
def job_campaign_run(campaign_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"campaign_id": campaign_id})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('run_job_campaign', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        models.ch_trace({"project": f"jobs-c{campaign_id}", "actor": "human", "action": "campaign_run",
                         "detail": f"Campaign {campaign_id} run triggered, task {task_id}",
                         "gate": "green", "decision": "proceed", "ok": 1})
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/jobs/new", methods=["GET", "POST"])
def job_campaign_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        titles = request.form.get("job_titles", "").strip()
        locations = request.form.get("locations", "").strip()
        resume_text = request.form.get("resume_text", "").strip()
        target = int(request.form.get("target_jobs_per_run", 10))
        interval = int(request.form.get("run_interval_hours", 24))

        if not name or not resume_text:
            return jsonify({"ok": False, "error": "name and resume_text required"}), 400

        conn = models.db()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO job_campaigns (name, status, target_jobs_per_run, run_interval_hours, resume_text, job_titles, locations)
                   VALUES (%s, 'draft', %s, %s, %s, %s, %s) RETURNING id""",
                (name, target, interval, resume_text,
                 [t.strip() for t in titles.split(",") if t.strip()],
                 [l.strip() for l in locations.split(",") if l.strip()]),
            )
            cid = cur.fetchone()["id"]
            conn.commit()
            models.ch_trace({"project": "jobs", "actor": "human", "action": "campaign_created",
                             "detail": f"Campaign '{name}' created, id={cid}",
                             "gate": "green", "decision": "proceed", "ok": 1})
            return jsonify({"ok": True, "campaign_id": cid})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        finally:
            conn.close()

    return render_template("job_new.html")


@app.route("/jobs/<int:campaign_id>/settings", methods=["POST"])
def job_campaign_settings(campaign_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        fields = ["name", "status", "target_jobs_per_run", "run_interval_hours",
                   "resume_text", "min_salary", "max_applications_per_company",
                   "follow_up_days", "max_follow_ups"]
        updates = []
        vals = []
        for f in fields:
            v = request.form.get(f)
            if v is not None:
                updates.append(f"{f}=%s")
                vals.append(v)
        if updates:
            vals.append(campaign_id)
            cur.execute(f"UPDATE job_campaigns SET {', '.join(updates)}, updated_at=now() WHERE id=%s", vals)
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/jobs/<int:campaign_id>/gmail-auth")
def job_gmail_auth(campaign_id):
    """Initiate Gmail OAuth for a campaign."""
    sys.path.insert(0, "/home/agency/agency-os/scripts")
    import importlib
    ga = importlib.import_module("jobs.gmail_auth")
    url = ga.get_auth_url(state=str(campaign_id))
    return redirect(url)


@app.route("/jobs/gmail-callback")
def job_gmail_callback():
    """OAuth callback (handles code exchange for installed apps flow)."""
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return render_template("base.html", content="<p>No authorization code received. Please try again.</p>")

    sys.path.insert(0, "/home/agency/agency-os/scripts")
    import importlib
    ga = importlib.import_module("jobs.gmail_auth")
    token = ga.exchange_code(code)
    encrypted = ga.encrypt_token(token)

    campaign_id = int(state) if state else None
    conn = models.db()
    try:
        cur = conn.cursor()
        if campaign_id:
            cur.execute("UPDATE job_campaigns SET gmail_token=%s, gmail_oauth_state='authorized' WHERE id=%s",
                        (encrypted, campaign_id))
        conn.commit()
    except Exception as e:
        return render_template("base.html", content=f"<p>Error storing token: {e}</p>")
    finally:
        conn.close()

    return render_template("base.html", content="<p>Gmail authorized successfully! You can close this tab.</p>")


@app.route("/jobs/<int:listing_id>/contact")
def job_listing_contact(listing_id):
    """View/manage contacts for a listing."""
    contacts = models.get_job_contacts(listing_id)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM job_listings WHERE id=%s", (listing_id,))
        listing = cur.fetchone()
    finally:
        conn.close()
    if not listing:
        return "Not found", 404
    return render_template("job_contacts.html", contacts=contacts, listing=listing)


@app.route("/jobs/<int:listing_id>/generate-contact", methods=["POST"])
def job_generate_contact(listing_id):
    """Trigger contact discovery for a listing."""
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"listing_id": listing_id})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('find_contacts', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/jobs/<int:listing_id>/generate-resume", methods=["POST"])
def job_generate_resume(listing_id):
    """Trigger resume tailoring for a listing."""
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"listing_id": listing_id})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('generate_resume', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/jobs/<int:listing_id>/generate-cover-letter", methods=["POST"])
def job_generate_cover_letter(listing_id):
    """Trigger cover letter generation for a listing."""
    conn = models.db()
    try:
        cur = conn.cursor()
        params = json.dumps({"listing_id": listing_id})
        cur.execute("INSERT INTO tasks (type, params) VALUES ('generate_cover_letter', %s) RETURNING id", (params,))
        task_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/jobs/applications")
def job_all_applications():
    apps = models.get_job_applications()
    return render_template("job_applications.html", applications=apps)


# ── Aetheria game-project dashboard ──────────────────────────────

@app.route("/aetheria")
def aetheria():
    return render_template("aetheria.html")


@app.route("/aetheria/data")
def aetheria_data():
    status = models.get_game_status()
    work_blocks = models.get_game_work_blocks()
    pending_approvals = models.get_game_pending_approvals()
    loop = models.get_loop_status()
    # Format for template
    for t in work_blocks:
        t["created_fmt"] = fmt_ts(t.get("created_at"))
        t["duration"] = fmt_dur(t.get("started_at"), t.get("finished_at"))
        t["tokens"] = (t.get("prompt_tokens") or 0) + (t.get("completion_tokens") or 0)
        t["cost_f"] = f"{float(t.get('cost') or 0):.4f}" if t.get("cost") else "0"
        t["model"] = (t.get("params") or {}).get("model", "")
        # Parse result_ref for commit range + next
        if t.get("result_ref"):
            try:
                t["result"] = json.loads(t["result_ref"])
            except (json.JSONDecodeError, TypeError):
                t["result"] = {}
        else:
            t["result"] = {}
    return render_template("fragments/aetheria_status.html",
                           status=status, work_blocks=work_blocks,
                           pending_approvals=pending_approvals, loop=loop)


@app.route("/aetheria/work-block/<int:tid>/monitor")
def aetheria_block_monitor(tid):
    """Live progress fragment for a work block (self-polling while running)."""
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, status, progress, progress_text, cost, "
                    "prompt_tokens, completion_tokens, finished_at, error, result_ref "
                    "FROM tasks WHERE id=%s", (tid,))
        t = cur.fetchone()
        if t:
            t = {k: dec_to_num(v) for k, v in dict(t).items()}
            t["tokens"] = (t.get("prompt_tokens") or 0) + (t.get("completion_tokens") or 0)
            t["cost_f"] = f"{float(t.get('cost') or 0):.4f}" if t.get("cost") else "0"
    finally:
        conn.close()
    if not t:
        return "", 404
    return render_template("_task_monitor.html", t=t)


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
