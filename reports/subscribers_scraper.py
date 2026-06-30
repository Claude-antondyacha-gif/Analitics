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
    s = re.sub(r"[^\d\-\+]", "", s.replace(" ", "").replace(" ", ""))
    try:
        return int(s)
    except ValueError:
        return 0


def _make_session(password: str) -> requests.Session:
    session = requests.Session()
    session.verify = False
    try:
        resp = session.post(f"{_BASE_URL}/login", data={"pw": password},
                            allow_redirects=True, timeout=15)
        if resp.status_code not in (200, 302) or "Вход" in resp.text[:500]:
            logger.error(f"Subscriber scraper login failed: HTTP {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Subscriber scraper login error: {e}")
        return None
    return session


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
    session = _make_session(pw)
    if session is None:
        return {}

    try:
        html = session.get(_BASE_URL, timeout=15).text
    except Exception as e:
        logger.error(f"Subscriber scraper request failed: {e}")
        return {}

    result = {}

    total_m = re.search(r'class="total-num"[^>]*>([\d\s ]+)<', html)
    delta_m = re.search(r'class="total-delta[^"]*"[^>]*>.*?([\+\-]\d[\d\s ]*)\s+за', html, re.DOTALL)
    if total_m:
        result["_total"] = {
            "name": "Всього",
            "total": _parse_number(total_m.group(1)),
            "weekly_delta": _parse_number(delta_m.group(1)) if delta_m else 0,
        }

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


def scrape_channel_history(slug: str, password: str | None = None) -> dict[str, int]:
    """
    Fetches daily subscriber totals for a channel from /channel/SLUG.
    Returns {date_iso: total_count}, e.g. {"2026-06-30": 3080, "2026-06-29": 3070, ...}
    Parses the <table class="snap-table"> with rows: DD.MM.YYYY | followers | Δ
    """
    pw = password or os.environ.get(_PASSWORD_ENV, _DEFAULT_PASSWORD)
    session = _make_session(pw)
    if session is None:
        return {}

    try:
        html = session.get(f"{_BASE_URL}/channel/{slug}", timeout=15).text
    except Exception as e:
        logger.error(f"Channel history fetch failed for {slug}: {e}")
        return {}

    result = {}
    # Parse <table class="snap-table"> rows
    table_m = re.search(r'<table class="snap-table">(.*?)</table>', html, re.DOTALL)
    if not table_m:
        logger.warning(f"No snap-table found for channel {slug}")
        return result

    rows = re.findall(r'<tr>(.*?)</tr>', table_m.group(1), re.DOTALL)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue
        date_str = cells[0].strip()
        followers_raw = re.sub(r'<[^>]+>', '', cells[1]).strip()

        # Convert DD.MM.YYYY → YYYY-MM-DD
        dm = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', date_str)
        if not dm:
            continue
        d, mo, y = dm.groups()
        date_iso = f"{y}-{mo.zfill(2)}-{d.zfill(2)}"

        total = _parse_number(followers_raw)
        if total > 0:
            result[date_iso] = total

    logger.info(f"Channel history {slug}: {len(result)} days")
    return result


def get_new_subscribers_summary(data: dict | None = None) -> dict:
    """Returns simplified summary: {channel_name: weekly_delta, ...}"""
    if data is None:
        data = scrape_subscribers()
    return {v["name"]: v["weekly_delta"] for k, v in data.items()}
