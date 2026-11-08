# CapEx platform — API image.
# Same image serves the web/api (gunicorn + uvicorn ASGI workers) and the
# django-q2 worker. Static files are collected at build time so whitenoise
# serves them from the manifest in both DEBUG modes.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ backend/
COPY frontend/ frontend/

RUN cd backend && python manage.py collectstatic --noinput

EXPOSE 8000
WORKDIR /app/backend
