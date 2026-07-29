import os
import json
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Backend selection
#   - If DATABASE_URL is set (e.g. Render PostgreSQL), use Postgres.
#   - Otherwise fall back to a local SQLite file (local development).
# All SQL below is written with "?" placeholders and translated to "%s" for
# Postgres via _q(), so the same statements work on both engines.
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "database.db"))
default_config_path = BASE_DIR.parent / "SAMC Micro digest" / "config.json"
if not default_config_path.parent.exists():
    default_config_path = BASE_DIR / "config.json"
CONFIG_JSON_PATH = os.environ.get("CONFIG_PATH", str(default_config_path))

DEFAULT_ADMIN_MOBILE = "9791117131"
PASSWORD_MIN_LEN = 6

if USE_PG:
    import psycopg2
    import psycopg2.extras

    _AUTOINC = "SERIAL PRIMARY KEY"

    def get_db_conn():
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
else:
    import sqlite3

    _AUTOINC = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def get_db_conn():
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _q(sql: str) -> str:
    """Translate ? placeholders to %s when running on Postgres."""
    return sql.replace("?", "%s") if USE_PG else sql


def _upsert_config_sql() -> str:
    if USE_PG:
        return ("INSERT INTO configs (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    return "INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)"


def _safe_add_column(conn, cursor, table: str, coldef: str) -> None:
    """Add a column if it does not already exist (portable, idempotent)."""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db():
    conn = get_db_conn()
    cursor = conn.cursor()

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS users (
        id {_AUTOINC},
        mobile TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        must_change_password INTEGER NOT NULL DEFAULT 0,
        reset_requested INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS configs (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS logs (
        id {_AUTOINC},
        mobile TEXT NOT NULL,
        action TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()

    # Migrate older user tables that predate the new columns.
    _safe_add_column(conn, cursor, "users", "status TEXT NOT NULL DEFAULT 'active'")
    _safe_add_column(conn, cursor, "users", "must_change_password INTEGER NOT NULL DEFAULT 0")
    _safe_add_column(conn, cursor, "users", "reset_requested INTEGER NOT NULL DEFAULT 0")

    # Seed default admin (Mobile: 9791117131, Pass: admin)
    cursor.execute(_q("SELECT id FROM users WHERE mobile = ?"), (DEFAULT_ADMIN_MOBILE,))
    if not cursor.fetchone():
        h = generate_password_hash("admin")
        cursor.execute(
            _q("INSERT INTO users (mobile, password_hash, role, status) VALUES (?, ?, ?, ?)"),
            (DEFAULT_ADMIN_MOBILE, h, "admin", "active"),
        )
        conn.commit()
        print("[DB] Seeded default admin user: 9791117131 / admin")

    # Seed default configurations if configs table is empty
    cursor.execute("SELECT COUNT(*) AS n FROM configs")
    if cursor.fetchone()["n"] == 0:
        config_data = {}
        if Path(CONFIG_JSON_PATH).exists():
            try:
                config_data = json.loads(Path(CONFIG_JSON_PATH).read_text(encoding="utf-8"))
            except Exception:
                pass

        default_config = {
            "active_company": config_data.get("active_company", "amc"),
            "webhook_url": config_data.get("webhook_url", ""),
            "disclaimer_text": config_data.get("disclaimer_text", "Mutual Fund investments are subject to market risks, read all scheme related documents carefully."),
            "watermark_mode": config_data.get("watermark_mode", "both"),
            "brand_colors": json.dumps(config_data.get("brand_colors", {
                "bg_color": "#FCF9F2",
                "text_color": "#1A1A1A",
                "yellow_brand": "#F7B500",
                "gray_text": "#555555",
            })),
            "manual_override": 1 if config_data.get("manual_override", False) else 0,
            "overrides": json.dumps(config_data.get("overrides", {
                "date": "", "bse_value": "", "bse_change": "", "nse_value": "", "nse_change": "",
                "mid_value": "", "mid_change": "", "small_value": "", "small_change": "",
                "fii_value": "", "dii_value": "", "brent_value": "", "gold_value": "",
                "silver_value": "", "usdinr_value": "", "gsec_value": "", "pe_value": "",
                "vix_value": "", "us10y_value": "", "dxy_value": "", "midcap_pe_value": "", "smallcap_pe_value": "",
                "headlines": [],
            })),
        }
        upsert = _upsert_config_sql()
        for k, v in default_config.items():
            cursor.execute(upsert, (k, str(v)))
        conn.commit()
        print("[DB] Seeded default configurations.")
    conn.close()

    sync_db_to_json()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def sync_db_to_json():
    """Mirror the configs table to config.json for the render scripts."""
    cfg = load_db_config()
    try:
        Path(CONFIG_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(CONFIG_JSON_PATH).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[DB Error] Failed to sync config to JSON file: {e}")


def load_db_config() -> dict:
    conn = get_db_conn()
    cursor = conn.cursor()
    cfg = {}
    try:
        cursor.execute("SELECT key, value FROM configs")
        for r in cursor.fetchall():
            k, v = r["key"], r["value"]
            if k in ("brand_colors", "overrides"):
                try:
                    cfg[k] = json.loads(v)
                except Exception:
                    cfg[k] = {}
            elif k == "manual_override":
                cfg[k] = bool(int(v))
            else:
                cfg[k] = v

        defaults = {
            "active_company": "amc",
            "webhook_url": "",
            "disclaimer_text": "Mutual Fund investments are subject to market risks, read all scheme related documents carefully.",
            "watermark_mode": "both",
            "brand_colors": {"bg_color": "#FCF9F2", "text_color": "#1A1A1A", "yellow_brand": "#F7B500", "gray_text": "#555555"},
            "manual_override": False,
            "overrides": {"date": "", "bse_value": "", "bse_change": "", "nse_value": "", "nse_change": "", "mid_value": "", "mid_change": "", "small_value": "", "small_change": "", "fii_value": "", "dii_value": "", "brent_value": "", "gold_value": "", "silver_value": "", "usdinr_value": "", "gsec_value": "", "pe_value": "", "vix_value": "", "us10y_value": "", "dxy_value": "", "midcap_pe_value": "", "smallcap_pe_value": "", "headlines": []},
        }
        for k, val in defaults.items():
            if k not in cfg:
                cfg[k] = val
        return cfg
    except Exception as e:
        print(f"[DB Error] Failed to load config: {e}")
        return {}
    finally:
        conn.close()


def save_db_config(cfg: dict) -> None:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        upsert = _upsert_config_sql()
        for k, v in cfg.items():
            if k in ("brand_colors", "overrides"):
                val = json.dumps(v, ensure_ascii=False)
            elif k == "manual_override":
                val = "1" if v else "0"
            else:
                val = str(v)
            cursor.execute(upsert, (k, val))
        conn.commit()
    except Exception as e:
        print(f"[DB Error] Failed to save config: {e}")
    finally:
        conn.close()

    sync_db_to_json()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_mobile(mobile: str) -> tuple[bool, str]:
    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        return False, "Mobile number must be a valid 10-digit number."
    return True, ""


def validate_password(password: str) -> tuple[bool, str]:
    if not password or len(password) < PASSWORD_MIN_LEN:
        return False, f"Password must be at least {PASSWORD_MIN_LEN} characters."
    return True, ""


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
def add_user(mobile: str, password: str, role: str = "user", status: str = "active") -> tuple[bool, str]:
    ok, msg = validate_mobile(mobile)
    if not ok:
        return False, msg
    ok, msg = validate_password(password)
    if not ok:
        return False, msg

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        h = generate_password_hash(password)
        cursor.execute(
            _q("INSERT INTO users (mobile, password_hash, role, status) VALUES (?, ?, ?, ?)"),
            (mobile, h, role, status),
        )
        conn.commit()
        return True, "User created successfully."
    except Exception as e:
        conn.rollback()
        # Unique-constraint violation (mobile already exists) on either engine.
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return False, f"An account with mobile number {mobile} already exists."
        return False, str(e)
    finally:
        conn.close()


def signup_user(mobile: str, password: str) -> tuple[bool, str]:
    """Public self-registration: creates a pending account awaiting admin approval."""
    return add_user(mobile, password, role="user", status="pending")


def verify_user(mobile: str, password: str) -> dict | None:
    """Return the user dict if the password matches (regardless of status).
    The caller is responsible for checking 'status' and 'must_change_password'."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            _q("SELECT id, mobile, password_hash, role, status, must_change_password FROM users WHERE mobile = ?"),
            (mobile,),
        )
        row = cursor.fetchone()
        if row and check_password_hash(row["password_hash"], password):
            return {
                "id": row["id"],
                "mobile": row["mobile"],
                "role": row["role"],
                "status": row["status"],
                "must_change_password": bool(row["must_change_password"]),
            }
        return None
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            _q("SELECT id, mobile, role, status, must_change_password, reset_requested FROM users WHERE id = ?"),
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, mobile, role, status, must_change_password, reset_requested, created_at FROM users ORDER BY created_at DESC")
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def count_active_admins(exclude_id: int | None = None) -> int:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        if exclude_id is not None:
            cursor.execute(_q("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active' AND id <> ?"), (exclude_id,))
        else:
            cursor.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'")
        return int(cursor.fetchone()["n"])
    finally:
        conn.close()


def set_user_status(user_id: int, status: str) -> tuple[bool, str]:
    if status not in ("active", "pending", "disabled"):
        return False, "Invalid status."
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("UPDATE users SET status = ? WHERE id = ?"), (status, user_id))
        conn.commit()
        return True, "Status updated."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def set_user_role(user_id: int, role: str) -> tuple[bool, str]:
    if role not in ("user", "admin"):
        return False, "Invalid role."
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("UPDATE users SET role = ? WHERE id = ?"), (role, user_id))
        conn.commit()
        return True, "Role updated."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str, force_change: bool = False) -> tuple[bool, str]:
    """Admin-set password. force_change=True marks the user to change it on next login
    (used for the forgot-password reset flow) and clears any pending reset request."""
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        h = generate_password_hash(new_password)
        cursor.execute(
            _q("UPDATE users SET password_hash = ?, must_change_password = ?, reset_requested = 0 WHERE id = ?"),
            (h, 1 if force_change else 0, user_id),
        )
        conn.commit()
        return True, "Password updated successfully."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def change_own_password(user_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
    ok, msg = validate_password(new_password)
    if not ok:
        return False, msg
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("SELECT password_hash FROM users WHERE id = ?"), (user_id,))
        row = cursor.fetchone()
        if not row or not check_password_hash(row["password_hash"], current_password):
            return False, "Current password is incorrect."
        h = generate_password_hash(new_password)
        cursor.execute(
            _q("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?"),
            (h, user_id),
        )
        conn.commit()
        return True, "Password changed successfully."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def request_password_reset(mobile: str) -> bool:
    """Flag a reset request for the admin to action. Returns silently regardless of
    whether the mobile exists (avoid leaking which numbers are registered)."""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("UPDATE users SET reset_requested = 1 WHERE mobile = ?"), (mobile,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_user(user_id: int) -> tuple[bool, str]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("DELETE FROM users WHERE id = ?"), (user_id,))
        conn.commit()
        return True, "User deleted successfully."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------
def add_log(mobile: str, action: str):
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("INSERT INTO logs (mobile, action) VALUES (?, ?)"), (mobile, action))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


from datetime import datetime, timezone, timedelta

def list_logs(limit: int = 100) -> list[dict]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(_q("SELECT mobile, action, timestamp FROM logs ORDER BY timestamp DESC LIMIT ?"), (limit,))
        logs = []
        for r in cursor.fetchall():
            item = dict(r)
            ts_str = str(item.get("timestamp") or "")
            if ts_str:
                try:
                    dt_utc = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    dt_ist = dt_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
                    item["timestamp"] = dt_ist.strftime("%d %b %Y, %I:%M:%S %p IST")
                except Exception:
                    pass
            logs.append(item)
        return logs
    finally:
        conn.close()
