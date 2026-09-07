"""Django settings for the fuel-route optimizer.

Configuration is environment-driven via python-decouple so the same code runs
locally against SQLite and in production against PostgreSQL + Redis.
"""

from __future__ import annotations

from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="django-insecure-dev-only-key-change-me-in-production",
)
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "routes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------
# Database - SQLite by default (zero-setup demo), PostgreSQL via DATABASE_URL
# --------------------------------------------------------------------------
DATABASE_URL = config("DATABASE_URL", default="")
if DATABASE_URL:
    from urllib.parse import urlparse

    _url = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _url.path.lstrip("/"),
            "USER": _url.username or "",
            "PASSWORD": _url.password or "",
            "HOST": _url.hostname or "",
            "PORT": str(_url.port or ""),
            "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=60, cast=int),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------
# Cache - LocMem locally, Redis in production via REDIS_URL
# --------------------------------------------------------------------------
REDIS_URL = config("REDIS_URL", default="")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "fuelroute",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuelroute-locmem",
            "OPTIONS": {"MAX_ENTRIES": 2000},
        }
    }

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "routes.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": config("API_THROTTLE_ANON", default="60/min")},
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Optimizer API",
    "DESCRIPTION": (
        "Plans a US driving route and selects cost-optimal fuel stops for a "
        "vehicle with a 500-mile range and 10 MPG fuel efficiency."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --------------------------------------------------------------------------
# Vehicle parameters (assignment constants, overridable for testing)
# --------------------------------------------------------------------------
VEHICLE_MAX_RANGE_MILES = config("VEHICLE_MAX_RANGE_MILES", default=500.0, cast=float)
VEHICLE_MPG = config("VEHICLE_MPG", default=10.0, cast=float)

# --------------------------------------------------------------------------
# Route / station search tuning
# --------------------------------------------------------------------------
FUEL_STATION_CORRIDOR_MILES = config("FUEL_STATION_CORRIDOR_MILES", default=10.0, cast=float)
# Route polyline is resampled to this spacing before the proximity search.
ROUTE_RESAMPLE_MILES = config("ROUTE_RESAMPLE_MILES", default=1.0, cast=float)
# Safety margin so a plan never depends on arriving with exactly zero fuel.
FUEL_RESERVE_GALLONS = config("FUEL_RESERVE_GALLONS", default=0.0, cast=float)

# --------------------------------------------------------------------------
# External providers
# --------------------------------------------------------------------------
OSRM_BASE_URL = config("OSRM_BASE_URL", default="https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS = config("OSRM_TIMEOUT_SECONDS", default=15.0, cast=float)
OSRM_MAX_RETRIES = config("OSRM_MAX_RETRIES", default=2, cast=int)

NOMINATIM_BASE_URL = config("NOMINATIM_BASE_URL", default="https://nominatim.openstreetmap.org")
NOMINATIM_TIMEOUT_SECONDS = config("NOMINATIM_TIMEOUT_SECONDS", default=10.0, cast=float)
# Nominatim's usage policy requires a genuine identifying User-Agent.
GEOCODER_USER_AGENT = config(
    "GEOCODER_USER_AGENT",
    default="fuel-route-optimizer/1.0 (backend coding assessment)",
)

CENSUS_GAZETTEER_YEAR = config("CENSUS_GAZETTEER_YEAR", default="2024")
CENSUS_GAZETTEER_BASE_URL = config(
    "CENSUS_GAZETTEER_BASE_URL",
    default="https://www2.census.gov/geo/docs/maps-data/data/gazetteer",
)

GEOCODE_CACHE_TTL_SECONDS = config("GEOCODE_CACHE_TTL_SECONDS", default=60 * 60 * 24 * 30, cast=int)
ROUTE_CACHE_TTL_SECONDS = config("ROUTE_CACHE_TTL_SECONDS", default=60 * 60 * 24, cast=int)
PLAN_CACHE_TTL_SECONDS = config("PLAN_CACHE_TTL_SECONDS", default=60 * 60 * 6, cast=int)

# --------------------------------------------------------------------------
# Security - conservative defaults, hardened automatically when DEBUG is off
# --------------------------------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = config("DATA_UPLOAD_MAX_MEMORY_SIZE", default=16384, cast=int)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_BROWSER_XSS_FILTER = True
    X_FRAME_OPTIONS = "DENY"
    SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=True, cast=bool)
    CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=True, cast=bool)
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=0, cast=int)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL = config("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)-8s %(name)s | %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "routes": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
