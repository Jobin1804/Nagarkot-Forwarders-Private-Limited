"""Q13 (queue == ability), Q10/Q11 (explanations, history) and the HTTP layer."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app import policy as P
from app import service as S
from app.db import connect, shift


# --------------------------------------------------------------- Q13 parity
def _python_inbox(conn, user_id, now):
    """The slow, obviously-correct definition: run decide() on every job."""
    absences = P.absences_at(conn, now)
    out = set()
    for r in conn.execute("SELECT * FROM jobs"):
        job = dict(r)
        nxt = P.NEXT_ACTION.get(job["stage"])
        if nxt and P.routed_to(user_id, job, absences) and P.decide(conn, user_id, nxt, job, now).allowed:
            out.add(job["id"])
    return out


def _sql_inbox(conn, user_id, now):
    ib = S.inbox(conn, user_id, now, limit=100000)
    assert ib["total"] == len(ib["items"])
    return {i["id"] for i in ib["items"]}


def test_inbox_matches_what_each_person_can_do(conn, now):
    users = [r[0] for r in conn.execute("SELECT id FROM users")]
    for u in users:
        assert _sql_inbox(conn, u, now) == _python_inbox(conn, u, now), u


def test_parity_survives_access_changes(conn, now):
    gid = conn.execute("SELECT id FROM grants WHERE user_id='sneha'").fetchone()[0]
    S.revoke_grant(conn, "anita", gid, "Sneha moves to training", shift(now, minutes=1))
    S.create_grant(conn, "anita", dict(user_id="rahul", role_id="checker", branch_id="BOM", reason="Covers for Sneha"),
                   shift(now, minutes=2))
    t = shift(now, minutes=3)
    for u in ("sneha", "rahul", "priya", "kavya", "gopal"):
        assert _sql_inbox(conn, u, t) == _python_inbox(conn, u, t), u


def test_visible_list_matches_decide(conn, now):
    for u in ("farah", "rahul", "ramesh", "svc-parser", "anita"):
        listed = {j["id"] for j in S.visible_jobs(conn, u, now, limit=100000)["items"]}
        expected = {r["id"] for r in conn.execute("SELECT * FROM jobs")
                    if P.decide(conn, u, "view", dict(r), now).allowed}
        assert listed == expected, u


# ---------------------------------------------------------- Q10 explanation
def test_why_can_she_and_he_cannot(conn, now):
    r = S.compare(conn, "sneha", "rahul", "check", "J000001", now)
    assert "Sneha Iyer can and Rahul Mehta cannot" in r["headline"]
    assert "direction is import but the job's is export" in r["headline"]
    assert [d["check"] for d in r["differences"]] == ["permission_in_scope"]


# --------------------------------------------------------------- Q11 history
def test_who_could_change_job_on_the_3rd(conn, now):
    w = S.who_could(conn, "J000003", "2026-10-03T00:00:00Z", "2026-10-04T00:00:00Z")
    could = {c["user_id"]: c["actions"] for c in w["could_change"]}
    assert "edit" in could["kavya"] and "approve_request" in could["neha"]
    assert "karan" not in could and "rahul" not in could and "ramesh" not in could
    # Before 11:00 the job was Filed: nobody could edit it. Kavya's edit window opens at the reopen.
    assert could["kavya"]["edit"][0][0] == "2026-10-03T11:00:00Z"
    did = [(e["actor_id"], e["action"]) for e in w["actually_changed"]]
    assert ("neha", "reopened") in did and ("kavya", "edited") in did
    tried = {(d["user_id"], d["action"]) for d in w["denied_attempts"]}
    assert {("priya", "edit"), ("karan", "edit"), ("imran", "approve_request")} <= tried


def test_history_records_who_and_on_what_authority(conn, now):
    h = S.job_history(conn, "J000003")
    reopen = next(e for e in h["events"] if e["action"] == "reopened")
    assert reopen["authority"]["requested_by"] == "imran" and reopen["authority"]["approved_by"] == "neha"
    prep = next(e for e in h["events"] if e["action"] == "prepare")
    assert any("Maker" in gr for gr in prep["authority"]["grants"])
    assert len(h["denied_attempts"]) >= 3


def test_history_is_as_of_not_as_now(conn, now):
    # Gopal checked at Chennai until 20 Oct. A question about 10 Oct must say yes.
    job = {"id": "x", "client_id": "INDUS", "branch_id": "MAA", "direction": "import", "mode": "air",
           "stage": "Prepared", "prepared_by": "manoj", "filed_by": None}
    assert P.decide(conn, "gopal", "check", job, "2026-10-10T10:00:00Z").allowed
    assert not P.decide(conn, "gopal", "check", job, now).allowed


# --------------------------------------------------------------------- API
@pytest.fixture
def client(db_file):
    main.configure(db_file)
    return TestClient(main.app)


def H(user):
    return {"X-User": user}


def test_api_denied_action_returns_explanation(client):
    r = client.post("/jobs/J000001/actions/check", headers=H("rahul"), json={})
    assert r.status_code == 403
    body = r.json()
    assert body["decision"]["allowed"] is False
    assert any(c["check"] == "permission_in_scope" and not c["passed"] for c in body["decision"]["checks"])


def test_api_inbox_explain_and_history(client):
    ib = client.get("/me/inbox", headers=H("rahul")).json()
    assert "J000002" in [i["id"] for i in ib["items"]] and ib["total"] >= 1
    cmp = client.get("/explain/compare", params=dict(users="sneha,rahul", action="check", job="J000001"),
                     headers=H("tarun")).json()
    assert "Rahul Mehta cannot" in cmp["headline"]
    assert client.get("/explain/compare", params=dict(users="sneha,rahul", action="check", job="J000001"),
                      headers=H("priya")).status_code == 403
    h = client.get("/jobs/J000003/history", headers=H("tarun")).json()
    assert h["events"][0]["action"] == "created_by_parser"
    w = client.get("/jobs/J000003/who-could", params={"date": "2026-10-03"}, headers=H("tarun")).json()
    assert any(c["user_id"] == "kavya" for c in w["could_change"])


def test_api_client_user_sees_redacted(client):
    r = client.get("/jobs", headers=H("ramesh")).json()
    assert r["items"] and all(i["client_id"] == "ACME" and "internal_notes" not in i["data"] for i in r["items"])
    assert client.get("/jobs/J000003", headers=H("ramesh")).status_code == 404


def test_api_parser_endpoint_only_for_service_accounts(client):
    item = dict(external_ref="API-1", client_id="ACME", branch_id="BOM", direction="import", mode="air", data={})
    assert client.post("/parser/sync", headers=H("anita"), json={"items": [item]}).status_code == 403
    r = client.post("/parser/sync", headers=H("svc-parser"), json={"run_id": "t", "items": [item]})
    assert r.status_code == 200 and r.json()["summary"] == {"created": 1}


def test_api_self_grant_refused(client):
    r = client.post("/grants", headers=H("anita"),
                    json=dict(user_id="anita", role_id="maker", reason="I would like to help out"))
    assert r.status_code == 403 and "themselves" in r.json()["error"]


def test_denied_privilege_escalation_is_audited(client, db_file):
    r = client.post("/grants", headers=H("anita"),
                    json=dict(user_id="anita", role_id="maker", reason="I would like to help out"))
    assert r.status_code == 403
    conn = connect(db_file)
    try:
        row = conn.execute(
            "SELECT actor_id, action, target_type, target_id, detail FROM access_events "
            "WHERE action='access_denied' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["actor_id"] == "anita"
    assert row["target_type"] == "access_attempt" and row["target_id"] == "anita"
    assert json.loads(row["detail"])["operation"] == "grant"


def test_access_directory_requires_access_read(client, db_file):
    assert client.get("/roles", headers=H("priya")).status_code == 403
    assert client.get("/users", headers=H("priya")).status_code == 403
    assert client.get("/users/rahul/access", headers=H("priya")).status_code == 403
    assert client.get("/users/priya/access", headers=H("priya")).status_code == 200
    assert client.get("/roles", headers=H("tarun")).status_code == 200
    assert client.get("/users", headers=H("tarun")).status_code == 200

    conn = connect(db_file)
    try:
        conn.execute("UPDATE users SET deactivated_at=? WHERE id='tarun'", ("2026-11-03T09:00:00Z",))
    finally:
        conn.close()
    assert client.get("/audit", headers=H("tarun")).status_code == 403


def test_authorization_storage_unavailable_fails_closed(client, db_file, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    conn = connect(db_file)
    try:
        before = conn.execute("SELECT stage FROM jobs WHERE id='J000001'").fetchone()[0]
        before_logs = conn.execute(
            "SELECT COUNT(*) FROM decision_log WHERE job_id='J000001' AND user_id='sneha' "
            "AND action='check' AND allowed=1").fetchone()[0]
    finally:
        conn.close()

    monkeypatch.setattr(S, "_save_job", unavailable)
    r = client.post("/jobs/J000001/actions/check", headers=H("sneha"), json={})
    assert r.status_code == 503
    assert "verify write state before retrying" in r.json()["error"]

    conn = connect(db_file)
    try:
        after = conn.execute("SELECT stage FROM jobs WHERE id='J000001'").fetchone()[0]
        after_logs = conn.execute(
            "SELECT COUNT(*) FROM decision_log WHERE job_id='J000001' AND user_id='sneha' "
            "AND action='check' AND allowed=1").fetchone()[0]
    finally:
        conn.close()
    assert after == before and after_logs == before_logs


def test_api_plain_english_access(client):
    a = client.get("/users/rahul/access", headers=H("tarun")).json()
    assert a["plain_english"][0].startswith("Rahul Mehta (rahul, staff, Executive) has role Checker for client "
                                            "ACME Textiles, branch Mumbai, imports only, all modes")
