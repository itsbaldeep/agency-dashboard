"""Read-only dashboard view of the core enquiry inbox."""

import os
import re
import sqlite3
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, make_response, render_template, request

import models
from marketing_connections import _brand_project


enquiries = Blueprint("core_enquiries", __name__)
ENQUIRY_ROOT = Path("/agency-enquiries")
UNAVAILABLE = "Enquiries are unavailable."
_MAX_ID = 2147483647
_MAX_SOURCE_ID = 9223372036854775807
_MAX_MAPPING_BYTES = 2048
_MAX_MAPPINGS = 64
_MAPPING = re.compile(r"[1-9][0-9]*:[1-9][0-9]*\Z", re.ASCII)
_MAX_TEXT = {"name": 200, "email": 200, "company": 200, "message": 2000}


def _owners():
    """Return the strict owner mapping, or an empty mapping if invalid."""
    raw = os.environ.get("AGENCY_CORE_ENQUIRY_OWNERS", "")
    if not raw:
        return {}
    try:
        if len(raw.encode("ascii")) > _MAX_MAPPING_BYTES:
            return {}
    except (UnicodeEncodeError, AttributeError):
        return {}
    owners = {}
    projects = set()
    try:
        items = raw.split(",")
        if len(items) > _MAX_MAPPINGS:
            return {}
        for item in items:
            if not _MAPPING.fullmatch(item):
                return {}
            brand_text, project_text = item.split(":", 1)
            brand_id, project_id = int(brand_text), int(project_text)
            if (brand_id <= 0 or project_id <= 0 or brand_id > _MAX_ID or project_id > _MAX_ID
                    or brand_id in owners or project_id in projects
                    or str(brand_id) != brand_text or str(project_id) != project_text):
                return {}
            owners[brand_id] = project_id
            projects.add(project_id)
    except (TypeError, ValueError, OverflowError):
        return {}
    return owners


def configured_for_brand(brand_id, project_id):
    """Whether the strict environment mapping assigns this core project."""
    if type(brand_id) is not int or type(project_id) is not int:
        return False
    return (0 < brand_id <= _MAX_ID and 0 < project_id <= _MAX_ID
            and _owners().get(brand_id) == project_id)


def _safe_database(path):
    """Require every fixed path component to be real and the leaf regular."""
    try:
        current = Path(path)
        for component in [*current.parents[::-1], current]:
            info = os.lstat(component)
            if stat.S_ISLNK(info.st_mode):
                return False
            if component == current:
                if not stat.S_ISREG(info.st_mode):
                    return False
            elif not stat.S_ISDIR(info.st_mode):
                return False
        return True
    except (OSError, ValueError):
        return False


def _text(value, field):
    if type(value) is not str or len(value) > _MAX_TEXT[field]:
        raise ValueError("bad enquiry shape")
    allowed_message = field == "message"
    for char in value:
        code = ord(char)
        if code < 32 or code == 127:
            if not (allowed_message and char in "\r\n\t"):
                raise ValueError("bad enquiry shape")
    return value


def _read_rows(path, before=None):
    if not _safe_database(path):
        return None
    connection = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + 1.0
        connection.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
        schema = connection.execute("PRAGMA table_info(leads)").fetchall()
        columns = {row["name"]: row for row in schema}
        id_column = columns.get("id")
        if id_column is None or id_column["pk"] != 1:
            return None
        query = "SELECT id,name,email,company,message,created_at,notified FROM leads"
        params = ()
        if before is not None:
            query += " WHERE id < ?"
            params = (before,)
        rows = connection.execute(query + " ORDER BY id DESC LIMIT 51", params).fetchall()
        has_more = len(rows) > 50
        rows = rows[:50]
        result = []
        for row in rows:
            lead_id = row["id"]
            if type(lead_id) is not int or not (0 < lead_id <= _MAX_SOURCE_ID):
                raise ValueError("bad enquiry shape")
            created_at = row["created_at"]
            if type(created_at) is not str or not created_at or len(created_at) > 64:
                raise ValueError("bad enquiry shape")
            if any(ord(char) < 32 or ord(char) == 127 for char in created_at):
                raise ValueError("bad enquiry shape")
            received = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if received.tzinfo is None:
                received = received.replace(tzinfo=UTC)
            created_at = received.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
            notified = row["notified"]
            if type(notified) is int and notified in (0, 1):
                sent = bool(notified)
            else:
                raise ValueError("bad enquiry shape")
            result.append({
                "id": lead_id,
                "name": _text(row["name"], "name"),
                "email": _text(row["email"], "email"),
                "company": _text(row["company"], "company"),
                "message": _text(row["message"], "message"),
                "created_at": created_at or "",
                "notified": sent,
            })
        return result, (result[-1]["id"] if has_more and result else None)
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, OverflowError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _response(**context):
    response = make_response(render_template("marketing_enquiries.html", **context))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@enquiries.route("/brands/<int:brand_id>/enquiries")
def brand_enquiries(brand_id):
    if set(request.args) - {"before"} or len(request.args.getlist("before")) > 1:
        return _response(available=False, rows=[], brand_id=brand_id, message=UNAVAILABLE), 400
    before = request.args.get("before")
    if before is not None:
        try:
            before_id = int(before)
        except (TypeError, ValueError, OverflowError):
            before_id = None
        if (not re.fullmatch(r"[1-9][0-9]*", before, re.ASCII)
                or before_id is None or str(before_id) != before or before_id > _MAX_SOURCE_ID):
            return _response(available=False, rows=[], brand_id=brand_id, message=UNAVAILABLE), 400
        before = before_id
    owners = _owners()
    project_id = owners.get(brand_id)
    if not project_id:
        return _response(available=False, rows=[], brand_id=brand_id, message=UNAVAILABLE)
    connection = None
    try:
        connection = models.db()
        brand = _brand_project(connection.cursor(), brand_id)
    except Exception:
        brand = None
    finally:
        if connection is not None:
            connection.close()
    if not brand or brand.get("classification") != "core" or brand.get("lifecycle") != "active" or brand.get("project_id") != project_id:
        return _response(available=False, rows=[], brand_id=brand_id, message=UNAVAILABLE)
    result = _read_rows(ENQUIRY_ROOT / str(brand_id) / "leads.db", before=before)
    if result is None:
        return _response(available=False, rows=[], brand_id=brand_id, message=UNAVAILABLE)
    rows, next_before = result
    return _response(available=True, rows=rows, next_before=next_before, brand_id=brand_id, message="")
