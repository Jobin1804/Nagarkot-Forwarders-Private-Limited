"""Sample data: `python -m app.seed` (rebuilds data/nfpl.db).

Everything after the bootstrap goes through the real service functions, in
time order, so history, grants and decisions are genuine: the seed is itself a
long integration test. A heap merges many small "stories" (one per job plus
global events like a revocation) so each step runs at its own timestamp.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import os
import random

from . import policy as P
from . import service as S
from .db import connect, init_schema, iso, parse_ts, shift, utcnow, dumps

BRANCHES = {"BOM": "Mumbai", "DEL": "Delhi", "MAA": "Chennai", "BLR": "Bengaluru", "CCU": "Kolkata"}
CLIENTS = {
    "ACME": "ACME Textiles", "BHARAT": "Bharat Pharma", "COASTAL": "Coastal Spices", "DECCAN": "Deccan Motors",
    "EVEREST": "Everest Electronics", "FALCON": "Falcon Apparel", "GANGA": "Ganga Foods",
    "HIMALAYA": "Himalaya Herbals", "INDUS": "Indus Steel", "JAIPUR": "Jaipur Gems",
}

ROLES = {
    # id: (name, audience, permissions, description)
    "admin": ("Administrator", "staff",
              ["access.read", "access.grant", "access.role_manage", "access.absence_manage", "job.view", "job.admin_fix"],
              "Runs access. Can unstick jobs. Does not do customs work."),
    "maker": ("Maker", "staff", ["job.view", "job.create", "job.edit", "job.prepare"], "Prepares checklists."),
    "checker": ("Checker", "staff", ["job.view", "job.check"], "Checks / approves checklists."),
    "filer": ("Filer", "staff", ["job.view", "job.file"], "Files checked jobs with customs."),
    "ops_manager": ("Operations Manager", "staff",
                    ["job.view", "job.edit", "job.cancel", "job.assign", "job.request_cancel_filed",
                     "job.request_reopen", "access.absence_manage"],
                    "Runs a branch day to day; can ask for filed jobs to be cancelled or reopened."),
    "compliance_approver": ("Compliance Approver", "staff", ["job.view", "job.approve_sensitive", "access.read"],
                            "Second signature on cancel-filed and reopen."),
    "branch_access_admin": ("Branch Access Admin", "staff", ["job.view", "access.grant"],
                            "Can grant/revoke access, but only inside their own branch."),
    "auditor": ("Auditor", "staff", ["job.view", "access.read"], "Reads everything, changes nothing."),
    "client_viewer": ("Client Viewer", "client", ["job.view_client"], "A client's employee following their shipments."),
    "parser": ("Overnight Parser", "service", ["job.sync"], "Creates/updates Confirmed jobs from the feed."),
}

# (id, name, kind, client, home branch, title)
USERS = [
    ("anita", "Anita Rao", "staff", None, "BOM", "Head of IT Operations"),
    ("vikram", "Vikram Shah", "staff", None, "DEL", "IT Administrator"),
    ("priya", "Priya Nair", "staff", None, "BOM", "Senior Executive"),
    ("rahul", "Rahul Mehta", "staff", None, "BOM", "Executive"),
    ("sneha", "Sneha Iyer", "staff", None, "BOM", "Team Lead"),
    ("arjun", "Arjun Das", "staff", None, "DEL", "Executive"),
    ("kavya", "Kavya Menon", "staff", None, "DEL", "Executive"),
    ("manoj", "Manoj Kumar", "staff", None, "MAA", "Executive"),
    ("pooja", "Pooja Sharma", "staff", None, "DEL", "Team Lead"),
    ("deepa", "Deepa Pillai", "staff", None, "BOM", "Filing Officer"),
    ("suresh", "Suresh Gupta", "staff", None, "MAA", "Filing Officer"),
    ("meera", "Meera Joshi", "staff", None, "BOM", "Branch Manager, Mumbai"),
    ("imran", "Imran Sheikh", "staff", None, "DEL", "Branch Manager, Delhi"),
    ("rohan", "Rohan Bose", "staff", None, "CCU", "Branch In-charge, Kolkata"),
    ("neha", "Neha Kapoor", "staff", None, "BOM", "Head of Compliance"),
    ("amit", "Amit Verma", "staff", None, "BOM", "Air Specialist"),
    ("farah", "Farah Khan", "staff", None, "BLR", "Executive"),
    ("tarun", "Tarun Reddy", "staff", None, "BOM", "Director (read-only)"),
    ("lakshmi", "Lakshmi S", "staff", None, "BLR", "Executive"),
    ("gopal", "Gopal Rao", "staff", None, "MAA", "Executive"),
    ("karan", "Karan Malhotra", "staff", None, "BOM", "Executive (left 15 Oct)"),
    ("ramesh", "Ramesh Patel", "client", "ACME", None, "Logistics Manager at ACME Textiles"),
    ("svc-parser", "Overnight Parser", "service", None, None, "Service account"),
]

SETUP = "2026-09-01T03:30:00Z"


def T(day, hh=9, mm=0, month=10):
    return f"2026-{month:02d}-{day:02d}T{hh:02d}:{mm:02d}:00Z"


def g(user, role, client=None, branch=None, direction=None, mode=None, valid_from=None, valid_to=None,
      reason="Initial access set-up for the sample data"):
    return dict(user_id=user, role_id=role, client_id=client, branch_id=branch, direction=direction, mode=mode,
                valid_from=valid_from, valid_to=valid_to, reason=reason)


def bootstrap(conn, now=SETUP):
    """The one step that does not go through the rules: there is no admin yet to approve
    the first admins. It is recorded in the audit trail as actor 'system:bootstrap'."""
    init_schema(conn)
    with S.tx(conn):
        for k, v in BRANCHES.items():
            conn.execute("INSERT INTO branches VALUES (?,?)", (k, v))
        for k, v in CLIENTS.items():
            conn.execute("INSERT INTO clients VALUES (?,?)", (k, v))
        for uid, name, kind, client, home, title in USERS:
            conn.execute("INSERT INTO users(id,name,kind,client_id,home_branch,title,created_at) VALUES (?,?,?,?,?,?,?)",
                         (uid, name, kind, client, home, title, now))
        for rid, (name, aud, perms, desc) in ROLES.items():
            priv = int(bool(set(perms) & P.PRIVILEGED_PERMISSIONS))
            conn.execute("INSERT INTO roles VALUES (?,?,?,?,?,?,?)", (rid, name, desc, aud, priv, now, "system:bootstrap"))
            conn.execute("INSERT INTO role_versions(role_id,version,permissions,status,effective_from,changed_by,created_at,reason)"
                         " VALUES (?,?,?,?,?,?,?,?)", (rid, 1, json.dumps(sorted(perms)), "active", now,
                                                      "system:bootstrap", now, "initial role catalogue"))
        for uid in ("anita", "vikram"):
            cur = conn.execute(
                "INSERT INTO grants(user_id,role_id,valid_from,status,effective_from,reason,granted_by,approved_by,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)", (uid, "admin", now, "active", now, "bootstrap administrator",
                                                 "system:bootstrap", "system:bootstrap", now))
            S.access_event(conn, now, "system:bootstrap", "grant_created", "grant", cur.lastrowid,
                           {"user_id": uid, "role_id": "admin"})


def grant_all(conn, now):
    """All other access is granted by Anita through the normal API rules; privileged
    roles are approved by Vikram (four eyes)."""
    t = now
    specs = [
        g("priya", "maker"),
        g("priya", "checker", client="ACME"),
        g("priya", "checker", client="BHARAT"),
        g("rahul", "checker", client="ACME", branch="BOM", direction="import"),
        g("sneha", "checker", branch="BOM"),
        g("arjun", "maker", branch="DEL"),
        g("kavya", "maker", branch="DEL"),
        g("manoj", "maker", branch="MAA"),
        g("pooja", "checker", branch="DEL"),
        g("pooja", "checker", branch="MAA"),
        g("deepa", "filer", branch="BOM"),
        g("deepa", "filer", branch="DEL"),
        g("suresh", "filer", branch="MAA"),
        g("suresh", "filer", branch="BLR"),
        g("suresh", "filer", branch="CCU"),
        g("meera", "ops_manager", branch="BOM"),
        g("meera", "branch_access_admin", branch="BOM"),
        g("imran", "ops_manager", branch="DEL"),
        g("rohan", "maker", branch="CCU"),
        g("rohan", "checker", branch="CCU"),
        g("rohan", "filer", branch="CCU"),
        g("neha", "compliance_approver"),
        g("amit", "checker", client="COASTAL", mode="air"),
        g("amit", "checker", client="DECCAN", mode="air"),
        g("farah", "maker", client="EVEREST", branch="BLR", direction="export"),
        g("tarun", "auditor"),
        g("lakshmi", "maker", branch="BLR"),
        g("lakshmi", "checker", branch="BLR"),
        g("gopal", "maker", branch="MAA"),
        g("gopal", "checker", branch="MAA"),
        g("karan", "maker", branch="BOM"),
        g("ramesh", "client_viewer"),          # client forced to ACME by the service
        g("svc-parser", "parser"),
    ]
    ids = {}
    for spec in specs:
        t = shift(t, minutes=1)
        out = S.create_grant(conn, "anita", spec, t)
        if out["status"] == "pending":
            out = S.approve_grant(conn, "vikram", out["id"], shift(t, seconds=30))
        ids[(spec["user_id"], spec["role_id"], spec.get("client_id"), spec.get("branch_id"))] = out["id"]
    return ids


# ------------------------------------------------------------------ helpers
def eligible(conn, action, job, t, exclude=()):
    absent = P.absences_at(conn, t)
    out = []
    for (uid,) in conn.execute("SELECT id FROM users WHERE kind='staff' ORDER BY id"):
        if uid in exclude or uid in absent:
            continue
        if P.decide(conn, uid, action, job, t).allowed:
            out.append(uid)
    return out


def parser_create(conn, t, ext, client, branch, direction, mode, data, run_id):
    res = S.parser_sync(conn, "svc-parser", [dict(external_ref=ext, client_id=client, branch_id=branch,
                                                  direction=direction, mode=mode, data=data)], t, run_id)
    r = res["results"][0]
    assert r["status"] == "created", r
    return r["job_id"]


def fake_data(rng, client):
    return {"consignee": CLIENTS[client], "invoice_value_usd": rng.randrange(2_000, 250_000, 100),
            "hs_code": f"{rng.randrange(1000, 9999)}.{rng.randrange(10, 99)}",
            "bl_or_awb": f"{rng.choice(['MAEU', 'HLCU', 'AWB'])}{rng.randrange(10**7, 10**8)}",
            "internal_notes": rng.choice(["", "Client prefers morning calls", "Check COO original",
                                          "Duty drawback claim pending"])}


# ------------------------------------------------------------------ stories
def demo_stories(conn, now):
    """Named jobs used in README / DEMO. Created first so they get J000001..J000007."""

    def d1():  # ACME / BOM / export / air, prepared by Priya. Sneha can check; Rahul can't; Priya can't.
        yield T(1, 3, 0)
        j = parser_create(conn, T(1, 3, 0), "ACME-EXP-0001", "ACME", "BOM", "export", "air",
                          {"consignee": "Zara Spain", "invoice_value_usd": 48200, "internal_notes": "Rush"}, "night-2026-10-01")
        yield T(2, 10)
        S.claim(conn, "priya", j, T(2, 10))
        yield T(2, 11)
        S.perform(conn, "priya", j, "prepare", T(2, 11))

    def d2():  # ACME / BOM / import / fcl, prepared by Priya. Rahul CAN check this one.
        yield T(1, 3, 1)
        j = parser_create(conn, T(1, 3, 1), "ACME-IMP-0002", "ACME", "BOM", "import", "fcl",
                          {"consignee": "ACME Textiles", "invoice_value_usd": 91000}, "night-2026-10-01")
        yield T(2, 12)
        S.perform(conn, "priya", j, "prepare", T(2, 12))

    def d3():  # BHARAT / DEL / import / air: filed, then reopened on the 3rd. The history demo.
        yield T(1, 3, 2)
        j = parser_create(conn, T(1, 3, 2), "BHARAT-IMP-0003", "BHARAT", "DEL", "import", "air",
                          {"consignee": "Bharat Pharma", "invoice_value_usd": 120000, "hs_code": "3004.90"},
                          "night-2026-10-01")
        yield T(1, 10)
        S.perform(conn, "kavya", j, "prepare", T(1, 10))
        yield T(2, 9)
        S.perform(conn, "priya", j, "check", T(2, 9))
        yield T(2, 15)
        S.perform(conn, "deepa", j, "file", T(2, 15))
        yield T(3, 8)
        try:  # the incident pattern: someone tries to alter a filed job. Refused and recorded.
            S.edit_job(conn, "priya", j, {"data": {"invoice_value_usd": 102000}}, T(3, 8))
        except S.Denied:
            pass
        yield T(3, 9)
        req = S.create_request(conn, "imran", j, "reopen",
                               "Supplier sent a corrected invoice: value is USD 102,000 not 120,000", T(3, 9))
        yield T(3, 9, 30)
        try:  # requester tries to approve own request
            S.decide_request(conn, "imran", req["id"], True, T(3, 9, 30), "approving my own request")
        except S.Denied:
            pass
        yield T(3, 11)
        S.decide_request(conn, "neha", req["id"], True, T(3, 11), "Corrected invoice seen; amendment needed")
        yield T(3, 12)
        S.edit_job(conn, "kavya", j, {"data": {"invoice_value_usd": 102000}}, T(3, 12),
                   reason="Corrected invoice from supplier")
        yield T(3, 12, 30)
        try:  # karan (Mumbai maker) has no business touching a Delhi job
            S.edit_job(conn, "karan", j, {"data": {"internal_notes": "x"}}, T(3, 12, 30))
        except S.Denied:
            pass
        yield T(3, 14)
        S.perform(conn, "kavya", j, "prepare", T(3, 14))
        yield T(3, 15)
        try:  # kavya prepared it; she may not check it (and is not a checker then)
            S.perform(conn, "kavya", j, "check", T(3, 15))
        except S.Denied:
            pass
        yield T(4, 10)
        S.perform(conn, "pooja", j, "check", T(4, 10))
        yield T(4, 16)
        S.perform(conn, "deepa", j, "file", T(4, 16))

    def d4():  # COASTAL / BOM / export / air: filed, cancel requested and waiting for Neha.
        yield T(1, 3, 3)
        j = parser_create(conn, T(1, 3, 3), "COASTAL-EXP-0004", "COASTAL", "BOM", "export", "air",
                          {"consignee": "Spice Hub Dubai", "invoice_value_usd": 15500}, "night-2026-10-01")
        yield T(5, 10)
        S.perform(conn, "priya", j, "prepare", T(5, 10))
        yield T(5, 14)
        S.perform(conn, "amit", j, "check", T(5, 14))
        yield T(6, 11)
        S.perform(conn, "deepa", j, "file", T(6, 11))
        yield shift(now, hours=-3)
        S.create_request(conn, "meera", j, "cancel_filed",
                         "Buyer cancelled the order before loading; shipping bill must be withdrawn", shift(now, hours=-3))

    def d5():  # JAIPUR / BLR / export / lcl: prepared, then the parser tries to change it -> held.
        yield T(1, 3, 4)
        j = parser_create(conn, T(1, 3, 4), "JAIPUR-EXP-0005", "JAIPUR", "BLR", "export", "lcl",
                          {"consignee": "Gems Antwerp", "invoice_value_usd": 64000}, "night-2026-10-01")
        yield T(7, 10)
        S.perform(conn, "lakshmi", j, "prepare", T(7, 10))
        yield shift(now, hours=-6)
        S.parser_sync(conn, "svc-parser", [dict(external_ref="JAIPUR-EXP-0005", client_id="JAIPUR", branch_id="BLR",
                                                direction="export", mode="lcl",
                                                data={"invoice_value_usd": 66500})], shift(now, hours=-6), "night-latest")

    def d6():  # EVEREST / BLR / export / lcl: Farah's narrow slice.
        yield T(1, 3, 5)
        parser_create(conn, T(1, 3, 5), "EVEREST-EXP-0006", "EVEREST", "BLR", "export", "lcl",
                      {"consignee": "Mouser EU", "invoice_value_usd": 33000}, "night-2026-10-01")

    def d7():  # GANGA / CCU / import / fcl: Rohan prepared it and is the only CCU checker -> stuck.
        yield T(1, 3, 6)
        j = parser_create(conn, T(1, 3, 6), "GANGA-IMP-0007", "GANGA", "CCU", "import", "fcl",
                          {"consignee": "Ganga Foods", "invoice_value_usd": 27000}, "night-2026-10-01")
        yield T(8, 10)
        S.perform(conn, "rohan", j, "prepare", T(8, 10))

    return [d1(), d2(), d3(), d4(), d5(), d6(), d7()]


def random_job_story(conn, rng, n, start, latest):
    client = rng.choice(list(CLIENTS))
    branch = rng.choice(list(BRANCHES))
    direction = rng.choice(P.DIRECTIONS)
    mode = rng.choice(P.MODES)
    target = rng.choices(["Confirmed", "Prepared", "Checked", "Filed", "Cancelled", "Reopened"],
                         [24, 18, 12, 36, 5, 5])[0]
    span = (parse_ts(latest) - parse_ts(start)).total_seconds()
    t = shift(start, seconds=int(rng.random() * span * 0.85))
    t = t[:14] + "00:00Z"
    ext = f"{client}-{direction[:3].upper()}-{1000 + n}"

    def step(hours_lo=2, hours_hi=40):
        nonlocal t
        t = shift(t, hours=rng.randint(hours_lo, hours_hi))
        return t if t < latest else None

    def story():
        yield t
        job_id = parser_create(conn, t, ext, client, branch, direction, mode, fake_data(rng, client),
                               f"night-{t[:10]}")
        job = S.get_job(conn, job_id)
        # Some makers pick work up from the pool as soon as it lands.
        if rng.random() < 0.35:
            who = eligible(conn, "prepare", job, t)
            if who:
                S.claim(conn, rng.choice(who), job_id, t)
        if target == "Confirmed":
            return
        nxt = step()
        if nxt is None:
            return
        yield nxt
        job = S.get_job(conn, job_id)
        who = [job["assignee_id"]] if job["assignee_id"] and P.decide(conn, job["assignee_id"], "prepare", job, nxt).allowed \
            and job["assignee_id"] not in P.absences_at(conn, nxt) else eligible(conn, "prepare", job, nxt)
        if not who:
            return
        S.perform(conn, rng.choice(who), job_id, "prepare", nxt)
        if target == "Prepared":
            if rng.random() < 0.3:
                job = S.get_job(conn, job_id)
                c = eligible(conn, "check", job, nxt)
                if c:
                    S.claim(conn, rng.choice(c), job_id, shift(nxt, minutes=5))
            return
        nxt = step()
        if nxt is None:
            return
        yield nxt
        job = S.get_job(conn, job_id)
        if job["assignee_id"]:
            who = [job["assignee_id"]] if P.decide(conn, job["assignee_id"], "check", job, nxt).allowed else []
        else:
            who = eligible(conn, "check", job, nxt)
        if not who:
            return
        if rng.random() < 0.08:  # a send-back now and then
            S.perform(conn, who[0], job_id, "send_back", nxt, reason="HS code does not match invoice description")
            return
        S.perform(conn, rng.choice(who), job_id, "check", nxt)
        if target in ("Checked",):
            return
        if target == "Cancelled":
            nxt = step(1, 10)
            if nxt is None:
                return
            yield nxt
            job = S.get_job(conn, job_id)
            who = eligible(conn, "cancel", job, nxt)
            if who:
                S.perform(conn, rng.choice(who), job_id, "cancel", nxt, reason="Client cancelled the shipment")
            return
        nxt = step()
        if nxt is None:
            return
        yield nxt
        job = S.get_job(conn, job_id)
        who = eligible(conn, "file", job, nxt)
        if not who:
            return
        S.perform(conn, rng.choice(who), job_id, "file", nxt)
        if target == "Reopened":
            nxt = step(24, 72)
            if nxt is None:
                return
            yield nxt
            job = S.get_job(conn, job_id)
            req_by = eligible(conn, "request_reopen", job, nxt)
            if not req_by:
                return
            req = S.create_request(conn, rng.choice(req_by), job_id, "reopen",
                                   "Customs query requires an amendment to the bill of entry", nxt)
            nxt = step(1, 6)
            if nxt is None:
                return
            yield nxt
            S.decide_request(conn, "neha", req["id"], True, nxt, "Amendment justified by customs query")

    return story()


def global_stories(conn, now, grant_ids):
    def karan_leaves():
        yield T(15, 18)
        with S.tx(conn):
            conn.execute("UPDATE users SET deactivated_at=? WHERE id='karan'", (T(15, 18),))
            S.access_event(conn, T(15, 18), "anita", "user_deactivated", "user", "karan",
                           {"reason": "Left the company"})

    def gopal_moves():
        # Gopal stops checking at Chennai on 20 Oct; he keeps making. From next month he checks at Bengaluru.
        yield T(20, 9)
        S.revoke_grant(conn, "anita", grant_ids[("gopal", "checker", None, "MAA")],
                       "Gopal moves to making only at Chennai (team restructure)", T(20, 9))
        S.create_grant(conn, "anita", g("gopal", "checker", branch="BLR", valid_from=shift(now, days=30),
                                        reason="Transfer: will check for Bengaluru from next month"), T(20, 9, 5))

    def absences():
        start = shift(now, days=-2)
        yield start
        S.set_absence(conn, "arjun", dict(starts_at=start, ends_at=shift(now, days=12), delegate_id="kavya",
                                          reason="Annual leave"), start)
        yield shift(now, days=-1)
        S.set_absence(conn, "anita", dict(user_id="manoj", starts_at=shift(now, days=-1), ends_at=shift(now, days=5),
                                          reason="Sick leave"), shift(now, days=-1))

    def temp_cover():
        t = shift(now, days=-2, minutes=5)
        yield t
        S.create_grant(conn, "anita", g("kavya", "checker", client="BHARAT", branch="DEL",
                                        valid_to=shift(now, days=12),
                                        reason="Temporary cover: Bharat Pharma checks at Delhi while Arjun is away"), t)

    def overnight():
        t = shift(now, hours=-6, minutes=1)
        yield t
        rng = random.Random(7)
        items = []
        for i in range(8):
            c = rng.choice(list(CLIENTS))
            items.append(dict(external_ref=f"{c}-NEW-{9000 + i}", client_id=c, branch_id=rng.choice(list(BRANCHES)),
                              direction=rng.choice(P.DIRECTIONS), mode=rng.choice(P.MODES), data=fake_data(rng, c)))
        items.append(dict(external_ref="BAD-0001", client_id="NOSUCH", branch_id="BOM", direction="import",
                          mode="air", data={}))  # unknown client: quarantined, not guessed
        items.append(dict(external_ref="EVEREST-EXP-0006", client_id="EVEREST", branch_id="BLR", direction="export",
                          mode="lcl", data={"consignee": "Mouser EU", "invoice_value_usd": 33500}))  # update
        S.parser_sync(conn, "svc-parser", items, t, "night-latest")

    return [karan_leaves(), gopal_moves(), absences(), temp_cover(), overnight()]


def run(conn, stories):
    heap, seq = [], itertools.count()
    for st in stories:
        try:
            heapq.heappush(heap, (next(st), next(seq), st))
        except StopIteration:
            pass
    while heap:
        _t, _, st = heapq.heappop(heap)
        try:
            heapq.heappush(heap, (next(st), next(seq), st))
        except StopIteration:
            pass


def build(conn, now=None, n_jobs=300, seed=42):
    now = now or utcnow()
    if now < "2026-11-01":
        raise SystemExit("the sample story runs through October 2026; use a 'now' on or after 2026-11-01")
    bootstrap(conn)
    grant_ids = grant_all(conn, "2026-09-01T04:00:00Z")
    rng = random.Random(seed)
    latest = shift(now, days=-1)
    stories = demo_stories(conn, now) + global_stories(conn, now, grant_ids)
    stories += [random_job_story(conn, rng, i, T(1, 6), latest) for i in range(n_jobs)]
    run(conn, stories)
    with S.tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('seeded_at',?)", (now,))
    S.reconcile_expired(conn, now)
    return now


def main():
    ap = argparse.ArgumentParser(description="Load the NFPL sample data (replaces the database).")
    ap.add_argument("--db", default=None, help="database path (default data/nfpl.db or $NFPL_DB)")
    ap.add_argument("--now", default=None, help="pretend 'now' (default: real clock)")
    ap.add_argument("--jobs", type=int, default=300)
    a = ap.parse_args()
    from .db import DEFAULT_DB
    path = a.db or DEFAULT_DB
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = connect(path)
    now = build(conn, iso(parse_ts(a.now)) if a.now else None, a.jobs)
    counts = {r[0]: r[1] for r in conn.execute("SELECT stage, COUNT(*) FROM jobs GROUP BY stage")}
    print(f"Loaded sample data into {os.path.abspath(path)} (as of {now})")
    print(f"  users={conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]} "
          f"grants={conn.execute('SELECT COUNT(*) FROM grants').fetchone()[0]} "
          f"jobs={sum(counts.values())} events={conn.execute('SELECT COUNT(*) FROM job_events').fetchone()[0]}")
    print("  jobs by stage:", dumps(counts))


if __name__ == "__main__":
    main()
