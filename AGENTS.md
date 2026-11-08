# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project overview

- **Stack**: Django 5 (SQLite) + HTMX + Alpine.js + Tailwind v4, managed with `uv`, orchestrated through `make`.
- **Purpose**: bootstrap for an upcoming, still-unknown assignment. The `core` app is a small demo (task list) that doubles as a pattern reference. When the assignment arrives, either build on `core` or add a new app following the same conventions — delete demo code (models, views, templates, tests, migrations) once it is no longer useful, in the same change.

## Spec-first workflow

When the assignment arrives, write a one-page-per-feature `SPEC.md` (problem restated in own words, data model, views/fragments with the HTMX exchange for each, edge cases, out-of-scope list, assumptions made) and get user sign-off before implementing. Default to a **single** `SPEC.md` with a section per feature plus the shared data model and conventions; only split into `docs/specs/<feature>.md` (with `SPEC.md` reduced to an index + shared decisions) if the doc exceeds ~6 pages or features are genuinely independent modules. Non-trivial changes to existing code follow the same gate: if there is no spec covering the change, propose one first. UI design follows the same gate via Pencil (`.pen` mockups of pages/fragments incl. empty/error/loading states, reviewed with the user before implementation; exports can be embedded into the spec) — mockups are visual reference only: templates stay the single source of UI truth, built by hand with Tailwind classes. Keep specs truthful as implementation reveals new information — they ship as part of the submission and double as documentation of intent.

## Commands

- Setup once: `make setup`
- Dev server: `make run` (http://127.0.0.1:8000)
- Docker alternative (no host tooling): `docker compose up --build` — needs Docker, nothing else
- Tests: `make test` (pytest + pytest-django; a tmp SQLite db is used automatically)
- Lint/format: `make lint`, `make fmt` (ruff; auto-fixes)
- Migrations: after changing models run `uv run manage.py makemigrations <app>` then `make migrate`
- CSS: `make css` after editing any Tailwind class in templates or `static/css/input.css`
- Python must be invoked as `uv run ...` — do not use pip, poetry, or the system Python.
- Makefile-free equivalents of every `make` target live in `README.md` ("Without make") — use those if `make` is unavailable; the Makefile is just an alias layer over them.

## Architecture rules

1. **Server-render everything.** Django templates are the single source of UI truth. No client-side rendering, no JSON APIs, no fetch calls — HTMX owns all network-triggered DOM updates.
2. **Partial vs full-page pattern.** A view that serves an HTMX endpoint renders `templates/<app>/partials/<fragment>.html` when `request.htmx` is truthy and the full page otherwise (see `core/views.py: home`). Never render a full page into an `hx-target`.
3. **Fragment hygiene.** Partials contain only the swapped node. Each HTMX exchange targets the smallest possible DOM element (`hx-target="closest ..."` for row-level updates). `task_list.html` (list-level) and `task_row.html` (row-level) show both granularities.
4. **Alpine only for client-local state** (show/hide, tabs, modals, inputs). If state must survive a redirect, reload, or be shared between two components → that is server state; round-trip it through Django, HTMX, or Django sessions instead.
5. **CSRF**: Django's CSRF middleware is active and HTMX does not send Django's token by itself. The wiring is already in place — `base.html` has a global `htmx:configRequest` listener that copies the `csrftoken` cookie into the `X-CSRFToken` header, and page views set `@ensure_csrf_cookie` so the cookie always exists. When adding a new full-page view that contains `hx-post`/`hx-delete` triggers, decorate it with `@ensure_csrf_cookie` too. Never disable the middleware to "fix" 403s.
6. **Models/migrations**: never edit existing migrations; create new ones. No `Meta.default_permissions` or custom SQL unless required.
7. **All forms** get explicit `{% csrf_token %}` or proper `hx-` attributes; prefer Django `ModelForm`s over raw hand-rolled validation.

## Templates

- `templates/base.html` is the only layout. Pages extend it; partials must NOT extend it (no headers, no scripts).
- Frontend libs are vendored under `static/js/vendor/` — never add CDN `<script>` tags.
- Style exclusively with Tailwind utility classes inside templates. Color and type go through the Bluefin token layer in `static/css/input.css` (`bg-fin-*`, `text-fin-*`, `rounded-card`, `shadow-card`, `font-head`/`font-mono`) — never raw hex values. If a template edit adds a class that isn't in `static/css/tailwind.css`, run `make css`.
- Dark mode is session server state (`data-theme` on `<html>`, toggled via `/theme/set/`). Restyled elements must look right in both themes — `fin-accent` flips polarity between them, so text placed on it must use `text-fin-on-accent`, not white/black.
- `x-cloak` is pre-styled in `base.html` to prevent Alpine flash-of-content; use `x-cloak` on elements hidden until Alpine initializes.
- `htmx-indicator` (spinner) pattern exists in `templates/core/home.html`; reuse `#spinner` or add a local one per exchange.

## Tests

- Put tests in the app's `tests.py` (or `tests/` module if it grows). Use `pytest.mark.django_db` (module-level `pytestmark` is already set in `core/tests.py`).
- Always test the HTMX path explicitly: `client.get("/", HTTP_HX_REQUEST="true")` must return fragment-only content (no `<html` in the body).
- Run `make test` before declaring any change done; `make lint` too.

## Conventions

- Comments only when non-obvious. Docstrings for every model, view, and non-trivial helper.
- No unrelated edits while fixing bugs. Do not bump dependencies.
- Do not commit `db.sqlite3`, `staticfiles/` contents (except `.gitkeep`), or `node_modules/`.
- Don't touch `uv.lock` manually — dependency changes go through `uv add/remove`.

## When HTMX isn't enough

Escalate to (or raise with the user about adopting) a React/SPA frontend only if the work requires: complex reactive client state, WebSockets/sse live dashboards dominated in the browser, drag-heavy graph editors, or a large component library the assignment already specifies. Otherwise stay in the HTMX/Alpine model — the whole repo is organized around it.
