"""
Scrapes subscriber counts from the Sfero social analytics dashboard.
Returns a dict of channel_name -> {total, weekly_delta}.
"""
import os
import re
import logging

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://sfero-social.178.104.128.154.sslip.io"
_PASSWORD_ENV = "SFERO_SOCIAL_PASSWORD"
_DEFAULT_PASSWORD = "Sf@r0_26"


def _parse_number(s: str) -> int:
    """Parse '5 201' or '+63' → int."""
    s = re.sub(r"[^\d\-\+]", "", s.replace(" ", "").replace(" ", ""))
    try:
        return int(s)
    except ValueError:
        return 0


def scrape_subscribers(password: str | None = None) -> dict:
    """
    Returns dict keyed by channel slug:
    {
      "instagram": {"name": "Instagram", "total": 5201, "weekly_delta": 63},
      ...
      "_total": {"name": "Всього", "total": 16973, "weekly_delta": 346},
    }
    """
    pw = password or os.environ.get(_PASSWORD_ENV, _DEFAULT_PASSWORD)
    session = requests.Session()
    session.verify = False  # self-signed cert on sslip.io host

    try:
        # Login
        resp = session.post(f"{_BASE_URL}/login", data={"pw": pw}, allow_redirects=True, timeout=15)
        if resp.status_code not in (200, 302) or "Вход" in resp.text[:500]:
            logger.error(f"Subscriber scraper login failed: HTTP {resp.status_code}")
            return {}

        html = resp.text

    except Exception as e:
        logger.error(f"Subscriber scraper request failed: {e}")
        return {}

    result = {}

    # Parse overall total
    total_m = re.search(r'class="total-num"[^>]*>([\d\s ]+)<', html)
    delta_m = re.search(r'class="total-delta[^"]*"[^>]*>.*?([\+\-]\d[\d\s ]*)\s+за', html, re.DOTALL)
    if total_m:
        result["_total"] = {
            "name": "Всього",
            "total": _parse_number(total_m.group(1)),
            "weekly_delta": _parse_number(delta_m.group(1)) if delta_m else 0,
        }

    # Parse per-channel cards
    cards = re.findall(r'<a\s+class="ch"\s+href="/channel/([^"]+)">(.*?)</a>', html, re.DOTALL)
    for slug, content in cards:
        name_m = re.search(r'<h4>(.*?)</h4>', content)
        num_m = re.search(r'class="ch-num"[^>]*>([\d\s ]+)<', content)
        delta_raw = re.search(r'([\+\-]\d[\d\s ]*)\s*/', content)

        if not name_m or not num_m:
            continue

        result[slug] = {
            "name": name_m.group(1).strip(),
            "total": _parse_number(num_m.group(1)),
            "weekly_delta": _parse_number(delta_raw.group(1)) if delta_raw else 0,
        }

    logger.info(f"Subscriber scraper: {len(result)-1} channels, total={result.get('_total', {}).get('total', '?')}")
    return result


def get_new_subscribers_summary(data: dict | None = None) -> dict:
    """
    Returns simplified summary for Google Sheets:
    {channel_name: weekly_delta, ...} plus "_total_new" key.
    """
    if data is None:
        data = scrape_subscribers()
    return {
        v["name"]: v["weekly_delta"]
        for k, v in data.items()
    }
