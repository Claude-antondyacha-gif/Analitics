# Meta Ads Analytics Dashboard

AI-агент для автоматичної аналітики та звітності реклами у Meta Ads (Facebook/Instagram).

## Що робить система

- **Щодня о 07:00** автоматично вивантажує всі метрики з Meta Ads API
- **AI-агент (Claude Opus)** аналізує дані за 1/3/7/14/30 днів, виявляє проблеми, дає рекомендації
- **Самописний дашборд** показує всі ключові метрики: витрати, CPL, CTR, CPC, ROAS, ліди, підписники
- **Google Sheets sync** — автозаповнення таблиці звіту
- **AI Чат** — запитай про будь-яку кампанію звичайною мовою
- **Виконання дій** — зупинити/запустити кампанію прямо з дашборду або за рекомендацією AI

## Структура

```
├── collector/          # Збір даних з Meta Ads API
├── agent/              # AI-агент на Claude Opus
├── storage/            # SQLite база даних
├── reports/            # Синхронізація з Google Sheets
├── dashboard/          # Flask веб-дашборд
├── scheduler.py        # Щоденний планувальник (07:00 Kyiv)
└── .env                # Твої API ключі
```

## Швидкий старт

### 1. Встановлення
```bash
bash setup.sh
```

### 2. Налаштування .env
```bash
# Meta Ads — отримай на developers.facebook.com
META_ACCESS_TOKEN=EAAxxxxxx...
META_AD_ACCOUNT_ID=123456789       # без "act_"
META_APP_ID=xxxxxxxx
META_APP_SECRET=xxxxxxxx

# Claude AI — отримай на console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-xxxxx...

# Google Sheets (необов'язково)
GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
GOOGLE_SHEET_ID=1BxiMVs0...
```

### 3. Завантаж історичні дані
```bash
source venv/bin/activate
python scheduler.py --backfill 30
```

### 4. Запусти дашборд
```bash
python dashboard/app.py
# Відкрий http://localhost:5000
```

### 5. Запусти планувальник (окремий термінал або systemd)
```bash
python scheduler.py
# Буде запускатись щодня о 07:00
```

## Як отримати Meta Access Token

1. Зайди на [developers.facebook.com](https://developers.facebook.com)
2. Створи App → Business тип
3. Додай продукт Marketing API
4. В Graph API Explorer згенеруй Long-lived token з правами:
   - `ads_read`, `ads_management`, `read_insights`
5. Продовж токен через [Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/)

## Метрики на дашборді

| Метрика | Опис |
|---------|------|
| CPL | Вартість ліда |
| CPP | Вартість покупки |
| ROAS | Return on Ad Spend |
| CTR | Click-through rate |
| CPC | Вартість кліку |
| CPM | Вартість 1000 показів |
| Reach | Унікальне охоплення |
| Page Likes | Нові підписники сторінки |

## Запуск як systemd сервіс (Linux сервер)

```bash
sudo nano /etc/systemd/system/meta-analytics.service
```
```ini
[Unit]
Description=Meta Ads Analytics Scheduler
After=network.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/Analitics
ExecStart=/path/to/Analitics/venv/bin/python scheduler.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now meta-analytics
```
