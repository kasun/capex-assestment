"""
Django settings for the CapEx platform take-home.

Environment-driven where it matters (DB, Redis, secret), defaults elsewhere.
Single Postgres DB + single Redis are the two external dependencies.
"""

import os
from datetime import timedelta
from pathlib import Path

# backend/ — the Django project root (manage.py, config/, capex/).
BASE_DIR = Path(__file__).resolve().parent.parent
# repo-root/frontend — templates and static assets (the HTMX UI) live here
# rather than inside backend/, so the deliverable tree reads as spec'd.
FRONTEND_DIR = BASE_DIR.parent / "frontend"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h
]
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost:8000").split(",") if o
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "django_htmx",
    "rest_framework",
    "django_q",
    # Local
    "capex",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [FRONTEND_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

# --- Data stores -----------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "capex"),
        "USER": os.environ.get("POSTGRES_USER", "capex"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "capex"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
_REDIS_PARTS = REDIS_URL.replace("redis://", "").split("/")
_REDIS_HOST, _REDIS_PORT, _REDIS_DB = (_REDIS_PARTS + ["0"] * 2)[:3]
REDIS_DB_NUM = int(_REDIS_DB or 0)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

Q_CLUSTER = {  # django-q2: broker = Redis, scheduler in the same qcluster process
    "name": "capex",
    "workers": int(os.environ.get("Q_WORKERS", "2")),
    "timeout": 300,
    "retry": 360,  # must exceed timeout or django-q2 re-runs running jobs
    "catch_up": False,  # don't stampede scheduled catch-ups on worker startup
    "redis": {"host": _REDIS_HOST, "port": _REDIS_PORT, "db": REDIS_DB_NUM},
}

# --- Auth ------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = []  # seeded demo passwords don't pass policy checks

AUTH_USER_MODEL = "capex.User"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Deny-by-default: a view that forgets its permission leaks loudly (401/403),
    # not silently. ScopedModelView then refines to object-level scoping.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "capex.api.exceptions.service_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_RATES": {"auth": "30/minute"},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# --- i18n / tz / static ----------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [FRONTEND_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Domain knobs (single place so DESIGN.md can cite them) ----------------

# Debounce window the worker waits before refreshing dirty orgs' rollups.
AGGREGATION_DEBOUNCE_SECONDS = int(os.environ.get("AGG_DEBOUNCE", "1"))
# Full reconciliation sweep cadence (drift insurance on incremental math).
AGGREGATION_RECONCILE_MINUTES = int(os.environ.get("AGG_RECONCILE_MINUTES", "5"))
# TTL for the analytics endpoint's Redis cache (staleness ceiling for reads).
ANALYTICS_CACHE_TTL = int(os.environ.get("ANALYTICS_TTL", "60"))
