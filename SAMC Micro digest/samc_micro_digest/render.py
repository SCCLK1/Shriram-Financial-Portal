"""Render the SAMC Micro Digest mobile card variants from fetched data."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .fetch import compute_market_mood


# ---------------------------------------------------------------------------
# Indian-style comma formatting  (e.g. 1,23,456)
# ---------------------------------------------------------------------------

def _comma_fmt(value) -> str:
    """Format an integer with Indian-style comma grouping."""
    try:
        n = int(round(float(value)))
    except (ValueError, TypeError):
        return str(value)
    if n < 0:
        return "-" + _comma_fmt(-n)
    s = str(n)
    if len(s) <= 3:
        return s
    last3 = s[-3:]
    rest = s[:-3]
    groups: list[str] = []
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    return ",".join(groups) + "," + last3


def _build_fmt(data: dict) -> dict:
    """Pre-format all numeric values so the template only needs {{ fmt.xxx }}."""
    mc = data.get("mobile_card") or {}
    q = mc.get("quotes") or {}
    fmt: dict[str, Any] = {}

    # Indices: value + change
    for key in ("bse", "nse", "mid", "small"):
        idx = q.get(key) or {}
        val = idx.get("value")
        chg = idx.get("change")
        fmt[f"{key}_val"] = _comma_fmt(val) if val is not None else "N/A"
        if chg is not None:
            sign = "+" if chg >= 0 else ""
            fmt[f"{key}_chg"] = f"{sign}{_comma_fmt(chg)}"
            fmt[f"{key}_cls"] = "up" if chg >= 0 else "down"
        else:
            fmt[f"{key}_chg"] = None
            fmt[f"{key}_cls"] = ""

    # FII / DII — ticker uses the ₹ symbol, the Market Pulse box spells out "INR"
    fii = mc.get("fii")
    dii = mc.get("dii")
    if fii is not None:
        sign = "+" if fii >= 0 else "-"
        fmt["fii"] = f"{sign}₹{abs(fii):,.2f} Cr"
        fmt["fii_box"] = f"INR {sign} {abs(fii):,.2f} Cr"
        fmt["fii_cls"] = "up" if fii >= 0 else "down"
    else:
        fmt["fii"] = "N/A"
        fmt["fii_box"] = "N/A"
        fmt["fii_cls"] = ""

    if dii is not None:
        sign = "+" if dii >= 0 else "-"
        fmt["dii"] = f"{sign}₹{abs(dii):,.2f} Cr"
        fmt["dii_box"] = f"INR {sign} {abs(dii):,.2f} Cr"
        fmt["dii_cls"] = "up" if dii >= 0 else "down"
    else:
        fmt["dii"] = "N/A"
        fmt["dii_box"] = "N/A"
        fmt["dii_cls"] = ""

    # Commodities / Currency
    brent = (q.get("brent") or {}).get("value")
    fmt["brent"] = f"{brent:.2f}" if brent is not None else "N/A"

    gold = (q.get("gold") or {}).get("value")
    fmt["gold"] = f"{gold:,.2f}" if gold is not None else "N/A"

    silver = (q.get("silver") or {}).get("value")
    fmt["silver"] = f"{silver:.2f}" if silver is not None else "N/A"

    usdinr = (q.get("usdinr") or {}).get("value")
    fmt["usdinr"] = f"{usdinr:.2f}" if usdinr is not None else "N/A"

    # G-Sec
    gsec = mc.get("gsec") or {}
    fmt["gsec"] = f"{gsec['value']:.4f}%" if gsec.get("available") else "N/A"

    # PE ratios
    pe = mc.get("pe") or {}
    fmt["pe"] = f"{pe['value']:.1f}x" if pe.get("available") else "N/A"

    midcap_pe = mc.get("midcap_pe") or {}
    fmt["midcap_pe"] = f"{midcap_pe['value']:.1f}x" if midcap_pe.get("available") else "N/A"

    smallcap_pe = mc.get("smallcap_pe") or {}
    fmt["smallcap_pe"] = f"{smallcap_pe['value']:.1f}x" if smallcap_pe.get("available") else "N/A"

    # India VIX / US 10Y / DXY
    vix = (q.get("vix") or {}).get("value")
    fmt["vix"] = f"{vix:.2f}" if vix is not None else "N/A"

    us10y = (q.get("us10y") or {}).get("value")
    fmt["us10y"] = f"{us10y:.3f}%" if us10y is not None else "N/A"

    dxy = (q.get("dxy") or {}).get("value")
    fmt["dxy"] = f"{dxy:.2f}" if dxy is not None else "N/A"

    # Market mood — recomputed here so manual overrides (applied before render)
    # are reflected in the gauge instead of the original pre-override fetch.
    mood = compute_market_mood(q)
    fmt["mood_label"] = mood["label"]
    fmt["mood_color"] = mood["color"]
    fmt["mood_angle"] = mood["angle"]

    return fmt


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------

def get_base64_img(path: Path) -> str:
    """Read file and return it as a base64 encoded data URI, auto-detecting SVG contents."""
    if not path.exists():
        return ""
    try:
        is_svg = False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                header = f.read(200)
                if "<svg" in header.lower():
                    is_svg = True
        except Exception:
            pass
            
        if is_svg:
            mime = "image/svg+xml"
        else:
            suffix = path.suffix.lower()
            if suffix in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif suffix == ".png":
                mime = "image/png"
            elif suffix == ".webp":
                mime = "image/webp"
            elif suffix == ".svg":
                mime = "image/svg+xml"
            else:
                mime = "image/png"
                
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_report(data: dict[str, Any], output_dir: Path) -> None:
    import json
    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load config.json
    config_path = Path(__file__).parent.parent / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
            
    watermark_mode = config.get("watermark_mode", "both")
    colors = config.get("brand_colors", {})
    disclaimer_text = config.get("disclaimer_text", "Mutual Fund investments are subject to market risks, read all scheme related documents carefully.")
    
    # Dynamic per-company logo (matches the rest of the portal's branding switch).
    # Prefer a vector .svg export over the .jpeg raster when one exists.
    company_id = config.get("active_company", "wealth")
    logos_dir = Path(__file__).parent.parent / "logos"
    logo_src = logos_dir / f"{company_id}.svg"
    if not logo_src.exists():
        logo_src = logos_dir / f"{company_id}.jpeg"
    logo_base64 = ""
    if logo_src.exists():
        logo_base64 = get_base64_img(logo_src)
    else:
        project_logo = logos_dir / "logo.png"
        desktop_logo = Path("C:/Users/K964/OneDrive - Shriram Finance Limited/Desktop/logo.png")
        if project_logo.exists():
            logo_base64 = get_base64_img(project_logo)
        elif desktop_logo.exists():
            logo_base64 = get_base64_img(desktop_logo)

    # Real assets exported from the Claude Design project: the building/bull/
    # title banner artwork (clean re-export, no logo or date baked in) and
    # the 11 Market Snapshot icons. The footer's target icon and social
    # glyphs keep their hand-drawn SVG versions instead — the exported PNGs
    # for those have an opaque background that doesn't match the footer's
    # gold and would show as a visible seam.
    dc_assets = Path(__file__).parent / "dc_assets"
    banner_base64 = get_base64_img(dc_assets / "banner.png")

    icon_names = {
        "brent": "oil", "gold": "gold", "silver": "silver", "usdinr": "usd",
        "gsec": "bank", "us10y": "flag", "dxy": "dollaridx", "vix": "vix",
        "nifty_pe": "pe", "midcap_pe": "people", "smallcap_pe": "building",
    }
    icons_base64 = {
        key: get_base64_img(dc_assets / "icon" / f"{fname}.png")
        for key, fname in icon_names.items()
    }

    # Pre-format all numbers (no custom Jinja2 filters needed)
    fmt = _build_fmt(data)

    # Render the single consolidated Daily Update card
    card_daily_template = env.get_template("card_daily.html.j2")
    card_daily_html = card_daily_template.render(
        logo_base64=logo_base64,
        banner_base64=banner_base64,
        icon_brent_base64=icons_base64["brent"],
        icon_gold_base64=icons_base64["gold"],
        icon_silver_base64=icons_base64["silver"],
        icon_usdinr_base64=icons_base64["usdinr"],
        icon_gsec_base64=icons_base64["gsec"],
        icon_us10y_base64=icons_base64["us10y"],
        icon_dxy_base64=icons_base64["dxy"],
        icon_vix_base64=icons_base64["vix"],
        icon_nifty_pe_base64=icons_base64["nifty_pe"],
        icon_midcap_pe_base64=icons_base64["midcap_pe"],
        icon_smallcap_pe_base64=icons_base64["smallcap_pe"],
        watermark_mode=watermark_mode,
        colors=colors,
        disclaimer_text=disclaimer_text,
        fmt=fmt,
        **data
    )
    
    # Load and apply style overrides to retain design customisations across generation
    overrides_file = output_dir / "layout_overrides.json"
    if overrides_file.exists():
        try:
            import json
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
            if overrides:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(card_daily_html, 'html.parser')
                
                # Apply style overrides
                for selector, style_str in overrides.items():
                    if selector.startswith("__"):
                        continue
                    el = soup.select_one(selector)
                    if el:
                        el['style'] = style_str
                        
                # Apply DOM sibling ordering if present
                dom_orders = overrides.get("__dom_orders__")
                if dom_orders:
                    for parent_sel, child_sels in dom_orders.items():
                        parent_el = soup.select_one(parent_sel)
                        if parent_el:
                            resolved_children = []
                            for child_sel in child_sels:
                                child_el = soup.select_one(child_sel)
                                if child_el and child_el.parent == parent_el:
                                    resolved_children.append(child_el)
                            for child_el in resolved_children:
                                parent_el.append(child_el)
                                
                card_daily_html = str(soup)
        except Exception:
            pass

    (output_dir / "card_daily.html").write_text(card_daily_html, encoding="utf-8")
