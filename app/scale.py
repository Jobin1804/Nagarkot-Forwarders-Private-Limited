"""Scale case: `python -m app.scale` -> data/scale.db with 500 users and 200,000 jobs,
then times the hot paths. Bulk-inserted (not via the service) because the point is to
measure reads; the sample data covers correctness of writes.

    python -m app.scale                       # build + benchmark
    python -m app.scale --bench-only          # benchmark an existing scale.db
    NFPL_DB=data/scale.db uvicorn app.main:app   # serve it
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time

from . import policy as P
from . import seed
from . import service as S
from .db import connect, dumps, shift

NOW = "2026-11-03T09:00:00Z"
SETUP = "2025-01-01T00:00:00Z"   # before the oldest generated job


def build(path, users=500, jobs=200_000, rnd=1):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = connect(path)
    conn.execute("PRAGMA synchronous = OFF")
    seed.bootstrap(conn, SETUP)
    rng = random.Random(rnd)
    t0 = time.time()
    branches = list(seed.BRANCHES)
    clients = list(seed.CLIENTS) + [f"C{n:03d}" for n in range(90)]   # 100 clients
    with S.tx(conn):
        conn.executemany("INSERT INTO clients VALUES (?,?)", [(c, f"Client {c}") for c in clients[10:]])
        # Keep the 23 named sample users (same awkward cases), add generated staff up to `users`.
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new_users = [(f"u{n:04d}", f"Staff {n:04d}", "staff", None, rng.choice(branches), None, SETUP)
                     for n in range(users - existing)]
        conn.executemany("INSERT INTO users(id,name,kind,client_id,home_branch,title,created_at) VALUES (?,?,?,?,?,?,?)",
                         new_users)
        grants = []

        def add(uid, role, c=None, b=None, d=None, m=None):
            grants.append((uid, role, c, b, d, m, SETUP, "active", SETUP, "scale data", "system:scale",
                           SETUP))

        # the named awkward cases
        add("priya", "maker"); add("priya", "checker", "ACME"); add("priya", "checker", "BHARAT")
        add("rahul", "checker", "ACME", "BOM", "import"); add("sneha", "checker", None, "BOM")
        add("farah", "maker", "EVEREST", "BLR", "export"); add("tarun", "auditor")
        add("neha", "compliance_approver"); add("ramesh", "client_viewer", "ACME"); add("svc-parser", "parser")
        for uid, *_ in new_users:
            role = rng.choices(["maker", "checker", "filer", "ops_manager", "auditor"], [50, 30, 10, 5, 5])[0]
            shape = rng.choices(["global", "branch", "client", "client_branch", "narrow"], [5, 35, 30, 20, 10])[0]
            for _ in range(rng.choice([1, 1, 2, 3])):
                c, b = rng.choice(clients), rng.choice(branches)
                if shape == "global":
                    add(uid, role)
                elif shape == "branch":
                    add(uid, role, None, b)
                elif shape == "client":
                    add(uid, role, c)
                elif shape == "client_branch":
                    add(uid, role, c, b)
                else:
                    add(uid, role, c, b, rng.choice(P.DIRECTIONS), rng.choice(P.MODES))
        conn.executemany("""INSERT INTO grants(user_id,role_id,client_id,branch_id,direction,mode,valid_from,status,
                            effective_from,reason,granted_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", grants)
        staff = [u[0] for u in new_users]
        absent = rng.sample(staff, 15)
        conn.executemany("INSERT INTO absences(user_id,starts_at,ends_at,delegate_id,created_by,created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         [(u, shift(NOW, days=-2), shift(NOW, days=5), rng.choice([None, rng.choice(staff)]),
                           "system:scale", NOW) for u in absent])
    print(f"  users/grants written in {time.time() - t0:.1f}s")

    t0 = time.time()
    stages = ["Confirmed", "Prepared", "Checked", "Filed", "Cancelled", "Reopened"]
    weights = [15, 10, 7, 60, 5, 3]
    makers = staff[: len(staff) // 2] + ["priya"]
    batch_jobs, batch_events = [], []
    with S.tx(conn):
        for n in range(1, jobs + 1):
            jid = f"J{n:06d}"
            stage = rng.choices(stages, weights)[0]
            created = shift("2025-11-01T00:00:00Z", minutes=n * 2 + rng.randint(0, 1))
            row = {"id": jid, "external_ref": f"EXT-{n}", "client_id": rng.choice(clients),
                   "branch_id": rng.choice(branches), "direction": rng.choice(P.DIRECTIONS),
                   "mode": rng.choice(P.MODES), "stage": stage,
                   "assignee_id": rng.choice(staff) if stage in P.OPEN_STAGES and rng.random() < 0.3 else None,
                   "prepared_by": rng.choice(makers) if stage in ("Prepared", "Checked", "Filed") else None,
                   "checked_by": None, "filed_by": None, "needs_attention": None,
                   "data": {"invoice_value_usd": rng.randrange(1000, 100000)}, "version": 1,
                   "created_at": created, "updated_at": created}
            batch_jobs.append((row["id"], row["external_ref"], row["client_id"], row["branch_id"], row["direction"],
                               row["mode"], row["stage"], row["assignee_id"], row["prepared_by"],
                               json.dumps(row["data"]), created, created))
            batch_events.append((jid, created, "svc-parser", "created_by_parser", None, stage, dumps(row)))
            if len(batch_jobs) == 20000:
                _flush(conn, batch_jobs, batch_events)
        _flush(conn, batch_jobs, batch_events)
    conn.execute("ANALYZE")
    print(f"  {jobs:,} jobs + events written in {time.time() - t0:.1f}s")
    return conn


def _flush(conn, jobs, events):
    conn.executemany("""INSERT INTO jobs(id,external_ref,client_id,branch_id,direction,mode,stage,assignee_id,
                        prepared_by,data,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", jobs)
    conn.executemany("INSERT INTO job_events(job_id,at,actor_id,action,from_stage,to_stage,snapshot) "
                     "VALUES (?,?,?,?,?,?,?)", events)
    jobs.clear()
    events.clear()


def timed(fn, n=5):
    xs = []
    out = None
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        xs.append((time.perf_counter() - t) * 1000)
    return statistics.median(xs), out


def bench(conn):
    S.reconcile_expired(conn, NOW)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"\nBenchmark on {total:,} jobs / {users} users (median of 5, ms)")
    narrow = conn.execute("SELECT user_id FROM grants WHERE mode IS NOT NULL AND role_id='maker' LIMIT 1").fetchone()[0]
    branchy = conn.execute("SELECT user_id FROM grants WHERE role_id='checker' AND client_id IS NULL "
                           "AND branch_id IS NOT NULL LIMIT 1").fetchone()[0]
    rows = []
    for label, u in [("maker everywhere (priya)", "priya"), ("branch checker", branchy),
                     ("narrow maker", narrow), ("narrow checker (rahul)", "rahul")]:
        ms, ib = timed(lambda: S.inbox(conn, u, NOW, limit=50))
        rows.append((f"inbox page+count: {label}", ms, f"{ib['total']:,} jobs waiting"))
    for label, u in [("auditor sees all (tarun)", "tarun"), ("client user (ramesh)", "ramesh"),
                     ("narrow maker (farah)", "farah")]:
        ms, v = timed(lambda: S.visible_jobs(conn, u, NOW, limit=50))
        rows.append((f"job list page+count: {label}", ms, f"{v['total']:,} visible"))
    ms, v = timed(lambda: S.visible_jobs(conn, "tarun", NOW, {"branch_id": "DEL", "stage": "Prepared"}, limit=50))
    rows.append(("job list filtered (branch+stage)", ms, f"{v['total']:,} rows"))
    mid = f"J{total // 2:06d}"
    job = S.get_job(conn, mid)
    ms, d = timed(lambda: P.decide(conn, "priya", "prepare", job, NOW), n=50)
    rows.append(("single decide()", ms, d.summary[:40] + "..."))
    day = job["created_at"][:10] + "T00:00:00Z"
    ms, w = timed(lambda: S.who_could(conn, mid, day, shift(day, days=1)), n=1)
    rows.append(("who-could, one job, one day, all users", ms, f"{len(w['could_change'])} people could"))
    for label, ms, note in rows:
        print(f"  {label:<45} {ms:>9.1f}   {note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "scale.db"))
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=200_000)
    ap.add_argument("--bench-only", action="store_true")
    a = ap.parse_args()
    if a.bench_only:
        conn = connect(a.db)
    else:
        print(f"Building scale database at {os.path.abspath(a.db)}")
        conn = build(a.db, a.users, a.jobs)
    bench(conn)


if __name__ == "__main__":
    main()
