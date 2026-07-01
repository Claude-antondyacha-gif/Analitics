"""
Sfero Analytics Telegram Bot
Відповідає на запити через Claude AI, показує статистику, тригерить синк.
"""
import os
import sys
import logging
import requests
import json
from datetime import date, timedelta
from pathlib import Path

import anthropic

# Add project root to path so we can import storage/reports
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Ensure DB tables exist (empty on first run; data arrives after /sync)
try:
    from storage.database import init_db
    init_db()
except Exception as _e:
    logger.warning(f"DB init skipped: {_e}")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Claude-antondyacha-gif/Analitics")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID_SFERO", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# CPL alert threshold per chat (in-memory, resets on restart)
_cpl_thresholds: dict[int, float] = {}


# ── Telegram helpers ─────────────────────────────────────────────────────────

def send_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    """Надіслати повідомлення в Telegram (до 4096 символів)."""
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...скорочено</i>"
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }, timeout=15)


def send_typing(chat_id: int):
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing",
    }, timeout=5)


# ── GitHub helpers ───────────────────────────────────────────────────────────

GITHUB_WORKFLOW_ID = os.environ.get("GITHUB_WORKFLOW_ID", "304657737")


def trigger_github_sync() -> tuple[bool, str]:
    """Returns (success, error_message)."""
    if not GITHUB_TOKEN:
        return False, "Змінна GITHUB_PAT не встановлена в Railway"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    # Try by numeric ID first (more reliable), fallback to filename
    for workflow_ref in [GITHUB_WORKFLOW_ID, "daily_collect.yml"]:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_ref}/dispatches"
        try:
            resp = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
            if resp.status_code == 204:
                return True, ""
            try:
                detail = resp.json().get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            last_error = f"GitHub API: {resp.status_code} — {detail} (workflow: {workflow_ref})"
        except Exception as e:
            last_error = str(e)
    return False, last_error


def get_last_workflow_status() -> str:
    if not GITHUB_TOKEN:
        return "невідомо (немає GitHub токену)"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/daily_collect.yml/runs?per_page=1"
    resp = requests.get(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }, timeout=15)
    if resp.status_code != 200:
        return "помилка запиту"
    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return "запусків не знайдено"
    run = runs[0]
    status = run.get("status", "")
    conclusion = run.get("conclusion", "")
    created = run.get("created_at", "")[:16].replace("T", " ")
    if status == "completed":
        icon = "✅" if conclusion == "success" else "❌"
        return f"{icon} {conclusion} ({created} UTC)"
    return f"⏳ {status} ({created} UTC)"


# ── Google Sheets context ────────────────────────────────────────────────────

def get_sheets_context() -> str:
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_file = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json")
        if not os.path.exists(creds_file):
            return ""

        creds = Credentials.from_service_account_file(creds_file, scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ])
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(GOOGLE_SHEET_ID)

        context_parts = []

        try:
            ws = ss.worksheet("Лідген Липень")
            rows = ws.get_all_values()
            data_rows = [r for r in rows if r and r[0] and r[0][0].isdigit()][-7:]
            if data_rows:
                context_parts.append("📊 Лідген (останні дні):")
                for r in data_rows:
                    date_val = r[0] if len(r) > 0 else ""
                    budget = r[1] if len(r) > 1 else ""
                    leads = r[2] if len(r) > 2 else ""
                    if budget or leads:
                        context_parts.append(f"  {date_val}: бюджет={budget}, ліди={leads}")
        except Exception:
            pass

        for sheet_name in ["Трафік на Ютуб", "Трафік на ChatBot", "Трафік на Telegram канал"]:
            try:
                ws = ss.worksheet(sheet_name)
                rows = ws.get_all_values()
                data_rows = [r for r in rows if r and r[0] and r[0].startswith("202")][-3:]
                if data_rows:
                    context_parts.append(f"\n📈 {sheet_name} (останні дні):")
                    for r in data_rows:
                        context_parts.append(f"  {r[0]}: витрати={r[1]}, кліки={r[4] if len(r)>4 else ''}")
            except Exception:
                pass

        return "\n".join(context_parts)
    except Exception as e:
        logger.warning(f"Sheets context error: {e}")
        return ""


# ── Database helpers ─────────────────────────────────────────────────────────

def _db_available() -> bool:
    try:
        from storage.database import get_aggregated_metrics
        return True
    except Exception:
        return False


def _fmt_money(val) -> str:
    if not val:
        return "$0"
    return f"${val:,.2f}"


def _fmt_num(val) -> str:
    if not val:
        return "0"
    return f"{int(val):,}".replace(",", " ")


_NO_DATA_MSG = (
    "📭 <b>Дані ще не зібрані</b>\n\n"
    "База порожня — потрібно спочатку запустити збір:\n"
    "→ надішли /sync\n\n"
    "Після першого синку всі команди запрацюють."
)


def build_leads_report() -> str:
    """Лідген: поточний тиждень і минулий."""
    try:
        from storage.database import get_metrics_by_period
        rows_check = get_metrics_by_period(days=1)
        # rows_check is empty list when table exists but no data yet
        has_any = False

        lines = [f"🎯 <b>Лідогенерація — Sfero</b>"]
        lines.append(f"📅 {date.today().strftime('%d.%m.%Y')}\n")

        for label, days in [("Вчора", 1), ("7 днів", 7), ("14 днів", 14), ("30 днів", 30)]:
            rows = get_metrics_by_period(days=days)
            spend, leads, impressions, link_clicks = 0.0, 0, 0, 0
            for r in rows:
                has_any = True
                obj = (r.get("campaign_objective") or "").upper()
                name = (r.get("campaign_name") or "")
                if obj != "OUTCOME_LEADS":
                    continue
                if "snap" not in name.lower():
                    continue
                spend += r.get("spend") or 0
                leads += r.get("leads") or 0
                impressions += r.get("impressions") or 0
                link_clicks += r.get("link_clicks") or 0
            cpl = spend / leads if leads > 0 else 0
            lines.append(f"📆 <b>{label}</b>")
            lines.append(f"  💰 Витрати: <b>{_fmt_money(spend)}</b>")
            lines.append(f"  🎯 Ліди: <b>{leads}</b>  |  CPL: <b>{_fmt_money(cpl)}</b>")
            lines.append(f"  👁 Покази: <b>{_fmt_num(impressions)}</b>  |  🖱 Кліки: <b>{_fmt_num(link_clicks)}</b>\n")

        if not has_any:
            return _NO_DATA_MSG
        return "\n".join(lines)
    except Exception as e:
        if "no such table" in str(e):
            return _NO_DATA_MSG
        return f"❌ Помилка: {e}"


def build_traffic_report() -> str:
    """Трафік по каналах."""
    try:
        from storage.database import get_metrics_by_period
        lines = [f"📈 <b>Трафік по каналах — Sfero</b>"]
        lines.append(f"📅 {date.today().strftime('%d.%m.%Y')}\n")

        channels = {
            "YouTube": "youtube",
            "Telegram канал": "telegram",
            "ChatBot": "chatbot",
        }

        has_any = False
        for days_label, days in [("7 днів", 7), ("30 днів", 30)]:
            rows = get_metrics_by_period(days=days)
            channel_stats: dict = {k: {"spend": 0.0, "clicks": 0, "impressions": 0} for k in channels}
            other = {"spend": 0.0, "clicks": 0, "impressions": 0}

            for r in rows:
                has_any = True
                obj = (r.get("campaign_objective") or "").upper()
                name = (r.get("campaign_name") or "").lower()
                if obj != "OUTCOME_TRAFFIC":
                    continue
                matched = False
                for ch_label, keyword in channels.items():
                    if keyword in name:
                        channel_stats[ch_label]["spend"] += r.get("spend") or 0
                        channel_stats[ch_label]["clicks"] += r.get("link_clicks") or r.get("clicks") or 0
                        channel_stats[ch_label]["impressions"] += r.get("impressions") or 0
                        matched = True
                        break
                if not matched:
                    other["spend"] += r.get("spend") or 0
                    other["clicks"] += r.get("link_clicks") or r.get("clicks") or 0

            lines.append(f"📆 <b>{days_label}</b>")
            for ch_label, s in channel_stats.items():
                cpc = s["spend"] / s["clicks"] if s["clicks"] > 0 else 0
                lines.append(f"  ▸ <b>{ch_label}</b>: {_fmt_money(s['spend'])} | {_fmt_num(s['clicks'])} кліків" +
                              (f" | CPC {_fmt_money(cpc)}" if cpc else ""))
            if other["spend"] > 0:
                lines.append(f"  ▸ Інше: {_fmt_money(other['spend'])}")
            lines.append("")

        if not has_any:
            return _NO_DATA_MSG
        return "\n".join(lines)
    except Exception as e:
        if "no such table" in str(e):
            return _NO_DATA_MSG
        return f"❌ Помилка: {e}"


def build_week_report() -> str:
    """Тижневий зведений звіт."""
    try:
        from storage.database import get_aggregated_metrics, get_metrics_by_period
        if not get_metrics_by_period(days=7):
            return _NO_DATA_MSG
        lines = [f"📊 <b>Тижневий звіт — Sfero</b>"]
        lines.append(f"📅 {date.today().strftime('%d.%m.%Y')}\n")

        s7 = get_aggregated_metrics(days=7)
        s14 = get_aggregated_metrics(days=14)

        spend7 = s7.get("total_spend") or 0
        spend14 = (s14.get("total_spend") or 0) - spend7
        leads7 = s7.get("total_leads") or 0
        leads14_total = s14.get("total_leads") or 0
        leads_prev = leads14_total - leads7
        cpl7 = spend7 / leads7 if leads7 > 0 else 0
        cpl_prev = spend14 / leads_prev if leads_prev > 0 else 0

        def _trend(now, prev):
            if not prev:
                return ""
            delta = (now - prev) / prev * 100
            return f" {'🔺' if delta > 0 else '🔻'}{abs(delta):.0f}%"

        lines.append("🗓 <b>Цей тиждень (7 днів)</b>")
        lines.append(f"  💰 Витрати: <b>{_fmt_money(spend7)}</b>{_trend(spend7, spend14)}")
        lines.append(f"  🎯 Ліди: <b>{leads7}</b>{_trend(leads7, leads_prev)}")
        lines.append(f"  📉 CPL: <b>{_fmt_money(cpl7)}</b>{_trend(cpl7, cpl_prev)}")
        lines.append(f"  👁 Покази: <b>{_fmt_num(s7.get('total_impressions'))}</b>")
        lines.append(f"  👆 CTR: <b>{s7.get('avg_ctr') or 0:.2f}%</b>  |  CPC: <b>{_fmt_money(s7.get('avg_cpc'))}</b>")
        ctr7 = s7.get("avg_ctr") or 0
        ctr_prev = s14.get("avg_ctr") or 0
        lines.append(f"  📊 Активних кампаній: <b>{s7.get('active_campaigns') or 0}</b>")

        lines.append("\n🗓 <b>Минулий тиждень (8–14 днів)</b>")
        lines.append(f"  💰 Витрати: <b>{_fmt_money(spend14)}</b>")
        lines.append(f"  🎯 Ліди: <b>{leads_prev}</b>")
        lines.append(f"  📉 CPL: <b>{_fmt_money(cpl_prev)}</b>")

        return "\n".join(lines)
    except Exception as e:
        if "no such table" in str(e):
            return _NO_DATA_MSG
        return f"❌ Помилка: {e}"


def check_cpl_alert(chat_id: int):
    """Перевірити CPL і надіслати алерт якщо перевищено поріг."""
    threshold = _cpl_thresholds.get(chat_id)
    if not threshold:
        return
    try:
        from storage.database import get_metrics_by_period
        rows = get_metrics_by_period(days=1)
        spend, leads = 0.0, 0
        for r in rows:
            obj = (r.get("campaign_objective") or "").upper()
            name = (r.get("campaign_name") or "")
            if obj != "OUTCOME_LEADS" or "snap" not in name.lower():
                continue
            spend += r.get("spend") or 0
            leads += r.get("leads") or 0
        if leads > 0:
            cpl = spend / leads
            if cpl > threshold:
                send_message(chat_id,
                    f"🚨 <b>CPL Алерт!</b>\n\n"
                    f"Вчора CPL = <b>{_fmt_money(cpl)}</b>\n"
                    f"Ваш поріг: <b>{_fmt_money(threshold)}</b>\n\n"
                    f"Витрати: {_fmt_money(spend)} | Ліди: {leads}"
                )
    except Exception as e:
        logger.error(f"CPL alert check error: {e}")


# ── Claude AI ────────────────────────────────────────────────────────────────

def ask_claude(user_message: str, sheets_context: str = "", db_context: str = "") -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    today = date.today()
    system_prompt = f"""Ти — AI-аналітик Meta Ads для проєкту Sfero (нерухомість в Іспанії).
Сьогодні: {today.strftime('%d.%m.%Y')} ({today.strftime('%A')}).

Проєкт має два напрямки реклами:
1. ЛІДОГЕНЕРАЦІЯ — кампанії OUTCOME_LEADS з "SNAP" в назві. Метрики: бюджет, ліди, покази, кліки. Лист: "Лідген Липень".
2. ТРАФІК — кампанії OUTCOME_TRAFFIC на 3 канали: YouTube, Telegram канал, ChatBot. Листи: "Трафік на Ютуб", "Трафік на Telegram канал", "Трафік на ChatBot".

Дані збираються з Meta API щодня о 07:00 і записуються в Google Sheets та SQLite.

Команди бота:
/leads — лідогенерація по періодах
/traffic — трафік по каналах
/week — тижневий порівняльний звіт
/report — повний щоденний звіт
/sync — запустити збір даних
/status — статус останнього синку
/alert [число] — встановити поріг CPL у $

Відповідай коротко і конкретно. Якщо питання про цифри — спирайся на надані дані.
Якщо даних немає — чесно скажи. Мова: українська."""

    content_parts = []
    if db_context:
        content_parts.append(f"Дані з бази даних:\n{db_context}")
    if sheets_context:
        content_parts.append(f"Дані з Google Sheets:\n{sheets_context}")
    content_parts.append(f"Питання: {user_message}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": "\n\n".join(content_parts)}],
    )
    return response.content[0].text


def get_db_context_for_ai() -> str:
    """Збирає актуальні метрики з БД для контексту Claude."""
    try:
        from storage.database import get_aggregated_metrics
        parts = []
        for label, days in [("Вчора", 1), ("7 днів", 7), ("30 днів", 30)]:
            s = get_aggregated_metrics(days=days)
            spend = s.get("total_spend") or 0
            leads = s.get("total_leads") or 0
            cpl = spend / leads if leads > 0 else 0
            ctr = s.get("avg_ctr") or 0
            parts.append(
                f"{label}: витрати=${spend:.2f}, ліди={leads}, CPL=${cpl:.2f}, CTR={ctr:.2f}%"
            )
        return "\n".join(parts) if parts else ""
    except Exception:
        return ""


# ── Command handlers ─────────────────────────────────────────────────────────

HELP_TEXT = (
    "👋 <b>Sfero Analytics Bot</b>\n\n"
    "<b>Команди:</b>\n"
    "/leads — лідогенерація по періодах\n"
    "/traffic — трафік по каналах (YouTube, Telegram, ChatBot)\n"
    "/week — тижневий порівняльний звіт\n"
    "/report — повний щоденний звіт\n"
    "/sync — запустити збір даних з Meta\n"
    "/status — статус останнього синку\n"
    "/stats — дані з Google Sheets\n"
    "/alert [число] — поріг CPL для алерту (напр. /alert 25)\n\n"
    "Або напиши будь-яке питання про рекламу — відповім через Claude AI 🤖"
)


def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        send_message(chat_id, "⛔ Доступ заборонено.")
        return

    if not text:
        return

    logger.info(f"Message from {chat_id}: {text[:80]}")

    # /start /help
    if text in ("/start", "/help"):
        send_message(chat_id, HELP_TEXT)
        return

    # /leads
    if text == "/leads":
        send_typing(chat_id)
        send_message(chat_id, build_leads_report())
        return

    # /traffic
    if text == "/traffic":
        send_typing(chat_id)
        send_message(chat_id, build_traffic_report())
        return

    # /week
    if text == "/week":
        send_typing(chat_id)
        send_message(chat_id, build_week_report())
        return

    # /report — повний щоденний звіт
    if text == "/report":
        send_typing(chat_id)
        try:
            from notifier.telegram_bot import build_daily_report
            report = build_daily_report()
            if len(report) > 4000:
                report = report[:3950] + "\n\n<i>...скорочено</i>"
            send_message(chat_id, report)
        except Exception as e:
            send_message(chat_id, f"❌ Помилка генерації звіту: {e}")
        return

    # /sync
    if text == "/sync":
        send_message(chat_id, "⏳ Запускаю синк...")
        ok, err = trigger_github_sync()
        if ok:
            send_message(chat_id, "✅ GitHub Actions запущено!\nДані оновляться за ~5 хвилин.")
        else:
            send_message(chat_id, f"❌ Не вдалося запустити\n\n<code>{err}</code>")
        return

    # /status
    if text == "/status":
        status = get_last_workflow_status()
        send_message(chat_id, f"📋 <b>Останній синк:</b>\n{status}")
        return

    # /stats
    if text == "/stats":
        send_typing(chat_id)
        context = get_sheets_context()
        if context:
            send_message(chat_id, f"📊 <b>Останні дані (Google Sheets):</b>\n\n{context}")
        else:
            send_message(chat_id, "Дані недоступні (Google Sheets не підключені).")
        return

    # /alert [число]
    if text.startswith("/alert"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current = _cpl_thresholds.get(chat_id)
            if current:
                send_message(chat_id, f"🔔 Поточний поріг CPL: <b>{_fmt_money(current)}</b>\nЩоб змінити: /alert 25")
            else:
                send_message(chat_id, "🔕 Алерт CPL не встановлено.\nЩоб встановити: /alert 25")
        else:
            try:
                threshold = float(parts[1].replace("$", "").replace(",", "."))
                _cpl_thresholds[chat_id] = threshold
                send_message(chat_id,
                    f"✅ Алерт CPL встановлено: <b>{_fmt_money(threshold)}</b>\n"
                    f"Отримуватимеш сповіщення якщо CPL перевищить цю суму."
                )
            except ValueError:
                send_message(chat_id, "❌ Вкажи суму числом. Приклад: /alert 25")
        return

    # AI відповідь
    send_typing(chat_id)
    try:
        sheets_ctx = get_sheets_context()
        db_ctx = get_db_context_for_ai()
        answer = ask_claude(text, sheets_ctx, db_ctx)
        send_message(chat_id, answer)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        send_message(chat_id, f"❌ Помилка: {e}")


# ── Polling loop ─────────────────────────────────────────────────────────────

def run_polling():
    import time
    logger.info("Starting Telegram bot (polling mode)...")
    offset = None

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset

            resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
            data = resp.json()

            if not data.get("ok"):
                logger.error(f"Telegram error: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    logger.error(f"Handle update error: {e}")

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_polling()
