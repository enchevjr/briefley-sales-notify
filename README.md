# Briefley sales notify

Telegram известия за нови триали и покупки. Проверка на всеки 5 минути (GitHub Actions). Ключовете са в GitHub Secrets.
- `kg.py` – бот „Briefley Градини“ (заявки за демо от /kindergarten-bookings + напомняне 15 мин преди среща). Отделен бот: TELEGRAM_KG_BOT_TOKEN. Пуска се от същия цикъл в notify.py. Лични данни само в Telegram, никога в логовете.
