"""HTTP API.

Identity: the caller is named by the `X-User` header. That is a stand-in for
the company's SSO (see DESIGN.md, "What I cut"). Every decision is made per
request against the database: there are no permission-bearing tokens, so a
revocation takes effect on the very next request.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import policy as P
from . import service as S
from .db import connect, init_schema, norm_ts, parse_ts, shift, utcnow, iso

app = FastAPI(title="NFPL Access Control Service", version="1.0",
              description="Decides who may do what on which job, explains why, and remembers.")
logger = logging.getLogger(__name__)

_DB_PATH: Optional[str] = None


def configure(db_path: str | None):
    global _DB_PATH
    _DB_PATH = db_path


def get_conn():
    conn = connect(_DB_PATH)
    init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def caller(x_user: str = Header(..., description="Acting user id (stand-in for SSO)")) -> str:
    return x_user


def clock(at: Optional[str] = None) -> str:
    return norm_ts(at) if at else utcnow()


@app.exception_handler(S.ServiceError)
def _service_error(_req: Request, exc: S.ServiceError):
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(Exception)
def _unexpected(_req: Request, exc: Exception):
    logger.exception("Unexpected authorization service failure")
    return JSONResponse(status_code=500, content={
        "error": "internal error; request could not be completed; verify write state before retrying",
        "type": type(exc).__name__})


@app.exception_handler(sqlite3.OperationalError)
def _database_unavailable(_req: Request, exc: sqlite3.OperationalError):
    logger.exception("Authorization storage is unavailable")
    return JSONResponse(status_code=503, content={
        "error": "authorization storage is unavailable; request could not be completed; "
                 "verify write state before retrying"})


# ------------------------------------------------------------------- models
class ActionBody(BaseModel):
    reason: Optional[str] = None
    expected_version: Optional[int] = None
    assignee_id: Optional[str] = None
    op: Optional[str] = None


class EditBody(BaseModel):
    data: dict[str, Any] = {}
    client_id: Optional[str] = None
    branch_id: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    reason: Optional[str] = None
    expected_version: Optional[int] = None


class CreateJobBody(BaseModel):
    client_id: str
    branch_id: str
    direction: str
    mode: str
    data: dict[str, Any] = {}


class RequestBody(BaseModel):
    kind: str
    reason: str


class DecideBody(BaseModel):
    note: Optional[str] = None


class GrantBody(BaseModel):
    user_id: str
    role_id: str
    client_id: Optional[str] = None
    branch_id: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    reason: str


class ReasonBody(BaseModel):
    reason: str


class RoleBody(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    audience: str = "staff"
    permissions: list[str]
    reason: Optional[str] = None


class RoleUpdateBody(BaseModel):
    permissions: list[str]
    reason: str


class AbsenceBody(BaseModel):
    user_id: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: str
    delegate_id: Optional[str] = None
    reason: Optional[str] = None


class SyncBody(BaseModel):
    run_id: Optional[str] = None
    items: list[dict[str, Any]]


# --------------------------------------------------------------- reference
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home():
    return """<h2>NFPL Access Control Service</h2><p>API docs: <a href='/docs'>/docs</a>.
    Send <code>X-User: &lt;user id&gt;</code> on every call. See README.md for the demo script.</p>"""


@app.get("/permissions")
def permissions():
    return {"permissions": P.PERMISSIONS, "privileged": sorted(P.PRIVILEGED_PERMISSIONS),
            "actions": {k: {"needs": v.perms, "stages": sorted(v.stages) if v.stages else "any",
                            "separation_of_duties": v.sod, "reason_required": v.reason_required,
                            "account_types": sorted(v.kinds)} for k, v in P.ACTIONS.items()}}


@app.get("/roles")
def list_roles(conn=Depends(get_conn), me: str = Depends(caller)):
    _require_auditor(conn, me)
    now = utcnow()
    return [S.get_role(conn, r[0], now) for r in conn.execute("SELECT id FROM roles ORDER BY id")]


@app.get("/users")
def list_users(conn=Depends(get_conn), me: str = Depends(caller)):
    _require_auditor(conn, me)
    return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


@app.get("/users/{user_id}/access")
def user_access(user_id: str, at: Optional[str] = None, conn=Depends(get_conn), me: str = Depends(caller)):
    if me != user_id:
        _require_auditor(conn, me)
    elif not P.user_active(P.get_user(conn, me), utcnow()):
        raise S.Denied("inactive accounts cannot read access details")
    return S.describe_access(conn, user_id, clock(at))


# -------------------------------------------------------------------- jobs
@app.get("/jobs")
def jobs(conn=Depends(get_conn), me: str = Depends(caller), client_id: Optional[str] = None,
         branch_id: Optional[str] = None, direction: Optional[str] = None, mode: Optional[str] = None,
         stage: Optional[str] = None, assignee_id: Optional[str] = None,
         limit: int = Query(50, le=500), offset: int = 0):
    f = dict(client_id=client_id, branch_id=branch_id, direction=direction, mode=mode, stage=stage,
             assignee_id=assignee_id)
    return S.visible_jobs(conn, me, utcnow(), f, limit, offset)


@app.post("/jobs", status_code=201)
def create_job(body: CreateJobBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.create_job(conn, me, body.model_dump(), utcnow())


@app.get("/jobs/{job_id}")
def get_job(job_id: str, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.visible_job(conn, me, job_id, utcnow())


@app.patch("/jobs/{job_id}")
def edit_job(job_id: str, body: EditBody, conn=Depends(get_conn), me: str = Depends(caller)):
    changes = {k: v for k, v in body.model_dump().items() if v is not None and k not in ("reason", "expected_version")}
    return S.edit_job(conn, me, job_id, changes, utcnow(), body.reason, body.expected_version)


@app.post("/jobs/{job_id}/actions/{action}")
def job_action(job_id: str, action: str, body: ActionBody = ActionBody(), conn=Depends(get_conn),
               me: str = Depends(caller)):
    return S.perform(conn, me, job_id, action, utcnow(), body.reason, body.expected_version,
                     body.assignee_id, body.op)


@app.get("/jobs/{job_id}/eligible")
def eligible(job_id: str, conn=Depends(get_conn), me: str = Depends(caller)):
    S.visible_job(conn, me, job_id, utcnow())
    return S.eligible_for_next(conn, job_id, utcnow())


@app.post("/jobs/{job_id}/claim")
def claim(job_id: str, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.claim(conn, me, job_id, utcnow())


@app.post("/jobs/{job_id}/requests", status_code=201)
def create_request(job_id: str, body: RequestBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.create_request(conn, me, job_id, body.kind, body.reason, utcnow())


@app.post("/requests/{request_id}/approve")
def approve_request(request_id: int, body: DecideBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.decide_request(conn, me, request_id, True, utcnow(), body.note)


@app.post("/requests/{request_id}/reject")
def reject_request(request_id: int, body: DecideBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.decide_request(conn, me, request_id, False, utcnow(), body.note)


@app.get("/jobs/{job_id}/history")
def history(job_id: str, conn=Depends(get_conn), me: str = Depends(caller)):
    S.visible_job(conn, me, job_id, utcnow())
    return S.job_history(conn, job_id)


@app.get("/jobs/{job_id}/who-could")
def who_could(job_id: str, date: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
              actions: Optional[str] = None, conn=Depends(get_conn), me: str = Depends(caller)):
    """`date=2026-10-03` means that whole UTC day; or give start/end."""
    _require_auditor(conn, me)
    if date:
        s = norm_ts(date)
        e = shift(s, days=1)
    elif start and end:
        s, e = norm_ts(start), norm_ts(end)
    else:
        raise S.BadRequest("give date=YYYY-MM-DD or start and end")
    acts = actions.split(",") if actions else None
    return S.who_could(conn, job_id, s, e, acts)


def _require_auditor(conn, me):
    now = utcnow()
    user = P.get_user(conn, me)
    if not P.user_active(user, now) or user["kind"] != "staff":
        raise S.Denied(f"{me} is not an active staff account")
    if not any("access.read" in g["permissions"] for g in P.grants_at(conn, me, now)):
        raise S.Denied(f"{me} does not hold access.read (needed for audit questions)")


# ----------------------------------------------------------------- explain
@app.get("/explain")
def explain(user: str, action: str, job: str, at: Optional[str] = None, conn=Depends(get_conn),
            me: str = Depends(caller)):
    if me != user:
        _require_auditor(conn, me)
    return S.explain(conn, user, action, job, clock(at))


@app.get("/explain/compare")
def compare(users: str, action: str, job: str, at: Optional[str] = None, conn=Depends(get_conn),
            me: str = Depends(caller)):
    a, b = [u.strip() for u in users.split(",")][:2]
    _require_auditor(conn, me)
    return S.compare(conn, a, b, action, job, clock(at))


# ------------------------------------------------------------------- queues
@app.get("/me/inbox")
def my_inbox(limit: int = Query(50, le=500), offset: int = 0, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.inbox(conn, me, utcnow(), limit, offset)


@app.get("/me/approvals")
def my_approvals(conn=Depends(get_conn), me: str = Depends(caller)):
    return S.pending_approvals(conn, me, utcnow())


@app.get("/me/access")
def my_access(conn=Depends(get_conn), me: str = Depends(caller)):
    if not P.user_active(P.get_user(conn, me), utcnow()):
        raise S.Denied("inactive accounts cannot read access details")
    return S.describe_access(conn, me, utcnow())


# ------------------------------------------------------------ access admin
@app.post("/grants", status_code=201)
def create_grant(body: GrantBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.create_grant(conn, me, body.model_dump(), utcnow())


@app.get("/grants")
def list_grants(user_id: Optional[str] = None, status: Optional[str] = None, conn=Depends(get_conn),
                me: str = Depends(caller)):
    _require_auditor(conn, me)
    sql, params = "SELECT g.*, r.name AS role_name FROM grants g JOIN roles r ON r.id=g.role_id WHERE 1=1", []
    if user_id:
        sql += " AND g.user_id=?"
        params.append(user_id)
    if status:
        sql += " AND g.status=?"
        params.append(status)
    return [dict(r) for r in conn.execute(sql + " ORDER BY g.id", params)]


@app.post("/grants/{gid}/approve")
def approve_grant(gid: int, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.approve_grant(conn, me, gid, utcnow(), True)


@app.post("/grants/{gid}/reject")
def reject_grant(gid: int, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.approve_grant(conn, me, gid, utcnow(), False)


@app.post("/grants/{gid}/revoke")
def revoke_grant(gid: int, body: ReasonBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.revoke_grant(conn, me, gid, body.reason, utcnow())


@app.post("/roles", status_code=201)
def create_role(body: RoleBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.create_role(conn, me, body.model_dump(), utcnow())


@app.put("/roles/{rid}")
def update_role(rid: str, body: RoleUpdateBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.update_role(conn, me, rid, body.permissions, body.reason, utcnow())


@app.post("/roles/{rid}/versions/{version}/approve")
def approve_role(rid: str, version: int, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.approve_role_version(conn, me, rid, version, utcnow(), True)


@app.post("/absences", status_code=201)
def absence(body: AbsenceBody, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.set_absence(conn, me, body.model_dump(), utcnow())


@app.post("/absences/{aid}/end")
def end_absence(aid: int, conn=Depends(get_conn), me: str = Depends(caller)):
    return S.end_absence(conn, me, aid, utcnow())


@app.get("/audit")
def audit(target_type: Optional[str] = None, target_id: Optional[str] = None, limit: int = Query(100, le=1000),
          conn=Depends(get_conn), me: str = Depends(caller)):
    _require_auditor(conn, me)
    sql, params = "SELECT * FROM access_events WHERE 1=1", []
    if target_type:
        sql += " AND target_type=?"
        params.append(target_type)
    if target_id:
        sql += " AND target_id=?"
        params.append(target_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit])]


# ------------------------------------------------------------------ parser
@app.post("/parser/sync")
def parser_sync(body: SyncBody, conn=Depends(get_conn), me: str = Depends(caller)):
    user = P.get_user(conn, me)
    if not P.user_active(user, utcnow()) or user["kind"] != "service":
        raise S.Denied("only an active parser service account may call the feed endpoint")
    return S.parser_sync(conn, me, body.items, utcnow(), body.run_id)
