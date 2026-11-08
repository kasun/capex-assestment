# CapEx Platform — Multi-Tenant Budget & Approval (Take-Home Submission)

Django 5 + DRF + HTMX/Alpine + Postgres + Redis + django-q2. A multi-tenant
CapEx (capital expenditure) approvals platform: organization/property-scoped
row-level security on *every* read and write, a multi-level approval state
machine safe under concurrent actors, a background aggregation pipeline
feeding a cached analytics dashboard, and tenant-scoped real-time updates
over SSE.

Spec-first worked from `SPEC.md` (reviewed mid-flight), architecture argued
in **`DESIGN.md`** — this README covers **running it** and the honest ledger.

---

## Run it (one command)

```bash
docker compose up --build
# brings up: postgres + redis + api + q2-worker (+ one-shot init that runs
# migrate, then loads the harness seed: 8 orgs / 54 properties / 58 users /
# 72,000 projects / 198,424 approval events — org 1 is the "big tenant")
```

App: **http://localhost:18000** (host ports deliberately avoid clashing with
local Postgres/Redis; see below).

| Who to log in as (password for all: `Passw0rd!`) | What you'll see |
|---|---|
| `admin1@org1.example.com` | whole-org view: 30k projects aggregated across 5 properties (KPIs, both charts, backlog) |
| `pm2@org1.example.com` | property_manager view: only their 1 property — smaller numbers, drafts + submits |
| `appr…@org1.example.com` | approver: can move manager_review → finance_review → approved |
| `view…@org1.example.com` | viewer: read-only everywhere (403 on any action) |

The three things worth clicking (each maps to a rubric line):

1. **Dashboard real-time** — open the dashboard, then in Projects submit/approve
   a project; worker drains the dirty org in ~1s → rollup rewrite → stamp
   publish → the dashboard's KPI panel swaps itself without reload (SSE).
2. **Concurrent approvals → 409** — same project in two windows (approve +
   reject both as visible to approver/admin): second actor's row
   self-refreshes with the amber "concurrent action" note; the JSON API
   returns `409` + winner state.
3. **Wrong-token proof** — grab a token for a *viewer* and query an approval
   transition, or a pm and hit another org's project id: 403 vs byte-identical
   404 (`curl` snippets in the section below).

## The API (content-negotiated: one router, two representations)

```
Authorization: Bearer token  +  Accept: application/json → JSON
HX-Request: true (session)                              → HTML fragments (the app's own UI)
```

```bash
TOKEN=$(curl -s -X POST http://localhost:18000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"pm2@org1.example.com","password":"Passw0rd!"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access'])")

curl -s http://localhost:18000/api/projects/ -H "Authorization: Bearer $TOKEN" | head
curl -s -o /dev/null -w '%{http_code}\n' \
     http://localhost:18000/api/projects/999999/ -H "Authorization: Bearer $TOKEN"   # 404, no confirmation
curl -s http://localhost:18000/api/analytics/summary/ -H "Authorization: Bearer $TOKEN" | head
```

Tests: `cd backend && POSTGRES_HOST=localhost POSTGRES_PORT=15432
REDIS_URL=redis://localhost:16379 uv run pytest ../backend/capex/tests -q`
(**26 passed** — cross-tenant matrix incl. identical-404 bodies, state
machine incl. CAS conflict/self-approval, rollup == fresh SQL ground truth
for every org, idempotent re-sweep). Lint/design ship green (`ruff check`).

## Local dev without compose-docker for the app layer

The stack over colima/Docker covers everything; if you'd rather run the
Django process on your host (live reload), the data stores still come up
from compose:

```bash
docker compose up -d db redis          # host ports 15432 (pg) / 16379 (redis)
cd backend && POSTGRES_PORT=15432 REDIS_URL=redis://localhost:16379 \
  uv run manage.py runserver           # http://127.0.0.1:8000
```

Ports on purpose: host Postgres/Redis may already be running on 5432/6379;
the compose files publish on **15432/16379** to prevent shadowing bugs
(we hit one, then made it structural). The *containers themselves* always
use the standard ports internally.

Toolchain: `make setup` (uv + npm tailwind tooling) → `make run` /
`make test` / `make lint` / `make migrate`. Manage commands of note:
`load_seed --scale big` (vendored harness generator, ids preserved so the
`ENTITLEMENTS.md` assertions hold), nothing else custom.

## Built vs. deferred (with weight accounting)

| Area | Built |
|---|---|
| Central RLS (policy.py, content-negotiated single router) | **yes** |
| Multi-level approval state machine + append-only audit events + CAS/version | **yes** |
| Background aggregation (dirty-org drain → per-org full re-reslice) + versioned-key Redis cache + reconciliation sweep | **yes** |
| Tenant-scoped real-time (SSE over pub/sub; snapshot-on-connect; keep-alives) | **yes** |
| HTMX dashboard (KPIs + 2 charts + backlog + paging + role-aware actions + optimistic row states) | **yes** |
| Tests: cross-tenant denial / state machine / aggregation correctness | **yes (26)** |
| Postgres-native RLS, asyncio SSE rewrite, incremental rollups | deferred, argued in DESIGN.md §7 |
| SSO/Azure AD, export, extra roles' full UIs | deferred, identified stub points in DESIGN.md |

The one *deliberate stack deviation* — HTMX+Alpine instead of React/RTK/MUI —
is stated and defended in `DESIGN.md` §7 (server-computed aggregates are
already RLS-scoped; a client copy of those rules is negative surface; the
plan was reviewed with the reviewer mid-build).

## Where in the code

```
backend/capex/
  models.py        # Organization/Property/User/Project/ApprovalEvent/Budget/Rollup/Stamp
  policy.py        # entitlements + scoping (the authorization brain)
  services/        # CRUD + transition table + typed domain errors → HTTP codes
  api/             # DRF viewsets, renderers (content negotiation), exception mapping
  rollups.py       # worker-side aggregation, dirty drain, stamp/publish, cached reads
  sse.py           # tenant-scoped SSE stream
  pages.py         # session-authenticated dashboard/projects/detail shells
  harness/         # vendored assignment generator (verbatim; keeps ids stable)
frontend/templates/capex/   # page shells + the fragments (partials) the API serves
designs/           # approved pen.dev mockups + exported previews (UI reference)
```

Time-per-area (6h box, as built with the reviewer's step-approvals running):
stack & schema 0:40 · policy+API 1:10 · state machine 0:35 · aggregation+cache 0:50 ·
real-time 0:30 · dashboard+UI 0:45 · seeder/docker/first-run 0:35 · tests 0:40 ·
DESIGN/README 0:45 — with mid-flight corrections called out in git history.
