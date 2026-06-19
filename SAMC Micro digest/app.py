"""SAMC Micro Digest — premium web app.

Run:
    python app.py
Then open http://127.0.0.1:5000/
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, abort, session, redirect, url_for, render_template
import db_helper

from samc_micro_digest.fetch import fetch_all
from samc_micro_digest.render import render_report
from samc_micro_digest.publish import publish_to_teams


BASE = Path(__file__).parent
OUTPUT = BASE / "output"
OUTPUTS = BASE / "outputs"
STATIC = BASE / "static"
OUTPUT.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)
META_FILE = OUTPUT / "meta.json"
CONFIG_FILE = BASE / "config.json"
VERSIONS_DIR = OUTPUT / "versions"
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "active_company": "wealth",
    "webhook_url": "",
    "disclaimer_text": "Mutual Fund investments are subject to market risks, read all scheme related documents carefully.",
    "watermark_mode": "both",
    "brand_colors": {
        "bg_color": "#FCF9F2",
        "text_color": "#1A1A1A",
        "yellow_brand": "#F7B500",
        "gray_text": "#555555",
    },
    "manual_override": False,
    "overrides": {
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
    },
}


def _find_chrome() -> str | None:
    candidates = [
        os.environ.get("SAMC_MICRO_DIGEST_CHROME"),
        os.environ.get("MARKET_DIGEST_CHROME"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


CHROME_PATH = _find_chrome()
app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.secret_key = "shriram_marketplace_secret_key"
db_helper.init_db()
_gen_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    return db_helper.load_db_config()


def _save_config(cfg: dict) -> None:
    db_helper.save_db_config(cfg)


def _coverage(data: dict) -> dict:
    mc = data.get("mobile_card") or {}
    quotes = mc.get("quotes") or {}
    gsec = mc.get("gsec") or {}
    pe = mc.get("pe") or {}
    return {
        "bse": bool(quotes.get("bse", {}).get("value")),
        "nse": bool(quotes.get("nse", {}).get("value")),
        "mid": bool(quotes.get("mid", {}).get("value")),
        "small": bool(quotes.get("small", {}).get("value")),
        "fii_dii": mc.get("fii") is not None and mc.get("dii") is not None,
        "gsec": bool(gsec.get("value") if gsec.get("available") else False),
        "pe": bool(pe.get("value") if pe.get("available") else False),
        "vix": bool(quotes.get("vix", {}).get("value")),
        "us10y": bool(quotes.get("us10y", {}).get("value")),
        "dxy": bool(quotes.get("dxy", {}).get("value")),
        "news": bool(mc.get("headlines")),
    }


def _save_meta(data: dict) -> None:
    meta = {
        "generated_at": (
            data["generated_at"].isoformat()
            if isinstance(data["generated_at"], datetime)
            else data["generated_at"]
        ),
        "coverage": _coverage(data),
    }
    META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _load_meta() -> dict | None:
    if not META_FILE.exists():
        return None
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _apply_overrides(data: dict, cfg: dict) -> dict:
    """Apply manual overrides from config onto the fetched data dict."""
    if not cfg.get("manual_override"):
        return data
    ov = cfg.get("overrides", {})
    mc = data["mobile_card"]
    q = mc["quotes"]

    def _float(k: str):
        v = ov.get(k, "")
        try:
            return float(v) if v != "" else None
        except Exception:
            return None

    if ov.get("date"):
        mc["date"] = ov["date"]
    for idx in ("bse", "nse", "mid", "small"):
        val = _float(f"{idx}_value")
        chg = _float(f"{idx}_change")
        if val is not None:
            q[idx]["value"] = val
        if chg is not None:
            q[idx]["change"] = chg
            prev = q[idx]["value"] or 1
            q[idx]["change_pct"] = (chg / (prev - chg)) * 100 if (prev - chg) else 0
    for k in ("fii", "dii"):
        v = _float(f"{k}_value")
        if v is not None:
            mc[k] = v
    for k in ("brent", "gold", "silver", "usdinr", "vix", "us10y", "dxy"):
        v = _float(f"{k}_value")
        if v is not None:
            q[k]["value"] = v
    gsec_v = _float("gsec_value")
    if gsec_v is not None:
        mc["gsec"] = {"available": True, "value": gsec_v, "change_pct": None}
    pe_v = _float("pe_value")
    if pe_v is not None:
        mc["pe"] = {"available": True, "value": pe_v}
    midcap_pe_v = _float("midcap_pe_value")
    if midcap_pe_v is not None:
        mc["midcap_pe"] = {"available": True, "value": midcap_pe_v}
    smallcap_pe_v = _float("smallcap_pe_value")
    if smallcap_pe_v is not None:
        mc["smallcap_pe"] = {"available": True, "value": smallcap_pe_v}
    headlines = ov.get("headlines")
    if headlines:
        mc["headlines"] = headlines if isinstance(headlines, list) else [headlines]
    return data


def _generate_html() -> dict:
    t0 = time.time()
    data = fetch_all()
    cfg = _load_config()
    data = _apply_overrides(data, cfg)
    duration = time.time() - t0
    render_report(data, OUTPUT)
    _save_meta(data)
    # Invalidate stale PNG
    for folder in (OUTPUT, OUTPUTS):
        img = folder / "card_daily.png"
        if img.exists():
            try:
                img.unlink()
            except Exception:
                pass
    meta = _load_meta()
    return {
        "generated_at": meta["generated_at"] if meta else None,
        "duration_s": round(duration, 1),
        "coverage": meta["coverage"] if meta else {},
    }


def _render_png(card_type: str) -> Path:
    html_file = OUTPUT / f"card_{card_type}.html"
    png_file = OUTPUT / f"card_{card_type}.png"
    if not html_file.exists():
        raise FileNotFoundError(f"Card {card_type} HTML not generated yet.")
    if not CHROME_PATH:
        raise RuntimeError("Chrome/Edge not found.")
    if png_file.exists() and png_file.stat().st_mtime >= html_file.stat().st_mtime:
        return png_file
    url = html_file.resolve().as_uri()
    cmd = [
        CHROME_PATH,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-device-scale-factor=2",
        "--window-size=1024,1850",
        f"--screenshot={png_file}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0 or not png_file.exists():
        raise RuntimeError(f"Chrome PNG generation failed (code {result.returncode}).")
    
    # Auto-crop bottom empty space
    try:
        from PIL import Image
        img = Image.open(png_file)
        w, h = img.size
        bg = img.getpixel((0, h - 1))
        last_y = h - 1
        found = False
        for y in range(h - 1, 0, -1):
            for x in range(w):
                if img.getpixel((x, y))[:3] != bg[:3]:
                    last_y = y
                    found = True
                    break
            if found:
                break
        
        # Add 20px padding at the bottom
        target_h = min(h, last_y + 20)
        if target_h < h:
            cropped = img.crop((0, 0, w, target_h))
            cropped.save(png_file)
            print(f"[Render] Cropped card daily screenshot from {h}px to {target_h}px")
    except Exception as e:
        print(f"[Render] PIL Crop Exception: {e}")
    try:
        shutil.copy2(png_file, OUTPUTS / f"card_{card_type}.png")
        shutil.copy2(html_file, OUTPUTS / f"card_{card_type}.html")
    except Exception:
        pass
    desktop = Path(r"C:\Users\K964\OneDrive - Shriram Finance Limited\Desktop")
    if not desktop.exists():
        desktop = Path(r"C:\Users\K964\Desktop")
    if desktop.exists():
        try:
            shutil.copy2(png_file, desktop / f"card_{card_type}.png")
        except Exception:
            pass
    return png_file


# ---------------------------------------------------------------------------
# Authentication & Session Hook
# ---------------------------------------------------------------------------
@app.before_request
def check_auth():
    # Public routes that do not require login
    if request.path == "/login" or request.path.startswith("/static/") or request.path == "/logo.jpeg":
        return
        
    # Check if user is logged in
    if not session.get("user_id"):
        # If API request, return 401
        if request.path.startswith("/api/") or "/api/" in request.path:
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        user = db_helper.verify_user(mobile, password)
        if user:
            session["user_id"] = user["id"]
            session["mobile"] = user["mobile"]
            session["role"] = user["role"]
            db_helper.add_log(user["mobile"], "Logged in standalone sub-app")
            return redirect(url_for("index"))
        else:
            db_helper.add_log(mobile or "unknown", "Failed login standalone sub-app")
            return render_template("login.html", error="Invalid mobile number or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    mobile = session.get("mobile", "unknown")
    db_helper.add_log(mobile, "Logged out standalone sub-app")
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes — web UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    index_file = STATIC / "index.html"
    if index_file.exists():
        return send_file(index_file, mimetype="text/html")
    return "<h1>SAMC Micro Digest</h1><p>Static files not found.</p>", 500


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    meta = _load_meta()
    has_daily = (OUTPUT / "card_daily.html").exists()
    chrome_ok = bool(CHROME_PATH)
    return jsonify({
        "has_report": bool(meta and has_daily),
        "has_daily": has_daily,
        "chrome_available": chrome_ok,
        "chrome_path": CHROME_PATH,
        **(meta or {}),
    })


@app.route("/api/generate", methods=["POST"])
def api_generate():
    if not _gen_lock.acquire(blocking=False):
        return jsonify({"error": "Another generation is in progress."}), 409
    try:
        info = _generate_html()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _gen_lock.release()


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_load_config())


@app.route("/api/config", methods=["POST"])
def api_config_save():
    try:
        body = request.get_json(force=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid JSON body."}), 400
        # Merge with defaults
        cfg = _load_config()
        # Top-level keys
        for k in ("webhook_url", "disclaimer_text", "watermark_mode", "manual_override", "active_company"):
            if k in body:
                cfg[k] = body[k]
        if "brand_colors" in body and isinstance(body["brand_colors"], dict):
            cfg.setdefault("brand_colors", {}).update(body["brand_colors"])
        if "overrides" in body and isinstance(body["overrides"], dict):
            cfg.setdefault("overrides", {}).update(body["overrides"])
        _save_config(cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/publish", methods=["POST"])
def api_publish():
    cfg = _load_config()
    webhook_url = cfg.get("webhook_url", "")
    body = request.get_json(force=True, silent=True) or {}
    if body.get("webhook_url"):
        webhook_url = body["webhook_url"]
    if not webhook_url:
        return jsonify({"error": "Webhook URL is not configured."}), 400
    ok, msg = publish_to_teams(webhook_url, OUTPUT)
    if ok:
        return jsonify({"ok": True, "message": msg})
    return jsonify({"ok": False, "error": msg}), 500


@app.route("/api/save-card", methods=["POST"])
def api_save_card():
    try:
        body = request.get_json(force=True)
        if not isinstance(body, dict) or "html" not in body:
            return jsonify({"error": "Invalid payload. 'html' is required."}), 400
        
        html_content = body["html"]
        html_file = OUTPUT / "card_daily.html"
        
        # Save HTML
        html_file.write_text(html_content, encoding="utf-8")
        
        # Also save overrides if present to retain them across generation
        overrides = body.get("layout_overrides")
        print(f"DEBUG: api_save_card overrides = {overrides}")
        if overrides is not None:
            try:
                overrides_file = OUTPUT / "layout_overrides.json"
                print(f"DEBUG: writing overrides to {overrides_file}")
                overrides_file.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
                print("DEBUG: overrides successfully written!")
            except Exception as e:
                print(f"DEBUG: Exception writing overrides: {e}")
        
        # Invalidate cached PNGs
        for folder in (OUTPUT, OUTPUTS):
            img = folder / "card_daily.png"
            if img.exists():
                try:
                    img.unlink()
                except Exception:
                    pass
        
        mobile = session.get("mobile", "unknown")
        db_helper.add_log(mobile, "Saved custom layout for Daily card")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset-card", methods=["POST"])
def api_reset_card():
    try:
        mobile = session.get("mobile", "unknown")
        db_helper.add_log(mobile, "Reset custom layout for Daily card")
        
        # Delete layout overrides JSON
        overrides_file = OUTPUT / "layout_overrides.json"
        if overrides_file.exists():
            try:
                overrides_file.unlink()
            except Exception:
                pass
                
        _generate_html()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/version/save", methods=["POST"])
def api_version_save():
    try:
        body = request.get_json(force=True, silent=True) or {}
        version_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = body.get("name") or f"Version {version_id}"
        ver_dir = VERSIONS_DIR / version_id
        ver_dir.mkdir(parents=True, exist_ok=True)

        # Copy current files into the version snapshot
        for fname in ("layout_overrides.json", "card_daily.html", "card_daily.png"):
            src = OUTPUT / fname
            if src.exists():
                shutil.copy2(src, ver_dir / fname)

        # Save metadata
        created_at = datetime.now().isoformat()
        meta = {
            "id": version_id,
            "name": name,
            "created_at": created_at,
            "user": session.get("mobile", "unknown"),
        }
        (ver_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        mobile = session.get("mobile", "unknown")
        db_helper.add_log(mobile, f"Saved version {version_id} ({name})")
        return jsonify({"ok": True, "version": {"id": version_id, "name": name, "created_at": created_at}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/versions", methods=["GET"])
def api_versions_list():
    try:
        versions = []
        if VERSIONS_DIR.exists():
            for d in VERSIONS_DIR.iterdir():
                meta_file = d / "meta.json"
                if d.is_dir() and meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        meta["has_png"] = (d / "card_daily.png").exists()
                        versions.append(meta)
                    except Exception:
                        pass
        versions.sort(key=lambda v: v.get("created_at", ""), reverse=True)
        return jsonify({"versions": versions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/version/restore/<version_id>", methods=["POST"])
def api_version_restore(version_id: str):
    try:
        ver_dir = VERSIONS_DIR / version_id
        if not ver_dir.exists() or not ver_dir.is_dir():
            return jsonify({"error": "Version not found."}), 404

        # Restore layout_overrides.json
        src_overrides = ver_dir / "layout_overrides.json"
        dst_overrides = OUTPUT / "layout_overrides.json"
        if src_overrides.exists():
            shutil.copy2(src_overrides, dst_overrides)
        elif dst_overrides.exists():
            dst_overrides.unlink()

        # Restore card_daily.html
        src_html = ver_dir / "card_daily.html"
        if src_html.exists():
            shutil.copy2(src_html, OUTPUT / "card_daily.html")

        # Invalidate cached PNGs
        for folder in (OUTPUT, OUTPUTS):
            img = folder / "card_daily.png"
            if img.exists():
                try:
                    img.unlink()
                except Exception:
                    pass

        mobile = session.get("mobile", "unknown")
        db_helper.add_log(mobile, f"Restored version {version_id}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/version/<version_id>", methods=["DELETE"])
def api_version_delete(version_id: str):
    try:
        ver_dir = VERSIONS_DIR / version_id
        if not ver_dir.exists() or not ver_dir.is_dir():
            return jsonify({"error": "Version not found."}), 404

        shutil.rmtree(ver_dir)

        mobile = session.get("mobile", "unknown")
        db_helper.add_log(mobile, f"Deleted version {version_id}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/render-png/<card_type>", methods=["POST"])
def api_render_png(card_type: str):
    if card_type != "daily":
        return jsonify({"error": "Invalid card type."}), 400
    try:
        _render_png(card_type)
        return jsonify({"ok": True, "card": card_type})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — card assets
# ---------------------------------------------------------------------------

@app.route("/card/daily")
def card_daily():
    html_file = OUTPUT / "card_daily.html"
    if not html_file.exists():
        abort(404, "Daily card not generated yet.")
    return send_file(html_file, mimetype="text/html")


@app.route("/card/daily.png")
def card_daily_png():
    try:
        png = _render_png("daily")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(png, mimetype="image/png")


def get_company_logo(company_id: str) -> tuple[bytes, str] | None:
    logo_path = BASE / "logos" / f"{company_id}.jpeg"
    if logo_path.exists():
        content = logo_path.read_bytes()
        is_svg = False
        try:
            header = content[:200].decode("utf-8", errors="ignore")
            if "<svg" in header.lower():
                is_svg = True
        except Exception:
            pass
        mime = "image/svg+xml" if is_svg else "image/jpeg"
        return content, mime
    return None


@app.route("/logo.jpeg")
def logo():
    company_id = request.args.get("c")
    if not company_id or company_id not in ("wealth", "amc", "insights", "financial"):
        cfg = _load_config()
        company_id = cfg.get("active_company", "wealth")
    logo_info = get_company_logo(company_id)
    if logo_info:
        content, mime = logo_info
        return Response(content, mimetype=mime)
    return abort(404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("SAMC_PORT", 8050))
    print("SAMC Micro Digest web app starting …")
    print(f"  Chrome:  {CHROME_PATH or '(not found)'}")
    print(f"  Output:  {OUTPUT}")
    print(f"  Open:    http://127.0.0.1:{port}/")
    app.run(host="127.0.0.1", port=port, debug=False)
