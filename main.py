import imaplib
import email
from email.header import decode_header
import schedule
import time
import asyncio
from telegram import Bot
import re
from charset_normalizer import detect
import pickle
import os

# Конфигурация


bot = Bot(token=TOKEN)
cached_folder = None  # Переменная для кэширования имени папки

def load_cached_folder():
    """Загружает кэшированное имя папки из файла"""
    global cached_folder
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                cached_folder = pickle.load(f)
                print(f"Загружен кэш: {cached_folder}")
        except:
            print("Ошибка загрузки кэша, будет выполнен новый поиск")
            cached_folder = None
    return cached_folder

def save_cached_folder(folder_name):
    """Сохраняет имя папки в кэш"""
    global cached_folder
    cached_folder = folder_name
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(folder_name, f)
        print(f"Кэш сохранен: {folder_name}")
    except:
        print("Ошибка сохранения кэша")

def decode_folder_name(folder_bytes):
    """Декодирует название папки из IMAP-формата (включая UTF-7)"""
    try:
        folder_str = folder_bytes.decode('utf-7', errors='ignore')
        parts = re.split(r'"/"', folder_str)
        if len(parts) > 1:
            return parts[-1].strip().strip('"')
        return folder_str.strip().strip('"')
    except:
        try:
            result = detect(folder_bytes)
            encoding = result['encoding'] or 'utf-8'
            folder_str = folder_bytes.decode(encoding, errors='ignore')
            parts = re.split(r'"/"', folder_str)
            if len(parts) > 1:
                return parts[-1].strip().strip('"')
            return folder_str.strip().strip('"')
        except:
            return str(folder_bytes)

def find_target_folder(mail, target_name):
    """Находит папку по имени"""
    status, folders = mail.list()
    if status != "OK":
        print("Ошибка при получении списка папок")
        return None

    print("Доступные папки (декодированные):")
    for folder_bytes in folders:
        folder_str = decode_folder_name(folder_bytes)
        print(f" - {folder_str}")
        if target_name.lower() in folder_str.lower():
            if '"/"' in folder_str:
                return folder_str.split('"/"')[-1].strip('"')
            return folder_str
    return None

def extract_email_address(from_header):
    """Извлекает чистый email из заголовка From"""
    if not from_header:
        return ""
    match = re.search(r'<(.+?)>', from_header)
    return match.group(1).lower() if match else from_header.lower()

async def fetch_emails():
    global cached_folder
    mail = None
    try:
        # Подключение к серверу
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)

        # Проверка кэшированной папки
        if cached_folder:
            print(f"Проверяем кэшированную папку: {cached_folder}")
            status, _ = mail.select(f'"{cached_folder}"')
            if status == "OK":
                target_folder = cached_folder
            else:
                print(f"Кэшированная папка {cached_folder} недоступна, ищем заново...")
                cached_folder = None

        # Если кэш пустой или папка недоступна, ищем папку
        if not cached_folder:
            target_folder = find_target_folder(mail, TARGET_FOLDER_NAME)
            if not target_folder:
                print(f"Не удалось найти папку '{TARGET_FOLDER_NAME}'. Проверяем INBOX...")
                target_folder = "INBOX"
            save_cached_folder(target_folder)
            status, _ = mail.select(f'"{target_folder}"')
            if status != "OK":
                print(f"Не удалось выбрать папку: {target_folder}")
                return

        print(f"Используем папку: {target_folder}")

        # Поиск непрочитанных писем от конкретного отправителя
        status, messages = mail.search(None, f'(UNSEEN FROM "{ALLOWED_SENDER_EMAIL}")')
        if status != "OK":
            print("Ошибка при поиске писем")
            return

        email_ids = messages[0].split()
        for e_id in email_ids:
            try:
                _, msg_data = mail.fetch(e_id, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Обработка темы письма
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8", errors='ignore')

                # Проверка отправителя
                from_ = msg.get("From", "")
                actual_email = extract_email_address(from_)
                if ALLOWED_SENDER_EMAIL.lower() not in actual_email:
                    continue

                # Извлечение текста письма
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors='ignore')
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors='ignore')

                # Поиск кода
                code_match = re.search(r'Введите код:\s*(\d{6})', body)
                if code_match:
                    code = code_match.group(1)
                    message_text = f"🔐 *Новый код SSPVO*\n\nКод: `{code}`\n\n[Авторизация](http://10.3.60.2/account/user/20191/emailcode)"
                else:
                    message_text = f"✉️ *Письмо от SSPVO*\n\n{body}"

                # Отправка в Telegram (асинхронно)
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=message_text,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                    print(f"Сообщение отправлено для письма {e_id}")
                except Exception as telegram_error:
                    print(f"Ошибка отправки в Telegram для письма {e_id}: {telegram_error}")
                    continue

                # Пометить как прочитанное
                mail.store(e_id, "+FLAGS", "\\Seen")

            except Exception as e:
                print(f"Ошибка обработки письма {e_id}: {e}")
                continue

    except Exception as e:
        print(f"Ошибка подключения: {e}")
        # Сбрасываем кэш при ошибке подключения
        if cached_folder:
            print("Сбрасываем кэш из-за ошибки подключения")
            cached_folder = None
            save_cached_folder(None)
    finally:
        try:
            if mail:
                mail.close()
                mail.logout()
        except:
            pass

async def main():
    """Основная функция для интеграции schedule и asyncio"""
    # Загрузка кэша при старте
    load_cached_folder()

    # Запуск проверки каждые 2 минуты
    schedule.every(2).minutes.do(lambda: asyncio.create_task(fetch_emails()))

    print(f"Бот запущен. Поиск папки '{TARGET_FOLDER_NAME}'...")
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")