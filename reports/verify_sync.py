"""
Verify that Google Sheets data matches the SQLite database (Meta API source).

Checks:
  - Лідген [Місяць]: columns B (spend), C (leads), D (impressions), E (link_clicks)
  - Трафік на ChatBot / YouTube / Telegram канал: spend, impressions, clicks

Returns a list of VerifyResult objects and a human-readable summary.
"""
import os
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TOLERANCE = 0.05   # 5% tolerance for float rounding differences
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID_SFERO", "")

UA_MONTHS = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
             "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]

FUNNEL_SHEETS = [
    ("Трафік на ChatBot",         "chatbot"),
    ("Трафік на Ютуб",            "youtube"),
    ("Трафік на Telegram канал",  "telegram"),
]


@dataclass
class VerifyResult:
    sheet: str
    date_iso: str
    field: str
    db_value: float
    sheet_value: float
    ok: bool
    diff_pct: float = 0.0
    note: str = ""


def _near(a: float, b: float) -> tuple[bool, float]:
    """Returns (is_close, diff_pct)."""
    if a == 0 and b == 0:
        return True, 0.0
    if a == 0 or b == 0:
        return False, 100.0
    diff = abs(a - b) / max(abs(a), abs(b))
    return diff <= TOLERANCE, round(diff * 100, 1)


def _get_sheets_client():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=[
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ])
    return gspread.authorize(creds)


def _parse_num(val) -> float:
    """Parse a cell value to float, handling €/$ symbols and comma decimals."""
    if val is None or val == "":
        return 0.0
    s = str(val).replace("€", "").replace("$", "").replace(" ", "").strip()
    # Handle European comma-decimal like "38,79"
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    elif s.count(",") > 1:
        # Thousands separator: "1 234,56" → already handled above
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _db_leadgen_day(target_date: date) -> dict:
    """Aggregate SNAP OUTCOME_LEADS metrics for a given date from DB."""
    from storage.database import get_metrics_by_period
    days_back = (date.today() - target_date).days + 1
    rows = get_metrics_by_period(days=max(days_back, 1))
    spend, leads, impressions, link_clicks = 0.0, 0, 0, 0
    for r in rows:
        if r.get("date") != target_date.isoformat():
            continue
        obj = (r.get("campaign_objective") or "").upper()
        name = (r.get("campaign_name") or "")
        if obj != "OUTCOME_LEADS" or "snap" not in name.lower():
            continue
        spend += r.get("spend") or 0
        leads += r.get("leads") or 0
        impressions += r.get("impressions") or 0
        link_clicks += r.get("link_clicks") or 0
    return {"spend": round(spend, 2), "leads": leads,
            "impressions": impressions, "link_clicks": link_clicks}


def _db_traffic_day(target_date: date, channel_keyword: str) -> dict:
    """Aggregate OUTCOME_TRAFFIC metrics for a channel and date from DB."""
    from storage.database import get_metrics_by_period
    days_back = (date.today() - target_date).days + 1
    rows = get_metrics_by_period(days=max(days_back, 1))
    spend, impressions, clicks = 0.0, 0, 0
    for r in rows:
        if r.get("date") != target_date.isoformat():
            continue
        obj = (r.get("campaign_objective") or "").upper()
        name = (r.get("campaign_name") or "").lower()
        if obj != "OUTCOME_TRAFFIC" or channel_keyword not in name:
            continue
        spend += r.get("spend") or 0
        impressions += r.get("impressions") or 0
        clicks += r.get("link_clicks") or r.get("clicks") or 0
    return {"spend": round(spend, 2), "impressions": impressions, "clicks": clicks}


def _verify_leadgen(ss, results: list[VerifyResult], days_back: int = 7):
    """Check Лідген [Місяць] sheet for last N days."""
    sheet_title = f"Лідген {UA_MONTHS[date.today().month]}"
    try:
        ws = ss.worksheet(sheet_title)
    except Exception:
        logger.warning(f"Worksheet '{sheet_title}' not found — skipping leadgen verify")
        return

    rows = ws.get_all_values()
    # Build date→row_index map
    date_map: dict[str, int] = {}
    for i, row in enumerate(rows):
        if not row:
            continue
        cell = row[0].strip()
        # Match DD.MM.YYYY
        import re
        if re.match(r"\d{2}\.\d{2}\.\d{4}", cell):
            try:
                d = date(int(cell[6:]), int(cell[3:5]), int(cell[:2]))
                date_map[d.isoformat()] = i
            except Exception:
                pass

    for offset in range(days_back):
        target = date.today() - timedelta(days=offset + 1)
        iso = target.isoformat()
        if iso not in date_map:
            continue
        row_i = date_map[iso]
        row = rows[row_i]

        db = _db_leadgen_day(target)
        if db["spend"] == 0 and db["leads"] == 0:
            continue  # No DB data for this day — skip

        checks = [
            ("Бюджет (B)",   db["spend"],       _parse_num(row[1] if len(row) > 1 else "")),
            ("Ліди (C)",     db["leads"],        _parse_num(row[2] if len(row) > 2 else "")),
            ("Покази (D)",   db["impressions"],  _parse_num(row[3] if len(row) > 3 else "")),
            ("Кліки (E)",    db["link_clicks"],  _parse_num(row[4] if len(row) > 4 else "")),
        ]
        for fname, db_val, sheet_val in checks:
            ok, diff = _near(float(db_val), sheet_val)
            results.append(VerifyResult(
                sheet=sheet_title, date_iso=iso,
                field=fname, db_value=float(db_val), sheet_value=sheet_val,
                ok=ok, diff_pct=diff,
            ))


def _verify_traffic(ss, results: list[VerifyResult], days_back: int = 7):
    """Check Трафік sheets for last N days."""
    for sheet_title, keyword in FUNNEL_SHEETS:
        try:
            ws = ss.worksheet(sheet_title)
        except Exception:
            logger.warning(f"Worksheet '{sheet_title}' not found — skipping")
            continue

        rows = ws.get_all_values()
        date_map: dict[str, int] = {}
        import re
        for i, row in enumerate(rows):
            if not row:
                continue
            cell = row[0].strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", cell):
                date_map[cell] = i

        for offset in range(days_back):
            target = date.today() - timedelta(days=offset + 1)
            iso = target.isoformat()
            if iso not in date_map:
                continue
            row_i = date_map[iso]
            row = rows[row_i]

            db = _db_traffic_day(target, keyword)
            if db["spend"] == 0 and db["clicks"] == 0:
                continue

            checks = [
                ("Витрати (B)", db["spend"],       _parse_num(row[1] if len(row) > 1 else "")),
                ("Покази (C)",  db["impressions"],  _parse_num(row[2] if len(row) > 2 else "")),
                ("Кліки (E)",   db["clicks"],       _parse_num(row[4] if len(row) > 4 else "")),
            ]
            for fname, db_val, sheet_val in checks:
                ok, diff = _near(float(db_val), sheet_val)
                results.append(VerifyResult(
                    sheet=sheet_title, date_iso=iso,
                    field=fname, db_value=float(db_val), sheet_value=sheet_val,
                    ok=ok, diff_pct=diff,
                ))


def run_verification(days_back: int = 7) -> tuple[list[VerifyResult], str]:
    """
    Run full verification. Returns (results, summary_text).
    summary_text is formatted for Telegram (HTML).
    """
    results: list[VerifyResult] = []

    try:
        gc = _get_sheets_client()
        ss = gc.open_by_key(SHEET_ID)
    except Exception as e:
        msg = f"❌ Не вдалося підключитись до Google Sheets: {e}"
        logger.error(msg)
        return [], msg

    _verify_leadgen(ss, results, days_back)
    _verify_traffic(ss, results, days_back)

    if not results:
        return results, "ℹ️ Немає даних для перевірки (DB порожня або Google Sheets не підключені)"

    errors = [r for r in results if not r.ok]
    ok_count = len(results) - len(errors)

    lines = [f"🔍 <b>Верифікація синку — {date.today().strftime('%d.%m.%Y')}</b>\n"]
    lines.append(f"✅ Збігів: <b>{ok_count}</b>  |  ❌ Розбіжностей: <b>{len(errors)}</b>\n")

    if errors:
        lines.append("━━━ <b>РОЗБІЖНОСТІ</b> ━━━")
        # Group by sheet
        by_sheet: dict = {}
        for r in errors:
            by_sheet.setdefault(r.sheet, []).append(r)
        for sheet, errs in by_sheet.items():
            lines.append(f"\n📋 <b>{sheet}</b>")
            for r in errs[:10]:
                lines.append(
                    f"  {r.date_iso} | {r.field}\n"
                    f"  DB: <b>{r.db_value}</b> → Sheets: <b>{r.sheet_value}</b>"
                    f" (різниця {r.diff_pct}%)"
                )
    else:
        lines.append("✅ Всі цифри збігаються з Meta API даними")

    return results, "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    results, summary = run_verification(days_back=7)
    print(summary)
