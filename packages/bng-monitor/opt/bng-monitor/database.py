"""Database setup and helpers using aiosqlite."""
import aiosqlite
import os
import time
from config import DB_PATH, DATA_DIR

_db: aiosqlite.Connection = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA synchronous=NORMAL")
        await init_tables(_db)
    return _db

async def init_tables(db: aiosqlite.Connection):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            br_name TEXT NOT NULL,
            session_count INTEGER NOT NULL,
            rx_bytes INTEGER DEFAULT 0,
            tx_bytes INTEGER DEFAULT 0,
            rx_pps INTEGER DEFAULT 0,
            tx_pps INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_history_ts ON history_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_history_br ON history_snapshots(br_name);

        CREATE TABLE IF NOT EXISTS system_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            cpu_percent REAL DEFAULT 0,
            mem_used_mb REAL DEFAULT 0,
            mem_total_mb REAL DEFAULT 0,
            vpp_rss_mb REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sys_ts ON system_snapshots(ts);

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            ack_at REAL,
            ack_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

        CREATE TABLE IF NOT EXISTS disconnect_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            br_name TEXT NOT NULL,
            username TEXT,
            ip TEXT,
            mac TEXT,
            session_id TEXT,
            reason TEXT,
            duration REAL
        );
        CREATE INDEX IF NOT EXISTS idx_disc_ts ON disconnect_log(ts);
        CREATE INDEX IF NOT EXISTS idx_disc_user ON disconnect_log(username);

        CREATE TABLE IF NOT EXISTS radius_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            br_name TEXT NOT NULL,
            server_ip TEXT,
            state TEXT,
            auth_sent INTEGER DEFAULT 0,
            auth_lost_total INTEGER DEFAULT 0,
            auth_avg_time_1m INTEGER DEFAULT 0,
            acct_sent INTEGER DEFAULT 0,
            acct_lost_total INTEGER DEFAULT 0,
            acct_avg_time_1m INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            queue_length INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_radius_ts ON radius_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_radius_br ON radius_snapshots(br_name);
    """)
    await db.commit()

    # --- Migration: add RBAC columns to users table ---
    try:
        await db.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'admin'")
        await db.commit()
    except Exception:
        pass  # column already exists
    try:
        await db.execute("ALTER TABLE users ADD COLUMN allowed_pages TEXT DEFAULT '[]'")
        await db.commit()
    except Exception:
        pass  # column already exists
    # Ensure existing admin users have full allowed_pages
    all_pages = '["dashboard","sessions","trace","br-mgmt","alerts","history","radius","traffic","users"]'
    await db.execute(
        "UPDATE users SET allowed_pages = ? WHERE role = 'admin' AND (allowed_pages IS NULL OR allowed_pages = '[]')",
        (all_pages,)
    )
    await db.commit()

async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

async def cleanup_old_data(days=30):
    """Remove data older than N days."""
    db = await get_db()
    cutoff = time.time() - (days * 86400)
    await db.execute("DELETE FROM history_snapshots WHERE ts < ?", (cutoff,))
    await db.execute("DELETE FROM system_snapshots WHERE ts < ?", (cutoff,))
    await db.execute("DELETE FROM disconnect_log WHERE ts < ?", (cutoff,))
    await db.execute("DELETE FROM radius_snapshots WHERE ts < ?", (cutoff,))
    await db.commit()
