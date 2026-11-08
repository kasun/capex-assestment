# SPEC — Multi-Tenant CapEx Budget & Approval Platform

> Status: **DRAFT — awaiting sign-off.** Source: `instructions/ASSIGNMENT.md` + harness.
> This spec governs the submission; see `DESIGN.md` (to be written) for scale/tradeoff prose,
> which this file deliberately avoids duplicating.

## 1. Problem restated (own words)

One deployment, many companies (orgs = tenants). Users belong to exactly one org and are
granted **specific properties** inside it. Tenancy is **two-level**: (a) no org ever sees
another org's rows; (b) non-admins never see other properties *within their own org* that
aren't in their grant. Users submit CapEx projects that must pass a **multi-level approval
state machine** (submitted → manager_review → finance_review → approved, or rejected at any
point — no skipping, no out-of-turn, safe under concurrent actors). Leadership consumes an
**analytics dashboard** (budget vs actuals, variance %, spend by category, pending-approval
backlog, avg approval cycle time) that must be **fast because it reads precomputed
aggregates**, never a live GROUP BY over millions of rows. Approval changes propagate
**near-real-time** to the right org's dashboards only. The graded hard parts: **central
row-level security**, **precomputed analytics**, **tenant-scoped real-time**. Any
cross-tenant leak is an automatic fail.

## 2. Stack (decided, with the one deliberate deviation)

- **Backend**: Django 5 + **DRF** as the **single API surface** — every project/approval/
  analytics endpoint is a DRF viewset that **content-negotiates**: `JSON` (JWT, `Accept:
  application/json` — programmatic/evaluator clients) or an **HTML fragment** (`HX-Request:`
  header, session auth — the HTMX UI's own exchanges, via `TemplateHTMLRenderer`). Same
  URLs, same policy, same serializer dict. Tailwind for styling; Chart.js vendored for the
  two charts; Alpine for client-local UI state.
- **DB**: **Postgres** (docker compose, from scaffold zero — no SQLite path built).
- **Redis**: worker broker + rollup cache + SSE pub/sub. **django-q2**: aggregation jobs
  (async + beat scheduler).
- **Real-time**: **SSE over Redis pub/sub**, consumed by HTMX's `sse` extension. Channels
  dropped (unidirectional push fits the fragment-swap client; SSE explicitly allowed with
  justification → `DESIGN.md` §4).
- **Containers**: `docker compose up` → `db` (postgres), `redis`, `api` (gunicorn+uvicorn
  worker — the SSE view is async, the rest sync), `worker` (q2 cluster), `migrate/seed`
  one-shot init. No `frontend` service (no JS build step; Django serves assets).
- **The deviation, stated up front**: the assignment lists React+RTK+MUI as Blue Ocean's
  stack. We build the UI in HTMX+Alpine and defend it in `DESIGN.md` §2/§5 — the dashboard
  is *server-computed aggregates*; a client-side re-derivation of the same role/scope rules
  RLS already enforces is negative-value, and the reclaimed effort is reinvested where the
  rubric weights 25/20/10 (authz, aggregation, tests/design). Familiarity is *mentioned*
  honestly but is not the framed argument.

## 3. Submission layout

```
/
├── README.md            # setup/run (~1 de command), built vs deferred, time per area
├── DESIGN.md            # Part B
├── SPEC.md              # this file (submitted as statement of intent/assumptions)
├── docker-compose.yml   # db, redis, api, worker, init
├── backend/             # manage.py, config/, apps: capex/ (models, api, policy, rollups, sse, management commands)
├── frontend/            # templates/, static/ (vendored htmx+alpine+chart.js, tailwind build) — the HTMX UI
├── instructions/        # unchanged assignment source (excluded from submission zip)
├── designs/             # capex-ui.pen (canvas) + mockups/*.html (token/theme source) + exports/capex-*.png (approved UI reference)
└── Makefile / uv.lock / package.json
```

The current demo `core/` app is deleted in the same change that adds `capex/` (per repo
AGENTS.md). Repo config (`config/`, Makefile, uv) remains the driver env; `backend/` holds
the Django project itself.

## 4. Data model

All ids are integers to match the harness JSON; `org_id`/`property_id` are **denormalized
onto project rows** (and approval events) so no join is *ever* needed to scope them — cheap
index scans, and the rollup/invalidation path never trusts a join to be correct.

```
Organization(id, name, slug)
Property(id, org FK, name, code)
User(AbstractUser)(email UNIQUE, org FK, role ∈ {org_admin, property_manager, approver, viewer},
                  property_scope m2m → Property)          # org_admin ⇒ scope = whole org, resolved
Project(id, org_id, property_id FK, title, category, fiscal_period, budget_amount,
        actual_cost, status, current_level, version int, submitted_by FK→User,
        created_at/updated_at)
ApprovalEvent(id, project FK, org_id, property_id, level, to_status, by_user, at)
   # append-only; the seeder's history is loaded into these — cycle time is computed here
Budget(id, org FK, property FK, fiscal_period, allocated_amount, UNIQUE(property, fiscal_period))
ProjectRollup(id, org_id, property_id, fiscal_period, category,
              project_count, budget_sum, actual_sum,
              status_last_count, status_pending_count,      # submitted/manager/finance_review
              cycle_time_seconds_sum, decided_count)        # approval throughput inputs
   # grain: (org, property, fiscal_period, category); UNIQUE on the grain
DashboardStamp(org_id PK, updated_at, version)              # for cache/JS freshness signals
```

Indexes (the DESIGN.md §6 index note): `Project(org_id, property_id)`, `Project(org_id,
property_id, fiscal_period, category)`, `Project(org_id, status)` partial on pending + the
rollup UNIQUE grain. Fiscal periods: `FY2024..FY2026` (harness constants).

**Auth**: one `User` table serves both session (HTMX UI) and JWT (API). Passwords from the
harness (plaintext) are hashed at load time with Django's hasher. Seeded password
`Passw0rd!`, login form for UI, `/api/auth/token/` for API.

## 5. Entitlements + central RLS (the crux)

**One module, `capex/policy.py`**, is the single brain:

```python
@dataclass Entitlements: org_id, property_ids (frozenset | ALL), role, is_read_only

def resolve(user) → Entitlements     # DB hit once per request, cached on request
def scoped(qs, ent) → queryset       # apply the org/property predicate (never trusts joins)
def can_write(ent) / can_act(ent)    # role predicates
```

Layered enforcement, in order:

1. **One router, one enforcement point**: every graded surface (`/api/projects/…`,
   `…/transition/`, `/api/analytics/summary/`) is a DRF viewset inheriting
   `ScopedModelView` — its `get_queryset()` is `policy.scoped(base_queryset,
   entitlements)`, plus an object-level permission that turns a foreign-id hit into
   **404**. No view in the codebase may construct a `Project.objects` queryset by hand
   (ruff rule, code-review note).
2. **The app itself is an API client**: the HTMX UI exchanges with these **same URLs** —
   content negotiation selects an HTML fragment renderer instead of JSON, but permission,
   queryset, and serializer output are identical. There is no second route table, so the
   ENTITLEMENTS.md matrix runs against the exact endpoints the running UI exercises; the
   "graded API is not the API the app uses" critique is structurally impossible rather
   than argued away.
3. **Auth layering**: `JWTAuthentication` and `SessionAuthentication` both resolve
   `request.user`; policy doesn't know which one brought it. **JWT is identity-only**
   anyway — role/scope in tokens is never trusted for authz; the DB is queried per
   request. Explicitly stated because "we'll hit the API with the wrong user's token"
   implies they may also try a forged-claims token.
4. **403 vs 404 (defended)**: detail/write on a guessed foreign id → **404** (existence
   is data we do not confirm). An *authorized-for-org but wrong-role* action on a visible
   object (viewer attempting approval) → **403**. Workspaceless list endpoints simply
   return scoped results (never leak by omission).
5. **Analytics scoped the same way**: rollup reads filtered by the caller's property set;
   a property_manager's KPI cards sum only their properties — that assertion is in the
   tests too (aggregates themselves are a leak channel).

## 6. Approval state machine

- Transition table (single source, declarative):

| from → to     | required role (any of)         |
|---------------|--------------------------------|
| draft → submitted | property_manager, org_admin of that property |
| manager_review → finance_review | approver, org_admin |
| finance_review → approved | approver, org_admin |
| any active → rejected | approver/org_admin at that level |
| draft → *edit/deletion* | property_manager, org_admin |

- **No skipping/out-of-turn**: enforced by the table — an action not in the table returns
  **422** with reason "transition not allowed at current level".
- **Concurrency**: `Project(version)` + `UPDATE ... WHERE version = $1` (compare-and-swap
  on F expressions). Two approvers acting at once: first writer wins, second gets **409**
  with the fresh state → the UI reconciles. Choice of CAS over `select_for_update`
  defended in DESIGN.md (no transaction-held-during-HTTP; plays well with q2 workers
  reading the same rows).
- **Approving own submission**: a user cannot approve a level on a project they submitted
  past the level at which their role is allowed — enforced by comparing `submitted_by` at
  the transition table check. One real workflow path is all the assignment demands; we
  build the table generally but test the seed'd path (manager → finance → approved).
- Every accepted transition appends an `ApprovalEvent` and marks the rollup dirty.

**Writes never recompute anything inline** — they flip a dirty bit; the request path stays
fast and correctness rides on the worker.

## 7. Analytics: precompute + cache (the biggest weight area)

**Rollups** (§4 table) computed by `capex/aggregation.py`, always org-watched via
`Entitlements` scoping so a property_manager's read filters rollup rows the same way.

- **The refresh strategy (defended)**: centralized —
  - **Dirty-tick, not on-write-recompute**: writes/transition mark `(org_id ∈ scope)` into
    a Redis dirty-set after the DB commit; django-q2 workers **debounce** (drain on a
    ~1s tick),
    recompute **incrementally only the affected rows** (per property/category deltas using
    projects whose `updated_at` changed since last full sweep), and UPSERT into rollups.
    This avoids re-aggregating 40k-row tenants on every keystroke.
  - **Reconciliation sweep** (q2 scheduler, e.g. every 5 min): full recompute of org 1
    (the big tenant) and any org whose last sweep is older than N — catches drift from
    any bug in incremental delta math.
  - **The API request path never computes**: cached rollup reads (per-entitlement key,
    TTL 60s) served by Redis; a rollup change PUBLISHes a "dashboard dirty" signal → SSE
    (§8) nudges affected org's UI to refetch — **even the cached read is a warm path**.
- **Staleness contract stated in DESIGN.md**: p99 ≲ 2s from write to dashboard-visible for
  self-actions (sub-second once the worker drains; the 2s is the debounce+refresh window),
  ≈60s fixed staleness for *others'-writes* in the worst case (SSE dropped and the
  heartbeat-sync took over),
  and the numbers behind it (Variance %, cycle time) computed to the same idempotent SQL
  as ground truth (tested below).
- `actual_cost` is authoritative as seeded; the seeder's approval history is loaded into
  ApprovalEvents — cycle time = per-project submission→t principal decision deltas,
  averaged per scope in the rollup.

## 8. Real-time (SSE)

- `GET /sse/dashboard/` — auth'd with the **session** (org resolved server-side; the
  subscription is to `capex:org:{org_id}:dirty` **only** — scoping at subscribe time, not
  per-event filtering). Cross-org events are unreachable by construction (test proves it
  at the channel layer).
- After each successful rollup UPSERT, the worker PUBLISHes `{"org": id, "ver": stamp}`.
- HTMX: `hx-ext="sse"`, sse-swap on the KPI area and pending-approvals region only —
  **not** the full page — with `hx-trigger="every 30s"` on a hidden heartbeat sync as the
  reconnect/fallback insurance.
- Evidence boundary stated: SSE keeps one Redis subscription per api-process per
  connection; DESIGN.md §4 has the 10k-sockets fan-out math, reconnect-storm damping
  (jittered backoff), and the honest note about connection limits per pod → scale story.

## 9. Endpoint map (single router; content negotiated per request)

| Endpoint | Responses | Notes |
|---|---|---|
| `POST /api/auth/token/` | JSON (JWT issue) — `AllowAny`, rate-limited | SimpleJWT obtain-pair; credentials = seeded user email + password |
| `POST /api/auth/token/refresh/` | JSON (rotating refresh) | UI never touches JWTs; session-authed |
| `POST /accounts/login/` (session) | HTML | django.auth login for the UI path; HTMX exchanges ride the cookie |
| `GET /api/projects/` , `/{id}` , `POST`/`PATCH` | JSON + `HX-Request:` fragment (list rows / detail card) | scoped everywhere; foreign detail = 404 both formats |
| `POST /api/projects/{id}/transition/` | JSON, or row fragment on `HX-Request` | role+version checks → 422/409/403; 409 re-renders fresh row |
| `GET /api/analytics/summary/` | JSON, or KPI fragment on `HX-Request` | rollup slice + categories/backlog/cycle-time; 60s Redis cache |
| **Full pages** (session): `GET /` dashboard (KPIs + charts + backlog), `GET /projects/`, `GET /projects/{id}/` | HTML (Django) | these are the app shell — not graded API; all *data* inside is XHR'd from the table above |
| `GET /sse/dashboard/` | SSE stream | session; one Redis subscription per connection |

Fragments contain **only the swapped node** (partials never extend base). Row-level
approval actions target `closest <article>`; KPI area is list-level (both granularities
shown, mirroring the repo's task pattern).

## 10. Harness integration

- `capex/management/commands/load_seed.py`: reads harness JSON dir; **bulk_creates**
  everything (~48k projects at `--scale big` in <2 min); hashes passwords; preserves ids
  (so `ENTITLEMENTS.md` assertions hold verbatim); loading is idempotent per-id
  (truncate + reload for the slice — stated as an assumption).
- Generated once into `seed_data/` (not committed); `compose` init runs
  `migrate && load_seed --scale big` on an empty DB. `/api/*` treats big-tenant org 1 as
  the perf story.

## 11. Tests (pytest; rubric targets only)

1. **Cross-tenant denial matrix** — for *each* user fixture in a mini-seeded DB, with
   **both content types on the same endpoints** (JWT+`Accept: application/json` and
   session+`HX-Request:`, parametrized):
   - detail/list/analytics: other-org project → 404; same-org out-of-scope property →
     404; analytics sums exclude foreign rows (baked from `ENTITLEMENTS.md`).
   - asserts that the fragment response contains no foreign row either (presentation is
     the same permission path, but we prove it).
2. **State machine**: invalid skips → 422; wrong role → 403; viewer can't act → 403;
   concurrent CAS: `transaction.on_commit` + delayed
   second POST may race — bounded by single-writer version test.
3. **Aggregation correctness**: for every org: rollup == freshly computed ground truth
   (SQL mirror) for full scales incl. 10k+ in one property; and **incremental == full**
   after randomized write bursts.
4. **RLS in write paths**: transition attempt on foreign id by enumerated wrong users →
   404/403 distinction clear.
5. **SSE scoping** (channel-layer level test): org A subscriber receives org-A event,
   never org B's.
6. Plus repo hygiene tests (`client.get("/", HTMX=true)` gives fragment-only exchange).

## 12. Explicitly deferred (for README/DESIGN honesty)

- Real SSO/Azure AD (stubbed + discussed), export & file uploads, multi-org-per-user,
  per-row audit-log UI (bonus if time), Postgres-native RLS escalation prose, live load
  benchmark of 10k sockets (reasoned in DESIGN.md, not measured), per-user OAuth flows,
  i18n/mobile/responsive audit beyond dashboard-fit Tablet breakpoint.

## 13. Assumptions (stated per ground rules)

- A project has exactly one approval *chain* (no per-org configurable chain lengths);
  3 stages as the harness fix dicts.
- `budgets.allocated_amount` is authoritative for the "budget" rollup input; variance is
  actuals vs per-property fiscal budget **and** per-project budget_amount both meaningful
  — the dashboard uses per-property rollups for budget-vs-actual tiles and per-project
  variance in lists (both, clearly labeled; the ambiguity is stated not hidden).
- One user = one org (harness guarantees it; noted so nothing assumes otherwise silently).
- JWT and session are both accepted by the same viewsets (auth layering, §5.3); the
  grader may pick either — there is exactly one router, so both paths literally cannot
  diverge in enforcement.

## 14. Time budget (as submission README will report it)

Allocated ~6h: models+policy (0:45) → API+state machine (1:45) → rollup+worker (1:00) →
SSE bridge (0:45) → HTMX dashboard fragments (0:45) → tests (0:45) → DESIGN/README
(remainder-ish buffer). Actuals filled in as built.

---

### Open questions before sign-off

1. OK to delete the `core/` demo app in the same change (assumption per repo AGENTS.md)?
2. Repo root stays the submission root (instructions/ becomes `build-context-excluded`
   rather than a separate folder)?
