"""
worker.py — запускается кроном каждые 5 минут.
Проверяет новые звонки cargo-менеджеров в Bitrix24,
транскрибирует и отправляет аудит в Telegram.
"""

import os, time, logging, requests, psycopg2
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import groq as groq_sdk
import subprocess

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BITRIX_BASE = (
    f"{os.getenv('BITRIX_PORTAL')}/rest/"
    f"{os.getenv('BITRIX_USER_ID')}/{os.getenv('BITRIX_TOKEN')}"
)
DB_URL     = os.getenv("Postgres_URL")
GROQ_KEY   = os.getenv("GROQ_API_KEY")

groq_client = groq_sdk.Groq(api_key=GROQ_KEY)

CARGO_MANAGERS = {
    "Говорова":  int(os.getenv("BITRIX_ID_VICTORIA_GOVOROVA", 55)),
    "Никитина":  int(os.getenv("BITRIX_ID_VICTORIA_NIKITINA", 53)),
    "Батыгина":  int(os.getenv("BITRIX_ID_MARIA_BATYGINA", 83)),
    "Михалина":  int(os.getenv("BITRIX_ID_EKATERINA_MIKHALINA", 51)),
}


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
                    created_at      TIMESTAMP DEFAULT NOW(),
                    manager_name    TEXT,
                    result_type     TEXT,
                    summary         TEXT,
                    tg_sent         BOOLEAN DEFAULT FALSE
                )
            """)
        conn.commit()


def file_already_saved(file_id: int) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM call_transcripts WHERE file_id = %s", (file_id,))
            return cur.fetchone() is not None


def save_transcript(lead_id, act_id, file_id, call_date, phone, subject, transcript) -> int:
    lead_url = f"{os.getenv('BITRIX_PORTAL')}/crm/lead/details/{lead_id}/" if lead_id else None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO call_transcripts
                    (lead_id, lead_url, bitrix_act_id, file_id, call_date, phone, subject, transcript_raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (lead_id, lead_url, act_id, file_id, call_date, phone, subject, transcript))
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def bitrix(method, params=None):
    r = requests.get(f"{BITRIX_BASE}/{method}", params=params or {}, timeout=20)
    return r.json().get("result", {})


def download_mp3(file_id: int):
    info = bitrix("disk.file.get", {"id": file_id})
    url  = info.get("DOWNLOAD_URL", "")
    if not url:
        return None
    r = requests.get(url, timeout=60)
    return r.content if r.status_code == 200 and len(r.content) > 5000 else None


def transcribe(audio: bytes) -> str:
    result = groq_client.audio.transcriptions.create(
        file=("call.mp3", audio),
        model="whisper-large-v3-turbo",
        language="ru",
        response_format="text",
    )
    return str(result).strip()


def process_lead(lead_id, phone=""):
    """Найти последний необработанный звонок с записью и обработать его."""
    acts = bitrix("crm.activity.list", {
        "FILTER[OWNER_TYPE_ID]": 1,
        "FILTER[OWNER_ID]": lead_id,
        "FILTER[TYPE_ID]": 2,
        "SELECT[]": ["ID", "START_TIME", "FILES", "SUBJECT", "COMMUNICATIONS"],
        "ORDER[START_TIME]": "DESC",
        "LIMIT": 5,
    })
    if not isinstance(acts, list):
        return

    for act in acts:
        files = act.get("FILES", [])
        if not files:
            continue
        file_id = files[0]["id"]
        if file_already_saved(file_id):
            continue

        if not phone:
            for c in act.get("COMMUNICATIONS", []):
                phone = c.get("VALUE", "")
                break

        log.info(f"Лид {lead_id}: скачиваю fileID={file_id}")
        audio = download_mp3(file_id)
        if not audio:
            log.warning(f"Лид {lead_id}: не удалось скачать MP3")
            return

        t0 = time.time()
        transcript = transcribe(audio)
        log.info(f"Транскрипция: {len(transcript)} симв. за {time.time()-t0:.1f}с")

        subject = act.get("SUBJECT", "")
        call_date = act.get("START_TIME", "")
        row_id = save_transcript(lead_id, act["ID"], file_id, call_date, phone, subject, transcript)
        log.info(f"Сохранено id={row_id}")

        # Запускаем аудит для этой записи
        audit_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.py")
        subprocess.Popen(
            ["python3", audit_path, "--id", str(row_id)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return  # обрабатываем только последний необработанный звонок


def run():
    ensure_table()

    # Ищем лиды cargo-менеджеров за последние 60 минут
    since = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S")
    log.info(f"Проверяю звонки с {since}")

    batch_cmd = {}
    for name, uid in CARGO_MANAGERS.items():
        batch_cmd[f"leads_{uid}"] = (
            f"crm.lead.list?FILTER[ASSIGNED_BY_ID]={uid}"
            f"&FILTER[>DATE_MODIFY]={since}&SELECT[]=ID&ORDER[ID]=DESC&LIMIT=20"
        )
    batch_cmd["leads_new"] = (
        f"crm.lead.list?FILTER[STATUS_ID]=NEW"
        f"&FILTER[>DATE_MODIFY]={since}&SELECT[]=ID&ORDER[ID]=DESC&LIMIT=20"
    )

    try:
        r = requests.post(
            f"{BITRIX_BASE}/batch",
            json={"halt": 0, "cmd": batch_cmd},
            timeout=30
        )
        results = r.json().get("result", {}).get("result", {})
    except Exception as e:
        log.error(f"Batch ошибка: {e}")
        return

    seen = set()
    lead_ids = []
    for leads in results.values():
        if isinstance(leads, list):
            for l in leads:
                if l["ID"] not in seen:
                    seen.add(l["ID"])
                    lead_ids.append(l["ID"])

    if not lead_ids:
        log.info("Новых лидов нет")
        return

    log.info(f"Проверяю {len(lead_ids)} лидов")
    for lid in lead_ids:
        try:
            process_lead(lid)
        except Exception as e:
            log.error(f"Ошибка обработки лида {lid}: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    run()
