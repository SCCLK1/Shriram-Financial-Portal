"""Render the Market Digest HTML report and mobile card variants from fetched data."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


def get_base64_img(path: Path) -> str:
    """Read file and return it as a base64 encoded data URI, auto-detecting SVG contents."""
    if not path.exists():
        return ""
    try:
        # Check if it's actually an SVG based on file headers
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
                mime = "image/png"  # default fallback
                
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return ""



def render_report(data: dict[str, Any], output_path: Path) -> Path:
    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    
    # Render the main report
    template = env.get_template("report.html.j2")
    html = template.render(**data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    
    # Copy bull_bear_banner.png to output directory (for backup/direct view)
    banner_src = Path(__file__).parent / "bull_bear_banner.png"
    banner_dest = output_path.parent / "bull_bear_banner.png"
    if banner_src.exists():
        try:
            shutil.copy2(banner_src, banner_dest)
        except Exception:
            pass
        
    # Load config.json to resolve the active company logo
    import json
    import os
    BASE_DIR = Path(__file__).resolve().parent.parent
    default_config_path = BASE_DIR.parent / "SAMC Micro digest" / "config.json"
    if not default_config_path.parent.exists():
        default_config_path = BASE_DIR / "config.json"
    config_path = Path(os.environ.get("CONFIG_PATH", str(default_config_path)))
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
            
    company_id = config.get("active_company", "wealth")
    logo_src = Path(__file__).parent.parent / "logos" / f"{company_id}.jpeg"
    logo_base64 = ""
    if logo_src.exists():
        logo_base64 = get_base64_img(logo_src)
    else:
        logos_dir = Path(__file__).parent.parent / "logos"
        project_logo = logos_dir / "logo.png"
        desktop_logo = Path("C:/Users/K964/OneDrive - Shriram Finance Limited/Desktop/logo.png")
        if project_logo.exists():
            logo_base64 = get_base64_img(project_logo)
        elif desktop_logo.exists():
            logo_base64 = get_base64_img(desktop_logo)
            
    banner_base64 = ""
    if banner_src.exists():
        banner_base64 = get_base64_img(banner_src)
        
    # Render Mobile Card - Indices
    card_indices_template = env.get_template("card_indices.html.j2")
    card_indices_html = card_indices_template.render(
        logo_base64=logo_base64,
        banner_base64=banner_base64,
        **data
    )
    (output_path.parent / "card_indices.html").write_text(card_indices_html, encoding="utf-8")
    
    # Render Mobile Card - News
    card_news_template = env.get_template("card_news.html.j2")
    card_news_html = card_news_template.render(
        logo_base64=logo_base64,
        banner_base64=banner_base64,
        **data
    )
    (output_path.parent / "card_news.html").write_text(card_news_html, encoding="utf-8")
    
    return output_path
