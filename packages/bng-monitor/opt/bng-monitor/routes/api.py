"""API routes for BNG Monitor."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import json
import time

from auth import decode_token, verify_password, hash_password, create_access_token
from database import get_db
from collectors import vpp, accel, system as sys_collector
from collectors import evaluate_alerts, get_alert_config, update_alert_config

security = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/api")


# --- Auth dependency ---

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_info = decode_token(credentials.credentials)
    if user_info is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user_info


def require_admin(user: dict):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


# --- Auth routes ---

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

@router.post("/auth/login")
async def login(req: LoginRequest):
    db = await get_db()
    cursor = await db.execute("SELECT username, hashed_password, role, allowed_pages FROM users WHERE username = ?", (req.username,))
    row = await cursor.fetchone()
    if not row or not verify_password(req.password, row[1]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    role = row[2] or "admin"
    try:
        allowed_pages = json.loads(row[3]) if row[3] else []
    except (json.JSONDecodeError, TypeError):
        allowed_pages = []
    token = create_access_token(row[0], role=role)
    return {"access_token": token, "token_type": "bearer", "username": row[0], "role": role, "allowed_pages": allowed_pages}

@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    db = await get_db()
    cursor = await db.execute("SELECT hashed_password FROM users WHERE username = ?", (user["username"],))
    row = await cursor.fetchone()
    if not row or not verify_password(req.old_password, row[0]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new_hash = hash_password(req.new_password)
    await db.execute("UPDATE users SET hashed_password = ? WHERE username = ?", (new_hash, user["username"]))
    await db.commit()
    return {"status": "ok"}


# --- Dashboard ---

@router.get("/dashboard")
async def dashboard(user: dict = Depends(get_current_user)):
    vpp_data = vpp.get_cache()
    accel_data = accel.get_cache()
    sys_data = sys_collector.get_cache()

    total_sessions = sum(
        inst.get("session_count", 0)
        for inst in accel_data.get("instances", {}).values()
    )

    brs = []
    for br_name, inst in accel_data.get("instances", {}).items():
        brs.append({
            "name": br_name,
            "running": inst.get("running", False),
            "pid": inst.get("pid"),
            "session_count": inst.get("session_count", 0),
            "vlan": inst.get("vlan"),
            "gw_ip": inst.get("gw_ip"),
            "interface": inst.get("interface"),
        })

    # Recent alerts count
    db = await get_db()
    one_hour_ago = time.time() - 3600
    cursor = await db.execute(
        "SELECT COUNT(*) FROM alerts WHERE ts > ? AND acknowledged = 0", (one_hour_ago,)
    )
    unack_alerts = (await cursor.fetchone())[0]

    return {
        "vpp": {
            "running": vpp_data.get("vpp_running", False),
            "pid": vpp_data.get("vpp_pid"),
            "version": vpp_data.get("vpp_version", ""),
            "uptime": vpp_data.get("vpp_uptime", ""),
            "total_pppoe_sessions": vpp_data.get("pppoe_summary", {}).get("total", 0),
        },
        "system": {
            "cpu_percent": sys_data.get("cpu_percent", 0),
            "mem_percent": sys_data.get("mem_percent", 0),
            "mem_used_mb": sys_data.get("mem_used_mb", 0),
            "mem_total_mb": sys_data.get("mem_total_mb", 0),
            "disk_percent": sys_data.get("disk_percent", 0),
            "load_avg": sys_data.get("load_avg", [0, 0, 0]),
            "uptime_seconds": sys_data.get("uptime_seconds", 0),
            "vpp_rss_mb": sys_data.get("vpp_rss_mb", 0),
        },
        "brs": brs,
        "total_sessions": total_sessions,
        "unack_alerts": unack_alerts,
        "last_update": max(
            vpp_data.get("last_update", 0),
            accel_data.get("last_update", 0),
            sys_data.get("last_update", 0),
        ),
    }


# --- Sessions ---

@router.get("/sessions")
async def list_sessions(
    br: str = Query(None, description="Filter by BR name"),
    search: str = Query(None, description="Search username/IP/MAC"),
    user: dict = Depends(get_current_user),
):
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()

    all_sessions = []
    for br_name, inst in accel_data.get("instances", {}).items():
        if br and br != br_name:
            continue
        for sess in inst.get("sessions", []):
            entry = {
                "br": br_name,
                "ifname": sess.get("ifname", ""),
                "username": sess.get("username", ""),
                "ip": sess.get("ip", sess.get("address", "")),
                "mac": sess.get("calling-sid", sess.get("mac", "")),
                "uptime": sess.get("uptime", ""),
                "rate_limit": sess.get("rate-limit", sess.get("rate", "")),
                "state": sess.get("state", "active"),
            }

            # Enrich with VPP data
            ifname = entry["ifname"]
            vpp_iface = vpp_data.get("interfaces", {}).get(ifname, {})
            if vpp_iface:
                entry["vpp_state"] = vpp_iface.get("state", "unknown")
                entry["rx_bytes"] = vpp_iface.get("rx_bytes", 0)
                entry["tx_bytes"] = vpp_iface.get("tx_bytes", 0)
                entry["rx_packets"] = vpp_iface.get("rx_packets", 0)
                entry["tx_packets"] = vpp_iface.get("tx_packets", 0)
                entry["sw_if_index"] = vpp_iface.get("sw_if_index")

            # Match VPP PPPoE session
            for vs in vpp_data.get("sessions", []):
                if vs.get("client_ip") == entry["ip"] or vs.get("session_name") == ifname:
                    entry["vpp_session_id"] = vs.get("session_id")
                    entry["vpp_encap_if"] = vs.get("encap_if_index")
                    break

            if search:
                s = search.lower()
                if not any(s in str(v).lower() for v in [entry.get("username"), entry.get("ip"), entry.get("mac"), entry.get("ifname")]):
                    continue

            all_sessions.append(entry)

    return {
        "sessions": all_sessions,
        "count": len(all_sessions),
    }


@router.get("/sessions/{br_name}/{ifname}")
async def session_detail(br_name: str, ifname: str, user: dict = Depends(get_current_user)):
    """Get detailed info for a specific session."""
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()

    inst = accel_data.get("instances", {}).get(br_name)
    if not inst:
        raise HTTPException(status_code=404, detail=f"BR {br_name} not found")

    session = None
    for s in inst.get("sessions", []):
        if s.get("ifname") == ifname:
            session = s
            break

    if not session:
        raise HTTPException(status_code=404, detail=f"Session {ifname} not found in {br_name}")

    result = {
        "br": br_name,
        "session": session,
        "vpp_interface": vpp_data.get("interfaces", {}).get(ifname, {}),
        "vpp_session": None,
        "policers": [],
    }

    # Find matching VPP session
    ip = session.get("ip", session.get("address", ""))
    for vs in vpp_data.get("sessions", []):
        if vs.get("client_ip") == ip or vs.get("session_name") == ifname:
            result["vpp_session"] = vs
            break

    # Find matching policers by sw_if_index
    result["policers"] = vpp.find_policers_for_interface(ifname)

    # Disconnect history
    db = await get_db()
    username = session.get("username", "")
    cursor = await db.execute(
        "SELECT * FROM disconnect_log WHERE username = ? ORDER BY ts DESC LIMIT 20",
        (username,)
    )
    rows = await cursor.fetchall()
    result["disconnect_history"] = [
        {
            "ts": r[1], "br_name": r[2], "username": r[3], "ip": r[4],
            "mac": r[5], "session_id": r[6], "reason": r[7], "duration": r[8]
        } for r in rows
    ]

    return result


# --- Trace / Debug ---

class PingRequest(BaseModel):
    destination: str
    source: str = ""
    count: int = 3

@router.post("/trace/ping")
async def trace_ping(req: PingRequest, user: dict = Depends(get_current_user)):
    """VPP ping to a subscriber IP.

    VPP ping source must be an interface name (e.g. loop100), not an IP.
    Auto-detect: find which BR subnet the destination belongs to, use its loopback.
    User can also pass interface name directly.
    """
    source = req.source.strip()

    if not source:
        # Auto-detect loopback from destination IP matching BR subnet
        accel_data = accel.get_cache()
        dest = req.destination.strip()
        for br_name, inst in accel_data.get("instances", {}).items():
            gw = inst.get("gw_ip", "")
            if not gw:
                continue
            # Check if dest is in same /24 as gateway (simple heuristic)
            gw_prefix = ".".join(gw.split(".")[:3])
            dest_prefix = ".".join(dest.split(".")[:3])
            if gw_prefix == dest_prefix:
                # Map vlan to loopback: vlan100->loop100, vlan200->loop101, etc.
                vlan = inst.get("vlan")
                if vlan:
                    source = f"loop{vlan // 100 + 99}"  # vlan100->loop100, vlan200->loop101, vlan300->loop102
                break

        # Fallback: try first available loopback
        if not source:
            vpp_data = vpp.get_cache()
            for iface_name in vpp_data.get("interfaces", {}):
                if iface_name.startswith("loop") and iface_name != "local0":
                    source = iface_name
                    break

    if not source:
        raise HTTPException(status_code=400, detail="No source interface available. Specify a loopback interface name (e.g. loop100)")
    result = await vpp.vpp_ping(source, req.destination, req.count)
    return result

@router.get("/trace/traffic/{ifname}")
async def trace_traffic(ifname: str, user: dict = Depends(get_current_user)):
    """Get real-time traffic stats for a session interface."""
    vpp_data = vpp.get_cache()
    iface = vpp_data.get("interfaces", {}).get(ifname)
    if not iface:
        raise HTTPException(status_code=404, detail=f"Interface {ifname} not found")
    return iface

@router.get("/trace/policer/{ifname}")
async def trace_policer(ifname: str, user: dict = Depends(get_current_user)):
    """Get policer info for a session interface.

    Accepts interface name (e.g. 'testing') or direct policer name.
    Looks up sw_if_index from cached interfaces, then finds policers
    with matching _<sw_if_index>_ in their name pattern:
      vyos_<br>_<sw_if_index>_<rate>_<burst>_<direction>
    """
    matching = vpp.find_policers_for_interface(ifname)
    return {"policers": matching, "lookup": ifname}

@router.get("/trace/disconnects")
async def trace_disconnects(
    username: str = Query(None),
    br: str = Query(None),
    limit: int = Query(50, le=500),
    user: dict = Depends(get_current_user),
):
    """Get disconnect history."""
    db = await get_db()
    query = "SELECT * FROM disconnect_log WHERE 1=1"
    params = []
    if username:
        query += " AND username LIKE ?"
        params.append(f"%{username}%")
    if br:
        query += " AND br_name = ?"
        params.append(br)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {
        "disconnects": [
            {"id": r[0], "ts": r[1], "br_name": r[2], "username": r[3], "ip": r[4],
             "mac": r[5], "session_id": r[6], "reason": r[7], "duration": r[8]}
            for r in rows
        ]
    }


# --- BR Management ---

@router.get("/br")
async def list_brs(user: dict = Depends(get_current_user)):
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()
    instances = accel_data.get("instances", {})
    # Attach health score to each instance
    for br_name, inst in instances.items():
        inst["health"] = accel.compute_health_score(br_name, inst, vpp_data.get("interfaces", {}))
    return {"instances": instances}

@router.get("/br/{br_name}")
async def get_br(br_name: str, user: dict = Depends(get_current_user)):
    accel_data = accel.get_cache()
    inst = accel_data.get("instances", {}).get(br_name)
    if not inst:
        raise HTTPException(status_code=404, detail=f"BR {br_name} not found")
    return inst

@router.post("/br/{br_name}/restart")
async def restart_br_route(br_name: str, user: dict = Depends(get_current_user)):
    result = await accel.restart_br(br_name)
    return result

@router.post("/br/{br_name}/reload")
async def reload_br_route(br_name: str, user: dict = Depends(get_current_user)):
    """Graceful reload via SIGUSR1 — re-reads config without dropping sessions."""
    result = await accel.reload_br(br_name)
    return result

@router.post("/sessions/{br_name}/{ifname}/disconnect")
async def disconnect_session(br_name: str, ifname: str, user: dict = Depends(get_current_user)):
    """Disconnect a specific session via accel-cmd."""
    accel_data = accel.get_cache()
    inst = accel_data.get("instances", {}).get(br_name)
    if not inst:
        raise HTTPException(status_code=404, detail=f"BR {br_name} not found")
    cli_port = inst.get("cli_port")
    if not cli_port:
        raise HTTPException(status_code=400, detail=f"No CLI port for {br_name}")

    result = await accel.disconnect_session(cli_port, ifname)
    return result

@router.get("/br/{br_name}/logs")
async def get_br_logs(br_name: str, lines: int = Query(100, le=1000), user: dict = Depends(get_current_user)):
    logs = await accel.get_br_logs(br_name, lines)
    return {"br_name": br_name, "logs": logs}

@router.get("/br/{br_name}/logs/file")
async def get_br_logs_file(
    br_name: str,
    lines: int = Query(200, le=2000),
    grep: str = Query("", description="Filter log lines"),
    user: dict = Depends(get_current_user),
):
    """Get log lines from accel-ppp log file with optional grep filter."""
    logs = await accel.get_br_logs_file(br_name, lines, grep)
    return {"br_name": br_name, "logs": logs, "filter": grep}

@router.get("/br/{br_name}/config")
async def get_br_config(br_name: str, user: dict = Depends(get_current_user)):
    config = await accel.get_br_config(br_name)
    return {"br_name": br_name, "config": config}

class SaveConfigRequest(BaseModel):
    config: str

@router.put("/br/{br_name}/config")
async def save_br_config(br_name: str, req: SaveConfigRequest, user: dict = Depends(get_current_user)):
    """Save config file for a BR instance (creates backup first)."""
    result = await accel.save_br_config(br_name, req.config)
    return result

@router.get("/br/{br_name}/health")
async def get_br_health(br_name: str, user: dict = Depends(get_current_user)):
    """Get health score for a BR instance."""
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()
    inst = accel_data.get("instances", {}).get(br_name)
    if not inst:
        raise HTTPException(status_code=404, detail=f"BR {br_name} not found")
    health = accel.compute_health_score(br_name, inst, vpp_data.get("interfaces", {}))
    return {"br_name": br_name, **health}


# --- Alerts ---

@router.get("/alerts")
async def list_alerts(
    limit: int = Query(50, le=500),
    unack_only: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    db = await get_db()
    query = "SELECT * FROM alerts"
    params = []
    if unack_only:
        query += " WHERE acknowledged = 0"
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {
        "alerts": [
            {"id": r[0], "ts": r[1], "severity": r[2], "category": r[3],
             "title": r[4], "message": r[5], "acknowledged": bool(r[6]),
             "ack_at": r[7], "ack_by": r[8]}
            for r in rows
        ]
    }

@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, user: dict = Depends(get_current_user)):
    db = await get_db()
    await db.execute(
        "UPDATE alerts SET acknowledged = 1, ack_at = ?, ack_by = ? WHERE id = ?",
        (time.time(), user["username"], alert_id)
    )
    await db.commit()
    return {"status": "ok"}

@router.get("/alerts/config")
async def get_alerts_config(user: dict = Depends(get_current_user)):
    return get_alert_config()

class AlertConfigUpdate(BaseModel):
    enabled: bool = True
    session_drop_threshold: float = 0.3
    cooldown_seconds: int = 300
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    checks: dict = {}
    thresholds: dict = {}

@router.put("/alerts/config")
async def put_alerts_config(req: AlertConfigUpdate, user: dict = Depends(get_current_user)):
    cfg = req.model_dump()
    update_alert_config(cfg)
    return {"status": "ok", "config": cfg}


# --- History ---

@router.get("/history/sessions")
async def history_sessions(
    hours: int = Query(24, le=168),
    br: str = Query(None),
    user: dict = Depends(get_current_user),
):
    db = await get_db()
    since = time.time() - (hours * 3600)
    query = "SELECT ts, br_name, session_count, rx_bytes, tx_bytes FROM history_snapshots WHERE ts > ?"
    params = [since]
    if br:
        query += " AND br_name = ?"
        params.append(br)
    query += " ORDER BY ts ASC"
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {
        "data": [
            {"ts": r[0], "br": r[1], "sessions": r[2], "rx_bytes": r[3], "tx_bytes": r[4]}
            for r in rows
        ]
    }

@router.get("/history/system")
async def history_system(
    hours: int = Query(24, le=168),
    user: dict = Depends(get_current_user),
):
    db = await get_db()
    since = time.time() - (hours * 3600)
    cursor = await db.execute(
        "SELECT ts, cpu_percent, mem_used_mb, mem_total_mb, vpp_rss_mb FROM system_snapshots WHERE ts > ? ORDER BY ts ASC",
        (since,)
    )
    rows = await cursor.fetchall()
    return {
        "data": [
            {"ts": r[0], "cpu": r[1], "mem_used": r[2], "mem_total": r[3], "vpp_rss": r[4]}
            for r in rows
        ]
    }


# --- VPP raw ---

@router.get("/vpp/sessions")
async def vpp_sessions(user: dict = Depends(get_current_user)):
    return {"sessions": vpp.get_cache().get("sessions", [])}

@router.get("/vpp/interfaces")
async def vpp_interfaces(user: dict = Depends(get_current_user)):
    return {"interfaces": vpp.get_cache().get("interfaces", {})}

@router.get("/vpp/policers")
async def vpp_policers(user: dict = Depends(get_current_user)):
    return {"policers": vpp.get_cache().get("policers", [])}


# --- RADIUS monitoring ---

@router.get("/radius")
async def radius_status(user: dict = Depends(get_current_user)):
    """Get current RADIUS stats from all BRs."""
    accel_data = accel.get_cache()
    result = {}
    for br_name, inst in accel_data.get("instances", {}).items():
        stats = inst.get("stats", {})
        radius = stats.get("_radius", {})
        pppoe = stats.get("_pppoe", {})
        result[br_name] = {
            "radius": radius,
            "pppoe": pppoe,
            "running": inst.get("running", False),
            "session_count": inst.get("session_count", 0),
        }
    return {"brs": result}

@router.get("/radius/history")
async def radius_history(
    hours: int = Query(24, le=168),
    br: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """Get RADIUS historical data."""
    db = await get_db()
    since = time.time() - (hours * 3600)
    query = "SELECT ts, br_name, server_ip, state, auth_sent, auth_lost_total, auth_avg_time_1m, acct_sent, acct_lost_total, acct_avg_time_1m, fail_count, queue_length FROM radius_snapshots WHERE ts > ?"
    params = [since]
    if br:
        query += " AND br_name = ?"
        params.append(br)
    query += " ORDER BY ts ASC"
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {
        "data": [
            {"ts": r[0], "br": r[1], "server_ip": r[2], "state": r[3],
             "auth_sent": r[4], "auth_lost": r[5], "auth_latency": r[6],
             "acct_sent": r[7], "acct_lost": r[8], "acct_latency": r[9],
             "fail_count": r[10], "queue_length": r[11]}
            for r in rows
        ]
    }


# --- Traffic analytics ---

@router.get("/traffic/top")
async def traffic_top(
    limit: int = Query(10, le=50),
    user: dict = Depends(get_current_user),
):
    """Get top N sessions by traffic volume."""
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()

    sessions = []
    for br_name, inst in accel_data.get("instances", {}).items():
        for sess in inst.get("sessions", []):
            ifname = sess.get("ifname", "")
            iface = vpp_data.get("interfaces", {}).get(ifname, {})
            total_bytes = iface.get("rx_bytes", 0) + iface.get("tx_bytes", 0)
            sessions.append({
                "br": br_name,
                "ifname": ifname,
                "username": sess.get("username", ""),
                "ip": sess.get("ip", sess.get("address", "")),
                "rx_bytes": iface.get("rx_bytes", 0),
                "tx_bytes": iface.get("tx_bytes", 0),
                "rx_packets": iface.get("rx_packets", 0),
                "tx_packets": iface.get("tx_packets", 0),
                "total_bytes": total_bytes,
                "uptime": sess.get("uptime", ""),
                "rate_limit": sess.get("rate-limit", sess.get("rate", "")),
            })

    # Sort by total bytes descending
    sessions.sort(key=lambda s: s["total_bytes"], reverse=True)
    return {"sessions": sessions[:limit]}

@router.get("/traffic/summary")
async def traffic_summary(user: dict = Depends(get_current_user)):
    """Get aggregate traffic summary per BR."""
    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()

    brs = {}
    for br_name, inst in accel_data.get("instances", {}).items():
        rx_total = 0
        tx_total = 0
        rx_pkts = 0
        tx_pkts = 0
        drops = 0
        for sess in inst.get("sessions", []):
            ifname = sess.get("ifname", "")
            iface = vpp_data.get("interfaces", {}).get(ifname, {})
            rx_total += iface.get("rx_bytes", 0)
            tx_total += iface.get("tx_bytes", 0)
            rx_pkts += iface.get("rx_packets", 0)
            tx_pkts += iface.get("tx_packets", 0)
            drops += iface.get("drops", 0)

        brs[br_name] = {
            "rx_bytes": rx_total,
            "tx_bytes": tx_total,
            "rx_packets": rx_pkts,
            "tx_packets": tx_pkts,
            "drops": drops,
            "session_count": inst.get("session_count", 0),
        }

    return {"brs": brs}

@router.get("/traffic/export")
async def traffic_export(user: dict = Depends(get_current_user)):
    """Export current session traffic data as CSV."""
    from fastapi.responses import Response

    accel_data = accel.get_cache()
    vpp_data = vpp.get_cache()

    lines = ["BR,Interface,Username,IP,RX_Bytes,TX_Bytes,RX_Packets,TX_Packets,Drops,Uptime,Rate_Limit"]
    for br_name, inst in accel_data.get("instances", {}).items():
        for sess in inst.get("sessions", []):
            ifname = sess.get("ifname", "")
            iface = vpp_data.get("interfaces", {}).get(ifname, {})
            lines.append(",".join([
                br_name, ifname,
                sess.get("username", ""),
                sess.get("ip", sess.get("address", "")),
                str(iface.get("rx_bytes", 0)),
                str(iface.get("tx_bytes", 0)),
                str(iface.get("rx_packets", 0)),
                str(iface.get("tx_packets", 0)),
                str(iface.get("drops", 0)),
                sess.get("uptime", ""),
                sess.get("rate-limit", sess.get("rate", "")),
            ]))

    csv_content = "\n".join(lines)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bng_traffic_{int(time.time())}.csv"}
    )


# --- User Management ---

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    allowed_pages: list[str] = []

class UpdateUserRequest(BaseModel):
    role: str | None = None
    allowed_pages: list[str] | None = None
    password: str | None = None

@router.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Get current user info including role and allowed_pages."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT username, role, allowed_pages FROM users WHERE username = ?",
        (user["username"],)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        allowed_pages = json.loads(row[2]) if row[2] else []
    except (json.JSONDecodeError, TypeError):
        allowed_pages = []
    return {"username": row[0], "role": row[1] or "admin", "allowed_pages": allowed_pages}

@router.get("/users")
async def list_users(user: dict = Depends(get_current_user)):
    """List all users (admin only)."""
    require_admin(user)
    db = await get_db()
    cursor = await db.execute("SELECT id, username, role, allowed_pages, created_at FROM users ORDER BY id ASC")
    rows = await cursor.fetchall()
    users = []
    for r in rows:
        try:
            allowed_pages = json.loads(r[3]) if r[3] else []
        except (json.JSONDecodeError, TypeError):
            allowed_pages = []
        users.append({
            "id": r[0],
            "username": r[1],
            "role": r[2] or "admin",
            "allowed_pages": allowed_pages,
            "created_at": r[4],
        })
    return users

@router.post("/users")
async def create_user(req: CreateUserRequest, user: dict = Depends(get_current_user)):
    """Create a new user (admin only)."""
    require_admin(user)
    if req.role not in ("admin", "operator", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be admin, operator, or viewer")
    db = await get_db()
    # Check if username already exists
    cursor = await db.execute("SELECT id FROM users WHERE username = ?", (req.username,))
    if await cursor.fetchone():
        raise HTTPException(status_code=409, detail="Username already exists")
    hashed = hash_password(req.password)
    allowed_pages_json = json.dumps(req.allowed_pages)
    await db.execute(
        "INSERT INTO users (username, hashed_password, created_at, role, allowed_pages) VALUES (?, ?, ?, ?, ?)",
        (req.username, hashed, time.time(), req.role, allowed_pages_json)
    )
    await db.commit()
    return {"status": "ok", "message": f"User '{req.username}' created"}

@router.put("/users/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, user: dict = Depends(get_current_user)):
    """Update a user (admin only)."""
    require_admin(user)
    db = await get_db()
    cursor = await db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if req.role is not None:
        if req.role not in ("admin", "operator", "viewer"):
            raise HTTPException(status_code=400, detail="Role must be admin, operator, or viewer")
        await db.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
    if req.allowed_pages is not None:
        await db.execute("UPDATE users SET allowed_pages = ? WHERE id = ?", (json.dumps(req.allowed_pages), user_id))
    if req.password is not None:
        hashed = hash_password(req.password)
        await db.execute("UPDATE users SET hashed_password = ? WHERE id = ?", (hashed, user_id))
    await db.commit()
    return {"status": "ok", "message": f"User '{row[1]}' updated"}

@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(get_current_user)):
    """Delete a user (admin only, cannot delete self)."""
    require_admin(user)
    db = await get_db()
    cursor = await db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if row[1] == user["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return {"status": "ok", "message": f"User '{row[1]}' deleted"}
