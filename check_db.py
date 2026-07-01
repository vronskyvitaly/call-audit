#!/usr/bin/env python3
"""
Мониторинг новых расшифровок звонков в call_transcripts.
Каждые 30 минут пн-пт 9-18 МСК шлёт письмо с новыми записями.
Работает пока не удалят вручную из crontab.
"""
import os, sys, subprocess, smtplib, ssl, psycopg2
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

DB_URL       = os.getenv("Postgres_URL") or "postgres://postgres:trLNB8TjhnFqZwOftVohdJmN8YWsFaIFRE79bFYE9xLCPBYbk4G7l9HwtOe6il66@213.136.66.25:5439/postgres"
SMTP_HOST    = "smtp.yandex.ru"
SMTP_PORT    = 465
SMTP_USER    = "office@tamozhennyy.broker"
SMTP_PASS    = "rjdzrmhxdueixfuk"
TO_EMAIL     = "vronskyvitaly@mail.ru"
LAST_ID_FILE = "/app/check_db_last_id.txt"

MSK = timezone(timedelta(hours=3))

def is_working_hours():
    now = datetime.now(MSK)
    return now.weekday() < 5 and 9 <= now.hour < 18

def get_last_sent_id():
    try:
        return int(open(LAST_ID_FILE).read().strip())
    except:
        return 0

def save_last_id(last_id):
    open(LAST_ID_FILE, "w").write(str(last_id))

def check_db(since_id):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM call_transcripts")
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT id, lead_id, phone, call_date, transcript_raw, created_at
        FROM call_transcripts
        WHERE id > %s
        ORDER BY id ASC
    """, (since_id,))
    new_rows = cur.fetchall()
    conn.close()
    return total, new_rows

def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"]    = SMTP_USER
    msg["To"]      = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, TO_EMAIL, msg.as_string())

if __name__ == "__main__":
    if not is_working_hours():
        sys.exit(0)

    now_msk = datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")
    last_id = get_last_sent_id()

    try:
        total, new_rows = check_db(last_id)

        if new_rows:
            subject = f"🆕 SEO-агент: {len(new_rows)} новых расшифровок | {now_msk}"
            parts = [f"Новые расшифровки звонков [{now_msk}]\nВсего в БД: {total}\n"]
            parts.append("=" * 60)

            for row in new_rows:
                rid, lead_id, phone, call_date, transcript, created_at = row
                parts.append(f"\n📞 Звонок ID={rid} | Лид #{lead_id}")
                parts.append(f"   Телефон:    {phone}")
                parts.append(f"   Дата:       {call_date}")
                parts.append(f"   Добавлено:  {created_at}")
                parts.append(f"   Лид в CRM:  https://royalcargo.bitrix24.ru/crm/lead/details/{lead_id}/")
                parts.append(f"\n   Расшифровка:")
                parts.append(f"   {(transcript or '').strip()}")
                parts.append("\n" + "-" * 60)

            send_email(subject, "\n".join(parts))
            save_last_id(new_rows[-1][0])

        else:
            subject = f"⏳ SEO-агент: новых расшифровок нет | {now_msk}"
            body = f"Проверка БД [{now_msk}]\n\nНовых записей нет. Всего в БД: {total}"
            send_email(subject, body)

    except Exception as e:
        send_email("❌ SEO-агент: ошибка проверки БД", f"Ошибка при проверке: {e}")
