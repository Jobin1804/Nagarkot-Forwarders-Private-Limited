"""One command, no server, no Swagger:   python demo.py

Loads a fresh copy of the sample data into data/demo.db (your main database is
not touched), then plays every scenario through the real HTTP API and prints
what happened and whether it matched what should happen.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DB = os.path.join(HERE, "data", "demo.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import main, seed  # noqa: E402
from app.db import connect  # noqa: E402

results = []


def title(n, text):
    print("\n" + "=" * 78)
    print(f" STEP {n}: {text}")
    print("=" * 78)


def expect(label, ok):
    results.append((label, ok))
    print(f"   [{'OK' if ok else 'UNEXPECTED'}] {label}")


def H(user):
    return {"X-User": user}


def main_demo():
    print("Loading a fresh copy of the sample data ...")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            os.remove(DB + suffix)
    conn = connect(DB)
    seed.build(conn)
    conn.close()
    main.configure(DB)
    api = TestClient(main.app)
    print("Done: 23 people, 10 roles, ~315 jobs with history.")

    title(1, "Who has what? (plain English)")
    for u in ("priya", "rahul", "sneha", "neha", "ramesh"):
        for line in api.get(f"/users/{u}/access", headers=H("tarun")).json()["plain_english"]:
            print("   " + line.split(" (since")[0])
    expect("everyone's access can be read in plain English", True)

    title(2, "Rahul's to-do list (only jobs he can actually do)")
    ib = api.get("/me/inbox", headers=H("rahul")).json()
    print(f"   Rahul has {ib['total']} job(s) waiting:")
    for i in ib["items"]:
        print(f"   - {i['id']}: {i['client_id']} / {i['branch_id']} / {i['direction']} -> needs '{i['next_action']}'")
    expect("Rahul's list contains only J000002 (ACME, Mumbai, import)", [i["id"] for i in ib["items"]] == ["J000002"])

    title(3, "Rahul tries to check J000001 (ACME, Mumbai, but EXPORT)")
    r = api.post("/jobs/J000001/actions/check", headers=H("rahul"), json={})
    print(f"   HTTP {r.status_code}: {r.json()['error']}")
    expect("refused (403) with the reason given", r.status_code == 403 and "import" in r.json()["error"])

    title(4, "Why can Sneha do this and Rahul can't?")
    r = api.get("/explain/compare", params=dict(users="sneha,rahul", action="check", job="J000001"),
                headers=H("tarun")).json()
    print("   " + r["headline"])
    expect("the answer points at the one difference (direction)", "direction is import" in r["headline"])
    r = api.get("/explain/compare", params=dict(users="priya,sneha", action="check", job="J000001"),
                headers=H("tarun")).json()
    print("   " + r["headline"])
    expect("Priya is qualified but prepared it herself, so she can't check it", "prepared" in r["headline"])

    title(5, "Sneha checks J000001")
    r = api.post("/jobs/J000001/actions/check", headers=H("sneha"), json={})
    print(f"   HTTP {r.status_code}: job is now {r.json().get('stage')}")
    expect("allowed (200) and the job moves to Checked", r.status_code == 200 and r.json()["stage"] == "Checked")

    title(6, "History of J000003 (filed, reopened on 3 Oct, filed again)")
    h = api.get("/jobs/J000003/history", headers=H("tarun")).json()
    rows = [(e["at"], f"{e['actor_id']:<10} {e['action']:<18} -> {e['to_stage']}") for e in h["events"]]
    rows += [(d["at"], f"REFUSED: {d['summary'][8:]}") for d in h["denied_attempts"]]
    for at, line in sorted(rows):
        print(f"   {at[:16].replace('T', ' ')}  {line[:110]}")
    reopen = next(e for e in h["events"] if e["action"] == "reopened")
    expect("reopening needed two people (Imran asked, Neha approved)",
           reopen["authority"]["requested_by"] == "imran" and reopen["authority"]["approved_by"] == "neha")
    expect("refused attempts are kept in the history", len(h["denied_attempts"]) >= 3)

    title(7, "Who could have changed J000003 on 3 Oct 2026?")
    w = api.get("/jobs/J000003/who-could", params={"date": "2026-10-03"}, headers=H("tarun")).json()
    for c in w["could_change"]:
        acts = ", ".join(f"{a} {ws[0][0][11:16]}-{ws[-1][1][11:16]}" for a, ws in c["actions"].items())
        print(f"   {c['name']:<14} {acts}")
    names = {c["user_id"] for c in w["could_change"]}
    expect("Delhi staff and the approver could; Karan (Mumbai) could not", {"kavya", "neha"} <= names and "karan" not in names)

    title(8, "Ramesh, an employee of the client ACME, follows his shipments")
    jobs = api.get("/jobs", headers=H("ramesh")).json()
    print(f"   Ramesh sees {jobs['total']} jobs, all ACME: {all(j['client_id'] == 'ACME' for j in jobs['items'])}")
    print(f"   Internal notes hidden: {all('internal_notes' not in j['data'] for j in jobs['items'])}")
    other = api.get("/jobs/J000003", headers=H("ramesh"))
    print(f"   Asking for another client's job J000003 -> HTTP {other.status_code}")
    expect("client user sees only their own jobs, redacted", other.status_code == 404
           and all(j["client_id"] == "ACME" for j in jobs["items"]))

    title(9, "Admin Anita tries to give herself more power")
    r = api.post("/grants", headers=H("anita"),
                 json=dict(user_id="anita", role_id="compliance_approver", reason="I want more power please"))
    print(f"   HTTP {r.status_code}: {r.json()['error']}")
    expect("self-promotion refused", r.status_code == 403)
    r = api.post("/grants", headers=H("anita"),
                 json=dict(user_id="tarun", role_id="admin", reason="Backup administrator needed"))
    g = r.json()
    print(f"   Anita makes Tarun an admin -> status '{g['status']}' (needs a second admin)")
    r2 = api.post(f"/grants/{g['id']}/approve", headers=H("anita"))
    print(f"   Anita approves her own request -> HTTP {r2.status_code}")
    r3 = api.post(f"/grants/{g['id']}/approve", headers=H("vikram"))
    print(f"   Vikram approves -> status '{r3.json()['status']}'")
    expect("powerful roles need a second admin", g["status"] == "pending" and r2.status_code == 403
           and r3.json()["status"] == "active")

    title(10, "Cancelling a FILED job needs two different people")
    reqs = api.get("/me/approvals", headers=H("neha")).json()
    req = next(x for x in reqs if x["job_id"] == "J000004")
    print(f"   Request #{req['id']} by {req['requested_by']}: \"{req['reason']}\"")
    r = api.post(f"/requests/{req['id']}/approve", headers=H("meera"), json={"note": "approving my own request"})
    print(f"   Meera approves her own request -> HTTP {r.status_code}")
    r2 = api.post(f"/requests/{req['id']}/approve", headers=H("neha"),
                  json={"note": "Buyer cancellation confirmed in writing"})
    stage = api.get("/jobs/J000004", headers=H("tarun")).json()["stage"]
    print(f"   Neha approves -> HTTP {r2.status_code}; J000004 is now {stage}")
    expect("requester can't approve; a second person can", r.status_code == 403 and stage == "Cancelled")
    r = api.patch("/jobs/J000003", headers=H("kavya"), json={"data": {"invoice_value_usd": 1}})
    print(f"   Kavya tries to edit filed job J000003 directly -> HTTP {r.status_code}")
    expect("filed jobs are locked against editing", r.status_code == 403)

    title(11, "The overnight parser and a stuck job")
    j5 = api.get("/jobs/J000005", headers=H("tarun")).json()
    print(f"   J000005: {j5['needs_attention']}")
    expect("parser did not overwrite a job people were working on", j5["needs_attention"] is not None)
    e = api.get("/jobs/J000007/eligible", headers=H("tarun")).json()
    print(f"   J000007: {e['note']}")
    expect("a job nobody can move is reported as STUCK", e["eligible"] == [])

    ok = sum(1 for _, x in results if x)
    print("\n" + "=" * 78)
    print(f" RESULT: {ok}/{len(results)} checks behaved as expected")
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main_demo())
