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
from datetime import datetime, date, timedelta
import calendar

import gspread
from google.oauth2.service_account import Credentials

from storage.database import get_metrics_by_period, get_aggregated_metrics, get_metrics_by_date

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TRAFFIC_KEYWORDS = ["traffic", "tof", "трафік", "traffik", "awareness", "reach", "snap"]
LEADGEN_KEYWORDS = ["лид", "lead", "ленд", "land", "quiz", "квиз", "квіз", "форм", "form"]

# Funnel classification for traffic campaigns — order matters (chatbot/bot checked before generic channel keywords)
FUNNEL_DEFS = [
    ("chatbot", ["chatbot", "чатбот", "чат-бот", "bot", "бот"], "telegram_bot", "Трафік на ChatBot"),
    ("youtube", ["youtube", "ютуб", "yt"], "youtube", "Трафік на Ютуб"),
    ("kanal", ["kanal", "канал", "channel"], "telegram_channel", "Трафік на Telegram канал"),
]

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


def _classify_funnel(name: str) -> str | None:
    """Maps a traffic campaign name to a funnel key (matches FUNNEL_DEFS), or None if no match."""
    n = name.lower()
    for funnel_key, keywords, _, _ in FUNNEL_DEFS:
        if any(k in n for k in keywords):
            return funnel_key
    return None


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


FUNNEL_HEADERS = [
    "Дата", "Витрати ($)", "Покази", "Охоплення", "Кліки",
    "CTR (%)", "CPC ($)", "CPM ($)", "Підписники всього",
    "Нових підписників", "Вартість підписника ($)", "Конверсія клік→підписник (%)",
]


MONTHLY_SUMMARY_HEADERS = [
    "Місяць", "Витрати ($)", "Покази", "Охоплення", "Кліки",
    "CTR (%)", "CPC ($)", "CPM ($)", "Підписників приросло",
    "Вартість підписника ($)", "Конверсія клік→підписник (%)",
]


def _build_funnel_row(date_iso: str, stats: dict,
                      channel_total: int | str, prev_total: int | str) -> list:
    """Будує один рядок для воронкового листа."""
    spend = sum(s["spend"] for s in stats.values())
    impressions = sum(s["impressions"] for s in stats.values())
    reach = max((s["reach"] for s in stats.values()), default=0)
    clicks = sum(s["clicks"] for s in stats.values())
    all_ctrs = [c for s in stats.values() for c in s["ctrs"]]
    all_cpcs = [c for s in stats.values() for c in s["cpcs"]]
    all_cpms = [c for s in stats.values() for c in s["cpms"]]

    new_subs = ""
    if isinstance(channel_total, int) and isinstance(prev_total, int) and channel_total >= prev_total:
        new_subs = channel_total - prev_total

    cost_per_sub = round(spend / new_subs, 2) if new_subs and spend > 0 else ""
    conversion = round(new_subs / clicks * 100, 2) if new_subs and clicks > 0 else ""

    return [
        date_iso,
        round(spend, 2),
        impressions,
        reach,
        clicks,
        _avg(all_ctrs),
        _avg(all_cpcs),
        _avg(all_cpms),
        channel_total,
        new_subs,
        cost_per_sub,
        conversion,
    ]


def _build_monthly_summary_rows(data_rows: list) -> list:
    """
    Приймає список рядків (без заголовку), повертає рядки місячної зведки.
    data_rows: кожен рядок відповідає FUNNEL_HEADERS.
    """
    monthly = {}
    for row in data_rows:
        if not row or len(row) < 10:
            continue
        d = row[0]  # YYYY-MM-DD
        if len(d) < 7:
            continue
        month_key = d[:7]  # YYYY-MM
        if month_key not in monthly:
            monthly[month_key] = {
                "spend": 0, "impressions": 0, "reach": 0, "clicks": 0,
                "ctrs": [], "cpcs": [], "cpms": [],
                "new_subs": 0,
            }
        m = monthly[month_key]
        try: m["spend"] += float(row[1]) if row[1] != "" else 0
        except: pass
        try: m["impressions"] += int(row[2]) if row[2] != "" else 0
        except: pass
        try: m["reach"] = max(m["reach"], int(row[3])) if row[3] != "" else m["reach"]
        except: pass
        try: m["clicks"] += int(row[4]) if row[4] != "" else 0
        except: pass
        try:
            if row[5] != "": m["ctrs"].append(float(row[5]))
        except: pass
        try:
            if row[6] != "": m["cpcs"].append(float(row[6]))
        except: pass
        try:
            if row[7] != "": m["cpms"].append(float(row[7]))
        except: pass
        try: m["new_subs"] += int(row[9]) if row[9] not in ("", None) else 0
        except: pass

    result = []
    for month_key in sorted(monthly):
        y, mo = month_key.split("-")
        month_label = f"{calendar.month_name[int(mo)]} {y}"
        m = monthly[month_key]
        spend = round(m["spend"], 2)
        new_subs = m["new_subs"]
        clicks = m["clicks"]
        cost_per_sub = round(spend / new_subs, 2) if new_subs and spend > 0 else ""
        conversion = round(new_subs / clicks * 100, 2) if new_subs and clicks > 0 else ""
        result.append([
            f"📊 {month_label}",
            spend,
            m["impressions"],
            m["reach"],
            clicks,
            _avg(m["ctrs"]),
            _avg(m["cpcs"]),
            _avg(m["cpms"]),
            new_subs,
            cost_per_sub,
            conversion,
        ])
    return result


def _backfill_funnel_sheet(spreadsheet, sheet_title: str,
                            funnel_key: str, channel_slug: str,
                            all_campaign_rows: list,
                            channel_history: dict[str, int],
                            start_date: date):
    """
    Заповнює воронковий лист рядками від start_date до вчора (включно).
    all_campaign_rows — всі рядки з БД за весь потрібний період (не тільки сьогодні).
    channel_history — {date_iso: total} зі sfero-social.
    Вже існуючі рядки (за датою) не перезаписуються.
    В кінці додає місячну зведку.
    """
    ws = _get_or_create_with_headers(spreadsheet, sheet_title, FUNNEL_HEADERS)
    existing = ws.get_all_values()
    existing_dates = {r[0] for r in existing[1:] if r and r[0]}

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Групуємо всі метрики по даті та воронці
    rows_by_date: dict[str, dict] = {}
    for r in all_campaign_rows:
        funnel = _classify_funnel(r["campaign_name"])
        if funnel != funnel_key:
            continue
        d = r["date"]
        if d not in rows_by_date:
            rows_by_date[d] = {}
        cid = r["campaign_id"]
        if cid not in rows_by_date[d]:
            rows_by_date[d][cid] = {
                "campaign_id": cid,
                "campaign_name": r["campaign_name"],
                "spend": 0, "impressions": 0, "clicks": 0,
                "leads": 0, "link_clicks": 0, "reach": 0,
                "video_views": 0, "page_likes": 0, "purchases": 0,
                "purchase_value": 0, "ctrs": [], "cpcs": [], "cpms": [],
            }
        s = rows_by_date[d][cid]
        s["spend"] += r["spend"]
        s["impressions"] += r["impressions"]
        s["clicks"] += r["clicks"]
        s["leads"] += r["leads"]
        s["link_clicks"] += r["link_clicks"]
        s["reach"] = max(s["reach"], r["reach"])
        if r["ctr"]: s["ctrs"].append(r["ctr"])
        if r["cpc"]: s["cpcs"].append(r["cpc"])
        if r["cpm"]: s["cpms"].append(r["cpm"])

    new_rows = []
    sorted_dates = sorted(rows_by_date.keys())
    for date_iso in sorted_dates:
        dt = date.fromisoformat(date_iso)
        if dt < start_date or dt > yesterday:
            continue
        if date_iso in existing_dates:
            continue  # вже є — не перезаписуємо

        stats = rows_by_date[date_iso]
        channel_total = channel_history.get(date_iso, "")

        # prev_total — попередній день з channel_history
        prev_dt = dt - timedelta(days=1)
        prev_total = channel_history.get(prev_dt.isoformat(), "")

        row = _build_funnel_row(date_iso, stats, channel_total, prev_total)
        new_rows.append(row)

    if not new_rows:
        logger.info(f"Бекфіл {sheet_title}: нових рядків немає")
        return

    # Додаємо нові рядки (без очищення існуючих)
    all_data_rows = [r for r in existing[1:] if r and r[0] and r[0] != ""]
    all_data_rows += new_rows
    all_data_rows.sort(key=lambda r: r[0])

    # Місячна зведка
    monthly_rows = _build_monthly_summary_rows(all_data_rows)

    ws.clear()
    ws.update("A1", [FUNNEL_HEADERS] + all_data_rows + [[]] + [MONTHLY_SUMMARY_HEADERS] + monthly_rows)
    logger.info(f"Бекфіл {sheet_title}: додано {len(new_rows)} рядків, місяців: {len(monthly_rows)}")


def _write_funnel_today(ws, sheet_title: str, stats: dict,
                        channel_total: int | str, existing: list):
    """Оновлює або додає рядок за сьогодні у вже відкритому worksheet."""
    today = date.today().isoformat()

    # Знаходимо попередній total з останнього рядка даних (не зведка, не порожній)
    data_rows = [r for r in existing[1:] if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0])]
    prev_rows = [r for r in data_rows if r[0] < today]

    prev_total = ""
    if prev_rows:
        try:
            prev_total = int(prev_rows[-1][8])
        except (ValueError, IndexError):
            prev_total = ""

    new_row = _build_funnel_row(today, stats, channel_total, prev_total)

    # Всі рядки без сьогодні + сьогодні + порожній + зведка
    other_rows = [r for r in data_rows if r[0] != today]
    all_data = sorted(other_rows + [new_row], key=lambda r: r[0])
    monthly_rows = _build_monthly_summary_rows(all_data)

    ws.clear()
    ws.update("A1", [FUNNEL_HEADERS] + all_data + [[]] + [MONTHLY_SUMMARY_HEADERS] + monthly_rows)


def _sync_funnel_sheets(spreadsheet, label: str, traffic_today: dict, subs: dict,
                        backfill_from: date | None = None):
    """
    Splits traffic campaigns by funnel:
    - Якщо backfill_from задано — завантажує всі дані з БД з тієї дати і заповнює пропуски
    - Щодня оновлює рядок за сьогодні
    - В кінці таблиці — місячна зведка
    """
    from reports.subscribers_scraper import scrape_channel_history

    by_funnel = {key: {} for key, _, _, _ in FUNNEL_DEFS}
    for cid, s in traffic_today.items():
        fk = _classify_funnel(s["campaign_name"])
        if fk:
            by_funnel[fk][cid] = s

    # Якщо бекфіл потрібен — завантажуємо всю історію з БД
    if backfill_from:
        days_back = (date.today() - backfill_from).days + 1
        all_rows = get_metrics_by_period(days=days_back)
    else:
        all_rows = []

    for funnel_key, keywords, channel_slug, sheet_title in FUNNEL_DEFS:
        stats_today = by_funnel[funnel_key]

        # Отримуємо щоденну історію підписників для цього каналу
        channel_history = {}
        try:
            channel_history = scrape_channel_history(channel_slug)
        except Exception as e:
            logger.warning(f"Channel history {channel_slug}: {e}")

        channel_total_today = channel_history.get(date.today().isoformat(),
                              subs.get(channel_slug, {}).get("total", ""))

        ws = _get_or_create_with_headers(spreadsheet, sheet_title, FUNNEL_HEADERS)
        existing = ws.get_all_values()

        # Спочатку бекфіл (якщо задано)
        if backfill_from and all_rows:
            _backfill_funnel_sheet(spreadsheet, sheet_title, funnel_key, channel_slug,
                                   all_rows, channel_history, backfill_from)
            existing = ws.get_all_values()  # перечитуємо після бекфілу

        # Потім рядок за сьогодні (тільки якщо є кампанії)
        if stats_today:
            _write_funnel_today(ws, sheet_title, stats_today, channel_total_today, existing)
            logger.info(f"Sheet '{label}' {sheet_title}: оновлено рядок {date.today().isoformat()}")

    # Combined / підсумок по всьому трафіку
    all_traffic = {cid: s for fk in by_funnel for cid, s in by_funnel[fk].items()}
    if all_traffic:
        total_history = {}
        try:
            # Сума total по всіх воронкових каналах на кожну дату
            for _, _, channel_slug, _ in FUNNEL_DEFS:
                ch = scrape_channel_history(channel_slug)
                for d, v in ch.items():
                    total_history[d] = total_history.get(d, 0) + v
        except Exception:
            pass

        total_today = total_history.get(date.today().isoformat(),
                      subs.get("_total", {}).get("total", ""))

        ws_sum = _get_or_create_with_headers(spreadsheet, "Трафік - підсумок", FUNNEL_HEADERS)
        existing_sum = ws_sum.get_all_values()

        if backfill_from and all_rows:
            # Бекфіл підсумку: всі воронки разом
            # Будуємо псевдо funnel_key "all" — перевизначаємо _classify_funnel тимчасово
            _backfill_all_traffic(spreadsheet, "Трафік - підсумок",
                                  all_rows, total_history, backfill_from)
            existing_sum = ws_sum.get_all_values()

        _write_funnel_today(ws_sum, "Трафік - підсумок", all_traffic, total_today, existing_sum)
        logger.info(f"Sheet '{label}' Трафік - підсумок оновлено")


def _backfill_all_traffic(spreadsheet, sheet_title: str,
                           all_rows: list, channel_history: dict[str, int],
                           start_date: date):
    """Бекфіл для зведеного листа по всіх воронках разом."""
    ws = _get_or_create_with_headers(spreadsheet, sheet_title, FUNNEL_HEADERS)
    existing = ws.get_all_values()
    existing_dates = {r[0] for r in existing[1:] if r and r[0]}

    today = date.today()
    yesterday = today - timedelta(days=1)

    rows_by_date: dict[str, dict] = {}
    for r in all_rows:
        if _classify_campaign(r["campaign_name"]) == "leadgen":
            continue  # лідген не включаємо
        d = r["date"]
        if d not in rows_by_date:
            rows_by_date[d] = {}
        cid = r["campaign_id"]
        if cid not in rows_by_date[d]:
            rows_by_date[d][cid] = {
                "campaign_id": cid, "campaign_name": r["campaign_name"],
                "spend": 0, "impressions": 0, "clicks": 0,
                "leads": 0, "link_clicks": 0, "reach": 0,
                "video_views": 0, "page_likes": 0, "purchases": 0,
                "purchase_value": 0, "ctrs": [], "cpcs": [], "cpms": [],
            }
        s = rows_by_date[d][cid]
        s["spend"] += r["spend"]; s["impressions"] += r["impressions"]
        s["clicks"] += r["clicks"]; s["reach"] = max(s["reach"], r["reach"])
        if r["ctr"]: s["ctrs"].append(r["ctr"])
        if r["cpc"]: s["cpcs"].append(r["cpc"])
        if r["cpm"]: s["cpms"].append(r["cpm"])

    new_rows = []
    for date_iso in sorted(rows_by_date):
        dt = date.fromisoformat(date_iso)
        if dt < start_date or dt > yesterday or date_iso in existing_dates:
            continue
        stats = rows_by_date[date_iso]
        channel_total = channel_history.get(date_iso, "")
        prev_total = channel_history.get((dt - timedelta(days=1)).isoformat(), "")
        new_rows.append(_build_funnel_row(date_iso, stats, channel_total, prev_total))

    if not new_rows:
        return

    all_data = [r for r in existing[1:] if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0])]
    all_data = sorted(all_data + new_rows, key=lambda r: r[0])
    monthly_rows = _build_monthly_summary_rows(all_data)

    ws.clear()
    ws.update("A1", [FUNNEL_HEADERS] + all_data + [[]] + [MONTHLY_SUMMARY_HEADERS] + monthly_rows)
    logger.info(f"Бекфіл {sheet_title}: {len(new_rows)} нових рядків")


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

    # ── Per-funnel tabs (Трафік на Ютуб / Telegram канал / ChatBot) ──
    # Бекфіл від 16.06 (старт кампаній) — заповнює пропуски у вже існуючих листах
    campaign_start = date(2026, 6, 16)
    _sync_funnel_sheets(spreadsheet, label, traffic_today, subs, backfill_from=campaign_start)

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
