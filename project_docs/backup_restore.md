# Off-site Backup & Restore

Каждый день (дважды) выделенный **бэкап-бот** (`@rowsbackup_bot`, свой токен —
только в серверном env, не в git) отправляет gzip-снапшот `words.db` (WAL-safe) в
**приватный Telegram-канал**. История канала = хранилище (храним всё). Это
независимо от основного бота: если Telegram забанит основного бота — бэкапы
уцелеют.

- Пайплайн: `scripts/offsite_backup.py` → `bot/offsite_backup/backup.py`
  (`backup_database()` через backup API → gzip → `sendDocument`).
- Расписание: `deploy/offsite-backup.{service,timer}`, дважды в день в 03:00 и
  15:00 UTC.
- Имя файла: `words-YYYYMMDD-HHMM.db.gz.enc` (UTC; `.enc` = запечатан, см.
  Шифрование). Подпись: дата, схема `user_version`, размер, состояние шифрования,
  sha256 отправленного артефакта и счётчики ключевых таблиц (words / progress /
  learning_items / quiz_events / users / user_settings).
- Обработка ошибок: 3 попытки с бэкоффом; при стойком сбое бот шлёт алерт в
  `BACKUP_ALERT_CHAT_ID` (по умолчанию = канал) и процесс выходит с ненулевым
  кодом (видно в journal). Мониторинг «тишиной» — нет ночного файла = проверить.

## Шифрование (асимметричное — ТЗ #4)
Бэкапы запечатываются libsodium sealed box (`bot/offsite_backup/crypto.py`): на
сервере только **публичный** ключ (шифрует, но не расшифровывает), **приватный**
ключ — у владельца, офлайн, и только он расшифровывает бэкап. Утечка канала — или
даже взлом сервера — не раскрывает содержимое бэкапа. Подробный мануал —
`backup_encryption.md`.

- Сгенерировать пару один раз:
  ```bash
  python3 -m scripts.backup_keygen            # печатает публичный ключ; приватный пишет в файл 0600
  ```
  Публичный ключ — в серверный env как `BACKUP_PUBLIC_KEY`. Файл приватного ключа
  храни у себя (менеджер паролей + одна офлайн-копия). **Потеря приватного ключа
  делает все бэкапы навсегда нечитаемыми.** Приватный ключ не должен попадать на
  сервер или в git.
- Если `BACKUP_PUBLIC_KEY` не задан — скрипт шлёт открытый `.gz`, а подпись
  говорит `⚠️ plaintext`, так что сбой конфигурации виден, а не молчалив.

## Серверный env (не коммитить)
```
BACKUP_BOT_TOKEN=<токен @rowsbackup_bot>
BACKUP_CHANNEL_ID=<-100… id приватного канала>
BACKUP_PUBLIC_KEY=<base64 публичный ключ из backup_keygen>
BACKUP_ALERT_CHAT_ID=<опц.; по умолчанию = канал>
# DB_PATH уже задан для основного бота и переиспользуется.
```

## Процедура восстановления
Предполагается, что `.enc`-бэкап скачан из канала.

1. **Расшифровать + проверить архив ДО того, как трогать прод.** Расшифровку делай
   на своей машине (приватный ключ на сервер попадать не должен):
   ```bash
   sha256sum words-YYYYMMDD-HHMM.db.gz.enc        # сверить с подписью сообщения
   python3 -m scripts.backup_decrypt words-YYYYMMDD-HHMM.db.gz.enc backup_private_key.txt
   gunzip -k words-YYYYMMDD-HHMM.db.gz            # -> words-YYYYMMDD-HHMM.db
   sqlite3 words-YYYYMMDD-HHMM.db 'PRAGMA integrity_check;'   # должно быть: ok
   venv/bin/python3 -m scripts.smoke_check_db --db words-YYYYMMDD-HHMM.db
   ```
   Также глазами сверь счётчики строк из подписи с ожиданиями, прежде чем идти
   дальше.
2. **Остановить бота**, чтобы во время подмены никто не писал:
   `systemctl stop deutsch-alphatest`  (на проде — имя прод-сервиса).
3. **Сохранить текущую (возможно битую) БД** — никогда не перезаписывать вслепую:
   `cp data/words.db data/words.before-restore-$(date +%Y%m%d-%H%M%S).db`
   (и убрать устаревшие sidecar’ы: `rm -f data/words.db-wal data/words.db-shm`).
4. **Подставить** проверенный снапшот: `cp words-YYYYMMDD-HHMM.db data/words.db`.
5. **Запустить бота**: `systemctl start deutsch-alphatest`.
6. **Healthcheck**: `venv/bin/python3 -m scripts.smoke_check_db` по живой БД,
   убедиться, что бот отвечает на `/start`; смотреть `journalctl -u <service> -n 50`.

## Приёмка (гейт — сделать один раз, отметить в log.md)
- [x] `@rowsbackup_bot` создан + приватный канал «Backups» + бот-админ; `chat_id`
      получен. Пара ключей сгенерирована (приватный ключ на машине владельца, 0600).
- [x] Зашифрованная отправка проверена сквозняком локально: запечатанный
      `.db.gz.enc` доставлен в канал; расшифровка → gunzip → `integrity_check ok`
      реальной парой ключей.
- [ ] Прописать `BACKUP_BOT_TOKEN` / `BACKUP_CHANNEL_ID` / `BACKUP_PUBLIC_KEY` в
      **серверный** `.env`; поставить `deploy/offsite-backup.{service,timer}`;
      разово `systemctl start offsite-backup.service` → в канал падает `.db.gz.enc`.
- [ ] Таймер оставить включённым 2–3 дня → два файла в день, ~03:00 и ~15:00 UTC.
- [ ] **Одна реальная репетиция восстановления на тест-сервере** (расшифровать →
      проверить → стоп → подмена → старт → healthcheck). Только тогда гейт закрыт.

## Позже (фаза 3, не сейчас)
Второй, независимый от Telegram эшелон: `rclone copy` того же `.enc` в облако
(Google Drive / S3 / Backblaze) отдельным cron/таймером — чтобы сбой Telegram или
бан бота не убил обе копии разом.
