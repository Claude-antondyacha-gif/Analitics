"""
Sfero Analytics Telegram Bot
Відповідає на запити через Claude AI, показує статистику, тригерить синк.
"""
import os
import logging
import requests
import json
from datetime import date, timedelta

import anthropic

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Claude-antondyacha-gif/Analitics")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID_SFERO", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Надіслати повідомлення в Telegram."""
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    })


def trigger_github_sync() -> bool:
    """Тригернути GitHub Actions workflow_dispatch."""
    if not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/daily_collect.yml/dispatches"
    resp = requests.post(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }, json={"ref": "main"})
    return resp.status_code == 204


def get_last_workflow_status() -> str:
    """Перевірити статус останнього запуску Actions."""
    if not GITHUB_TOKEN:
        return "невідомо (немає GitHub токену)"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/daily_collect.yml/runs?per_page=1"
    resp = requests.get(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    })
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


def get_sheets_context() -> str:
    """Читає останні дані з Google Sheets для контексту до Claude."""
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

        # Лідген Липень — останні 7 рядків з даними
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

        # Трафік на Ютуб — останні 5 рядків
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


def ask_claude(user_message: str, sheets_context: str = "") -> str:
    """Надіслати запит до Claude з контекстом проєкту."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = """Ти — AI-асистент для аналізу Meta Ads реклами проєкту Sfero (нерухомість в Іспанії).

Проєкт має два напрямки реклами:
1. ЛІДОГЕНЕРАЦІЯ — кампанії OUTCOME_LEADS з "SNAP" в назві. Відстежується: бюджет, ліди, покази, кліки. Звіт: лист "Лідген Липень".
2. ТРАФІК — кампанії OUTCOME_TRAFFIC на 3 канали: YouTube, Telegram канал, ChatBot. Звіт: листи "Трафік на Ютуб", "Трафік на Telegram канал", "Трафік на ChatBot".

Дані автоматично збираються з Meta API щодня о 07:00 і записуються в Google Sheets.

Відповідай коротко і по справі. Якщо питання про цифри — спирайся на надані дані.
Мова спілкування: українська."""

    messages = []

    if sheets_context:
        messages.append({
            "role": "user",
            "content": f"Ось актуальні дані з таблиць:\n\n{sheets_context}\n\nТепер моє питання: {user_message}"
        })
    else:
        messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def handle_update(update: dict):
    """Обробити одне оновлення від Telegram."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    # Перевірка доступу
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        send_message(chat_id, "⛔ Доступ заборонено.")
        return

    if not text:
        return

    logger.info(f"Message from {chat_id}: {text[:80]}")

    # ── Команди ────────────────────────────────────────────────────
    if text == "/start" or text == "/help":
        send_message(chat_id, (
            "👋 *Sfero Analytics Bot*\n\n"
            "Команди:\n"
            "/sync — запустити збір даних з Meta\n"
            "/status — статус останнього синку\n"
            "/stats — зведена статистика\n\n"
            "Або просто напиши будь-яке питання про рекламу — відповім через Claude AI 🤖"
        ))
        return

    if text == "/sync":
        send_message(chat_id, "⏳ Запускаю синк...")
        ok = trigger_github_sync()
        if ok:
            send_message(chat_id, "✅ GitHub Actions запущено! Дані оновляться за ~5 хвилин.")
        else:
            send_message(chat_id, "❌ Не вдалося запустити. Перевір GitHub PAT токен.")
        return

    if text == "/status":
        status = get_last_workflow_status()
        send_message(chat_id, f"📋 *Останній синк:*\n{status}")
        return

    if text == "/stats":
        send_message(chat_id, "⏳ Читаю дані...")
        context = get_sheets_context()
        if context:
            send_message(chat_id, f"📊 *Останні дані:*\n\n{context}", parse_mode="")
        else:
            send_message(chat_id, "Дані недоступні (Google Sheets не підключені в цьому середовищі).")
        return

    # ── AI відповідь ────────────────────────────────────────────────
    send_message(chat_id, "🤔 Думаю...")
    try:
        context = get_sheets_context()
        answer = ask_claude(text, context)
        send_message(chat_id, answer, parse_mode="")
    except Exception as e:
        logger.error(f"Claude error: {e}")
        send_message(chat_id, f"❌ Помилка: {e}")


def run_polling():
    """Long polling — бот сам запитує Telegram кожні 2 секунди."""
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
