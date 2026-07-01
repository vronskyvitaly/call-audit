"""
Аудит звонков: читает расшифровки из БД, анализирует через Claude,
отправляет отчёт руководителю в Telegram.
Запускается вручную или по расписанию.
"""

import os, json, time, psycopg2, anthropic, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_URL        = "postgres://postgres:trLNB8TjhnFqZwOftVohdJmN8YWsFaIFRE79bFYE9xLCPBYbk4G7l9HwtOe6il66@213.136.66.25:5439/postgres"
CLAUDE_KEY    = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL  = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
TG_TOKEN      = os.getenv("TG_BOT_TOKEN_AUDIT")
# Получатели отчётов: разработчик + Александр (руководитель)
# Александр должен написать /start боту @ROYALAGENT011_bot прежде чем начнёт получать сообщения
TG_RECIPIENTS = ["8055160350", "1343266643", "5602533893"]
BITRIX_PORTAL = os.getenv("BITRIX_PORTAL", "https://royalcargo.bitrix24.ru")

MSK = timezone(timedelta(hours=3))

RESULT_EMOJI = {
    "green":  "🟢",
    "yellow": "🟡",
    "red":    "🔴",
}
RESULT_LABEL = {
    "green":  "Отличный результат",
    "yellow": "Непонятно / нет результата",
    "red":    "Клиент ушёл",
}


def get_db():
    return psycopg2.connect(DB_URL)


def extract_manager(subject: str) -> str:
    if subject and "|" in subject:
        return subject.split("|")[0].strip()
    return "Неизвестен"


def analyze_call(transcript: str, subject: str) -> dict:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)

    prompt = f"""Ты — аудитор качества звонков таможенного брокера. Контекст: компания занимается таможенным оформлением грузов.

Расшифровка звонка (тема: {subject}):
---
{transcript}
---

Верни ТОЛЬКО валидный JSON без markdown-блоков:
{{
  "manager_name": "имя менеджера из текста (только имя, например 'Екатерина'). Если не определить — null",
  "result": "green" или "yellow" или "red",
  "summary": "краткое резюме (1-2 предложения)",
  "reason": "почему такая оценка"
}}

Критерии:
- green: менеджер профессионально помог клиенту, достигнут позитивный результат
- yellow: непонятен итог (перевод на коллегу без финала, слишком короткий звонок, технические проблемы)
- red: клиент отказался, ушёл, ситуация разрешилась негативно"""

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()
    # Вырезаем JSON на случай если Claude обернул его в ```
    start = text.find("{")
    end   = text.rfind("}") + 1
    return json.loads(text[start:end])


def format_tg_message(row: dict, analysis: dict) -> str:
    emoji  = RESULT_EMOJI.get(analysis["result"], "⚪")
    label  = RESULT_LABEL.get(analysis["result"], analysis["result"])
    mgr    = row["manager_name"] or "Неизвестен"
    phone  = row["phone"] or "не указан"
    lead_id = row["lead_id"]

    # Дата в МСК
    call_dt = row["call_date"]
    if call_dt:
        if hasattr(call_dt, "tzinfo") and call_dt.tzinfo is None:
            call_dt = call_dt.replace(tzinfo=MSK)
        call_dt_msk = call_dt.astimezone(MSK)
        date_str = call_dt_msk.strftime("%d.%m.%Y %H:%M МСК")
    else:
        date_str = "не указана"

    # Ссылка на лид
    if lead_id and row.get("lead_url"):
        lead_link = f'<a href="{row["lead_url"]}">Лид #{lead_id}</a>'
    elif lead_id:
        lead_link = f'<a href="{BITRIX_PORTAL}/crm/lead/details/{lead_id}/">Лид #{lead_id}</a>'
    else:
        lead_link = "нет лида"

    transcript = (row.get("transcript_raw") or "").strip()

    header = (
        f"{emoji} <b>{label}</b>\n"
        f"\n"
        f"👤 <b>Менеджер:</b> {mgr}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"🔗 <b>Лид:</b> {lead_link}\n"
        f"\n"
        f"📝 <b>Резюме:</b> {analysis['summary']}\n"
        f"<i>Причина оценки: {analysis['reason']}</i>\n"
        f"\n"
        f"💬 <b>Расшифровка:</b>\n"
        f"{transcript}\n"
        f"\n"
        f"<i>Причина оценки: {analysis['reason']}</i>"
    )
    return header


def send_tg(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Telegram лимит — 4096 символов. Разбиваем на части по абзацам.
    limit = 4096
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)

    ok_all = True
    for chat_id in TG_RECIPIENTS:
        for part in parts:
            r = requests.post(url, json={
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
            if not r.ok:
                ok_all = False
            if len(parts) > 1:
                time.sleep(0.3)
    return ok_all


def run_audit(only_new: bool = True):
    """
    only_new=True  — обрабатывать только записи у которых tg_sent=FALSE
    only_new=False — переобработать все записи
    """
    conn = get_db()
    cur  = conn.cursor()

    if only_new:
        cur.execute("""
            SELECT id, lead_id, lead_url, phone, call_date, subject, transcript_raw, manager_name
            FROM call_transcripts
            WHERE tg_sent = FALSE OR tg_sent IS NULL
            ORDER BY id ASC
        """)
    else:
        cur.execute("""
            SELECT id, lead_id, lead_url, phone, call_date, subject, transcript_raw, manager_name
            FROM call_transcripts
            ORDER BY id ASC
        """)

    rows = cur.fetchall()
    print(f"Найдено записей для обработки: {len(rows)}")

    if not rows:
        print("Нет новых записей.")
        conn.close()
        return

    # Шапка аудита
    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")
    header = (
        f"📊 <b>Аудит звонков</b>\n"
        f"Дата отчёта: {now_msk}\n"
        f"Всего звонков: {len(rows)}"
    )
    send_tg(header)
    time.sleep(1)

    for r in rows:
        rid, lead_id, lead_url, phone, call_date, subject, transcript, db_manager = r

        row_dict = {
            "id": rid,
            "lead_id": lead_id,
            "lead_url": lead_url,
            "phone": phone,
            "call_date": call_date,
            "subject": subject,
            "manager_name": db_manager or extract_manager(subject),
            "transcript_raw": transcript,
        }

        transcript = (transcript or "").strip()
        if not transcript:
            # Пустая расшифровка — помечаем yellow
            analysis = {
                "result":  "yellow",
                "summary": "Расшифровка отсутствует или пустая.",
                "reason":  "Нет текста для анализа",
            }
        else:
            try:
                analysis = analyze_call(transcript, subject or "")
            except Exception as e:
                print(f"  Ошибка анализа ID={rid}: {e}")
                analysis = {
                    "result":  "yellow",
                    "summary": "Не удалось выполнить анализ.",
                    "reason":  str(e)[:200],
                }

        # Приоритет: Новофон (db_manager) > Claude > subject
        manager_name = db_manager or analysis.get("manager_name") or row_dict["manager_name"]
        row_dict["manager_name"] = manager_name

        # Сохраняем анализ в БД (tg_sent пока FALSE)
        cur.execute("""
            UPDATE call_transcripts
            SET manager_name = %s,
                result_type  = %s,
                summary      = %s
            WHERE id = %s
        """, (manager_name, analysis["result"], analysis["summary"], rid))
        conn.commit()

        # Отправляем в Telegram — ставим tg_sent=TRUE только если дошло
        msg = format_tg_message(row_dict, analysis)
        ok  = send_tg(msg)
        if ok:
            cur.execute("UPDATE call_transcripts SET tg_sent = TRUE WHERE id = %s", (rid,))
            conn.commit()
        status = "✓" if ok else "✗ ошибка TG"
        print(f"  ID={rid} {analysis['result']:6} {status} — {subject}")

        time.sleep(1.5)  # пауза между сообщениями

    conn.close()
    print("\nАудит завершён.")


def run_single(record_id: int):
    """Обработать одну конкретную запись по ID (вызывается из watcher.py)."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, lead_id, lead_url, phone, call_date, subject, transcript_raw, manager_name
        FROM call_transcripts WHERE id = %s
    """, (record_id,))
    row = cur.fetchone()
    if not row:
        print(f"Запись ID={record_id} не найдена.")
        conn.close()
        return

    rid, lead_id, lead_url, phone, call_date, subject, transcript, db_manager = row
    row_dict = {
        "id": rid, "lead_id": lead_id, "lead_url": lead_url,
        "phone": phone, "call_date": call_date, "subject": subject,
        "manager_name": db_manager or extract_manager(subject),
        "transcript_raw": transcript,
    }

    transcript = (transcript or "").strip()
    if not transcript:
        analysis = {"result": "yellow", "summary": "Расшифровка пустая.", "reason": "Нет текста"}
    else:
        try:
            analysis = analyze_call(transcript, subject or "")
        except Exception as e:
            analysis = {"result": "yellow", "summary": "Ошибка анализа.", "reason": str(e)[:200]}

    # Приоритет: Новофон (db_manager) > Claude > subject
    manager_name = db_manager or analysis.get("manager_name") or row_dict["manager_name"]
    row_dict["manager_name"] = manager_name

    cur.execute("""
        UPDATE call_transcripts
        SET manager_name = %s, result_type = %s, summary = %s
        WHERE id = %s
    """, (manager_name, analysis["result"], analysis["summary"], rid))
    conn.commit()

    msg = format_tg_message(row_dict, analysis)
    ok  = send_tg(msg)
    if ok:
        cur.execute("UPDATE call_transcripts SET tg_sent = TRUE WHERE id = %s", (rid,))
        conn.commit()
    conn.close()
    print(f"ID={rid} {analysis['result']} {'✓' if ok else '✗ ошибка TG'} — {subject}")


if __name__ == "__main__":
    import sys
    if "--id" in sys.argv:
        idx = sys.argv.index("--id")
        run_single(int(sys.argv[idx + 1]))
    elif "--all" in sys.argv:
        run_audit(only_new=False)
    else:
        run_audit(only_new=True)
