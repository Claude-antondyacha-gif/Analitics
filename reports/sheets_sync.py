"""
Google Sheets sync — writes daily metrics and AI recommendations to Google Sheets.
Supports multiple spreadsheets via GOOGLE_SHEET_ID_SFERO, GOOGLE_SHEET_ID_ZEEKR, etc.
"""
import os
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from storage.database import get_metrics_by_period, get_aggregated_metrics, get_latest_recommendations

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

METRICS_HEADERS = [
    "Дата", "Кампанія", "Група оголошень", "Витрати ($)", "Охоплення",
    "Покази", "Кліки", "CTR (%)", "CPC ($)", "CPM ($)",
    "Ліди", "Вартість ліда ($)", "Покупки", "ROAS",
    "Переходи за посиланням", "Вподобання сторінки", "Перегляди відео", "Частота",
]

SUMMARY_HEADERS = [
    "Період", "Витрати ($)", "Ліди", "CPL ($)", "Покупки", "ROAS",
    "CTR (%)", "CPC ($)", "Кліки", "Охоплення", "Покази",
]


def _get_client():
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_worksheet(spreadsheet, title: str, rows: int = 2000, cols: int = 25):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _format_row(r: dict) -> list:
    return [
        r.get("date", ""),
        r.get("campaign_name", ""),
        r.get("adset_name", ""),
        round(r.get("spend", 0), 2),
        r.get("reach", 0),
        r.get("impressions", 0),
        r.get("clicks", 0),
        round(r.get("ctr", 0), 2),
        round(r.get("cpc", 0), 2),
        round(r.get("cpm", 0), 2),
        r.get("leads", 0),
        round(r.get("cost_per_lead", 0), 2),
        r.get("purchases", 0),
        round(r.get("purchase_roas", 0), 2),
        r.get("link_clicks", 0),
        r.get("page_likes", 0),
        r.get("video_views", 0),
        round(r.get("frequency", 0), 2),
    ]


def _sync_one_sheet(gc, sheet_id: str, label: str, days: int = 30, campaign_id: str = None):
    """Sync data to a single spreadsheet."""
    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as e:
        logger.error(f"Cannot open sheet {label} ({sheet_id}): {e}")
        return

    rows = get_metrics_by_period(days=days, campaign_id=campaign_id)

    # ── Детальні дані ──
    ws_raw = _ensure_worksheet(spreadsheet, "Детальні дані")
    data = [METRICS_HEADERS] + [_format_row(r) for r in rows]
    ws_raw.clear()
    ws_raw.update("A1", data)
    try:
        ws_raw.format("A1:R1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.18, "green": 0.27, "blue": 0.6},
        })
    except Exception:
        pass

    # ── Підсумок по периодах ──
    ws_summary = _ensure_worksheet(spreadsheet, "Підсумок")
    period_defs = [(1, "Вчора"), (3, "3 дні"), (7, "7 днів"), (14, "2 тижні"), (30, "Місяць")]
    summary_rows = [SUMMARY_HEADERS]
    for d, period_label in period_defs:
        agg = get_aggregated_metrics(days=d, campaign_id=campaign_id)
        summary_rows.append([
            period_label,
            round(agg.get("total_spend") or 0, 2),
            agg.get("total_leads") or 0,
            round(agg.get("cost_per_lead") or 0, 2),
            agg.get("total_purchases") or 0,
            round(agg.get("roas") or 0, 2),
            round(agg.get("avg_ctr") or 0, 2),
            round(agg.get("avg_cpc") or 0, 2),
            agg.get("total_clicks") or 0,
            agg.get("total_reach") or 0,
            agg.get("total_impressions") or 0,
        ])
    ws_summary.clear()
    ws_summary.update("A1", summary_rows)
    try:
        ws_summary.format("A1:K1", {"textFormat": {"bold": True}})
    except Exception:
        pass

    # ── AI Рекомендації ──
    ws_ai = _ensure_worksheet(spreadsheet, "AI Рекомендації")
    recs = get_latest_recommendations(limit=20)
    ai_rows = [["Дата", "Період", "Огляд", "Рекомендації", "Критичні сповіщення"]]
    for rec in recs:
        ai_rows.append([
            rec["created_at"][:16].replace("T", " "),
            rec["period"],
            rec["summary"],
            " | ".join(r.get("text", "") for r in rec.get("recommendations", [])),
            " | ".join(a.get("message", "") for a in rec.get("critical_alerts", [])),
        ])
    ws_ai.clear()
    ws_ai.update("A1", ai_rows)
    try:
        ws_ai.format("A1:E1", {"textFormat": {"bold": True}})
    except Exception:
        pass

    logger.info(f"Sheet '{label}' synced: {len(rows)} rows, {len(recs)} recommendations")


def sync_to_sheets(days: int = 30):
    """Sync all configured spreadsheets."""
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    if not os.path.exists(creds_file):
        logger.warning(f"Google credentials file not found: {creds_file}. Skipping Sheets sync.")
        return

    gc = _get_client()

    # Find all configured sheet IDs
    sheets_config = []
    for key, val in os.environ.items():
        if key.startswith("GOOGLE_SHEET_ID_") and val:
            label = key.replace("GOOGLE_SHEET_ID_", "").capitalize()
            sheets_config.append((label, val))

    # Also check legacy GOOGLE_SHEET_ID
    legacy_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if legacy_id and not any(v == legacy_id for _, v in sheets_config):
        sheets_config.append(("Main", legacy_id))

    if not sheets_config:
        logger.warning("No GOOGLE_SHEET_ID_* environment variables set. Skipping.")
        return

    for label, sheet_id in sheets_config:
        logger.info(f"Syncing sheet: {label} ({sheet_id[:20]}...)")
        _sync_one_sheet(gc, sheet_id, label, days=days)
