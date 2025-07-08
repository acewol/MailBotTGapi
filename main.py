import email
import imaplib
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
