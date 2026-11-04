#!/usr/bin/env bash
# Five-minute demo. Start the server first:  python -m app.seed && uvicorn app.main:app
set -u
BASE=${BASE:-http://localhost:8000}
PY=${PY:-python3}
pretty() { $PY -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d, indent=2)[:${1:-1800}])"; }
field() { $PY -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }
step() { echo; echo "=== $1"; [ -n "${NOPAUSE:-}" ] || read -r -p "(enter) " _; }

step "1. The service is running: the permission catalogue"
curl -s "$BASE/permissions" | field "['actions']['check']"

step "2. Who has what, in plain English"
$PY -m app.cli access rahul
$PY -m app.cli access sneha
$PY -m app.cli access priya

step "3. Rahul's queue: jobs waiting for HIM (count + items)"
curl -s -H "X-User: rahul" "$BASE/me/inbox" | $PY -c "import json,sys; d=json.load(sys.stdin); print('total', d['total']); [print(' ', i['id'], i['client_id'], i['branch_id'], i['direction'], i['stage'], '->', i['next_action'], '|', i['why']) for i in d['items']]"

step "4. Rahul tries to check J000001 (ACME / Mumbai / EXPORT)"
curl -s -X POST -H "X-User: rahul" -H "Content-Type: application/json" -d '{}' -w "\nHTTP %{http_code}\n" "$BASE/jobs/J000001/actions/check" | head -c 600; echo

step "5. Why can Sneha do this and Rahul can't?"
curl -s -H "X-User: tarun" "$BASE/explain/compare?users=sneha,rahul&action=check&job=J000001" | field "['headline']"

step "6. And Priya, who IS a qualified ACME checker?"
curl -s -H "X-User: tarun" "$BASE/explain/compare?users=priya,sneha&action=check&job=J000001" | field "['headline']"

step "7. Sneha checks it"
curl -s -X POST -H "X-User: sneha" -H "Content-Type: application/json" -d '{}' -w "\nHTTP %{http_code}\n" "$BASE/jobs/J000001/actions/check" | tail -c 120; echo

step "8. A job's history: J000003 (filed, reopened on the 3rd, re-filed)"
$PY -m app.cli history J000003

step "9. Who could have changed J000003 on 3 Oct 2026?"
$PY -m app.cli who-could J000003 2026-10-03

step "10. Admin tries to make herself more powerful"
curl -s -X POST -H "X-User: anita" -H "Content-Type: application/json" -w "\nHTTP %{http_code}\n" \
  -d '{"user_id":"anita","role_id":"compliance_approver","reason":"I want to approve things too"}' "$BASE/grants"

step "11. A stuck job, and a held parser update"
curl -s -H "X-User: tarun" "$BASE/jobs/J000007/eligible" | field "['note']"
curl -s -H "X-User: tarun" "$BASE/jobs/J000005" | field "['needs_attention']"
echo; echo "Done. Re-run 'python -m app.seed' to reset the data."
