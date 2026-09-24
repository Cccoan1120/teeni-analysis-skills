import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent


def env_or_file(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    path_value = os.environ.get(f"{name}_FILE", "")
    if not path_value:
        return default
    path = Path(path_value)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ImproperlyConfigured(f"Unable to read {name}_FILE") from exc
    if not value or "\n" in value or "\r" in value:
        raise ImproperlyConfigured(f"{name}_FILE must contain one non-empty line")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0") == "1"


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = env_or_file("DJANGO_SECRET_KEY", "local-development-only-change-me")
if not DEBUG and SECRET_KEY == "local-development-only-change-me":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG=0")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
    "topics",
    "interests",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
        "DIRS": [],
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

def database_config() -> dict:
    database_url = env_or_file("DATABASE_URL").strip()
    if not database_url:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.environ.get("TEENI_DB_PATH", BASE_DIR / "db.sqlite3")),
            "OPTIONS": {"timeout": 20},
        }

    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ImproperlyConfigured("DATABASE_URL must be a PostgreSQL URL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname,
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


DATABASES = {"default": database_config()}

AUTH_PASSWORD_VALIDATORS = []
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "login"

TEENI_PUBLISH_TOKEN = env_or_file("TEENI_PUBLISH_TOKEN")
TEENI_INTEREST_IDENTITY_KEY = env_or_file("TEENI_INTEREST_IDENTITY_KEY")
TEENI_BASELINE_PATH = BASE_DIR / "dashboard" / "data" / "historical_baseline.json"
TEENI_TOPIC_STORAGE_ROOT = Path(
    os.environ.get("TEENI_TOPIC_STORAGE_ROOT", BASE_DIR / "data" / "topic-jobs")
)
TEENI_TOPIC_MAX_UPLOAD_BYTES = int(os.environ.get("TEENI_TOPIC_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
TEENI_TOPIC_CHUNK_BYTES = int(os.environ.get("TEENI_TOPIC_CHUNK_BYTES", str(8 * 1024**2)))
DATA_UPLOAD_MAX_MEMORY_SIZE = TEENI_TOPIC_CHUNK_BYTES + 1024**2
TEENI_TOPIC_RETENTION_DAYS = int(os.environ.get("TEENI_TOPIC_RETENTION_DAYS", "30"))
TEENI_TOPIC_DISK_RESERVE_RATIO = float(os.environ.get("TEENI_TOPIC_DISK_RESERVE_RATIO", "0.20"))
TEENI_TOPIC_MODEL = os.environ.get("TEENI_TOPIC_MODEL", "Qwen3.8-27B")
TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS = int(
    os.environ.get("TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS", str(2 * 60 * 60))
)
if not 60 <= TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS <= 2 * 60 * 60:
    raise ImproperlyConfigured("TEENI_INTEREST_ANALYSIS_TIMEOUT_SECONDS must be between 60 and 7200")
TEENI_TOPIC_PIPELINE_MODE = "interest_v1"
TEENI_BASE_SKILL_ROOT = os.environ.get("TEENI_BASE_SKILL_ROOT", "")
TEENI_ANALYSIS_NODE = os.environ.get("TEENI_ANALYSIS_NODE", "node")
TEENI_ANALYSIS_PYTHON = os.environ.get("TEENI_ANALYSIS_PYTHON", "python3")
TEENI_NODE_MODULES = os.environ.get("TEENI_NODE_MODULES", "")
TEENI_TOPICS_BASE_URL = os.environ.get("TEENI_TOPICS_BASE_URL", "")
TEENI_TOPICS_CA_FILE = os.environ.get("TEENI_TOPICS_CA_FILE", "")
TEENI_TOPICS_API_KEY_FILE = os.environ.get("TEENI_TOPICS_API_KEY_FILE", "")
TEENI_FEISHU_WEBHOOK_FILE = os.environ.get("TEENI_FEISHU_WEBHOOK_FILE", "")
TEENI_PUBLIC_BASE_URL = os.environ.get("TEENI_PUBLIC_BASE_URL", "http://127.0.0.1:9120")
TEENI_LOGIN_MAX_FAILURES = int(os.environ.get("TEENI_LOGIN_MAX_FAILURES", "5"))
TEENI_LOGIN_LOCK_MINUTES = int(os.environ.get("TEENI_LOGIN_LOCK_MINUTES", "15"))

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = TEENI_PUBLIC_BASE_URL.startswith("https://") or env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = TEENI_PUBLIC_BASE_URL.startswith("https://") or env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "86400" if TEENI_PUBLIC_BASE_URL.startswith("https://") else "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS")
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD")
