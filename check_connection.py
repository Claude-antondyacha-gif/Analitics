"""
Перевірка підключення до Meta API та Claude API.
Запуск: python check_connection.py
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, ".")


def check_meta():
    print("\n=== META ADS API ===")
    try:
        from facebook_business.api import FacebookAdsApi
        from facebook_business.adobjects.business import Business

        FacebookAdsApi.init(access_token=os.environ["META_ACCESS_TOKEN"])

        for bm_key, label in [("META_BM_ID_MAIN", "Головний BM"), ("META_BM_ID_ZEEKR", "Zeekr Ukraine BM")]:
            bm_id = os.environ.get(bm_key, "")
            if not bm_id:
                print(f"  ⚠️  {label}: BM ID не вказано")
                continue
            try:
                bm = Business(bm_id)
                info = bm.api_get(fields=["id", "name"])
                print(f"  ✅ {label}: {info.get('name')} (id: {info.get('id')})")

                accounts = bm.get_owned_ad_accounts(fields=["id", "name", "account_status", "currency"])
                if accounts:
                    print(f"     Рекламні акаунти ({len(list(accounts))}):")
                    for acc in bm.get_owned_ad_accounts(fields=["id", "name", "account_status", "currency"]):
                        status = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_CLOSURE", 9: "IN_GRACE_PERIOD"}.get(acc.get("account_status"), "UNKNOWN")
                        print(f"       • {acc.get('name')} | act_{acc.get('id')} | {status} | {acc.get('currency')}")
                else:
                    print(f"     ⚠️  Немає акаунтів або немає прав доступу")
            except Exception as e:
                print(f"  ❌ {label} (id:{bm_id}): {e}")
    except Exception as e:
        print(f"  ❌ Помилка ініціалізації Meta API: {e}")


def check_claude():
    print("\n=== CLAUDE AI API ===")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": "Відповідь одним словом: готовий?"}],
        )
        print(f"  ✅ Claude API підключено: {resp.content[0].text.strip()}")
    except Exception as e:
        print(f"  ❌ Claude API помилка: {e}")


def check_db():
    print("\n=== БАЗА ДАНИХ ===")
    try:
        from storage.database import init_db, get_campaigns_list
        init_db()
        camps = get_campaigns_list()
        print(f"  ✅ SQLite OK | Кампаній у базі: {len(camps)}")
    except Exception as e:
        print(f"  ❌ DB помилка: {e}")


if __name__ == "__main__":
    print("Перевірка підключень...")
    check_meta()
    check_claude()
    check_db()
    print("\nГотово. Якщо всі ✅ — запускай: python scheduler.py --backfill 30")
