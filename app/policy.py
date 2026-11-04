"""The policy: one place that says who may do what on which job.

`decide()` answers a single question and explains itself, check by check.
`action_clause()` compiles the *same* rules into a SQL predicate so lists
("jobs waiting for me") and counts stay fast at 200k jobs.  The two are kept
honest by tests/test_inbox_parity.py, which checks that for every user and
every job they agree.

Everything is evaluated "as of" a timestamp. Pass now=<the past> and you get
the answer the system would have given then: that is how history questions
are answered without a separate code path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# ------------------------------------------------------------------ vocabulary
STAGES = ("Confirmed", "Prepared", "Checked", "Filed", "Cancelled", "Reopened")
OPEN_STAGES = frozenset({"Confirmed", "Prepared", "Checked", "Reopened"})
CLOSED_STAGES = frozenset({"Filed", "Cancelled"})
DIRECTIONS = ("import", "export")
MODES = ("air", "fcl", "lcl")
SCOPE_FIELDS = ("client_id", "branch_id", "direction", "mode")

# Permissions are the fixed vocabulary engineering owns. Roles (bundles of
# permissions) are data that operations owns.
PERMISSIONS: dict[str, str] = {
    "job.view": "See full job details",
    "job.view_client": "See the client-safe view of a job (no internal notes, no staff names)",
    "job.create": "Open a new job by hand",
    "job.edit": "Change job data while it is being worked on (Confirmed / Reopened)",
    "job.prepare": "Prepare the checklist (Maker)",
    "job.check": "Check / approve a prepared checklist, or send it back (Checker)",
    "job.file": "File a checked job with customs",
    "job.cancel": "Cancel a job that has not been filed",
    "job.request_cancel_filed": "Ask for a filed job to be cancelled",
    "job.request_reopen": "Ask for a filed or cancelled job to be reopened",
    "job.approve_sensitive": "Approve or reject cancel-filed / reopen requests",
    "job.assign": "Assign or reassign the person working a job",
    "job.sync": "Create and update jobs from the overnight feed (parser only)",
    "job.admin_fix": "Unstick a job: release it or reset it to Confirmed (never forward)",
    "access.read": "Read grants, roles and audit history",
    "access.grant": "Grant, approve and revoke access (within own scope, never to self)",
    "access.role_manage": "Create roles and change their permissions",
    "access.absence_manage": "Record absences and delegates for other people",
}
# Holding any of these makes a role privileged: granting it needs a second person.
PRIVILEGED_PERMISSIONS = frozenset({"job.approve_sensitive", "job.admin_fix", "access.grant", "access.role_manage"})
# What each kind of account may ever hold, whatever a role says.
AUDIENCE_PERMISSIONS = {
    "staff": frozenset(PERMISSIONS) - {"job.sync", "job.view_client"},
    "client": frozenset({"job.view_client"}),
    "service": frozenset({"job.sync"}),
}


@dataclass(frozen=True)
class ActionSpec:
    perms: tuple[str, ...]            # any one of these, in scope, is enough
    stages: frozenset | None          # stages in which the action makes sense
    kinds: frozenset = frozenset({"staff"})
    to_stage: str | None = None
    sod: tuple[str, ...] = ()         # separation-of-duties rules
    reason_required: bool = False
    mutating: bool = True
    label: str = ""


ACTIONS: dict[str, ActionSpec] = {
    "view": ActionSpec(("job.view", "job.view_client"), None, frozenset({"staff", "client"}),
                       mutating=False, label="see this job"),
    "create": ActionSpec(("job.create",), None, label="open a job"),
    "edit": ActionSpec(("job.edit",), frozenset({"Confirmed", "Reopened"}), label="edit job data"),
    "prepare": ActionSpec(("job.prepare",), frozenset({"Confirmed", "Reopened"}), to_stage="Prepared",
                          label="prepare the checklist"),
    "send_back": ActionSpec(("job.check",), frozenset({"Prepared"}), to_stage="Confirmed",
                            reason_required=True, label="send the checklist back to the maker"),
    "check": ActionSpec(("job.check",), frozenset({"Prepared"}), to_stage="Checked",
                        sod=("not_preparer",), label="check / approve the checklist"),
    "file": ActionSpec(("job.file",), frozenset({"Checked"}), to_stage="Filed",
                       sod=("not_preparer",), label="file with customs"),
    "cancel": ActionSpec(("job.cancel",), OPEN_STAGES, to_stage="Cancelled", reason_required=True,
                         label="cancel an unfiled job"),
    "request_cancel_filed": ActionSpec(("job.request_cancel_filed",), frozenset({"Filed"}),
                                       reason_required=True, label="request cancellation of a filed job"),
    "request_reopen": ActionSpec(("job.request_reopen",), CLOSED_STAGES, reason_required=True,
                                 label="request reopening of a closed job"),
    "approve_request": ActionSpec(("job.approve_sensitive",), CLOSED_STAGES,
                                  sod=("not_requester", "not_filer"), label="approve a cancel/reopen request"),
    "assign": ActionSpec(("job.assign",), OPEN_STAGES, label="assign the job"),
    "sync": ActionSpec(("job.sync",), frozenset({"Confirmed"}), frozenset({"service"}),
                       label="create/update the job from the overnight feed"),
    "admin_fix": ActionSpec(("job.admin_fix",), OPEN_STAGES, reason_required=True,
                            label="unstick the job (release / reset)"),
}
# The step a job is waiting for, by stage. Drives "jobs waiting for me".
NEXT_ACTION = {"Confirmed": "prepare", "Reopened": "prepare", "Prepared": "check", "Checked": "file"}
# Actions that count as "changing a job" for history questions.
CHANGE_ACTIONS = ("edit", "prepare", "send_back", "check", "file", "cancel", "assign", "sync",
                  "admin_fix", "approve_request")

SOD_TEXT = {
    "not_preparer": "the person who prepared the checklist may not also {verb} it",
    "not_requester": "the person who asked for this may not approve it",
    "not_filer": "the person who filed the job may not approve undoing that filing",
}


# --------------------------------------------------------------- data access
def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def user_active(user, at) -> bool:
    return user is not None and (user["deactivated_at"] is None or user["deactivated_at"] > at) \
        and user["created_at"] <= at


def role_permissions_at(conn, at) -> dict[str, set[str]]:
    """{role_id: permissions} using the latest active version effective at `at`."""
    out: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT rv.role_id, rv.permissions FROM role_versions rv
           WHERE rv.status='active' AND rv.effective_from <= ?
             AND rv.version = (SELECT MAX(v2.version) FROM role_versions v2
                               WHERE v2.role_id = rv.role_id AND v2.status='active' AND v2.effective_from <= ?)""",
        (at, at),
    ):
        out[r["role_id"]] = set(json.loads(r["permissions"]))
    return out


GRANT_ACTIVE_SQL = """g.effective_from IS NOT NULL AND g.effective_from <= :at
    AND (g.valid_to IS NULL OR g.valid_to > :at)
    AND (g.revoked_at IS NULL OR g.revoked_at > :at)"""


def grants_at(conn, user_id, at, role_perms=None) -> list[dict]:
    """Grants in force for a user at `at`, each annotated with its permissions then."""
    role_perms = role_perms if role_perms is not None else role_permissions_at(conn, at)
    rows = conn.execute(
        f"""SELECT g.*, r.name AS role_name FROM grants g JOIN roles r ON r.id = g.role_id
            WHERE g.user_id = :uid AND {GRANT_ACTIVE_SQL} ORDER BY g.id""",
        {"uid": user_id, "at": at},
    ).fetchall()
    out = []
    for r in rows:
        g = dict(r)
        g["permissions"] = role_perms.get(g["role_id"], set())
        out.append(g)
    return out


def absences_at(conn, at) -> dict[str, str | None]:
    """{absent_user_id: delegate_id or None} for absences covering `at`."""
    out = {}
    for r in conn.execute(
        "SELECT user_id, delegate_id FROM absences WHERE starts_at <= ? AND ends_at > ? ORDER BY id", (at, at)
    ):
        out[r["user_id"]] = r["delegate_id"]
    return out


# ------------------------------------------------------------------- scopes
def scope_matches(grant, job) -> bool:
    return all(grant[f] is None or grant[f] == job.get(f) for f in SCOPE_FIELDS)


def scope_mismatches(grant, job) -> list[str]:
    return [f"{_FIELD_WORD[f]} is {grant[f]} but the job's is {job.get(f)}"
            for f in SCOPE_FIELDS if grant[f] is not None and grant[f] != job.get(f)]


def scope_covers(outer, inner) -> bool:
    """True if every job matched by `inner` is also matched by `outer`."""
    return all(outer[f] is None or outer[f] == inner.get(f) for f in SCOPE_FIELDS)


_FIELD_WORD = {"client_id": "client", "branch_id": "branch", "direction": "direction", "mode": "mode"}
_ALL_WORD = {"client_id": "all clients", "branch_id": "all branches", "direction": "all directions",
             "mode": "all modes"}


def describe_scope(g, names=None) -> str:
    names = names or {}
    parts = []
    for f in SCOPE_FIELDS:
        v = g.get(f)
        if v is None:
            parts.append(_ALL_WORD[f])
        elif f == "direction":
            parts.append(f"{v}s only")
        elif f == "mode":
            parts.append(f"{v.upper()} only")
        else:
            parts.append(f"{_FIELD_WORD[f]} {names.get(v, v)}")
    return ", ".join(parts)


def describe_grant(g, names=None) -> str:
    when = f"from {g['effective_from'] or g['valid_from']}"
    if g.get("valid_to"):
        when += f" until {g['valid_to']}"
    if g.get("revoked_at"):
        when += f", revoked {g['revoked_at']}"
    return f"{g.get('role_name') or g['role_id']} for {describe_scope(g, names)} ({when}; grant #{g['id']})"


# ------------------------------------------------------------------- decide
@dataclass
class Decision:
    allowed: bool
    user_id: str
    action: str
    job_id: str | None
    at: str
    checks: list = field(default_factory=list)
    matched_grants: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.allowed:
            via = "; ".join(self.matched_grants) or "n/a"
            return f"ALLOWED: {self.user_id} may {self.action} {self.job_id or ''} via {via}".strip()
        failed = [c["detail"] for c in self.checks if not c["passed"]]
        return f"DENIED: {self.user_id} may not {self.action} {self.job_id or ''}: " + " | ".join(failed)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "summary": self.summary, "user_id": self.user_id,
                "action": self.action, "job_id": self.job_id, "evaluated_at": self.at,
                "checks": self.checks, "matched_grants": self.matched_grants, "notes": self.notes}


def decide(conn, user_id, action, job, at, ctx=None, *, role_perms=None, names=None) -> Decision:
    """May `user_id` do `action` on `job` at time `at`? Always explains why.

    Fails closed: unknown user, unknown action, incomplete job data, or any
    rule that can't be evaluated -> deny, with the reason written down.
    All checks are evaluated (not short-circuited) so the explanation is complete.
    """
    ctx = ctx or {}
    d = Decision(False, user_id, action, (job or {}).get("id"), at)

    def check(name, passed, detail):
        d.checks.append({"check": name, "passed": bool(passed), "detail": detail})

    user = get_user(conn, user_id)
    if user is None:
        check("user_known", False, f"no such user '{user_id}'")
        return d
    check("user_active", user_active(user, at),
          "account is active" if user_active(user, at) else f"account {user_id} is not active at {at}")

    spec = ACTIONS.get(action)
    if spec is None:
        check("action_known", False, f"'{action}' is not an action this system knows; unknown means no")
        return d

    check("account_type", user["kind"] in spec.kinds,
          f"{user['kind']} accounts may {spec.label}" if user["kind"] in spec.kinds
          else f"{user['kind']} accounts can never {spec.label} (only {', '.join(sorted(spec.kinds))})")

    job = job or {}
    missing = [f for f in SCOPE_FIELDS if not job.get(f)]
    check("job_data_complete", not missing,
          "job has client, branch, direction and mode" if not missing
          else f"job is missing {', '.join(missing)}; cannot tell who it belongs to, so nobody may act")

    if spec.stages is not None:
        ok = job.get("stage") in spec.stages
        check("stage", ok, f"job is {job.get('stage')}, and '{action}' is allowed in {job.get('stage')}" if ok
              else f"job is {job.get('stage')}; '{action}' is only possible when the job is "
                   f"{' / '.join(s for s in STAGES if s in spec.stages)}")

    grants = grants_at(conn, user_id, at, role_perms)
    relevant = [g for g in grants if g["permissions"] & set(spec.perms)]
    matched = [g for g in relevant if scope_matches(g, job)]
    if matched:
        d.matched_grants = [describe_grant(g, names) for g in matched]
        check("permission_in_scope", True,
              f"holds {'/'.join(sorted(matched[0]['permissions'] & set(spec.perms)))} for this job via "
              + "; ".join(d.matched_grants))
    elif relevant:
        why = "; ".join(f"{g['role_name']} grant #{g['id']}: " + ", ".join(scope_mismatches(g, job))
                        for g in relevant)
        check("permission_in_scope", False,
              f"has {'/'.join(spec.perms)} but not for this job ({why})")
    else:
        held = sorted({g["role_name"] for g in grants}) or ["no roles at all"]
        check("permission_in_scope", False,
              f"none of their roles ({', '.join(held)}) includes {' or '.join(spec.perms)}")

    for rule in spec.sod:
        if rule == "not_preparer":
            ok = job.get("prepared_by") != user_id
            verb = "file" if action == "file" else "check"
            check("separation_of_duties", ok, "did not prepare this checklist" if ok
                  else SOD_TEXT[rule].format(verb=verb) + f" ({user_id} prepared it)")
        elif rule == "not_requester":
            req = ctx.get("request")
            if req is None:
                d.notes.append("no specific request given; requester rule not evaluated")
                continue
            ok = req["requested_by"] != user_id
            check("separation_of_duties", ok, "is not the requester" if ok else SOD_TEXT[rule])
        elif rule == "not_filer":
            ok = job.get("filed_by") != user_id
            check("separation_of_duties_filer", ok, "did not file this job" if ok else SOD_TEXT[rule])

    absent = absences_at(conn, at)
    if user_id in absent:
        d.notes.append(f"{user_id} is marked absent at {at}; absence routes work away but does not remove authority")

    d.allowed = all(c["passed"] for c in d.checks)
    return d


# -------------------------------------------------------- SQL compilation
def scope_sql(grants, alias="j"):
    parts, params = [], []
    for g in grants:
        conds = []
        for f in SCOPE_FIELDS:
            if g[f] is not None:
                conds.append(f"{alias}.{f} = ?")
                params.append(g[f])
        parts.append("(" + " AND ".join(conds) + ")" if conds else "1=1")
    if not parts:
        return "0=1", []
    if "1=1" in parts:
        return "1=1", []
    return "(" + " OR ".join(parts) + ")", params


def action_clause(conn, user_id, action, at, *, role_perms=None, alias="j", stages=None):
    """SQL predicate equivalent to decide(user, action, job, at).allowed for jobs in the table.

    (job_data_complete always holds for stored jobs: the columns are NOT NULL.)
    """
    spec = ACTIONS[action]
    user = get_user(conn, user_id)
    if not user_active(user, at) or user["kind"] not in spec.kinds:
        return "0=1", []
    grants = [g for g in grants_at(conn, user_id, at, role_perms) if g["permissions"] & set(spec.perms)]
    sql, params = scope_sql(grants, alias)
    clauses, all_params = [sql], list(params)
    use_stages = stages if stages is not None else spec.stages
    if use_stages is not None:
        st = sorted(use_stages)
        clauses.append(f"{alias}.stage IN ({','.join('?' * len(st))})")
        all_params += st
    if "not_preparer" in spec.sod:
        clauses.append(f"({alias}.prepared_by IS NULL OR {alias}.prepared_by != ?)")
        all_params.append(user_id)
    if "not_filer" in spec.sod:
        clauses.append(f"({alias}.filed_by IS NULL OR {alias}.filed_by != ?)")
        all_params.append(user_id)
    return " AND ".join(clauses), all_params


# --------------------------------------------------------- inbox routing
def routing(user_id, absences):
    """Whose work flows to this user right now, besides their own and the unassigned pool.

    Work assigned to an absent person goes back to everyone eligible (so nothing waits
    on someone who isn't there). Their named delegate sees it flagged 'covering for X'
    so they know it's theirs first, but the delegate gets the queue, not the authority:
    they still need their own permission for each job.
    """
    absent = [u for u in absences if u != user_id]
    cover = [u for u, dlg in absences.items() if dlg == user_id]
    return absent, cover


def routed_to(user_id, job, absences) -> str | None:
    """Python twin of the routing SQL. Returns why the job is in this user's queue."""
    a = job.get("assignee_id")
    if a == user_id:
        return "assigned to you"
    if a is None:
        return "unassigned (team pool)"
    absent, cover = routing(user_id, absences)
    if a in cover:
        return f"covering for {a} (absent; you are the named delegate)"
    if a in absent:
        return f"{a} is absent (back in the team pool)"
    return None


def routing_sql(user_id, absences, alias="j"):
    absent, _cover = routing(user_id, absences)
    sql = f"({alias}.assignee_id IS NULL OR {alias}.assignee_id = ?"
    params = [user_id]
    if absent:
        sql += f" OR {alias}.assignee_id IN ({','.join('?' * len(absent))})"
        params += absent
    return sql + ")", params
