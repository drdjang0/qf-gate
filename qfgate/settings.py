"""Django settings for QF-gate.

All secrets and environment-specific values come from environment variables
(or a local `.env` file, see `.env.example`). Nothing secret lives in code.
"""

import importlib.util
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real env vars always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off")
    SECRET_KEY = "dev-only-insecure-key-change-me"

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "market",
    "trading",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if importlib.util.find_spec("whitenoise") else []),
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "qfgate.urls"

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
                "market.context_processors.services",
            ],
        },
    },
]

WSGI_APPLICATION = "qfgate.wsgi.application"
ASGI_APPLICATION = "qfgate.asgi.application"

# Database: SQLite for local dev; set POSTGRES_DB (+ friends) to use PostgreSQL,
# which is recommended once the live stream and bots write concurrently.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,  # long-running worker/stream survive DB restarts
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # IMMEDIATE: take the write lock when a transaction starts, so a busy database makes
            # it wait (up to `timeout`) instead of failing at once with "database is locked"
            # when a read-then-write transaction collides with the stream's writes.
            "OPTIONS": {"timeout": 30, "init_command": "PRAGMA journal_mode=WAL;", "transaction_mode": "IMMEDIATE"},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"  # storage is always UTC; the day boundary is configured below
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if importlib.util.find_spec("whitenoise") and not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "market:coin_list"
LOGOUT_REDIRECT_URL = "login"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- QF-gate market settings --------------------------------------------------
GATE = {
    "REST_BASE": os.environ.get("GATE_REST_BASE", "https://api.gateio.ws/api/v4"),
    "WS_URL": os.environ.get("GATE_WS_URL", "wss://api.gateio.ws/ws/v4/"),
    "ARCHIVE_BASE": os.environ.get("GATE_ARCHIVE_BASE", "https://download.gatedata.org"),
    # Used only by live bots (trading/brokers.py). Trade-only permission, no withdrawals,
    # IP-whitelisted, read only from the environment.
    "API_KEY": os.environ.get("GATE_API_KEY", ""),
    "API_SECRET": os.environ.get("GATE_API_SECRET", ""),
    "TIMEOUT": float(os.environ.get("GATE_TIMEOUT", "20")),
}

# Bots, alerts and live trading. Secrets come from the environment only, never the database.
TRADING = {
    # Third switch for live orders (with API keys + the Control page). Anything but "true" keeps bots dry-run only.
    "LIVE_TRADING": os.environ.get("LIVE_TRADING", "false"),
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),  # only this chat may receive alerts / send commands
    "WEBHOOK_URLS": [u.strip() for u in os.environ.get("WEBHOOK_URLS", "").split(",") if u.strip()],
    "STALE_SERVICE_SECONDS": int(os.environ.get("STALE_SERVICE_SECONDS", "180")),
    # signal sharing: other instances read /trading/api/v1/signals and push to .../ingest with this token
    "SIGNAL_API_TOKEN": os.environ.get("SIGNAL_API_TOKEN", ""),
}

MARKET = {
    "HISTORY_DAYS": int(os.environ.get("MARKET_HISTORY_DAYS", "60")),
    # Day boundary for daily high/low. UTC matches Gate.io's 1d candles and
    # the BTC sample spreadsheet. Any IANA zone works, e.g. "Asia/Karachi".
    "DAY_TIMEZONE": os.environ.get("MARKET_DAY_TIMEZONE", "UTC"),
    "DEFAULT_QUOTE": os.environ.get("MARKET_DEFAULT_QUOTE", "USDT"),
    # Downloaded monthly trade archives are cached here (40-60 MB each for BTC).
    "ARCHIVE_CACHE_DIR": Path(os.environ.get("MARKET_ARCHIVE_CACHE_DIR", BASE_DIR / "var" / "archive")),
    # Verify chain + contract address against Gate.io when a coin is saved.
    "VERIFY_ON_SAVE": env_bool("MARKET_VERIFY_ON_SAVE", True),
    # Background schedule run by `python manage.py run_worker`.
    "SCHEDULE": {
        "SYNC_EVERY_MINUTES": int(os.environ.get("MARKET_SYNC_EVERY_MINUTES", "15")),  # 0 = off
        "DAILY_AT_UTC": os.environ.get("MARKET_DAILY_AT_UTC", "00:05"),
        "KEEP_JOBS_DAYS": int(os.environ.get("MARKET_KEEP_JOBS_DAYS", "14")),
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"market": {"handlers": ["console"], "level": os.environ.get("MARKET_LOG_LEVEL", "INFO")}},
}
