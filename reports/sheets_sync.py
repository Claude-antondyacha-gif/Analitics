"""
Google Sheets sync — fills existing user sheets + creates helper auto-tabs.

For Sfero:
  - Fills "Червень Traffic  - звіт " (existing sheet) — finds today's row by date,
    writes ad metrics + new subscriber count from sfero-social scraper.
  - Creates "Трафік (авто)", "Лідген (авто)", "Підсумок (авто)" auto-tabs.

NEVER deletes or overwrites existing data in user tabs except the matched date row.
"""
import os
import logging
import warnings
import re
from datetime import datetime, date

import gspread
from google.oauth2.service_account import Credentials

from storage.database import get_metrics_by_period, get_aggregated_metrics

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRAFFIC_KEYWORDS = ["traffic", "tof", "трафік", "traffik", "awareness", "reach", "snap"]
LEADGEN_KEYWORDS = ["лид", "lead", "ленд", "land", "quiz", "квиз", "квіз", "форм", "form"]

# UAH exchange rate — override with env var USD_TO_UAH_RATE
def _uah_rate() -> float:
    try:
        return float(os.environ.get("USD_TO_UAH_RATE", "41.5"))
    except ValueError:
        return 41.5


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


# ── Червень Traffic звіт filler ─────────────────────────────────────────────

def _normalize_date_cell(cell: str) -> str:
    """Convert 'DD.MM.YYYY' or 'YYYY-MM-DD' to 'YYYY-MM-DD' for comparison."""
    cell = cell.strip()
    # DD.MM.YYYY or D.M.YYYY
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', cell)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return cell  # already ISO or unknown


def _fill_traffic_report_sheet(spreadsheet, sheet_name: str,
                                traffic_stats: dict, subs: dict):
    """
    Finds today's row in the existing traffic report sheet by date and fills it.
    Columns (1-based):
      A=ДЕНЬ, B=БЮДЖЕТ$, C=БЮДЖЕТ грн, D=ОХОПЛЕННЯ, E=ПОКАЗИ,
      F=ЧАСТОТА, G=CPM$, H=КЛІКИ УСІ, I=ЦІНА КЛІКА,
      J=CTR%, K=НОВИХ ПІДПИСНИКІВ, L=ВАРТІСТЬ ПІДПИСНИКА, M=КОНВЕРСІЯ
    """
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        logger.warning(f"Sheet '{sheet_name}' not found — skipping traffic report fill")
        return

    today_iso = date.today().isoformat()
    # Also build DD.MM.YYYY format to match cells
    today_dt = date.today()
    today_dmY = today_dt.strftime("%-d.%-m.%Y")   # e.g. 30.6.2026
    today_ddmmYYYY = today_dt.strftime("%d.%m.%Y") # e.g. 30.06.2026

    all_values = ws.get_all_values()

    # Find the row index (1-based for Sheets API)
    target_row = None
    for i, row in enumerate(all_values):
        if not row:
            continue
        cell = row[0].strip()
        normalized = _normalize_date_cell(cell)
        if normalized == today_iso or cell in (today_dmY, today_ddmmYYYY, today_iso):
            target_row = i + 1  # 1-based
            break

    if target_row is None:
        # Date not pre-filled — append a new row at the end
        logger.info(f"Date {today_iso} not found in '{sheet_name}' — appending new row")
        # Find last non-empty row
        last_row = len(all_values)
        while last_row > 0 and not any(c.strip() for c in all_values[last_row - 1]):
            last_row -= 1
        target_row = last_row + 1
        # Write the date cell first
        ws.update(f"A{target_row}", [[today_ddmmYYYY]])

    # Aggregate traffic metrics
    total_spend = sum(s["spend"] for s in traffic_stats.values())
    total_impressions = sum(s["impressions"] for s in traffic_stats.values())
    total_reach = max((s["reach"] for s in traffic_stats.values()), default=0)
    total_clicks = sum(s["clicks"] for s in traffic_stats.values())

    all_ctrs = [c for s in traffic_stats.values() for c in s["ctrs"]]
    all_cpcs = [c for s in traffic_stats.values() for c in s["cpcs"]]
    all_cpms = [c for s in traffic_stats.values() for c in s["cpms"]]

    avg_ctr = _avg(all_ctrs)
    avg_cpc = _avg(all_cpcs)
    avg_cpm = _avg(all_cpms)
    frequency = round(total_impressions / total_reach, 2) if total_reach > 0 else 0

    uah = _uah_rate()
    spend_uah = round(total_spend * uah, 2)

    # Subscribers: use weekly delta as "new subscribers this period"
    new_subs = subs.get("_total", {}).get("weekly_delta", "")
    cost_per_sub = round(total_spend / new_subs, 2) if new_subs and total_spend > 0 else ""
    conversion = round(new_subs / total_clicks * 100, 2) if new_subs and total_clicks > 0 else ""

    # Build values for columns B–M (leaving A=date as-is)
    values = [
        round(total_spend, 2),   # B: БЮДЖЕТ $
        spend_uah,               # C: БЮДЖЕТ грн
        total_reach,             # D: ОХОПЛЕННЯ
        total_impressions,       # E: ПОКАЗИ
        frequency,               # F: ЧАСТОТА
        avg_cpm,                 # G: CPM $
        total_clicks,            # H: КЛІКИ УСІ
        avg_cpc,                 # I: ЦІНА КЛІКА
        avg_ctr,                 # J: CTR %
        new_subs,                # K: НОВИХ ПІДПИСНИКІВ
        cost_per_sub,            # L: ВАРТІСТЬ ПІДПИСНИКА
        conversion,              # M: КОНВЕРСІЯ В ПІДПИСКУ
    ]

    # Write to row, starting from column B (col index 2)
    cell_range = f"B{target_row}:M{target_row}"
    ws.update(cell_range, [values])
    logger.info(f"Filled '{sheet_name}' row {target_row} ({today_iso}): spend=${total_spend:.2f}, reach={total_reach}, subs_new={new_subs}")


def _sync_sheet(gc, sheet_id: str, label: str, days: int = 30):
    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except Exception as e:
        logger.error(f"Cannot open sheet {label} ({sheet_id[:20]}...): {e}")
        return

    today = date.today().isoformat()
    rows_today = get_metrics_by_period(days=1)

    today_stats = _aggregate_campaigns(rows_today)

    traffic_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) in ("traffic", "other")}
    leadgen_today = {k: v for k, v in today_stats.items()
                     if _classify_campaign(v["campaign_name"]) == "leadgen"}

    # ── Fetch subscriber data ────────────────────────────────────────
    subs = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from reports.subscribers_scraper import scrape_subscribers
            subs = scrape_subscribers()
    except Exception as e:
        logger.warning(f"Subscriber scrape failed (non-critical): {e}")

    # ── Fill existing "Червень Traffic  - звіт " sheet ───────────────
    # Try both exact name variants (with/without trailing space)
    for sheet_name in ["Червень Traffic  - звіт ", "Червень Traffic  - звіт", "Червень Traffic - звіт"]:
        try:
            ws_check = spreadsheet.worksheet(sheet_name)
            _fill_traffic_report_sheet(spreadsheet, sheet_name, traffic_today, subs)
            break
        except gspread.WorksheetNotFound:
            continue

    # ── Трафік (авто) — per-campaign rows ───────────────────────────
    ws_traffic = _get_or_create_with_headers(spreadsheet, "Трафік (авто)", CAMPAIGN_HEADERS)
    traffic_rows = _build_campaign_rows(traffic_today, today)
    if traffic_rows:
        _upsert_rows(ws_traffic, CAMPAIGN_HEADERS, traffic_rows)
        logger.info(f"Sheet '{label}' Трафік: {len(traffic_rows)} рядків за {today}")

    # ── Лідген (авто) — per-campaign rows ───────────────────────────
    ws_leadgen = _get_or_create_with_headers(spreadsheet, "Лідген (авто)", CAMPAIGN_HEADERS)
    leadgen_rows = _build_campaign_rows(leadgen_today, today)
    if leadgen_rows:
        _upsert_rows(ws_leadgen, CAMPAIGN_HEADERS, leadgen_rows)
        logger.info(f"Sheet '{label}' Лідген: {len(leadgen_rows)} рядків за {today}")

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
