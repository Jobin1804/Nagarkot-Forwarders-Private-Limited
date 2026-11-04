# NFPL Access Control Service

A service that decides **who may do what on which job**, explains every answer, shows each person the jobs waiting for them, and keeps a history that can answer "who could have changed this job on the 3rd?".

- **Design document:** [`DESIGN.md`](DESIGN.md) (also as [`DESIGN.pdf`](DESIGN.pdf), 3 pages): the model, trade-offs, and answers to all 14 questions plus four added ones.
- **Demo script:** [`DEMO.md`](DEMO.md): a 5-minute walkthrough, with commands.
- **Stack:** Python 3.10+, FastAPI, SQLite. No other infrastructure.

| Brief's deliverable | Where |
|---|---|
| 1. Working code | `app/` (run it with the Quick start below) |
| 2. Design document | `DESIGN.md` / `DESIGN.pdf` |
| 3. Sample data, one command | `python -m app.seed`; everyone's access is listed below |
| 4. Tests | `tests/` (54 tests: maker/checker, absent owner, changed responsibility, filed cancel/reopen, admin self-escalation, parser, inbox parity, history) |
| 5. Demo | `demo.py` (runs every scenario), `DEMO.md` (5-minute talk track), `demo.sh` |

## Quick start

**Windows, easiest:** double-click **`run_everything.bat`**. It sets up a private Python environment, loads the sample data, runs all 54 tests, and plays every scenario (`demo.py`), printing what happened and whether it was right. Then double-click **`start_server.bat`** to open the API in your browser.

**Any OS, by hand:**

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m app.seed                                     # load the sample data (one command)
uvicorn app.main:app --reload                          # http://localhost:8000/docs
pytest -q                                              # 54 tests
```

Every request names the acting user in an `X-User` header (a stand-in for the company SSO; see DESIGN.md §5):

```bash
curl -H "X-User: rahul" localhost:8000/me/inbox
curl -H "X-User: tarun" "localhost:8000/explain/compare?users=sneha,rahul&action=check&job=J000001"
curl -H "X-User: tarun" "localhost:8000/jobs/J000003/who-could?date=2026-10-03"
```

Or play every scenario automatically, with no server (uses its own copy of the data):

```bash
python demo.py                               # 11 scenarios, 16 checks, prints OK / UNEXPECTED
```

The same questions from the command line (read-only, no server needed):

```bash
python -m app.cli access                     # everyone's access in plain English
python -m app.cli compare sneha rahul check J000001
python -m app.cli history J000003
python -m app.cli who-could J000003 2026-10-03
python -m app.cli inbox kavya
```

A `Makefile` wraps these (`make install seed run test scale demo`).

### Scale case (500 users, 200,000 jobs)

```bash
python -m app.scale                          # builds data/scale.db (~10 s) and prints timings
NFPL_DB=data/scale.db uvicorn app.main:app   # serve it
```

Measured on a laptop-class machine:

| Operation (200,000 jobs, 500 users) | Median |
|---|---|
| Inbox page + exact count, maker for every client (35,389 jobs waiting) | ~35 ms |
| Inbox page + exact count, branch checker / narrow user | ~25 ms / <1 ms |
| Job list page + count, auditor who sees all 200,000 | ~35 ms |
| Single `decide()` | ~0.1 ms |
| Who could change one job on one day (all 500 users replayed) | ~150 ms |

## Project layout

```
app/policy.py     the rules: permission catalogue, actions, decide(), SQL compilation of the same rules
app/service.py    operations: workflow, grants, roles, absences, parser, inbox, history, who-could
app/main.py       HTTP API (FastAPI; interactive docs at /docs)
app/db.py         schema and helpers (SQLite; append-only history)
app/seed.py       sample data, built by driving the real service along a dated story
app/scale.py      scale generator + benchmark
app/cli.py        read-only command-line helpers
tests/            rule edge cases, inbox/list parity, history, API
```

## API

| Method & path | What it does |
|---|---|
| `GET /permissions` | The permission catalogue and each action's rules |
| `GET /me/inbox` | Jobs waiting for me (what I can do next, routed to me) with an exact count |
| `GET /me/approvals` | Cancel-filed / reopen requests I may approve |
| `GET /me/access`, `GET /users/{id}/access` | Access in plain English: current, upcoming, pending, past |
| `GET /jobs`, `GET /jobs/{id}` | Jobs I may see (client users get a redacted view) |
| `POST /jobs`, `PATCH /jobs/{id}` | Open a job; edit data (moving scope needs authority on both sides) |
| `POST /jobs/{id}/actions/{action}` | `prepare`, `check`, `send_back`, `file`, `cancel`, `assign`, `admin_fix` |
| `POST /jobs/{id}/claim` | Pick a job up from the pool or from an absent colleague |
| `GET /jobs/{id}/eligible` | Who could do the next step right now (empty = STUCK) |
| `POST /jobs/{id}/requests` | Ask to cancel a filed job or reopen a closed one (reason required) |
| `POST /requests/{id}/approve` / `reject` | Second person decides (with a note) |
| `GET /jobs/{id}/history` | Every event, the authority behind it, and refused attempts |
| `GET /jobs/{id}/who-could?date=` | Who could have changed it, when, via which grant; what happened; what was refused |
| `GET /explain?user=&action=&job=` | One decision, check by check |
| `GET /explain/compare?users=a,b&action=&job=` | "Why can she and he can't?": only the differing checks |
| `POST /grants`, `POST /grants/{id}/approve`, `/reject`, `/revoke` | Manage access (four-eyes for privileged roles) |
| `POST /roles`, `PUT /roles/{id}`, `POST /roles/{id}/versions/{v}/approve` | New / changed roles as data |
| `POST /absences`, `POST /absences/{id}/end` | Absence with optional delegate |
| `POST /parser/sync` | Overnight feed (service account only) |
| `GET /audit`, `GET /grants` | Access-change audit trail (needs `access.read`) |

Denied actions return **403 with the full decision**; jobs you may not see return **404**.

## Roles in the sample data

Roles are data (`GET /roles`); these are the ones the seed creates. Privileged roles need a second person to approve each grant.

| Role | Permissions | Notes |
|---|---|---|
| Maker | view, create, edit, prepare | |
| Checker | view, check (incl. send back) | may not check what they prepared |
| Filer | view, file | may not file what they prepared |
| Operations Manager | view, edit, cancel (unfiled), assign, request cancel-filed, request reopen, record absences | |
| Compliance Approver | view, approve sensitive requests, read audit | privileged |
| Administrator | read/grant/manage roles/absences, view, admin_fix | privileged; no customs work |
| Branch Access Admin | view, grant (within own scope) | privileged |
| Auditor | view, read audit | read-only |
| Client Viewer | client-safe view | client accounts only |
| Overnight Parser | sync | service account only |

## Sample data: each person's access

`python -m app.seed` creates 5 branches (Mumbai BOM, Delhi DEL, Chennai MAA, Bengaluru BLR, Kolkata CCU), 10 clients, 23 accounts and ~315 jobs across every direction, mode and stage, with about 950 history events from October 2026 onward. Everything after the initial two administrators is created **through the real service**, so all history is genuine. The output of `python -m app.cli access` is the authoritative version of this list.

- **Anita Rao** (`anita`, Head of IT Operations) has role **Administrator** for all clients, all branches, all directions, all modes.
- **Vikram Shah** (`vikram`, IT Administrator) has role **Administrator** for all clients, all branches, all directions, all modes.
- **Priya Nair** (`priya`, Senior Executive) has role **Maker** for all clients, all branches, all directions, all modes; and role **Checker** for client ACME Textiles, all branches, all directions, all modes; and role **Checker** for client Bharat Pharma, all branches, all directions, all modes.
- **Rahul Mehta** (`rahul`, Executive) has role **Checker** for client ACME Textiles, branch Mumbai, imports only, all modes.
- **Sneha Iyer** (`sneha`, Team Lead) has role **Checker** for all clients, branch Mumbai, all directions, all modes.
- **Arjun Das** (`arjun`, Executive) has role **Maker** for all clients, branch Delhi, all directions, all modes.
- **Kavya Menon** (`kavya`, Executive) has role **Maker** for all clients, branch Delhi, all directions, all modes; and role **Checker** for client Bharat Pharma, branch Delhi, all directions, all modes *(temporary: ends 12 days after the seed date)*.
- **Manoj Kumar** (`manoj`, Executive) has role **Maker** for all clients, branch Chennai, all directions, all modes.
- **Pooja Sharma** (`pooja`, Team Lead) has role **Checker** for all clients, branch Delhi, all directions, all modes; and role **Checker** for all clients, branch Chennai, all directions, all modes.
- **Deepa Pillai** (`deepa`, Filing Officer) has role **Filer** for all clients, branch Mumbai, all directions, all modes; and role **Filer** for all clients, branch Delhi, all directions, all modes.
- **Suresh Gupta** (`suresh`, Filing Officer) has role **Filer** for all clients, branch Chennai, all directions, all modes; and role **Filer** for all clients, branch Bengaluru, all directions, all modes; and role **Filer** for all clients, branch Kolkata, all directions, all modes.
- **Meera Joshi** (`meera`, Branch Manager, Mumbai) has role **Operations Manager** for all clients, branch Mumbai, all directions, all modes; and role **Branch Access Admin** for all clients, branch Mumbai, all directions, all modes.
- **Imran Sheikh** (`imran`, Branch Manager, Delhi) has role **Operations Manager** for all clients, branch Delhi, all directions, all modes.
- **Rohan Bose** (`rohan`, Branch In-charge, Kolkata) has role **Maker** for all clients, branch Kolkata, all directions, all modes; and role **Checker** for all clients, branch Kolkata, all directions, all modes; and role **Filer** for all clients, branch Kolkata, all directions, all modes.
- **Neha Kapoor** (`neha`, Head of Compliance) has role **Compliance Approver** for all clients, all branches, all directions, all modes.
- **Amit Verma** (`amit`, Air Specialist) has role **Checker** for client Coastal Spices, all branches, all directions, AIR only; and role **Checker** for client Deccan Motors, all branches, all directions, AIR only.
- **Farah Khan** (`farah`, Executive) has role **Maker** for client Everest Electronics, branch Bengaluru, exports only, all modes.
- **Tarun Reddy** (`tarun`, Director (read-only)) has role **Auditor** for all clients, all branches, all directions, all modes.
- **Lakshmi S** (`lakshmi`, Executive) has role **Maker** for all clients, branch Bengaluru, all directions, all modes; and role **Checker** for all clients, branch Bengaluru, all directions, all modes.
- **Gopal Rao** (`gopal`, Executive) has role **Maker** for all clients, branch Chennai, all directions, all modes; and role **Checker** for all clients, branch Chennai, all directions, all modes *(revoked 20 Oct 2026)*; and role **Checker** for all clients, branch Bengaluru, all directions, all modes *(future-dated: starts 30 days after the seed date)*.
- **Karan Malhotra** (`karan`, Executive (left 15 Oct)) has role **Maker** for all clients, branch Mumbai, all directions, all modes.
- **Ramesh Patel** (`ramesh`, Logistics Manager at ACME Textiles) has role **Client Viewer** for client ACME Textiles, all branches, all directions, all modes.
- **Overnight Parser** (`svc-parser`, Service account) has role **Overnight Parser** for all clients, all branches, all directions, all modes.

### The awkward cases, on purpose

| Case from the brief | Who |
|---|---|
| Maker everywhere, Checker for only a few clients | **Priya** (checks only ACME and Bharat) |
| Checker for one client, one branch, imports only | **Rahul** (ACME / Mumbai / import) |
| Absent user, with a delegate | **Arjun**: on leave from 2 days before the seed date for 2 weeks; delegate **Kavya**, who also has a temporary Bharat checker grant |
| Absent user, no delegate | **Manoj**: sick leave; his assigned jobs return to the Chennai pool |
| Client-side user | **Ramesh** (ACME Textiles): sees only ACME jobs, redacted, can't act |
| Admins | **Anita** and **Vikram**: each needs the other for privileged changes |
| Overnight parser | **svc-parser**: created every job; latest run includes a held update and a rejected item |
| Changed responsibility | **Gopal**: Chennai checker until 20 Oct (revoked; his queued jobs were released), Bengaluru checker from next month (future-dated) |
| Left the company | **Karan**: deactivated 15 Oct; history about him still works |
| One-person branch | **Rohan** (Kolkata) is maker, checker and filer, so his own jobs need someone else |
| Scoped access administrator | **Meera** can grant access, but only inside Mumbai |

### Named jobs for the demo

| Job | Client / branch / direction / mode | State and why it's interesting |
|---|---|---|
| J000001 | ACME / BOM / export / air | Prepared by Priya. Sneha can check; Rahul can't (export); Priya can't (she prepared it) |
| J000002 | ACME / BOM / import / FCL | Prepared by Priya. Rahul *can* check this one |
| J000003 | Bharat / DEL / import / air | Filed 2 Oct, reopened on **3 Oct** via request (Imran) + approval (Neha), corrected, re-filed 4 Oct; four refused attempts on the 3rd. The history demo |
| J000004 | Coastal / BOM / export / air | Filed; cancellation requested by Meera, waiting in Neha's approvals |
| J000005 | Jaipur / BLR / export / LCL | Prepared; last night's parser update was **held** (`needs_attention`) |
| J000006 | Everest / BLR / export / LCL | Farah's narrow slice; updated by the parser while Confirmed |
| J000007 | Ganga / CCU / import / FCL | Prepared by Rohan, the only Kolkata checker, so it's **STUCK** (`/jobs/J000007/eligible`) |

## Assumptions

Listed in full in DESIGN.md §5. The main ones: the stage flow is Confirmed → Prepared → Checked → Filed, with Cancelled and Reopened; a checker can send a checklist back; the filer must not be the preparer; times are UTC; identity comes from SSO (simulated by `X-User`).
