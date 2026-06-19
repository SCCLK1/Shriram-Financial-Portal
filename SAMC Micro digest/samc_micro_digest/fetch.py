"""Data fetching for the SAMC Micro Digest mobile cards.

External sources used:
  - Yahoo Finance (yfinance): market indices, commodities futures, USD/INR exchange rate
  - Moneycontrol: Daily FII/DII flow statistics (embedded __NEXT_DATA__ JSON)
  - Moneycontrol RSS: News headlines
  - TradingEconomics: India 10-Year Government Bond Yield (G-Sec)
  - Screener.in: Nifty 50 Price-to-Earnings (P/E) Ratio

All fetched data is checked against strict range validation bounds to ensure accuracy.
"""
from __future__ import annotations

import html
import json
import math
import re
import warnings
from datetime import datetime
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

# Validation bounds
VAL_BOUNDS = {
    "bse": (40000.0, 180000.0),
    "nse": (12000.0, 55000.0),
    "mid": (5000.0, 40000.0),
    "small": (5000.0, 40000.0),
    "fii": (-30000.0, 30000.0),
    "dii": (-30000.0, 30000.0),
    "brent": (30.0, 180.0),
    "gold": (1000.0, 6000.0),
    "silver": (10.0, 100.0),
    "usdinr": (70.0, 110.0),
    "gsec": (5.0, 11.0),
    "pe": (12.0, 45.0),
    "vix": (5.0, 60.0),
    "us10y": (1.0, 10.0),
    "dxy": (70.0, 130.0),
}


def _validate(key: str, val: Any) -> float | None:
    """Validate a numeric value against historical limits."""
    if val is None:
        return None
    try:
        f_val = float(val)
        low, high = VAL_BOUNDS[key]
        if low <= f_val <= high:
            return f_val
        print(f"  [validation] WARNING: {key} value {f_val} is out of bounds ({low}, {high}). Setting to None.")
    except (ValueError, TypeError):
        print(f"  [validation] WARNING: {key} value {val} is not a valid number. Setting to None.")
    return None


def fetch_mobile_card_quotes() -> dict[str, Any]:
    """Fetch indices, commodities and currency exchange rates from Yahoo Finance."""
    tickers = {
        "bse": ("^BSESN", "Sensex"),
        "nse": ("^NSEI", "Nifty 50"),
        "mid": ("^NSEMDCP50", "Nifty Midcap 50"),
        "small": ("^CNXSC", "Nifty Smallcap 100"),
        "brent": ("BZ=F", "Brent Crude"),
        "gold": ("GC=F", "Gold USD/oz"),
        "silver": ("SI=F", "Silver USD/oz"),
        "usdinr": ("USDINR=X", "USD/INR"),
        "vix": ("^INDIAVIX", "India VIX"),
        "us10y": ("^TNX", "US 10Y Treasury Yield"),
        "dxy": ("DX-Y.NYB", "US Dollar Index"),
    }
    symbols = [t[0] for t in tickers.values()]
    out = {}
    
    try:
        # Batch download history (5 days is safe for getting last 2 trading days)
        df = yf.download(symbols, period="5d", group_by="ticker", progress=False, threads=True, auto_adjust=False)
        for key, (sym, label) in tickers.items():
            try:
                sub = df[sym] if sym in df.columns.get_level_values(0) else df.xs(sym, axis=1, level=0)
                if isinstance(sub, pd.DataFrame) and "Close" in sub.columns:
                    sub = sub.dropna(subset=["Close"])
                    
                if sub.empty:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                
                if len(sub) == 1:
                    last_val = float(sub["Close"].iloc[-1])
                    valid_last = _validate(key, last_val)
                    # Fallback: use .info to get previousClose when history has only 1 row
                    change, change_pct = None, None
                    try:
                        info = yf.Ticker(sym).info
                        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
                        if prev_close and valid_last:
                            change = valid_last - float(prev_close)
                            change_pct = (change / float(prev_close)) * 100
                            print(f"  [fetch] {label}: used .info previousClose={prev_close} for change calc")
                    except Exception:
                        pass
                    out[key] = {
                        "value": valid_last,
                        "change": change,
                        "change_pct": change_pct
                    }
                    continue
                
                last_val = float(sub["Close"].iloc[-1])
                prev_val = float(sub["Close"].iloc[-2])
                
                # Validate the value before using it
                valid_last = _validate(key, last_val)
                if valid_last is None:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                
                change = last_val - prev_val
                change_pct = (change / prev_val) * 100 if prev_val else 0.0
                
                if math.isnan(change):
                    change = None
                if math.isnan(change_pct):
                    change_pct = None
                    
                out[key] = {
                    "value": valid_last,
                    "change": change,
                    "change_pct": change_pct
                }
            except Exception as e:
                print(f"  [fetch] Failed processing {label} ({sym}): {e}")
                out[key] = {"value": None, "change": None, "change_pct": None}
    except Exception as e:
        print(f"  [fetch] Batch download failed, trying individual tickers: {e}")
        # Fallback to individual yf.Ticker queries
        for key, (sym, label) in tickers.items():
            try:
                h = yf.Ticker(sym).history(period="5d")
                if isinstance(h, pd.DataFrame) and "Close" in h.columns:
                    h = h.dropna(subset=["Close"])
                    
                if h.empty:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                
                if len(h) == 1:
                    last_val = float(h["Close"].iloc[-1])
                    valid_last = _validate(key, last_val)
                    # Fallback: use .info to get previousClose
                    change, change_pct = None, None
                    try:
                        info = yf.Ticker(sym).info
                        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
                        if prev_close and valid_last:
                            change = valid_last - float(prev_close)
                            change_pct = (change / float(prev_close)) * 100
                            print(f"  [fetch] {label}: used .info previousClose={prev_close} for change calc")
                    except Exception:
                        pass
                    out[key] = {
                        "value": valid_last,
                        "change": change,
                        "change_pct": change_pct
                    }
                    continue
                
                last_val = float(h["Close"].iloc[-1])
                prev_val = float(h["Close"].iloc[-2])
                
                # Validate the value
                valid_last = _validate(key, last_val)
                if valid_last is None:
                    out[key] = {"value": None, "change": None, "change_pct": None}
                    continue
                
                change = last_val - prev_val
                change_pct = (change / prev_val) * 100 if prev_val else 0.0
                
                if math.isnan(change):
                    change = None
                if math.isnan(change_pct):
                    change_pct = None
                    
                out[key] = {
                    "value": valid_last,
                    "change": change,
                    "change_pct": change_pct
                }
            except Exception as ex:
                print(f"  [fetch] Failed individual fetch for {label} ({sym}): {ex}")
                out[key] = {"value": None, "change": None, "change_pct": None}
                
    return out


def fetch_fii_dii() -> tuple[float | None, float | None]:
    """Fetch daily FII and DII flow numbers (in Cr) from Moneycontrol."""
    try:
        # We need verify=False due to host-level SSL/intermediate issues
        r = requests.get(
            "https://www.moneycontrol.com/markets/fii-dii-data/",
            headers={"User-Agent": UA}, timeout=15, verify=False
        )
        r.raise_for_status()
        m = re.search(r'__NEXT_DATA__[^>]*>([^<]+)', r.text)
        if not m:
            print("  [fetch] Moneycontrol __NEXT_DATA__ JSON blob not found.")
            return None, None
            
        data = json.loads(m.group(1))
        rows = data["props"]["pageProps"]["FiiDiiData"]["fiiDiiData"]
        if not rows:
            print("  [fetch] No FII/DII data rows found.")
            return None, None
            
        # Get the latest row
        latest = rows[0]
        fii_val = float(latest["fiiCM"].replace(",", ""))
        dii_val = float(latest["diiCM"].replace(",", ""))
        
        # Validate values
        valid_fii = _validate("fii", fii_val)
        valid_dii = _validate("dii", dii_val)
        return valid_fii, valid_dii
    except Exception as e:
        print(f"  [fetch] FII/DII fetch failed: {e}")
        return None, None


def fetch_gsec_yield() -> dict[str, Any]:
    """Fetch benchmark India 10-Year Government Bond Yield from TradingEconomics."""
    url = "https://tradingeconomics.com/india/government-bond-yield"
    try:
        # TradingEconomics works with verify=True when headers are sent
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
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
                    
                    # Detect positive/negative change direction from triangle style
                    span = tr.find(id="triangle")
                    if span and span.find("span", class_="market-negative-image"):
                        change_pct = -abs(change_pct)
                    elif "-" in change_pct_str:
                        change_pct = -abs(change_pct)
                        
                    # Validate
                    valid_val = _validate("gsec", val)
                    if valid_val is not None:
                        return {"available": True, "value": valid_val, "change_pct": change_pct}
                    break
    except Exception as e:
        print(f"  [fetch] G-Sec yield fetch failed: {e}")
        
    return {"available": False, "reason": "Fetch or validation failed"}


def fetch_nifty_pe() -> dict[str, Any]:
    """Fetch Nifty 50 P/E ratio from Screener.in."""
    url = "https://www.screener.in/company/NIFTY/"
    try:
        # Screener requires verify=False due to intermediate certificate issues on Windows
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, verify=False)
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
                    
                    # Validate
                    valid_val = _validate("pe", val)
                    if valid_val is not None:
                        return {"available": True, "value": valid_val}
                    break
    except Exception as e:
        print(f"  [fetch] Nifty PE fetch failed: {e}")
        
    return {"available": False, "reason": "Fetch or validation failed"}


def fetch_news(limit: int = 5) -> list[str]:
    """Fetch corporate and market news headlines, scored for data-backed quantitative content."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone

    feeds = [
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/buzzingstocks.xml",
        "https://www.moneycontrol.com/rss/business.xml",
        "https://www.livemint.com/rss/markets",
        "https://www.livemint.com/rss/companies",
        "https://www.livemint.com/rss/economy"
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
    
    return [c["title"] for c in candidates[:limit]]


def compute_market_mood(quotes: dict[str, Any]) -> dict[str, Any]:
    """Derive a simple Bullish/Neutral/Bearish reading from index momentum and India VIX.

    Heuristic only (no ML/external sentiment source): averages the Nifty/Sensex
    percentage change and dampens it when VIX is elevated, since a rally on
    high VIX is less convincingly "bullish" than one on calm volatility.
    """
    pct_values = [
        v["change_pct"] for v in (quotes.get("nse"), quotes.get("bse"))
        if v and v.get("change_pct") is not None
    ]
    avg_pct = sum(pct_values) / len(pct_values) if pct_values else 0.0

    vix_val = (quotes.get("vix") or {}).get("value")
    vix_drag = max(0.0, (vix_val - 15.0)) * 0.05 if vix_val is not None else 0.0
    score = avg_pct - vix_drag if avg_pct >= 0 else avg_pct + vix_drag

    if score > 0.15:
        label, color = "BULLISH", "#2E7D32"
    elif score < -0.15:
        label, color = "BEARISH", "#C62828"
    else:
        label, color = "NEUTRAL", "#B8860B"

    angle = max(-90.0, min(90.0, (score / 1.5) * 90.0))
    return {"label": label, "color": color, "angle": round(angle, 1)}


def fetch_all() -> dict[str, Any]:
    """Orchestrate all data fetchers for the mobile card infographics."""
    print("  [fetch] Fetching market quotes...")
    quotes = fetch_mobile_card_quotes()

    print("  [fetch] Fetching FII/DII activity...")
    fii_val, dii_val = fetch_fii_dii()

    print("  [fetch] Fetching G-Sec yield...")
    gsec = fetch_gsec_yield()

    print("  [fetch] Fetching Nifty PE ratio...")
    pe = fetch_nifty_pe()

    print("  [fetch] Fetching news headlines...")
    headlines = fetch_news()

    now = datetime.now()

    # Packaged in the exact structure expected by the render templates
    return {
        "generated_at": now,
        "mobile_card": {
            "date": now.strftime("%d/%m/%Y"),
            "day": now.strftime("%A").upper(),
            "time": now.strftime("%I:%M %p"),
            "quotes": quotes,
            "fii": fii_val,
            "dii": dii_val,
            "gsec": gsec,
            "pe": pe,
            # No reliable free live source for these two — manual override or N/A.
            "midcap_pe": {"available": False},
            "smallcap_pe": {"available": False},
            "mood": compute_market_mood(quotes),
            "headlines": headlines
        }
    }
