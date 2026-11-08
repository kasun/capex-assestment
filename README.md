# Full-stack assessment (bootstrap)

Django + HTMX + Alpine.js, styled with Tailwind, managed with `uv`.
SQLite by default; no build pipeline beyond CSS.

## Stack

| Layer     | Choice                                              |
| --------- | --------------------------------------------------- |
| Backend   | Django 5, HTTP layer serves both HTML and HTMX partials |
| Frontend  | HTMX (partial swaps) + Alpine.js (local interactivity) |
| Styling   | Tailwind v4 (Bluefin token set in `static/css/input.css`, light/dark themes; fonts vendored in `static/fonts/`) |
| Packaging | `uv` (+ optional `make` aliases; raw commands in README) |
| Testing   | `pytest` + `pytest-django`                          |
| Linting   | `ruff`                                              |

## Setup

```sh
make setup
```

This installs Python + Node deps, applies migrations, and builds the CSS once.

## Run

```sh
make run          # Django dev server on http://127.0.0.1:8000
make css-watch    # optional second terminal: rebuild CSS on edit
```

If pages look unstyled after editing templates, run `make css` — Tailwind v4
scans the repo for class names and only emits what it finds.

## Everyday commands

```sh
make test         # pytest
make lint         # ruff check
make fmt          # ruff autofix + format
make migrate      # apply migrations
make shell        # Django shell
```

### Without make

Every target is just a plain command — use these directly if `make` is not
installed (the Makefile is a convenience alias over exactly these):

```sh
uv sync                                                     # setup: install Python deps
npm install                                                 # setup: install Node deps (Tailwind)
uv run manage.py migrate                                    # setup/migrate
npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --minify  # setup/css
uv run manage.py runserver                                  # run
npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --watch   # css-watch
uv run pytest                                               # test
uv run ruff check .                                         # lint
uv run ruff check --fix . && uv run ruff format .           # fmt
uv run manage.py shell                                      # shell
uv run manage.py collectstatic --noinput                    # collectstatic (production)
```

## Project layout

```
config/              Django project (settings, urls, asgi, wsgi)
core/                Main app (models, views, urls, forms, tests)
templates/
  base.html          App shell: sidebar layout, theme attr, CSRF wiring
  partials/          _sidebar.html (app shell rail)
  core/              Full-page templates
    partials/        HTMX response fragments (never standalone pages)
static/
  css/input.css      Tailwind input incl. Bluefin design tokens (committed)
  css/tailwind.css   Tailwind output (generated, git-ignored)
  fonts/             Vendored Google Fonts: Inter, Space Grotesk, Geist Mono
  js/vendor/         htmx.min.js, alpine.min.js (vendored, no CDN)
designs/
  bluefin-ui.pen     UI mockups (pen.dev); exports/ has PNG previews
```

## Design system

The UI follows the **Bluefin** mockups (`designs/bluefin-ui.pen`, exported PNGs in
`designs/exports/`). Tokens (colors, fonts, radii) live in `static/css/input.css`
as CSS variables mapped through Tailwind's `@theme inline` — use the `fin-*`
utilities (`bg-fin-card`, `text-fin-muted`, `rounded-card`, …), never raw hex
values. Dark mode is server state: the sidebar toggle POSTs to `/theme/set/`,
the session stores it, and `base.html` renders `<html data-theme=…>` before
CSS loads (no flash).

## The core pattern: server-rendered partials

HTMX fetches fragments from Django; Django decides whether to render a whole
page or just the fragment based on `request.htmx`. See `core/views.py`:

```python
if request.htmx:
    return render(request, "core/partials/task_list.html", context)  # fragment
return render(request, "core/home.html", context)  # full page
```

Templates mirror that split: `templates/core/*.html` are full pages, and
`templates/core/partials/*.html` are fragments swapped in by `hx-*` attributes.

## The demo app

The `core` app ships with a task list that exercises the full loop: create via
HTMX POST, inline toggle, delete with `hx-confirm`, and debounced search. Treat
it as a living pattern reference and repurpose/delete it once the real
assignment requirements land.

## Agent harness support

This repo is designed to be worked on by any AI coding agent harness. There is
one source of truth for agent guidance — [`AGENTS.md`](AGENTS.md) — and every
other file points at it rather than duplicating it:

| Harness                                              | File it reads                              |
| ---------------------------------------------------- | ------------------------------------------ |
| OpenCode, Codex, Cline, Amp, Zed, Jules, Windsurf    | `AGENTS.md` (natively)                     |
| Claude Code                                          | `CLAUDE.md` → imports `AGENTS.md`          |
| GitHub Copilot                                       | `.github/copilot-instructions.md` → same   |

Deeper workflow guides ("skills") live in `.agents/skills/*/SKILL.md` and are
exposed to specific harnesses via symlinks (`.opencode/skills`,
`.claude/skills`). If you add a skill, add it once under `.agents/skills/` —
every harness picks it up. Do not duplicate or fork `AGENTS.md` into pointers;
edit the source and let the pointers ride along.

## Docker

No host tooling needed (no Python, `uv`, Node, or `make`):

```sh
docker compose up --build    # http://localhost:8000
docker compose run tests     # the pytest suite inside a dev image (test deps live there, not in the web image)
docker compose down          # stop
```

Runs with `DJANGO_DEBUG=0` (whitenoise serves static files; `collectstatic`
happens at image build). The SQLite database lives inside the container, so it
is re-created fresh each run — the container is a self-contained demo, not
persistent storage. Set `DJANGO_SECRET_KEY` for anything non-trivial.

## Production-ish config

Environment variables: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default on),
`DJANGO_ALLOWED_HOSTS`. `DEBUG=0` switches static storage to whitenoise's
compressed manifest storage and requires `collectstatic` to be run first.

## Why HTMX + Alpine (not React)

Server-rendered Django templates already produce the HTML; HTMX handles
network-triggered DOM updates without writing fetch/JSON glue, and Alpine
covers client-local state (dropdowns, modals, tabs) declaratively inside
templates. React is only worth introducing if the assignment ends up needing
heavy client-side state, real-time streams, or a component ecosystem — see
`AGENTS.md` for the guidance on making that call.
