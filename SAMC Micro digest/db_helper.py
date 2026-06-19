import os
import sqlite3
import json
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "database.db"))
CONFIG_JSON_PATH = os.environ.get("CONFIG_PATH", str(BASE_DIR / "config.json"))

def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # Make sure parent directory exists
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # 1. Create users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL, -- 'admin', 'user'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # 2. Create configs table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS configs (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    
    # 3. Create logs table for monitoring
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile TEXT NOT NULL,
        action TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    
    # Seed default admin user (Mobile: 9791117131, Pass: admin)
    cursor.execute("SELECT * FROM users WHERE mobile = ?", ("9791117131",))
    if not cursor.fetchone():
        h = generate_password_hash("admin")
        cursor.execute("INSERT INTO users (mobile, password_hash, role) VALUES (?, ?, ?)", ("9791117131", h, "admin"))
        conn.commit()
        print("[DB] Seeded default admin user: 9791117131 / admin")
        
    # Seed default configurations if configs table is empty
    cursor.execute("SELECT COUNT(*) FROM configs")
    if cursor.fetchone()[0] == 0:
        # Load from config.json if exists
        config_data = {}
        if Path(CONFIG_JSON_PATH).exists():
            try:
                config_data = json.loads(Path(CONFIG_JSON_PATH).read_text(encoding="utf-8"))
            except Exception:
                pass
                
        default_config = {
            "active_company": config_data.get("active_company", "wealth"),
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
                "date": "",
                "bse_value": "",
                "bse_change": "",
                "nse_value": "",
                "nse_change": "",
                "mid_value": "",
                "mid_change": "",
                "small_value": "",
                "small_change": "",
                "fii_value": "",
                "dii_value": "",
                "brent_value": "",
                "gold_value": "",
                "silver_value": "",
                "usdinr_value": "",
                "gsec_value": "",
                "pe_value": "",
                "vix_value": "",
                "us10y_value": "",
                "dxy_value": "",
                "midcap_pe_value": "",
                "smallcap_pe_value": "",
                "headlines": [],
            }))
        }
        for k, v in default_config.items():
            cursor.execute("INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)", (k, str(v)))
        conn.commit()
        print("[DB] Seeded default configurations in SQLite table.")
    conn.close()
    
    # Sync config to json just in case
    sync_db_to_json()


def sync_db_to_json():
    """Sync the configs table contents to config.json for backward compatibility with render scripts."""
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
        rows = cursor.fetchall()
        for r in rows:
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
        
        # Ensure mandatory keys are populated with defaults
        defaults = {
            "active_company": "wealth",
            "webhook_url": "",
            "disclaimer_text": "Mutual Fund investments are subject to market risks, read all scheme related documents carefully.",
            "watermark_mode": "both",
            "brand_colors": {"bg_color": "#FCF9F2", "text_color": "#1A1A1A", "yellow_brand": "#F7B500", "gray_text": "#555555"},
            "manual_override": False,
            "overrides": {"date": "", "bse_value": "", "bse_change": "", "nse_value": "", "nse_change": "", "mid_value": "", "mid_change": "", "small_value": "", "small_change": "", "fii_value": "", "dii_value": "", "brent_value": "", "gold_value": "", "silver_value": "", "usdinr_value": "", "gsec_value": "", "pe_value": "", "vix_value": "", "us10y_value": "", "dxy_value": "", "midcap_pe_value": "", "smallcap_pe_value": "", "headlines": []}
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
        for k, v in cfg.items():
            if k in ("brand_colors", "overrides"):
                val = json.dumps(v, ensure_ascii=False)
            elif k == "manual_override":
                val = "1" if v else "0"
            else:
                val = str(v)
            cursor.execute("INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)", (k, val))
        conn.commit()
    except Exception as e:
        print(f"[DB Error] Failed to save config: {e}")
    finally:
        conn.close()
        
    # Sync config to json
    sync_db_to_json()


# ---------------------------------------------------------------------------
# User Management Helpers
# ---------------------------------------------------------------------------

def add_user(mobile: str, password: str, role: str = "user") -> tuple[bool, str]:
    if not mobile or not password:
        return False, "Mobile and password are required."
    if len(mobile) != 10 or not mobile.isdigit():
        return False, "Mobile number must be a valid 10-digit number."
        
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        h = generate_password_hash(password)
        cursor.execute("INSERT INTO users (mobile, password_hash, role) VALUES (?, ?, ?)", (mobile, h, role))
        conn.commit()
        return True, "User created successfully."
    except sqlite3.IntegrityError:
        return False, f"User with mobile number {mobile} already exists."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def verify_user(mobile: str, password: str) -> dict | None:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, mobile, password_hash, role FROM users WHERE mobile = ?", (mobile,))
        row = cursor.fetchone()
        if row and check_password_hash(row["password_hash"], password):
            return {"id": row["id"], "mobile": row["mobile"], "role": row["role"]}
        return None
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, mobile, role, created_at FROM users ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str) -> tuple[bool, str]:
    if not new_password:
        return False, "Password cannot be empty."
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        h = generate_password_hash(new_password)
        cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (h, user_id))
        conn.commit()
        return True, "Password updated successfully."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def delete_user(user_id: int) -> tuple[bool, str]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True, "User deleted successfully."
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Monitoring / Activity Log Helpers
# ---------------------------------------------------------------------------

def add_log(mobile: str, action: str):
    import datetime
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO logs (mobile, action) VALUES (?, ?)", (mobile, action))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

    try:
        log_dir = BASE_DIR / "output"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "activity_logs.txt"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] Mobile: {mobile} | Action: {action}\n")
    except Exception as e:
        print(f"[DB Error] Failed to write to activity_logs.txt: {e}")


def list_logs(limit: int = 100) -> list[dict]:
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT mobile, action, timestamp FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
