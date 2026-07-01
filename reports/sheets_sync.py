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
    "Дата", "Витрати (€)", "Покази", "Охоплення", "Кліки",
    "CTR (%)", "CPC (€)", "CPM (€)", "Нових підписників",
    "Вартість підписника (€)", "Конверсія клік→підписник", "Підписники всього",
]

# Індекси колонок у FUNNEL_HEADERS (для зручності читання)
_F_DATE, _F_SPEND, _F_IMP, _F_REACH, _F_CLICKS = 0, 1, 2, 3, 4
_F_CTR, _F_CPC, _F_CPM, _F_NEW_SUBS, _F_CPP, _F_CONV, _F_TOTAL_SUBS = 5, 6, 7, 8, 9, 10, 11


def _build_funnel_row(date_iso: str, stats: dict,
                      channel_total: int | str, prev_total: int | str) -> list:
    """Будує один рядок для воронкового листа."""
    spend = sum(s["spend"] for s in stats.values())
    impressions = sum(s["impressions"] for s in stats.values())
    reach = sum(s["reach"] for s in stats.values())
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
        date_iso,           # A: Дата
        round(spend, 2),    # B: Витрати (€)
        impressions,        # C: Покази
        reach,              # D: Охоплення
        clicks,             # E: Кліки
        _avg(all_ctrs),     # F: CTR (%)
        _avg(all_cpcs),     # G: CPC (€)
        _avg(all_cpms),     # H: CPM (€)
        new_subs,           # I: Нових підписників
        cost_per_sub,       # J: Вартість підписника (€)
        conversion,         # K: Конверсія клік→підписник
        channel_total,      # L: Підписники всього
    ]


def _build_monthly_summary_rows(data_rows: list) -> list:
    """
    Повертає рядки місячної зведки у форматі FUNNEL_HEADERS.
    Колонка A: "За місяць" (або назва місяця якщо місяців > 1).
    """
    monthly = {}
    for row in data_rows:
        if not row or len(row) < 8:
            continue
        d = str(row[_F_DATE])
        if not re.match(r'\d{4}-\d{2}', d):
            continue
        month_key = d[:7]
        if month_key not in monthly:
            monthly[month_key] = {
                "spend": 0, "impressions": 0, "reach": 0, "clicks": 0,
                "ctrs": [], "cpcs": [], "cpms": [], "new_subs": 0,
            }
        m = monthly[month_key]
        def _safe_add(m, key, val, cast=float):
            try: m[key] += cast(val) if val not in ("", None) else 0
            except: pass
        def _safe_max(m, key, val, cast=int):
            try: m[key] = max(m[key], cast(val)) if val not in ("", None) else m[key]
            except: pass
        def _safe_list(lst, val, cast=float):
            try:
                if val not in ("", None): lst.append(cast(val))
            except: pass
        _safe_add(m, "spend", row[_F_SPEND])
        _safe_add(m, "impressions", row[_F_IMP], int)
        _safe_max(m, "reach", row[_F_REACH])
        _safe_add(m, "clicks", row[_F_CLICKS], int)
        _safe_list(m["ctrs"], row[_F_CTR])
        _safe_list(m["cpcs"], row[_F_CPC])
        _safe_list(m["cpms"], row[_F_CPM])
        _safe_add(m, "new_subs", row[_F_NEW_SUBS], int)

    result = []
    months = sorted(monthly)
    for month_key in months:
        m = monthly[month_key]
        # Якщо один місяць — підпис "За місяць", інакше — назва місяця
        if len(months) == 1:
            label = "За місяць"
        else:
            y, mo = month_key.split("-")
            ua_months = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень",
                         "Червень", "Липень", "Серпень", "Вересень", "Жовтень",
                         "Листопад", "Грудень"]
            label = f"{ua_months[int(mo)]} {y}"

        spend = round(m["spend"], 2)
        new_subs = m["new_subs"]
        clicks = m["clicks"]
        cost_per_sub = round(spend / new_subs, 2) if new_subs and spend > 0 else ""
        conversion = round(new_subs / clicks * 100, 2) if new_subs and clicks > 0 else ""
        result.append([
            label,          # A: За місяць / назва місяця
            spend,          # B: Витрати (€)
            m["impressions"],# C: Покази
            m["reach"],     # D: Охоплення
            clicks,         # E: Кліки
            _avg(m["ctrs"]),# F: CTR (%)
            _avg(m["cpcs"]),# G: CPC (€)
            _avg(m["cpms"]),# H: CPM (€)
            new_subs,       # I: Нових підписників
            cost_per_sub,   # J: Вартість підписника (€)
            conversion,     # K: Конверсія клік→підписник
            "",             # L: Підписники всього (не агрегується)
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

    # Пишемо тільки нові рядки — кожен окремо в потрібний рядок аркуша
    # Не чіпаємо існуючі рядки (включно з ручними правками та формулами "За місяць")
    existing_dates = {r[0]: i + 1 for i, r in enumerate(existing) if r and r[0]}
    # Знаходимо останній рядок з датою щоб знати куди вставляти нові
    last_date_sheet_row = 1
    for i, r in enumerate(existing):
        if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0]):
            last_date_sheet_row = i + 1

    appended = 0
    for row in sorted(new_rows, key=lambda r: r[0]):
        date_val = row[0]
        if date_val in existing_dates:
            # Рядок з цією датою вже є — не чіпаємо
            continue
        last_date_sheet_row += 1
        ws.update(f"A{last_date_sheet_row}", [row])
        existing_dates[date_val] = last_date_sheet_row
        appended += 1

    logger.info(f"Бекфіл {sheet_title}: вставлено {appended} нових рядків (існуючі не змінено)")


def _write_funnel_today(ws, sheet_title: str, stats: dict,
                        channel_total: int | str, existing: list,
                        target_date: date | None = None):
    """
    Оновлює або додає рядок за target_date (default: вчора) — без ws.clear().
    Знаходить рядок за датою і пише тільки в нього.
    "За місяць" рядок (з формулами) не чіпаємо.
    """
    today = (target_date or date.today() - timedelta(days=1)).isoformat()

    # Знаходимо попередній total з останнього рядка з датою (col L = індекс 11)
    data_rows = [r for r in existing[1:] if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0])]
    prev_rows = [r for r in data_rows if r[0] < today]
    prev_total = ""
    if prev_rows:
        try:
            prev_total = int(prev_rows[-1][_F_TOTAL_SUBS])
        except (ValueError, IndexError):
            prev_total = ""

    new_row = _build_funnel_row(today, stats, channel_total, prev_total)

    # Шукаємо рядок з сьогоднішньою датою
    target_row_idx = None
    for i, r in enumerate(existing):
        if r and r[0] == today:
            target_row_idx = i + 1  # 1-based для Sheets API
            break

    if target_row_idx:
        # Рядок є — оновлюємо тільки його
        ws.update(f"A{target_row_idx}", [new_row])
    else:
        # Знаходимо останній рядок з датою і вставляємо після нього
        last_date_row = 1
        for i, r in enumerate(existing):
            if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0]):
                last_date_row = i + 1
        ws.update(f"A{last_date_row + 1}", [new_row])


def _prepare_month_skeleton(ws, year: int, month: int):
    """
    Додає кістяк місяця під існуючі дані:
    - рядки з датами (col A) для кожного дня місяця
    - рядок "За місяць" з формулами SUM/розрахунок внизу
    Структура відповідає скріншоту: дані → За місяць.
    Не чіпає існуючі рядки.
    """
    num_days = calendar.monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}"
    month_dates = [date(year, month, d).isoformat() for d in range(1, num_days + 1)]

    existing = ws.get_all_values()
    existing_dates = {r[0] for r in existing if r and r[0]}

    # Перевіряємо чи кістяк цього місяця вже є
    month_dates_present = [d for d in month_dates if d in existing_dates]
    if len(month_dates_present) == num_days:
        return  # вже повний кістяк — нічого не робимо

    # Знаходимо останній рядок з датою в таблиці
    last_data_row = 1
    for i, r in enumerate(existing):
        if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0]):
            last_data_row = i + 1

    # Вставляємо дати поточного місяця яких ще немає
    first_new_row = last_data_row + 1
    for d_iso in month_dates:
        if d_iso in existing_dates:
            continue
        ws.update(f"A{first_new_row}", [[d_iso]])
        existing_dates.add(d_iso)
        first_new_row += 1

    # Рядок "За місяць" з формулами — одразу після останньої дати місяця
    # Знаходимо sheet-рядки де дати цього місяця
    existing = ws.get_all_values()
    month_sheet_rows = [
        i + 1 for i, r in enumerate(existing)
        if r and r[0] and r[0].startswith(month_prefix)
    ]

    if not month_sheet_rows:
        return

    r_first = month_sheet_rows[0]   # перший рядок місяця
    r_last  = month_sheet_rows[-1]  # останній рядок місяця
    r_summary = r_last + 1

    # Перевіряємо чи рядок "За місяць" вже є
    if r_summary - 1 < len(existing) and existing[r_summary - 1]:
        existing_val = existing[r_summary - 1][0] if existing[r_summary - 1] else ""
        if existing_val == "За місяць":
            return  # вже є

    # Формули — відповідають структурі FUNNEL_HEADERS
    # B=spend C=imp D=reach E=clicks F=CTR G=CPC H=CPM I=new_subs J=cost_sub K=conv L=total_subs
    B = f"B{r_first}:B{r_last}"
    C = f"C{r_first}:C{r_last}"
    D = f"D{r_first}:D{r_last}"
    E = f"E{r_first}:E{r_last}"
    I_ = f"I{r_first}:I{r_last}"

    summary_row = [
        "За місяць",
        f"=SUM({B})",                                      # B: Витрати
        f"=SUM({C})",                                      # C: Покази
        f"=SUM({D})",                                      # D: Охоплення
        f"=SUM({E})",                                      # E: Кліки
        f"=IFERROR(SUM({E})/SUM({C})*100,0)",             # F: CTR %
        f"=IFERROR(SUM({B})/SUM({E}),0)",                 # G: CPC €
        f"=IFERROR(SUM({B})/SUM({C})*1000,0)",            # H: CPM €
        f"=SUM({I_})",                                     # I: Нових підписників
        f"=IFERROR(SUM({B})/SUM({I_}),0)",                # J: Вартість підписника
        f"=IFERROR(SUM({I_})/SUM({E})*100,0)",            # K: Конверсія
        "",                                                # L: Підписники всього (не агрегується)
    ]

    ws.update(f"A{r_summary}", [summary_row])

    # Форматування рядка "За місяць" — жирний, фон як заголовок
    try:
        ws.format(f"A{r_summary}:L{r_summary}", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.85},
        })
    except Exception:
        pass

    logger.info(f"Skeleton {ws.title}: кістяк {month_prefix} готовий (рядки {r_first}–{r_last}), За місяць → рядок {r_summary}")


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

    yesterday = date.today() - timedelta(days=1)

    for funnel_key, keywords, channel_slug, sheet_title in FUNNEL_DEFS:
        stats_yesterday = by_funnel[funnel_key]

        # Щоденна історія підписників
        channel_history = {}
        try:
            channel_history = scrape_channel_history(channel_slug)
        except Exception as e:
            logger.warning(f"Channel history {channel_slug}: {e}")

        channel_total_yesterday = channel_history.get(yesterday.isoformat(),
                                  subs.get(channel_slug, {}).get("total", ""))

        ws = _get_or_create_with_headers(spreadsheet, sheet_title, FUNNEL_HEADERS)
        existing = ws.get_all_values()

        # Бекфіл пропущених дат (якщо задано)
        if backfill_from and all_rows:
            _backfill_funnel_sheet(spreadsheet, sheet_title, funnel_key, channel_slug,
                                   all_rows, channel_history, backfill_from)
            existing = ws.get_all_values()

        # Кістяк поточного місяця (дати без даних)
        _prepare_month_skeleton(ws, date.today().year, date.today().month)
        existing = ws.get_all_values()

        # Запис за вчора (тільки якщо є кампанії)
        if stats_yesterday:
            _write_funnel_today(ws, sheet_title, stats_yesterday,
                                channel_total_yesterday, existing, yesterday)
            logger.info(f"Sheet '{label}' {sheet_title}: заповнено {yesterday.isoformat()}")

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

        total_yesterday = total_history.get(yesterday.isoformat(),
                          subs.get("_total", {}).get("total", ""))

        ws_sum = _get_or_create_with_headers(spreadsheet, "Трафік - підсумок", FUNNEL_HEADERS)
        existing_sum = ws_sum.get_all_values()

        if backfill_from and all_rows:
            _backfill_all_traffic(spreadsheet, "Трафік - підсумок",
                                  all_rows, total_history, backfill_from)
            existing_sum = ws_sum.get_all_values()

        # Кістяк поточного місяця
        _prepare_month_skeleton(ws_sum, date.today().year, date.today().month)
        existing_sum = ws_sum.get_all_values()

        _write_funnel_today(ws_sum, "Трафік - підсумок", all_traffic,
                            total_yesterday, existing_sum, yesterday)
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

    last_date_sheet_row = 1
    for i, r in enumerate(existing):
        if r and r[0] and re.match(r'\d{4}-\d{2}-\d{2}', r[0]):
            last_date_sheet_row = i + 1

    existing_date_set = {r[0] for r in existing if r and r[0]}
    appended = 0
    for row in sorted(new_rows, key=lambda r: r[0]):
        if row[0] in existing_date_set:
            continue
        last_date_sheet_row += 1
        ws.update(f"A{last_date_sheet_row}", [row])
        existing_date_set.add(row[0])
        appended += 1

    logger.info(f"Бекфіл {sheet_title}: {appended} нових рядків (існуючі збережено)")


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
    total_reach = sum(s["reach"] for s in traffic_stats.values())
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
