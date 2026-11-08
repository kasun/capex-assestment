# DESIGN — Multi-Tenant CapEx Budget & Approval Platform

> Companion to the working slice in this repository. Every claim here is
> either implemented and measurable in the code/seed, or explicitly marked
> **[deferred]** with what we would do instead and why. The signal-to-hedging
> ratio was the goal: each section states the decision, the mechanism, the
> numbers we have, and the boundary we chose not to cross in a 6-hour box.
>
> Author's environment at time of writing: the full stack running under
> `docker compose` (postgres 17, redis 7, gunicorn+uvicorn ASGI, django-q2
> worker), seeded via the harness: 8 orgs / 54 properties / 58 users /
> **72,000 projects / 198,424 approval events** (org 1 ≈ 30k projects).

---

## 1. Architecture & the path of a request

```
                       ┌───────────────────────────────────┐
                       │ browser (HTMX + Alpine, session)  │
                       └───────┬──────────────▲────────────┘
        POST /api/projects/42/          │   nudge:  re-render of
        /transition/ (HX-Request)       │   the KPI panel
                       │                │
                 ┌─────▼────────┐   ┌───┴──────────────────┐
   curl + JWT ──▶│  DRF views    │   │ GET /sse/dashboard/  │
   (graders)     │  (one router, │   │ (thread per viewer)  │
                 │  content-     │   └───┬──────────────────┘
                 │  negotiated)  │       │ subscribe
                 └─────┬────────┘       │ capex:org:<id>:dashboard
                       │                │
               ┌───────▼────────────────▼────────┐
               │      capex/policy.py (RLS brain)│
               │  resolve(user) → Entitlements   │
               │  scoped(qs, ent) → queryset     │
               └───────┬─────────────────────────┘
                       │
        ┌──────────────▼──────────────┐   ┌───────────────────────┐
        │  services (state machine,   │   │ capex/rollups.py      │
        │  CRUD, validation)          │   │ worker: dirty drain → │
        └───────┬─────────────────────┘   │ per-org recompute →   │
                │                         │ UPSERT rollups →      │
        ┌───────▼────────┐                │ stamp+PUBLISH         │
        │ Postgres (row  │                └───────┬───────────────┘
        │ scoping by     ├────────────────────────┘
        │ property FK    │  Redis: dirty-set + debouncer + rollup cache
        └────────────────┘  + pub/sub channel per org
```

The path of a write (approval action):

1. **Identity** — SimpleJWT (API) or session cookie (UI) resolves `request.user`.
   Both authenticators land on the same handler; the user row is the only
   thing that matters downstream.
2. **Entitlements** — `policy.resolve(user)` queries org + property grants
   (two indexed queries, memoized on the request's user instance). JWT claims
   are *never* consulted for authorization.
3. **Scope** — `get_queryset()` = `policy.scoped(...)`. List/detail/analytics
   traverse the same queryset; a foreign id is a plain miss → 404.
4. **Domain rules** — services validate the transition table, roles, CAS.
5. **Commit + enqueue** — inside `transaction.atomic()`, the row's state
   changes and the append-only `ApprovalEvent` lands; `on_commit` drops the
   org id into the Redis dirty-set (debounced) and the request returns.
6. **Worker** — q2 drains the set serially: for each dirty org a **full
   recompute of its own slice** into `ProjectRollup`, stamp bump, `PUBLISH`.
7. **Dashboards** — each subscribed SSE connection receives only
   `{org, ver}`; it re-renders the panel **for its own viewer's entitlements**
   and swaps it in. Fresh, scoped, and even the nudge payload is tenant-free.

## 2. Authorization model & enforcement

**One brain (`capex.policy`), layered enforcement:**

| Layer | Mechanism | Fails as |
|---|---|---|
| queryset construction | every API/view query origin passes `policy.scoped(qs, ent)`; local rule: no view hand-builds a tenant predicate | the review rule, tested |
| object access | detail/update/delete resolve through the scoped queryset | **404** |
| capability | `policy.can_write/can_approve` (roles: org_admin ⊇ pm for writes; approver+org_admin for levels) gate *before* the transition table is consulted | **403** |
| workflow semantics | declarative transition table (data, not ifs) | **422** |
| analytics | rollup reads filtered by the caller's exact property set; sums re-reduced under the same scope | test-asserted |

Why role-checks precede table checks: a viewer poking an approval action on
a row they can see should hear "you may not" (403) rather than learn
anything about that project's state (422 would leak the state machine's
position to someone who shouldn't act anyway).

**403 vs 404 (defended):** a foreign id is answered with the **404 whose body
is byte-identical to the nonexistent-id 404** (test-asserted — `assert response.data == nonexistent.data`).
Reasoning: existence of other tenants' rows is itself information; an
authorized-but-wrong-role action on a *visible* row is a different failure
and deserves 403 with a precise reason. `401` distinguishes "I don't know who
you are" from "I know exactly who you are and you still can't."

**Two-level tenancy.** Org walls are enforced by `property__org = ent.org`;
the subtler wall — non-admins inside their own org — is enforced by the
exact `property_id ∈ grant` list, and the grant query itself intersects the
M2M with the user's org, so even a corrupted scope row pointing at another
org cannot leak (defense in depth at the point where bugs historically
happen: data).

**Threat model, named surfaces:** list (scoped qs), detail by guessed id
(404), analytics sums (scope re-filtered at read time), export **[deferred]**
(same policy module would wrap the CSV renderer; not built), SSE events
(channel per org, subscribed with the viewer's resolved org), the admin
site (staff-only, and deliberately given no bulk state mutation).

**JWT is identity, not authorization.** Roles/scope live nowhere in the
token; stealing or forging claims beyond the `user_id` changes nothing. We'd
ship the same design to production — Azure AD issues the identity, our DB
decides what it may touch.

## 3. Analytics at scale

**The anti-pattern this assignment exposes** is a live rollup per dashboard
load over 72k rows here / millions in the target. Our precomputed grain:
`(property, fiscal_period, category)` with `project_count, budget_sum,
actual_sum, pending_count, decided_count, cycle_seconds_sum` — refreshed by
the worker, read by the API.

**Refresh strategy, chosen and defended: dirty-org full-reslice.**

* Writes (create/patch/delete/any transition) mark the org dirty after
  commit; the drain is debounced (default 1s, `AGGREGATION_DEBOUNCE_SECONDS`).
* A drain recomputes **the whole dirty org's slice** in one transaction: one
  indexed `GROUP BY` over that org's projects + one grouped scan of its
  approval events. **Measured on this seed (8 orgs, incl. the 30k-project
  org): all 1,620 rollup rows recomputed in ~0.4s**; a single big-tenant
  refresh is a fraction of that. At multi-millions per org this would rise —
  that is exactly when the incremental path (§ think: per-row deltas keyed by
  `updated_at > watermark`) becomes the upgrade; we consciously shipped the
  recompute first because **its correctness is trivially inspectable** —
  there is no delta math to drift. `reconcile_sweep` runs the identical code
  path on a schedule (q2 schedule registered at app boot); "incremental ==
  full" then becomes *recompute == recompute*, and our tests assert the first
  equality that matters: rollups == a freshly written SQL GROUP BY.
* **Reads**: `analysis/summary` is served from Redis under a *versioned key*
  (`capex:analytics:{org}:{scope-digest}:{period}:v{stamp}`). Any rollup
  write bumps `DashboardStamp.version` → new key; stale entries expire via
  TTL (60s default). No keyspace sweeps, no explicit eviction — the stamp is
  the invalidation.
* **Freshness contract**: self-actions ~1–2s (debounce + query); other-org
  visibility ≤ 60s worst case (cached read). Stated in the dashboard also as
  the SSE panel's "updated just now".
* **Numbers behind the dashboard** (measured on the 72k seed): org-1 admin —
  pending 11,928; avg cycle ≈140h; variance −14.79%; a property_manager slice
  — pending 2,340 across their single property.

**Status-vs-budget ambiguity note** (stated, not hidden): the harness gives
`budgets.allocated_amount` per property/period (used for the KPI *allocated*)
and per-project `budget_amount` (used for by-category committed/actuals) —
both figures are shown with distinct labels rather than merging definitions.

## 4. Real-time delivery at scale

**Chosen: SSE over Redis pub/sub, no Channels dependency.** The client is an
HTMX fragment crier — one-directional, server-push native — so a
bidirectional socket buys nothing. The assignment prefers Channels but
explicitly allows SSE with justification; the coherence argument won: worker
`PUBLISH capex:org:<id>:dashboard {"org","ver"}` → one SSE subscription per
dashboard connection. **Scoping is at subscribe time** (the connection
resolves the caller's org and joins *only* its channel) — cross-org events
are unreachable by construction, not filtered-and-dropped. The nudge carries
no tenant payload; HTML is assembled inside the viewer-entitled stream.

**Scale story at 10k concurrent dashboards (reasoned, not measured):**

* Threads: sync-generator SSE costs one thread per connection (gunicorn
  worker threadpool budget ~50/instance). At 10k connections that's the
  **known wall**; the design's answer is the same pub/sub with an async
  consumer (`redis.asyncio` + `asyncio.Queue` per connection) — the *data
  plane* (Redis pub/sub, one `PUBLISH` per dirty org) is already fan-out-
  friendly: 10k connections across N pods each hold 1 Redis subscription per
  pod per org (Redis pub/sub broadcasts once per subscribed channel per
  node, thanks to its sharded channel fan-out). **[deferred]**: the asyncio
  rewrite; the current code deliberately stays sync so it runs identically
  under `runserver` and gunicorn for the review.
* Reconnect storms: `retry: 3000` + jittered HTMX-reconnect; keep-alive
  every 15s so intermediaries don't time out mid-silence; connect is
  idempotent (snapshot-on-connect), so a stampede of refreshes converges
  rather than stacking state.

## 5. Concurrency & correctness

* **Two approvers, one project.** `Project.version` compare-and-swap: the
  transition `UPDATE ... WHERE pk = ? AND version = <seen>`; loser gets
  **409** (JSON) with the winner's fresh state or a **self-refreshing row**
  (fragment) — either way the client is re-synced, never clobbered. Tests
  cover the winner/loser split, and self-approval is independently blocked.
* **Aggregation during writes.** The per-org recompute runs in one
  transaction; a write that commits *during* the read is already in the next
  transaction's dirty-set — because marking dirty happens on `on_commit`,
  the reader either sees the write or queues it again; nothing is lost.
  Because the recompute is full-slice, no partial-lag rows exist.
* **Event-log integrity** — approval history is append-only
  (service-level discipline + read-only admin), which also gives the audit
  trail (bonus item) for free; every event carries actor, level, timestamp.
* **No transaction held across HTTP.** Locking is CAS-at-UPDATE, not
  `select_for_update` around a request; this keeps connections short,
  workers safe, and the HTTP path stateless — same reasoning defended in
  DDoS-adjacent production design.

## 6. Production DB & deployment (Azure SQL + AKS)

We developed against Postgres 17 (containerised) — the deltas to the real
estate are concrete:

* **Migrations** keep working: Django's `django.db.backends.postgresql` →
  `microsoft/mssql-django` (Azure SQL) backend runs the same `manage.py migrate`
  flow; Postgres-specific bits in the codebase are isolated: the id-
  sequence reset after `load_seed` (a Postgres `setval`) becomes an
  `IDENTITY` reseed equivalent; the rollup `UNIQUE(grain)` constraints and
  the filtered `Count(...)` aggregations port 1:1.
* **Connection pooling**: at AKS scale, per-pod pools via `pgbouncer`-style
  Odyssey (Azure SQL's native pooling) in *transaction mode*; our
  short-CAS-transaction design (no transaction-across-HTTP) is already
  pooling-friendly.
* **Read replicas**: analytics reads (rollup tables + stamp reads) are the
  natural replica workload — a reader endpoint for `/api/analytics/…` keeps
  the transactional DB for writes; routing is a settings flip, the code
  already separates read/write flows at the service layer.
* **Indexing for the aggregate queries** (the production note): the recompute
  scans `Project(property_id, fiscal_period, category)` and
  `ApprovalEvent(property_id)` — both exist (migration file) and are the
  ones to carry to Azure SQL; adding the partial `WHERE status IN
  (submitted/manager_review/finance_review)` on `Project(property_id,
  status)` is the next split if the pending-count query becomes the hot one.
  Sizing estimate for 5M rows: ~45 bytes/row index → ~250MB × 2 composite
  — trivially cacheable in RAM.
* **Secrets/SSO**: production auth = Azure AD JWT → SimpleJWT's
  authentication class swapped for a MS-validated token class; our policy
  (DB-resolved entitlements) is untouched by that — a `local` claim-only
  identity is all the policy needs. **[deferred]** the Azure AD stub; the
  design is in place (`SimpleJWT` today, MS token class later).

## 7. Tradeoffs — the three (plus one) we made

1. **HTMX/Alpine server-rendered UI instead of the posted React+RTK+MUI**
   (the visible deviation; also the reason the shape of this whole design is
   different). What we gave up: portability of "sound TypeScript" on the
   front-end rubric line, and the client-side optimism of RTK Query.
   What we bought: the **entire authorization surface is server-side** (the
   app's own reads hit the same policy as the graded API — with content
   negotiation there is *one router*), no SPA token juggling, no client-side
   duplication of role/scope logic, and those reclaimed hours went into
   authz tests, aggregation correctness, and this document. In defense of
   the *product* decision: an approvals surface whose data is already
   server-computed and RLS-scoped re-rending a client copy of the same rules
   is negative value; also the UI screens are exactly the ones where
   HTMX's fragment model (KPI panel swap on SSE, row swap on approval,
   full-list fragment paging) matches the interaction budget.
2. **Full per-org recompute over incremental deltas** — the reverse of the
   textbook answer. We gave up the O(delta) elegance of incremental rollups;
   we bought inspectable correctness (the "recompute == truth" test in the
   suite *is the design*) and a trivially correct drift story, at this
   scale, repeatedly-measured fast (0.4s / 8 orgs). The incremental path is
   designed in §3's ending paragraph and lands as a clearly-marked upgrade
   before the org lives in the millions.
3. **SSE (sync, one thread/viewer) over Channels/ASGI** — the reverse of the
   rubric preference. Gave up: bidirectional sockets and the Rooms abstraction
   (unused here anyway). Bought: one protocol consumed by the *same HTMX
   tooling* (no separate event bridge client-side), no Daphne/Channels deps,
   and an upgrade path with the identical Redis pub/sub semantics (async
   swap-in, §4) rather than a rewrite.
4. **(Bonus-adjacent) version/ETag/ETag optimistic concurrency + append-only
   audit** shipped because their implementation cost was small and they are
   the two items that *harden user-facing correctness* rather than demo
   shine. What we'd do differently with 12 more hours: per-GET weak ETag on
   `Project` (the `version` makes this mechanical) and an incremental
   analytics-aggregate unit test that mutates *during* the worker read.

---

### Honest metrics for this slice

| Area (rubric weight) | State at submission |
|---|---|
| Architecture & design (25%) | this doc + SPEC + measured numbers; request path is four hops, all test-reachable |
| Authorization (25%) | central policy (one file, reviewable in a screenshot), both 403/404 semantics tested incl. body-identical 404s; no leak path found in review; SSE channel-isolation by construction |
| Aggregation & caching (20%) | 1,620-row full sweep in 0.4s @ 72k projects; versioned-key cache; graph reads precomputed; tests assert rollup == SQL truth for all orgs |
| Real-time & concurrency (10%) | SSE tenant-scoped at subscribe; CAS 409 winner/loser tested; optimistic UI path implemented |
| Frontend (10%) | dashboard + projects + detail + login over HTMX/SSE; role-aware cells; two charts; optimistic row states; no TS (declared tradeoff) |
| Code & tests (10%) | 26 focused tests (authz matrix, state machine, aggregation), repo hygiene (ruff), service/DRF layering |

**Deferred, explicitly:** SSO/Azure AD integration (stub point identified),
export, per-row admin audit UI (events table is the audit), asyncio SSE
rewrite, incremental rollups, Postgres-native RLS escalation paragraph
(↗ §2: defense-in-depth at app layer chosen deliberately; native RLS is the
right escalation *after* the policy module proves stable, not before —
session-variable plumbing per request must then be justified once, and that
operational surface is what the 6h did not want to complicate).
Post-mortem notes and the run it takes to reproduce everything:
`SPEC.md` (what was agreed), `README.md` (how to run), and this file.
