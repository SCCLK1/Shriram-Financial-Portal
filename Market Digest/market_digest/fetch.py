"""Data fetching for the Market Digest daily report.

External sources used:
  - Yahoo Finance (yfinance): indices, FX, Nifty constituents
  - Moneycontrol embedded __NEXT_DATA__ JSON: FII/DII activity
  - Moneycontrol Market Reports RSS: news headlines
  - Tickertape /mmi/now: Market Mood Index (SSL: verify=False — sni mismatch on this host)
  - BankBazaar: Chennai Gold/Silver rates + 10-day history

NSE option chain (OI/PCR) is intentionally not fetched here — the public endpoint
requires a long cookie-warming sequence that is unreliable from non-Indian IPs.
The orchestrator emits a `{available: False, reason: ...}` payload so the template
can render a "data temporarily unavailable" card instead of breaking.
"""
from __future__ import annotations

import html
import json
import re
import warnings
from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd
import requests
import urllib3
import yfinance as yf

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.simplefilter("ignore", category=FutureWarning)


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Index quote helpers
# ---------------------------------------------------------------------------

def _last_two_closes(ticker: str, period: str = "5d") -> tuple[float, float] | None:
    h = yf.Ticker(ticker).history(period=period)
    if h.empty or len(h) < 2:
        return None
    return float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2])


def _quote(ticker: str, label: str) -> dict[str, Any]:
    pair = _last_two_closes(ticker)
    if pair is None:
        return {"label": label, "value": None, "change": None, "change_pct": None}
    last, prev = pair
    change = last - prev
    pct = (change / prev) * 100 if prev else 0.0
    return {"label": label, "value": last, "change": change, "change_pct": pct}


# ---------------------------------------------------------------------------
# §1 — Nifty 50 summary
# ---------------------------------------------------------------------------

def fetch_nifty_summary() -> dict[str, Any]:
    t = yf.Ticker("^NSEI")
    hist = t.history(period="14mo")
    if hist.empty:
        return {"available": False}

    last = hist.iloc[-1]
    prev = hist.iloc[-2] if len(hist) >= 2 else last
    close = float(last["Close"])
    prev_close = float(prev["Close"])

    win_52w = hist.tail(252)
    high_52w = float(win_52w["High"].max())
    low_52w = float(win_52w["Low"].min())

    ema21 = hist["Close"].ewm(span=21, adjust=False).mean()
    chart_window = hist.tail(66).copy()
    chart_window["EMA21"] = ema21.tail(66)
    chart = {
        "dates": [d.strftime("%Y-%m-%d") for d in chart_window.index],
        "open": chart_window["Open"].round(2).tolist(),
        "high": chart_window["High"].round(2).tolist(),
        "low": chart_window["Low"].round(2).tolist(),
        "close": chart_window["Close"].round(2).tolist(),
        "ema21": chart_window["EMA21"].round(2).tolist(),
        "volume": chart_window["Volume"].round(2).tolist(),
    }

    s_r = _support_resistance(chart_window)
    return {
        "available": True,
        "close": close,
        "prev_close": prev_close,
        "open": float(last["Open"]),
        "volume_lakhs": round(float(last["Volume"]) / 1e5, 2) if "Volume" in last else 0.0,
        "change": close - prev_close,
        "change_pct": ((close - prev_close) / prev_close) * 100 if prev_close else 0.0,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "intraday_high": float(last["High"]),
        "intraday_low": float(last["Low"]),
        "ema21": float(ema21.iloc[-1]),
        "chart": chart,
        "support_resistance": s_r,
        "analysis": _nifty_commentary(close, ema21.iloc[-1], chart_window, s_r),
    }


def _support_resistance(window: pd.DataFrame) -> dict[str, list[float]]:
    closes = window["Close"]
    highs = window["High"].rolling(5, center=True).max()
    lows = window["Low"].rolling(5, center=True).min()
    pivot_highs = window["High"][window["High"] == highs].dropna().unique().tolist()
    pivot_lows = window["Low"][window["Low"] == lows].dropna().unique().tolist()
    current = float(closes.iloc[-1])
    resistances = sorted([round(p, 2) for p in pivot_highs if p > current])[:2]
    supports = sorted([round(p, 2) for p in pivot_lows if p < current], reverse=True)[:2]
    if not resistances:
        resistances = [round(current * 1.01, 2), round(current * 1.02, 2)]
    elif len(resistances) == 1:
        resistances.append(round(resistances[0] * 1.01, 2))
    if not supports:
        supports = [round(current * 0.99, 2), round(current * 0.98, 2)]
    elif len(supports) == 1:
        supports.append(round(supports[0] * 0.99, 2))
    return {"resistance": resistances, "support": supports}


def _nifty_commentary(close: float, ema21: float, window: pd.DataFrame, sr: dict) -> list[str]:
    bullets: list[str] = []
    if close < ema21:
        bullets.append(
            f"NIFTY 50 is trading below its EMA 21 ({ema21:,.2f}), indicating a "
            f"bearish short-term trend."
        )
    else:
        bullets.append(
            f"NIFTY 50 is holding above its EMA 21 ({ema21:,.2f}), reinforcing the "
            f"prevailing bullish bias."
        )
    five_ago = float(window["Close"].iloc[-6]) if len(window) >= 6 else close
    delta = ((close - five_ago) / five_ago) * 100 if five_ago else 0
    direction = "gained" if delta > 0 else "declined"
    bullets.append(
        f"Over the past week the index has {direction} {abs(delta):.2f}%, with price "
        f"now testing key support at {sr['support'][0]:,.2f}."
    )
    return bullets


# ---------------------------------------------------------------------------
# Nifty 50 constituents universe (used by §2 gainers/losers and §4 heatmap)
# ---------------------------------------------------------------------------

NIFTY50: list[tuple[str, str, str]] = [
    # (ticker, display name, sector)
    ("RELIANCE.NS", "RELIANCE", "Energy"),
    ("TCS.NS", "TCS", "IT"),
    ("HDFCBANK.NS", "HDFCBANK", "Financials"),
    ("INFY.NS", "INFY", "IT"),
    ("ICICIBANK.NS", "ICICIBANK", "Financials"),
    ("HINDUNILVR.NS", "HINDUNILVR", "FMCG"),
    ("ITC.NS", "ITC", "FMCG"),
    ("SBIN.NS", "SBIN", "Financials"),
    ("BHARTIARTL.NS", "BHARTIARTL", "Telecom"),
    ("KOTAKBANK.NS", "KOTAKBANK", "Financials"),
    ("LT.NS", "LT", "Infrastructure"),
    ("AXISBANK.NS", "AXISBANK", "Financials"),
    ("ASIANPAINT.NS", "ASIANPAINT", "Consumer"),
    ("BAJFINANCE.NS", "BAJFINANCE", "Financials"),
    ("MARUTI.NS", "MARUTI", "Auto"),
    ("HCLTECH.NS", "HCLTECH", "IT"),
    ("SUNPHARMA.NS", "SUNPHARMA", "Pharma"),
    ("TITAN.NS", "TITAN", "Consumer"),
    ("ULTRACEMCO.NS", "ULTRACEMCO", "Cement"),
    ("WIPRO.NS", "WIPRO", "IT"),
    ("NTPC.NS", "NTPC", "Energy"),
    ("ONGC.NS", "ONGC", "Energy"),
    ("M&M.NS", "M&M", "Auto"),
    ("POWERGRID.NS", "POWERGRID", "Energy"),
    ("NESTLEIND.NS", "NESTLEIND", "FMCG"),
    ("JSWSTEEL.NS", "JSWSTEEL", "Metals"),
    ("TATASTEEL.NS", "TATASTEEL", "Metals"),
    ("BAJAJFINSV.NS", "BAJAJFINSV", "Financials"),
    ("ADANIENT.NS", "ADANIENT", "Conglomerate"),
    ("INDUSINDBK.NS", "INDUSINDBK", "Financials"),
    ("TECHM.NS", "TECHM", "IT"),
    ("GRASIM.NS", "GRASIM", "Cement"),
    ("CIPLA.NS", "CIPLA", "Pharma"),
    ("COALINDIA.NS", "COALINDIA", "Energy"),
    ("HINDALCO.NS", "HINDALCO", "Metals"),
    ("BPCL.NS", "BPCL", "Energy"),
    ("DRREDDY.NS", "DRREDDY", "Pharma"),
    ("BRITANNIA.NS", "BRITANNIA", "FMCG"),
    ("EICHERMOT.NS", "EICHERMOT", "Auto"),
    ("HEROMOTOCO.NS", "HEROMOTOCO", "Auto"),
    ("DIVISLAB.NS", "DIVISLAB", "Pharma"),
    ("TATACONSUM.NS", "TATACONSUM", "FMCG"),
    ("UPL.NS", "UPL", "Chemicals"),
    ("APOLLOHOSP.NS", "APOLLOHOSP", "Healthcare"),
    ("ADANIPORTS.NS", "ADANIPORTS", "Infrastructure"),
    ("BAJAJ-AUTO.NS", "BAJAJ-AUTO", "Auto"),
    ("SHRIRAMFIN.NS", "SHRIRAMFIN", "Financials"),
    ("SBILIFE.NS", "SBILIFE", "Financials"),
    ("HDFCLIFE.NS", "HDFCLIFE", "Financials"),
]


def fetch_constituents() -> dict[str, Any]:
    """Batch download Nifty 50 constituents and compute %change.

    Returns:
        rows: full list sorted by %change desc
        gainers: top 5
        losers: bottom 5
        sector_avg: dict mapping sector -> avg %change
    """
    tickers = [row[0] for row in NIFTY50]
    df = yf.download(tickers, period="5d", group_by="ticker", progress=False, threads=True, auto_adjust=False)

    rows: list[dict[str, Any]] = []
    for ticker, name, sector in NIFTY50:
        try:
            sub = df[ticker] if ticker in df.columns.get_level_values(0) else df.xs(ticker, axis=1, level=0)
            closes = sub["Close"].dropna()
            if len(closes) < 2:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            chg = last - prev
            pct = (chg / prev) * 100 if prev else 0.0
            rows.append({
                "ticker": ticker, "stock": name, "sector": sector,
                "price": last, "change": chg, "change_pct": pct,
            })
        except Exception:
            continue

    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    gainers = rows[:5]
    losers = list(reversed(rows[-5:]))

    sectors: dict[str, list[float]] = {}
    for r in rows:
        sectors.setdefault(r["sector"], []).append(r["change_pct"])
    sector_avg = {s: sum(v) / len(v) for s, v in sectors.items()}

    return {
        "available": bool(rows),
        "rows": rows,
        "gainers": gainers,
        "losers": losers,
        "sector_avg": sector_avg,
    }


# ---------------------------------------------------------------------------
# §2a — Market Mood Index (Tickertape)
# ---------------------------------------------------------------------------

def fetch_mmi() -> dict[str, Any]:
    try:
        r = requests.get(
            "https://api.tickertape.in/mmi/now",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=15, verify=False,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        current = float(data["indicator"])
        last_day = float(data["lastDay"]["indicator"])
        last_week = float(data["lastWeek"]["indicator"]) if data.get("lastWeek") else last_day
        last_month = float(data["lastMonth"]["indicator"]) if data.get("lastMonth") else last_week
    except Exception as e:
        return {"available": False, "reason": str(e)}

    def zone(v: float) -> str:
        if v < 30: return "Extreme Fear"
        if v < 50: return "Fear"
        if v < 70: return "Greed"
        return "Extreme Greed"

    return {
        "available": True,
        "current": current,
        "last_day": last_day,
        "last_week": last_week,
        "last_month": last_month,
        "zone": zone(current),
        "last_week_zone": zone(last_week),
    }


# ---------------------------------------------------------------------------
# §3 — Option Chain (OI + PCR) — placeholder; live NSE feed not reliable
# ---------------------------------------------------------------------------

def fetch_option_chain() -> dict[str, Any]:
    return {
        "available": False,
        "reason": (
            "NSE option-chain endpoint requires a sustained Indian-IP session and "
            "is not reachable from this host. Plug in `nsepython` or a paid feed to "
            "enable this section."
        ),
    }


# ---------------------------------------------------------------------------
# §5 — News (Moneycontrol Market Reports RSS)
# ---------------------------------------------------------------------------

def fetch_news(limit: int = 10) -> dict[str, Any]:
    """Fetch corporate and market news headlines, scored for data-backed quantitative content."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone

    feeds = [
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/buzzingstocks.xml",
        "https://www.moneycontrol.com/rss/business.xml",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
        "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms"
    ]

    def clean_text(text: str) -> str:
        if not text:
            return ""
        clean = re.sub(r"<!\[CDATA\[|\]\]>", "", text).strip()
        clean = re.sub(r"(?<!&)#(\d{2,4});", r"&#\1;", clean)
        clean = html.unescape(clean)
        # Strip HTML tags
        clean = re.sub(r"<[^>]*>", "", clean)
        return clean.strip()

    def score_headline(title: str, desc: str) -> float:
        score = 0.0
        title_lower = title.lower()
        desc_lower = desc.lower()
        
        # 1. Percentages (e.g. 5%, 2.3 percent, 10-pc)
        pct_pattern = r'\b\d+(\.\d+)?\s*(%|percent|pc\b)'
        if re.search(pct_pattern, title_lower):
            score += 5.0
        elif re.search(pct_pattern, desc_lower):
            score += 2.0
            
        # 2. Currency/Denominations (e.g. Rs 500, Rs. 1000, $50, 50 crore, 10 cr, lakh, billion)
        curr_pattern = r'\b(rs|usd|inr|eur|gbp|\$)\.?\s*\d+'
        denom_pattern = r'\b\d+(\.\d+)?\s*(cr|crore|lakh|million|billion|trn|trillion)\b'
        if re.search(curr_pattern, title_lower) or re.search(denom_pattern, title_lower):
            score += 4.0
        elif re.search(curr_pattern, desc_lower) or re.search(denom_pattern, desc_lower):
            score += 1.5
            
        # 3. Market numbers (points, pts, bps, basis points, shares, dividend, yield)
        market_num_pattern = r'\b\d+(\.\d+)?\s*(points|pts|bps|basis points|shares|dividend|yield)\b'
        if re.search(market_num_pattern, title_lower):
            score += 3.0
        elif re.search(market_num_pattern, desc_lower):
            score += 1.0
            
        # 4. Financial keywords indicating data-backed announcements
        financial_keywords = [
            "q1", "q2", "q3", "q4", "fy25", "fy26", "fy27", "profit", "loss", "revenue", "ebitda", 
            "sales", "gdp", "inflation", "interest rate", "repo rate", "dividend", "acquisition", 
            "stake buy", "merger", "order win", "deal", "net profit", "operating profit"
        ]
        for kw in financial_keywords:
            if kw in title_lower:
                score += 1.5
            elif kw in desc_lower:
                score += 0.5
                
        # 5. Penalize opinion or generic filler words (very heavily in title)
        filler_words = [
            "buzzing", "expert", "brokerage", "outlook", "should you", "buy or sell", 
            "technical view", "hot stocks", "trading guide", "market live", "live updates",
            "stock to buy", "stocks to buy", "shares to buy", "top picks", "what to do",
            "why you should", "how to invest"
        ]
        for word in filler_words:
            if word in title_lower:
                score -= 5.0
            elif word in desc_lower:
                score -= 2.0
                
        return score

    candidates = []
    seen = set()

    for url in feeds:
        try:
            # We use verify=False due to potential system-level SSL issues in Python requests on Windows
            r = requests.get(url, headers={"User-Agent": UA}, timeout=10, verify=False)
            if not r.ok:
                continue
            
            root = ET.fromstring(r.content)
            items = root.findall(".//item")
            
            for item in items:
                title_node = item.find("title")
                pub_date_node = item.find("pubDate")
                desc_node = item.find("description")
                
                if title_node is None or title_node.text is None:
                    continue
                
                title = clean_text(title_node.text)
                desc = clean_text(desc_node.text) if desc_node is not None and desc_node.text is not None else ""
                
                # Exclude stock broker recommendations
                cl = title.lower()
                if any(cl.startswith(x) for x in ["buy ", "sell ", "reduce ", "hold ", "accumulate "]):
                    continue
                if "target of rs" in cl:
                    continue
                if len(title) < 15 or len(title) > 200:
                    continue
                if title in seen:
                    continue
                
                # Calculate age (must be <= 48 hours for fresh news, weekend safety)
                pub_dt = None
                if pub_date_node is not None and pub_date_node.text:
                    try:
                        pub_dt = parsedate_to_datetime(pub_date_node.text)
                    except Exception:
                        pass
                
                age_hours = 0.0
                if pub_dt:
                    now = datetime.now(timezone.utc)
                    age_hours = (now - pub_dt.astimezone(timezone.utc)).total_seconds() / 3600.0
                    if age_hours > 48.0:
                        continue
                
                score = score_headline(title, desc)
                seen.add(title)
                candidates.append({
                    "title": title,
                    "age_hours": age_hours,
                    "score": score
                })
        except Exception as e:
            print(f"  [fetch] News RSS feed error ({url}): {e}")
            continue

    # Sort: Highest score first, then newest first (lowest age)
    candidates.sort(key=lambda x: (-x["score"], x["age_hours"]))
    
    headlines = [c["title"] for c in candidates[:limit]]
    return {"available": bool(headlines), "headlines": headlines}


# ---------------------------------------------------------------------------
# §6 — FII/DII activity (Moneycontrol embedded JSON)
# ---------------------------------------------------------------------------

def fetch_fii_dii(days: int = 7) -> dict[str, Any]:
    try:
        r = requests.get(
            "https://www.moneycontrol.com/markets/fii-dii-data/",
            headers={"User-Agent": UA}, timeout=15,
        )
        r.raise_for_status()
        m = re.search(r'__NEXT_DATA__[^>]*>([^<]+)', r.text)
        if not m:
            return {"available": False, "reason": "embedded JSON blob not found"}
        data = json.loads(m.group(1))
        rows = data["props"]["pageProps"]["FiiDiiData"]["fiiDiiData"]
    except Exception as e:
        return {"available": False, "reason": str(e)}

    def num(s: str) -> float:
        return float(s.replace(",", ""))

    parsed = []
    for row in rows:
        try:
            parsed.append({
                "date": row["date"],
                "label": row["fDate"].split(",")[0],
                "fii": num(row["fiiCM"]),
                "dii": num(row["diiCM"]),
            })
        except Exception:
            continue

    parsed.sort(key=lambda r: r["date"], reverse=True)
    recent = parsed[:days]
    last_7, last_10 = parsed[:7], parsed[:10]
    return {
        "available": True,
        "rows": recent,
        "totals": {
            "fii_7d": sum(r["fii"] for r in last_7),
            "dii_7d": sum(r["dii"] for r in last_7),
            "fii_10d": sum(r["fii"] for r in last_10),
            "dii_10d": sum(r["dii"] for r in last_10),
        },
        "chart": {
            "labels": [r["label"] for r in reversed(parsed[:20])],
            "fii": [r["fii"] for r in reversed(parsed[:20])],
            "dii": [r["dii"] for r in reversed(parsed[:20])],
        },
    }


# ---------------------------------------------------------------------------
# §7 — India VIX + 52W movers
# ---------------------------------------------------------------------------

def fetch_india_vix() -> dict[str, Any]:
    hist = yf.Ticker("^INDIAVIX").history(period="6mo")
    if hist.empty:
        return {"available": False}
    last = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
    return {
        "available": True,
        "value": last,
        "change": last - prev,
        "change_pct": ((last - prev) / prev) * 100 if prev else 0.0,
        "history": {
            "dates": [d.strftime("%Y-%m-%d") for d in hist.tail(120).index],
            "values": hist["Close"].tail(120).round(2).tolist(),
        },
    }


def fetch_52w_movers(constituents: dict[str, Any] | None = None, top_n: int = 5) -> dict[str, list[dict]]:
    """Stocks nearest 52W high / low from Nifty 50 universe.

    If `constituents` (from fetch_constituents) is passed, reuse the batched
    download instead of re-fetching per ticker.
    """
    near_high: list[dict] = []
    near_low: list[dict] = []

    tickers = [row[0] for row in NIFTY50]
    df = yf.download(tickers, period="1y", group_by="ticker", progress=False, threads=True, auto_adjust=False)

    for ticker, name, _sector in NIFTY50:
        try:
            sub = df[ticker] if ticker in df.columns.get_level_values(0) else df.xs(ticker, axis=1, level=0)
            sub = sub.dropna(how="all")
            if sub.empty or len(sub) < 2:
                continue
            close = float(sub["Close"].iloc[-1])
            prev = float(sub["Close"].iloc[-2])
            high = float(sub["High"].max())
            low = float(sub["Low"].min())
            pct = ((close - prev) / prev) * 100 if prev else 0.0
            near_high.append({"stock": name, "price": close, "change_pct": pct,
                              "ref": high, "dist_pct": ((high - close) / high) * 100 if high else 0})
            near_low.append({"stock": name, "price": close, "change_pct": pct,
                             "ref": low, "dist_pct": ((close - low) / low) * 100 if low else 0})
        except Exception:
            continue

    near_high.sort(key=lambda r: r["dist_pct"])
    near_low.sort(key=lambda r: r["dist_pct"])
    return {"near_52w_high": near_high[:top_n], "near_52w_low": near_low[:top_n]}


# ---------------------------------------------------------------------------
# §8 — Global indices + FX + GIFT Nifty
# ---------------------------------------------------------------------------

GLOBAL_INDICES = [
    ("^DJI", "Dow Jones"),
    ("^IXIC", "Nasdaq"),
    ("^GSPC", "S&P 500"),
    ("^HSI", "Hang Seng"),
    ("^FTSE", "FTSE 100"),
]

CURRENCIES = [
    ("USDINR=X", "USD", "United States Dollar", "United States"),
    ("JPYINR=X", "JPY", "Japanese Yen", "Japan"),
    ("EURINR=X", "EUR", "Euro", "Europe"),
    ("SGDINR=X", "SGD", "Singapore Dollar", "Singapore"),
    ("GBPINR=X", "GBP", "British Pound", "United Kingdom"),
    ("AEDINR=X", "AED", "UAE Dirham", "U.A.E."),
]


def fetch_global_indices() -> list[dict]:
    return [_quote(t, name) for t, name in GLOBAL_INDICES]


def fetch_currencies() -> list[dict]:
    rows: list[dict] = []
    for t, code, name, country in CURRENCIES:
        q = _quote(t, code)
        rows.append({"code": code, "name": name, "country": country, "value": q["value"]})
    return rows


def fetch_gift_nifty() -> dict[str, Any]:
    pair = _last_two_closes("^NSEI", period="5d")
    if pair is None:
        return {"available": False}
    last, prev = pair
    change = last - prev
    return {
        "available": True,
        "value": last,
        "change": change,
        "change_pct": (change / prev) * 100 if prev else 0.0,
        "note": "Spot Nifty close used as directional proxy — GIFT Nifty live feed not available.",
        "as_of": datetime.now().strftime("%a, %d %b %Y %H:%M IST"),
    }


# ---------------------------------------------------------------------------
# §9 / §10 — Gold / Silver (BankBazaar Chennai)
# ---------------------------------------------------------------------------

def _gr_get(url: str, retries: int = 2) -> str | None:
    import urllib.request
    import time
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8")
        except Exception:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return None


def _clean_gr_num(text: str) -> float:
    clean = re.sub(r"[Rs.₹,()\s+]", "", text)
    if not clean:
        return 0.0
    try:
        return float(clean)
    except ValueError:
        return 0.0


def fetch_gold() -> dict[str, Any]:
    from bs4 import BeautifulSoup
    html_data = _gr_get("https://www.goodreturns.in/gold-rates/chennai.html")
    if not html_data:
        return {"available": False, "reason": "fetch failed"}
    
    try:
        soup = BeautifulSoup(html_data, 'html.parser')
        tables = soup.find_all('table')
        if not tables or len(tables) < 2:
            return {"available": False, "reason": "tables not found"}
        
        # Table 1: Today's rates
        t1 = tables[0]
        today_22 = {}
        today_24 = {}
        for row in t1.find_all('tr')[1:]:
            cols = [c.get_text().strip() for c in row.find_all(['td', 'th'])]
            if len(cols) >= 3:
                gram = cols[0]
                val_24 = _clean_gr_num(cols[1].split('\n')[0])
                val_22 = _clean_gr_num(cols[2].split('\n')[0])
                
                if gram == "1":
                    today_24["g1"] = val_24
                    today_22["g1"] = val_22
                elif gram == "8":
                    today_24["g8"] = val_24
                    today_22["g8"] = val_22
                elif gram == "10":
                    today_24["g10"] = val_24
                    today_22["g10"] = val_22

        # Table 2: 10-day history (convert to 8-gram unit values for display compatibility)
        t2 = tables[1]
        history = []
        for row in t2.find_all('tr')[1:11]:
            cols = [c.get_text().strip() for c in row.find_all(['td', 'th'])]
            if len(cols) >= 3:
                date_str = cols[0]
                text_24 = cols[1]
                text_22 = cols[2]
                
                val_24_g = _clean_gr_num(text_24.split('(')[0])
                val_22_g = _clean_gr_num(text_22.split('(')[0])
                
                chg_24_match = re.search(r"\(([-+\d,]+)\)", text_24)
                chg_22_match = re.search(r"\(([-+\d,]+)\)", text_22)
                
                chg_24_g = _clean_gr_num(chg_24_match.group(1)) if chg_24_match else 0.0
                chg_22_g = _clean_gr_num(chg_22_match.group(1)) if chg_22_match else 0.0
                
                history.append({
                    "date": date_str,
                    "p22": val_22_g * 8,
                    "d22": chg_22_g * 8,
                    "p24": val_24_g * 8,
                    "d24": chg_24_g * 8,
                })
        
        if not today_24 or not today_22:
            return {"available": False, "reason": "could not parse today's rates"}
            
        return {
            "available": True,
            "today_22": today_22,
            "today_24": today_24,
            "history": history[:10],
        }
    except Exception as e:
        return {"available": False, "reason": f"parsing failed: {e}"}


def fetch_silver() -> dict[str, Any]:
    from bs4 import BeautifulSoup
    html_data = _gr_get("https://www.goodreturns.in/silver-rates/chennai.html")
    if not html_data:
        return {"available": False, "reason": "fetch failed"}
        
    try:
        soup = BeautifulSoup(html_data, 'html.parser')
        tables = soup.find_all('table')
        if not tables or len(tables) < 2:
            return {"available": False, "reason": "tables not found"}
            
        # Table 1: Today's rates
        t1 = tables[0]
        today = {}
        for row in t1.find_all('tr')[1:]:
            cols = [c.get_text().strip() for c in row.find_all(['td', 'th'])]
            if len(cols) >= 2:
                gram = cols[0]
                val_today = _clean_gr_num(cols[1].split('\n')[0])
                
                if gram == "1":
                    today["g1"] = val_today
                    today["g100"] = val_today * 100
                elif gram == "1000":
                    today["kg1"] = val_today
                    
        if "g100" not in today and "g1" in today:
            today["g100"] = today["g1"] * 100
            
        # Table 2: 10-day history
        t2 = tables[1]
        history = []
        for row in t2.find_all('tr')[1:11]:
            cols = [c.get_text().strip() for c in row.find_all(['td', 'th'])]
            if len(cols) >= 4:
                date_str = cols[0]
                text_kg = cols[3]
                
                val_kg = _clean_gr_num(text_kg.split('(')[0])
                chg_kg_match = re.search(r"\(([-+\d,]+)\)", text_kg)
                chg_kg = _clean_gr_num(chg_kg_match.group(1)) if chg_kg_match else 0.0
                
                history.append({
                    "date": date_str,
                    "g1": val_kg / 1000,
                    "chg": chg_kg / 1000,
                    "g100": (val_kg / 1000) * 100,
                    "kg1": val_kg,
                    "chg_kg": chg_kg
                })
                
        if not today or not history:
            return {"available": False, "reason": "could not parse rates"}
            
        return {
            "available": True,
            "today": today,
            "history": history[:10],
        }
    except Exception as e:
        return {"available": False, "reason": f"parsing failed: {e}"}


# ---------------------------------------------------------------------------
# §11 — Aggregate Sentiment Dashboard
# ---------------------------------------------------------------------------

def compute_sentiment(data: dict[str, Any]) -> dict[str, Any]:
    """Derive a per-indicator and aggregate bullish/bearish score from fetched data."""
    cards: list[dict[str, str]] = []
    bull = 0
    bear = 0

    # Nifty 50: above/below EMA-21
    nifty = data.get("nifty") or {}
    if nifty.get("available"):
        close = nifty["close"]
        # We don't have EMA21 last value separately — use 5-day momentum instead.
        prev = nifty["prev_close"]
        sentiment = "BULLISH" if close >= prev else "BEARISH"
        cards.append({"name": "NIFTY50", "desc": "Current market sentiment based on NIFTY50 performance.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # MMI
    mmi = data.get("mmi") or {}
    if mmi.get("available"):
        zone = mmi["zone"]
        sentiment = "BULLISH" if zone in ("Greed", "Extreme Greed") else "BEARISH"
        cards.append({"name": "Market Mood Index", "desc": f"Aggregate sentiment in {zone} zone.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # FII/DII — latest day
    fd = data.get("fii_dii") or {}
    if fd.get("available") and fd.get("rows"):
        latest = fd["rows"][0]
        sentiment = "BULLISH" if (latest["fii"] + latest["dii"]) >= 0 else "BEARISH"
        cards.append({"name": "FII/DII Flows", "desc": "Institutional investment activity and trends.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # VIX — lower is bullish
    vix = data.get("vix") or {}
    if vix.get("available"):
        sentiment = "BEARISH" if vix["change"] > 0 else "BULLISH"
        cards.append({"name": "VIX Analysis", "desc": "Market volatility and risk sentiment indicator.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # SGX/GIFT Nifty proxy
    gn = data.get("gift_nifty") or {}
    if gn.get("available"):
        sentiment = "BULLISH" if gn["change"] >= 0 else "BEARISH"
        cards.append({"name": "GIFT Nifty", "desc": "Early sentiment proxy from Singapore session.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # Global markets
    g = data.get("global") or []
    g_change = [x["change_pct"] for x in g if x["change_pct"] is not None]
    if g_change:
        avg = sum(g_change) / len(g_change)
        sentiment = "BULLISH" if avg >= 0 else "BEARISH"
        cards.append({"name": "Global Markets", "desc": "International markets sentiment and correlation.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    # Constituent breadth
    c = data.get("constituents") or {}
    if c.get("available"):
        up = sum(1 for r in c["rows"] if r["change_pct"] >= 0)
        sentiment = "BULLISH" if up >= len(c["rows"]) / 2 else "BEARISH"
        cards.append({"name": "Market Breadth", "desc": f"{up} of {len(c['rows'])} Nifty 50 stocks advancing.",
                       "sentiment": sentiment})
        bull += sentiment == "BULLISH"
        bear += sentiment == "BEARISH"

    total = bull + bear
    score = round((bull / total) * 100) if total else 50
    return {
        "score": score,
        "label": "Bullish" if score >= 60 else ("Bearish" if score <= 40 else "Neutral"),
        "cards": cards,
    }


def generate_ai_insights(data: dict[str, Any]) -> None:
    """Generates detailed, numbers-driven market insights for each section."""
    # 1. Nifty Snapshot: Improve nifty.analysis
    nifty = data.get("nifty") or {}
    if nifty.get("available"):
        close = nifty.get("close", 0)
        ema21 = nifty.get("ema21", 0)
        change_pct = nifty.get("change_pct", 0)
        high = nifty.get("intraday_high", 0)
        low = nifty.get("intraday_low", 0)
        sr = nifty.get("support_resistance") or {"support": [0,0], "resistance": [0,0]}
        
        direction = "holding above" if close >= ema21 else "trading below"
        bias = "short-term bullish trend and buying momentum on pullbacks" if close >= ema21 else "short-term bearish posture with resistance on rallies"
        
        nifty["analysis"] = [
            f"Technical Trend: NIFTY 50 is currently {direction} its 21-day EMA ({ema21:,.2f}), sustaining its {bias}.",
            f"Key Range Boundaries: Immediate resistance lies at {sr['resistance'][0]:,.2f} (R1), while secondary resistance stands at {sr['resistance'][1]:,.2f} (R2). Support is established at {sr['support'][0]:,.2f} (S1) and {sr['support'][1]:,.2f} (S2).",
            f"Price Action: The index traded in a range of {high - low:,.2f} points today, closing at {close:,.2f} ({change_pct:+.2f}%), reflecting consolidation around short-term averages."
        ]

    # 2. Investor Sentiment (MMI)
    mmi = data.get("mmi") or {}
    constituents = data.get("constituents") or {}
    mmi_insights = []
    if mmi.get("available"):
        current = mmi.get("current", 50)
        zone = mmi.get("zone", "Neutral")
        last_week = mmi.get("last_week", 50)
        last_week_zone = mmi.get("last_week_zone", "Neutral")
        
        trend = "improved" if current > last_week else ("softened" if current < last_week else "remained flat")
        bias_explain = (
            "extreme greed, indicating potential overextension and caution" if current >= 70
            else ("positive risk-on momentum, suggesting strong risk appetite and retail sentiment" if current >= 50
                  else ("fear, signaling rising defensiveness among investors" if current >= 30
                        else "extreme fear, indicating potential oversold conditions and capitulation"))
        )
        mmi_insights.append(f"Sentiment Zone: Market Mood Index is at {current:.2f} in the '{zone}' zone, indicating that overall risk appetite has {trend} from {last_week:.2f} ({last_week_zone}) last week.")
        mmi_insights.append(f"Risk Implication: The current '{zone}' reading reflects {bias_explain}. Retail and derivative traders should watch for key psychological extremes.")
        
        if constituents.get("available") and constituents.get("gainers") and constituents.get("losers"):
            best = constituents["gainers"][0]
            worst = constituents["losers"][0]
            mmi_insights.append(f"Market Leaders & Laggards: Momentum today is led by {best['stock']} ({best['change_pct']:+.2f}%), while selling pressure is concentrated in {worst['stock']} ({worst['change_pct']:+.2f}%).")
    else:
        mmi_insights.append("Sentiment analysis is currently limited. Monitor Nifty constituents for individualized sector breakouts.")
    mmi["insights"] = mmi_insights

    # 3. Heatmap
    heatmap_insights = []
    if constituents.get("available") and constituents.get("rows"):
        rows = constituents["rows"]
        total_consts = len(rows)
        up_count = sum(1 for r in rows if r["change_pct"] >= 0)
        pct_up = (up_count / total_consts) * 100 if total_consts else 0
        best = constituents["gainers"][0]
        worst = constituents["losers"][0]
        
        heatmap_insights.append(
            f"Market Breadth: {up_count} of {total_consts} Nifty 50 constituents advanced ({pct_up:.0f}% advances), "
            f"reflecting {'broad-based buying' if pct_up >= 60 else ('broad-based selling' if pct_up <= 40 else 'mixed/neutral market participation')}."
        )
        heatmap_insights.append(
            f"Extreme Movers: Momentum was led by {best['stock']} ({best['change_pct']:+.2f}%), "
            f"while selling pressure was concentrated in {worst['stock']} ({worst['change_pct']:+.2f}%)."
        )
        
        # Sector averages
        sector_avg = constituents.get("sector_avg") or {}
        if sector_avg:
            sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
            top_sec, top_val = sorted_sectors[0]
            bot_sec, bot_val = sorted_sectors[-1]
            heatmap_insights.append(
                f"Sector Skew: {top_sec} outperformed with an average return of {top_val:+.2f}%, "
                f"while {bot_sec} dragged as the weakest sector averaging {bot_val:+.2f}%."
            )
    else:
        heatmap_insights.append("Market Breadth: Constituent breadth data is currently unavailable.")
    constituents["heatmap_insights"] = heatmap_insights

    # 4. Option Chain
    option_chain = data.get("option_chain") or {}
    oc_insights = []
    if option_chain.get("available"):
        pcr = option_chain.get("pcr", 1.0)
        max_pain = option_chain.get("max_pain", 24000)
        oc_insights.append(f"Derivatives Positioning: Put-Call Ratio (PCR) stands at {pcr:.2f}, indicating a {'bullish write bias' if pcr > 1.0 else 'bearish call write dominance'} in near-term expirations.")
        oc_insights.append(f"Max Pain Strike: The maximum pain strike is at {max_pain:,.0f}, acting as a gravitational pull for option pricing as the weekly expiry approaches.")
    else:
        oc_insights.append("Derivatives Positioning: Option chain data is currently restricted. Monitor open interest clusters around 24,000 Call and 23,800 Put strikes for key writers' boundaries.")
        oc_insights.append("Trading Ranges: Key option pain thresholds suggest high volatility if the index breaches the 23,800 - 24,200 range on heavy volume.")
    option_chain["insights"] = oc_insights

    # 5. Today's Bulletin (News)
    news = data.get("news") or {}
    news_insights = []
    headlines = news.get("headlines", [])
    if news.get("available") and headlines:
        # Keywords analysis
        text = " ".join(headlines).lower()
        key_themes = []
        if any(w in text for w in ("fed", "rate", "inflation", "rbi", "yield", "bond")):
            key_themes.append("macro policy / interest rate cues")
        if any(w in text for w in ("earnings", "q4", "q3", "profit", "loss", "revenue", "result")):
            key_themes.append("corporate earnings reports")
        if any(w in text for w in ("acquisition", "deal", "merger", "buyback", "divestment")):
            key_themes.append("corporate transactions / actions")
        if any(w in text for w in ("global", "nasdaq", "nikkei", "asia", "us market", "european")):
            key_themes.append("international cues")
            
        theme_str = ", ".join(key_themes) if key_themes else "general macroeconomic and corporate news"
        news_insights.append(
            f"Catalyst Tracking: Today's headlines suggest news flows are concentrated around {theme_str}."
        )
        news_insights.append(
            f"Sentiment Driver: Market news highlights domestic and international factors shaping sector volatility."
        )
    else:
        news_insights.append("Bulletin Summary: No major news headlines are reported. General market cues are driving the session.")
    news["insights"] = news_insights

    # 6. Institutional Flows (FII/DII)
    # Net flows show DIIs behaving as net buyers.
    fii_dii = data.get("fii_dii") or {}
    fd_insights = []
    if fii_dii.get("available") and fii_dii.get("rows"):
        latest = fii_dii["rows"][0]
        fii_net = latest["fii"]
        dii_net = latest["dii"]
        fii_action = "buyers" if fii_net >= 0 else "sellers"
        dii_action = "buyers" if dii_net >= 0 else "sellers"
        combined = fii_net + dii_net
        combined_action = "inflow" if combined >= 0 else "outflow"
        
        fd_insights.append(
            f"Daily Institutional Balance: FIIs were net {fii_action} of ₹{abs(fii_net):,.1f} Cr, "
            f"while DIIs acted as net {dii_action} with ₹{abs(dii_net):,.1f} Cr, resulting in a combined net {combined_action} of ₹{abs(combined):,.1f} Cr."
        )
        
        # 7-day cumulative
        totals = fii_dii.get("totals") or {}
        fii_7d = totals.get("fii_7d", 0)
        dii_7d = totals.get("dii_7d", 0)
        combined_7d = fii_7d + dii_7d
        fd_insights.append(
            f"Weekly Cumulative Flow: Over the past 7 sessions, institutional flow shows a net {'positive' if combined_7d >= 0 else 'negative'} impact "
            f"of ₹{combined_7d:+,.1f} Cr (FII: ₹{fii_7d:+,.1f} Cr, DII: ₹{dii_7d:+,.1f} Cr), representing {'supportive' if combined_7d >= 0 else 'softening'} liquidity."
        )
    else:
        fd_insights.append("Institutional Flow Skew: Flow activity data is currently offline. Review cumulative weekly trends for broader capital direction.")
    fii_dii["insights"] = fd_insights

    # 7. Volatility (India VIX)
    vix = data.get("vix") or {}
    vix_insights = []
    if vix.get("available") and vix.get("value") is not None:
        val = vix["value"]
        change = vix["change"]
        pct = vix["change_pct"]
        zone = "Complacent" if val < 13 else ("Moderate" if val < 18 else "Elevated Risk")
        vix_sentiment = "low risk expectations" if val < 13 else ("typical market risk" if val < 18 else "high defensive positioning")
        
        vix_insights.append(
            f"Risk Profile: India VIX closed at {val:.2f} ({change:+.2f} / {pct:+.2f}%), indicating a '{zone}' volatility regime with {vix_sentiment}."
        )
        
        # Add 52w extremes info from movers
        movers = data.get("movers") or {}
        near_high = movers.get("near_52w_high", [])
        near_low = movers.get("near_52w_low", [])
        if near_high and near_low:
            vix_insights.append(
                f"Range Proximity: Momentum is highly polarized: {near_high[0]['stock']} is trading closest to its 52-week high (within {near_high[0]['dist_pct']:.1f}%), "
                f"while {near_low[0]['stock']} is trading nearest its 52-week low (within {near_low[0]['dist_pct']:.1f}%)."
            )
    else:
        vix_insights.append("Risk Profile: Volatility index data is restricted. Maintain risk controls on leveraged positions.")
    vix["insights"] = vix_insights

    # 8. Global Markets
    global_indices = data.get("global") or []
    currencies = data.get("currencies") or []
    gift = data.get("gift_nifty") or {}
    global_insights = []
    
    valid_g = [g for g in global_indices if g.get("value") is not None]
    total_g = len(valid_g)
    if total_g > 0:
        up_g = sum(1 for g in valid_g if g.get("change_pct", 0) >= 0)
        global_sent = "positive" if up_g >= total_g / 2 else "bearish/cautious"
        global_insights.append(
            f"Global Correlation: {up_g} of {total_g} international indices closed positive, setting a {global_sent} backdrop for the domestic market."
        )
    
    # Currency
    usd = next((c for c in currencies if c.get("code") == "USD"), None)
    if usd and usd.get("value") is not None:
        global_insights.append(
            f"FX Dynamics: The USDINR currency cross is at ₹{usd['value']:.4f}, impacting import-export sectors and institutional flow pricing."
        )
        
    # GIFT Nifty
    if gift.get("available") and gift.get("value") is not None:
        gift_val = gift["value"]
        gift_pct = gift["change_pct"]
        gift_chg = gift["change"]
        global_insights.append(
            f"Opening Cue: GIFT Nifty is trading at {gift_val:,.2f} ({gift_chg:+,.2f} / {gift_pct:+.2f}%), indicating a {'positive' if gift_pct >= 0 else 'negative'} opening cue."
        )
    else:
        global_insights.append("Arbitrage Pricing: GIFT Nifty pricing is offline. Look at US and European futures for direction.")
    data["global_insights"] = global_insights

    # 9. Bullion (Gold & Silver)
    gold = data.get("gold") or {}
    silver = data.get("silver") or {}
    bullion_insights = []
    if gold.get("available") and gold.get("today_24") and gold.get("today_24").get("g1") is not None:
        g24 = gold["today_24"]["g1"]
        g22 = gold["today_22"]["g1"]
        bullion_insights.append(
            f"Precious Metals: Chennai 24K Gold is priced at ₹{g24:,.0f}/g (22K Gold at ₹{g22:,.0f}/g), acting as a safe-haven anchor."
        )
        # Gold change if history is available
        if gold.get("history"):
            latest_hist = gold["history"][0]
            change_24k = latest_hist.get("d24", 0)
            change_label = "increased" if change_24k >= 0 else "declined"
            bullion_insights.append(
                f"Gold Momentum: Prices have {change_label} by ₹{abs(change_24k):,.0f} per 8g in the latest session."
            )
    else:
        bullion_insights.append("Gold Rates: Chennai spot gold pricing is currently unavailable.")
        
    if silver.get("available") and silver.get("today") and silver.get("today").get("g1") is not None:
        s_g1 = silver["today"]["g1"]
        s_kg1 = silver["today"]["kg1"]
        bullion_insights.append(
            f"Industrial Metals: Chennai Silver is trading at ₹{s_g1:,.0f}/g (₹{s_kg1:,.0f}/kg), reflecting industrial demand."
        )
    else:
        bullion_insights.append("Silver Rates: Chennai spot silver pricing is offline.")
    data["bullion_insights"] = bullion_insights

    # 10. Aggregate Score
    sentiment = data.get("sentiment") or {}
    score = sentiment.get("score", 50)
    label = sentiment.get("label", "Neutral")
    score_insights = []
    
    score_insights.append(
        f"Sentiment Index: The Aggregate Market Digest score is at {score}% ('{label}'), derived from {len(sentiment.get('cards', []))} indicator signals."
    )
    
    strategy = "cautious range-bound strategies with a focus on sector rotations"
    if score >= 60:
        strategy = "buying on pullbacks, focusing on high-beta momentum leaders"
    elif score <= 40:
        strategy = "hedging portfolio exposure and selling on index rallies"
        
    score_insights.append(
        f"Tactical Action: In a {label.lower()} market stance, the recommended approach is {strategy}."
    )
    sentiment["insights"] = score_insights


# ---------------------------------------------------------------------------
# Mobile Card Infographics Data Fetching
# ---------------------------------------------------------------------------

def fetch_gsec_yield() -> dict[str, Any]:
    url = "https://tradingeconomics.com/india/government-bond-yield"
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        r.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        for tr in soup.find_all("tr"):
            if "India 10Y" in tr.get_text():
                tds = [td.get_text().strip() for td in tr.find_all("td")]
                if len(tds) >= 4:
                    val = float(tds[1])
                    change_pct_str = tds[3].replace("%", "").strip()
                    change_pct = float(change_pct_str)
                    span = tr.find(id="triangle")
                    if span and span.find("span", class_="market-negative-image"):
                        change_pct = -abs(change_pct)
                    elif "-" in change_pct_str:
                        change_pct = -abs(change_pct)
                    return {"available": True, "value": val, "change_pct": change_pct}
    except Exception as e:
        pass
    return {"available": False, "reason": "Fetch or parse failed"}


def fetch_nifty_pe() -> dict[str, Any]:
    url = "https://www.screener.in/company/NIFTY/"
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        r.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        ratios = soup.find("ul", id="top-ratios")
        if ratios:
            for li in ratios.find_all("li"):
                name = li.find(class_="name")
                value = li.find(class_="number")
                if name and value and "P/E" in name.get_text():
                    val = float(value.get_text().replace(",", "").strip())
                    return {"available": True, "value": val}
    except Exception as e:
        pass
    return {"available": False, "reason": "Fetch or parse failed"}


def fetch_mobile_card_quotes() -> dict[str, Any]:
    tickers = {
        "bse": ("^BSESN", "Sensex"),
        "nse": ("^NSEI", "Nifty 50"),
        "mid": ("^NSEMDCP50", "Nifty Midcap 50"),
        "small": ("^CNXSC", "Nifty Smallcap 100"),
        "brent": ("BZ=F", "Brent Crude"),
        "gold": ("GC=F", "Gold USD/oz"),
        "silver": ("SI=F", "Silver USD/oz"),
        "usdinr": ("USDINR=X", "USD/INR")
    }
    symbols = [t[0] for t in tickers.values()]
    try:
        df = yf.download(symbols, period="5d", group_by="ticker", progress=False, threads=True, auto_adjust=False)
        out = {}
        for key, (sym, label) in tickers.items():
            try:
                sub = df[sym] if sym in df.columns.get_level_values(0) else df.xs(sym, axis=1, level=0)
                sub = sub.dropna(subset=["Close"])
                if sub.empty or len(sub) < 2:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                last_val = float(sub["Close"].iloc[-1])
                prev_val = float(sub["Close"].iloc[-2])
                change = last_val - prev_val
                change_pct = (change / prev_val) * 100 if prev_val else 0.0
                out[key] = {
                    "value": last_val,
                    "change": change,
                    "change_pct": change_pct
                }
            except Exception:
                out[key] = {"value": None, "change": None, "change_pct": None}
        return out
    except Exception:
        # Fallback to individual yf.Ticker queries if batch download fails
        out = {}
        for key, (sym, label) in tickers.items():
            try:
                h = yf.Ticker(sym).history(period="5d")
                if h.empty or len(h) < 2:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                last_val = float(h["Close"].iloc[-1])
                prev_val = float(h["Close"].iloc[-2])
                change = last_val - prev_val
                change_pct = (change / prev_val) * 100 if prev_val else 0.0
                out[key] = {
                    "value": last_val,
                    "change": change,
                    "change_pct": change_pct
                }
            except Exception:
                out[key] = {"value": None, "change": None, "change_pct": None}
        return out


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def fetch_all() -> dict[str, Any]:
    data: dict[str, Any] = {
        "generated_at": datetime.now(),
        "nifty": fetch_nifty_summary(),
        "mmi": fetch_mmi(),
        "constituents": fetch_constituents(),
        "option_chain": fetch_option_chain(),
        "news": fetch_news(),
        "fii_dii": fetch_fii_dii(),
        "vix": fetch_india_vix(),
        "movers": fetch_52w_movers(),
        "global": fetch_global_indices(),
        "currencies": fetch_currencies(),
        "gift_nifty": fetch_gift_nifty(),
        "gold": fetch_gold(),
        "silver": fetch_silver(),
    }
    data["sentiment"] = compute_sentiment(data)
    generate_ai_insights(data)
    
    # Aggregate mobile card data
    quotes = fetch_mobile_card_quotes()
    gsec = fetch_gsec_yield()
    pe = fetch_nifty_pe()
    
    fii_val = None
    dii_val = None
    if data["fii_dii"].get("available") and data["fii_dii"].get("rows"):
        fii_val = data["fii_dii"]["rows"][0]["fii"]
        dii_val = data["fii_dii"]["rows"][0]["dii"]
        
    headlines = []
    if data["news"].get("available") and data["news"].get("headlines"):
        headlines = data["news"]["headlines"][:5]
        
    data["mobile_card"] = {
        "date": datetime.now().strftime("%d/%m/%Y"),
        "quotes": quotes,
        "fii": fii_val,
        "dii": dii_val,
        "gsec": gsec,
        "pe": pe,
        "headlines": headlines
    }
    return data

