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

# === Конфигурация ===
TOKEN = os.getenv("TELEGRAM_TOKEN", "")       # лучше через env
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
EMAIL = os.getenv("IMAP_USER", "")
PASSWORD = os.getenv("IMAP_PASS", "")
IMAP_SERVER = os.getenv("IMAP_SERVER", "")
ALLOWED_SENDER_EMAIL = os.getenv("ALLOWED_SENDER_EMAIL", "").lower()
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "").lower()
ALLOWED_URL_PREFIX = os.getenv("ALLOWED_URL_PREFIX", "")
CACHE_FILE = "folder_cache.pkl"
QUEUE_FILE = "delete_queue.pkl"
TARGET_FOLDER = "INBOX/SSPVO"
CHECK_INTERVAL = 120    # 2 минуты
MAX_EMAILS = 3          # не более 3 за раз
DELETE_AFTER = 600      # удалять через 10 минут

bot = Bot(token=TOKEN)
cached_folder = None
mail_connection = None

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
        logger.info(f"Кэш папки сохранён: {folder_name}")
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша папки: {e}")

def load_delete_queue():
    global delete_queue
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, 'rb') as f:
                items = pickle.load(f)
            now = asyncio.get_event_loop().time()
            delete_queue = deque(
                (cid, mid, ts)
                for cid, mid, ts in items
                if now - ts < DELETE_AFTER
            )
            logger.info(f"Загружена очередь удаления: {len(delete_queue)} сообщений")
        except Exception as e:
            logger.error(f"Ошибка загрузки очереди удаления: {e}")
            delete_queue = deque()
    else:
        logger.info("Очередь удаления не найдена — инициализируем пустую")

def save_delete_queue():
    try:
        with open(QUEUE_FILE, 'wb') as f:
            pickle.dump(list(delete_queue), f)
        logger.info(f"Очередь удаления сохранена: {len(delete_queue)} сообщений")
    except Exception as e:
        logger.error(f"Ошибка сохранения очереди удаления: {e}")

def extract_email_address(from_header: str) -> str:
    match = re.search(r'<(.+?)>', from_header)
    return match.group(1).lower() if match else from_header.lower()

async def verify_message_exists(chat_id, message_id):
    try:
        await bot.get_chat(chat_id=chat_id)
        return True
    except Exception as e:
        logger.debug(f"Чат {chat_id} недоступен: {e}")
        return False

async def fetch_emails():
    global mail_connection, cached_folder

    # Профилинг памяти
    process = psutil.Process()
    logger.info(f"Использование памяти: {process.memory_info().rss/1024/1024:.2f} MB")
    tracemalloc.start()
    start_snapshot = tracemalloc.take_snapshot()

    try:
        # (Re)connect IMAP
        if not mail_connection or not getattr(mail_connection, "socket", None):
            if mail_connection:
                try:
                    mail_connection.logout()
                except: pass
            mail_connection = imaplib.IMAP4_SSL(IMAP_SERVER)
            mail_connection.login(EMAIL, PASSWORD)
            logger.info("IMAP-соединение установлено")

        folder = cached_folder or TARGET_FOLDER
        status, _ = mail_connection.select(f'"{folder}"')
        if status != "OK":
            logger.warning(f"Не удалось выбрать папку {folder}, сброс кэша")
            save_cached_folder(None)
            mail_connection.logout()
            mail_connection = None
            return

        # Ищем непрочитанные от нужного From
        status, data = mail_connection.search(None, f'(UNSEEN FROM "{ALLOWED_SENDER_EMAIL}")')
        if status != "OK":
            logger.error("IMAP search failed")
            return

        for e_id in data[0].split()[:MAX_EMAILS]:
            try:
                _, msg_data = mail_connection.fetch(e_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                # 1) Жёсткая проверка Return-Path
                rp = msg.get("Return-Path", "")
                if ALLOWED_DOMAIN not in rp.lower():
                    logger.warning(f"Письмо {e_id} с Return-Path {rp} — пропускаем")
                    mail_connection.store(e_id, "+FLAGS", "\\Seen")
                    continue

                # 2) Парсим тело
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="ignore")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")

                # 3) Ищем строго код
                code_match = re.search(r'Введите код:\s*(\d{6})\b', body)
                if not code_match:
                    logger.warning(f"Письмо {e_id} без корректного кода — пропускаем")
                    mail_connection.store(e_id, "+FLAGS", "\\Seen")
                    continue
                code = code_match.group(1)

                # 4) Фильтрация URL
                urls = re.findall(r'https?://\S+', body)
                if any(not url.startswith(ALLOWED_URL_PREFIX) for url in urls):
                    logger.warning(f"Письмо {e_id} содержит посторонние ссылки {urls} — пропускаем")
                    mail_connection.store(e_id, "+FLAGS", "\\Seen")
                    continue

                # Всё OK — отправляем в Telegram
                text = (
                    f"🔐 *Новый код SSPVO*\n\n"
                    f"Код: `{code}`\n\n"
                    f"[Авторизация]({ALLOWED_URL_PREFIX}account/user/20191/emailcode)"
                )
                sent = await bot.send_message(
                    chat_id=CHAT_ID,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                logger.info(f"Отправлено сообщение {sent.message_id} для письма {e_id}")

                # Помечаем прочитанным и ставим в очередь на удаление
                mail_connection.store(e_id, "+FLAGS", "\\Seen")
                async with queue_lock:
                    delete_queue.append((sent.chat_id, sent.message_id, asyncio.get_event_loop().time()))
                    save_delete_queue()

            except Exception as inner:
                logger.error(f"Ошибка обработки письма {e_id}: {inner}")
                continue

    except Exception as e:
        logger.error(f"Ошибка в fetch_emails: {e}")
        save_cached_folder(None)  # сбросим кэш, если что-то упало
    finally:
        if mail_connection:
            try:
                mail_connection.logout()
                mail_connection = None
            except: pass

        # Логи утечек памяти
        end_snapshot = tracemalloc.take_snapshot()
        for stat in end_snapshot.compare_to(start_snapshot, "lineno")[:5]:
            logger.info(f"Leak: {stat}")
        tracemalloc.stop()
        gc.collect()

async def auto_delete_messages():
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        now = asyncio.get_event_loop().time()
        async with queue_lock:
            while delete_queue and now - delete_queue[0][2] >= DELETE_AFTER:
                cid, mid, _ = delete_queue.popleft()
                if await verify_message_exists(cid, mid):
                    try:
                        await bot.delete_message(chat_id=cid, message_id=mid)
                        logger.info(f"Удалено сообщение {mid}")
                    except Exception as e:
                        logger.error(f"Failed to delete {mid}: {e}")
                save_delete_queue()

async def run_email_loop():
    while True:
        await fetch_emails()
        await asyncio.sleep(CHECK_INTERVAL)

async def main():
    load_cached_folder()
    load_delete_queue()
    logger.info("Бот запущен")

    app = Application.builder().token(TOKEN).build()
    await app.initialize()
    await app.start()

    asyncio.create_task(auto_delete_messages())
    asyncio.create_task(run_email_loop())

    # Чтобы процесс не завершался
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")

