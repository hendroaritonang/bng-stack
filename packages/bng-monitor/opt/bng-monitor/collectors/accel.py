"""Accel-PPP data collector — talks to accel-cmd for each BR instance."""
import asyncio
import glob
import os
import re
import time
import logging

from config import ACCEL_CMD, ACCEL_CONF_GLOB

log = logging.getLogger("bng.collector.accel")

# Cached state per-BR
_cache = {
    "instances": {},   # br_name -> {port, vlan, gw_ip, running, pid, sessions: [...], stats: {}, uptime: ""}
    "last_update": 0,
}

# Previous sessions snapshot for disconnect detection
# Key: (br_name, ifname) -> {username, ip, mac, connected_at}
_prev_sessions: dict[tuple[str, str], dict] = {}

# Log file read positions for incremental parsing
_log_positions: dict[str, int] = {}

# Pending disconnect events to be written to DB
_pending_disconnects: list[dict] = []


def discover_instances() -> dict:
    """Read /etc/accel-ppp-*.conf files and extract BR config."""
    instances = {}
    for conf_path in sorted(glob.glob(ACCEL_CONF_GLOB)):
        # Extract br name: /etc/accel-ppp-br1.conf -> br1
        m = re.search(r"accel-ppp-(\w+)\.conf$", conf_path)
        if not m:
            continue
        br_name = m.group(1)

        info = {
            "name": br_name,
            "conf_path": conf_path,
            "cli_port": None,
            "vlan": None,
            "gw_ip": None,
            "interface": None,
        }

        try:
            with open(conf_path) as f:
                content = f.read()

            # CLI port — format: tcp=127.0.0.1:2001
            port_m = re.search(r"^\s*tcp\s*=\s*[\d.]+:(\d+)", content, re.MULTILINE)
            if port_m:
                info["cli_port"] = int(port_m.group(1))
            else:
                # fallback: port=XXXX
                port_m2 = re.search(r"^\s*port\s*=\s*(\d+)", content, re.MULTILINE)
                if port_m2:
                    info["cli_port"] = int(port_m2.group(1))

            # Interface (vlan)
            iface_m = re.search(r"^\s*interface\s*=\s*(\S+)", content, re.MULTILINE)
            if iface_m:
                info["interface"] = iface_m.group(1).split(",")[0]
                vlan_m = re.search(r"vlan(\d+)", info["interface"])
                if vlan_m:
                    info["vlan"] = int(vlan_m.group(1))

            # Gateway IP
            gw_m = re.search(r"^\s*gw-ip-address\s*=\s*([\d.]+)", content, re.MULTILINE)
            if gw_m:
                info["gw_ip"] = gw_m.group(1)

            # Local IP (for PPPoE)
            lip_m = re.search(r"^\s*gw\s*=\s*([\d./]+)", content, re.MULTILINE)
            if lip_m:
                info["local_ip"] = lip_m.group(1)

        except Exception as e:
            log.warning(f"Failed to parse {conf_path}: {e}")

        instances[br_name] = info

    return instances


async def _run(cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, ""
    except Exception as e:
        return -1, str(e)


async def check_br_running(br_name: str) -> tuple[bool, int | None]:
    """Check if accel-ppp@{br_name} is running."""
    rc, out = await _run(f"systemctl is-active accel-ppp@{br_name}.service")
    active = rc == 0 and "active" in out.strip()

    pid = None
    if active:
        rc2, out2 = await _run(f"systemctl show -p MainPID accel-ppp@{br_name}.service")
        pid_m = re.search(r"MainPID=(\d+)", out2)
        if pid_m:
            p = int(pid_m.group(1))
            if p > 0:
                pid = p

    return active, pid


async def collect_sessions(cli_port: int) -> list[dict]:
    """Get sessions from accel-cmd."""
    if cli_port is None:
        return []

    rc, out = await _run(f"{ACCEL_CMD} -p {cli_port} show sessions")
    if rc != 0 or not out.strip():
        return []

    sessions = []
    lines = out.strip().split("\n")

    # Find header line
    header_idx = -1
    for i, line in enumerate(lines):
        if "ifname" in line.lower() or "username" in line.lower():
            header_idx = i
            break

    if header_idx < 0:
        return []

    # Parse header columns
    header = lines[header_idx]
    # Find column positions by splitting on |
    if "|" in header:
        cols = [c.strip().lower() for c in header.split("|")]
        for line in lines[header_idx + 1:]:
            if not line.strip() or line.startswith("-"):
                continue
            vals = [v.strip() for v in line.split("|")]
            session = {}
            for j, col in enumerate(cols):
                if j < len(vals):
                    session[col] = vals[j]
            if session:
                sessions.append(session)
    else:
        # Space-separated format - try to parse
        for line in lines[header_idx + 1:]:
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            parts = line.split()
            if len(parts) >= 4:
                sessions.append({
                    "ifname": parts[0] if len(parts) > 0 else "",
                    "username": parts[1] if len(parts) > 1 else "",
                    "ip": parts[2] if len(parts) > 2 else "",
                    "uptime": parts[3] if len(parts) > 3 else "",
                    "calling-sid": parts[4] if len(parts) > 4 else "",
                    "rate-limit": parts[5] if len(parts) > 5 else "",
                })

    return sessions


async def collect_stats(cli_port: int) -> dict:
    """Get stats from accel-cmd, including parsed RADIUS stats."""
    if cli_port is None:
        return {}

    rc, out = await _run(f"{ACCEL_CMD} -p {cli_port} show stat")
    if rc != 0:
        return {}

    stats = {}
    radius_stats = {}
    in_radius = False

    for line in out.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Detect radius section: "radius(1, 202.162.204.175):"
        radius_m = re.match(r'radius\((\d+),\s*([^)]+)\):', stripped)
        if radius_m:
            in_radius = True
            radius_stats["server_index"] = int(radius_m.group(1))
            radius_stats["server_ip"] = radius_m.group(2).strip()
            continue

        if in_radius:
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()

                if key == "state":
                    radius_stats["state"] = val
                elif key == "fail count":
                    radius_stats["fail_count"] = int(val) if val.isdigit() else 0
                elif key == "request count":
                    radius_stats["request_count"] = int(val) if val.isdigit() else 0
                elif key == "queue length":
                    radius_stats["queue_length"] = int(val) if val.isdigit() else 0
                elif key == "auth sent":
                    radius_stats["auth_sent"] = int(val) if val.isdigit() else 0
                elif key.startswith("auth lost"):
                    # "0/0/0" -> total, 5m, 1m
                    parts = val.split("/")
                    radius_stats["auth_lost_total"] = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
                    radius_stats["auth_lost_5m"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                    radius_stats["auth_lost_1m"] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                elif key.startswith("auth avg query time"):
                    # "0/0 ms" -> 5m, 1m
                    val_clean = val.replace("ms", "").strip()
                    parts = val_clean.split("/")
                    radius_stats["auth_avg_time_5m"] = int(parts[0]) if len(parts) > 0 and parts[0].strip().isdigit() else 0
                    radius_stats["auth_avg_time_1m"] = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
                elif key == "acct sent":
                    radius_stats["acct_sent"] = int(val) if val.isdigit() else 0
                elif key.startswith("acct lost"):
                    parts = val.split("/")
                    radius_stats["acct_lost_total"] = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
                    radius_stats["acct_lost_5m"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                    radius_stats["acct_lost_1m"] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                elif key.startswith("acct avg query time"):
                    val_clean = val.replace("ms", "").strip()
                    parts = val_clean.split("/")
                    radius_stats["acct_avg_time_5m"] = int(parts[0]) if len(parts) > 0 and parts[0].strip().isdigit() else 0
                    radius_stats["acct_avg_time_1m"] = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
                    in_radius = False  # Last radius field
                else:
                    # Other stat line
                    stats[key] = val
            elif not line.startswith(" ") and not line.startswith("\t"):
                in_radius = False
        else:
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                stats[key.strip()] = val.strip()

    # Also extract PPPoE stats
    pppoe_stats = {}
    for key in ["recv PADI", "drop PADI", "sent PADO", "sent PADS", "filtered"]:
        if key in stats:
            try:
                pppoe_stats[key.replace(" ", "_").lower()] = int(stats[key])
            except ValueError:
                pass
    # Parse "recv PADR(dup): 4(0)"
    padr_val = stats.get("recv PADR(dup)", "")
    padr_m = re.match(r"(\d+)\((\d+)\)", padr_val)
    if padr_m:
        pppoe_stats["recv_padr"] = int(padr_m.group(1))
        pppoe_stats["recv_padr_dup"] = int(padr_m.group(2))

    stats["_radius"] = radius_stats
    stats["_pppoe"] = pppoe_stats

    return stats


async def collect_instance(br_name: str, info: dict) -> dict:
    """Collect all data for a single BR instance."""
    running, pid = await check_br_running(br_name)

    result = {
        **info,
        "running": running,
        "pid": pid,
        "sessions": [],
        "session_count": 0,
        "stats": {},
    }

    if running and info.get("cli_port"):
        sessions, stats = await asyncio.gather(
            collect_sessions(info["cli_port"]),
            collect_stats(info["cli_port"]),
        )
        result["sessions"] = sessions
        result["session_count"] = len(sessions)
        result["stats"] = stats

    return result


async def restart_br(br_name: str) -> dict:
    """Restart a BR instance."""
    rc, out = await _run(f"sudo systemctl restart accel-ppp@{br_name}.service", timeout=30)
    return {
        "success": rc == 0,
        "output": out.strip(),
        "br_name": br_name,
    }


async def reload_br(br_name: str) -> dict:
    """Graceful reload a BR instance via SIGUSR1.

    SIGUSR1 tells accel-ppp to finish existing sessions gracefully
    and re-read config without dropping active sessions immediately.
    """
    # Get PID first
    running, pid = await check_br_running(br_name)
    if not running or not pid:
        return {"success": False, "output": f"{br_name} is not running", "br_name": br_name}

    rc, out = await _run(f"sudo kill -USR1 {pid}", timeout=10)
    return {
        "success": rc == 0,
        "output": out.strip() if out.strip() else f"SIGUSR1 sent to PID {pid}",
        "br_name": br_name,
        "pid": pid,
    }


async def save_br_config(br_name: str, config_content: str) -> dict:
    """Save config file for a BR instance.

    Writes to /etc/accel-ppp-{br_name}.conf with a backup.
    """
    conf_path = f"/etc/accel-ppp-{br_name}.conf"
    backup_path = f"{conf_path}.bak"

    try:
        # Create backup via sudo
        rc, out = await _run(f"sudo cp {conf_path} {backup_path}", timeout=5)
        if rc != 0:
            log.warning(f"Failed to backup {conf_path}: {out}")

        # Write new config to temp file, then sudo move
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as tmp:
            tmp.write(config_content)
            tmp_path = tmp.name

        rc, out = await _run(f"sudo cp {tmp_path} {conf_path} && rm -f {tmp_path}", timeout=5)
        if rc != 0:
            # Attempt restore from backup
            await _run(f"sudo cp {backup_path} {conf_path}", timeout=5)
            return {"success": False, "output": f"Failed to write config: {out}"}

        return {"success": True, "output": f"Config saved to {conf_path} (backup at {backup_path})"}
    except Exception as e:
        return {"success": False, "output": str(e)}


async def get_br_logs_file(br_name: str, lines: int = 200, grep_filter: str = "") -> str:
    """Get recent log lines from accel-ppp log file with optional grep filter."""
    log_path = f"/var/log/accel-ppp/{br_name}.log"

    if grep_filter:
        # Use grep with context
        safe_filter = grep_filter.replace("'", "'\\''")
        cmd = f"grep -i '{safe_filter}' {log_path} | tail -n {lines}"
    else:
        cmd = f"tail -n {lines} {log_path}"

    rc, out = await _run(cmd, timeout=15)
    if rc != 0 and not out.strip():
        return f"No log data (file may not exist: {log_path})"
    return out


def compute_health_score(br_name: str, inst: dict, vpp_interfaces: dict) -> dict:
    """Compute a health score (0-100) for a BR instance.

    Factors:
    - Running status (0 if down)
    - Session count vs capacity
    - Error rate from VPP interfaces (drops, errors)
    - Uptime stability
    """
    if not inst.get("running", False):
        return {"score": 0, "status": "down", "factors": {"running": False}}

    score = 100
    factors = {"running": True}

    # Session health: having sessions is good, but not required
    session_count = inst.get("session_count", 0)
    factors["sessions"] = session_count

    # Check VPP interface errors/drops for this BR's sessions
    total_drops = 0
    total_errors = 0
    total_packets = 0
    for sess in inst.get("sessions", []):
        ifname = sess.get("ifname", "")
        iface = vpp_interfaces.get(ifname, {})
        total_drops += iface.get("drops", 0)
        total_errors += iface.get("rx_errors", 0) + iface.get("tx_errors", 0)
        total_packets += iface.get("rx_packets", 0) + iface.get("tx_packets", 0)

    factors["total_drops"] = total_drops
    factors["total_errors"] = total_errors
    factors["total_packets"] = total_packets

    # Penalize for drops/errors
    if total_packets > 0:
        drop_rate = total_drops / total_packets
        error_rate = total_errors / total_packets
        if drop_rate > 0.05:
            score -= 30
        elif drop_rate > 0.01:
            score -= 15
        elif drop_rate > 0:
            score -= 5

        if error_rate > 0.01:
            score -= 20
        elif error_rate > 0:
            score -= 5

    factors["drop_rate"] = round(total_drops / max(total_packets, 1) * 100, 2)
    factors["error_rate"] = round(total_errors / max(total_packets, 1) * 100, 2)

    # Determine status label
    if score >= 90:
        status = "healthy"
    elif score >= 70:
        status = "degraded"
    elif score >= 50:
        status = "warning"
    else:
        status = "critical"

    return {"score": max(score, 0), "status": status, "factors": factors}


async def disconnect_session(cli_port: int, ifname: str) -> dict:
    """Disconnect a specific session via accel-cmd."""
    rc, out = await _run(f"{ACCEL_CMD} -p {cli_port} terminate if {ifname}", timeout=10)
    return {
        "success": rc == 0,
        "output": out.strip(),
        "ifname": ifname,
    }


async def get_br_logs(br_name: str, lines: int = 100) -> str:
    """Get recent journal logs for a BR."""
    rc, out = await _run(
        f"journalctl -u accel-ppp@{br_name}.service -n {lines} --no-pager",
        timeout=15
    )
    return out if rc == 0 else f"Failed to get logs: {out}"


async def get_br_config(br_name: str) -> str:
    """Read config file for a BR."""
    conf_path = f"/etc/accel-ppp-{br_name}.conf"
    try:
        with open(conf_path) as f:
            return f.read()
    except Exception as e:
        return f"Error reading config: {e}"


def detect_disconnects(new_instances: dict):
    """Compare current sessions to previous snapshot, detect disappearances.

    When a session that was in _prev_sessions is no longer present,
    record it as a disconnect event.
    """
    global _prev_sessions

    current_sessions: dict[tuple[str, str], dict] = {}

    for br_name, inst in new_instances.items():
        for sess in inst.get("sessions", []):
            ifname = sess.get("ifname", "")
            if not ifname:
                continue
            key = (br_name, ifname)
            current_sessions[key] = {
                "username": sess.get("username", ""),
                "ip": sess.get("ip", sess.get("address", "")),
                "mac": sess.get("calling-sid", sess.get("mac", "")),
                "uptime": sess.get("uptime", ""),
                "seen_at": time.time(),
            }

    # Find sessions that disappeared
    for key, prev in _prev_sessions.items():
        if key not in current_sessions:
            br_name, ifname = key
            uptime_str = prev.get("uptime", "")
            duration = _parse_uptime(uptime_str)

            _pending_disconnects.append({
                "ts": time.time(),
                "br_name": br_name,
                "username": prev.get("username", ""),
                "ip": prev.get("ip", ""),
                "mac": prev.get("mac", ""),
                "session_id": ifname,
                "reason": "session disappeared",
                "duration": duration,
            })
            log.info(f"Disconnect detected: {prev.get('username', '?')}@{br_name} ({ifname})")

    _prev_sessions = current_sessions


def _parse_uptime(uptime_str: str) -> float:
    """Parse accel-cmd uptime string like '01:47:29' or '2d 03:15:22' to seconds."""
    if not uptime_str:
        return 0
    try:
        days = 0
        time_part = uptime_str
        # Handle "Xd HH:MM:SS"
        d_m = re.match(r'(\d+)d\s+(.+)', uptime_str)
        if d_m:
            days = int(d_m.group(1))
            time_part = d_m.group(2)
        parts = time_part.split(":")
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0


def get_pending_disconnects() -> list[dict]:
    """Return and clear pending disconnect events for DB flush."""
    global _pending_disconnects
    events = _pending_disconnects[:]
    _pending_disconnects = []
    return events


async def parse_log_disconnects(br_name: str, log_path: str) -> list[dict]:
    """Parse accel-ppp log file for PADT (disconnect) and terminate events.

    Log format examples:
      [2026-04-16 08:55:29]: info: vlan100: recv [PPPoE PADT ...]
      [2026-04-15 14:28:23]: info: terminate, sig = 15

    We track file position for incremental reads.
    """
    global _log_positions

    events = []
    if not os.path.exists(log_path):
        return events

    try:
        file_size = os.path.getsize(log_path)
        last_pos = _log_positions.get(log_path, 0)

        # If file was truncated/rotated, reset position
        if file_size < last_pos:
            last_pos = 0

        # Don't read more than 512KB at a time
        if file_size - last_pos > 524288:
            last_pos = file_size - 524288

        if file_size <= last_pos:
            return events

        with open(log_path, 'r', errors='replace') as f:
            f.seek(last_pos)
            new_data = f.read()
            _log_positions[log_path] = f.tell()

        for line in new_data.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Parse PADT received (client disconnected)
            # [2026-04-16 08:55:29]: info: vlan100: recv [PPPoE PADT 48:a9:8a:11:23:a1 => a2:08:78:df:22:02 sid=0001]
            padt_m = re.match(
                r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*recv \[PPPoE PADT\s+([0-9a-fA-F:]+)\s+=>\s+[0-9a-fA-F:]+\s+sid=([0-9a-fA-F]+)',
                line
            )
            if padt_m:
                events.append({
                    "ts_str": padt_m.group(1),
                    "br_name": br_name,
                    "mac": padt_m.group(2),
                    "session_id": f"0x{padt_m.group(3)}",
                    "reason": "PADT received (client disconnect)",
                })
                continue

            # Parse terminate signal
            # [2026-04-15 14:28:23]: info: terminate, sig = 15
            term_m = re.match(
                r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*terminate,\s*sig\s*=\s*(\d+)',
                line
            )
            if term_m:
                sig = term_m.group(2)
                reason = f"terminate sig={sig}" + (" (SIGTERM, restart)" if sig == "15" else "")
                events.append({
                    "ts_str": term_m.group(1),
                    "br_name": br_name,
                    "mac": "",
                    "session_id": "",
                    "reason": reason,
                })
                continue

            # Parse authentication failures
            # [2026-04-14 12:44:09]: info: testing: authentication failed
            auth_m = re.match(
                r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*info:\s+(\S+):\s+authentication failed',
                line
            )
            if auth_m:
                events.append({
                    "ts_str": auth_m.group(1),
                    "br_name": br_name,
                    "username": auth_m.group(2),
                    "mac": "",
                    "session_id": "",
                    "reason": "authentication failed",
                })

    except Exception as e:
        log.warning(f"Error parsing log {log_path}: {e}")

    return events


async def scan_logs_for_disconnects() -> list[dict]:
    """Scan all BR log files for new disconnect events."""
    all_events = []
    instances_info = discover_instances()

    for br_name, info in instances_info.items():
        log_path = f"/var/log/accel-ppp/{br_name}.log"
        events = await parse_log_disconnects(br_name, log_path)
        all_events.extend(events)

    return all_events


async def collect_all():
    """Collect data from all BR instances."""
    instances_info = discover_instances()

    tasks = []
    for br_name, info in instances_info.items():
        tasks.append(collect_instance(br_name, info))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    instances = {}
    for i, (br_name, _) in enumerate(instances_info.items()):
        if isinstance(results[i], Exception):
            log.error(f"Failed to collect {br_name}: {results[i]}")
            instances[br_name] = {**instances_info[br_name], "running": False, "sessions": [], "session_count": 0, "stats": {}, "pid": None}
        else:
            instances[br_name] = results[i]

    # Detect disconnects by comparing to previous session snapshot
    detect_disconnects(instances)

    _cache["instances"] = instances
    _cache["last_update"] = time.time()
    return _cache


def get_cache() -> dict:
    return _cache.copy()
