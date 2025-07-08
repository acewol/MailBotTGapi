import email
import imaplib
from email.header import decode_header
import schedule
import time
from telegram import Bot

# Конфигурация
TOKEN = ""
CHAT_ID = ""
EMAIL = ""
PASSWORD = ""
IMAP_SERVER = "outlook.office365.com"

bot = Bot(token=TOKEN)

def fetch_emails():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL, PASSWORD)
    mail.select("inbox")

    status, message = mail.search(None, "(UNSEEN)")
    if status != "OK":
        return


    email_ids = message[0].split()
    for e_id in email_ids:
        _, msg_data = mail.fetch(e_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8")

        from_ = msg.get("From")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode()
                    break
        else:
            body = msg.get_payload(decode=True).decode()

        message = f"*Пришел код из Outlook*\n\n*От:* {from_}\n*Тема:* {subject}\n\n{body}"
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")

        mail.store(e_id, "+FLAGS", "\\Seen") # Пометка как прочитанное

    mail.close()
    mail.logout()


# Проверяем почту каждые 5 минут
schedule.every(5).minutes.do(fetch_emails)

while True:
    schedule.run_pending()
    time.sleep(1)

