"""AICortex's built-in settings (since 2.7.1).

Up to 2.7.0 some seventy environment variables tuned limits, moved data folders or
switched protections off. Since 2.7.1 the secure value is built in here and the
environment is no longer read for any of them. What an install still sets in .env:

  required   BASE_URL, OIDC_CONFIG_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET,
             JWT_SIGNING_KEY, STORAGE_ENCRYPTION_KEY (+ PUID, PGID, TZ for the container)
  optional   INTERNAL_ALLOW_CIDRS, FORWARDED_ALLOW_IPS, OPERATORS, MCP_REDIRECT_URIS,
             STORAGE_ENCRYPTION_KEY_OLD
  rarely     OIDC_SCOPE, AUTH_ROLE_CLAIM, RUNNER_TOKEN, MCP_ALLOWED_HOSTS,
             MCP_ALLOWED_ORIGINS
  trial      ALLOW_INSECURE=1 — only for trying AICortex on your own computer with no
             sign-in at all (README "Try it in 5 minutes"); the start warns loudly

Three settings moved into the WebUI (Overview → Settings, see admin_settings.py): the
mail recipient allow-list, the default tool profile and auto-memory.

Every data folder lies under /data (``data_dir``). A folder an install from before
2.7.1 set explicitly (MEMORY_DIR=… and the like) is still honoured, so a custom path
never loses its data — but it is no longer documented.

The one test hook: tests (and the glama / upgrade-check launchers) change a value by
monkeypatching this module before the code that reads it runs. Production never does.
"""
import os
from pathlib import Path

# ── network: the container listens here (the compose file maps the host port) ──
HOST = "0.0.0.0"
PORT = 8787

# ── sign-in and roles ──
# A signed-in person nobody configured gets no default role of their own: only
# operators are admin, everyone else "user" (policy.json "default" can still set one).
DEFAULT_ROLE = ""
# The runner token (RUNNER_TOKEN) is a non-admin "user".
RUNNER_ROLE = "user"
# On a start with nobody in charge, the first WebUI sign-in within this many minutes
# becomes the operator; later sign-ins never do.
OPERATOR_BOOTSTRAP_MINUTES = 30

# ── audit log (<data>/auth/audit.log): refusals are logged, rotated by size ──
AUDIT_MAX_BYTES = 5_000_000
AUDIT_KEEP = 3

# ── HTTP ──
HSTS = "max-age=31536000"            # sent when BASE_URL is https; this host only
# Per client IP ("N/min", "N/hour" …): see ratelimit.py for the paths of each group.
RATE_LIMITS = {"auth": "30/min", "login": "30/min", "register": "10/hour",
               "hooks": "60/min", "ui": "300/min"}
RATE_LIMIT_AUTH_FAILURES = "20/min"  # 401/403 with credentials on /mcp and /api/v1 …
RATE_LIMIT_FAILURE_COOLDOWN = 300    # … then this many seconds of 429
UI_SESSION_TTL = 12 * 3600           # WebUI session lifetime (seconds)
UI_MAX_BODY_BYTES = 1_000_000
API_MAX_BODY_BYTES = 1_000_000
API_RATE_PER_MIN = 60                # per REST key
WEBHOOK_MAX_BODY_BYTES = 256_000
WEBHOOK_REPLAY_TTL = 600             # seconds a signed delivery id is remembered

# ── size and time limits of the tools ──
FS_VIEW_CEILING_BYTES = 30_000_000
FS_MAX_WRITE_BYTES = 5_000_000
FS_WORKSPACE_QUOTA_BYTES = 2_000_000_000
IMAP_MAX_BODY_BYTES = 40_000
IMAP_MAX_ATTACH_BYTES = 25_000_000
MAIL_MAX_ATTACH_BYTES = 25_000_000
PRINT_MAX_BYTES = 20_000_000
FTP_CURL_MAX_TIME = 300              # seconds per FTPS transfer
AGENT_ONLINE_SECS = 300              # an agent seen this recently is "online" …
AGENT_IDLE_SECS = 1800               # … this recently "idle", else "offline"
SESSION_RETENTION_DAYS = 90          # saved sessions older than this are pruned

# Defaults of the settings an admin changes in the WebUI (admin_settings.py).
ADMIN_DEFAULTS = {"mail_allowed_recipients": "", "tool_profile": "core",
                  "learn_autocapture": True}

# Environment variables earlier releases read and 2.7.1 no longer does: the startup
# log names the ones still set, so they can be deleted. (The three WebUI settings are
# taken over from the environment once, at the first start — see admin_settings.py.)
NO_LONGER_USED = (
    "AUTH_ENFORCE", "ALLOW_PLAINTEXT_VAULT", "OIDC_DEFAULT_ROLE",
    "RUNNER_ROLE", "OPERATOR_BOOTSTRAP_MINUTES", "AUTH_AUDIT_ALL", "AUDIT_MAX_BYTES",
    "AUDIT_KEEP", "HSTS", "RATE_LIMIT", "RATE_LIMIT_AUTH", "RATE_LIMIT_LOGIN",
    "RATE_LIMIT_REGISTER", "RATE_LIMIT_HOOKS", "RATE_LIMIT_UI", "RATE_LIMIT_AUTH_FAILURES",
    "RATE_LIMIT_FAILURE_COOLDOWN", "UI_ENABLED", "UI_SESSION_TTL", "UI_MAX_BODY_BYTES",
    "API_ENABLED", "API_MAX_BODY_BYTES", "API_RATE_PER_MIN", "WEBHOOK_MAX_BODY_BYTES",
    "WEBHOOK_REPLAY_TTL", "FS_VIEW_CEILING_BYTES", "FS_MAX_WRITE_BYTES",
    "FS_WORKSPACE_QUOTA_BYTES", "IMAP_MAX_BODY_BYTES", "IMAP_MAX_ATTACH_BYTES",
    "MAIL_MAX_ATTACH_BYTES", "PRINT_MAX_BYTES", "FTP_CURL_MAX_TIME",
    "CATALOG_CACHE_FILE", "AGENT_ONLINE_SECS", "AGENT_IDLE_SECS", "SESSION_RETENTION_DAYS",
    "SSH_STRICT_HOST_KEYS",
    "MAIL_ALLOWED_RECIPIENTS", "TOOL_PROFILE_DEFAULT", "LEARN_AUTOCAPTURE",
)


# The ONE test hook. The test suite sets this switch for its own process, so the
# thousands of existing tests can still tune a fixed value through the environment.
# Production never sets it — and if it is set anyway, the start says so loudly.
TEST_SWITCH = "AICORTEX_TEST_OVERRIDES"


def test_overrides() -> bool:
    return os.environ.get(TEST_SWITCH, "").strip() == "1"


def knob(name: str, default, cast=None):
    """A built-in value. Only under the test switch may the environment variable of
    the same (pre-2.7.1) name replace it; production always gets `default`."""
    if not test_overrides():
        return default
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    if cast is None:
        return raw
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def flag(name: str, default: bool) -> bool:
    """A built-in on/off value (same test-switch rule as ``knob``)."""
    raw = knob(name, None)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default                     # an unknown value keeps the built-in one


def data_root() -> Path:
    """The data volume: /data (DATA_ROOT, undocumented, only for a test or a checkout)."""
    return Path(os.environ.get("DATA_ROOT") or "/data")


def data_dir(env: str, sub: str) -> Path:
    """The data folder `sub` under the data volume — or the path an install from before
    2.7.1 set explicitly in `env` (e.g. MEMORY_DIR), so it keeps its data."""
    return Path(os.environ.get(env) or data_root() / sub)


def leftovers(environ=None) -> list:
    """The variables of NO_LONGER_USED that are still set (non-empty)."""
    environ = os.environ if environ is None else environ
    return [n for n in NO_LONGER_USED if str(environ.get(n, "")).strip()]


def warn_leftovers(environ=None) -> str:
    """Startup: one log line naming the leftover variables, or nothing — and a loud
    warning when the test switch is set outside the test suite."""
    environ = os.environ if environ is None else environ
    if str(environ.get(TEST_SWITCH, "")).strip() == "1":
        print(f"[AICortex] WARNING: {TEST_SWITCH}=1 is set — built-in security values can be "
              f"changed through the environment. This switch is for the test suite only; "
              f"remove it from a real installation.")
    names = leftovers(environ)
    if not names:
        return ""
    line = ("[AICortex] settings no longer used (fixed since 2.7.1), you can delete them: "
            + ", ".join(names))
    print(line)
    return line
