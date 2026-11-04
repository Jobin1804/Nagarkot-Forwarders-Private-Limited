# Demo: about 5 minutes

**Quickest:** `python demo.py` (or double-click `run_everything.bat` on Windows) plays all of the steps below automatically and prints the results. Record your screen while it runs and talk over it using the table below.

Two terminals. Reset first so the data is fresh (the demo changes one job):

```bash
python -m app.seed                 # terminal 1
uvicorn app.main:app               # terminal 1, leave running
./demo.sh                          # terminal 2: runs the steps below, pausing between each
```

`demo.sh` needs only `curl` and Python. On Windows, run the commands below by hand in Git Bash, or use `/docs` in a browser (Swagger: click "Try it out", set the `X-User` header).

| Time | Show | Say |
|---|---|---|
| 0:00 | `http://localhost:8000/docs` | "One small service: FastAPI + SQLite. Every request says who's acting; every answer explains itself." |
| 0:30 | `python -m app.cli access rahul` and `... access sneha` | "Access is roles over a scope. Rahul checks ACME, Mumbai, imports only. Sneha checks everything at Mumbai." |
| 1:00 | `GET /me/inbox` as Rahul | "His queue has exactly one job, J000002, because that's the only job waiting for a step he's allowed to do. The count is the same rule compiled to SQL; a test checks it against every job for every user." |
| 1:30 | `POST /jobs/J000001/actions/check` as Rahul → **403** | "J000001 is ACME at Mumbai too, but it's an export. Refused, and the refusal carries the reason, check by check." |
| 2:00 | `GET /explain/compare?users=sneha,rahul…` | **"Why can she do this and he can't?"** "Only the differing check is printed: same permission, but his grant is limited to imports." |
| 2:30 | compare priya vs sneha | "Priya is *qualified* to check ACME, but she prepared this checklist. Separation of duties is a rule, not a setting someone can forget." |
| 2:50 | Sneha checks J000001 → 200 | "Sneha can, so she does. That event records which grant allowed it." |
| 3:10 | `python -m app.cli history J000003` | **A job's history.** "Filed on the 2nd; on the 3rd Imran asked to reopen with a reason, Neha approved: two people, both recorded. Kavya corrected it, and it went through maker, checker, filer again. The bottom lines are refused attempts: Priya tried to edit it while it was filed, which is last month's incident, and it simply can't happen now." |
| 4:00 | `python -m app.cli who-could J000003 2026-10-03` | **"Who could have changed this job on the 3rd?"** "Replayed, not guessed: the same decision function, run as of every moment that day. Until 11:00 it was filed, so only Neha could act; after the reopen, Delhi makers could edit. Karan from Mumbai never could, and his attempt is listed." |
| 4:40 | (optional) admin self-grant → 403; `GET /jobs/J000007/eligible` → STUCK; `GET /jobs/J000005` shows the held parser update | "Admins can't promote themselves; when nobody can act the system says so rather than bending a rule; and the overnight parser never overwrites work in progress." |
