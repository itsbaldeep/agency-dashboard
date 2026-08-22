import concurrent.futures
import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import docker
import psycopg2
import psycopg2.extras
import requests

DB_HOST = os.environ.get("DB_HOST", "agency-postgres")
DB_NAME = os.environ.get("DB_NAME", "agencyos")
DB_USER = os.environ.get("DB_USER", "agency")
DB_PASS = os.environ.get("DB_PASS") or os.environ.get("POSTGRES_PASSWORD")

CH_HOST = os.environ.get("CH_HOST", "agency-clickhouse")
CH_USER = os.environ.get("CH_USER", "agency")
CH_PASS = os.environ.get("CH_PASS") or os.environ.get("CLICKHOUSE_PASSWORD")


def db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def ch_query(sql):
    url = f"http://{CH_HOST}:8123/"
    try:
        r = requests.post(url, auth=(CH_USER, CH_PASS), data=sql, timeout=10)
        if r.status_code == 404:
            return [], []
        r.raise_for_status()
        text = r.text.strip()
        if text:
            lines = text.split("\n")
            cols = lines[0].split("\t")
            rows = [line.split("\t") for line in lines[1:]]
            return cols, rows
    except Exception as e:
        print(f"ClickHouse error: {e}", flush=True)
    return [], []


def ch_trace(event):
    try:
        cols = ["project","session_id","actor","action","detail","gate","decision","ok"]
        vals = [str(event.get(k, "")) for k in cols]
        sql = "INSERT INTO default.events (" + ",".join(cols) + ") FORMAT TabSeparated\n" + "\t".join(vals)
        requests.post(f"http://{CH_HOST}:8123/", auth=(CH_USER, CH_PASS), data=sql, timeout=5)
    except Exception:
        pass


def _container_stat(c):
    cname = c.name
    try:
        cstats = c.stats(stream=False)
    except Exception:
        return None
    cpu_delta = cstats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - \
                cstats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    sys_delta = cstats.get("cpu_stats", {}).get("system_cpu_usage", 0) - \
                cstats.get("precpu_stats", {}).get("system_cpu_usage", 0)
    num_cpus = len(cstats.get("cpu_stats", {}).get("cpu_usage", {}).get("percpu_usage", [])) or 1
    cpu_perc = 0.0
    if sys_delta > 0 and cpu_delta > 0:
        cpu_perc = round((cpu_delta / sys_delta) * num_cpus * 100.0, 2)
    mem_stats = cstats.get("memory_stats", {})
    mem_used = mem_stats.get("usage", 0)
    mem_limit = mem_stats.get("limit", 0)
    mem_perc = round((mem_used / mem_limit) * 100.0, 2) if mem_limit > 0 else 0.0
    pids = cstats.get("pids_stats", {}).get("current", 0)
    return {
        "Container": cname,
        "CPUPerc": f"{cpu_perc}%",
        "MemUsage": f"{mem_used / (1024 * 1024):.0f}MiB / {mem_limit / (1024 * 1024):.0f}MiB",
        "MemPerc": f"{mem_perc}%",
        "PIDs": str(pids),
    }

def get_docker_stats():
    dc = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    stats = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(_container_stat, dc.containers.list()):
            if result:
                stats.append(result)
    return {s["Container"]: s for s in stats}


def _host_memory():
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                parts = line.split()
                if parts[0] == "MemTotal:":
                    info["total_kb"] = int(parts[1])
                elif parts[0] == "MemAvailable:":
                    info["avail_kb"] = int(parts[1])
        if "total_kb" in info and "avail_kb" in info:
            total_mb = info["total_kb"] / 1024
            avail_mb = info["avail_kb"] / 1024
            used_mb = total_mb - avail_mb
            return {"total_gb": round(total_mb / 1024, 1), "used_gb": round(used_mb / 1024, 1),
                    "avail_gb": round(avail_mb / 1024, 1), "used_perc": f"{round(used_mb / total_mb * 100)}%",
                    "total_mb": round(total_mb), "used_mb": round(used_mb), "avail_mb": round(avail_mb)}
    except Exception:
        pass
    return {}


def _host_cpu():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().strip().split()
            return {"load_1m": parts[0], "load_5m": parts[1], "load_15m": parts[2]}
    except Exception:
        return {}


def _host_disk():
    try:
        out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        parts = out.stdout.strip().split("\n")[-1].split()
        if len(parts) >= 5:
            return {"size": parts[1], "used": parts[2], "avail": parts[3], "use_perc": parts[4]}
    except Exception:
        pass
    return {}


def _parse_mem_val(val):
    val = str(val).strip()
    if val.endswith("MiB"):
        return float(val[:-3])
    if val.endswith("GiB"):
        return float(val[:-3]) * 1024
    if val.endswith("KiB"):
        return float(val[:-3]) / 1024
    try:
        return float(val) / (1024 * 1024)
    except ValueError:
        return 0


def _caddy_sites():
    caddy_dir = "/caddy-apps"
    sites = {}
    try:
        for fname in os.listdir(caddy_dir):
            if fname.endswith(".caddy"):
                content = open(os.path.join(caddy_dir, fname)).read()
                m = re.search(r'^(\S+)\s*\{[^}]*reverse_proxy\s+[\d.]+:(\d+)', content, re.MULTILINE)
                if m:
                    hostname = m.group(1)
                    port = m.group(2)
                    sites[port] = f"https://{hostname}"
    except Exception:
        pass
    return sites


# ── Unified Engagement Model ─────────────────────────────────────

def _engagement_row(ref_type, ref_id, name, onboarding, client_status, has_code,
                    brand_id, brand_name, brand_slug, access_tier,
                    project_id, project_name, project_state, repo_url,
                    created_at, intake_params, agent_allowed=False,
                    classification=None, lifecycle=None, recovery_ref=None):
    return {
        "ref_type": ref_type, "ref_id": ref_id,
        "name": name or project_name or brand_name or "Unknown",
        "onboarding": onboarding,
        "client_status": client_status or "active",
        "has_code": has_code,
        "brand_id": brand_id, "brand_name": brand_name, "brand_slug": brand_slug,
        "access_tier": access_tier or "0",
        "project_id": project_id, "project_name": project_name,
        "project_state": project_state, "repo_url": repo_url,
        "agent_allowed": agent_allowed,
        "classification": classification,
        "lifecycle": lifecycle,
        "recovery_ref": recovery_ref,
        "created_at": created_at,
        "intake_params": intake_params,
        "last_audit_date": None, "audit_count": 0,
        "suggestion_pending": 0, "content_count": 0,
        "content_draft_count": 0, "has_report": False,
        "capability_count": 0, "last_activity": None,
    }


def _engagement_type_badge(e):
    """Classify an engagement as Black-box / Code / Scaffolded."""
    if e.get("project_id") and e.get("project_state") in (
            "scaffolded", "building", "preview", "staged", "live"):
        return "Scaffolded"
    if e.get("has_code") or e.get("project_id"):
        return "Code"
    return "Black-box"


def get_engagements():
    """Merge projects, clients, brands into unified engagement cards."""
    conn = db()
    engs = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, type, status, brand_id, project_id, created_at, intake_params FROM clients ORDER BY created_at DESC")
        for c in cur.fetchall():
            ctype = c["type"]
            engs.append(_engagement_row(
                ref_type="client", ref_id=c["id"],
                name=c["name"], onboarding={
                    "black_box": "marketing_only",
                    "import_repo": "existing_code_marketing",
                    "new_project": "clean_slate",
                }.get(ctype, "marketing_only"),
                client_status=c["status"], has_code=ctype != "black_box",
                brand_id=c["brand_id"], brand_name=None, brand_slug=None, access_tier=None,
                project_id=c["project_id"], project_name=None, project_state=None, repo_url=None,
                created_at=c["created_at"], intake_params=c["intake_params"],
            ))

        # Project engagements not represented by a client/brand. Core is shown
        # here only when it is also a marketing engagement (Deployden).
        cur.execute("""
            SELECT p.* FROM projects p
            WHERE p.id NOT IN (SELECT project_id FROM clients WHERE project_id IS NOT NULL)
              AND p.id NOT IN (SELECT project_id FROM brands WHERE project_id IS NOT NULL)
              AND p.lifecycle <> 'hard_parked'
              AND (p.classification='engagement' OR lower(p.name)='deployden')
            ORDER BY p.created_at DESC
        """)
        for p in cur.fetchall():
            engs.append(_engagement_row(
                ref_type="project", ref_id=p["id"],
                name=p["name"], onboarding="clean_slate",
                client_status=p["state"] or "building", has_code=True,
                brand_id=None, brand_name=None, brand_slug=None, access_tier=None,
                project_id=p["id"], project_name=p["name"],
                project_state=p["state"], repo_url=p["repo_url"],
                created_at=p["created_at"], intake_params=None,
                classification=p.get("classification"), lifecycle=p.get("lifecycle"),
                recovery_ref=p.get("recovery_ref"),
            ))

        # Orphan brands (no client)
        cur.execute("""
            SELECT b.* FROM brands b
            WHERE b.id NOT IN (SELECT brand_id FROM clients WHERE brand_id IS NOT NULL)
            ORDER BY b.created_at DESC
        """)
        for b in cur.fetchall():
            engs.append(_engagement_row(
                ref_type="brand", ref_id=b["id"],
                name=b["name"], onboarding="marketing_only",
                client_status="active", has_code=False,
                brand_id=b["id"], brand_name=b["name"],
                brand_slug=b["slug"], access_tier=b["access_tier"],
                project_id=b["project_id"], project_name=None, project_state=None, repo_url=None,
                created_at=b["created_at"], intake_params=None,
            ))

    finally:
        conn.close()

    # Fetch linked names + agent_allowed for client rows
    conn2 = db()
    try:
        cur2 = conn2.cursor()
        for e in engs:
            if e["brand_id"]:
                cur2.execute("SELECT name FROM brands WHERE id=%s", (e["brand_id"],))
                r = cur2.fetchone()
                if r: e["brand_name"] = r["name"]
            if e["project_id"]:
                cur2.execute("SELECT name,state,repo_url,agent_allowed,classification,lifecycle,recovery_ref "
                             "FROM projects WHERE id=%s", (e["project_id"],))
                r = cur2.fetchone()
                if r:
                    e["project_name"] = r["name"]
                    e["project_state"] = r["state"]
                    e["repo_url"] = r["repo_url"]
                    e["agent_allowed"] = bool(r["agent_allowed"])
                    e["classification"] = r.get("classification")
                    e["lifecycle"] = r.get("lifecycle")
                    e["recovery_ref"] = r.get("recovery_ref")
    finally:
        conn2.close()

    engs = [e for e in engs if e.get("lifecycle") != "hard_parked"]
    _enrich_engagements(engs)
    engs.sort(key=lambda e: e["last_activity"] or e["created_at"] or datetime.min, reverse=True)
    return engs


def _enrich_engagements(engs):
    """Bulk-fetch per-engagement metrics (audits, suggestions, content, tasks,
    capabilities) and merge them into the engagement dicts in place."""
    brand_ids = [e["brand_id"] for e in engs if e["brand_id"]]
    project_ids = [e["project_id"] for e in engs if e["project_id"]]
    if not brand_ids and not project_ids:
        return
    conn = db()
    try:
        cur = conn.cursor()
        # ── Brand-scoped metrics (audits, suggestions, content) ──
        if brand_ids:
            cur.execute("""
                SELECT b.id AS brand_id,
                    (SELECT max(created_at) FROM audits WHERE brand_id=b.id) AS last_audit_date,
                    (SELECT count(*) FROM audits WHERE brand_id=b.id) AS audit_count,
                    (SELECT count(*) FROM suggestions WHERE brand_id=b.id AND status='pending') AS suggestion_pending,
                    (SELECT count(*) FROM content_items WHERE brand_id=b.id) AS content_count,
                    (SELECT count(*) FROM content_items WHERE brand_id=b.id AND status='draft') AS content_draft_count
                FROM brands b WHERE b.id = ANY(%s)
            """, (brand_ids,))
            bmap = {r["brand_id"]: r for r in cur.fetchall()}
            # Brand-scoped task activity (params->>'brand_id')
            cur.execute("""
                SELECT (params->>'brand_id')::int AS bid, max(created_at) AS last_activity
                FROM tasks WHERE (params->>'brand_id')::int = ANY(%s)
                GROUP BY bid
            """, (brand_ids,))
            tmap = {r["bid"]: r["last_activity"] for r in cur.fetchall()}
        else:
            bmap, tmap = {}, {}

        # ── Project-scoped metrics (capabilities + task activity) ──
        if project_ids:
            cur.execute("""
                SELECT project_id, count(*) AS capability_count
                FROM capabilities WHERE project_id = ANY(%s::bigint[])
                GROUP BY project_id
            """, (project_ids,))
            cmap = {r["project_id"]: r["capability_count"] for r in cur.fetchall()}
            cur.execute("""
                SELECT (params->>'project_id')::bigint AS pid, max(created_at) AS last_activity
                FROM tasks WHERE (params->>'project_id')::bigint = ANY(%s::bigint[])
                GROUP BY pid
            """, (project_ids,))
            ptask = {r["pid"]: r["last_activity"] for r in cur.fetchall()}
        else:
            cmap, ptask = {}, {}

        for e in engs:
            bid = e["brand_id"]
            if bid and bid in bmap:
                m = bmap[bid]
                e["last_audit_date"] = m["last_audit_date"]
                e["audit_count"] = m["audit_count"] or 0
                e["suggestion_pending"] = m["suggestion_pending"] or 0
                e["content_count"] = m["content_count"] or 0
                e["content_draft_count"] = m["content_draft_count"] or 0
                e["has_report"] = (m["audit_count"] or 0) > 0
                if bid in tmap:
                    e["last_activity"] = tmap[bid]
            pid = e["project_id"]
            if pid and pid in cmap:
                e["capability_count"] = cmap[pid]
            if pid and pid in ptask:
                la = ptask[pid]
                if not e["last_activity"] or (la and la > e["last_activity"]):
                    e["last_activity"] = la
            # Fall back to last_audit_date if no task activity yet
            if not e["last_activity"] and e["last_audit_date"]:
                e["last_activity"] = e["last_audit_date"]
            # Audit age in days (for color coding); None if no audit
            lad = e["last_audit_date"]
            if lad:
                try:
                    if lad.tzinfo is None:
                        lad = lad.replace(tzinfo=timezone.utc)
                    e["audit_age_days"] = (datetime.now(timezone.utc) - lad).days
                except Exception:
                    e["audit_age_days"] = None
            else:
                e["audit_age_days"] = None
    finally:
        conn.close()


def get_engagement_detail(ref_type, ref_id):
    """Full detail for a single engagement by type and id."""
    conn = db()
    try:
        cur = conn.cursor()
        e = None

        if ref_type == "client":
            cur.execute("""
                SELECT c.id, c.name, c.type AS client_type, c.status AS client_status,
                       c.brand_id, c.project_id, c.intake_params, c.created_at,
                       b.name AS brand_name, b.slug AS brand_slug, b.access_tier,
                       p.name AS project_name, p.state AS project_state,
                       p.repo_url, p.prd_path
                FROM clients c
                LEFT JOIN brands b ON b.id = c.brand_id
                LEFT JOIN projects p ON p.id = c.project_id
                WHERE c.id = %s
            """, (ref_id,))
            row = cur.fetchone()
            if not row:
                return None
            e = dict(row)
            e["ref_type"] = "client"
            e["ref_id"] = ref_id
            ctype = e.get("client_type", "black_box")
            e["onboarding"] = {"black_box": "marketing_only", "import_repo": "existing_code_marketing", "new_project": "clean_slate"}.get(ctype, "marketing_only")
            e["has_code"] = ctype != "black_box"
            if isinstance(e.get("intake_params"), str):
                try:
                    e["intake_params"] = json.loads(e["intake_params"])
                except (json.JSONDecodeError, TypeError):
                    e["intake_params"] = {}

        elif ref_type == "project":
            cur.execute("SELECT * FROM projects WHERE id=%s", (ref_id,))
            row = cur.fetchone()
            if not row:
                return None
            e = dict(row)
            e["ref_type"] = "project"
            e["ref_id"] = ref_id
            e["onboarding"] = "clean_slate"
            e["has_code"] = True
            e["client_name"] = row["name"]
            e["client_status"] = e.get("state", "building")
            e["intake_params"] = None
            e["project_id"] = e["id"]
            e["project_name"] = e["name"]
            e["project_state"] = e.get("state", "building")
            e["brand_id"] = None
            e["brand_name"] = None

        elif ref_type == "brand":
            cur.execute("""
                SELECT b.*, bp.enabled_stages, bp.schedule_cron,
                       p.name AS project_name,p.state AS project_state,p.repo_url,
                       p.local_path,p.classification,p.lifecycle,p.recovery_ref
                FROM brands b
                LEFT JOIN brand_pipelines bp ON bp.brand_id = b.id
                LEFT JOIN projects p ON p.id=b.project_id
                WHERE b.id = %s
            """, (ref_id,))
            row = cur.fetchone()
            if not row:
                return None
            e = dict(row)
            e["ref_type"] = "brand"
            e["ref_id"] = ref_id
            e["onboarding"] = "marketing_only"
            e["has_code"] = bool(e.get("local_path"))
            e["client_name"] = row["name"]
            e["client_status"] = "active"
            e["intake_params"] = None
            e["project_id"] = row.get("project_id")
            e["project_name"] = row.get("project_name")
            e["brand_id"] = e["id"]
            e["brand_name"] = e["name"]

        if e is None:
            return None

        # ── Code section (services, DNS, docker stats, design) ──
        e["services"] = []
        e["dns_records"] = []
        if e.get("project_id"):
            pid = e["project_id"]
            cur.execute("SELECT * FROM services WHERE project_id=%s ORDER BY name", (pid,))
            e["services"] = cur.fetchall()
            cur.execute("SELECT * FROM dns_records WHERE project_id=%s ORDER BY created_at DESC", (pid,))
            e["dns_records"] = cur.fetchall()

            stats = get_docker_stats()
            for svc in e["services"]:
                cname = svc.get("container")
                svc["docker"] = stats.get(cname, {}) if cname else {}

            sites = _caddy_sites()
            for svc in e["services"]:
                ps = str(svc.get("port", ""))
                svc["hostname"] = sites.get(ps, "")

            # Design variations for this project
            cur.execute("SELECT * FROM concept_variations WHERE project_id=%s AND skill='design_page' ORDER BY created_at DESC", (pid,))
            e["designs"] = cur.fetchall()
            for d in e["designs"]:
                if isinstance(d.get("spec_json"), str):
                    try:
                        d["spec_json"] = json.loads(d["spec_json"])
                    except (json.JSONDecodeError, TypeError):
                        d["spec_json"] = {}
        else:
            e["designs"] = []

        # ── Marketing section (brand data) ──
        e["audits"] = []
        e["suggestions"] = []
        e["content_items"] = []
        e["competitors"] = []
        e["brand_properties"] = []
        if e.get("brand_id"):
            bid = e["brand_id"]
            cur.execute("SELECT * FROM brand_properties WHERE brand_id=%s", (bid,))
            e["brand_properties"] = cur.fetchall()
            cur.execute("SELECT * FROM competitors WHERE brand_id=%s ORDER BY domain", (bid,))
            e["competitors"] = cur.fetchall()
            cur.execute("SELECT * FROM audits WHERE brand_id=%s ORDER BY created_at DESC", (bid,))
            e["audits"] = cur.fetchall()
            cur.execute("SELECT * FROM suggestions WHERE brand_id=%s ORDER BY created_at DESC", (bid,))
            e["suggestions"] = cur.fetchall()
            cur.execute("SELECT * FROM content_items WHERE brand_id=%s ORDER BY created_at DESC", (bid,))
            e["content_items"] = cur.fetchall()
            for a in e["audits"]:
                if isinstance(a.get("summary"), str):
                    try:
                        a["summary"] = json.loads(a["summary"])
                    except (json.JSONDecodeError, TypeError):
                        a["summary"] = {}
        return e
    finally:
        conn.close()


# ── Dashboard Overview ───────────────────────────────────────────

def get_overview():
    mem = _host_memory()
    cpu = _host_cpu()
    disk = _host_disk()

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM clients")
        client_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM projects WHERE lifecycle <> 'hard_parked'")
        project_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM projects WHERE lifecycle = 'soft_parked'")
        parked_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM brands")
        brand_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='queued'")
        queued_tasks = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='running'")
        running_tasks = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='needs_input'")
        needs_input_tasks = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM approvals WHERE status='pending'")
        pending_approvals = cur.fetchone()["c"]
        cur.execute("""
            SELECT id,type,status,left(COALESCE(error,''),180) AS error,created_at
            FROM tasks
            WHERE status IN ('failed','orphaned')
              AND created_at >= now()-interval '24 hours'
            ORDER BY created_at DESC LIMIT 8
        """)
        task_incidents = cur.fetchall()
        cur.execute("""
            SELECT t.id,t.type,t.created_at,
                   CASE WHEN t.type='execute_approval' THEN 'approval' ELSE 'workflow' END AS source
            FROM tasks t
            WHERE t.status='needs_input'
            ORDER BY t.created_at LIMIT 8
        """)
        input_queue = cur.fetchall()
        cur.execute("""
            SELECT j.name,r.id,r.status,left(COALESCE(r.detail,''),180) AS detail,r.started_at
            FROM job_runs r JOIN background_jobs j ON j.id=r.job_id
            WHERE r.status='failed' AND r.started_at >= now()-interval '24 hours'
            ORDER BY r.started_at DESC LIMIT 8
        """)
        job_incidents = cur.fetchall()
        cur.execute("""
            SELECT count(*) FILTER (WHERE status='done') AS done,
                   count(*) FILTER (WHERE status='failed') AS failed
            FROM tasks
            WHERE type IN ('content_research','content_outline','content_compose',
                           'generate_draft','publish_content')
              AND created_at >= now()-interval '7 days'
        """)
        content_reliability = cur.fetchone()
        cur.execute("""
            SELECT s.container,p.name AS project_name,s.name AS service_name
            FROM services s JOIN projects p ON p.id=s.project_id
            WHERE p.lifecycle='active' AND s.status='running' AND s.container IS NOT NULL
            ORDER BY p.name,s.name
        """)
        expected_services = cur.fetchall()
    finally:
        conn.close()

    try:
        running_containers = {
            c.name for c in docker.DockerClient(base_url="unix:///var/run/docker.sock").containers.list()
        }
    except Exception:
        running_containers = set()
    service_incidents = [s for s in expected_services if s["container"] not in running_containers]

    cols, rows = ch_query(
        "SELECT ts, project, actor, action, detail, gate, decision, ok "
        "FROM default.events "
        "WHERE ok = 0 OR gate NOT IN ('', 'green') "
        "   OR action NOT IN ('health_check','job_completed','memory_sweep') "
        "ORDER BY ts DESC LIMIT 12 "
        "FORMAT TabSeparatedWithNames"
    )
    events = [dict(zip(cols, row)) for row in rows] if cols else []

    return {
        "memory": mem, "cpu": cpu, "disk": disk,
        "client_count": client_count, "project_count": project_count,
        "parked_count": parked_count,
        "brand_count": brand_count, "queued_tasks": queued_tasks,
        "running_tasks": running_tasks, "needs_input_tasks": needs_input_tasks,
        "pending_approvals": pending_approvals,
        "task_incidents": task_incidents, "job_incidents": job_incidents,
        "input_queue": input_queue, "service_incidents": service_incidents,
        "content_reliability": content_reliability,
        "events": events,
    }


# ── Content ──────────────────────────────────────────────────────

def get_content():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ci.*, b.name AS brand_name,
                   t.id AS task_id, t.cost AS task_cost,
                   t.started_at AS task_started, t.finished_at AS task_finished,
                   t.params->>'model' AS task_model,
                   CASE WHEN t.started_at IS NOT NULL AND t.finished_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (t.finished_at - t.started_at))::int
                        ELSE NULL END AS task_duration_s
            FROM content_items ci
            JOIN brands b ON b.id = ci.brand_id
            LEFT JOIN tasks t ON t.id = ci.task_id
            ORDER BY ci.created_at DESC
        """)
        articles = cur.fetchall()

        cur.execute("""
            SELECT cv.*, p.name AS project_name
            FROM concept_variations cv
            JOIN projects p ON p.id = cv.project_id
            WHERE cv.skill = 'design_page'
            ORDER BY cv.created_at DESC
        """)
        designs = cur.fetchall()
        for d in designs:
            if isinstance(d.get("spec_json"), str):
                try:
                    d["spec_json"] = json.loads(d["spec_json"])
                except (json.JSONDecodeError, TypeError):
                    d["spec_json"] = {}
        return {"articles": articles, "designs": designs}
    finally:
        conn.close()


# ── Operations (Jobs + Approvals) ───────────────────────────────

def get_jobs():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM background_jobs ORDER BY name")
        jobs = cur.fetchall()
        for j in jobs:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='running') AS running,
                       COUNT(*) FILTER (WHERE status='completed') AS completed,
                       COUNT(*) FILTER (WHERE status='failed') AS failed,
                       MAX(started_at) AS last_run
                FROM job_runs WHERE job_id=%s
            """, (j["id"],))
            j.update(cur.fetchone())
        return jobs
    finally:
        conn.close()


def get_job_runs(limit=50, job_id=None):
    conn = db()
    try:
        cur = conn.cursor()
        if job_id:
            cur.execute("""
                SELECT r.*, j.name AS job_name FROM job_runs r
                JOIN background_jobs j ON j.id=r.job_id
                WHERE r.job_id=%s ORDER BY r.started_at DESC LIMIT %s
            """, (job_id, limit))
        else:
            cur.execute("""
                SELECT r.*, j.name AS job_name FROM job_runs r
                JOIN background_jobs j ON j.id=r.job_id
                ORDER BY r.started_at DESC LIMIT %s
            """, (limit,))
        return cur.fetchall()
    finally:
        conn.close()


def get_approvals(pending_only=True):
    conn = db()
    try:
        cur = conn.cursor()
        if pending_only:
            cur.execute("""
                SELECT a.*, COALESCE(p.name, 'system') AS project_name
                FROM approvals a
                LEFT JOIN projects p ON p.id = a.project_id
                WHERE a.status = 'pending'
                ORDER BY a.requested_at DESC
            """)
        else:
            cur.execute("""
                SELECT a.*, COALESCE(p.name, 'system') AS project_name
                FROM approvals a
                LEFT JOIN projects p ON p.id = a.project_id
                ORDER BY a.requested_at DESC LIMIT 50
            """)
        approvals = cur.fetchall()
        for a in approvals:
            if isinstance(a.get("payload"), str):
                try:
                    a["payload"] = json.loads(a["payload"])
                except (json.JSONDecodeError, TypeError):
                    a["payload"] = {}
        return approvals
    finally:
        conn.close()


def run_approval(approval_id, decision, note=""):
    if decision not in ("approve", "reject"):
        return False, "decision must be 'approve' or 'reject'"
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,type::text,status::text,task_id FROM approvals WHERE id=%s FOR UPDATE", (approval_id,))
        approval = cur.fetchone()
        if not approval or approval["status"] != "pending":
            return False, "approval not found or already decided"
        if decision == "reject":
            cur.execute(
                "UPDATE approvals SET status='rejected', decided_at=now(), note=%s WHERE id=%s",
                (note[:1000], approval_id),
            )
            conn.commit()
            return True, ""

        cur.execute(
            "INSERT INTO tasks (type,status,params,triggered_by) "
            "VALUES ('execute_approval','queued',%s,'dashboard-approval') RETURNING id",
            (json.dumps({"approval_id": approval_id, "operator_input": note[:2000]}),),
        )
        task_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE approvals SET status='approved', decided_at=now(), note=%s, task_id=%s WHERE id=%s",
            (note[:1000], task_id, approval_id),
        )
        conn.commit()
        ch_trace({"project": "system", "actor": "agent", "action": "approval_approved",
                  "detail": f"Approval {approval_id} approved; task={task_id or 'mechanical'}",
                  "gate": "green", "decision": "proceed", "ok": 1})
        return True, str(task_id or "")
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


# ── Health ──────────────────────────────────────────────────────

def get_health():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.name, s.kind, s.port, s.status, s.container, s.mem_limit_mb, p.name AS project_name
            FROM services s JOIN projects p ON p.id = s.project_id
            ORDER BY p.name, s.name
        """)
        services = cur.fetchall()
        cur.execute("SELECT * FROM health_checks ORDER BY ts DESC LIMIT 30")
        checks = cur.fetchall()
        return {"services": services, "checks": checks}
    finally:
        conn.close()


# ── Spend ────────────────────────────────────────────────────────

def get_spend():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(prj.name, 'system') AS project_name,
                   COUNT(*) AS total_tasks,
                   SUM(t.cost) AS total_cost
            FROM tasks t
            LEFT JOIN LATERAL (
                SELECT p.name,
                       p.id = CASE WHEN t.params->>'project_id' ~ '^[0-9]+$'
                                   THEN (t.params->>'project_id')::int END AS bypid,
                       lower(p.repo_name) = lower(t.params->>'repo')       AS byrepo
                FROM projects p
                WHERE p.id = CASE WHEN t.params->>'project_id' ~ '^[0-9]+$'
                                  THEN (t.params->>'project_id')::int END
                   OR lower(p.repo_name) = lower(t.params->>'repo')
                   OR lower(p.name)      = lower(t.params->>'repo')
                ORDER BY bypid DESC NULLS LAST, byrepo DESC NULLS LAST, p.id
                LIMIT 1
            ) prj ON true
            WHERE t.cost IS NOT NULL
            GROUP BY project_name
            ORDER BY total_cost DESC NULLS LAST
        """)
        by_project = cur.fetchall()
        cur.execute("""
            SELECT SUM(prompt_tokens) AS total_tokens_in,
                   SUM(completion_tokens) AS total_tokens_out,
                   SUM(cost) AS total_cost,
                   COUNT(*) AS total_calls
            FROM tasks WHERE cost IS NOT NULL
        """)
        totals = cur.fetchone()
        return {"by_project": by_project, "totals": totals}
    finally:
        conn.close()


# ── Resources ───────────────────────────────────────────────────

def get_resources():
    stats = get_docker_stats()
    mem = _host_memory()
    disk = _host_disk()

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM projects ORDER BY name")
        all_projects = cur.fetchall()
        cur.execute("""
            SELECT s.name AS service_name, s.container, s.mem_limit_mb,
                   p.name AS project_name
            FROM services s
            JOIN projects p ON p.id = s.project_id
            ORDER BY p.name, s.name
        """)
        db_services = cur.fetchall()
    finally:
        conn.close()

    container_map = {s["container"]: s for s in db_services if s["container"]}
    project_map = {p["name"]: {"name": p["name"], "services": [], "cpu_sum": 0.0, "mem_mb": 0.0}
                   for p in all_projects}
    total_mb = 0.0

    for cname, s in stats.items():
        svc = container_map.get(cname)
        proj_name = svc["project_name"] if svc else "unmanaged"
        svc_name = svc["service_name"] if svc else cname

        mem_used = s.get("MemUsage", "0B / 0B").split(" / ")[0]
        mem_mb = _parse_mem_val(mem_used)
        total_mb += mem_mb

        entry = {
            "container": cname, "project": proj_name, "service": svc_name,
            "cpu_perc": s.get("CPUPerc", "0%"), "mem_used": mem_used,
            "mem_mb": mem_mb, "mem_perc": s.get("MemPerc", "0%"),
            "pids": s.get("PIDs", "0"),
        }
        project_map.setdefault(proj_name, {"name": proj_name, "services": [], "cpu_sum": 0.0, "mem_mb": 0.0})
        project_map[proj_name]["services"].append(entry)
        project_map[proj_name]["mem_mb"] += mem_mb
        project_map[proj_name]["cpu_sum"] += float(s.get("CPUPerc", "0%").rstrip("%"))

    return {
        "projects": sorted(project_map.values(), key=lambda p: p["cpu_sum"], reverse=True),
        "memory": mem, "disk": disk,
        "total_container_mem_mb": round(total_mb, 0),
        "container_count": len(stats),
    }


# ── Activity ────────────────────────────────────────────────────

def get_activity(ref_type, ref_id, name):
    """Recent events and tasks for a specific engagement."""
    conn = db()
    tasks = []
    try:
        cur = conn.cursor()
        key = {"brand": "brand_id", "project": "project_id", "client": "client_id"}.get(ref_type)
        cur.execute("""
            SELECT id, type, status, params, error, created_at, started_at, finished_at
            FROM tasks
            WHERE (%s IS NOT NULL AND params->>%s = %s)
               OR params->>'domain' = %s
               OR params->>'repo' = %s
            ORDER BY created_at DESC LIMIT 20
        """, (key, key, str(ref_id), name, name))
        tasks = cur.fetchall()
    except Exception:
        tasks = []
    finally:
        conn.close()

    events = []
    identity = f"{ref_type}:{ref_id}"
    safe_identity = identity.replace("'", "\\'")
    safe_name = name.replace("'", "\\'")
    try:
        cols, rows = ch_query(
            "SELECT ts, actor, action, detail, gate, decision, ok "
            "FROM default.events "
            f"WHERE project IN ('{safe_identity}','{safe_name}') "
            "ORDER BY ts DESC LIMIT 20 "
            "FORMAT TabSeparatedWithNames"
        )
        events = [dict(zip(cols, row)) for row in rows] if cols else []
    except Exception:
        events = []

    return {"tasks": tasks, "events": events}


# ── System Map ──────────────────────────────────────────────────

def get_system_data():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, schedule, enabled, requires_approval FROM background_jobs ORDER BY name")
        jobs = cur.fetchall()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        tables = [r["table_name"] for r in cur.fetchall()]
    finally:
        conn.close()

    networks = []
    try:
        dc = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        networks = sorted([n.name for n in dc.networks.list() if n.name.startswith("net-") and n.name != "net-control"])
    except Exception:
        pass

    skills = []
    skills_dir = "/home/agency/projects/.skills"
    if os.path.isdir(skills_dir):
        skills = sorted([f.replace(".md", "") for f in os.listdir(skills_dir) if f.endswith(".md")])

    return {
        "jobs": jobs, "tables": tables, "networks": networks, "skills": skills,
        "task_types": {
            "run_brand_audit": "Full black-box brand recon: crawl -> classify -> competitors -> visibility -> suggestions",
            "generate_draft": "Generate blog/article from suggestion + brand context with compliance check",
            "propose_fix": "Git branch + OpenCode headless + GitHub PR (human review required)",
            "client_import_repo": "Clone public repo, analyze, generate AGENTS.md, create project",
            "client_new_project": "Scaffold new project from brief on GitHub + push",
            "design_page": "Two-stage: generate concept specs, then render chosen spec to HTML/CSS/JS",
            "competitor_scan": "Sitemap scan for competitor pages (deterministic, zero LLM)",
        },
    }
    if e.get("classification") == "core":
        return "Core"
