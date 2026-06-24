"""
Webhook-сервер для мониторинга звонков Bitrix24.
Bitrix24 отправляет событие ONVOXIMPLANTCALLEND → мы скачиваем запись,
транскрибируем через Groq и сохраняем в Postgres.
"""

import os, time, logging, requests, psycopg2, threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import groq as groq_sdk

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

BITRIX_BASE    = f"{os.getenv('BITRIX_PORTAL')}/rest/{os.getenv('BITRIX_USER_ID')}/{os.getenv('BITRIX_TOKEN')}"
GROQ_KEY       = os.getenv("GROQ_API_KEY")
DB_URL         = os.getenv("Postgres_URL")
WEBHOOK_TOKEN  = os.getenv("BITRIX_WEBHOOK_TOKEN", "")

groq_client   = groq_sdk.Groq(api_key=GROQ_KEY)


# ── Helpers ────────────────────────────────────────────────────────────────────

def bitrix_get(method, params=""):
    r = requests.get(f"{BITRIX_BASE}/{method}", params=dict(p.split("=", 1) for p in params.split("&") if "=" in p), timeout=15)
    return r.json().get("result", {})


def get_db():
    return psycopg2.connect(DB_URL)


def ensure_table():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS call_transcripts (
                    id              SERIAL PRIMARY KEY,
                    lead_id         INTEGER,
                    lead_url        TEXT,
                    bitrix_act_id   INTEGER,
                    file_id         INTEGER,
                    call_date       TIMESTAMP,
                    phone           TEXT,
                    subject         TEXT,
                    transcript_raw  TEXT,
                    created_at      TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()


def save_transcript(lead_id, act_id, file_id, call_date, phone, subject, transcript):
    lead_url = f"{os.getenv('BITRIX_PORTAL')}/crm/lead/details/{lead_id}/" if lead_id else None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO call_transcripts
                    (lead_id, lead_url, bitrix_act_id, file_id, call_date, phone, subject, transcript_raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (lead_id, lead_url, act_id, file_id, call_date, phone, subject, transcript))
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def download_mp3(file_id):
    """Получает свежий токен и скачивает MP3."""
    file_info = bitrix_get("disk.file.get", f"id={file_id}")
    url = file_info.get("DOWNLOAD_URL", "")
    if not url:
        log.warning(f"Нет DOWNLOAD_URL для fileID={file_id}")
        return None
    r = requests.get(url, timeout=60)
    if r.status_code != 200 or len(r.content) < 5000:
        log.warning(f"Не скачался fileID={file_id}: HTTP {r.status_code}, {len(r.content)} bytes")
        return None
    return r.content


def transcribe(audio_bytes, filename="call.mp3"):
    """Отправляет аудио в Groq Whisper, возвращает текст."""
    result = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3-turbo",
        language="ru",
        response_format="text"
    )
    return str(result).strip()


def find_recording_in_lead(lead_id):
    """Ищет последний звонок с записью в лиде."""
    acts = bitrix_get("crm.activity.list",
        f"FILTER[OWNER_TYPE_ID]=1&FILTER[OWNER_ID]={lead_id}&FILTER[TYPE_ID]=2"
        f"&SELECT[]=ID&SELECT[]=SUBJECT&SELECT[]=START_TIME&SELECT[]=FILES&ORDER[START_TIME]=DESC&LIMIT=3"
    )
    if not isinstance(acts, list):
        return None
    for act in acts:
        files = act.get("FILES", [])
        if files:
            return act
    return None


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.form.to_dict()

    # Проверка токена Bitrix24
    if WEBHOOK_TOKEN and data.get("auth[application_token]") != WEBHOOK_TOKEN:
        log.warning("Неверный токен webhook — запрос отклонён")
        return jsonify({"ok": False}), 403

    event = data.get("event", "")
    log.info(f"Событие: {event} | данные: {list(data.keys())}")

    # Событие окончания звонка
    if event not in ("ONVOXIMPLANTCALLEND", "ONCRMACTIVITYADD"):
        return jsonify({"ok": True})

    # Обрабатываем в фоне — сразу отвечаем Bitrix24 чтобы не получить retry
    threading.Thread(target=process_event, args=(data,), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def process_event(data):
    # Из ONVOXIMPLANTCALLEND приходит CALL_ID и CRM_ENTITY_ID (лид)
    lead_id  = data.get("data[CRM_ENTITY_ID]") or data.get("data[LEAD_ID]")
    call_id  = data.get("data[CALL_ID]", "")
    phone    = data.get("data[PHONE_NUMBER]", "")

    log.info(f"Лид={lead_id} | phone={phone} | call_id={call_id}")

    if not lead_id:
        log.warning("Нет lead_id в событии")
        return

    # Ждём пока запись появится в Bitrix (загружается асинхронно, 30-120 сек)
    time.sleep(60)

    # Ищем активность с записью — до 3 попыток с интервалом 60 сек
    act = None
    for attempt in range(3):
        act = find_recording_in_lead(lead_id)
        if act:
            break
        log.info(f"Лид {lead_id}: запись ещё не появилась, ждём ещё 60 сек (попытка {attempt+1}/3)")
        time.sleep(60)

    if not act:
        log.warning(f"Лид {lead_id}: запись так и не появилась за 3 мин")
        return

    file_id   = act["FILES"][0]["id"]
    act_id    = act["ID"]
    subject   = act.get("SUBJECT", "")
    call_date = act.get("START_TIME", "")

    log.info(f"Скачиваю fileID={file_id}...")
    audio = download_mp3(file_id)
    if not audio:
        return

    log.info(f"Транскрибирую {len(audio)//1024}KB через Groq...")
    t0 = time.time()
    transcript = transcribe(audio)
    elapsed = time.time() - t0
    log.info(f"Готово за {elapsed:.1f}с: {len(transcript)} символов")

    row_id = save_transcript(lead_id, act_id, file_id, call_date, phone, subject, transcript)
    log.info(f"Сохранено в БД: id={row_id} | Лид #{lead_id}")


# ── Запуск ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        ensure_table()
        log.info("БД: таблица call_transcripts готова")
    except Exception as e:
        log.error(f"Не удалось подключиться к БД при старте: {e}")
    log.info("Сервер запущен. Ожидаю webhook от Bitrix24...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
