import os
import json
import time
import sqlite3
import secrets
import threading
import requests
from urllib.parse import urlencode
from flask import Flask, render_template, request, session, jsonify, redirect, url_for
from dotenv import load_dotenv
load_dotenv()

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

static_dir = os.path.join(BASE_DIR, "static")
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=static_dir
)
app.secret_key = config.SECRET_KEY

# Ensure session cookies are hardened (Secure enabled on Vercel HTTPS)
is_vercel = bool(os.getenv("VERCEL"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_vercel,
    TEMPLATES_AUTO_RELOAD=not is_vercel
)
app.jinja_env.auto_reload = not is_vercel

# Admin Panel Credentials (/admin)
ADMIN_PANEL_USER = config.ADMIN_PANEL_USER
ADMIN_PANEL_PASS = config.ADMIN_PANEL_PASS

# API & Server Configuration loaded directly from config.py
API_KEY = config.API_KEY
API_URL = config.API_URL
API_ACTION_ADD = getattr(config, "API_ACTION_ADD", "add")
API_ACTION_EDIT = getattr(config, "API_ACTION_EDIT", "change_uid")
API_ACTION_DELETE = getattr(config, "API_ACTION_DELETE", "delete")

DISCORD_SERVER_INVITE = config.DISCORD_SERVER_INVITE
MEOW_DISCORD_SERVER_INVITE = config.MEOW_DISCORD_SERVER_INVITE

# Multi-tier Persistent Storage Configuration:
# Tier 1: Local / BASE_DIR settings.json (persistent across VPS, local, and deployments)
# Tier 2: /tmp settings.json & SQLite (ephemeral serverless scratch)
# Tier 3: seed_db (BASE_DIR/database.db)
# Tier 4: Environment variable overrides (e.g. MAINTENANCE_MODE=1)
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

if is_vercel:
    import tempfile
    import shutil
    tmp_dir = "/tmp" if os.path.exists("/tmp") else tempfile.gettempdir()
    tmp_db = os.path.join(tmp_dir, "database.db")
    seed_db = os.path.join(BASE_DIR, "database.db")
    TMP_SETTINGS_FILE = os.path.join(tmp_dir, "settings.json")
    if not os.path.exists(tmp_db) and os.path.exists(seed_db):
        try:
            shutil.copy2(seed_db, tmp_db)
        except Exception:
            pass
    DB_PATH = tmp_db
else:
    tmp_dir = BASE_DIR
    DB_PATH = os.path.abspath(os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "database.db")))
    seed_db = DB_PATH
    TMP_SETTINGS_FILE = SETTINGS_FILE

# In-memory settings cache for zero-latency lookups and inter-request durability
_SETTINGS_CACHE = {}
_SETTINGS_CACHE_TIME = 0.0

def load_persistent_settings() -> dict:
    """Load persistent settings with multi-tier fallback (env var -> tmp JSON -> base JSON -> DB)."""
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME

    # 1. Environment variable override (highest priority, immune to serverless cold starts)
    env_maint = os.getenv("MAINTENANCE_MODE")
    if env_maint is not None:
        val = "1" if str(env_maint).strip().lower() in ("1", "true", "yes", "on") else "0"
        return {"maintenance_mode": val, "source": "env"}

    # 2. Check in-memory cache if fresh (within 3 seconds)
    now = time.time()
    if _SETTINGS_CACHE and (now - _SETTINGS_CACHE_TIME < 3.0):
        return dict(_SETTINGS_CACHE)

    # 3. Try reading tmp_dir / settings.json or base_dir / settings.json
    for path in (TMP_SETTINGS_FILE, SETTINGS_FILE):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "maintenance_mode" in data:
                        _SETTINGS_CACHE = data
                        _SETTINGS_CACHE_TIME = now
                        return dict(data)
            except Exception:
                pass

    # 4. Try reading from active SQLite DB
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH, timeout=5)
            c = conn.cursor()
            c.execute("SELECT value FROM system_settings WHERE key = 'maintenance_mode'")
            row = c.fetchone()
            conn.close()
            if row:
                data = {"maintenance_mode": str(row[0])}
                _SETTINGS_CACHE = data
                _SETTINGS_CACHE_TIME = now
                return data
    except Exception:
        pass

    # 5. Try reading from seed_db SQLite DB if distinct
    if seed_db and os.path.exists(seed_db) and seed_db != DB_PATH:
        try:
            conn = sqlite3.connect(seed_db, timeout=5)
            c = conn.cursor()
            c.execute("SELECT value FROM system_settings WHERE key = 'maintenance_mode'")
            row = c.fetchone()
            conn.close()
            if row:
                data = {"maintenance_mode": str(row[0])}
                _SETTINGS_CACHE = data
                _SETTINGS_CACHE_TIME = now
                return data
        except Exception:
            pass

    return _SETTINGS_CACHE or {"maintenance_mode": "0"}


def save_persistent_settings(settings: dict) -> bool:
    """Save persistent settings across all available tiers (cache, files, databases)."""
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME
    _SETTINGS_CACHE.update(settings)
    _SETTINGS_CACHE_TIME = time.time()
    success = False

    # 1. Save to TMP_SETTINGS_FILE
    try:
        tmp_target = TMP_SETTINGS_FILE + ".tmp"
        with open(tmp_target, "w", encoding="utf-8") as f:
            json.dump(_SETTINGS_CACHE, f, indent=2)
        if os.path.exists(TMP_SETTINGS_FILE):
            os.remove(TMP_SETTINGS_FILE)
        os.rename(tmp_target, TMP_SETTINGS_FILE)
        success = True
    except Exception:
        pass

    # 2. Save to BASE_DIR SETTINGS_FILE (if writable and distinct)
    if SETTINGS_FILE != TMP_SETTINGS_FILE:
        try:
            tmp_target = SETTINGS_FILE + ".tmp"
            with open(tmp_target, "w", encoding="utf-8") as f:
                json.dump(_SETTINGS_CACHE, f, indent=2)
            if os.path.exists(SETTINGS_FILE):
                os.remove(SETTINGS_FILE)
            os.rename(tmp_target, SETTINGS_FILE)
            success = True
        except Exception:
            pass

    # 3. Save to active SQLite DB
    try:
        conn = get_db()
        cursor = conn.cursor()
        maint_val = str(_SETTINGS_CACHE.get("maintenance_mode", "0"))
        cursor.execute("""
            INSERT INTO system_settings (key, value)
            VALUES ('maintenance_mode', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (maint_val,))
        conn.commit()
        conn.close()
        success = True
    except Exception:
        pass

    # 4. Save to seed_db directly if writable and distinct
    if seed_db and os.path.exists(seed_db) and seed_db != DB_PATH:
        try:
            conn = sqlite3.connect(seed_db, timeout=5)
            cursor = conn.cursor()
            maint_val = str(_SETTINGS_CACHE.get("maintenance_mode", "0"))
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)
            """)
            cursor.execute("""
                INSERT INTO system_settings (key, value)
                VALUES ('maintenance_mode', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (maint_val,))
            conn.commit()
            conn.close()
            success = True
        except Exception:
            pass

    return success

# Post-seed check on Vercel cold-boot to propagate saved settings to tmp_db
if is_vercel and os.path.exists(DB_PATH):
    try:
        init_maint = load_persistent_settings().get("maintenance_mode")
        if init_maint:
            c_conn = sqlite3.connect(DB_PATH, timeout=5)
            c_cursor = c_conn.cursor()
            c_cursor.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
            c_cursor.execute("""
                INSERT INTO system_settings (key, value)
                VALUES ('maintenance_mode', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (str(init_maint),))
            c_conn.commit()
            c_conn.close()
    except Exception:
        pass

COOLDOWN_SECONDS = 86400  # Strict 24-hour cycle

# Concurrency lock to prevent race-condition bypass exploits
claim_lock = threading.Lock()

# ==================== CLOUDFLARE & CLIENT IP DETECTION ====================
def get_client_ip() -> str:
    """Accurately resolve the visitor's real IP behind Cloudflare or reverse proxies."""
    # Cloudflare standard header
    cf_connecting_ip = request.headers.get("CF-Connecting-IP")
    if cf_connecting_ip:
        return cf_connecting_ip.strip()

    # Standard real IP header
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Standard proxy chain header (leftmost IP is origin)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[0]

    return request.remote_addr or "127.0.0.1"

def get_client_identifiers():
    """Retrieve both IP address and persistent session client token for layered anti-bypass."""
    ip = get_client_ip()
    if "client_token" not in session:
        session["client_token"] = secrets.token_hex(16)
    return ip, session["client_token"]

# ==================== DATABASE INITIALIZATION & HELPERS ====================
def get_db():
    """Create a SQLite connection configured with in-memory journaling to prevent .db-journal files."""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode = MEMORY;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    # Rate limit table tracking claims per identifier (IP and session token)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            identifier TEXT PRIMARY KEY,
            last_claim_timestamp INTEGER,
            last_claimed_uid TEXT
        )
    """)
    # Registered UIDs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS registered_uids (
            uid TEXT PRIMARY KEY,
            discord_id TEXT,
            added_timestamp INTEGER,
            expiry_days INTEGER,
            claim_type TEXT,
            identifier TEXT
        )
    """)
    # Ensure identifier column exists if table was created in an older schema
    cursor.execute("PRAGMA table_info(registered_uids);")
    columns = [col[1] for col in cursor.fetchall()]
    if "identifier" not in columns:
        try:
            cursor.execute("ALTER TABLE registered_uids ADD COLUMN identifier TEXT;")
        except Exception:
            pass
    if "discord_id" not in columns:
        try:
            cursor.execute("ALTER TABLE registered_uids ADD COLUMN discord_id TEXT;")
        except Exception:
            pass

    # System settings table for maintenance mode and dynamic configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    init_maint_val = str(load_persistent_settings().get("maintenance_mode", "0"))
    cursor.execute("""
        INSERT OR IGNORE INTO system_settings (key, value)
        VALUES ('maintenance_mode', ?)
    """, (init_maint_val,))

    # Devices table for tracking player hardware IDs, bypass vs normal clients, and bans/terminations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            player_uid TEXT,
            player_name TEXT,
            player_type TEXT DEFAULT 'normal',
            ip_address TEXT,
            user_agent TEXT,
            app_version TEXT,
            status TEXT DEFAULT 'active',
            ban_reason TEXT,
            terminate_reason TEXT,
            first_seen INTEGER,
            last_seen INTEGER
        )
    """)
    cursor.execute("PRAGMA table_info(devices);")
    device_cols = [col[1] for col in cursor.fetchall()]
    for col_name, col_type in [
        ("player_uid", "TEXT"),
        ("player_name", "TEXT"),
        ("player_type", "TEXT DEFAULT 'normal'"),
        ("ip_address", "TEXT"),
        ("user_agent", "TEXT"),
        ("app_version", "TEXT"),
        ("status", "TEXT DEFAULT 'active'"),
        ("ban_reason", "TEXT"),
        ("terminate_reason", "TEXT"),
        ("first_seen", "INTEGER"),
        ("last_seen", "INTEGER")
    ]:
        if col_name not in device_cols:
            try:
                cursor.execute(f"ALTER TABLE devices ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

    conn.commit()
    conn.close()

init_db()

# ==================== ADMIN & MAINTENANCE HELPERS ====================
def is_maintenance_mode() -> bool:
    """Check if free UID claims are paused for maintenance via multi-tier persistence."""
    settings = load_persistent_settings()
    return str(settings.get("maintenance_mode", "0")) == "1"

def set_maintenance_mode(enabled: bool):
    """Enable or disable free UID claims maintenance mode and persist across all tiers."""
    val = "1" if enabled else "0"
    save_persistent_settings({
        "maintenance_mode": val,
        "updated_at": int(time.time())
    })

def get_admin_stats() -> dict:
    """Compile metrics on lifetime claims, today's claims, and active rate limits."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        current_time = int(time.time())
        day_ago = current_time - 86400

        cursor.execute("SELECT COUNT(*) FROM registered_uids")
        total_claims = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM registered_uids WHERE added_timestamp >= ?", (day_ago,))
        claims_24h = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT identifier) FROM rate_limits WHERE last_claim_timestamp >= ?", (day_ago,))
        active_cooldowns = cursor.fetchone()[0]

        return {
            "total_claims": total_claims,
            "claims_24h": claims_24h,
            "active_cooldowns": active_cooldowns,
            "maintenance_mode": is_maintenance_mode()
        }
    finally:
        conn.close()

def get_all_claims(query: str = None, limit: int = 200) -> list:
    """Retrieve claimed UIDs with timestamp and client identifier."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        current_time = int(time.time())
        if query:
            q = f"%{query.strip()}%"
            cursor.execute("""
                SELECT uid, added_timestamp, expiry_days, claim_type, identifier
                FROM registered_uids
                WHERE uid LIKE ? OR identifier LIKE ?
                ORDER BY added_timestamp DESC
                LIMIT ?
            """, (q, q, limit))
        else:
            cursor.execute("""
                SELECT uid, added_timestamp, expiry_days, claim_type, identifier
                FROM registered_uids
                ORDER BY added_timestamp DESC
                LIMIT ?
            """, (limit,))

        rows = cursor.fetchall()
        claims = []
        for row in rows:
            uid, added_ts, expiry_days, claim_type, identifier = row
            is_active = (added_ts + (expiry_days * 86400)) > current_time
            readable_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(added_ts))
            claims.append({
                "uid": uid,
                "added_timestamp": added_ts,
                "readable_date": readable_date,
                "expiry_days": expiry_days,
                "claim_type": claim_type or "free_web",
                "identifier": identifier or "Unknown",
                "is_active": is_active
            })
        return claims
    finally:
        conn.close()

def delete_claimed_uid(uid: str) -> bool:
    """Remove a UID from whitelist registration and clear its cooldown."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM registered_uids WHERE uid = ?", (uid,))
        cursor.execute("DELETE FROM rate_limits WHERE last_claimed_uid = ?", (uid,))
        conn.commit()
        return True
    finally:
        conn.close()

def reset_identifier_cooldown(identifier: str) -> bool:
    """Clear 24-hour rate limit for a specific IP or UID."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rate_limits WHERE identifier = ? OR last_claimed_uid = ?", (identifier, identifier))
        conn.commit()
        return True
    finally:
        conn.close()

def reset_all_rate_limits() -> bool:
    """Reset all active 24-hour rate limits, allowing immediate re-claims."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rate_limits")
        conn.commit()
        return True
    finally:
        conn.close()

# ==================== DEVICE TRACKING & ENFORCEMENT HELPERS ====================
def get_device_by_id(device_id: str) -> dict:
    """Retrieve full device record by unique device ID."""
    if not device_id:
        return None
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT device_id, player_uid, player_name, player_type, ip_address, 
                   user_agent, app_version, status, ban_reason, terminate_reason, 
                   first_seen, last_seen
            FROM devices WHERE device_id = ?
        """, (str(device_id).strip(),))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "player_uid": row[1] or "",
            "player_name": row[2] or "",
            "player_type": row[3] or "normal",
            "ip_address": row[4] or "",
            "user_agent": row[5] or "",
            "app_version": row[6] or "1.0",
            "status": row[7] or "active",
            "ban_reason": row[8] or "",
            "terminate_reason": row[9] or "",
            "first_seen": row[10] or 0,
            "last_seen": row[11] or 0
        }
    finally:
        conn.close()

def register_or_update_device(
    device_id: str,
    player_uid: str = None,
    player_name: str = None,
    player_type: str = "normal",
    ip: str = None,
    user_agent: str = None,
    app_version: str = None
) -> dict:
    """
    Capture incoming device ID from any player (bypass or normal client).
    Updates heartbeat timestamp, IP, and client metadata while maintaining enforcement status.
    """
    clean_id = str(device_id).strip()
    if not clean_id:
        return None

    conn = get_db()
    try:
        cursor = conn.cursor()
        current_time = int(time.time())
        cursor.execute("""
            SELECT status, ban_reason, terminate_reason, first_seen, player_type, player_uid, player_name
            FROM devices WHERE device_id = ?
        """, (clean_id,))
        row = cursor.fetchone()

        if row:
            status, ban_reason, terminate_reason, first_seen, existing_type, existing_uid, existing_name = row
            effective_type = player_type if (player_type and player_type != "normal") else (existing_type or player_type or "normal")
            effective_uid = player_uid if player_uid else (existing_uid or "")
            effective_name = player_name if player_name else (existing_name or "")

            cursor.execute("""
                UPDATE devices SET
                    last_seen = ?,
                    ip_address = COALESCE(?, ip_address),
                    user_agent = COALESCE(?, user_agent),
                    app_version = COALESCE(?, app_version),
                    player_type = ?,
                    player_uid = ?,
                    player_name = ?
                WHERE device_id = ?
            """, (current_time, ip, user_agent, app_version, effective_type, effective_uid, effective_name, clean_id))
            conn.commit()

            return {
                "device_id": clean_id,
                "status": status or "active",
                "ban_reason": ban_reason or "",
                "terminate_reason": terminate_reason or "",
                "player_type": effective_type,
                "player_uid": effective_uid,
                "player_name": effective_name,
                "first_seen": first_seen or current_time,
                "last_seen": current_time,
                "is_new": False
            }
        else:
            clean_type = player_type or "normal"
            clean_uid = player_uid or ""
            clean_name = player_name or ""
            cursor.execute("""
                INSERT INTO devices (
                    device_id, player_uid, player_name, player_type, ip_address,
                    user_agent, app_version, status, ban_reason, terminate_reason,
                    first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '', '', ?, ?)
            """, (clean_id, clean_uid, clean_name, clean_type, ip or "", user_agent or "", app_version or "1.0", current_time, current_time))
            conn.commit()

            return {
                "device_id": clean_id,
                "status": "active",
                "ban_reason": "",
                "terminate_reason": "",
                "player_type": clean_type,
                "player_uid": clean_uid,
                "player_name": clean_name,
                "first_seen": current_time,
                "last_seen": current_time,
                "is_new": True
            }
    finally:
        conn.close()

def set_device_status(device_id: str, new_status: str, reason: str = "") -> bool:
    """Update device enforcement state: 'active', 'banned', or 'terminated'."""
    clean_id = str(device_id).strip()
    if not clean_id:
        return False

    conn = get_db()
    try:
        cursor = conn.cursor()
        current_time = int(time.time())
        ban_r = reason if new_status == "banned" else ""
        term_r = reason if new_status == "terminated" else ""

        cursor.execute("SELECT device_id FROM devices WHERE device_id = ?", (clean_id,))
        exists = cursor.fetchone()

        if exists:
            cursor.execute("""
                UPDATE devices SET
                    status = ?,
                    ban_reason = CASE WHEN ? = 'banned' THEN ? ELSE '' END,
                    terminate_reason = CASE WHEN ? = 'terminated' THEN ? ELSE '' END,
                    last_seen = ?
                WHERE device_id = ?
            """, (new_status, new_status, ban_r, new_status, term_r, current_time, clean_id))
        else:
            cursor.execute("""
                INSERT INTO devices (
                    device_id, player_uid, player_name, player_type, ip_address,
                    user_agent, app_version, status, ban_reason, terminate_reason,
                    first_seen, last_seen
                ) VALUES (?, '', '', 'manual', 'admin_action', 'Admin Panel', '1.0', ?, ?, ?, ?, ?)
            """, (clean_id, new_status, ban_r, term_r, current_time, current_time))

        conn.commit()
        return True
    finally:
        conn.close()

def delete_device_record(device_id: str) -> bool:
    """Remove a device record completely from tracking database."""
    clean_id = str(device_id).strip()
    if not clean_id:
        return False
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM devices WHERE device_id = ?", (clean_id,))
        conn.commit()
        return True
    finally:
        conn.close()

def get_device_stats() -> dict:
    """Compile aggregated counts for total, active, banned, terminated, and player classifications."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM devices")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM devices WHERE status = 'active'")
        active = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM devices WHERE status = 'banned'")
        banned = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM devices WHERE status = 'terminated'")
        terminated = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM devices WHERE LOWER(player_type) LIKE '%bypass%'")
        bypass_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM devices WHERE LOWER(player_type) NOT LIKE '%bypass%'")
        normal_count = cursor.fetchone()[0]

        return {
            "total": total,
            "active": active,
            "banned": banned,
            "terminated": terminated,
            "bypass": bypass_count,
            "normal": normal_count
        }
    finally:
        conn.close()

def get_all_devices(query: str = None, status_filter: str = None, type_filter: str = None, limit: int = 300) -> list:
    """Retrieve full list of captured devices with online status and relative timestamps."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        now = int(time.time())

        sql = """
            SELECT device_id, player_uid, player_name, player_type, ip_address,
                   user_agent, app_version, status, ban_reason, terminate_reason,
                   first_seen, last_seen
            FROM devices
        """
        conditions = []
        params = []

        if query:
            q = f"%{query.strip()}%"
            conditions.append("(device_id LIKE ? OR player_uid LIKE ? OR player_name LIKE ? OR ip_address LIKE ?)")
            params.extend([q, q, q, q])

        if status_filter and status_filter.lower() != "all":
            conditions.append("status = ?")
            params.append(status_filter.lower())

        if type_filter and type_filter.lower() != "all":
            if type_filter.lower() == "bypass":
                conditions.append("LOWER(player_type) LIKE '%bypass%'")
            elif type_filter.lower() == "normal":
                conditions.append("LOWER(player_type) NOT LIKE '%bypass%'")

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        device_list = []
        for row in rows:
            dev_id, p_uid, p_name, p_type, ip, ua, ver, status, ban_r, term_r, f_seen, l_seen = row
            is_online = (now - (l_seen or 0)) < 300

            diff = now - (l_seen or 0)
            if diff < 60:
                rel_time = f"{diff}s ago"
            elif diff < 3600:
                rel_time = f"{diff // 60}m ago"
            elif diff < 86400:
                rel_time = f"{diff // 3600}h ago"
            else:
                rel_time = f"{diff // 86400}d ago"

            readable_last = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(l_seen)) if l_seen else "Never"
            readable_first = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(f_seen)) if f_seen else "Never"

            device_list.append({
                "device_id": dev_id,
                "player_uid": p_uid or "—",
                "player_name": p_name or "—",
                "player_type": p_type or "normal",
                "ip_address": ip or "Unknown",
                "user_agent": ua or "",
                "app_version": ver or "1.0",
                "status": status or "active",
                "ban_reason": ban_r or "",
                "terminate_reason": term_r or "",
                "first_seen": f_seen,
                "last_seen": l_seen,
                "readable_last": readable_last,
                "readable_first": readable_first,
                "relative_last": rel_time,
                "is_online": is_online
            })
        return device_list
    finally:
        conn.close()


def get_cooldown_data(ip: str, client_token: str, discord_id: str = None):
    """Check cooldown status across IP, session cookie, and Discord ID to eliminate bypasses."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        identifiers = [ip, client_token]
        if discord_id:
            identifiers.append(f"discord_{discord_id}")

        placeholders = ",".join("?" for _ in identifiers)
        cursor.execute(
            f"SELECT MAX(last_claim_timestamp), last_claimed_uid FROM rate_limits WHERE identifier IN ({placeholders})",
            identifiers
        )
        row = cursor.fetchone()
        last_claim_timestamp = row[0] if (row and row[0]) else 0
        last_claimed_uid = row[1] if (row and row[1]) else None
        current_time = int(time.time())
        elapsed = current_time - last_claim_timestamp
        remaining = max(0, COOLDOWN_SECONDS - elapsed) if last_claim_timestamp > 0 else 0

        return {
            "last_claim_timestamp": last_claim_timestamp,
            "last_claimed_uid": last_claimed_uid,
            "cooldown_remaining": remaining,
            "can_claim": remaining == 0,
            "current_time": current_time
        }
    finally:
        conn.close()

def record_claim(ip: str, client_token: str, uid: str, current_time: int, discord_id: str = None):
    """Atomically record claim timestamp across IP, session token, and Discord ID without journal disk artifacts."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        effective_discord_id = discord_id or ip
        identifiers = [ip, client_token]
        if discord_id:
            identifiers.append(f"discord_{discord_id}")

        for identifier in identifiers:
            cursor.execute("""
                INSERT INTO rate_limits (identifier, last_claim_timestamp, last_claimed_uid)
                VALUES (?, ?, ?)
                ON CONFLICT(identifier) DO UPDATE SET
                    last_claim_timestamp = excluded.last_claim_timestamp,
                    last_claimed_uid = excluded.last_claimed_uid
            """, (identifier, current_time, uid))

        cursor.execute("""
            INSERT INTO registered_uids (uid, discord_id, identifier, added_timestamp, expiry_days, claim_type)
            VALUES (?, ?, ?, ?, 1, 'free_web')
            ON CONFLICT(uid) DO UPDATE SET
                discord_id = excluded.discord_id,
                identifier = excluded.identifier,
                added_timestamp = excluded.added_timestamp,
                expiry_days = 1,
                claim_type = 'free_web'
        """, (uid, effective_discord_id, ip, current_time))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def is_uid_already_active(uid: str, current_time: int) -> bool:
    """Verify if UID has already been whitelisted within an active window."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT added_timestamp, expiry_days FROM registered_uids WHERE uid = ?",
            (uid,)
        )
        row = cursor.fetchone()
        if row:
            added_ts, expiry_days = row
            if added_ts + (expiry_days * 86400) > current_time:
                return True
        return False
    finally:
        conn.close()

# ==================== EXTERNAL API CLIENT ====================
def call_upstream_uid_api(uid: str, days: int = 1) -> dict:
    """Secure upstream whitelist API call. No keys or URLs leak to client."""
    current_key = os.getenv("API_KEY") or API_KEY or "GTCAPI-BB05166D26D7EC81F86BA3C893998613"
    current_url = os.getenv("API_URL") or API_URL or "https://gtccheats.xyz/Api/uidbypassapi/api_user.php"
    action = os.getenv("API_ACTION_ADD", API_ACTION_ADD)

    url = f"{current_url}?action={action}"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": current_key
    }
    payload = {
        "account_id": str(uid),
        "for_days": int(days)
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=12)
        try:
            data = response.json()
        except Exception:
            data = {"success": False, "message": "Unexpected upstream gateway response."}

        is_success = (response.status_code in [200, 201]) or (data.get("success") is True) or ("already" in str(data.get("message", "")).lower())
        message = data.get("message", "Whitelist request processed.") if is_success else data.get("message", "Upstream server rejected request.")

        return {
            "status": 200 if is_success else 502,
            "success": is_success,
            "message": message
        }
    except requests.exceptions.Timeout:
        return {"status": 504, "success": False, "message": "Upstream gate timed out. Please try again in a few moments."}
    except Exception:
        return {"status": 500, "success": False, "message": "Network error while connecting to activation gateway."}

def admin_add_registered_uid(uid: str, days: int = 1, claim_type: str = "admin", identifier: str = "ADMIN") -> bool:
    """Insert or update a registered UID from the admin panel."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        current_time = int(time.time())
        cursor.execute("""
            INSERT INTO registered_uids (uid, discord_id, identifier, added_timestamp, expiry_days, claim_type)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                discord_id = excluded.discord_id,
                identifier = excluded.identifier,
                added_timestamp = excluded.added_timestamp,
                expiry_days = excluded.expiry_days,
                claim_type = excluded.claim_type
        """, (str(uid), "admin", identifier, current_time, int(days), claim_type))
        conn.commit()
        return True
    finally:
        conn.close()

def delete_upstream_uid_api(uid: str) -> dict:
    """Delete a UID from the upstream whitelist API using configured API_ACTION_DELETE."""
    current_key = os.getenv("API_KEY") or API_KEY or "GTCAPI-BB05166D26D7EC81F86BA3C893998613"
    current_url = os.getenv("API_URL") or API_URL or "https://gtccheats.xyz/Api/uidbypassapi/api_user.php"
    action = os.getenv("API_ACTION_DELETE", API_ACTION_DELETE)

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": current_key
    }
    payload = {
        "account_id": str(uid)
    }

    try:
        url = f"{current_url}?action={action}"
        response = requests.post(url, json=payload, headers=headers, timeout=12)
        try:
            data = response.json()
        except Exception:
            data = {"success": False, "message": "Unexpected upstream gateway response."}

        # If configured action was 'delete' but upstream server expects 'remove', auto-fallback
        if not data.get("success") and "Invalid action" in str(data.get("message", "")) and action != "remove":
            fallback_url = f"{current_url}?action=remove"
            response = requests.post(fallback_url, json=payload, headers=headers, timeout=12)
            try:
                data = response.json()
            except Exception:
                data = {"success": False, "message": "Unexpected upstream gateway response."}

        is_success = (
            (response.status_code in [200, 201])
            or (data.get("success") is True)
            or ("not found" in str(data.get("message", "")).lower())
            or ("deleted" in str(data.get("message", "")).lower())
            or ("removed" in str(data.get("message", "")).lower())
        )
        return {
            "status": 200 if is_success else response.status_code,
            "success": is_success,
            "message": data.get("message", "Upstream UID deletion processed.")
        }
    except requests.exceptions.Timeout:
        return {"status": 504, "success": False, "message": "Upstream gate timed out."}
    except Exception as e:
        return {"status": 500, "success": False, "message": f"Network error: {str(e)}"}

# ==================== DISCORD OAUTH2 & MEMBER VERIFICATION ====================
def get_discord_redirect_uri():
    if config.DISCORD_REDIRECT_URI:
        return config.DISCORD_REDIRECT_URI
    base = request.host_url.rstrip("/")
    forwarded_proto = request.headers.get("X-Forwarded-Proto")
    if forwarded_proto == "https" or (is_vercel and base.startswith("http://")):
        base = "https://" + base.split("://", 1)[-1]
    return f"{base}/auth/discord/callback"

def get_discord_oauth_url():
    if not config.DISCORD_CLIENT_ID:
        return None
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": get_discord_redirect_uri(),
        "response_type": "code",
        "scope": "identify guilds guilds.join",
        "prompt": "consent"
    }
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"

def exchange_discord_code(code: str):
    data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": get_discord_redirect_uri()
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post("https://discord.com/api/v10/oauth2/token", data=data, headers=headers, timeout=12)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_discord_user_profile(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def add_and_verify_guild_member(user_id: str, access_token: str) -> bool:
    """Automatically joins user to guild via guilds.join and verifies membership."""
    guild_id = config.MEOW_GUILD_ID or config.GUILD_ID
    bot_token = config.DISCORD_BOT_TOKEN
    is_member = False

    # 1. Bot Token Join Attempt: PUT /guilds/{guild_id}/members/{user_id}
    if bot_token and guild_id and access_token:
        join_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
        join_headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json"
        }
        try:
            put_res = requests.put(join_url, json={"access_token": access_token}, headers=join_headers, timeout=10)
            if put_res.status_code in (201, 204):
                is_member = True
        except Exception:
            pass

        # Check membership via Bot API: GET /guilds/{guild_id}/members/{user_id}
        if not is_member:
            try:
                get_res = requests.get(join_url, headers=join_headers, timeout=10)
                if get_res.status_code == 200:
                    is_member = True
            except Exception:
                pass

    # 2. Dual fallback: check user's guilds list via user access_token
    if not is_member and access_token:
        try:
            g_res = requests.get(
                "https://discord.com/api/v10/users/@me/guilds",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10
            )
            if g_res.status_code == 200:
                user_guilds = g_res.json()
                for g in user_guilds:
                    if str(g.get("id")) in (str(config.MEOW_GUILD_ID), str(config.GUILD_ID)):
                        is_member = True
                        break
        except Exception:
            pass

    return is_member

# ==================== ROUTES ====================
@app.route("/")
def index():
    ip, token = get_client_identifiers()
    discord_user = session.get("discord_user")
    discord_id = discord_user.get("id") if discord_user else None

    status_info = get_cooldown_data(ip, token, discord_id=discord_id)
    maintenance = is_maintenance_mode()

    discord_oauth_enabled = bool(config.DISCORD_CLIENT_ID)
    is_discord_verified = (not discord_oauth_enabled) or (discord_user and discord_user.get("in_guild"))

    can_claim = status_info["can_claim"] and not maintenance and is_discord_verified

    return render_template(
        "index.html",
        cooldown_remaining=status_info["cooldown_remaining"],
        can_claim=can_claim,
        maintenance_mode=maintenance,
        last_claimed_uid=status_info["last_claimed_uid"],
        last_claim_timestamp=status_info["last_claim_timestamp"],
        current_time=status_info["current_time"],
        discord_user=discord_user,
        discord_oauth_enabled=discord_oauth_enabled,
        is_discord_verified=is_discord_verified,
        discord_oauth_url=get_discord_oauth_url(),
        NOVA_SERVER_INVITE=DISCORD_SERVER_INVITE,
        MEOW_SERVER_INVITE=MEOW_DISCORD_SERVER_INVITE
    )

@app.route("/auth/discord")
def auth_discord():
    oauth_url = get_discord_oauth_url()
    if not oauth_url:
        return redirect("/?error=discord_oauth_not_configured")
    return redirect(oauth_url)

@app.route("/auth/discord/callback")
def auth_discord_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")

    token_data = exchange_discord_code(code)
    if not token_data or "access_token" not in token_data:
        return redirect("/?auth_error=token_failed")

    access_token = token_data["access_token"]
    user_info = get_discord_user_profile(access_token)
    if not user_info or "id" not in user_info:
        return redirect("/?auth_error=profile_failed")

    user_id = str(user_info["id"])
    avatar = user_info.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=128" if avatar else "https://cdn.discordapp.com/embed/avatars/0.png"

    in_guild = add_and_verify_guild_member(user_id, access_token)

    session["discord_user"] = {
        "id": user_id,
        "username": user_info.get("username", "Member"),
        "global_name": user_info.get("global_name") or user_info.get("username", "Member"),
        "avatar": avatar_url,
        "access_token": access_token,
        "in_guild": in_guild
    }
    if not in_guild:
        return redirect("/?join_prompt=1")
    return redirect("/?verified=1")

@app.route("/auth/discord/check-guild")
def auth_discord_check_guild():
    user = session.get("discord_user")
    if not user:
        return jsonify({"success": False, "authenticated": False}), 401
    in_guild = add_and_verify_guild_member(user["id"], user.get("access_token"))
    user["in_guild"] = in_guild
    session["discord_user"] = user
    session.modified = True
    return jsonify({"success": True, "in_guild": in_guild})

@app.route("/auth/discord/logout")
def auth_discord_logout():
    session.pop("discord_user", None)
    return redirect("/")

@app.route("/api/status", methods=["GET"])
def api_status():
    """Live status query endpoint for client countdown synchronization and device verification."""
    ip, token = get_client_identifiers()
    discord_user = session.get("discord_user")
    discord_id = discord_user.get("id") if discord_user else None
    status_info = get_cooldown_data(ip, token, discord_id=discord_id)
    maintenance = is_maintenance_mode()

    discord_oauth_enabled = bool(config.DISCORD_CLIENT_ID)
    is_discord_verified = (not discord_oauth_enabled) or (discord_user and discord_user.get("in_guild"))

    device_id = request.args.get("device_id", "").strip()
    device_status = "active"
    device_message = ""
    if device_id:
        dev = get_device_by_id(device_id)
        if dev:
            device_status = dev.get("status", "active")
            if device_status == "banned":
                device_message = dev.get("ban_reason") or "This device is permanently banned."
            elif device_status == "terminated":
                device_message = dev.get("terminate_reason") or "This device session has been terminated."

    can_claim = status_info["can_claim"] and not maintenance and is_discord_verified and (device_status == "active")

    return jsonify({
        "can_claim": can_claim,
        "cooldown_remaining": status_info["cooldown_remaining"],
        "maintenance_mode": maintenance,
        "current_time": status_info["current_time"],
        "last_claim_timestamp": status_info["last_claim_timestamp"],
        "last_claimed_uid": status_info["last_claimed_uid"],
        "device_status": device_status,
        "device_message": device_message,
        "discord_user": {
            "username": discord_user.get("username"),
            "global_name": discord_user.get("global_name"),
            "in_guild": discord_user.get("in_guild")
        } if discord_user else None,
        "is_discord_verified": is_discord_verified
    })

# ==================== DEVICE REPORT & HEARTBEAT API ====================
@app.route("/api/device/report", methods=["POST"])
@app.route("/api/device/heartbeat", methods=["POST"])
@app.route("/api/device/verify", methods=["POST", "GET"])
def api_device_report():
    """
    Primary endpoint for all player clients (bypass and normal players).
    Receives device ID, client type, and optional in-game UID.
    Returns enforcement directives:
      - 'allow' (status: active)
      - 'ban' (status: banned) -> client permanently locks
      - 'terminate' (status: terminated) -> client forcefully exits
    """
    if request.method == "GET":
        data = request.args
    else:
        data = request.get_json(silent=True) or request.form.to_dict() or {}

    device_id = str(data.get("device_id") or request.headers.get("X-Device-ID") or "").strip()
    if not device_id:
        return jsonify({
            "success": False,
            "allowed": False,
            "error": "missing_device_id",
            "message": "Device ID is required for verification."
        }), 400

    player_uid = str(data.get("player_uid") or data.get("uid") or "").strip()
    player_name = str(data.get("player_name") or data.get("ign") or data.get("name") or "").strip()
    player_type = str(data.get("player_type") or data.get("type") or "normal").strip().lower()
    app_version = str(data.get("app_version") or data.get("version") or "1.0").strip()

    ip = get_client_ip()
    user_agent = request.headers.get("User-Agent", "Player Client")

    dev = register_or_update_device(
        device_id=device_id,
        player_uid=player_uid if player_uid else None,
        player_name=player_name if player_name else None,
        player_type=player_type,
        ip=ip,
        user_agent=user_agent,
        app_version=app_version
    )

    status = dev.get("status", "active")

    if status == "banned":
        reason = dev.get("ban_reason") or "This device has been permanently banned by administrator."
        return jsonify({
            "success": False,
            "allowed": False,
            "status": "banned",
            "action": "ban",
            "device_id": device_id,
            "reason": reason,
            "message": f"DEVICE BANNED: {reason}"
        }), 403

    if status == "terminated":
        reason = dev.get("terminate_reason") or "Your game session has been terminated by administrator."
        return jsonify({
            "success": False,
            "allowed": False,
            "status": "terminated",
            "action": "terminate",
            "device_id": device_id,
            "reason": reason,
            "message": f"SESSION TERMINATED: {reason}"
        }), 403

    return jsonify({
        "success": True,
        "allowed": True,
        "status": "active",
        "action": "allow",
        "device_id": device_id,
        "player_type": dev.get("player_type", "normal"),
        "message": "Device verified and operational."
    }), 200

@app.route("/api/device/status/<device_id>", methods=["GET"])
def api_device_status(device_id: str):
    """Query status of a specific device ID."""
    dev = get_device_by_id(device_id)
    if not dev:
        return jsonify({
            "success": False,
            "status": "unknown",
            "allowed": False,
            "message": "Device not registered in database."
        }), 404

    return jsonify({
        "success": True,
        "device_id": dev["device_id"],
        "status": dev["status"],
        "allowed": dev["status"] == "active",
        "action": "allow" if dev["status"] == "active" else dev["status"],
        "ban_reason": dev["ban_reason"],
        "terminate_reason": dev["terminate_reason"],
        "player_type": dev["player_type"],
        "player_uid": dev["player_uid"],
        "last_seen": dev["last_seen"]
    })

# ==================== PUBLIC UID CLAIM / WHITELIST API ====================
@app.route("/api/whitelist", methods=["POST"])
@app.route("/api/claim", methods=["POST"])
def api_claim():
    """Whitelists a numeric UID with strict 24-hour rate limit, anti-glitch lock, and device enforcement."""
    if is_maintenance_mode():
        return jsonify({
            "success": False,
            "maintenance": True,
            "message": "Free UID Whitelist is currently under maintenance. Claims are temporarily paused by administration."
        }), 503

    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip()

    # Device ID Enforcement: Check if client device is banned or terminated immediately
    device_id = str(data.get("device_id") or request.headers.get("X-Device-ID") or "").strip()
    if device_id:
        dev = get_device_by_id(device_id)
        if dev:
            if dev.get("status") == "banned":
                ban_msg = dev.get("ban_reason") or "This device is permanently banned from claiming UIDs."
                return jsonify({
                    "success": False,
                    "device_banned": True,
                    "message": f"DEVICE BANNED: {ban_msg}"
                }), 403
            if dev.get("status") == "terminated":
                term_msg = dev.get("terminate_reason") or "This device session has been terminated by administrator."
                return jsonify({
                    "success": False,
                    "device_terminated": True,
                    "message": f"SESSION TERMINATED: {term_msg}"
                }), 403

    discord_user = session.get("discord_user")
    discord_id = discord_user.get("id") if discord_user else None

    # Discord Membership gate check
    if bool(config.DISCORD_CLIENT_ID) and config.REQUIRE_DISCORD_LOGIN:
        if not discord_user:
            return jsonify({
                "success": False,
                "require_discord": True,
                "message": "Discord verification required! Please authorize your Discord account before claiming."
            }), 401

        if not discord_user.get("in_guild"):
            # Real-time re-verify membership in case they just clicked join
            if add_and_verify_guild_member(discord_user["id"], discord_user.get("access_token")):
                discord_user["in_guild"] = True
                session["discord_user"] = discord_user
            else:
                return jsonify({
                    "success": False,
                    "require_guild": True,
                    "message": "You must join the MeowGG Discord server before whitelisting your UID!"
                }), 403

    # UID Validation: numeric only, 5 to 20 digits
    if not uid or not uid.isdigit():
        return jsonify({"success": False, "message": "Invalid UID! Please provide a numeric Account ID."}), 400

    if len(uid) < 5 or len(uid) > 20:
        return jsonify({"success": False, "message": "UID length must be between 5 and 20 digits."}), 400

    ip, token = get_client_identifiers()
    current_time = int(time.time())

    # Threading lock prevents parallel requests from racing past the cooldown check
    with claim_lock:
        status_info = get_cooldown_data(ip, token, discord_id=discord_id)
        if not status_info["can_claim"]:
            seconds_left = status_info["cooldown_remaining"]
            h = seconds_left // 3600
            m = (seconds_left % 3600) // 60
            s = seconds_left % 60
            return jsonify({
                "success": False,
                "message": f"Daily limit reached! 24 ghante me sirf 1 UID add ho sakta hai. Next claim unlock in: {h}h {m}m {s}s.",
                "cooldown_remaining": seconds_left
            }), 429

        # Verify UID is not already registered and currently active
        if is_uid_already_active(uid, current_time):
            return jsonify({
                "success": False,
                "message": f"UID {uid} is already active in the whitelist system."
            }), 400

        # Execute upstream API call
        upstream_result = call_upstream_uid_api(uid)
        if not upstream_result.get("success"):
            return jsonify({
                "success": False,
                "message": upstream_result.get("message", "Upstream server rejected registration.")
            }), upstream_result.get("status", 502)

        # Record claim and enforce 24-hour cooldown
        try:
            record_claim(ip, token, uid, current_time, discord_id=discord_id)
            if device_id:
                register_or_update_device(
                    device_id=device_id,
                    player_uid=uid,
                    player_type="web",
                    ip=ip,
                    user_agent=request.headers.get("User-Agent", "Web Portal")
                )
        except Exception:
            return jsonify({"success": False, "message": "Database write error. Please try again."}), 500

        return jsonify({
            "success": True,
            "message": f"SUCCESS! UID {uid} has been activated for 24 hours.",
            "uid": uid,
            "validity": "24 Hours (1 Day)",
            "cooldown_remaining": COOLDOWN_SECONDS
        }), 200

# ==================== ADMIN PANEL ROUTES (/admin) ====================
@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
    """Admin panel: authentication with user 72 / pass 12, device ban/terminate manager, maintenance toggle, and claim monitoring."""
    if request.method == "POST":
        username = str(request.form.get("username", "")).strip()
        password = str(request.form.get("password", "")).strip()

        if username == ADMIN_PANEL_USER and password == ADMIN_PANEL_PASS:
            session["admin_authenticated"] = True
            return redirect("/admin")
        else:
            return render_template("admin_login.html", error="Invalid admin username or password.")

    # GET Request
    if not session.get("admin_authenticated"):
        return render_template("admin_login.html")

    active_tab = request.args.get("tab", "devices").strip()
    search_q = request.args.get("q", "").strip()
    device_q = request.args.get("dq", "").strip()
    device_status = request.args.get("status", "all").strip()
    device_type = request.args.get("type", "all").strip()
    msg = request.args.get("msg", "").strip()
    error = request.args.get("error", "").strip()

    stats = get_admin_stats()
    device_stats = get_device_stats()
    claims = get_all_claims(query=search_q if search_q else None)
    devices = get_all_devices(
        query=device_q if device_q else None,
        status_filter=device_status,
        type_filter=device_type
    )

    return render_template(
        "admin.html",
        stats=stats,
        device_stats=device_stats,
        claims=claims,
        devices=devices,
        active_tab=active_tab,
        search_q=search_q,
        device_q=device_q,
        device_status=device_status,
        device_type=device_type,
        msg=msg,
        error=error,
        maintenance_mode=stats["maintenance_mode"],
        api_action_add=API_ACTION_ADD,
        is_vercel=is_vercel
    )

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect("/admin")

@app.route("/admin/add-uid", methods=["POST"])
def admin_add_uid():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    uid = (request.form.get("uid") or (request.json.get("uid") if request.is_json else "") or "").strip()
    days_val = request.form.get("days") or (request.json.get("days") if request.is_json else 1)
    try:
        days = max(1, min(3650, int(days_val)))
    except (TypeError, ValueError):
        days = 1

    if not uid:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "UID cannot be empty."}), 400
        return redirect("/admin?tab=uids&error=UID+cannot+be+empty")

    if not uid.isdigit():
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "UID must contain numeric digits only."}), 400
        return redirect("/admin?tab=uids&error=UID+must+contain+numeric+digits+only")

    # Call upstream API with configured API_ACTION_ADD
    res = call_upstream_uid_api(uid, days=days)
    if not res.get("success"):
        err_msg = res.get("message", "Upstream server rejected registration.")
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": err_msg}), 400
        return redirect(f"/admin?tab=uids&error={requests.utils.quote(err_msg)}")

    # Record in database under admin
    admin_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "127.0.0.1"
    admin_add_registered_uid(uid, days=days, claim_type="admin", identifier=f"admin_{admin_ip}")

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"UID {uid} whitelisted successfully for {days} day(s).",
            "uid": uid,
            "days": days
        })

    return redirect(f"/admin?tab=uids&msg=UID+{uid}+whitelisted+successfully+for+{days}+day(s)")

@app.route("/admin/toggle-maintenance", methods=["POST"])
def admin_toggle_maintenance():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    current = is_maintenance_mode()
    new_state = not current
    set_maintenance_mode(new_state)
    status_str = "ON" if new_state else "OFF"
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "maintenance_mode": new_state,
            "message": f"Maintenance mode successfully turned {status_str}."
        })
    return redirect(f"/admin?msg=Maintenance+mode+successfully+turned+{status_str}")

@app.route("/admin/reset-cooldown", methods=["POST"])
def admin_reset_cooldown():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    target = request.form.get("target", "").strip()
    if target:
        reset_identifier_cooldown(target)
    return redirect("/admin?tab=uids")

@app.route("/admin/reset-all-cooldowns", methods=["POST"])
def admin_reset_all_cooldowns():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    reset_all_rate_limits()
    return redirect("/admin?tab=uids")

@app.route("/admin/delete-uid", methods=["POST"])
def admin_delete_uid():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    uid = (request.form.get("uid") or (request.json.get("uid") if request.is_json else "") or "").strip()
    if uid:
        delete_upstream_uid_api(uid)
        delete_claimed_uid(uid)
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "success": True,
                "message": f"UID {uid} deleted successfully from database and upstream whitelist."
            })
    return redirect("/admin?tab=uids")

# ==================== ADMIN DEVICE MANAGEMENT ROUTES ====================
@app.route("/admin/device/ban", methods=["POST"])
def admin_device_ban():
    """Ban a device ID permanently with an optional violation reason."""
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    device_id = (request.form.get("device_id") or (request.json.get("device_id") if request.is_json else "") or "").strip()
    reason = (request.form.get("reason") or (request.json.get("reason") if request.is_json else "Device banned by administrator") or "").strip()

    if not device_id:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Device ID is required."}), 400
        return redirect("/admin?tab=devices&error=Device+ID+is+required")

    set_device_status(device_id, "banned", reason=reason or "Banned by administrator")

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"Device {device_id} has been banned.",
            "device_id": device_id,
            "status": "banned"
        })

    return redirect(f"/admin?tab=devices&msg=Device+{device_id}+has+been+banned")

@app.route("/admin/device/terminate", methods=["POST"])
def admin_device_terminate():
    """Terminate a player's device session immediately. Client agent auto-kills process."""
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    device_id = (request.form.get("device_id") or (request.json.get("device_id") if request.is_json else "") or "").strip()
    reason = (request.form.get("reason") or (request.json.get("reason") if request.is_json else "Session terminated by administrator") or "").strip()

    if not device_id:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Device ID is required."}), 400
        return redirect("/admin?tab=devices&error=Device+ID+is+required")

    set_device_status(device_id, "terminated", reason=reason or "Session terminated by administrator")

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"Device {device_id} session terminated. Kill directive active.",
            "device_id": device_id,
            "status": "terminated"
        })

    return redirect(f"/admin?tab=devices&msg=Device+{device_id}+session+terminated")

@app.route("/admin/device/unban", methods=["POST"])
def admin_device_unban():
    """Restore a banned or terminated device back to active operational state."""
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    device_id = (request.form.get("device_id") or (request.json.get("device_id") if request.is_json else "") or "").strip()

    if not device_id:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Device ID is required."}), 400
        return redirect("/admin?tab=devices&error=Device+ID+is+required")

    set_device_status(device_id, "active", reason="")

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"Device {device_id} restored to active status.",
            "device_id": device_id,
            "status": "active"
        })

    return redirect(f"/admin?tab=devices&msg=Device+{device_id}+unbanned+successfully")

@app.route("/admin/device/delete", methods=["POST"])
def admin_device_delete():
    """Delete a device record from database."""
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    device_id = (request.form.get("device_id") or (request.json.get("device_id") if request.is_json else "") or "").strip()

    if not device_id:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Device ID is required."}), 400
        return redirect("/admin?tab=devices&error=Device+ID+is+required")

    delete_device_record(device_id)

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"Device {device_id} deleted from tracking database."
        })

    return redirect(f"/admin?tab=devices&msg=Device+{device_id}+deleted+from+records")

@app.route("/admin/device/add", methods=["POST"])
def admin_device_add():
    """Manually register or pre-ban a device ID from the admin console."""
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    device_id = (request.form.get("device_id") or (request.json.get("device_id") if request.is_json else "") or "").strip()
    status = (request.form.get("status") or (request.json.get("status") if request.is_json else "active") or "active").strip().lower()
    player_type = (request.form.get("player_type") or (request.json.get("player_type") if request.is_json else "normal") or "normal").strip().lower()
    player_uid = (request.form.get("player_uid") or (request.json.get("player_uid") if request.is_json else "") or "").strip()
    player_name = (request.form.get("player_name") or (request.json.get("player_name") if request.is_json else "") or "").strip()
    reason = (request.form.get("reason") or (request.json.get("reason") if request.is_json else "") or "").strip()

    if not device_id:
        if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Device ID cannot be empty."}), 400
        return redirect("/admin?tab=devices&error=Device+ID+cannot+be+empty")

    admin_ip = get_client_ip()
    register_or_update_device(
        device_id=device_id,
        player_uid=player_uid,
        player_name=player_name,
        player_type=player_type,
        ip=f"admin_{admin_ip}",
        user_agent="Admin Manual Entry",
        app_version="Manual"
    )

    if status in ("banned", "terminated"):
        set_device_status(device_id, status, reason=reason or f"Manually set to {status} by administrator")

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "success": True,
            "message": f"Device {device_id} saved with status: {status}.",
            "device_id": device_id,
            "status": status
        })

    return redirect(f"/admin?tab=devices&msg=Device+{device_id}+saved+successfully")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[*] MeowGG Portal initialized on port {port}")
    # debug=False strictly enforced to conceal debugger and prevent stack trace leaks
    app.run(host="0.0.0.0", port=port, debug=False)

