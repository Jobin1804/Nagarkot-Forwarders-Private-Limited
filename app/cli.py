"""Command-line helpers (read-only).

    python -m app.cli access                 # everyone's access in plain English
    python -m app.cli access rahul
    python -m app.cli compare sneha rahul check J000001
    python -m app.cli explain priya check J000001
    python -m app.cli history J000003
    python -m app.cli who-could J000003 2026-10-03
    python -m app.cli inbox kavya
"""
from __future__ import annotations

import argparse
import json

from . import service as S
from .db import connect, norm_ts, shift, utcnow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--at", default=None, help="evaluate as of this time (default now)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("access"); a.add_argument("user", nargs="?")
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b"); c.add_argument("action"); c.add_argument("job")
    e = sub.add_parser("explain"); e.add_argument("user"); e.add_argument("action"); e.add_argument("job")
    h = sub.add_parser("history"); h.add_argument("job")
    w = sub.add_parser("who-could"); w.add_argument("job"); w.add_argument("date")
    i = sub.add_parser("inbox"); i.add_argument("user")
    args = ap.parse_args()
    conn = connect(args.db)
    now = norm_ts(args.at) if args.at else utcnow()

    if args.cmd == "access":
        users = [args.user] if args.user else [r[0] for r in conn.execute("SELECT id FROM users ORDER BY rowid")]
        for u in users:
            d = S.describe_access(conn, u, now)
            for line in d["plain_english"]:
                print("- " + line)
            for x in d["upcoming"]:
                print(f"    - upcoming: {x}")
            for x in d["pending_approval"]:
                print(f"    - pending: {x}")
            for x in d["past"]:
                print(f"    - no longer: {x}")
            for ab in d["absences"]:
                dl = f", delegate {ab['delegate_id']}" if ab["delegate_id"] else ", no delegate"
                print(f"    - absent {ab['starts_at']} to {ab['ends_at']}{dl}")
    elif args.cmd == "compare":
        r = S.compare(conn, args.a, args.b, args.action, args.job, now)
        print(r["headline"])
    elif args.cmd == "explain":
        d = S.explain(conn, args.user, args.action, args.job, now)
        print(d["summary"])
        for ch in d["checks"]:
            print(f"  [{'PASS' if ch['passed'] else 'FAIL'}] {ch['check']}: {ch['detail']}")
        for n in d["notes"]:
            print(f"  note: {n}")
    elif args.cmd == "history":
        h = S.job_history(conn, args.job)
        j = h["job"]
        print(f"{j['id']} {j['client_id']}/{j['branch_id']}/{j['direction']}/{j['mode']} now {j['stage']}")
        lines = []
        for ev in h["events"]:
            auth = ev["authority"]
            via = ""
            if auth.get("approved_by"):
                via = f" [requested by {auth['requested_by']}, approved by {auth['approved_by']}]"
            elif auth.get("grants"):
                via = f" [via {auth['grants'][0].split(' (from')[0]}]"
            lines.append((ev["at"], f"  {ev['at'][:16].replace('T', ' ')}  {ev['actor_id']:<10} {ev['action']:<18} "
                                    f"{(ev['from_stage'] or '-'):>9} -> {ev['to_stage']:<9} {ev['reason'] or ''}{via}"))
        for dn in h["denied_attempts"]:
            lines.append((dn["at"], f"  {dn['at'][:16].replace('T', ' ')}  REFUSED  {dn['summary'][8:]}"))
        for _, line in sorted(lines, key=lambda x: x[0]):
            print(line)
    elif args.cmd == "who-could":
        s = norm_ts(args.date)
        r = S.who_could(conn, args.job, s, shift(s, days=1))
        print(f"Who could have changed {args.job} on {args.date}:")
        for c in r["could_change"]:
            acts = "; ".join(f"{a} {', '.join(w[0][11:16] + '-' + w[1][11:16] for w in ws)}"
                             for a, ws in c["actions"].items())
            print(f"  {c['name']:<16} {acts}")
        print("What actually happened:")
        for e in r["actually_changed"]:
            print(f"  {e['at'][11:16]} {e['actor_id']:<8} {e['action']}")
        print("Refused attempts:")
        for d in r["denied_attempts"]:
            print(f"  {d['at'][11:16]} {d['summary']}")
    elif args.cmd == "inbox":
        r = S.inbox(conn, args.user, now, limit=20)
        print(f"{args.user}: {r['total']} jobs waiting")
        for it in r["items"]:
            print(f"  {it['id']} {it['client_id']}/{it['branch_id']}/{it['direction']}/{it['mode']} "
                  f"{it['stage']:<10} -> {it['next_action']:<8} {it['why']}")
    else:
        print(json.dumps({}, indent=2))


if __name__ == "__main__":
    main()
