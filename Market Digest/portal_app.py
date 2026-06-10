"""Consolidated Marketplace Tools Portal — hosts all tools under a single port.

Run:
    python portal_app.py
Then open http://127.0.0.1:8000/
"""
from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, Blueprint, jsonify, request, send_file, render_template, send_from_directory, abort, Response, session, redirect, url_for
import db_helper

# Set up paths
BASE_PORTAL = Path(__file__).parent
PARENT_DIR = BASE_PORTAL.parent
SAMC_BASE = PARENT_DIR / "SAMC Micro digest"
if not SAMC_BASE.exists():
    SAMC_BASE = BASE_PORTAL
TEMPLATES = BASE_PORTAL / "templates"
STATIC = BASE_PORTAL / "static"

app = Flask(__name__, template_folder=str(TEMPLATES))
app.secret_key = os.environ.get("SECRET_KEY", "shriram_marketplace_secret_key")

# Initialize database
db_helper.init_db()


# ---------------------------------------------------------------------------
# Chrome Browser Discovery Helper
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Import yfinance digest scripts
# ---------------------------------------------------------------------------
import sys
sys.path.append(str(BASE_PORTAL))
sys.path.append(str(SAMC_BASE))

import market_digest.fetch as md_fetch
import market_digest.render as md_render

import samc_micro_digest.fetch as samc_fetch
import samc_micro_digest.render as samc_render
import samc_micro_digest.publish as samc_publish


# ---------------------------------------------------------------------------
# Blueprint 1: SAMC Micro Digest (WhatsApp Cards)
# ---------------------------------------------------------------------------
samc_bp = Blueprint("samc", __name__)

SAMC_OUTPUT = SAMC_BASE / "output"
SAMC_OUTPUTS = SAMC_BASE / "outputs"
SAMC_META_FILE = SAMC_OUTPUT / "meta.json"
SAMC_LAST_DATA = SAMC_OUTPUT / "last_data.json"
SAMC_CONFIG_FILE = SAMC_BASE / "config.json"
SAMC_STATIC = SAMC_BASE / "static"

SAMC_DEFAULT_CONFIG = {
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
        "headlines": [],
    },
}

samc_gen_lock = threading.Lock()


def _samc_rerender_cards() -> bool:
    """Re-render the SAMC cards from the last generated dataset so a logo or
    branding change is reflected immediately, without re-fetching market data.
    Returns True if cards were re-rendered."""
    if not SAMC_LAST_DATA.exists():
        return False
    try:
        data = json.loads(SAMC_LAST_DATA.read_text(encoding="utf-8"))
    except Exception:
        return False
    # render_report reads config.json directly; make sure it mirrors the DB
    # (active_company, brand colours, disclaimer, watermark).
    try:
        db_helper.sync_db_to_json()
    except Exception:
        pass
    samc_render.render_report(data, SAMC_OUTPUT)
    # Invalidate cached PNGs so they re-render from the refreshed HTML.
    for card in ("indices", "news"):
        for folder in (SAMC_OUTPUT, SAMC_OUTPUTS):
            img = folder / f"card_{card}.png"
            if img.exists():
                try:
                    img.unlink()
                except Exception:
                    pass
    return True


def _samc_load_config() -> dict:
    return db_helper.load_db_config()


def _samc_save_config(cfg: dict) -> None:
    db_helper.save_db_config(cfg)


def _samc_coverage(data: dict) -> dict:
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
        "news": bool(mc.get("headlines")),
    }


def _samc_save_meta(data: dict) -> None:
    meta = {
        "generated_at": (
            data["generated_at"].isoformat()
            if isinstance(data["generated_at"], datetime)
            else data["generated_at"]
        ),
        "coverage": _samc_coverage(data),
    }
    SAMC_META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _samc_load_meta() -> dict | None:
    if not SAMC_META_FILE.exists():
        return None
    try:
        return json.loads(SAMC_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _samc_apply_overrides(data: dict, cfg: dict) -> dict:
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
    for k in ("brent", "gold", "silver", "usdinr"):
        v = _float(f"{k}_value")
        if v is not None:
            q[k]["value"] = v
    gsec_v = _float("gsec_value")
    if gsec_v is not None:
        mc["gsec"] = {"available": True, "value": gsec_v, "change_pct": None}
    pe_v = _float("pe_value")
    if pe_v is not None:
        mc["pe"] = {"available": True, "value": pe_v}
    headlines = ov.get("headlines")
    if headlines:
        mc["headlines"] = headlines if isinstance(headlines, list) else [headlines]
    return data


def _samc_generate_html() -> dict:
    t0 = time.time()
    data = samc_fetch.fetch_all()
    cfg = _samc_load_config()
    data = _samc_apply_overrides(data, cfg)
    duration = time.time() - t0
    samc_render.render_report(data, SAMC_OUTPUT)
    _samc_save_meta(data)
    # Persist the dataset so cards can be re-rendered (e.g. after a logo/branding
    # change) without re-fetching live market data.
    try:
        SAMC_LAST_DATA.write_text(json.dumps(data, default=str, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[SAMC] Could not persist last dataset: {e}")
    for card in ("indices", "news"):
        for folder in (SAMC_OUTPUT, SAMC_OUTPUTS):
            img = folder / f"card_{card}.png"
            if img.exists():
                try:
                    img.unlink()
                except Exception:
                    pass
    meta = _samc_load_meta()
    return {
        "generated_at": meta["generated_at"] if meta else None,
        "duration_s": round(duration, 1),
        "coverage": meta["coverage"] if meta else {},
    }


def _samc_render_png(card_type: str) -> Path:
    html_file = SAMC_OUTPUT / f"card_{card_type}.html"
    png_file = SAMC_OUTPUT / f"card_{card_type}.png"
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
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--window-size=540,1200",
        f"--screenshot={png_file}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0 or not png_file.exists():
        raise RuntimeError(f"Chrome PNG generation failed (code {result.returncode}).")
    try:
        shutil.copy2(png_file, SAMC_OUTPUTS / f"card_{card_type}.png")
        shutil.copy2(html_file, SAMC_OUTPUTS / f"card_{card_type}.html")
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


@samc_bp.route("/")
def samc_index():
    return send_file(SAMC_STATIC / "index.html", mimetype="text/html")


@samc_bp.route("/static/<path:filename>")
def samc_static_files(filename):
    return send_from_directory(str(SAMC_STATIC), filename)


@samc_bp.route("/api/status")
def samc_api_status():
    meta = _samc_load_meta()
    has_indices = (SAMC_OUTPUT / "card_indices.html").exists()
    has_news = (SAMC_OUTPUT / "card_news.html").exists()
    chrome_ok = bool(CHROME_PATH)
    return jsonify({
        "has_report": bool(meta and has_indices),
        "has_indices": has_indices,
        "has_news": has_news,
        "chrome_available": chrome_ok,
        "chrome_path": CHROME_PATH,
        **(meta or {}),
    })


@samc_bp.route("/api/generate", methods=["POST"])
def samc_api_generate():
    if not samc_gen_lock.acquire(blocking=False):
        return jsonify({"error": "Another generation is in progress."}), 409
    try:
        info = _samc_generate_html()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        samc_gen_lock.release()


@samc_bp.route("/api/config", methods=["GET"])
def samc_api_config_get():
    return jsonify(_samc_load_config())


@samc_bp.route("/api/config", methods=["POST"])
def samc_api_config_save():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        body = request.get_json(force=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid JSON body."}), 400
        cfg = _samc_load_config()
        for k in ("webhook_url", "disclaimer_text", "watermark_mode", "manual_override", "active_company"):
            if k in body:
                cfg[k] = body[k]
        if "brand_colors" in body and isinstance(body["brand_colors"], dict):
            cfg.setdefault("brand_colors", {}).update(body["brand_colors"])
        if "overrides" in body and isinstance(body["overrides"], dict):
            cfg.setdefault("overrides", {}).update(body["overrides"])
        _samc_save_config(cfg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@samc_bp.route("/api/publish", methods=["POST"])
def samc_api_publish():
    cfg = _samc_load_config()
    webhook_url = cfg.get("webhook_url", "")
    body = request.get_json(force=True, silent=True) or {}
    if body.get("webhook_url"):
        webhook_url = body["webhook_url"]
    if not webhook_url:
        return jsonify({"error": "Webhook URL is not configured."}), 400
    ok, msg = samc_publish.publish_to_teams(webhook_url, SAMC_OUTPUT)
    if ok:
        return jsonify({"ok": True, "message": msg})
    return jsonify({"ok": False, "error": msg}), 500


@samc_bp.route("/api/render-png/<card_type>", methods=["POST"])
def samc_api_render_png(card_type: str):
    if card_type not in ("indices", "news"):
        return jsonify({"error": "Invalid card type."}), 400
    try:
        _samc_render_png(card_type)
        return jsonify({"ok": True, "card": card_type})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@samc_bp.route("/card/indices")
def samc_card_indices():
    html_file = SAMC_OUTPUT / "card_indices.html"
    if not html_file.exists():
        abort(404, "Indices card not generated yet.")
    return send_file(html_file, mimetype="text/html")


@samc_bp.route("/card/news")
def samc_card_news():
    html_file = SAMC_OUTPUT / "card_news.html"
    if not html_file.exists():
        abort(404, "News card not generated yet.")
    return send_file(html_file, mimetype="text/html")


@samc_bp.route("/card/indices.png")
def samc_card_indices_png():
    try:
        png = _samc_render_png("indices")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(png, mimetype="image/png")


@samc_bp.route("/card/news.png")
def samc_card_news_png():
    try:
        png = _samc_render_png("news")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(png, mimetype="image/png")


# ---------------------------------------------------------------------------
# Blueprint 2: Market Digest (PDF Generator)
# ---------------------------------------------------------------------------
market_bp = Blueprint("market", __name__)

MD_BASE = BASE_PORTAL
MD_OUTPUT = MD_BASE / "output"
MD_REPORT_HTML = MD_OUTPUT / "report.html"
MD_REPORT_PDF = MD_OUTPUT / "report.pdf"
MD_META_FILE = MD_OUTPUT / "meta.json"

md_gen_lock = threading.Lock()


def _md_coverage(data: dict) -> dict:
    out = {}
    for key in ("nifty", "mmi", "constituents", "news", "fii_dii",
                "vix", "gift_nifty", "gold", "silver"):
        section = data.get(key) or {}
        out[key] = bool(section.get("available"))
    out["global"] = sum(1 for g in data["global"] if g["value"] is not None)
    out["currencies"] = sum(1 for c in data["currencies"] if c["value"] is not None)
    if data.get("sentiment"):
        out["sentiment_score"] = data["sentiment"]["score"]
        out["sentiment_label"] = data["sentiment"]["label"]
    return out


def _md_save_meta(data: dict) -> None:
    meta = {
        "generated_at": data["generated_at"].isoformat(),
        "coverage": _md_coverage(data),
    }
    MD_META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _md_load_meta() -> dict | None:
    if not MD_META_FILE.exists():
        return None
    return json.loads(MD_META_FILE.read_text(encoding="utf-8"))


def _md_generate_html() -> dict:
    t0 = time.time()
    data = md_fetch.fetch_all()
    duration = time.time() - t0
    md_render.render_report(data, MD_REPORT_HTML)
    _md_save_meta(data)
    if MD_REPORT_PDF.exists():
        MD_REPORT_PDF.unlink()
    return {
        "generated_at": data["generated_at"].isoformat(),
        "duration_s": round(duration, 1),
        "coverage": _md_coverage(data),
    }


def _md_render_pdf() -> Path:
    if not MD_REPORT_HTML.exists():
        raise FileNotFoundError("No report has been generated yet.")
    if not CHROME_PATH:
        raise RuntimeError("Chrome/Edge not found.")
    if MD_REPORT_PDF.exists():
        if MD_REPORT_PDF.stat().st_mtime >= MD_REPORT_HTML.stat().st_mtime:
            return MD_REPORT_PDF

    url = MD_REPORT_HTML.resolve().as_uri()
    cmd = [
        CHROME_PATH,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-pdf-header-footer",
        "--hide-scrollbars",
        "--virtual-time-budget=20000",
        "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={MD_REPORT_PDF}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0 or not MD_REPORT_PDF.exists():
        raise RuntimeError(
            f"Chrome PDF generation failed (code {result.returncode}). "
            f"stderr: {result.stderr.decode('utf-8', errors='ignore')[:400]}"
        )
    return MD_REPORT_PDF


def _md_render_png(card_type: str) -> Path:
    html_file = MD_OUTPUT / f"card_{card_type}.html"
    png_file = MD_OUTPUT / f"card_{card_type}.png"
    if not html_file.exists():
        raise FileNotFoundError(f"Card {card_type} HTML not generated yet.")
    if not CHROME_PATH:
        raise RuntimeError("Chrome/Edge not found.")
    if png_file.exists():
        if png_file.stat().st_mtime >= html_file.stat().st_mtime:
            return png_file

    url = html_file.resolve().as_uri()
    cmd = [
        CHROME_PATH,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--window-size=540,1200",
        f"--screenshot={png_file}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0 or not png_file.exists():
        raise RuntimeError(
            f"Chrome PNG generation failed (code {result.returncode}). "
            f"stderr: {result.stderr.decode('utf-8', errors='ignore')[:400]}"
        )
    return png_file


MD_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Market Digest — Generator</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
:root {
  --bg: #FBFBFD; --surface: #FFFFFF; --surface-2: #F5F5F7;
  --text: #1D1D1F; --text-2: #6E6E73; --text-3: #86868B;
  --hair: rgba(0,0,0,0.07); --shadow-sm: 0 1px 2px rgba(0,0,0,0.04), 0 4px 24px rgba(0,0,0,0.05);
  --up: #00875A; --up-soft: #E6F4EE; --down: #D70015; --down-soft: #FBE9EA;
  --accent: #0071E3;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.5; letter-spacing: -0.005em;
  -webkit-font-smoothing: antialiased; }
.wrap { max-width: 720px; margin: 0 auto; padding: 64px 24px; }
.brand { display: flex; align-items: center; gap: 12px; font-weight: 600;
  font-size: 17px; margin-bottom: 48px; }
.brand-mark { width: 26px; height: 26px; border-radius: 7px;
  background: linear-gradient(180deg, #1D1D1F, #3A3A3D);
  display: grid; place-items: center; }
.brand-mark::after { content: ""; width: 10px; height: 10px; border-radius: 2px;
  background: linear-gradient(135deg, #34C759 50%, #FF3B30 50%); }
h1 { font-size: 38px; font-weight: 600; letter-spacing: -0.025em;
  line-height: 1.1; margin: 0 0 8px; }
.sub { color: var(--text-2); font-size: 16px; margin: 0 0 32px; max-width: 52ch; }
.card { background: var(--surface); border: 1px solid var(--hair);
  border-radius: 16px; padding: 28px; box-shadow: var(--shadow-sm); margin-bottom: 18px; }
.row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button, .btn { appearance: none; border: 1px solid var(--hair);
  background: var(--surface); color: var(--text);
  font: inherit; font-weight: 500; padding: 10px 18px;
  border-radius: 999px; cursor: pointer; transition: all .15s ease;
  text-decoration: none; display: inline-flex; align-items: center; gap: 8px; }
button:hover, .btn:hover { background: var(--surface-2); }
button.primary { background: var(--text); color: #fff; border-color: var(--text); }
button.primary:hover { background: #000; }
button:disabled { opacity: .5; cursor: wait; }
.status { margin-top: 16px; font-size: 13px; color: var(--text-2); }
.status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-3); margin-right: 6px; vertical-align: middle; }
.status .dot.ok { background: var(--up); box-shadow: 0 0 0 3px rgba(0,135,90,0.15); }
.status .dot.err { background: var(--down); }
.status .dot.busy { background: var(--accent); animation: pulse 1.2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }

.coverage { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px 14px; margin-top: 14px; font-size: 12px; }
.coverage span { display: inline-flex; align-items: center; gap: 6px; color: var(--text-2); }
.coverage span::before { content:""; width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-3); }
.coverage span.ok::before { background: var(--up); }
.coverage span.miss::before { background: var(--down); }
.empty { color: var(--text-3); font-size: 13px; padding: 16px 0; }
.spinner { width: 14px; height: 14px; border: 2px solid currentColor;
  border-right-color: transparent; border-radius: 50%; display: inline-block;
  animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.hint { font-size: 12px; color: var(--text-3); margin-top: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><span class="brand-mark"></span> Market Digest</div>

  <h1>Daily report generator</h1>
  <p class="sub">Pull the latest market data and render a printable report.
     One section per page in the PDF.</p>

  <div class="card">
    <div class="row">
      <button id="gen" class="primary">Generate today's report</button>
      <a id="view" class="btn" href="report" target="_blank" style="display:none;">View HTML →</a>
      <a id="dl" class="btn" href="report.pdf" style="display:none;">Download PDF ↓</a>
    </div>
    <div id="status" class="status">
      <span class="dot"></span>
      <span id="status-text">Loading status …</span>
    </div>
    <div id="cov-wrap" style="display:none;">
      <div class="hint" style="margin-top: 18px; font-weight: 600; color: var(--text-2);">Coverage</div>
      <div id="coverage" class="coverage"></div>
    </div>
  </div>

  <div class="hint">PDF rendering is server-side via headless Chrome. First-time PDF
     generation takes a few seconds.</div>
</div>

<script>
const $ = id => document.getElementById(id);
const SECTIONS = [
  ['nifty','NIFTY 50'], ['mmi','MMI'], ['constituents','Constituents'],
  ['news','News'], ['fii_dii','FII/DII'], ['vix','India VIX'],
  ['gift_nifty','GIFT Nifty'], ['gold','Gold'], ['silver','Silver'],
];

function setStatus(state, text) {
  $('status').innerHTML = `<span class="dot ${state}"></span><span id="status-text">${text}</span>`;
}

function renderCoverage(c) {
  if (!c) { $('cov-wrap').style.display = 'none'; return; }
  const parts = SECTIONS.map(([k,label]) => {
    const ok = c[k];
    return `<span class="${ok ? 'ok' : 'miss'}">${label}</span>`;
  });
  parts.push(`<span class="${c.global === 5 ? 'ok' : 'miss'}">Global (${c.global}/5)</span>`);
  parts.push(`<span class="${c.currencies === 6 ? 'ok' : 'miss'}">FX (${c.currencies}/6)</span>`);
  $('coverage').innerHTML = parts.join('');
  $('cov-wrap').style.display = '';
}

async function loadStatus() {
  try {
    const r = await fetch('status');
    const j = await r.json();
    if (!j.has_report) {
      setStatus('', 'No report generated yet.');
      return;
    }
    const when = new Date(j.generated_at);
    const ago = Math.round((Date.now() - when.getTime()) / 60000);
    setStatus('ok', `Last generated ${when.toLocaleString()} (${ago} min ago).`);
    $('view').style.display = '';
    $('dl').style.display = '';
    renderCoverage(j.coverage);
  } catch (e) {
    setStatus('err', 'Could not reach server.');
  }
}

$('gen').addEventListener('click', async () => {
  const btn = $('gen');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Fetching latest data …';
  setStatus('busy', 'Fetching market data (Yahoo, Moneycontrol, Tickertape, BankBazaar) — usually 10–30s.');
  try {
    const r = await fetch('generate', { method: 'POST' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    setStatus('ok', `Generated in ${j.duration_s}s.`);
    renderCoverage(j.coverage);
    $('view').style.display = '';
    $('dl').style.display = '';
  } catch (e) {
    setStatus('err', `Generation failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate again';
  }
});

loadStatus();
</script>
</body>
</html>
"""


@market_bp.route("/")
def md_index():
    return MD_INDEX_HTML


@market_bp.route("/status")
def md_status():
    meta = _md_load_meta()
    has_report = MD_REPORT_HTML.exists()
    return jsonify({
        "has_report": has_report,
        "generated_at": meta["generated_at"] if meta else None,
        "coverage": meta["coverage"] if meta else None,
    })


@market_bp.route("/generate", methods=["POST"])
def md_generate():
    if not md_gen_lock.acquire(blocking=False):
        return jsonify({"error": "Another generation is in progress."}), 409
    try:
        info = _md_generate_html()
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        md_gen_lock.release()


@market_bp.route("/report")
def md_report():
    if not MD_REPORT_HTML.exists():
        abort(404, "Report HTML not generated yet.")
    return send_file(MD_REPORT_HTML, mimetype="text/html")


@market_bp.route("/report.pdf")
def md_report_pdf():
    try:
        pdf = _md_render_pdf()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    today = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        pdf, mimetype="application/pdf",
        as_attachment=True,
        download_name=f"market-digest-{today}.pdf",
    )


@market_bp.route("/card/indices")
def md_card_indices():
    html_file = MD_OUTPUT / "card_indices.html"
    if not html_file.exists():
        abort(404, "Indices card HTML not generated yet.")
    return send_file(html_file, mimetype="text/html")


@market_bp.route("/card/news")
def md_card_news():
    html_file = MD_OUTPUT / "card_news.html"
    if not html_file.exists():
        abort(404, "News card HTML not generated yet.")
    return send_file(html_file, mimetype="text/html")


@market_bp.route("/card/indices.png")
def md_card_indices_png():
    try:
        png = _md_render_png("indices")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(png, mimetype="image/png")


@market_bp.route("/card/news.png")
def md_card_news_png():
    try:
        png = _md_render_png("news")
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return send_file(png, mimetype="image/png")


# Register Blueprints on Master App
app.register_blueprint(samc_bp, url_prefix="/tools/samc-micro-digest")
app.register_blueprint(market_bp, url_prefix="/tools/market-digest-pdf")


# ---------------------------------------------------------------------------
# Marketplace Portal Routes
# ---------------------------------------------------------------------------
TOOLS_METADATA = [
    {
        "id": "samc-micro-digest",
        "name": "Shriram Micro Digest",
        "description": "WhatsApp-optimized mobile card generator for Shriram Mutual Fund. Generates, configures, and publishes daily market digests.",
        "url": "/tools/samc-micro-digest/",
        "port": None,
        "category": "INFOGRAPHICS",
        "icon": "chart-bar",
        "tags": ["WhatsApp", "Mobile", "yfinance"],
        "status": "online"
    },
    {
        "id": "market-digest-pdf",
        "name": "Shriram Market Digest (PDF)",
        "description": "Comprehensive daily PDF market summaries and digests generated from live market sentiment and indicators, branded for Shriram.",
        "url": "/tools/market-digest-pdf/",
        "port": None,
        "category": "REPORTS",
        "icon": "document",
        "tags": ["PDF", "A4 Report", "Headless Chrome"],
        "status": "online"
    },
    {
        "id": "options-pricing",
        "name": "Options Valuation Engine",
        "description": "Black-Scholes option pricing model with real-time Greeks calculation and volatility surface plotting.",
        "url": "#",
        "port": None,
        "category": "ANALYTICS",
        "icon": "chart-line",
        "tags": ["Options", "Greeks"],
        "status": "coming-soon",
        "release": "Q3 2026"
    },
    {
        "id": "portfolio-optimizer",
        "name": "Portfolio Optimizer",
        "description": "Modern Portfolio Theory (MPT) simulation for optimal risk/return frontier allocation.",
        "url": "#",
        "port": None,
        "category": "ANALYTICS",
        "icon": "briefcase",
        "tags": ["Optimization", "MPT"],
        "status": "coming-soon",
        "release": "Q4 2026"
    }
]


# ---------------------------------------------------------------------------
# Authentication & Session Hook
# ---------------------------------------------------------------------------
# Routes reachable without an authenticated session.
PUBLIC_PATHS = {"/login", "/api/signup", "/api/forgot", "/logo.jpeg"}
# When a user must change their password, only these paths are reachable.
MUST_CHANGE_ALLOWED = {"/account/password", "/api/account/password", "/logout", "/logo.jpeg"}


@app.before_request
def check_auth():
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return

    if not session.get("user_id"):
        if path.startswith("/api/") or "/api/" in path:
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login"))

    # Force a password change before anything else is accessible.
    if session.get("must_change_password") and path not in MUST_CHANGE_ALLOWED:
        if path.startswith("/api/") or "/api/" in path:
            return jsonify({"error": "Password change required", "must_change_password": True}), 403
        return redirect(url_for("account_password"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        user = db_helper.verify_user(mobile, password)
        if not user:
            db_helper.add_log(mobile or "unknown", "Failed login attempt")
            return render_template("login.html", error="Invalid mobile number or password.")
        if user["status"] == "pending":
            return render_template("login.html", error="Your account is awaiting administrator approval.")
        if user["status"] == "disabled":
            return render_template("login.html", error="This account has been disabled. Contact an administrator.")

        session["user_id"] = user["id"]
        session["mobile"] = user["mobile"]
        session["role"] = user["role"]
        session["must_change_password"] = user["must_change_password"]
        db_helper.add_log(user["mobile"], "Logged in successfully")
        if user["must_change_password"]:
            return redirect(url_for("account_password"))
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    mobile = session.get("mobile", "unknown")
    db_helper.add_log(mobile, "Logged out")
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Self-service: signup, forgot-password, change own password
# ---------------------------------------------------------------------------
@app.route("/api/signup", methods=["POST"])
def api_signup():
    try:
        body = request.get_json(force=True)
        mobile = (body.get("mobile") or "").strip()
        password = body.get("password") or ""
        ok, msg = db_helper.signup_user(mobile, password)
        if ok:
            db_helper.add_log(mobile, "Requested a new account (pending approval)")
            return jsonify({"ok": True, "message": "Account created. An administrator will review and activate it shortly."})
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/forgot", methods=["POST"])
def api_forgot():
    try:
        body = request.get_json(force=True)
        mobile = (body.get("mobile") or "").strip()
        ok, msg = db_helper.validate_mobile(mobile)
        if not ok:
            return jsonify({"error": msg}), 400
        db_helper.request_password_reset(mobile)
        db_helper.add_log(mobile, "Requested a password reset")
        # Generic response regardless of whether the mobile exists.
        return jsonify({"ok": True, "message": "If this number is registered, an administrator has been notified to reset your password."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/account/password", methods=["GET"])
def account_password():
    return render_template("account_password.html",
                           forced=bool(session.get("must_change_password")),
                           mobile=session.get("mobile", ""))


@app.route("/api/account/password", methods=["POST"])
def api_account_password():
    try:
        body = request.get_json(force=True)
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        ok, msg = db_helper.change_own_password(session["user_id"], current, new)
        if ok:
            session["must_change_password"] = False
            db_helper.add_log(session.get("mobile"), "Changed their own password")
            return jsonify({"ok": True, "message": msg})
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Admin & User Management APIs
# ---------------------------------------------------------------------------
@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(db_helper.list_users())


@app.route("/api/admin/users", methods=["POST"])
def admin_create_user():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        body = request.get_json(force=True)
        mobile = body.get("mobile", "").strip()
        password = body.get("password", "")
        role = body.get("role", "user")
        
        ok, msg = db_helper.add_user(mobile, password, role)
        if ok:
            db_helper.add_log(session.get("mobile"), f"Created user {mobile} with role {role}")
            return jsonify({"ok": True})
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users", methods=["PUT"])
def admin_update_user_password():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        body = request.get_json(force=True)
        user_id = body.get("user_id")
        password = body.get("password", "")
        
        if not user_id:
            return jsonify({"error": "User ID is required."}), 400
            
        ok, msg = db_helper.update_user_password(int(user_id), password)
        if ok:
            # Get user mobile to log it
            conn = db_helper.get_db_conn()
            u = conn.execute("SELECT mobile FROM users WHERE id = ?", (user_id,)).fetchone()
            mobile = u["mobile"] if u else f"ID {user_id}"
            conn.close()
            db_helper.add_log(session.get("mobile"), f"Updated password for user {mobile}")
            return jsonify({"ok": True})
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users", methods=["DELETE"])
def admin_delete_user():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"error": "User ID is required."}), 400
        user_id = int(user_id)
        if user_id == session.get("user_id"):
            return jsonify({"error": "Cannot delete your own administrator account."}), 400
            
        # Get user mobile to log it before deletion
        conn = db_helper.get_db_conn()
        u = conn.execute("SELECT mobile FROM users WHERE id = ?", (user_id,)).fetchone()
        mobile = u["mobile"] if u else f"ID {user_id}"
        conn.close()
        
        ok, msg = db_helper.delete_user(user_id)
        if ok:
            db_helper.add_log(session.get("mobile"), f"Deleted user {mobile}")
            return jsonify({"ok": True})
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/users/<int:uid>/approve", methods=["POST"])
def admin_approve_user(uid):
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    user = db_helper.get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    ok, msg = db_helper.set_user_status(uid, "active")
    if ok:
        db_helper.add_log(session.get("mobile"), f"Approved account {user['mobile']}")
        return jsonify({"ok": True})
    return jsonify({"error": msg}), 400


@app.route("/api/admin/users/<int:uid>/status", methods=["POST"])
def admin_set_status(uid):
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    body = request.get_json(force=True)
    status = body.get("status", "")
    user = db_helper.get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    if uid == session.get("user_id"):
        return jsonify({"error": "You cannot change the status of your own account."}), 400
    if user["mobile"] == db_helper.DEFAULT_ADMIN_MOBILE:
        return jsonify({"error": "The default administrator cannot be disabled."}), 400
    # Don't strand the system without an admin.
    if status != "active" and user["role"] == "admin" and db_helper.count_active_admins(exclude_id=uid) == 0:
        return jsonify({"error": "Cannot disable the last active administrator."}), 400
    ok, msg = db_helper.set_user_status(uid, status)
    if ok:
        db_helper.add_log(session.get("mobile"), f"Set status of {user['mobile']} to {status}")
        return jsonify({"ok": True})
    return jsonify({"error": msg}), 400


@app.route("/api/admin/users/<int:uid>/role", methods=["POST"])
def admin_set_role(uid):
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    body = request.get_json(force=True)
    role = body.get("role", "")
    if role not in ("user", "admin"):
        return jsonify({"error": "Invalid role."}), 400
    user = db_helper.get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    if user["mobile"] == db_helper.DEFAULT_ADMIN_MOBILE and role != "admin":
        return jsonify({"error": "The default administrator must remain an admin."}), 400
    # Prevent demoting the last admin.
    if role == "user" and user["role"] == "admin" and db_helper.count_active_admins(exclude_id=uid) == 0:
        return jsonify({"error": "Cannot demote the last active administrator."}), 400
    ok, msg = db_helper.set_user_role(uid, role)
    if ok:
        db_helper.add_log(session.get("mobile"), f"Changed role of {user['mobile']} to {role}")
        return jsonify({"ok": True})
    return jsonify({"error": msg}), 400


@app.route("/api/admin/users/<int:uid>/reset", methods=["POST"])
def admin_reset_password(uid):
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    body = request.get_json(force=True)
    temp_password = body.get("password", "")
    user = db_helper.get_user(uid)
    if not user:
        return jsonify({"error": "User not found."}), 404
    ok, msg = db_helper.update_user_password(uid, temp_password, force_change=True)
    if ok:
        db_helper.add_log(session.get("mobile"), f"Reset password for {user['mobile']} (temporary, must change on login)")
        return jsonify({"ok": True})
    return jsonify({"error": msg}), 400


@app.route("/api/admin/logs", methods=["GET"])
def admin_list_logs():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(db_helper.list_logs())


@app.route("/")
def index():
    return render_template("portal_index.html")


def get_company_logo(company_id: str) -> tuple[bytes, str] | None:
    logo_path = BASE_PORTAL / "logos" / f"{company_id}.jpeg"
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
        cfg = _samc_load_config()
        company_id = cfg.get("active_company", "wealth")
    logo_info = get_company_logo(company_id)
    if logo_info:
        content, mime = logo_info
        return Response(content, mimetype=mime)
    return abort(404)


@app.route("/api/admin/upload-logo", methods=["POST"])
def admin_upload_logo():
    if session.get("role") != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        company_id = request.form.get("company_id")
        if company_id not in ("wealth", "amc", "insights", "financial"):
            return jsonify({"error": "Invalid company ID"}), 400
        if 'logo' not in request.files:
            return jsonify({"error": "No logo file provided"}), 400
        file = request.files['logo']
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
            
        # Ensure logos folders exist
        logo_dir_portal = BASE_PORTAL / "logos"
        logo_dir_portal.mkdir(parents=True, exist_ok=True)
        
        logo_dir_samc = SAMC_BASE / "logos"
        logo_dir_samc.mkdir(parents=True, exist_ok=True)
        
        # Save file to both locations
        dest_portal = logo_dir_portal / f"{company_id}.jpeg"
        dest_samc = logo_dir_samc / f"{company_id}.jpeg"
        
        file_content = file.read()
        dest_portal.write_bytes(file_content)
        dest_samc.write_bytes(file_content)
        
        db_helper.add_log(session.get("mobile"), f"Uploaded new logo for Shriram {company_id.capitalize()}")

        # Re-bake the new logo into the existing SAMC cards so the change shows up
        # on the preview/images without needing a full re-generate.
        rerendered = False
        try:
            rerendered = _samc_rerender_cards()
        except Exception as e:
            print(f"[SAMC] Logo re-render failed: {e}")

        # The cards only display the ACTIVE company's logo. Tell the UI whether the
        # uploaded company is the active one so it can warn if not.
        active_company = _samc_load_config().get("active_company", "wealth")
        return jsonify({
            "ok": True,
            "rerendered": rerendered,
            "active_company": active_company,
            "is_active": company_id == active_company,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tools")
def api_tools():
    return jsonify(TOOLS_METADATA)


if __name__ == "__main__":
    port = int(os.environ.get("PORTAL_PORT", 8000))
    print("Marketplace Tools Portal starting...")
    print(f"  Portal:  http://127.0.0.1:{port}/")
    app.run(host="127.0.0.1", port=port, debug=False)
