"""Edge cases from the brief, one test (or a few) per question."""
import itertools
import sqlite3

import pytest

from app import policy as P
from app import service as S
from app.db import shift

_refs = itertools.count(1)


def new_job(conn, at, client="ACME", branch="BOM", direction="export", mode="air"):
    ref = f"TEST-{next(_refs)}"
    r = S.parser_sync(conn, "svc-parser", [dict(external_ref=ref, client_id=client, branch_id=branch,
                                                direction=direction, mode=mode,
                                                data={"invoice_value_usd": 1000, "internal_notes": "secret"})], at)
    assert r["results"][0]["status"] == "created", r
    return r["results"][0]["job_id"]


def t(now, minutes):
    return shift(now, minutes=minutes)


def failed_checks(d):
    return {c["check"] for c in d.checks if not c["passed"]}


# ------------------------------------------------------------ Q3 maker / checker
def test_preparer_cannot_check_even_when_qualified(conn, now):
    j = new_job(conn, now)                       # ACME / BOM / export
    S.perform(conn, "priya", j, "prepare", t(now, 1))
    job = S.get_job(conn, j)
    d = P.decide(conn, "priya", "check", job, t(now, 2))
    assert not d.allowed
    assert failed_checks(d) == {"separation_of_duties"}          # she *is* a qualified ACME checker
    assert P.decide(conn, "sneha", "check", job, t(now, 2)).allowed
    with pytest.raises(S.Denied):
        S.perform(conn, "priya", j, "check", t(now, 3))
    # the refused attempt is kept even though the transaction rolled back
    row = conn.execute("SELECT * FROM decision_log WHERE job_id=? AND allowed=0", (j,)).fetchone()
    assert row and row["user_id"] == "priya"


def test_preparer_cannot_file_own_job(conn, now):
    j = new_job(conn, now, branch="CCU")         # ACME / CCU
    S.perform(conn, "rohan", j, "prepare", t(now, 1))
    S.perform(conn, "priya", j, "check", t(now, 2))   # priya checks ACME anywhere
    assert "separation_of_duties" in failed_checks(
        P.decide(conn, "rohan", "file", S.get_job(conn, j), t(now, 3)))
    S.perform(conn, "suresh", j, "file", t(now, 4))
    assert S.get_job(conn, j)["stage"] == "Filed"


def test_one_person_branch_is_reported_as_stuck(conn, now):
    # J000007: Rohan prepared it and is the only CCU checker
    e = S.eligible_for_next(conn, "J000007", now)
    assert e["eligible"] == [] and "STUCK" in e["note"]


def test_send_back_returns_job_to_maker(conn, now):
    j = new_job(conn, now)
    S.perform(conn, "priya", j, "prepare", t(now, 1))
    with pytest.raises(S.BadRequest):
        S.perform(conn, "sneha", j, "send_back", t(now, 2))           # reason required
    S.perform(conn, "sneha", j, "send_back", t(now, 2), reason="HS code does not match the invoice")
    job = S.get_job(conn, j)
    assert job["stage"] == "Confirmed" and job["assignee_id"] == "priya" and job["prepared_by"] is None


# --------------------------------------------------------- Q2 describing scope
def test_maker_everywhere_checker_for_few(conn, now):
    # Maker everywhere: she can prepare a Falcon job at Delhi...
    j1 = new_job(conn, now, client="FALCON", branch="DEL")
    assert P.decide(conn, "priya", "prepare", S.get_job(conn, j1), t(now, 1)).allowed
    # ...but checks only for ACME and BHARAT (jobs someone else prepared, so SoD isn't the reason)
    j2 = new_job(conn, now, client="BHARAT", branch="DEL")
    j3 = new_job(conn, now, client="FALCON", branch="DEL")
    S.perform(conn, "kavya", j2, "prepare", t(now, 3))
    S.perform(conn, "kavya", j3, "prepare", t(now, 3))
    assert P.decide(conn, "priya", "check", S.get_job(conn, j2), t(now, 4)).allowed
    d = P.decide(conn, "priya", "check", S.get_job(conn, j3), t(now, 4))
    assert not d.allowed and failed_checks(d) == {"permission_in_scope"}
    assert "client is ACME but the job's is FALCON" in d.summary


@pytest.mark.parametrize("client,branch,direction,mode,ok,why", [
    ("ACME", "BOM", "import", "air", True, None),
    ("ACME", "BOM", "import", "lcl", True, None),
    ("ACME", "BOM", "export", "air", False, "direction is import but the job's is export"),
    ("ACME", "DEL", "import", "air", False, "branch is BOM but the job's is DEL"),
    ("BHARAT", "BOM", "import", "air", False, "client is ACME but the job's is BHARAT"),
])
def test_narrow_checker(conn, now, client, branch, direction, mode, ok, why):
    j = new_job(conn, now, client, branch, direction, mode)
    S.perform(conn, "priya", j, "prepare", t(now, 1))
    d = P.decide(conn, "rahul", "check", S.get_job(conn, j), t(now, 2))
    assert d.allowed is ok
    if why:
        assert why in d.summary


# --------------------------------------------------------- Q1 / Q7 visibility
def test_seeing_is_not_acting(conn, now):
    jobs = S.visible_jobs(conn, "tarun", now, limit=1)
    assert jobs["total"] == conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    j = new_job(conn, now)
    assert "permission_in_scope" in failed_checks(P.decide(conn, "tarun", "prepare", S.get_job(conn, j), now))


def test_narrow_user_sees_only_their_slice(conn, now):
    seen = S.visible_jobs(conn, "farah", now, limit=500)["items"]
    assert seen and all((x["client_id"], x["branch_id"], x["direction"]) == ("EVEREST", "BLR", "export") for x in seen)
    with pytest.raises(S.NotFound):                     # can't even confirm other jobs exist
        S.visible_job(conn, "farah", "J000001", now)


def test_client_user(conn, now):
    seen = S.visible_jobs(conn, "ramesh", now, limit=500)["items"]
    assert seen and all(x["client_id"] == "ACME" for x in seen)
    assert all("internal_notes" not in x["data"] and "assignee_id" not in x and "prepared_by" not in x for x in seen)
    j = S.visible_job(conn, "ramesh", "J000001", now)
    assert "internal_notes" not in j["data"]
    d = P.decide(conn, "ramesh", "edit", S.get_job(conn, "J000001"), now)
    assert "account_type" in failed_checks(d)
    with pytest.raises(S.BadRequest):   # a client account can never hold a staff role
        S.create_grant(conn, "anita", dict(user_id="ramesh", role_id="maker", reason="should never work"), now)
    with pytest.raises(S.BadRequest):   # ...nor see another client
        S.create_grant(conn, "anita", dict(user_id="ramesh", role_id="client_viewer", client_id="BHARAT",
                                           reason="should never work"), now)


# ------------------------------------------------------------------ Q4 absent
def test_absent_owner_with_delegate(conn, now):
    j = new_job(conn, shift(now, days=-5), branch="DEL")
    S.claim(conn, "arjun", j, shift(now, days=-4))        # before his leave
    ib = {i["id"]: i["why"] for i in S.inbox(conn, "kavya", now, limit=500)["items"]}
    assert "covering for arjun" in ib[j]
    ib_priya = {i["id"]: i["why"] for i in S.inbox(conn, "priya", now, limit=500)["items"]}
    assert "absent" in ib_priya[j]                         # everyone eligible can still pick it up
    assert j not in {i["id"] for i in S.inbox(conn, "pooja", now, limit=500)["items"]}  # checker: can't prepare
    S.claim(conn, "kavya", j, t(now, 1))
    assert S.get_job(conn, j)["assignee_id"] == "kavya"


def test_absent_owner_without_delegate_goes_to_pool_and_comes_back(conn, now):
    j = new_job(conn, shift(now, days=-5), branch="MAA")
    S.claim(conn, "manoj", j, shift(now, days=-4))
    assert j in {i["id"] for i in S.inbox(conn, "gopal", now, limit=500)["items"]}
    absence = conn.execute("SELECT id FROM absences WHERE user_id='manoj'").fetchone()[0]
    S.end_absence(conn, "manoj", absence, t(now, 1))
    assert j not in {i["id"] for i in S.inbox(conn, "gopal", t(now, 2), limit=500)["items"]}
    assert j in {i["id"] for i in S.inbox(conn, "manoj", t(now, 2), limit=500)["items"]}


def test_delegation_passes_the_queue_not_the_authority(conn, now):
    j = new_job(conn, now, branch="DEL")
    S.claim(conn, "kavya", j, t(now, 1))
    out = S.set_absence(conn, "kavya", dict(starts_at=t(now, 2), ends_at=shift(now, days=3), delegate_id="rahul"),
                        t(now, 2))
    assert j in out["delegate_cannot_cover"]
    assert not P.decide(conn, "rahul", "prepare", S.get_job(conn, j), t(now, 3)).allowed
    assert j not in {i["id"] for i in S.inbox(conn, "rahul", t(now, 3), limit=500)["items"]}
    assert j in {i["id"] for i in S.inbox(conn, "arjun", t(now, 3), limit=500)["items"]}   # pool, not stuck


def test_cannot_assign_to_someone_absent_or_unqualified(conn, now):
    j = new_job(conn, now, branch="DEL")
    with pytest.raises(S.BadRequest, match="absent"):
        S.perform(conn, "imran", j, "assign", t(now, 1), assignee_id="arjun")
    with pytest.raises(S.BadRequest, match="could not prepare"):
        S.perform(conn, "imran", j, "assign", t(now, 1), assignee_id="pooja")
    S.perform(conn, "imran", j, "assign", t(now, 1), assignee_id="kavya")


# ------------------------------------------------- Q5 changed responsibility
def test_revocation_takes_effect_now_and_releases_work_in_progress(conn, now):
    j = new_job(conn, now, branch="MAA")
    S.perform(conn, "gopal", j, "prepare", t(now, 1))
    S.claim(conn, "pooja", j, t(now, 2))
    gid = conn.execute("SELECT id FROM grants WHERE user_id='pooja' AND branch_id='MAA'").fetchone()[0]
    out = S.revoke_grant(conn, "anita", gid, "Pooja moves to Delhi only", t(now, 3))
    assert j in out["released_jobs"]
    job = S.get_job(conn, j)
    assert job["assignee_id"] is None and job["prepared_by"] == "gopal"      # finished work stands
    assert not P.decide(conn, "pooja", "check", job, t(now, 4)).allowed       # no waiting for a session to expire
    last = S.job_history(conn, j)["events"][-1]
    assert last["action"] == "unassigned" and "revoked" in last["reason"]


def test_future_dated_grant(conn, now):
    j = new_job(conn, now, branch="BLR")
    S.perform(conn, "lakshmi", j, "prepare", t(now, 1))
    job = S.get_job(conn, j)
    assert not P.decide(conn, "gopal", "check", job, t(now, 2)).allowed
    assert P.decide(conn, "gopal", "check", job, shift(now, days=31)).allowed
    assert any("starts" in u for u in S.describe_access(conn, "gopal", now)["upcoming"])


def test_temporary_grant_expires_and_queue_is_cleaned(conn, now):
    j = new_job(conn, now, client="BHARAT", branch="DEL")
    S.perform(conn, "arjun", j, "prepare", t(now, 1))
    S.claim(conn, "kavya", j, t(now, 2))         # temporary Bharat checker
    later = shift(now, days=13)
    assert not P.decide(conn, "kavya", "check", S.get_job(conn, j), later).allowed
    released = S.reconcile_expired(conn, later)
    assert j in released and S.get_job(conn, j)["assignee_id"] is None


def test_role_change_applies_to_every_holder_at_once(conn, now):
    j = "J000002"
    S.update_role(conn, "anita", "checker", ["job.view"], "Checks suspended pending retraining", t(now, 1))
    assert not P.decide(conn, "rahul", "check", S.get_job(conn, j), t(now, 2)).allowed
    assert P.decide(conn, "rahul", "check", S.get_job(conn, j), now).allowed      # history unchanged


def test_deactivated_user(conn, now):
    job = S.job_state_at(conn, "J000001", "2026-10-01T12:00:00Z")    # still Confirmed then
    assert P.decide(conn, "karan", "edit", job, "2026-10-01T12:00:00Z").allowed
    assert "user_active" in failed_checks(P.decide(conn, "karan", "edit", job, now))


# --------------------------------------------------- Q6 filed cancel / reopen
def test_filed_job_is_locked(conn, now):
    with pytest.raises(S.Denied, match="Filed"):
        S.edit_job(conn, "kavya", "J000003", {"data": {"invoice_value_usd": 1}}, now)
    with pytest.raises(S.Denied):
        S.perform(conn, "imran", "J000003", "cancel", now, reason="trying the direct route")


def test_cancel_filed_needs_two_people(conn, now):
    req = S.pending_approvals(conn, "neha", now)[0]
    assert req["job_id"] == "J000004" and req["requested_by"] == "meera"
    assert S.pending_approvals(conn, "meera", now) == []
    with pytest.raises(S.Denied):
        S.decide_request(conn, "meera", req["id"], True, t(now, 1), "approving my own request")
    with pytest.raises(S.BadRequest):
        S.decide_request(conn, "neha", req["id"], True, t(now, 1), "ok")     # reason too short
    S.decide_request(conn, "neha", req["id"], True, t(now, 2), "Buyer cancellation confirmed in writing")
    job = S.get_job(conn, "J000004")
    assert job["stage"] == "Cancelled"
    ev = S.job_history(conn, "J000004")["events"][-1]
    assert ev["authority"]["requested_by"] == "meera" and ev["authority"]["approved_by"] == "neha"


def test_filer_cannot_approve_undoing_own_filing(conn, now):
    g = S.create_grant(conn, "anita", dict(user_id="deepa", role_id="compliance_approver",
                                           reason="Second approver for the Mumbai branch"), now)
    S.approve_grant(conn, "vikram", g["id"], t(now, 1))
    req = conn.execute("SELECT * FROM requests WHERE job_id='J000004' AND status='pending'").fetchone()
    with pytest.raises(S.Denied, match="filed the job"):
        S.decide_request(conn, "deepa", req["id"], True, t(now, 2), "approve my own filing undo")


def test_reopen_starts_a_new_cycle_with_the_same_rules(conn, now):
    with pytest.raises(S.BadRequest):
        S.create_request(conn, "imran", "J000003", "reopen", "typo", now)          # reason too short
    with pytest.raises(S.Denied):
        S.create_request(conn, "kavya", "J000003", "reopen", "Makers cannot reopen jobs", now)
    req = S.create_request(conn, "imran", "J000003", "reopen", "Customs raised a query on the HS code", now)
    S.decide_request(conn, "neha", req["id"], True, t(now, 1), "Query letter attached to the job")
    job = S.get_job(conn, "J000003")
    assert job["stage"] == "Reopened" and job["prepared_by"] is None and job["filed_by"] is None
    S.perform(conn, "kavya", "J000003", "prepare", t(now, 2))
    assert not P.decide(conn, "kavya", "check", S.get_job(conn, "J000003"), t(now, 3)).allowed


# ---------------------------------------------------------------- Q8 admins
def test_admin_cannot_grant_to_self(conn, now):
    with pytest.raises(S.Denied, match="themselves"):
        S.create_grant(conn, "anita", dict(user_id="anita", role_id="maker", reason="I want to help out"), now)


def test_privileged_grant_needs_a_second_admin(conn, now):
    g = S.create_grant(conn, "anita", dict(user_id="tarun", role_id="admin", reason="Backup administrator"), now)
    assert g["status"] == "pending"
    assert not P.decide(conn, "tarun", "admin_fix", S.get_job(conn, "J000007"), t(now, 1)).allowed
    with pytest.raises(S.Denied):
        S.approve_grant(conn, "anita", g["id"], t(now, 1))           # can't approve own request
    S.approve_grant(conn, "vikram", g["id"], t(now, 2))
    assert P.decide(conn, "tarun", "admin_fix", S.get_job(conn, "J000007"), t(now, 3)).allowed


def test_admin_cannot_approve_grant_made_for_them(conn, now):
    g = S.create_grant(conn, "vikram", dict(user_id="anita", role_id="compliance_approver",
                                            reason="Anita wants approval rights"), now)
    with pytest.raises(S.Denied, match="other than the granter and the grantee"):
        S.approve_grant(conn, "anita", g["id"], t(now, 1))


def test_admin_cannot_grow_a_role_they_hold_alone(conn, now):
    r = S.update_role(conn, "anita", "admin",
                      ["access.read", "access.grant", "access.role_manage", "access.absence_manage", "job.view",
                       "job.admin_fix", "job.edit"], "Admins should be able to edit jobs", now)
    assert r["versions"][-1]["status"] == "pending"
    assert "job.edit" not in r["permissions_now"]
    with pytest.raises(S.Denied):
        S.approve_role_version(conn, "anita", "admin", 2, t(now, 1))
    denial = conn.execute(
        "SELECT detail FROM access_events WHERE action='access_denied' ORDER BY id DESC LIMIT 1").fetchone()
    assert denial and '"operation": "role_approval"' in denial["detail"]
    with pytest.raises(S.Denied):     # a filed job stays out of reach whatever happens
        S.edit_job(conn, "anita", "J000003", {"data": {"x": 1}}, t(now, 2))


def test_audit_events_are_append_only(conn):
    for table in ("job_events", "access_events", "decision_log"):
        row = conn.execute(f"SELECT id FROM {table} ORDER BY id LIMIT 1").fetchone()
        assert row
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))


def test_admin_cannot_do_customs_work(conn, now):
    j = new_job(conn, now)
    assert "permission_in_scope" in failed_checks(P.decide(conn, "anita", "prepare", S.get_job(conn, j), now))


def test_admin_can_unstick_but_never_push_forward(conn, now):
    with pytest.raises(S.BadRequest):
        S.perform(conn, "anita", "J000007", "admin_fix", now, op="reset_to_confirmed")      # no reason
    S.perform(conn, "anita", "J000007", "admin_fix", now, op="reset_to_confirmed",
              reason="Only CCU checker prepared it; send back so another maker can prepare")
    job = S.get_job(conn, "J000007")
    assert job["stage"] == "Confirmed" and job["prepared_by"] is None
    with pytest.raises(S.BadRequest):
        S.perform(conn, "anita", "J000007", "admin_fix", t(now, 1), op="mark_filed", reason="not a real op at all")
    with pytest.raises(S.Denied):
        S.perform(conn, "anita", "J000003", "admin_fix", t(now, 1), op="release", reason="Filed jobs are off limits")


def test_branch_access_admin_stays_inside_branch(conn, now):
    S.create_grant(conn, "meera", dict(user_id="rahul", role_id="checker", client_id="BHARAT", branch_id="BOM",
                                       reason="Rahul covers Bharat imports at Mumbai"), now)
    with pytest.raises(S.Denied, match="does not cover"):
        S.create_grant(conn, "meera", dict(user_id="rahul", role_id="checker", branch_id="DEL",
                                           reason="Outside Mumbai: must be refused"), now)
    with pytest.raises(S.Denied, match="does not cover"):
        S.create_grant(conn, "meera", dict(user_id="rahul", role_id="checker",
                                           reason="All branches: must be refused"), now)


# ---------------------------------------------------------------- Q9 parser
def test_parser_create_update_idempotent(conn, now):
    item = dict(external_ref="P-1", client_id="INDUS", branch_id="MAA", direction="import", mode="fcl",
                data={"invoice_value_usd": 10})
    assert S.parser_sync(conn, "svc-parser", [item], now)["results"][0]["status"] == "created"
    assert S.parser_sync(conn, "svc-parser", [item], t(now, 1))["results"][0]["status"] == "unchanged"
    item2 = {**item, "data": {"invoice_value_usd": 11}}
    r = S.parser_sync(conn, "svc-parser", [item2], t(now, 2))["results"][0]
    assert r["status"] == "updated"
    assert S.get_job(conn, r["job_id"])["data"]["invoice_value_usd"] == 11


def test_parser_never_overwrites_work_in_progress(conn, now):
    item = dict(external_ref="P-2", client_id="INDUS", branch_id="MAA", direction="import", mode="fcl",
                data={"invoice_value_usd": 10})
    jid = S.parser_sync(conn, "svc-parser", [item], now)["results"][0]["job_id"]
    S.perform(conn, "gopal", jid, "prepare", t(now, 1))
    r = S.parser_sync(conn, "svc-parser", [{**item, "data": {"invoice_value_usd": 99}}], t(now, 2))["results"][0]
    job = S.get_job(conn, jid)
    assert r["status"] == "held" and job["data"]["invoice_value_usd"] == 10 and "99" in job["needs_attention"]
    r = S.parser_sync(conn, "svc-parser", [{**item, "branch_id": "DEL"}], t(now, 3))["results"][0]
    assert r["status"] == "held" and S.get_job(conn, jid)["branch_id"] == "MAA"


def test_parser_rejects_what_it_cannot_place(conn, now):
    res = S.parser_sync(conn, "svc-parser", [
        dict(external_ref="P-3", client_id="NOPE", branch_id="BOM", direction="import", mode="air"),
        dict(external_ref="P-4", client_id="ACME", branch_id="BOM", direction="import", mode="air", stage="Filed"),
    ], now)
    assert [r["status"] for r in res["results"]] == ["rejected", "rejected"]
    assert conn.execute("SELECT COUNT(*) FROM access_events WHERE action='parser_item_rejected' AND at=?",
                        (now,)).fetchone()[0] == 2


def test_parser_cannot_do_human_work_and_humans_cannot_be_parsers(conn, now):
    d = P.decide(conn, "svc-parser", "prepare", S.get_job(conn, "J000006"), now)
    assert "account_type" in failed_checks(d)
    with pytest.raises(S.BadRequest):
        S.create_grant(conn, "anita", dict(user_id="priya", role_id="parser", reason="should never work"), now)
    with pytest.raises(S.BadRequest):
        S.create_grant(conn, "anita", dict(user_id="svc-parser", role_id="maker", reason="should never work"), now)
    res = S.parser_sync(conn, "priya", [dict(external_ref="P-5", client_id="ACME", branch_id="BOM",
                                             direction="import", mode="air")], now)
    assert res["results"][0]["status"] == "denied" and "account" in res["results"][0]["reason"]


# -------------------------------------------------------- Q12 new role as data
def test_new_role_without_a_deploy(conn, now):
    r = S.create_role(conn, "anita", dict(id="senior_checker", name="Senior Checker",
                                          permissions=["job.view", "job.check", "job.cancel"]), now)
    assert r["versions"][0]["status"] == "active"
    S.create_grant(conn, "anita", dict(user_id="rahul", role_id="senior_checker", branch_id="DEL",
                                       reason="Promotion to senior checker for Delhi"), t(now, 1))
    j = new_job(conn, now, branch="DEL", client="FALCON")
    assert P.decide(conn, "rahul", "cancel", S.get_job(conn, j), t(now, 2)).allowed
    with pytest.raises(S.BadRequest):
        S.create_role(conn, "anita", dict(id="bad", permissions=["job.teleport"]), now)
    with pytest.raises(S.BadRequest):
        S.create_role(conn, "anita", dict(id="bad2", audience="client", permissions=["job.prepare"]), now)
    r = S.create_role(conn, "anita", dict(id="deputy_admin", permissions=["access.grant", "job.view"]), now)
    assert r["privileged"] == 1 and r["versions"][0]["status"] == "pending"
    with pytest.raises(S.Denied):
        S.create_role(conn, "priya", dict(id="mine", permissions=["job.view"]), now)


# ------------------------------------------------------------ Q14 when unsure
def test_unknowns_are_denied(conn, now):
    job = S.get_job(conn, "J000001")
    assert not P.decide(conn, "nobody", "view", job, now).allowed
    assert not P.decide(conn, "sneha", "teleport", job, now).allowed
    d = P.decide(conn, "sneha", "check", {**job, "branch_id": None}, now)
    assert "job_data_complete" in failed_checks(d)


def test_optimistic_concurrency(conn, now):
    j = new_job(conn, now)
    v = S.get_job(conn, j)["version"]
    S.edit_job(conn, "priya", j, {"data": {"a": 1}}, t(now, 1), expected_version=v)
    with pytest.raises(S.Conflict):
        S.edit_job(conn, "priya", j, {"data": {"a": 2}}, t(now, 2), expected_version=v)


def test_moving_a_job_needs_authority_on_both_sides(conn, now):
    j = new_job(conn, now, branch="DEL")
    with pytest.raises(S.Denied):     # kavya is a Delhi maker; she can't push work into Mumbai
        S.edit_job(conn, "kavya", j, {"branch_id": "BOM"}, t(now, 1), reason="Wrong branch on the feed")
    S.edit_job(conn, "priya", j, {"branch_id": "BOM"}, t(now, 1), reason="Wrong branch on the feed")
    assert S.get_job(conn, j)["branch_id"] == "BOM"
