import imaplib
import email
from email.header import decode_header
import asyncio
from telegram import Bot
import re
from charset_normalizer import detect
import pickle
import os
import logging
import psutil
import gc
import tracemalloc
import sys

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация


bot = Bot(token=TOKEN)
cached_folder = None
mail_connection = None

def load_cached_folder():
    global cached_folder
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                cached_folder = pickle.load(f)
                logger.info(f"Загружен кэш: {cached_folder}")
        except Exception as e:
            logger.error(f"Ошибка загрузки кэша: {e}")
            cached_folder = None
    return cached_folder

def save_cached_folder(folder_name):
    global cached_folder
    cached_folder = folder_name
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(folder_name, f)
        logger.info(f"Кэш сохранен: {folder_name}")
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")

def extract_email_address(from_header):
    if not from_header:
        return ""
    match = re.search(r'<(.+?)>', from_header)
    return match.group(1).lower() if match else from_header.lower()

async def fetch_emails():
    global mail_connection, cached_folder
    process = psutil.Process()
    memory_usage = process.memory_info().rss / 1024 / 1024
    logger.info(f"Использование памяти: {memory_usage:.2f} MB")

    tracemalloc.start()
    start_snapshot = tracemalloc.take_snapshot()

    try:
        # Переподключение при необходимости
        if not mail_connection or not mail_connection.socket:
            if mail_connection:
                try:
                    mail_connection.logout()
                    logger.info("Предыдущее IMAP-соединение закрыто")
                except:
                    logger.error("Ошибка при закрытии предыдущего соединения")
            mail_connection = imaplib.IMAP4_SSL(IMAP_SERVER)
            mail_connection.login(EMAIL, PASSWORD)
            logger.info("Новое IMAP-соединение установлено")

        target_folder = cached_folder if cached_folder else TARGET_FOLDER
        logger.info(f"Подключаемся к папке: {target_folder}")
        status, _ = mail_connection.select(f'"{target_folder}"')
        if status != "OK":
            logger.error(f"Не удалось выбрать папку: {target_folder}. Сбрасываем кэш...")
            cached_folder = None
            save_cached_folder(None)
            if mail_connection:
                mail_connection.logout()
                mail_connection = None
            return

        logger.info(f"Используем папку: {target_folder}")

        status, messages = mail_connection.search(None, f'(UNSEEN FROM "{ALLOWED_SENDER_EMAIL}")')
        if status != "OK":
            logger.error("Ошибка при поиске писем")
            return

        email_ids = messages[0].split()[:MAX_EMAILS]  # Ограничение числа писем
        for e_id in email_ids:
            try:
                _, msg_data = mail_connection.fetch(e_id, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8", errors='ignore')

                from_ = msg.get("From", "")
                actual_email = extract_email_address(from_)
                if ALLOWED_SENDER_EMAIL.lower() not in actual_email:
                    continue

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors='ignore')

                code_match = re.search(r'Введите код:\s*(\d{6})', body)
                message_text = f"🔐 *Новый код SSPVO*\n\nКод: `{code_match.group(1)}`\n\n[Авторизация](http://10.3.60.2/account/user/20191/emailcode)" if code_match else f"✉️ *Письмо от SSPVO*\n\n{body[:500]}"  # Ограничим тело

                try:
                    sent_message = await bot.send_message(chat_id=CHAT_ID, text=message_text, parse_mode="Markdown", disable_web_page_preview=True)
                    logger.info(f"Сообщение отправлено для письма {e_id}")

                    # Планируем удаление через 15 минут
                    asyncio.create_task(delete_after_delay(sent_message.chat_id, sent_message.message_id, delay=15 * 60))
                    """await bot.send_message(chat_id=CHAT_ID, text=message_text, parse_mode="Markdown", disable_web_page_preview=True)
                    logger.info(f"Сообщение отправлено для письма {e_id}")"""
                    mail_connection.store(e_id, "+FLAGS", "\\Seen")
                except Exception as telegram_error:
                    logger.error(f"Ошибка отправки в Telegram для письма {e_id}: {telegram_error}")
                    continue

                # Освобождение памяти
                del msg, raw_email, msg_data, subject, from_, body, message_text, code_match
                gc.collect()

            except Exception as e:
                logger.error(f"Ошибка обработки письма {e_id}: {e}")
                continue

    except Exception as e:
        logger.error(f"Ошибка подключения: {e}")
        if cached_folder:
            logger.info("Сбрасываем кэш из-за ошибки подключения")
            cached_folder = None
            save_cached_folder(None)
    finally:
        if mail_connection:
            try:
                mail_connection.logout()
                mail_connection = None
                logger.info("IMAP-соединение закрыто")
            except Exception as e:
                logger.error(f"Ошибка при закрытии соединения: {e}")
            gc.collect()

    # Анализ утечек памяти
    end_snapshot = tracemalloc.take_snapshot()
    top_stats = end_snapshot.compare_to(start_snapshot, 'lineno')
    for stat in top_stats[:10]:  # Увеличим до 10 для детализации
        logger.info(f"Топ утечка памяти: {stat}")

async def delete_after_delay(chat_id, message_id, delay=300):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Удалено сообщение {message_id} из чата {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения {message_id}: {e}")

async def main():
    load_cached_folder()
    logger.info(f"Бот запущен. Проверка каждые {CHECK_INTERVAL} секунд, подключение к папке '{TARGET_FOLDER}'...")
    while True:
        await fetch_emails()
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if 'mail_connection' in globals() and mail_connection:
            try:
                mail_connection.logout()
                logger.info("IMAP-соединение закрыто при остановке")
            except:
                pass
        logger.info("Бот остановлен")