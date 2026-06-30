"""
Google Sheets sync — appends daily metrics to existing spreadsheets.
Creates "Трафік" and "Лідген" tabs; never modifies existing tabs.
"""
import os
import logging
from datetime import datetime, date

import gspread
from google.oauth2.service_account import Credentials

from storage.database import get_metrics_by_period, get_aggregated_metrics, get_latest_recommendations

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Keywords to classify campaign type
TRAFFIC_KEYWORDS = ["traffic", "tof", "трафік", "traffik", "awareness", "reach"]
LEADGEN_KEYWORDS = ["лид", "lead", "ленд", "land", "quiz", "квиз", "квіз", "форм", "form"]


def _get_client():
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def _classify_campaign(name: str) -> str:
    """Returns 'traffic', 'leadgen', or 'other'."""
    n = name.lower()
    if any(k in n for k in TRAFFIC_KEYWORDS):
        return "traffic"
    if any(k in n for k in LEADGEN_KEYWORDS):
        return "leadgen"
    return "other"


def _ensure_worksheet(spreadsheet, title: str, rows: int = 2000, cols: int = 20):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
        return ws


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


def _aggregate_campaigns(rows: list) -> dict:
    """Aggregate metrics per campaign from row list."""
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
        if r["ctr"]: s["ctrs"].append(r["ctr"])
        if r["cpc"]: s["cpcs"].append(r["cpc"])
        if r["cpm"]: s["cpms"].append(r["cpm"])
    return stats


def _build_campaign_rows(stats: dict, today: str) -> list:
    rows = []
    for s in sorted(stats.values(), key=lambda x: x["spend"], reverse=True):
        avg = lambda lst: round(sum(lst)/len(lst), 2) if lst else 0
        cpl = round(s["spend"] / s["leads"], 2) if s["leads"] > 0 else 0
        roas = round(s["purchase_value"] / s["spend"], 2) if s["spend"] > 0 else 0
        rows.append([
            today,
            s["campaign_name"],
            round(s["spend"], 2),
            s["impressions"],
            s["link_clicks"],
            s["clicks"],
            avg(s["ctrs"]),
            avg(s["cpcs"]),
            avg(s["cpms"]),
            s["leads"],
            cpl,
            s["video_views"],
            s["page_likes"],
            s["reach"],
            s["purchases"],
            roas,
        ])
    return rows


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


def _sync_sheet(gc, sheet_id: str, label: str, days: int = 30):
    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as e:
        logger.error(f"Cannot open sheet {label} ({sheet_id[:20]}...): {e}")
        return

    today = date.today().isoformat()
    rows_all = get_metrics_by_period(days=days)
    rows_today = get_metrics_by_period(days=1)

    # Classify campaigns
    today_stats = _aggregate_campaigns(rows_today)
    all_stats = _aggregate_campaigns(rows_all)

    traffic_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) == "traffic"}
    leadgen_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) == "leadgen"}
    other_today = {k: v for k, v in today_stats.items()
                   if _classify_campaign(v["campaign_name"]) == "other"}

    # ── Трафік tab ──────────────────────────────────────────────────
    ws_traffic = _get_or_create_with_headers(spreadsheet, "Трафік (авто)", CAMPAIGN_HEADERS)
    traffic_rows = _build_campaign_rows({**traffic_today, **other_today}, today)
    if traffic_rows:
        # Remove today's rows if already exist, then append fresh
        all_existing = ws_traffic.get_all_values()
        filtered = [r for r in all_existing if r and r[0] != today]
        ws_traffic.clear()
        ws_traffic.update("A1", [CAMPAIGN_HEADERS] + filtered[1:] + traffic_rows)
        logger.info(f"Sheet '{label}' Трафік: {len(traffic_rows)} рядків за {today}")

    # ── Лідген tab ──────────────────────────────────────────────────
    ws_leadgen = _get_or_create_with_headers(spreadsheet, "Лідген (авто)", CAMPAIGN_HEADERS)
    leadgen_rows = _build_campaign_rows(leadgen_today, today)
    if leadgen_rows:
        all_existing = ws_leadgen.get_all_values()
        filtered = [r for r in all_existing if r and r[0] != today]
        ws_leadgen.clear()
        ws_leadgen.update("A1", [CAMPAIGN_HEADERS] + filtered[1:] + leadgen_rows)
        logger.info(f"Sheet '{label}' Лідген: {len(leadgen_rows)} рядків за {today}")

    # ── Підсумок tab ────────────────────────────────────────────────
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
