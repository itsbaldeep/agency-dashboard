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
                           dev_activity=dev_activity, pending=pending)


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
            "VALUES ('onboard_project', 'queued', %s, 'dashboard')", (params,))
        conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()
    return redirect("/projects")


# ── Content ─────────────────────────────────────────────────────

@app.route("/content")
def content():
    return render_template("content.html")


@app.route("/content/data")
def content_data():
    data = models.get_content()
    return render_template("fragments/content_list.html", **data)


@app.route("/content/<int:item_id>/preview")
def content_preview(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, body, structured FROM content_items WHERE id=%s", (item_id,))
        ci = cur.fetchone()
        if not ci:
            return "Not found", 404
        banner = ('<div style="background:#fff7d6;border:1px solid #e6cc66;'
                  'padding:10px 14px;margin-bottom:16px;border-radius:6px;font-size:14px;">'
                  '&#9888; Preview only &#8212; final appearance depends on the target project\'s own site.'
                  '</div>')
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
        cur.execute("SELECT id, type, status, prompt_tokens, completion_tokens, cost, result_ref, error, finished_at FROM tasks WHERE id=%s", (tid,))
        t = cur.fetchone()
        if not t:
            return jsonify({"ok": False, "error": "not found"}), 404
        resp = {k: dec_to_num(v) for k, v in dict(t).items()}
        return jsonify(resp)
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


# ── Dev tasks (propose_fix) ─────────────────────────────────────

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
        return render_template("dev_tasks.html", tasks=tasks_list, repos=sorted(["hearth", "streamwise", "dashboard"]))
    finally:
        conn.close()


@app.route("/api/dev-tasks/create", methods=["POST"])
def dev_task_create():
    repo = request.form.get("repo", "")
    desc = request.form.get("description", "")
    base = request.form.get("base", "main")
    if repo not in ("hearth", "streamwise", "dashboard"):
        return jsonify({"ok": False, "error": f"Invalid repo: {repo}"}), 400
    if not desc.strip():
        return jsonify({"ok": False, "error": "description required"}), 400
    conn = models.db()
    try:
        cur = conn.cursor()
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
            SELECT id, type, status, cost, triggered_by, created_at, finished_at,
                   COALESCE(params->>'spec', params->>'description', params->>'prompt', params->>'question', '') AS gist
            FROM tasks ORDER BY id DESC LIMIT 50
        """)
        tasks_list = cur.fetchall()
    finally:
        conn.close()
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
    finally:
        conn.close()
    return render_template("task_detail.html", t=d)


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
