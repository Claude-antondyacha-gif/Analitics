#!/bin/bash
set -e

echo "=== Meta Ads Analytics Setup ==="

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create data directory
mkdir -p data

# Copy env template if .env doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "✅ Створено файл .env — заповни свої ключі API!"
fi

# Init database
python3 -c "
import sys; sys.path.insert(0,'.')
from storage.database import init_db
init_db()
print('✅ База даних ініціалізована')
"

echo ""
echo "=== ГОТОВО ==="
echo ""
echo "Наступні кроки:"
echo "1. Відредагуй .env файл — вкажи META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, ANTHROPIC_API_KEY"
echo "2. Завантаж 30 днів даних: python scheduler.py --backfill 30"
echo "3. Запусти дашборд:        python dashboard/app.py"
echo "4. Запусти планувальник:   python scheduler.py"
echo ""
echo "Дашборд буде доступний на http://localhost:5000"
