#!/usr/bin/env python3
"""
Вигрузка наскрізної аналітики по креативах (рівень оголошень) в Google Sheets.

Використання:
    python scripts/export_creatives.py                    # з 16 червня по вчора
    python scripts/export_creatives.py --from 2026-06-16  # конкретна дата початку
    python scripts/export_creatives.py --from 2026-06-16 --to 2026-06-30
    python scripts/export_creatives.py --days 45          # останні N днів
    python scripts/export_creatives.py --no-fetch         # тільки Sheets (дані вже є в DB)
"""
import os
import sys
import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

# Project root in path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_DATE_FROM = date(2026, 6, 16)
DEFAULT_DATE_TO   = date.today() - timedelta(days=1)

SHEET_NAME = "Креативи (авто)"

HEADERS = [
    "Дата", "Кампанія", "Група оголошень", "Креатив",
    "Витрати ($)", "Ліди", "CPL ($)",
    "Покази", "Кліки", "CTR (%)", "CPM ($)",
]


# ── Step 1: Fetch from Meta API ───────────────────────────────────────────────

def fetch_from_meta(date_from: date, date_to: date) -> int:
    """Pull ad-level insights from Meta for all accounts and save to DB."""
    logger.info(f"Ініціалізую Meta API...")
    from facebook_business.api import FacebookAdsApi
    FacebookAdsApi.init(access_token=os.environ["META_ACCESS_TOKEN"])

    from storage.database import init_db
    init_db()

    from collector.meta_collector import get_all_ad_accounts, fetch_campaigns_for_account, fetch_ad_insights_for_account
    accounts = get_all_ad_accounts()
    if not accounts:
        logger.error("Акаунти не знайдено. Перевір META_BM_ID_MAIN / META_BM_ID_ZEEKR і токен.")
        return 0

    logger.info(f"Знайдено {len(accounts)} акаунтів. Завантажую дані {date_from} → {date_to}...")

    total = 0
    for acc in accounts:
        acc_id = acc["id"]
        acc_name = acc.get("name", acc_id)
        logger.info(f"  → {acc_name} ({acc_id})")
        try:
            fetch_campaigns_for_account(acc_id)
            count = fetch_ad_insights_for_account(acc_id, date_from, date_to)
            total += count
            logger.info(f"     ad-level рядків: {count}")
        except Exception as e:
            logger.error(f"     Помилка: {e}")

    logger.info(f"Всього зібрано: {total} ad-level рядків")
    return total


# ── Step 2: Read from DB ──────────────────────────────────────────────────────

def load_from_db(date_from: date, date_to: date) -> list[dict]:
    """Load ad metrics from SQLite for the given date range."""
    from storage.database import get_connection
    conn = get_connection()
    query = """
        SELECT
            a.date,
            a.ad_id,
            a.ad_name,
            a.adset_id,
            a.adset_name,
            a.campaign_id,
            a.campaign_name,
            ROUND(a.spend, 2)          AS spend,
            a.impressions,
            a.clicks,
            a.leads,
            a.link_clicks,
            ROUND(a.ctr, 2)            AS ctr,
            ROUND(a.cpc, 2)            AS cpc,
            ROUND(a.cpm, 2)            AS cpm,
            a.cost_per_lead,
            COALESCE(c.objective, '')  AS campaign_objective
        FROM daily_ad_metrics a
        LEFT JOIN campaigns c ON a.campaign_id = c.id
        WHERE a.date BETWEEN :date_from AND :date_to
          AND (
            c.objective IN ('OUTCOME_LEADS', 'OUTCOME_SALES', 'OUTCOME_CONVERSIONS', 'CONVERSIONS')
            OR LOWER(a.campaign_name) LIKE '%snap%'
            OR LOWER(a.campaign_name) LIKE '%lead%'
            OR LOWER(a.campaign_name) LIKE '%конверс%'
          )
        ORDER BY a.date DESC, a.spend DESC
    """
    rows = conn.execute(query, {
        "date_from": date_from.isoformat(),
        "date_to":   date_to.isoformat(),
    }).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Step 3: Write to Google Sheets ────────────────────────────────────────────

def write_to_sheets(rows: list[dict], date_from: date, date_to: date):
    """Clear and rewrite the 'Креативи (авто)' sheet."""
    import gspread
    from google.oauth2.service_account import Credentials
    from datetime import datetime

    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    sheet_id   = os.environ.get("GOOGLE_SHEET_ID_SFERO") or os.environ.get("GOOGLE_SHEET_ID")

    if not sheet_id:
        logger.error("Не знайдено GOOGLE_SHEET_ID_SFERO у .env")
        return

    if not os.path.exists(creds_file):
        logger.error(f"Файл credentials не знайдено: {creds_file}")
        return

    logger.info(f"Підключаюсь до Google Sheets...")
    creds = Credentials.from_service_account_file(creds_file, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(sheet_id)

    # Get or create worksheet
    try:
        ws = ss.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=SHEET_NAME, rows=2000, cols=len(HEADERS) + 5)
        logger.info(f"Створено новий лист: {SHEET_NAME}")

    # Build data rows
    data_rows = []
    for r in rows:
        spend = r.get("spend") or 0
        leads = r.get("leads") or 0
        cpl   = round(spend / leads, 2) if leads > 0 else ""

        data_rows.append([
            r.get("date", ""),
            r.get("campaign_name", ""),
            r.get("adset_name", ""),
            r.get("ad_name", ""),
            spend,
            leads,
            cpl,
            r.get("impressions") or 0,
            r.get("clicks") or 0,
            r.get("ctr") or 0,
            r.get("cpm") or 0,
        ])

    # Title row with period info
    updated_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    title_row  = [f"Оновлено: {updated_at}  |  Період: {date_from.strftime('%d.%m.%Y')} – {date_to.strftime('%d.%m.%Y')}  |  Рядків: {len(data_rows)}"]

    logger.info(f"Записую {len(data_rows)} рядків у '{SHEET_NAME}'...")

    ws.clear()
    ws.update("A1", [title_row])
    ws.update("A2", [HEADERS])
    if data_rows:
        ws.update("A3", data_rows)

    # Format title row
    try:
        ws.format("A1:K1", {
            "textFormat": {"bold": True, "fontSize": 10},
            "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95},
        })
    except Exception:
        pass

    # Format header row
    try:
        ws.format(f"A2:{chr(64+len(HEADERS))}2", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.18, "green": 0.27, "blue": 0.6},
        })
    except Exception:
        pass

    # Freeze top 2 rows
    try:
        ws.freeze(rows=2)
    except Exception:
        pass

    logger.info(f"Готово! Лист '{SHEET_NAME}' оновлено: {len(data_rows)} рядків.")
    print(f"\n✅ Записано {len(data_rows)} рядків у лист «{SHEET_NAME}»")
    print(f"   Дати: {date_from} → {date_to}")
    print(f"   Унікальних креативів: {len({r['ad_name'] for r in rows})}")

    # Print quick summary in terminal
    if data_rows:
        print("\n📊 Топ-10 креативів за витратами:")
        from collections import defaultdict
        summary: dict = defaultdict(lambda: {"spend": 0, "leads": 0})
        for r in rows:
            key = r.get("ad_name", "—")
            summary[key]["spend"] += r.get("spend") or 0
            summary[key]["leads"] += r.get("leads") or 0
        sorted_ads = sorted(summary.items(), key=lambda x: x[1]["spend"], reverse=True)
        total_spend = sum(v["spend"] for v in summary.values())
        total_leads = sum(v["leads"] for v in summary.values())
        print(f"{'Креатив':<40} {'Витрати':>10} {'%бюдж':>7} {'Ліди':>6} {'CPL':>8}")
        print("─" * 75)
        for name, s in sorted_ads[:10]:
            spend = s["spend"]
            leads = s["leads"]
            cpl   = spend / leads if leads > 0 else 0
            share = spend / total_spend * 100 if total_spend > 0 else 0
            print(f"{name[:40]:<40} ${spend:>9.2f} {share:>6.1f}% {leads:>6} ${cpl:>7.2f}")
        print("─" * 75)
        overall_cpl = total_spend / total_leads if total_leads > 0 else 0
        print(f"{'РАЗОМ':<40} ${total_spend:>9.2f} {'100%':>7} {total_leads:>6} ${overall_cpl:>7.2f}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export ad-level creatives to Google Sheets")
    parser.add_argument("--from", dest="date_from", default=None,
                        help="Дата початку YYYY-MM-DD (за замовч. 2026-06-16)")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="Дата кінця YYYY-MM-DD (за замовч. вчора)")
    parser.add_argument("--days", type=int, default=None,
                        help="Альтернатива --from: останні N днів")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Не завантажувати з Meta API (використати дані що вже є в DB)")
    args = parser.parse_args()

    # Resolve date range
    date_to = date.fromisoformat(args.date_to) if args.date_to else DEFAULT_DATE_TO

    if args.days:
        date_from = date_to - timedelta(days=args.days - 1)
    elif args.date_from:
        date_from = date.fromisoformat(args.date_from)
    else:
        date_from = DEFAULT_DATE_FROM

    print(f"\n🚀 Експорт креативів: {date_from} → {date_to}")
    print(f"   Режим: {'тільки DB (--no-fetch)' if args.no_fetch else 'Meta API + DB'}\n")

    # Step 1: Fetch (unless --no-fetch)
    if not args.no_fetch:
        count = fetch_from_meta(date_from, date_to)
        if count == 0:
            logger.warning("Даних не отримано з Meta API. Спробуй --no-fetch якщо дані вже в DB.")

    # Step 2: Load from DB
    rows = load_from_db(date_from, date_to)
    if not rows:
        print("❌ В базі немає даних для цього діапазону.")
        print("   Перевір що META_ACCESS_TOKEN і BM IDs налаштовані в .env")
        sys.exit(1)

    logger.info(f"Завантажено {len(rows)} рядків з DB")

    # Step 3: Write to Sheets
    write_to_sheets(rows, date_from, date_to)


if __name__ == "__main__":
    main()
