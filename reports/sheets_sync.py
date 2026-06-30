"""
Google Sheets sync — appends daily metrics to existing spreadsheets.
Creates auto-tabs; NEVER modifies existing user tabs.

Auto-tabs created:
  "Трафік (авто)"   — one row per campaign per day, traffic+other campaigns
  "Лідген (авто)"   — one row per campaign per day, leadgen campaigns
  "Підсумок (авто)" — aggregated metrics for 1/3/7/14/30 days
  "Traffic звіт (авто)" — one row per day with totals + subscriber counts
"""
import os
import logging
import warnings
from datetime import datetime, date

import gspread
from google.oauth2.service_account import Credentials

from storage.database import get_metrics_by_period, get_aggregated_metrics

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRAFFIC_KEYWORDS = ["traffic", "tof", "трафік", "traffik", "awareness", "reach"]
LEADGEN_KEYWORDS = ["лид", "lead", "ленд", "land", "quiz", "квиз", "квіз", "форм", "form"]


def _get_client():
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def _classify_campaign(name: str) -> str:
    n = name.lower()
    if any(k in n for k in TRAFFIC_KEYWORDS):
        return "traffic"
    if any(k in n for k in LEADGEN_KEYWORDS):
        return "leadgen"
    return "other"


def _ensure_worksheet(spreadsheet, title: str, rows: int = 2000, cols: int = 30):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _get_or_create_with_headers(spreadsheet, title: str, headers: list) -> gspread.Worksheet:
    ws = _ensure_worksheet(spreadsheet, title)
    existing = ws.get_all_values()
    if not existing or existing[0] != headers:
        ws.clear()
        ws.update("A1", [headers])
        try:
            ws.format(f"A1:{chr(64+len(headers))}1", {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.18, "green": 0.27, "blue": 0.6},
            })
        except Exception:
            pass
    return ws


def _upsert_rows(ws, headers, new_rows, key_col: int = 0):
    """Replace rows where key_col matches today, append the rest."""
    all_existing = ws.get_all_values()
    today = date.today().isoformat()
    filtered = [r for r in all_existing[1:] if r and r[key_col] != today]
    ws.clear()
    ws.update("A1", [headers] + filtered + new_rows)


def _aggregate_campaigns(rows: list) -> dict:
    stats = {}
    for r in rows:
        cid = r["campaign_id"]
        if cid not in stats:
            stats[cid] = {
                "campaign_id": cid,
                "campaign_name": r["campaign_name"],
                "spend": 0, "impressions": 0, "clicks": 0,
                "leads": 0, "link_clicks": 0, "reach": 0,
                "video_views": 0, "page_likes": 0, "purchases": 0,
                "purchase_value": 0, "ctrs": [], "cpcs": [], "cpms": [],
            }
        s = stats[cid]
        s["spend"] += r["spend"]
        s["impressions"] += r["impressions"]
        s["clicks"] += r["clicks"]
        s["leads"] += r["leads"]
        s["link_clicks"] += r["link_clicks"]
        s["reach"] = max(s["reach"], r["reach"])
        s["video_views"] += r["video_views"]
        s["page_likes"] += r["page_likes"]
        s["purchases"] += r["purchases"]
        s["purchase_value"] += r["purchase_value"]
        if r["ctr"]:  s["ctrs"].append(r["ctr"])
        if r["cpc"]:  s["cpcs"].append(r["cpc"])
        if r["cpm"]:  s["cpms"].append(r["cpm"])
    return stats


def _avg(lst):
    return round(sum(lst) / len(lst), 2) if lst else 0


CAMPAIGN_HEADERS = [
    "Дата", "Кампанія", "Витрати ($)", "Покази", "Кліки по посиланню",
    "Кліки всього", "CTR (%)", "CPC ($)", "CPM ($)",
    "Ліди", "Ціна ліда ($)", "Перегляди відео", "Лайки сторінки",
    "Охоплення", "Покупки", "ROAS",
]

SUMMARY_HEADERS = [
    "Дата оновлення", "Період", "Витрати ($)", "Покази", "Кліки",
    "CTR (%)", "CPC ($)", "Ліди", "Ціна ліда ($)",
    "Відео", "Охоплення", "Покупки", "ROAS",
]

# Traffic daily report headers — matches structure of "Червень" style sheets
TRAFFIC_REPORT_HEADERS = [
    "Дата",
    "Витрати ($)", "Покази", "Охоплення", "Кліки", "CTR (%)", "CPC ($)", "CPM ($)",
    "Ліди", "Ціна ліда ($)", "Покупки", "ROAS",
    "Відео перегляди",
    # Subscriber columns
    "Підписники всього",
    "Instagram", "YouTube", "TikTok", "Facebook", "Telegram (канал)",
    "Telegram (бот)", "LinkedIn", "Threads",
    "Instagram (особистий)", "Facebook (особистий)",
    "Новіх підписників (тиж)",
]


def _build_campaign_rows(stats: dict, today: str) -> list:
    rows = []
    for s in sorted(stats.values(), key=lambda x: x["spend"], reverse=True):
        cpl = round(s["spend"] / s["leads"], 2) if s["leads"] > 0 else 0
        roas = round(s["purchase_value"] / s["spend"], 2) if s["spend"] > 0 else 0
        rows.append([
            today,
            s["campaign_name"],
            round(s["spend"], 2),
            s["impressions"],
            s["link_clicks"],
            s["clicks"],
            _avg(s["ctrs"]),
            _avg(s["cpcs"]),
            _avg(s["cpms"]),
            s["leads"],
            cpl,
            s["video_views"],
            s["page_likes"],
            s["reach"],
            s["purchases"],
            roas,
        ])
    return rows


def _build_traffic_report_row(today: str, all_traffic_stats: dict, subs: dict) -> list:
    """Build a single daily summary row for the traffic report sheet."""
    # Aggregate all traffic campaigns into one daily total
    total_spend = sum(s["spend"] for s in all_traffic_stats.values())
    total_impressions = sum(s["impressions"] for s in all_traffic_stats.values())
    total_reach = max((s["reach"] for s in all_traffic_stats.values()), default=0)
    total_clicks = sum(s["clicks"] for s in all_traffic_stats.values())
    total_leads = sum(s["leads"] for s in all_traffic_stats.values())
    total_purchases = sum(s["purchases"] for s in all_traffic_stats.values())
    total_purchase_value = sum(s["purchase_value"] for s in all_traffic_stats.values())
    total_video = sum(s["video_views"] for s in all_traffic_stats.values())

    all_ctrs = [c for s in all_traffic_stats.values() for c in s["ctrs"]]
    all_cpcs = [c for s in all_traffic_stats.values() for c in s["cpcs"]]
    all_cpms = [c for s in all_traffic_stats.values() for c in s["cpms"]]

    avg_ctr = _avg(all_ctrs)
    avg_cpc = _avg(all_cpcs)
    avg_cpm = _avg(all_cpms)
    cpl = round(total_spend / total_leads, 2) if total_leads > 0 else 0
    roas = round(total_purchase_value / total_spend, 2) if total_spend > 0 else 0

    # Subscriber totals from scraper (keyed by slug)
    def sub_total(slug):
        return subs.get(slug, {}).get("total", "")

    def sub_delta(slug):
        return subs.get(slug, {}).get("weekly_delta", "")

    overall_total = subs.get("_total", {}).get("total", "")
    overall_delta = subs.get("_total", {}).get("weekly_delta", "")

    return [
        today,
        round(total_spend, 2), total_impressions, total_reach, total_clicks,
        avg_ctr, avg_cpc, avg_cpm,
        total_leads, cpl, total_purchases, roas,
        total_video,
        overall_total,
        sub_total("instagram"),
        sub_total("youtube"),
        sub_total("tiktok"),
        sub_total("facebook"),
        sub_total("telegram_channel"),
        sub_total("telegram_bot"),
        sub_total("linkedin"),
        sub_total("threads"),
        sub_total("instagram_personal"),
        sub_total("facebook_personal"),
        overall_delta,
    ]


def _sync_sheet(gc, sheet_id: str, label: str, days: int = 30):
    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as e:
        logger.error(f"Cannot open sheet {label} ({sheet_id[:20]}...): {e}")
        return

    today = date.today().isoformat()
    rows_all = get_metrics_by_period(days=days)
    rows_today = get_metrics_by_period(days=1)

    today_stats = _aggregate_campaigns(rows_today)

    traffic_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) == "traffic"}
    leadgen_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) == "leadgen"}
    other_today = {k: v for k, v in today_stats.items()
                   if _classify_campaign(v["campaign_name"]) == "other"}

    # ── Трафік (авто) — per-campaign rows ───────────────────────────
    ws_traffic = _get_or_create_with_headers(spreadsheet, "Трафік (авто)", CAMPAIGN_HEADERS)
    traffic_rows = _build_campaign_rows({**traffic_today, **other_today}, today)
    if traffic_rows:
        _upsert_rows(ws_traffic, CAMPAIGN_HEADERS, traffic_rows)
        logger.info(f"Sheet '{label}' Трафік: {len(traffic_rows)} рядків за {today}")

    # ── Лідген (авто) — per-campaign rows ───────────────────────────
    ws_leadgen = _get_or_create_with_headers(spreadsheet, "Лідген (авто)", CAMPAIGN_HEADERS)
    leadgen_rows = _build_campaign_rows(leadgen_today, today)
    if leadgen_rows:
        _upsert_rows(ws_leadgen, CAMPAIGN_HEADERS, leadgen_rows)
        logger.info(f"Sheet '{label}' Лідген: {len(leadgen_rows)} рядків за {today}")

    # ── Traffic звіт (авто) — daily totals + subscribers ────────────
    # Fetch subscriber data (suppress SSL warnings for self-signed cert)
    subs = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from reports.subscribers_scraper import scrape_subscribers
            subs = scrape_subscribers()
    except Exception as e:
        logger.warning(f"Subscriber scrape failed (non-critical): {e}")

    ws_report = _get_or_create_with_headers(spreadsheet, "Traffic звіт (авто)", TRAFFIC_REPORT_HEADERS)
    report_row = _build_traffic_report_row(today, {**traffic_today, **other_today}, subs)
    _upsert_rows(ws_report, TRAFFIC_REPORT_HEADERS, [report_row])
    logger.info(f"Sheet '{label}' Traffic звіт: оновлено за {today}")

    # ── Підсумок (авто) ──────────────────────────────────────────────
    ws_summary = _get_or_create_with_headers(spreadsheet, "Підсумок (авто)", SUMMARY_HEADERS)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    period_defs = [(1, "Вчора"), (3, "3 дні"), (7, "7 днів"), (14, "2 тижні"), (30, "Місяць")]
    summary_rows = []
    for d, period_label in period_defs:
        agg = get_aggregated_metrics(days=d)
        summary_rows.append([
            now_str if d == 1 else "",
            period_label,
            round(agg.get("total_spend") or 0, 2),
            agg.get("total_impressions") or 0,
            agg.get("total_clicks") or 0,
            round(agg.get("avg_ctr") or 0, 2),
            round(agg.get("avg_cpc") or 0, 2),
            agg.get("total_leads") or 0,
            round(agg.get("cost_per_lead") or 0, 2),
            agg.get("total_video_views") or 0,
            agg.get("total_reach") or 0,
            agg.get("total_purchases") or 0,
            round(agg.get("roas") or 0, 2),
        ])
    ws_summary.clear()
    ws_summary.update("A1", [SUMMARY_HEADERS] + summary_rows)
    logger.info(f"Sheet '{label}' Підсумок оновлено")


def sync_to_sheets(days: int = 30):
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    if not os.path.exists(creds_file):
        logger.warning(f"Google credentials not found: {creds_file}. Skipping.")
        return

    gc = _get_client()

    sheets_config = []
    for key, val in os.environ.items():
        if key.startswith("GOOGLE_SHEET_ID_") and val:
            label = key.replace("GOOGLE_SHEET_ID_", "").capitalize()
            sheets_config.append((label, val))

    legacy = os.environ.get("GOOGLE_SHEET_ID", "")
    if legacy and not any(v == legacy for _, v in sheets_config):
        sheets_config.append(("Main", legacy))

    if not sheets_config:
        logger.warning("No GOOGLE_SHEET_ID_* set. Skipping Sheets sync.")
        return

    for label, sheet_id in sheets_config:
        logger.info(f"Syncing: {label}")
        _sync_sheet(gc, sheet_id, label, days=days)
