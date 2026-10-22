import os
import time
import sqlite3
import secrets
import threading
import requests
from flask import Flask, render_template, request, session, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

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
ADMIN_PANEL_USER = os.getenv("ADMIN_PANEL_USER", "72")
ADMIN_PANEL_PASS = os.getenv("ADMIN_PANEL_PASS", "12")

# API & Server Configuration strictly loaded from environment variables
API_KEY = os.getenv("API_KEY")
API_URL = os.getenv("API_URL")

DISCORD_SERVER_INVITE = os.getenv("DISCORD_SERVER_INVITE", "https://discord.gg/meowgg")
MEOW_DISCORD_SERVER_INVITE = os.getenv("MEOW_DISCORD_SERVER_INVITE", "https://discord.gg/meowgg")

# Vercel Serverless environment compatibility:
# /var/task is read-only on Vercel. Writable SQLite database must reside in temp directory (/tmp).
if is_vercel:
    import tempfile
    import shutil
    tmp_dir = "/tmp" if os.path.exists("/tmp") else tempfile.gettempdir()
    tmp_db = os.path.join(tmp_dir, "database.db")
    seed_db = os.path.join(BASE_DIR, "database.db")
    if not os.path.exists(tmp_db) and os.path.exists(seed_db):
        try:
            shutil.copy2(seed_db, tmp_db)
        except Exception:
            pass
    DB_PATH = tmp_db
else:
    DB_PATH = os.path.abspath(os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "database.db")))

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
    cursor.execute("""
        INSERT OR IGNORE INTO system_settings (key, value)
        VALUES ('maintenance_mode', '0')
    """)

    conn.commit()
    conn.close()

init_db()

# ==================== ADMIN & MAINTENANCE HELPERS ====================
def is_maintenance_mode() -> bool:
    """Check if free UID claims are paused for maintenance."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = 'maintenance_mode'")
        row = cursor.fetchone()
        return bool(row and row[0] == '1')
    except Exception:
        return False
    finally:
        conn.close()

def set_maintenance_mode(enabled: bool):
    """Enable or disable free UID claims maintenance mode."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        val = '1' if enabled else '0'
        cursor.execute("""
            INSERT INTO system_settings (key, value)
            VALUES ('maintenance_mode', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (val,))
        conn.commit()
    finally:
        conn.close()

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


def get_cooldown_data(ip: str, client_token: str):
    """Check cooldown status across IP and session cookie to eliminate bypasses."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(last_claim_timestamp), last_claimed_uid FROM rate_limits WHERE identifier IN (?, ?)",
            (ip, client_token)
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

def record_claim(ip: str, client_token: str, uid: str, current_time: int):
    """Atomically record claim timestamp across IP and session token without journal disk artifacts."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        # Record for both IP and client token
        for identifier in (ip, client_token):
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
        """, (uid, ip, ip, current_time))

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
def call_upstream_uid_api(uid: str) -> dict:
    """Secure upstream whitelist API call. No keys or URLs leak to client."""
    if not API_KEY or not API_URL:
        return {"status": 500, "message": "Server configuration error. API credentials missing in environment."}

    url = f"{API_URL}?action=add"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": API_KEY
    }
    payload = {
        "account_id": str(uid),
        "for_days": 1
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

# ==================== ROUTES ====================
@app.route("/")
def index():
    ip, token = get_client_identifiers()
    status_info = get_cooldown_data(ip, token)
    maintenance = is_maintenance_mode()
    return render_template(
        "index.html",
        cooldown_remaining=status_info["cooldown_remaining"],
        can_claim=status_info["can_claim"] and not maintenance,
        maintenance_mode=maintenance,
        last_claimed_uid=status_info["last_claimed_uid"],
        last_claim_timestamp=status_info["last_claim_timestamp"],
        current_time=status_info["current_time"],
        NOVA_SERVER_INVITE=DISCORD_SERVER_INVITE,
        MEOW_SERVER_INVITE=MEOW_DISCORD_SERVER_INVITE
    )

@app.route("/api/status", methods=["GET"])
def api_status():
    """Live status query endpoint for client countdown synchronization."""
    ip, token = get_client_identifiers()
    status_info = get_cooldown_data(ip, token)
    maintenance = is_maintenance_mode()
    return jsonify({
        "can_claim": status_info["can_claim"] and not maintenance,
        "cooldown_remaining": status_info["cooldown_remaining"],
        "maintenance_mode": maintenance,
        "current_time": status_info["current_time"],
        "last_claim_timestamp": status_info["last_claim_timestamp"],
        "last_claimed_uid": status_info["last_claimed_uid"]
    })

@app.route("/api/whitelist", methods=["POST"])
@app.route("/api/claim", methods=["POST"])
def api_claim():
    """Whitelists a numeric UID with strict 24-hour rate limit and anti-glitch concurrency lock."""
    if is_maintenance_mode():
        return jsonify({
            "success": False,
            "maintenance": True,
            "message": "Free UID Whitelist is currently under maintenance. Claims are temporarily paused by administration."
        }), 503

    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip()

    # UID Validation: numeric only, 5 to 20 digits
    if not uid or not uid.isdigit():
        return jsonify({"success": False, "message": "Invalid UID! Please provide a numeric Account ID."}), 400

    if len(uid) < 5 or len(uid) > 20:
        return jsonify({"success": False, "message": "UID length must be between 5 and 20 digits."}), 400

    ip, token = get_client_identifiers()
    current_time = int(time.time())

    # Threading lock prevents parallel requests from racing past the cooldown check
    with claim_lock:
        status_info = get_cooldown_data(ip, token)
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
            record_claim(ip, token, uid, current_time)
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
    """Admin panel: authentication with user 72 / pass 12, maintenance toggle, and claim monitoring."""
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

    search_q = request.args.get("q", "").strip()
    stats = get_admin_stats()
    claims = get_all_claims(query=search_q if search_q else None)

    return render_template(
        "admin.html",
        stats=stats,
        claims=claims,
        search_q=search_q,
        maintenance_mode=stats["maintenance_mode"]
    )

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect("/admin")

@app.route("/admin/toggle-maintenance", methods=["POST"])
def admin_toggle_maintenance():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    current = is_maintenance_mode()
    set_maintenance_mode(not current)
    return redirect("/admin")

@app.route("/admin/reset-cooldown", methods=["POST"])
def admin_reset_cooldown():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    target = request.form.get("target", "").strip()
    if target:
        reset_identifier_cooldown(target)
    return redirect("/admin")

@app.route("/admin/reset-all-cooldowns", methods=["POST"])
def admin_reset_all_cooldowns():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    reset_all_rate_limits()
    return redirect("/admin")

@app.route("/admin/delete-uid", methods=["POST"])
def admin_delete_uid():
    if not session.get("admin_authenticated"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    uid = request.form.get("uid", "").strip()
    if uid:
        delete_claimed_uid(uid)
    return redirect("/admin")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[*] MeowGG Portal initialized on port {port}")
    # debug=False strictly enforced to conceal debugger and prevent stack trace leaks
    app.run(host="0.0.0.0", port=port, debug=False)

