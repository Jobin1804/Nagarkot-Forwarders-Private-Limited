"""Operations. Every function takes an explicit `now` so the same code can be
driven by the API (real clock), the seed script (historic dates) and tests.

Pattern for every job mutation:
    1. load job  2. decide()  3. log the decision (allowed or not)
    4. refuse with the full explanation, or apply + write an event that records
       the snapshot and the grants/approvals that authorised it.
"""
from __future__ import annotations

import json

from . import policy as P
from .db import dumps, norm_ts, row_dict, tx

MIN_REASON = 10


# ------------------------------------------------------------------ errors
class ServiceError(Exception):
    status = 400

    def __init__(self, message, decision=None, **extra):
        super().__init__(message)
        self.message = message
        self.decision = decision
        self.extra = extra

    def to_dict(self):
        out = {"error": self.message, **self.extra}
        if self.decision is not None:
            out["decision"] = self.decision.to_dict() if hasattr(self.decision, "to_dict") else self.decision
        return out


class Denied(ServiceError):
    status = 403


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class BadRequest(ServiceError):
    status = 400


# ----------------------------------------------------------------- helpers
def names(conn) -> dict:
    out = {}
    for t in ("branches", "clients", "users"):
        for r in conn.execute(f"SELECT id, name FROM {t}"):
            out[r["id"]] = r["name"]
    return out


def get_job(conn, job_id) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"job {job_id} not found")
    j = dict(row)
    j["data"] = json.loads(j["data"])
    return j


def _require_reason(reason, what):
    if not reason or len(reason.strip()) < MIN_REASON:
        raise BadRequest(f"{what} needs a written reason of at least {MIN_REASON} characters")
    return reason.strip()


def log_decision(conn, d: P.Decision):
    conn.execute("INSERT INTO decision_log(at,user_id,action,job_id,allowed,summary) VALUES (?,?,?,?,?,?)",
                 (d.at, d.user_id, d.action, d.job_id, int(d.allowed), d.summary))


def access_event(conn, at, actor, action, target_type, target_id, detail=None):
    conn.execute("INSERT INTO access_events(at,actor_id,action,target_type,target_id,detail) VALUES (?,?,?,?,?,?)",
                 (at, actor, action, target_type, str(target_id), dumps(detail or {})))


def _snapshot(job) -> str:
    s = dict(job)
    if isinstance(s.get("data"), str):
        s["data"] = json.loads(s["data"])
    return dumps(s)


def _event(conn, job, at, actor, action, from_stage, reason=None, detail=None, authority=None):
    conn.execute(
        """INSERT INTO job_events(job_id,at,actor_id,action,from_stage,to_stage,reason,detail,authority,snapshot)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (job["id"], at, actor, action, from_stage, job["stage"], reason, dumps(detail or {}),
         dumps(authority or {}), _snapshot(job)),
    )


def _save_job(conn, job, expected_version=None):
    cur = conn.execute(
        """UPDATE jobs SET client_id=?,branch_id=?,direction=?,mode=?,stage=?,assignee_id=?,prepared_by=?,
                           checked_by=?,filed_by=?,needs_attention=?,data=?,version=version+1,updated_at=?
           WHERE id=? AND version=?""",
        (job["client_id"], job["branch_id"], job["direction"], job["mode"], job["stage"], job["assignee_id"],
         job["prepared_by"], job["checked_by"], job["filed_by"], job["needs_attention"], dumps(job["data"]),
         job["updated_at"], job["id"], expected_version if expected_version is not None else job["version"]),
    )
    if cur.rowcount != 1:
        raise Conflict(f"job {job['id']} was changed by someone else; reload and try again")
    job["version"] = (expected_version if expected_version is not None else job["version"]) + 1


def _authorise(conn, actor, action, job, now, ctx=None, log=True):
    d = P.decide(conn, actor, action, job, now, ctx, names=names(conn))
    should_log = log and P.ACTIONS.get(action, P.ACTIONS["view"]).mutating
    if not d.allowed:
        err = Denied(d.summary, decision=d)
        if should_log:  # survives the rollback: denied attempts are evidence
            err.after_rollback = lambda c: log_decision(c, d)
        raise err
    if should_log:
        log_decision(conn, d)
    return d


# ===================================================================== jobs
def visible_job(conn, actor, job_id, now) -> dict:
    job = get_job(conn, job_id)
    d = P.decide(conn, actor, "view", job, now)
    if not d.allowed:
        # Don't reveal whether a job the user may not see exists.
        raise NotFound(f"job {job_id} not found")
    perms = set().union(*[g["permissions"] for g in P.grants_at(conn, actor, now) if P.scope_matches(g, job)])
    return job if "job.view" in perms else client_view(job)


def client_view(job) -> dict:
    """What a client's own employee may see: their shipment's progress, nothing internal."""
    public_data = {k: v for k, v in job["data"].items() if not k.startswith("internal_")}
    return {"id": job["id"], "external_ref": job["external_ref"], "client_id": job["client_id"],
            "branch_id": job["branch_id"], "direction": job["direction"], "mode": job["mode"],
            "stage": job["stage"], "updated_at": job["updated_at"], "data": public_data}


def create_job(conn, actor, fields, now, job_id=None) -> dict:
    proto = {f: fields.get(f) for f in P.SCOPE_FIELDS}
    proto.update(stage="Confirmed", prepared_by=None, filed_by=None, id=job_id)
    with tx(conn):
        d = _authorise(conn, actor, "create", proto, now)
        job = _insert_job(conn, proto, fields.get("data") or {}, fields.get("external_ref"), now, job_id)
        _event(conn, job, now, actor, "created", None, detail={"data": job["data"]},
               authority={"grants": d.matched_grants})
    return job


def _insert_job(conn, proto, data, external_ref, now, job_id=None):
    if job_id is None:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] + 1
        job_id = f"J{n:06d}"
        while conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            n += 1
            job_id = f"J{n:06d}"
    conn.execute(
        """INSERT INTO jobs(id,external_ref,client_id,branch_id,direction,mode,stage,data,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'Confirmed',?,?,?)""",
        (job_id, external_ref, proto["client_id"], proto["branch_id"], proto["direction"], proto["mode"],
         dumps(data), now, now),
    )
    return get_job(conn, job_id)


def edit_job(conn, actor, job_id, changes, now, reason=None, expected_version=None) -> dict:
    """Edit data (and, carefully, scope) while the job is Confirmed/Reopened.

    Moving a job to another client/branch/direction/mode changes who can see it,
    so the editor must hold job.edit for BOTH the old and the new scope.
    """
    with tx(conn):
        job = get_job(conn, job_id)
        d_old = _authorise(conn, actor, "edit", job, now)
        new = dict(job)
        scope_change = {f: changes[f] for f in P.SCOPE_FIELDS if f in changes and changes[f] != job[f]}
        new.update(scope_change)
        if scope_change:
            _require_reason(reason, "Moving a job to another client/branch/direction/mode")
            _authorise(conn, actor, "edit", new, now)
        data_changes = changes.get("data") or {}
        before = {k: job["data"].get(k) for k in data_changes}
        new["data"] = {**job["data"], **data_changes}
        new["updated_at"] = now
        _save_job(conn, new, expected_version)
        _event(conn, new, now, actor, "edited", job["stage"], reason,
               detail={"data_before": before, "data_after": data_changes, "scope_change": scope_change},
               authority={"grants": d_old.matched_grants})
    return new


def perform(conn, actor, job_id, action, now, reason=None, expected_version=None, assignee_id=None,
            op=None) -> dict:
    """Stage transitions and job housekeeping (prepare/check/send_back/file/cancel/assign/admin_fix)."""
    if action not in ("prepare", "check", "send_back", "file", "cancel", "assign", "admin_fix"):
        raise BadRequest(f"'{action}' is not a direct job action here "
                         f"(use requests for cancel-filed/reopen, the parser endpoint for sync)")
    spec = P.ACTIONS[action]
    with tx(conn):
        job = get_job(conn, job_id)
        d = _authorise(conn, actor, action, job, now)
        if spec.reason_required:
            reason = _require_reason(reason, f"'{action}'")
        before_stage = job["stage"]
        new = dict(job)
        detail = {}
        if action == "prepare":
            new.update(stage="Prepared", prepared_by=actor, checked_by=None, assignee_id=None)
        elif action == "check":
            new.update(stage="Checked", checked_by=actor, assignee_id=None)
        elif action == "send_back":
            back_to = job["prepared_by"]
            new.update(stage="Confirmed", prepared_by=None, assignee_id=None)
            if back_to and P.decide(conn, back_to, "prepare", new, now).allowed:
                new["assignee_id"] = back_to  # straight back to the maker, if they still can
        elif action == "file":
            new.update(stage="Filed", filed_by=actor, assignee_id=None)
        elif action == "cancel":
            new.update(stage="Cancelled", assignee_id=None)
        elif action == "assign":
            _check_assignee(conn, new, assignee_id, now)
            detail = {"from": job["assignee_id"], "to": assignee_id}
            new["assignee_id"] = assignee_id
        elif action == "admin_fix":
            detail = _admin_fix(new, op)
        new["updated_at"] = now
        _save_job(conn, new, expected_version)
        _event(conn, new, now, actor, action, before_stage, reason, detail=detail,
               authority={"grants": d.matched_grants})
    return new


def _check_assignee(conn, job, assignee_id, now):
    if assignee_id is None:
        return
    nxt = P.NEXT_ACTION.get(job["stage"])
    d = P.decide(conn, assignee_id, nxt, job, now)
    if not d.allowed:
        raise BadRequest(f"cannot assign to {assignee_id}: they could not {nxt} this job", decision=d)
    if assignee_id in P.absences_at(conn, now):
        raise BadRequest(f"cannot assign to {assignee_id}: they are absent now")


def _admin_fix(job, op):
    """Admins can make a stuck job movable again. They can never move it forward,
    never touch Filed/Cancelled jobs (stage rule) and never edit its data."""
    if op == "release":
        before = job["assignee_id"]
        job["assignee_id"] = None
        return {"op": op, "released_from": before}
    if op == "reset_to_confirmed":
        if job["stage"] not in ("Prepared", "Checked"):
            raise BadRequest("reset_to_confirmed only applies to Prepared or Checked jobs")
        before = {k: job[k] for k in ("stage", "prepared_by", "checked_by")}
        job.update(stage="Confirmed", prepared_by=None, checked_by=None, assignee_id=None)
        return {"op": op, "before": before}
    if op == "clear_attention":
        before = job["needs_attention"]
        job["needs_attention"] = None
        return {"op": op, "cleared": before}
    raise BadRequest("admin_fix op must be one of: release, reset_to_confirmed, clear_attention")


def claim(conn, actor, job_id, now) -> dict:
    """Pick up a job from your queue (pool / absent colleague). Needs the authority for the next step."""
    with tx(conn):
        job = get_job(conn, job_id)
        nxt = P.NEXT_ACTION.get(job["stage"])
        if nxt is None:
            raise BadRequest(f"job is {job['stage']}; nothing to claim")
        d = _authorise(conn, actor, nxt, job, now)
        why = P.routed_to(actor, job, P.absences_at(conn, now))
        if why is None:
            raise Denied(f"job is assigned to {job['assignee_id']}, who is present; ask them or a manager")
        before = job["assignee_id"]
        job["assignee_id"] = actor
        job["updated_at"] = now
        _save_job(conn, job)
        _event(conn, job, now, actor, "claimed", job["stage"], detail={"from": before, "why": why},
               authority={"grants": d.matched_grants})
    return job


# ================================================== cancel-filed / reopen
def create_request(conn, actor, job_id, kind, reason, now) -> dict:
    action = {"cancel_filed": "request_cancel_filed", "reopen": "request_reopen"}.get(kind)
    if action is None:
        raise BadRequest("kind must be cancel_filed or reopen")
    with tx(conn):
        job = get_job(conn, job_id)
        d = _authorise(conn, actor, action, job, now)
        reason = _require_reason(reason, "A cancel/reopen request")
        if conn.execute("SELECT 1 FROM requests WHERE job_id=? AND status='pending'", (job_id,)).fetchone():
            raise Conflict("there is already a pending request for this job")
        cur = conn.execute(
            "INSERT INTO requests(job_id,kind,status,reason,requested_by,created_at) VALUES (?,?,?,?,?,?)",
            (job_id, kind, "pending", reason, actor, now))
        rid = cur.lastrowid
        _event(conn, job, now, actor, f"requested_{kind}", job["stage"], reason,
               detail={"request_id": rid}, authority={"grants": d.matched_grants})
    return row_dict(conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())


def decide_request(conn, actor, request_id, approve: bool, now, note=None) -> dict:
    with tx(conn):
        req = row_dict(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())
        if req is None:
            raise NotFound(f"request {request_id} not found")
        if req["status"] != "pending":
            raise Conflict(f"request is already {req['status']}")
        job = get_job(conn, req["job_id"])
        d = _authorise(conn, actor, "approve_request", job, now, ctx={"request": req})
        if req["kind"] == "cancel_filed" and job["stage"] != "Filed":
            raise Conflict(f"job is no longer Filed (it is {job['stage']})")
        note = _require_reason(note, "Approving or rejecting") if approve else (note or "rejected")
        conn.execute("UPDATE requests SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                     ("approved" if approve else "rejected", actor, now, note, request_id))
        authority = {"grants": d.matched_grants, "request_id": request_id,
                     "requested_by": req["requested_by"], "approved_by": actor}
        if not approve:
            _event(conn, job, now, actor, f"rejected_{req['kind']}", job["stage"], note, authority=authority)
        else:
            before = job["stage"]
            if req["kind"] == "cancel_filed":
                job.update(stage="Cancelled", assignee_id=None)
                act = "cancelled_filed"
            else:
                job.update(stage="Reopened", prepared_by=None, checked_by=None, filed_by=None, assignee_id=None)
                act = "reopened"
            job["updated_at"] = now
            _save_job(conn, job)
            _event(conn, job, now, actor, act, before, f"{req['reason']} / approved: {note}", authority=authority)
    return row_dict(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())


# ================================================================= parser
PARSER_ALLOWED = {"external_ref", "client_id", "branch_id", "direction", "mode", "data"}


def parser_sync(conn, actor, items, now, run_id=None) -> dict:
    """Overnight feed. Creates jobs and updates them only while nobody has
    started working on them. Anything it is not sure about is held for a human."""
    results = []
    valid = {t: {r[0] for r in conn.execute(f"SELECT id FROM {t}")} for t in ("clients", "branches")}
    for item in items:
        ref = item.get("external_ref")
        try:
            problems = []
            if not ref:
                problems.append("missing external_ref")
            if item.get("client_id") not in valid["clients"]:
                problems.append(f"unknown client {item.get('client_id')!r}")
            if item.get("branch_id") not in valid["branches"]:
                problems.append(f"unknown branch {item.get('branch_id')!r}")
            if item.get("direction") not in P.DIRECTIONS:
                problems.append(f"bad direction {item.get('direction')!r}")
            if item.get("mode") not in P.MODES:
                problems.append(f"bad mode {item.get('mode')!r}")
            extra = set(item) - PARSER_ALLOWED
            if extra:
                problems.append(f"fields the parser may not set: {sorted(extra)}")
            if problems:
                with tx(conn):
                    access_event(conn, now, actor, "parser_item_rejected", "external_ref", ref or "?",
                                 {"problems": problems, "item": item, "run_id": run_id})
                results.append({"external_ref": ref, "status": "rejected", "problems": problems})
                continue
            results.append(_parser_upsert(conn, actor, item, now, run_id))
        except Denied as e:
            results.append({"external_ref": ref, "status": "denied", "reason": e.message})
    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"run_id": run_id, "summary": summary, "results": results}


def _parser_upsert(conn, actor, item, now, run_id):
    with tx(conn):
        row = conn.execute("SELECT id FROM jobs WHERE external_ref=?", (item["external_ref"],)).fetchone()
        proto = {f: item[f] for f in P.SCOPE_FIELDS}
        if row is None:
            proto.update(stage="Confirmed", id=None)
            d = _authorise(conn, actor, "sync", proto, now)
            job = _insert_job(conn, proto, item.get("data") or {}, item["external_ref"], now)
            _event(conn, job, now, actor, "created_by_parser", None, detail={"run_id": run_id},
                   authority={"grants": d.matched_grants})
            return {"external_ref": item["external_ref"], "status": "created", "job_id": job["id"]}

        job = get_job(conn, row["id"])
        data_diff = {k: v for k, v in (item.get("data") or {}).items() if job["data"].get(k) != v}
        scope_diff = {f: item[f] for f in P.SCOPE_FIELDS if item[f] != job[f]}
        if not data_diff and not scope_diff:
            return {"external_ref": item["external_ref"], "status": "unchanged", "job_id": job["id"]}

        hold_reason = None
        if scope_diff:
            hold_reason = "feed wants to move the job to another client/branch/direction/mode"
        elif job["stage"] != "Confirmed":
            hold_reason = f"job is already {job['stage']}; people are working on it"
        if hold_reason:
            note = f"Parser update held ({now}): {hold_reason}: {dumps({**data_diff, **scope_diff})}"
            job["needs_attention"] = note
            job["updated_at"] = now
            _save_job(conn, job)
            _event(conn, job, now, actor, "parser_update_held", job["stage"], hold_reason,
                   detail={"run_id": run_id, "proposed_data": data_diff, "proposed_scope": scope_diff})
            return {"external_ref": item["external_ref"], "status": "held", "job_id": job["id"],
                    "reason": hold_reason}

        d = _authorise(conn, actor, "sync", job, now)
        before = {k: job["data"].get(k) for k in data_diff}
        job["data"] = {**job["data"], **data_diff}
        job["updated_at"] = now
        _save_job(conn, job)
        _event(conn, job, now, actor, "updated_by_parser", job["stage"],
               detail={"run_id": run_id, "data_before": before, "data_after": data_diff},
               authority={"grants": d.matched_grants})
        return {"external_ref": item["external_ref"], "status": "updated", "job_id": job["id"]}


# ======================================================= grants / roles
def _actor_grant_scopes(conn, actor, perm, now):
    return [g for g in P.grants_at(conn, actor, now) if perm in g["permissions"]]


def _require_access_perm(conn, actor, perm, now, scope=None):
    user = P.get_user(conn, actor)
    if not P.user_active(user, now) or user["kind"] != "staff":
        raise Denied(f"{actor} is not an active staff account")
    held = _actor_grant_scopes(conn, actor, perm, now)
    if not held:
        raise Denied(f"{actor} does not hold {perm}")
    if scope is not None and not any(P.scope_covers(g, scope) for g in held):
        raise Denied(f"{actor} holds {perm} only for "
                     + "; ".join(P.describe_scope(g) for g in held)
                     + f", which does not cover {P.describe_scope(scope)}")
    return held


def create_grant(conn, actor, spec, now) -> dict:
    """Give someone a role for a scope. Never to yourself; privileged roles need a second person."""
    with tx(conn):
        target = P.get_user(conn, spec.get("user_id"))
        if target is None:
            raise NotFound(f"user {spec.get('user_id')} not found")
        role = row_dict(conn.execute("SELECT * FROM roles WHERE id=?", (spec.get("role_id"),)).fetchone())
        if role is None:
            raise NotFound(f"role {spec.get('role_id')} not found")
        scope = {f: spec.get(f) for f in P.SCOPE_FIELDS}
        if target["kind"] == "client":
            if scope["client_id"] not in (None, target["client_id"]):
                raise BadRequest(f"{target['id']} works for client {target['client_id']}; "
                                 f"they can only ever be given access to that client")
            scope["client_id"] = target["client_id"]
        _require_access_perm(conn, actor, "access.grant", now, scope)
        if actor == target["id"]:
            raise Denied("nobody may grant access to themselves; ask another administrator")
        if role["audience"] != target["kind"]:
            raise BadRequest(f"role {role['id']} is for {role['audience']} accounts; "
                             f"{target['id']} is a {target['kind']} account")
        _validate_scope(conn, scope)
        reason = _require_reason(spec.get("reason"), "A grant")
        valid_from = norm_ts(spec.get("valid_from")) or now
        valid_to = norm_ts(spec.get("valid_to"))
        if valid_to and valid_to <= valid_from:
            raise BadRequest("valid_to must be after valid_from")
        privileged = bool(role["privileged"])
        status = "pending" if privileged else "active"
        effective = None if privileged else max(valid_from, now)  # never backdated
        cur = conn.execute(
            """INSERT INTO grants(user_id,role_id,client_id,branch_id,direction,mode,valid_from,valid_to,status,
                                  effective_from,reason,granted_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (target["id"], role["id"], scope["client_id"], scope["branch_id"], scope["direction"], scope["mode"],
             valid_from, valid_to, status, effective, reason, actor, now))
        gid = cur.lastrowid
        access_event(conn, now, actor, "grant_requested" if privileged else "grant_created", "grant", gid,
                     {"user_id": target["id"], "role_id": role["id"], **scope, "valid_from": valid_from,
                      "valid_to": valid_to, "reason": reason})
    return get_grant(conn, gid)


def _validate_scope(conn, scope):
    for f, table in (("client_id", "clients"), ("branch_id", "branches")):
        if scope[f] is not None and not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (scope[f],)).fetchone():
            raise BadRequest(f"unknown {f} {scope[f]}")
    if scope["direction"] not in (None, *P.DIRECTIONS):
        raise BadRequest(f"direction must be one of {P.DIRECTIONS}")
    if scope["mode"] not in (None, *P.MODES):
        raise BadRequest(f"mode must be one of {P.MODES}")


def get_grant(conn, gid) -> dict:
    g = row_dict(conn.execute("SELECT g.*, r.name AS role_name FROM grants g JOIN roles r ON r.id=g.role_id "
                              "WHERE g.id=?", (gid,)).fetchone())
    if g is None:
        raise NotFound(f"grant {gid} not found")
    return g


def approve_grant(conn, actor, gid, now, approve=True) -> dict:
    with tx(conn):
        g = get_grant(conn, gid)
        if g["status"] != "pending":
            raise Conflict(f"grant is {g['status']}, not pending")
        _require_access_perm(conn, actor, "access.grant", now, g)
        if actor in (g["granted_by"], g["user_id"]):
            raise Denied("a privileged grant must be approved by someone other than the granter and the grantee")
        if approve:
            conn.execute("UPDATE grants SET status='active', approved_by=?, effective_from=? WHERE id=?",
                         (actor, max(g["valid_from"], now), gid))
        else:
            conn.execute("UPDATE grants SET status='rejected', approved_by=? WHERE id=?", (actor, gid))
        access_event(conn, now, actor, "grant_approved" if approve else "grant_rejected", "grant", gid,
                     {"user_id": g["user_id"], "role_id": g["role_id"]})
    return get_grant(conn, gid)


def revoke_grant(conn, actor, gid, reason, now) -> dict:
    with tx(conn):
        g = get_grant(conn, gid)
        if g["status"] not in ("active", "pending"):
            raise Conflict(f"grant is already {g['status']}")
        if actor != g["user_id"]:  # giving up your own access is always allowed
            _require_access_perm(conn, actor, "access.grant", now, g)
        reason = _require_reason(reason, "Revoking access")
        conn.execute("UPDATE grants SET status='revoked', revoked_at=?, revoked_by=?, revoke_reason=? WHERE id=?",
                     (now, actor, reason, gid))
        access_event(conn, now, actor, "grant_revoked", "grant", gid,
                     {"user_id": g["user_id"], "role_id": g["role_id"], "reason": reason})
        released = _reconcile_user(conn, g["user_id"], now, actor, f"access changed: grant #{gid} revoked")
    out = get_grant(conn, gid)
    out["released_jobs"] = released
    return out


def _reconcile_user(conn, user_id, now, actor, why) -> list[str]:
    """After someone loses access, jobs assigned to them that they can no longer
    move forward go back to the pool. Work they already did stays (history is history)."""
    released = []
    rp = P.role_permissions_at(conn, now)
    for row in conn.execute("SELECT * FROM jobs WHERE assignee_id=? AND stage IN ('Confirmed','Prepared','Checked','Reopened')",
                            (user_id,)).fetchall():
        job = dict(row)
        job["data"] = json.loads(job["data"])
        nxt = P.NEXT_ACTION[job["stage"]]
        d = P.decide(conn, user_id, nxt, job, now, role_perms=rp)
        if not d.allowed:
            job["assignee_id"] = None
            job["updated_at"] = now
            _save_job(conn, job)
            _event(conn, job, now, actor, "unassigned", job["stage"], why,
                   detail={"from": user_id, "because": d.summary})
            released.append(job["id"])
    return released


def reconcile_expired(conn, now) -> list[str]:
    """Grants that simply ran out (valid_to) need the same clean-up as revocations.
    Called cheaply before any list is served, so queues never show stale owners."""
    last = conn.execute("SELECT value FROM meta WHERE key='reconciled_at'").fetchone()
    last = last[0] if last else "0000"
    released = []
    if last >= now:
        return released
    with tx(conn):
        users = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM grants WHERE valid_to > ? AND valid_to <= ?", (last, now))]
        users += [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM role_versions rv JOIN grants g ON g.role_id = rv.role_id "
            "WHERE rv.status='active' AND rv.effective_from > ? AND rv.effective_from <= ?", (last, now))]
        for u in set(users):
            released += _reconcile_user(conn, u, now, "system", "access changed: grant expired or role changed")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('reconciled_at',?)", (now,))
    return released


def _validate_role_perms(perms, audience):
    unknown = set(perms) - set(P.PERMISSIONS)
    if unknown:
        raise BadRequest(f"unknown permissions {sorted(unknown)}; permissions are a fixed list "
                         f"(GET /permissions), roles are combinations of them")
    not_allowed = set(perms) - P.AUDIENCE_PERMISSIONS[audience]
    if not_allowed:
        raise BadRequest(f"{audience} roles may not contain {sorted(not_allowed)}")
    if not perms:
        raise BadRequest("a role needs at least one permission")


def create_role(conn, actor, spec, now) -> dict:
    """New roles are data: no deploy needed. Privileged roles wait for a second admin."""
    with tx(conn):
        _require_access_perm(conn, actor, "access.role_manage", now)
        rid = (spec.get("id") or "").strip().lower()
        if not rid or not rid.replace("_", "").replace("-", "").isalnum():
            raise BadRequest("role id must be a short slug like 'senior_checker'")
        if conn.execute("SELECT 1 FROM roles WHERE id=?", (rid,)).fetchone():
            raise Conflict(f"role {rid} already exists; change it with a new version instead")
        audience = spec.get("audience", "staff")
        if audience not in P.AUDIENCE_PERMISSIONS:
            raise BadRequest("audience must be staff, client or service")
        perms = sorted(set(spec.get("permissions") or []))
        _validate_role_perms(perms, audience)
        privileged = bool(set(perms) & P.PRIVILEGED_PERMISSIONS)
        conn.execute("INSERT INTO roles(id,name,description,audience,privileged,created_at,created_by) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (rid, spec.get("name") or rid, spec.get("description"), audience, int(privileged), now, actor))
        status = "pending" if privileged else "active"
        conn.execute("INSERT INTO role_versions(role_id,version,permissions,status,effective_from,changed_by,created_at,reason)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (rid, 1, json.dumps(perms), status, None if privileged else now, actor, now, spec.get("reason")))
        access_event(conn, now, actor, "role_created", "role", rid, {"permissions": perms, "status": status})
    return get_role(conn, rid, now)


def update_role(conn, actor, rid, permissions, reason, now) -> dict:
    """Change a role's permissions: a new version, effective for every holder at once.

    Needs a second admin if the role is (or becomes) privileged, or if the editor
    holds the role themselves (that would be self-escalation by the back door)."""
    with tx(conn):
        _require_access_perm(conn, actor, "access.role_manage", now)
        role = row_dict(conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone())
        if role is None:
            raise NotFound(f"role {rid} not found")
        perms = sorted(set(permissions or []))
        _validate_role_perms(perms, role["audience"])
        reason = _require_reason(reason, "Changing a role")
        becomes_priv = bool(set(perms) & P.PRIVILEGED_PERMISSIONS)
        holds = any(g["role_id"] == rid for g in P.grants_at(conn, actor, now))
        needs_second = bool(role["privileged"]) or becomes_priv or holds
        version = conn.execute("SELECT MAX(version) FROM role_versions WHERE role_id=?", (rid,)).fetchone()[0] + 1
        status = "pending" if needs_second else "active"
        conn.execute("INSERT INTO role_versions(role_id,version,permissions,status,effective_from,changed_by,created_at,reason)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (rid, version, json.dumps(perms), status, None if needs_second else now, actor, now, reason))
        if becomes_priv and not role["privileged"]:
            conn.execute("UPDATE roles SET privileged=1 WHERE id=?", (rid,))
        access_event(conn, now, actor, "role_changed", "role", rid,
                     {"version": version, "permissions": perms, "status": status, "reason": reason})
        if status == "active":
            for (uid,) in conn.execute("SELECT DISTINCT user_id FROM grants WHERE role_id=? AND status='active'", (rid,)).fetchall():
                _reconcile_user(conn, uid, now, actor, f"role {rid} changed")
    return get_role(conn, rid, now)


def approve_role_version(conn, actor, rid, version, now, approve=True) -> dict:
    with tx(conn):
        _require_access_perm(conn, actor, "access.role_manage", now)
        rv = row_dict(conn.execute("SELECT * FROM role_versions WHERE role_id=? AND version=?", (rid, version)).fetchone())
        if rv is None:
            raise NotFound("role version not found")
        if rv["status"] != "pending":
            raise Conflict(f"version is {rv['status']}")
        if rv["changed_by"] == actor:
            raise Denied("a role change must be approved by someone other than the person who made it")
        conn.execute("UPDATE role_versions SET status=?, approved_by=?, effective_from=? WHERE role_id=? AND version=?",
                     ("active" if approve else "rejected", actor, now if approve else None, rid, version))
        access_event(conn, now, actor, "role_version_approved" if approve else "role_version_rejected", "role", rid,
                     {"version": version})
        if approve:
            for (uid,) in conn.execute("SELECT DISTINCT user_id FROM grants WHERE role_id=? AND status='active'", (rid,)).fetchall():
                _reconcile_user(conn, uid, now, actor, f"role {rid} changed")
    return get_role(conn, rid, now)


def get_role(conn, rid, now) -> dict:
    role = row_dict(conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone())
    if role is None:
        raise NotFound(f"role {rid} not found")
    role["versions"] = [dict(r, permissions=json.loads(r["permissions"])) for r in
                        conn.execute("SELECT * FROM role_versions WHERE role_id=? ORDER BY version", (rid,))]
    role["permissions_now"] = sorted(P.role_permissions_at(conn, now).get(rid, set()))
    return role


# ================================================================ absences
def set_absence(conn, actor, spec, now) -> dict:
    with tx(conn):
        uid = spec.get("user_id") or actor
        user = P.get_user(conn, uid)
        if user is None or user["kind"] != "staff":
            raise BadRequest("absences are recorded for staff accounts")
        if actor != uid:
            _require_access_perm(conn, actor, "access.absence_manage", now)
        starts = norm_ts(spec.get("starts_at")) or now
        ends = norm_ts(spec.get("ends_at"))
        if not ends or ends <= starts:
            raise BadRequest("ends_at must be after starts_at")
        delegate = spec.get("delegate_id")
        if delegate is not None:
            du = P.get_user(conn, delegate)
            if du is None or du["kind"] != "staff" or not P.user_active(du, now) or delegate == uid:
                raise BadRequest("delegate must be another active staff member")
        cur = conn.execute("INSERT INTO absences(user_id,starts_at,ends_at,delegate_id,reason,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (uid, starts, ends, delegate, spec.get("reason"), actor, now))
        access_event(conn, now, actor, "absence_recorded", "user", uid,
                     {"absence_id": cur.lastrowid, "starts_at": starts, "ends_at": ends, "delegate_id": delegate})
    out = row_dict(conn.execute("SELECT * FROM absences WHERE id=?", (cur.lastrowid,)).fetchone())
    # Tell the person setting this up which of the absentee's jobs the delegate can't do:
    # those go to the general pool, which is fine, but it shouldn't be a surprise.
    out["delegate_cannot_cover"] = []
    if delegate:
        for r in conn.execute("SELECT * FROM jobs WHERE assignee_id=? AND stage IN "
                              "('Confirmed','Prepared','Checked','Reopened')", (uid,)).fetchall():
            j = dict(r)
            if not P.decide(conn, delegate, P.NEXT_ACTION[j["stage"]], j, max(now, starts)).allowed:
                out["delegate_cannot_cover"].append(j["id"])
    return out


def end_absence(conn, actor, absence_id, now) -> dict:
    with tx(conn):
        a = row_dict(conn.execute("SELECT * FROM absences WHERE id=?", (absence_id,)).fetchone())
        if a is None:
            raise NotFound("absence not found")
        if actor != a["user_id"]:
            _require_access_perm(conn, actor, "access.absence_manage", now)
        conn.execute("UPDATE absences SET ends_at=? WHERE id=? AND ends_at > ?", (now, absence_id, now))
        access_event(conn, now, actor, "absence_ended", "user", a["user_id"], {"absence_id": absence_id})
    return row_dict(conn.execute("SELECT * FROM absences WHERE id=?", (absence_id,)).fetchone())


# =================================================================== lists
def inbox_parts(conn, user_id, now):
    """The 'jobs waiting for me' predicate, one part per next step:
    I can do the job's next step AND it is routed to me. Built from the same policy as
    decide(), so the count cannot drift from what I can do. Split per stage so each part
    can use the (stage, scope...) index instead of scanning every job."""
    rp = P.role_permissions_at(conn, now)
    rsql, rparams = P.routing_sql(user_id, P.absences_at(conn, now))
    parts = []
    for stage_set, action in ((("Confirmed", "Reopened"), "prepare"), (("Prepared",), "check"), (("Checked",), "file")):
        sql, p = P.action_clause(conn, user_id, action, now, role_perms=rp, stages=stage_set)
        if sql.startswith("0=1"):
            continue
        parts.append((f"{sql} AND {rsql}", p + rparams))
    return parts


def inbox(conn, user_id, now, limit=50, offset=0) -> dict:
    reconcile_expired(conn, now)
    parts = inbox_parts(conn, user_id, now)
    total, items = 0, []
    if parts:
        total = sum(conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", p).fetchone()[0] for w, p in parts)
        cols = "j.id, j.client_id, j.branch_id, j.direction, j.mode, j.stage, j.assignee_id, j.prepared_by, " \
               "j.needs_attention, j.updated_at"
        union = " UNION ALL ".join(f"SELECT {cols} FROM jobs j WHERE {w}" for w, _ in parts)
        params = [x for _, p in parts for x in p]
        rows = conn.execute(f"SELECT * FROM ({union}) ORDER BY updated_at, id LIMIT ? OFFSET ?",
                            params + [limit, offset]).fetchall()
        absences = P.absences_at(conn, now)
        for r in rows:
            it = dict(r)
            it["next_action"] = P.NEXT_ACTION[it["stage"]]
            it["why"] = P.routed_to(user_id, it, absences)
            items.append(it)
    return {"user_id": user_id, "as_of": now, "total": total, "limit": limit, "offset": offset, "items": items}


def eligible_for_next(conn, job_id, now) -> dict:
    """Who could move this job forward right now? Empty = stuck (Q4/Q14): surface it, don't guess."""
    job = get_job(conn, job_id)
    nxt = P.NEXT_ACTION.get(job["stage"])
    if nxt is None:
        return {"job_id": job_id, "stage": job["stage"], "next_action": None, "eligible": [],
                "note": "job is closed; only the cancel/reopen request workflow applies"}
    absences = P.absences_at(conn, now)
    rp = P.role_permissions_at(conn, now)
    out = []
    for (uid,) in conn.execute("SELECT id FROM users ORDER BY id"):
        if P.decide(conn, uid, nxt, job, now, role_perms=rp).allowed:
            out.append({"user_id": uid, "absent": uid in absences,
                        "queue": P.routed_to(uid, job, absences) or f"assigned to {job['assignee_id']}"})
    present = [e for e in out if not e["absent"]]
    note = None
    if not present:
        note = ("STUCK: nobody present may " + nxt + " this job. Grant someone access (or reset it with "
                "admin_fix if the preparer is the only checker).")
    return {"job_id": job_id, "stage": job["stage"], "next_action": nxt, "eligible": out, "note": note}


def pending_approvals(conn, user_id, now) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM requests WHERE status='pending' ORDER BY created_at"):
        req = dict(r)
        job = get_job(conn, req["job_id"])
        if P.decide(conn, user_id, "approve_request", job, now, ctx={"request": req}).allowed:
            out.append(req)
    return out


def visible_jobs(conn, user_id, now, filters=None, limit=50, offset=0) -> dict:
    """Jobs a person may see. Visibility comes from the same grants as action, but any role
    with a view permission in scope is enough to *see*; acting needs the specific permission."""
    reconcile_expired(conn, now)
    filters = filters or {}
    sql, params = P.action_clause(conn, user_id, "view", now)
    where, wparams = [sql], list(params)
    for f in ("client_id", "branch_id", "direction", "mode", "stage", "assignee_id"):
        if filters.get(f):
            where.append(f"j.{f} = ?")
            wparams.append(filters[f])
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", wparams).fetchone()[0]
    rows = conn.execute(f"SELECT j.* FROM jobs j WHERE {w} ORDER BY j.updated_at DESC, j.id LIMIT ? OFFSET ?",
                        wparams + [limit, offset]).fetchall()
    full_view = [g for g in P.grants_at(conn, user_id, now) if "job.view" in g["permissions"]]
    items = []
    for r in rows:
        j = dict(r)
        j["data"] = json.loads(j["data"])
        items.append(j if any(P.scope_matches(g, j) for g in full_view) else client_view(j))
    return {"total": total, "limit": limit, "offset": offset, "items": items}


# ================================================================ history
def job_history(conn, job_id) -> dict:
    job = get_job(conn, job_id)
    events = []
    for r in conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY at, id", (job_id,)):
        e = dict(r)
        e["detail"] = json.loads(e["detail"] or "{}")
        e["authority"] = json.loads(e["authority"] or "{}")
        e.pop("snapshot")
        events.append(e)
    denied = [dict(r) for r in conn.execute(
        "SELECT at,user_id,action,summary FROM decision_log WHERE job_id=? AND allowed=0 ORDER BY at", (job_id,))]
    reqs = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE job_id=? ORDER BY id", (job_id,))]
    return {"job": job, "events": events, "requests": reqs, "denied_attempts": denied}


def job_state_at(conn, job_id, at) -> dict | None:
    row = conn.execute("SELECT snapshot FROM job_events WHERE job_id=? AND at <= ? ORDER BY at DESC, id DESC LIMIT 1",
                       (job_id, at)).fetchone()
    return json.loads(row[0]) if row else None


def who_could(conn, job_id, start, end, actions=None) -> dict:
    """Who could have changed this job between start and end?

    The answer is replayed, not guessed: every moment in the window where
    something relevant changed (the job, any grant, any role version, any user)
    is a breakpoint; at each one we run the normal decide() as of that moment.
    """
    get_job(conn, job_id)  # 404 if unknown
    actions = actions or list(P.CHANGE_ACTIONS)
    points = {start}
    q = [
        ("SELECT at FROM job_events WHERE job_id=? AND at > ? AND at < ?", (job_id, start, end)),
        ("SELECT effective_from FROM grants WHERE effective_from > ? AND effective_from < ?", (start, end)),
        ("SELECT valid_to FROM grants WHERE valid_to > ? AND valid_to < ?", (start, end)),
        ("SELECT revoked_at FROM grants WHERE revoked_at > ? AND revoked_at < ?", (start, end)),
        ("SELECT effective_from FROM role_versions WHERE effective_from > ? AND effective_from < ?", (start, end)),
        ("SELECT deactivated_at FROM users WHERE deactivated_at > ? AND deactivated_at < ?", (start, end)),
    ]
    for sql, params in q:
        points.update(r[0] for r in conn.execute(sql, params))
    points = sorted(points)
    users = [dict(r) for r in conn.execute("SELECT id, name FROM users ORDER BY id")]
    nm = names(conn)
    windows: dict = {}
    via: dict = {}
    for i, t in enumerate(points):
        t_end = points[i + 1] if i + 1 < len(points) else end
        job = job_state_at(conn, job_id, t)
        if job is None:
            continue
        rp = P.role_permissions_at(conn, t)
        for u in users:
            for a in actions:
                d = P.decide(conn, u["id"], a, job, t, role_perms=rp, names=nm)
                if d.allowed:
                    w = windows.setdefault(u["id"], {}).setdefault(a, [])
                    if w and w[-1][1] == t:
                        w[-1][1] = t_end
                    else:
                        w.append([t, t_end])
                    via.setdefault(u["id"], set()).update(d.matched_grants)
    could = [{"user_id": uid, "name": nm.get(uid), "actions": acts, "via": sorted(via[uid])}
             for uid, acts in sorted(windows.items())]
    did = [dict(r) for r in conn.execute(
        "SELECT at, actor_id, action, from_stage, to_stage, reason, authority FROM job_events "
        "WHERE job_id=? AND at >= ? AND at < ? ORDER BY at, id", (job_id, start, end))]
    for e in did:
        e["authority"] = json.loads(e["authority"] or "{}")
    tried = [dict(r) for r in conn.execute(
        "SELECT at,user_id,action,summary FROM decision_log WHERE job_id=? AND allowed=0 AND at >= ? AND at < ? "
        "ORDER BY at", (job_id, start, end))]
    notes = []
    if job_state_at(conn, job_id, start) is None:
        notes.append("the job did not exist at the start of the window")
    return {"job_id": job_id, "from": start, "to": end, "breakpoints": points, "could_change": could,
            "actually_changed": did, "denied_attempts": tried, "notes": notes}


# ================================================================ explain
def explain(conn, user_id, action, job_id, now) -> dict:
    job = get_job(conn, job_id)
    req = None
    if action == "approve_request":
        req = row_dict(conn.execute("SELECT * FROM requests WHERE job_id=? AND status='pending'", (job_id,)).fetchone())
    return P.decide(conn, user_id, action, job, now, ctx={"request": req} if req else None,
                    names=names(conn)).to_dict()


def compare(conn, user_a, user_b, action, job_id, now) -> dict:
    a = explain(conn, user_a, action, job_id, now)
    b = explain(conn, user_b, action, job_id, now)
    by_a = {c["check"]: c for c in a["checks"]}
    by_b = {c["check"]: c for c in b["checks"]}
    diffs = []
    for name in list(dict.fromkeys(list(by_a) + list(by_b))):
        ca, cb = by_a.get(name), by_b.get(name)
        if (ca or {}).get("passed") != (cb or {}).get("passed"):
            diffs.append({"check": name, user_a: ca and ca["detail"], user_b: cb and cb["detail"]})
    nm = names(conn)
    headline = (f"{nm.get(user_a, user_a)} {'can' if a['allowed'] else 'cannot'} and "
                f"{nm.get(user_b, user_b)} {'can' if b['allowed'] else 'cannot'} {action} {job_id}.")
    if diffs:
        headline += " The difference: " + "; ".join(
            f"{d['check']}: {nm.get(user_a, user_a)} -> {d[user_a]}; {nm.get(user_b, user_b)} -> {d[user_b]}"
            for d in diffs)
    return {"headline": headline, "differences": diffs, user_a: a, user_b: b}


def describe_access(conn, user_id, now) -> dict:
    """A person's access in plain words, including what's coming and what's gone."""
    user = P.get_user(conn, user_id)
    if user is None:
        raise NotFound(f"user {user_id} not found")
    nm = names(conn)
    rp = P.role_permissions_at(conn, now)
    lines, current, upcoming, past, pending = [], [], [], [], []
    for r in conn.execute("SELECT g.*, r.name AS role_name FROM grants g JOIN roles r ON r.id=g.role_id "
                          "WHERE g.user_id=? ORDER BY g.id", (user_id,)):
        g = dict(r)
        text = f"{g['role_name']} for {P.describe_scope(g, nm)}"
        if g["status"] == "pending":
            pending.append(text + f" (awaiting second approval; grant #{g['id']})")
        elif g["status"] == "rejected":
            continue
        elif g["revoked_at"] and g["revoked_at"] <= now:
            past.append(text + f" (revoked {g['revoked_at']}: {g['revoke_reason']})")
        elif g["valid_to"] and g["valid_to"] <= now:
            past.append(text + f" (expired {g['valid_to']})")
        elif g["effective_from"] and g["effective_from"] > now:
            upcoming.append(text + f" (starts {g['effective_from']})")
        else:
            extra = f" until {g['valid_to']}" if g["valid_to"] else ""
            current.append(text + f" (since {g['effective_from']}{extra}) "
                           f"-> may: {', '.join(sorted(rp.get(g['role_id'], [])))}")
    absent = conn.execute("SELECT * FROM absences WHERE user_id=? AND ends_at > ? ORDER BY starts_at",
                          (user_id, now)).fetchall()
    who = f"{user['name']} ({user_id}, {user['kind']}{', ' + user['title'] if user['title'] else ''})"
    for c in current:
        lines.append(f"{who} has role {c}.")
    if not current:
        lines.append(f"{who} has no access right now.")
    return {"user": user, "now": current, "upcoming": upcoming, "pending_approval": pending, "past": past,
            "absences": [dict(a) for a in absent], "plain_english": lines}
