"""
Google Sheets sync — writes daily metrics and AI recommendations to a Google Sheet.
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
    "Ліди", "Вартість ліда ($)", "Покупки", "Вартість покупки ($)",
    "ROAS", "Переходи за посиланням", "Вподобання сторінки",
    "Перегляди відео", "Частота",
]

SUMMARY_HEADERS = [
    "Період", "Витрати", "Ліди", "CPL", "Покупки", "CPP", "ROAS",
    "CTR", "CPC", "Кліки", "Охоплення",
]


def _get_client():
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_worksheet(spreadsheet, title: str, rows: int = 1000, cols: int = 25):
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
        round(r.get("cost_per_lead", 0), 2),  # reuse field if no cost_per_purchase stored
        round(r.get("purchase_roas", 0), 2),
        r.get("link_clicks", 0),
        r.get("page_likes", 0),
        r.get("video_views", 0),
        round(r.get("frequency", 0), 2),
    ]


def sync_to_sheets(days: int = 30):
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        logger.warning("GOOGLE_SHEET_ID not set, skipping Sheets sync")
        return

    gc = _get_client()
    spreadsheet = gc.open_by_key(sheet_id)

    # --- Raw metrics tab ---
    ws_raw = _ensure_worksheet(spreadsheet, "Детальні дані")
    rows = get_metrics_by_period(days=days)
    data = [METRICS_HEADERS] + [_format_row(r) for r in rows]
    ws_raw.clear()
    ws_raw.update("A1", data)
    ws_raw.format("A1:S1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.8}})

    # --- Summary tab ---
    ws_summary = _ensure_worksheet(spreadsheet, "Підсумок")
    periods = [(1, "Вчора"), (3, "3 дні"), (7, "7 днів"), (14, "2 тижні"), (30, "Місяць")]
    summary_rows = [SUMMARY_HEADERS]
    for d, label in periods:
        agg = get_aggregated_metrics(days=d)
        summary_rows.append([
            label,
            f"${agg.get('total_spend', 0):.2f}",
            agg.get("total_leads", 0),
            f"${agg.get('cost_per_lead', 0):.2f}",
            agg.get("total_purchases", 0),
            f"${agg.get('cost_per_purchase', 0):.2f}",
            agg.get("roas", 0),
            f"{agg.get('avg_ctr', 0):.2f}%",
            f"${agg.get('avg_cpc', 0):.2f}",
            agg.get("total_clicks", 0),
            agg.get("total_reach", 0),
        ])
    ws_summary.clear()
    ws_summary.update("A1", summary_rows)
    ws_summary.format("A1:K1", {"textFormat": {"bold": True}})

    # --- AI Recommendations tab ---
    ws_ai = _ensure_worksheet(spreadsheet, "AI Рекомендації")
    recs = get_latest_recommendations(limit=20)
    ai_rows = [["Дата", "Період", "Тип", "Огляд", "Рекомендації", "Критичні сповіщення"]]
    for rec in recs:
        ai_rows.append([
            rec["created_at"][:16],
            rec["period"],
            rec["analysis_type"],
            rec["summary"],
            " | ".join(r.get("text", "") for r in rec["recommendations"]),
            " | ".join(a.get("message", "") for a in rec["critical_alerts"]),
        ])
    ws_ai.clear()
    ws_ai.update("A1", ai_rows)
    ws_ai.format("A1:F1", {"textFormat": {"bold": True}})

    logger.info(f"Google Sheets synced: {len(rows)} metric rows, {len(recs)} recommendations")
