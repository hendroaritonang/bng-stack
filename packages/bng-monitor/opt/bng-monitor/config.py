"""BNG Monitor Configuration"""
import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BNG_MONITOR_DATA", "/var/lib/bng-monitor")
DB_PATH = os.path.join(DATA_DIR, "bng_monitor.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# Server
HOST = os.environ.get("BNG_MONITOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("BNG_MONITOR_PORT", "8877"))

# Auth
SECRET_KEY = os.environ.get("BNG_MONITOR_SECRET", "bng-monitor-change-me-in-production")
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 hours
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"  # change on first login

# Collection intervals (seconds)
COLLECT_INTERVAL_FAST = 5      # sessions, VPP status
COLLECT_INTERVAL_MEDIUM = 30   # system metrics
COLLECT_INTERVAL_SLOW = 300    # historical snapshots (5 min)

# Alerting
ALERT_SESSION_DROP_THRESHOLD = 0.3  # 30% drop in 1 minute
ALERT_COOLDOWN_SECONDS = 300        # don't repeat same alert for 5 min

# Telegram (optional)
TELEGRAM_BOT_TOKEN = os.environ.get("BNG_MONITOR_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("BNG_MONITOR_TG_CHAT", "")

# Webhook (optional)
WEBHOOK_URL = os.environ.get("BNG_MONITOR_WEBHOOK_URL", "")

# BR discovery
ACCEL_CONF_GLOB = "/etc/accel-ppp-*.conf"

# Commands
VPPCTL = "/usr/local/bin/vppctl"
ACCEL_CMD = "/usr/local/bin/accel-cmd"

# Alert config file (persisted)
ALERT_CONFIG_PATH = os.path.join(DATA_DIR, "alert_config.json")

def load_alert_config():
    """Load alert config from file, or return defaults."""
    defaults = {
        "enabled": True,
        "session_drop_threshold": ALERT_SESSION_DROP_THRESHOLD,
        "cooldown_seconds": ALERT_COOLDOWN_SECONDS,
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_chat_id": TELEGRAM_CHAT_ID,
        "webhook_url": WEBHOOK_URL,
        "checks": {
            "vpp_down": True,
            "br_down": True,
            "session_drop": True,
            "high_cpu": True,
            "high_memory": True,
            "high_exceed_rate": True,
            "no_sessions": False,
            "session_threshold": False,
        },
        "thresholds": {
            "cpu_percent": 90,
            "memory_percent": 90,
            "exceed_rate_percent": 10,
            "session_max": 0,
        }
    }
    if os.path.exists(ALERT_CONFIG_PATH):
        try:
            with open(ALERT_CONFIG_PATH) as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_alert_config(cfg: dict):
    """Persist alert config."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ALERT_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
