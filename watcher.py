"""
call-audit / watcher.py
Два канала получения звонков из Bitrix24:
  1. Webhook ONVOXIMPLANTCALLEND — основной, срабатывает сразу после звонка
  2. Polling каждые 3 минуты — резерв на случай если webhook не пришёл
После транскрипции сразу запускает audit.py --id <N> → отчёт в Telegram.
"""

import os, time, logging, requests, psycopg2, threading, subprocess
from datetime import datetime, timedelta, timezone
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

BITRIX_BASE   = f"{os.getenv('BITRIX_PORTAL')}/rest/{os.getenv('BITRIX_USER_ID')}/{os.getenv('BITRIX_TOKEN')}"
GROQ_KEY      = os.getenv("GROQ_API_KEY")
DB_URL        = os.getenv("Postgres_URL")
WEBHOOK_TOKEN = os.getenv("BITRIX_WEBHOOK_TOKEN", "")

groq_client = groq_sdk.Groq(api_key=GROQ_KEY)

# Cargo-менеджеры которых мониторим
CARGO_MANAGERS = {
    "Говорова":  int(os.getenv("BITRIX_ID_VICTORIA_GOVOROVA", 55)),
    "Никитина":  int(os.getenv("BITRIX_ID_VICTORIA_NIKITINA", 53)),
    "Батыгина":  int(os.getenv("BITRIX_ID_MARIA_BATYGINA", 83)),
    "Михалина":  int(os.getenv("BITRIX_ID_EKATERINA_MIKHALINA", 51)),
}

# Карта добавочных номеров → Имя Фамилия (из .env)
EXTENSION_TO_NAME = {
    "703": "Мария Батыгина",
    "704": "Виктория Говорова",
    "705": "Екатерина Михалина",
    "706": "Елена Полетаева",
    "707": "Виктория Никитина",
    "700": "Дмитрий Каменев",
    "701": "Константин Прокофьев",
    "702": "Дмитрий Александров",
    "711": "Виталий Вронский",
    "200": "Ксения",
    "101": "Елена Кизявка",
    "103": "Елизавета Елизарова",
    "105": "Татьяна Матюшина",
    "107": "Олеся Лутонина",
    "108": "Юлия Кирюшина",
    "109": "Александр Баранов",
    "111": "Александр Березнев",
    "116": "Ирина Коростелева",
    "120": "Наталья Лыкошева",
    "124": "Людмила Медведева",
    "222": "Ольга Марилова",
    "332": "Дмитрий Березнев",
    "351": "Елизавета Балашова",
}


# ── DB ─────────────────────────────────────────────────────────────────────────

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


def file_already_saved(file_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM call_transcripts WHERE file_id=%s", (file_id,))
            return cur.fetchone() is not None


def save_transcript(lead_id, act_id, file_id, call_date, phone, subject, transcript, manager_name=None):
    lead_url = f"{os.getenv('BITRIX_PORTAL')}/crm/lead/details/{lead_id}/" if lead_id else None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO call_transcripts
                    (lead_id, lead_url, bitrix_act_id, file_id, call_date, phone, subject, transcript_raw, manager_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (lead_id, lead_url, act_id, file_id, call_date, phone, subject, transcript, manager_name))
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


# ── Bitrix API ─────────────────────────────────────────────────────────────────

def bitrix(method, params):
    r = requests.get(f"{BITRIX_BASE}/{method}", params=params, timeout=20)
    return r.json().get("result", {})


def download_mp3(file_id):
    info = bitrix("disk.file.get", {"id": file_id})
    url = info.get("DOWNLOAD_URL", "")
    if not url:
        return None
    r = requests.get(url, timeout=60)
    if r.status_code != 200 or len(r.content) < 5000:
        return None
    return r.content


def transcribe(audio_bytes, filename="call.mp3"):
    result = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3-turbo",
        language="ru",
        response_format="text"
    )
    return str(result).strip()


def process_call(lead_id, act_id, file_id, call_date, phone, subject, manager_name=None):
    """Скачивает, транскрибирует и сохраняет один звонок."""
    if file_already_saved(file_id):
        return

    audio = download_mp3(file_id)
    if not audio:
        log.warning(f"fileID={file_id}: не удалось скачать")
        return

    t0 = time.time()
    transcript = transcribe(audio, f"{file_id}.mp3")
    log.info(f"fileID={file_id}: {len(audio)//1024}KB → {len(transcript)} символов за {time.time()-t0:.1f}с")

    row_id = save_transcript(lead_id, act_id, file_id, call_date, phone, subject, transcript, manager_name)
    log.info(f"Сохранено в БД id={row_id} | Лид #{lead_id} | {call_date} | Менеджер: {manager_name or '—'}")

    # Запускаем аудит конкретной записи в фоне (audit.py лежит рядом)
    audit_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.py")
    subprocess.Popen(
        ["python3", audit_script, "--id", str(row_id)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    log.info(f"Запущен аудит для id={row_id}")


# ── Polling ────────────────────────────────────────────────────────────────────

def poll_new_calls():
    """Проверяет новые звонки за последние 60 минут — перекрывает рестарты контейнера."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S")
    log.info(f"Polling: ищем звонки с {since}")

    # Batch-запрос: лиды менеджеров + необработанные (STATUS_ID=NEW)
    batch_cmd = {}
    for name, uid in CARGO_MANAGERS.items():
        batch_cmd[f"leads_{uid}"] = (
            f"crm.lead.list?FILTER[ASSIGNED_BY_ID]={uid}"
            f"&FILTER[>DATE_MODIFY]={since}&SELECT[]=ID&ORDER[ID]=DESC&LIMIT=10"
        )
    # Необработанные лиды — входящие звонки ещё без менеджера
    batch_cmd["leads_new"] = (
        f"crm.lead.list?FILTER[STATUS_ID]=NEW"
        f"&FILTER[>DATE_MODIFY]={since}&SELECT[]=ID&ORDER[ID]=DESC&LIMIT=20"
    )

    try:
        r = requests.post(f"{BITRIX_BASE}/batch", json={"halt": 0, "cmd": batch_cmd}, timeout=30)
        results = r.json().get("result", {}).get("result", {})
    except Exception as e:
        log.error(f"Polling batch ошибка: {e}")
        return

    lead_ids = []
    seen = set()
    for key, leads in results.items():
        if isinstance(leads, list):
            for l in leads:
                if l["ID"] not in seen:
                    seen.add(l["ID"])
                    lead_ids.append(l["ID"])

    if not lead_ids:
        log.info("Polling: новых лидов нет")
        return

    log.info(f"Polling: проверяем {len(lead_ids)} лидов")

    # Batch: активности по всем найденным лидам
    act_cmd = {}
    for lid in lead_ids:
        act_cmd[f"act_{lid}"] = (
            f"crm.activity.list?FILTER[OWNER_TYPE_ID]=1&FILTER[OWNER_ID]={lid}"
            f"&FILTER[TYPE_ID]=2&FILTER[>START_TIME]={since}"
            f"&SELECT[]=ID&SELECT[]=START_TIME&SELECT[]=FILES&SELECT[]=SUBJECT&SELECT[]=COMMUNICATIONS"
        )

    try:
        r = requests.post(f"{BITRIX_BASE}/batch", json={"halt": 0, "cmd": act_cmd}, timeout=30)
        act_results = r.json().get("result", {}).get("result", {})
    except Exception as e:
        log.error(f"Polling activities ошибка: {e}")
        return

    for lid in lead_ids:
        acts = act_results.get(f"act_{lid}", []) or []
        for act in acts:
            files = act.get("FILES", [])
            if not files:
                continue
            file_id = files[0]["id"]
            comms = act.get("COMMUNICATIONS", [])
            phone = comms[0].get("VALUE", "") if comms else ""
            threading.Thread(
                target=process_call,
                args=(lid, act["ID"], file_id, act.get("START_TIME"), phone, act.get("SUBJECT", "")),
                daemon=True
            ).start()


def polling_loop():
    """Фоновый поток: polling каждые 3 минуты (резерв если webhook не сработал)."""
    while True:
        try:
            poll_new_calls()
        except Exception as e:
            log.error(f"Polling loop ошибка: {e}")
        time.sleep(180)  # 3 минуты


def process_call_by_lead(lead_id: str, phone: str = "", manager_name: str = None):
    """Найти последний звонок с записью в лиде и обработать его."""
    acts = bitrix("crm.activity.list", {
        "FILTER[OWNER_TYPE_ID]": 1, "FILTER[OWNER_ID]": lead_id, "FILTER[TYPE_ID]": 2,
        "SELECT[]": ["ID", "START_TIME", "FILES", "SUBJECT", "COMMUNICATIONS"],
        "ORDER[START_TIME]": "DESC", "LIMIT": 5,
    })
    if not isinstance(acts, list):
        log.warning(f"process_call_by_lead {lead_id}: нет активностей")
        return
    for act in acts:
        if act.get("FILES"):
            if not phone:
                for c in act.get("COMMUNICATIONS", []):
                    phone = c.get("VALUE", "")
                    break
            process_call(lead_id, act["ID"], act["FILES"][0]["id"],
                         act.get("START_TIME"), phone, act.get("SUBJECT", ""), manager_name)
            return
    log.warning(f"process_call_by_lead {lead_id}: звонков с записью не найдено")


# ── Webhook endpoint (резервный) ───────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.form.to_dict()

    if WEBHOOK_TOKEN and data.get("auth[application_token]") != WEBHOOK_TOKEN:
        log.warning("Неверный токен webhook")
        return jsonify({"ok": False}), 403

    event = data.get("event", "")
    log.info(f"Webhook: {event}")

    if event in ("ONVOXIMPLANTCALLEND", "ONCRMACTIVITYADD"):
        threading.Thread(target=process_webhook_event, args=(data,), daemon=True).start()

    return jsonify({"ok": True})


def process_webhook_event(data):
    lead_id = data.get("data[CRM_ENTITY_ID]") or data.get("data[LEAD_ID]")
    phone   = data.get("data[PHONE_NUMBER]", "")
    if not lead_id:
        return
    # Bitrix24 обрабатывает запись ~15-30 сек после окончания звонка
    log.info(f"Webhook: лид={lead_id}, ждём 30 сек пока появится запись...")
    time.sleep(30)
    for attempt in range(6):
        try:
            acts = bitrix("crm.activity.list", {
                "FILTER[OWNER_TYPE_ID]": 1, "FILTER[OWNER_ID]": lead_id,
                "FILTER[TYPE_ID]": 2, "SELECT[]": ["ID","START_TIME","FILES","SUBJECT"],
                "ORDER[START_TIME]": "DESC", "LIMIT": 3,
            })
            if isinstance(acts, list):
                for act in acts:
                    if act.get("FILES"):
                        file_id = act["FILES"][0]["id"]
                        process_call(lead_id, act["ID"], file_id, act.get("START_TIME"), phone, act.get("SUBJECT",""))
                        return
        except Exception as e:
            log.warning(f"Webhook поиск записи попытка {attempt+1}: {e}")
        time.sleep(30)
    log.warning(f"Webhook: запись для лида {lead_id} не появилась за 3 мин")


@app.route("/poll", methods=["GET", "POST"])
def poll_now():
    """Ручной запуск polling (для тестирования)."""
    threading.Thread(target=poll_new_calls, daemon=True).start()
    return jsonify({"ok": True, "message": "polling started"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/novofon", methods=["GET", "POST"])
def novofon_webhook():
    """Новофон: HTTP-уведомление о завершении звонка."""
    if request.form:
        data = request.form.to_dict()
    else:
        try:
            import json as _json
            data = _json.loads(request.data or b"{}")
        except Exception:
            data = {}
    log.info(f"Новофон webhook: {data}")

    # Новофон шлёт вложенный JSON: телефон и доб. внутри contact_info
    contact = data.get("contact_info") or {}
    employee = data.get("employee_info") or {}
    call_info = data.get("call_info") or {}

    phone = contact.get("contact_phone_number") or data.get("contact_phone_number", "")
    ext   = str(contact.get("communication_number") or data.get("communication_number") or "").strip()

    # Пропускаем переадресации без разговора (nobody talked)
    if call_info.get("talk_time_duration") == 0 and call_info.get("scenario_name") == "переадресация":
        log.info(f"Новофон: пропускаем переадресацию без разговора (phone={phone})")
        return jsonify({"ok": True, "skipped": "redirect_no_talk"})

    if not phone:
        log.warning(f"Новофон: нет телефона в данных: {data}")
        return jsonify({"ok": True, "skipped": "no_phone"})

    # Имя менеджера: сначала из employee_info, потом из добавочного номера
    manager_name = (
        employee.get("employee_full_name")
        or EXTENSION_TO_NAME.get(ext)
    )
    log.info(f"Новофон: звонок завершён. Клиент={phone}, доб.={ext} → {manager_name or 'неизвестен'}")

    phone_clean = phone.replace(" ", "").replace("-", "")

    def find_and_process():
        time.sleep(30)
        try:
            leads = bitrix("crm.lead.list", {
                "FILTER[PHONE]": phone_clean,
                "SELECT[]": ["ID"],
                "ORDER[ID]": "DESC",
                "LIMIT": 3,
            })
            if isinstance(leads, list) and leads:
                lead_id = leads[0]["ID"]
                log.info(f"Новофон: лид {lead_id} по телефону {phone_clean}, менеджер={manager_name}")
                process_call_by_lead(str(lead_id), phone_clean, manager_name)
            else:
                log.warning(f"Новофон: лид по телефону {phone_clean} не найден")
        except Exception as e:
            log.error(f"Новофон find_and_process ошибка: {e}")

    threading.Thread(target=find_and_process, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/process-lead", methods=["GET", "POST"])
def process_lead_endpoint():
    """Ручная обработка конкретного лида: /process-lead?lead_id=7771"""
    lead_id = request.args.get("lead_id") or (request.json or {}).get("lead_id")
    if not lead_id:
        return jsonify({"ok": False, "error": "lead_id required"}), 400
    phone = request.args.get("phone", "")
    threading.Thread(
        target=process_call_by_lead,
        args=(str(lead_id), phone),
        daemon=True
    ).start()
    return jsonify({"ok": True, "message": f"Processing lead {lead_id}"})


# ── Запуск ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        ensure_table()
        log.info("БД готова")
    except Exception as e:
        log.error(f"Ошибка БД при старте: {e}")

    # Запускаем polling в фоне
    threading.Thread(target=polling_loop, daemon=True).start()
    log.info("Polling запущен (каждые 10 мин). Сервер слушает webhook...")

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
