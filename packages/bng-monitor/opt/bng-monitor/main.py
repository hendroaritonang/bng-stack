"""BNG Monitor — Main application entry point."""
import asyncio
import logging
import os
import time
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager

from config import HOST, PORT, DATA_DIR, COLLECT_INTERVAL_FAST, COLLECT_INTERVAL_MEDIUM, COLLECT_INTERVAL_SLOW
from database import get_db, close_db, cleanup_old_data
from auth import ensure_admin_user, decode_token
from routes.api import router as api_router
from collectors import vpp, accel, system as sys_collector
from collectors import evaluate_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bng.main")

# WebSocket clients
_ws_clients: set[WebSocket] = set()

# Background tasks
_bg_tasks: list[asyncio.Task] = []


async def broadcast_ws(data: dict):
    """Send data to all connected WebSocket clients."""
    global _ws_clients
    if not _ws_clients:
        return
    msg = json.dumps(data, default=str)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


async def collector_fast():
    """Fast collection loop — sessions, VPP status."""
    while True:
        try:
            await asyncio.gather(
                vpp.collect_all(),
                accel.collect_all(),
            )
            # Evaluate alerts
            await evaluate_alerts(vpp.get_cache(), accel.get_cache(), sys_collector.get_cache())

            # Flush pending disconnect events to DB
            pending = accel.get_pending_disconnects()
            if pending:
                db = await get_db()
                for evt in pending:
                    await db.execute(
                        "INSERT INTO disconnect_log (ts, br_name, username, ip, mac, session_id, reason, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (evt["ts"], evt["br_name"], evt.get("username", ""),
                         evt.get("ip", ""), evt.get("mac", ""),
                         evt.get("session_id", ""), evt.get("reason", ""),
                         evt.get("duration", 0))
                    )
                await db.commit()
                log.info(f"Flushed {len(pending)} disconnect events to DB")

            # Broadcast to WebSocket clients
            await broadcast_ws({
                "type": "update",
                "vpp": {
                    "running": vpp.get_cache().get("vpp_running"),
                    "pid": vpp.get_cache().get("vpp_pid"),
                    "total_sessions": vpp.get_cache().get("pppoe_summary", {}).get("total", 0),
                },
                "brs": {
                    br: {"running": i.get("running"), "session_count": i.get("session_count", 0)}
                    for br, i in accel.get_cache().get("instances", {}).items()
                },
                "ts": time.time(),
            })
        except Exception as e:
            log.error(f"Fast collector error: {e}")
        await asyncio.sleep(COLLECT_INTERVAL_FAST)


async def collector_medium():
    """Medium collection loop — system metrics + log scanning."""
    while True:
        try:
            await sys_collector.collect_all()

            # Scan accel-ppp logs for disconnect/auth-fail events
            log_events = await accel.scan_logs_for_disconnects()
            if log_events:
                db = await get_db()
                for evt in log_events:
                    # Convert ts_str to epoch
                    ts = evt.get("ts", 0)
                    ts_str = evt.get("ts_str", "")
                    if ts_str and not ts:
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                            ts = dt.timestamp()
                        except Exception:
                            ts = time.time()

                    await db.execute(
                        "INSERT INTO disconnect_log (ts, br_name, username, ip, mac, session_id, reason, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ts, evt.get("br_name", ""), evt.get("username", ""),
                         evt.get("ip", ""), evt.get("mac", ""),
                         evt.get("session_id", ""), evt.get("reason", ""),
                         evt.get("duration", 0))
                    )
                await db.commit()
                log.info(f"Parsed {len(log_events)} events from accel-ppp logs")

        except Exception as e:
            log.error(f"Medium collector error: {e}")
        await asyncio.sleep(COLLECT_INTERVAL_MEDIUM)


async def collector_slow():
    """Slow collection loop — historical snapshots."""
    while True:
        try:
            db = await get_db()

            # Save session counts per BR
            accel_data = accel.get_cache()
            vpp_data = vpp.get_cache()
            now = time.time()

            for br_name, inst in accel_data.get("instances", {}).items():
                # Compute aggregate traffic for this BR
                rx_total = 0
                tx_total = 0
                for sess in inst.get("sessions", []):
                    ifname = sess.get("ifname", "")
                    iface = vpp_data.get("interfaces", {}).get(ifname, {})
                    rx_total += iface.get("rx_bytes", 0)
                    tx_total += iface.get("tx_bytes", 0)

                await db.execute(
                    "INSERT INTO history_snapshots (ts, br_name, session_count, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?, ?)",
                    (now, br_name, inst.get("session_count", 0), rx_total, tx_total)
                )

            # Save system metrics
            sys_data = sys_collector.get_cache()
            await db.execute(
                "INSERT INTO system_snapshots (ts, cpu_percent, mem_used_mb, mem_total_mb, vpp_rss_mb) VALUES (?, ?, ?, ?, ?)",
                (now, sys_data.get("cpu_percent", 0), sys_data.get("mem_used_mb", 0),
                 sys_data.get("mem_total_mb", 0), sys_data.get("vpp_rss_mb", 0))
            )

            # Save RADIUS snapshots per BR
            for br_name, inst in accel_data.get("instances", {}).items():
                r = inst.get("stats", {}).get("_radius", {})
                if r:
                    await db.execute(
                        "INSERT INTO radius_snapshots (ts, br_name, server_ip, state, auth_sent, auth_lost_total, auth_avg_time_1m, acct_sent, acct_lost_total, acct_avg_time_1m, fail_count, queue_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (now, br_name, r.get("server_ip", ""), r.get("state", ""),
                         r.get("auth_sent", 0), r.get("auth_lost_total", 0), r.get("auth_avg_time_1m", 0),
                         r.get("acct_sent", 0), r.get("acct_lost_total", 0), r.get("acct_avg_time_1m", 0),
                         r.get("fail_count", 0), r.get("queue_length", 0))
                    )

            await db.commit()

            # Cleanup old data once per hour
            if int(now) % 3600 < COLLECT_INTERVAL_SLOW:
                await cleanup_old_data(30)

        except Exception as e:
            log.error(f"Slow collector error: {e}")
        await asyncio.sleep(COLLECT_INTERVAL_SLOW)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # Init DB
    db = await get_db()
    await ensure_admin_user(db)
    log.info("Database initialized")

    # Initial collection
    await asyncio.gather(
        vpp.collect_all(),
        accel.collect_all(),
        sys_collector.collect_all(),
    )
    log.info("Initial data collection complete")

    # Start background collectors
    _bg_tasks.append(asyncio.create_task(collector_fast()))
    _bg_tasks.append(asyncio.create_task(collector_medium()))
    _bg_tasks.append(asyncio.create_task(collector_slow()))
    log.info("Background collectors started")

    log.info(f"BNG Monitor ready at http://{HOST}:{PORT}")

    yield

    # Shutdown
    for t in _bg_tasks:
        t.cancel()
    await close_db()
    log.info("BNG Monitor shutdown complete")


app = FastAPI(title="BNG Monitor", version="1.0.0", lifespan=lifespan)

# API routes
app.include_router(api_router)

# Static files
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Auth check via query param
    token = ws.query_params.get("token", "")
    username = decode_token(token) if token else None
    if not username:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    _ws_clients.add(ws)
    log.info(f"WebSocket client connected: {username}")

    try:
        while True:
            # Keep alive — client can send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        log.info(f"WebSocket client disconnected: {username}")


# Serve index.html for all non-API routes (SPA)
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/{path:path}")
async def spa_catch(path: str):
    # Try static file first
    file_path = os.path.join(STATIC_DIR, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")
