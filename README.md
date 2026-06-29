# call-audit — Аудит звонков Royal Cargo

Автоматическая система анализа телефонных звонков менеджеров таможенного брокера **Royal Cargo**. Каждый звонок транскрибируется, оценивается AI и отправляется руководителю в Telegram.

## Что делает

1. **Мониторит** звонки менеджеров в Bitrix24 через webhook + polling каждые 3 минуты
2. **Скачивает** MP3-записи разговоров
3. **Транскрибирует** аудио через Groq Whisper (~1 сек на файл)
4. **Сохраняет** расшифровку в PostgreSQL
5. **Анализирует** качество разговора через Claude AI
6. **Отправляет** отчёт руководителю в Telegram с оценкой 🟢 / 🟡 / 🔴, полной расшифровкой и ссылкой на лид

## Архитектура

```
Bitrix24 (звонок завершён)
      │
      ├── webhook ONVOXIMPLANTCALLEND ──▶ watcher.py
      └── polling каждые 3 мин ──────────▶ watcher.py
                                               │
                                          Groq Whisper
                                               │
                                          PostgreSQL (call_transcripts)
                                               │
                                          audit.py --id <N>
                                               │
                                          Claude AI (оценка + резюме)
                                               │
                                          Telegram @ROYALAGENT011_bot
                                          (руководителю + разработчику)
```

**watcher.py** — Flask-сервер на Coolify. Принимает webhook от Bitrix24, скачивает запись, транскрибирует и сразу запускает `audit.py` для конкретной записи.

**audit.py** — анализирует расшифровку через Claude, определяет оценку разговора (🟢/🟡/🔴), формирует и отправляет отчёт в Telegram с полной расшифровкой.

**check_db.py** — email-уведомления о новых расшифровках (cron пн-пт 9-18 МСК).

## Стек

| Компонент | Технология |
|---|---|
| Транскрипция | Groq Whisper API (`whisper-large-v3-turbo`) |
| Анализ | Anthropic Claude (`claude-sonnet-4-6`) |
| CRM | Bitrix24 REST API |
| База данных | PostgreSQL (psycopg2) |
| Сервер | Flask + Coolify (Docker) |
| Уведомления | Telegram Bot API (`@ROYALAGENT011_bot`) |

## Деплой

Сервер работает на Coolify по адресу `https://agentseven.tamozhennybrokeragents.ru`.

```bash
# Проверить статус
curl https://agentseven.tamozhennybrokeragents.ru/health

# Запустить polling вручную
curl https://agentseven.tamozhennybrokeragents.ru/poll

# Запустить аудит вручную (все непросмотренные)
python3 audit.py

# Запустить аудит конкретной записи
python3 audit.py --id 42

# Переотправить все 23 записи
python3 audit.py --all
```

Деплой происходит автоматически при `git push` в `main`.

## Переменные окружения

Все секреты хранятся в `.env` (не коммитится):

```
BITRIX_PORTAL, BITRIX_USER_ID, BITRIX_TOKEN
GROQ_API_KEY
CLAUDE_API_KEY, CLAUDE_MODEL
Postgres_URL
TG_BOT_TOKEN_AUDIT
BITRIX_WEBHOOK_TOKEN
BITRIX_ID_* (ID менеджеров)
```
# call-audit
