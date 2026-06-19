"""Publish generated mobile cards to Microsoft Teams via webhooks."""
from __future__ import annotations

import base64
from pathlib import Path
import requests


def publish_to_teams(webhook_url: str, output_dir: Path) -> tuple[bool, str]:
    """Base64-encode the generated PNG cards and send them to the Microsoft Teams webhook."""
    if not webhook_url:
        return False, "Webhook URL is empty."
        
    daily_png = output_dir / "card_daily.png"

    if not daily_png.exists():
        return False, "Generated card PNG file not found. Generate it first."

    try:
        # Base64 encode the PNG file
        with open(daily_png, "rb") as f:
            daily_b64 = base64.b64encode(f.read()).decode("utf-8")

        # Standard Connector MessageCard payload containing the consolidated card
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "F7B500",
            "summary": "SAMC Micro Digest - Daily Update",
            "sections": [
                {
                    "activityTitle": "SAMC Micro Digest - Daily Update",
                    "activitySubtitle": "Mobile-friendly daily update card",
                    "text": "Here is today's daily infographic:",
                    "images": [
                        {
                            "image": f"data:image/png;base64,{daily_b64}"
                        }
                    ]
                }
            ]
        }
        
        headers = {"Content-Type": "application/json"}
        r = requests.post(webhook_url, json=payload, headers=headers, timeout=30)
        
        if r.status_code in (200, 201, 202):
            return True, "Published successfully!"
        else:
            return False, f"HTTP Error {r.status_code}: {r.text}"
            
    except Exception as e:
        return False, f"Exception during publish: {e}"
