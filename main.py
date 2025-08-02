import imaplib
import email
from email.header import decode_header
import asyncio
from telegram import Bot
from telegram.ext import Application
import re
import pickle
import os
import logging
import psutil
import gc
import tracemalloc
from collections import deque
from asyncio import Lock

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = ""  # Замените на новый токен от BotFather
CHAT_ID = ""
EMAIL = ""
PASSWORD = ""
IMAP_SERVER = ""
ALLOWED_SENDER_EMAIL = ""
CACHE_FILE = "folder_cache.pkl"
QUEUE_FILE = "delete_queue.pkl"
TARGET_FOLDER = "INBOX/SSPVO"
CHECK_INTERVAL = 120  # 2 минуты
MAX_EMAILS = 3  # Максимум 3 письма за раз
DELETE_AFTER = 600  # Время жизни сообщения (10 минут)
ALLOWED_DOMAIN = ""
ALLOWED_URL_PREFIX = ""

bot = Bot(token=TOKEN)
cached_folder = None
mail_connection = None

# Очередь на удаление сообщений
delete_queue = deque()
queue_lock = Lock()

def load_cached_folder():
    global cached_folder
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                cached_folder = pickle.load(f)
                logger.info(f"Загружен кэш папки: {cached_folder}")
        except Exception as e:
            logger.error(f"Ошибка загрузки кэша папки: {e}")
            cached_folder = None
    return cached_folder

def save_cached_folder(folder_name):
    global cached_folder
    cached_folder = folder_name
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(folder_name, f)
        logger.info(f"Кэш папки сохранен: {folder_name}")
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша папки: {e}")

def load_delete_queue():
    global delete_queue
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, 'rb') as f:
                loaded_queue = pickle.load(f)
                current_time = asyncio.get_event_loop().time()
                # Фильтруем устаревшие сообщения (старше DELETE_AFTER)
                delete_queue = deque(
                    (chat_id, msg_id, timestamp)
                    for chat_id, msg_id, timestamp in loaded_queue
                    if current_time - timestamp < DELETE_AFTER
                )
                logger.info(f"Загружена очередь удаления: {len(delete_queue)} сообщений")
        except Exception as e:
            logger.error(f"Ошибка загрузки очереди удаления: {e}")
            delete_queue = deque()
    else:
        logger.info("Файл очереди удаления не найден, инициализируем пустую очередь")

def save_delete_queue():
    try:
        with open(QUEUE_FILE, 'wb') as f:
            pickle.dump(list(delete_queue), f)
        logger.info(f"Очередь удаления сохранена: {len(delete_queue)} сообщений")
    except Exception as e:
        logger.error(f"Ошибка сохранения очереди удаления: {e}")

def extract_email_address(from_header):
    if not from_header:
        return ""
    match = re.search(r'<(.+?)>', from_header)
    return match.group(1).lower() if match else from_header.lower()

async def verify_message_exists(chat_id, message_id):
    """Проверяет, существует ли сообщение в чате."""
    try:
        await bot.get_chat(chat_id=chat_id)  # Проверяем доступ к чату
        return True
    except Exception as e:
        logger.error(f"Сообщение {message_id} в чате {chat_id} недоступно: {e}")
        return False

async def fetch_emails():
    global mail_connection, cached_folder
    process = psutil.Process()
    memory_usage = process.memory_info().rss / 1024 / 1024
    logger.info(f"Использование памяти: {memory_usage:.2f} MB")

    tracemalloc.start()
    start_snapshot = tracemalloc.take_snapshot()

    try:
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

        email_ids = messages[0].split()[:MAX_EMAILS]
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
                message_text = (
                    f"🔐 *Новый код SSPVO*\n\nКод: `{code_match.group(1)}`\n\n"
                    f"[Авторизация](http://10.3.60.2/account/user/20191/emailcode)"
                    if code_match
                    else f"✉️ *Письмо от SSPVO*\n\n{body[:500]}"
                )

                try:
                    sent_message = await bot.send_message(
                        chat_id=CHAT_ID,
                        text=message_text,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                    logger.info(f"Отправлено сообщение {sent_message.message_id} для письма {e_id}")

                    async with queue_lock:
                        delete_queue.append((sent_message.chat_id, sent_message.message_id, asyncio.get_event_loop().time()))
                        save_delete_queue()

                    mail_connection.store(e_id, "+FLAGS", "\\Seen")

                except Exception as telegram_error:
                    logger.error(f"Ошибка отправки в Telegram для письма {e_id}: {telegram_error}")
                    continue

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

    end_snapshot = tracemalloc.take_snapshot()
    top_stats = end_snapshot.compare_to(start_snapshot, 'lineno')
    for stat in top_stats[:10]:
        logger.info(f"Топ утечка памяти: {stat}")

async def auto_delete_messages():
    """Удаляет сообщения из очереди, если их время жизни истекло."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        now = asyncio.get_event_loop().time()
        logger.info(f"Запуск auto_delete_messages, размер очереди: {len(delete_queue)}")
        async with queue_lock:
            logger.info(f"Содержимое delete_queue: {list(delete_queue)}")
            while delete_queue and (now - delete_queue[0][2]) >= DELETE_AFTER:
                chat_id, message_id, _ = delete_queue.popleft()
                if await verify_message_exists(chat_id, message_id):
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                        logger.info(f"🗑 Удалено сообщение {message_id}")
                    except Exception as e:
                        logger.error(f"❌ Не удалось удалить сообщение {message_id}: {e}")
                else:
                    logger.info(f"Сообщение {message_id} уже удалено или недоступно")
                save_delete_queue()
        logger.info(f"Завершение auto_delete_messages, размер очереди: {len(delete_queue)}")

async def main():
    load_cached_folder()
    load_delete_queue()
    logger.info(f"Бот запущен. Проверка каждые {CHECK_INTERVAL} секунд, подключение к папке '{TARGET_FOLDER}'...")

    app = Application.builder().token(TOKEN).build()

    await app.initialize()
    await app.start()

    asyncio.create_task(auto_delete_messages())
    asyncio.create_task(run_email_loop())

    while True:
        await asyncio.sleep(3600)

async def run_email_loop():
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
