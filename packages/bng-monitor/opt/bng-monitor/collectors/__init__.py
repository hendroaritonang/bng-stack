"""Alerting engine — monitors for anomalies and sends notifications."""
import asyncio
import time
import logging
import json
from urllib.request import urlopen, Request
from urllib.error import URLError

from config import load_alert_config, save_alert_config
from database import get_db

log = logging.getLogger("bng.alerter")

# In-memory state
_last_session_counts = {}   # br_name -> count
_last_alert_times = {}      # category -> timestamp
_alert_config = None


def get_alert_config() -> dict:
    global _alert_config
    if _alert_config is None:
        _alert_config = load_alert_config()
    return _alert_config


def update_alert_config(cfg: dict):
    global _alert_config
    _alert_config = cfg
    save_alert_config(cfg)


async def record_alert(severity: str, category: str, title: str, message: str):
    """Store alert in DB and send notifications."""
    cfg = get_alert_config()
    if not cfg.get("enabled", True):
        return

    # Cooldown check
    cooldown = cfg.get("cooldown_seconds", 300)
    now = time.time()
    last = _last_alert_times.get(category, 0)
    if now - last < cooldown:
        return

    _last_alert_times[category] = now

    # Store in DB
    db = await get_db()
    await db.execute(
        "INSERT INTO alerts (ts, severity, category, title, message) VALUES (?, ?, ?, ?, ?)",
        (now, severity, category, title, message)
    )
    await db.commit()

    log.warning(f"ALERT [{severity}] {category}: {title} — {message}")

    # Send notifications in background
    asyncio.create_task(_send_notifications(severity, title, message, cfg))


async def _send_notifications(severity: str, title: str, message: str, cfg: dict):
    """Send Telegram and/or webhook notifications."""
    loop = asyncio.get_event_loop()

    # Telegram
    tg_token = cfg.get("telegram_bot_token", "")
    tg_chat = cfg.get("telegram_chat_id", "")
    if tg_token and tg_chat:
        try:
            await loop.run_in_executor(None, _send_telegram, tg_token, tg_chat, severity, title, message)
        except Exception as e:
            log.error(f"Telegram send failed: {e}")

    # Webhook
    webhook_url = cfg.get("webhook_url", "")
    if webhook_url:
        try:
            await loop.run_in_executor(None, _send_webhook, webhook_url, severity, title, message)
        except Exception as e:
            log.error(f"Webhook send failed: {e}")


def _send_telegram(token: str, chat_id: str, severity: str, title: str, message: str):
    icon = {"critical": "\u274c", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}.get(severity, "\u2753")
    text = f"{icon} *BNG Alert — {severity.upper()}*\n\n*{title}*\n{message}\n\n_{time.strftime('%Y-%m-%d %H:%M:%S')}_"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=10)
    except URLError as e:
        log.error(f"Telegram API error: {e}")


def _send_webhook(webhook_url: str, severity: str, title: str, message: str):
    payload = {
        "severity": severity,
        "title": title,
        "message": message,
        "timestamp": time.time(),
        "source": "bng-monitor",
    }
    data = json.dumps(payload).encode()
    req = Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=10)
    except URLError as e:
        log.error(f"Webhook error: {e}")


async def evaluate_alerts(vpp_cache: dict, accel_cache: dict, sys_cache: dict):
    """Check all alert conditions."""
    cfg = get_alert_config()
    if not cfg.get("enabled", True):
        return

    checks = cfg.get("checks", {})
    thresholds = cfg.get("thresholds", {})

    # 1. VPP down
    if checks.get("vpp_down", True):
        if not vpp_cache.get("vpp_running", False):
            await record_alert("critical", "vpp_down", "VPP is DOWN", "vpp_main process is not running. All PPPoE sessions are affected.")

    # 2. BR down
    if checks.get("br_down", True):
        for br_name, info in accel_cache.get("instances", {}).items():
            if not info.get("running", False):
                await record_alert("warning", f"br_down_{br_name}", f"BR {br_name} is DOWN", f"accel-ppp@{br_name} service is not active.")

    # 3. Session drop anomaly
    if checks.get("session_drop", True):
        threshold = cfg.get("session_drop_threshold", 0.3)
        for br_name, info in accel_cache.get("instances", {}).items():
            current = info.get("session_count", 0)
            prev = _last_session_counts.get(br_name)
            if prev is not None and prev > 0:
                drop_pct = (prev - current) / prev
                if drop_pct >= threshold and (prev - current) >= 2:
                    await record_alert(
                        "critical", f"session_drop_{br_name}",
                        f"Session drop on {br_name}",
                        f"Sessions dropped from {prev} to {current} ({drop_pct*100:.0f}% drop)"
                    )
            _last_session_counts[br_name] = current

    # 4. High CPU
    if checks.get("high_cpu", True):
        cpu_thresh = thresholds.get("cpu_percent", 90)
        if sys_cache.get("cpu_percent", 0) > cpu_thresh:
            await record_alert("warning", "high_cpu", "High CPU Usage", f"CPU at {sys_cache['cpu_percent']:.1f}% (threshold: {cpu_thresh}%)")

    # 5. High memory
    if checks.get("high_memory", True):
        mem_thresh = thresholds.get("memory_percent", 90)
        if sys_cache.get("mem_percent", 0) > mem_thresh:
            await record_alert("warning", "high_memory", "High Memory Usage", f"Memory at {sys_cache['mem_percent']:.1f}% (threshold: {mem_thresh}%)")

    # 6. High policer exceed rate
    if checks.get("high_exceed_rate", True):
        exceed_thresh = thresholds.get("exceed_rate_percent", 10)
        policers = vpp_cache.get("policers", [])
        for pol in policers:
            total_pkts = (pol.get("conform_packets", 0) + pol.get("exceed_packets", 0) + pol.get("violate_packets", 0))
            if total_pkts > 0:
                exceed_pct = pol.get("exceed_packets", 0) / total_pkts * 100
                violate_pct = pol.get("violate_packets", 0) / total_pkts * 100
                pol_name = pol.get("name", "unknown")
                direction = pol.get("direction", "?")
                if exceed_pct >= exceed_thresh:
                    await record_alert(
                        "warning", f"exceed_{pol_name}",
                        f"High exceed rate on policer ({direction})",
                        f"Policer {pol_name}: exceed {exceed_pct:.1f}% of traffic (threshold: {exceed_thresh}%). "
                        f"Conform: {pol.get('conform_packets', 0)}, Exceed: {pol.get('exceed_packets', 0)}, Violate: {pol.get('violate_packets', 0)}"
                    )
                if violate_pct > 0:
                    await record_alert(
                        "critical", f"violate_{pol_name}",
                        f"Policer dropping traffic ({direction})",
                        f"Policer {pol_name}: {violate_pct:.1f}% of traffic dropped (violate). "
                        f"Violate packets: {pol.get('violate_packets', 0)}"
                    )

    # 7. No sessions on running BR
    if checks.get("no_sessions", True):
        for br_name, info in accel_cache.get("instances", {}).items():
            if info.get("running", False) and info.get("session_count", 0) == 0:
                await record_alert(
                    "info", f"no_sessions_{br_name}",
                    f"No sessions on {br_name}",
                    f"BR {br_name} is running but has 0 active sessions."
                )

    # 8. Session count exceeds max threshold
    if checks.get("session_threshold", True):
        session_max = thresholds.get("session_max", 0)
        if session_max > 0:
            total_sessions = sum(
                info.get("session_count", 0) for info in accel_cache.get("instances", {}).values()
            )
            if total_sessions >= session_max:
                await record_alert(
                    "warning", "session_threshold",
                    "Session count threshold reached",
                    f"Total sessions ({total_sessions}) reached or exceeded the max threshold ({session_max})."
                )


async def record_disconnect(br_name: str, username: str, ip: str, mac: str, session_id: str, reason: str, duration: float = 0):
    """Log a session disconnect event."""
    db = await get_db()
    await db.execute(
        "INSERT INTO disconnect_log (ts, br_name, username, ip, mac, session_id, reason, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), br_name, username, ip, mac, session_id, reason, duration)
    )
    await db.commit()
