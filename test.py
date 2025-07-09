import asyncio
from telegram import Bot

async def get_chat_id():
    bot = Bot(token="7800536583:AAGW4xeOUWUWVJqZNqdXakyKGeSEGBJBShE")
    updates = await bot.get_updates()
    for update in updates:
        print(f"Chat ID: {update.message.chat_id}")
    print("Отправьте сообщение боту и перезапустите скрипт для получения нового Chat ID")

asyncio.run(get_chat_id())