"""
Telegram-бот: в выбранных темах (топиках) форума удаляет любые сообщения,
в которых нет символа "#" (в тексте или в подписи к медиа).

Команды (вызывать ВНУТРИ нужной темы, только для админов чата):
  /require_hashtag    — включить правило для текущей темы
  /unrequire_hashtag  — выключить правило для текущей темы
  /list_topics        — показать список тем с включённым правилом

Требования к боту:
  - Должен быть добавлен в группу с включёнными темами (форум).
  - Должен быть администратором с правом "Удаление сообщений".
"""

import asyncio
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Токен берется из переменной окружения Render (Environment Variables)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

DATA_FILE = Path(__file__).parent / "topics.json"

# Сообщение считается корректным, только если после "#" сразу идёт слово
HASHTAG_WITH_WORD_RE = re.compile(r"#\w+", re.UNICODE)


# --- HTTP-сервер для Render и UptimeRobot ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK: Bot is alive and running!")

    def log_message(self, format, *args):
        # Отключаем спам в логах от частых запросов UptimeRobot
        pass


def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Health-check HTTP сервер запущен на порту %s", port)
    server.serve_forever()


# --- Логика бота ---
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", DATA_FILE, e)
    return {}


def save_data(data: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка сохранения %s: %s", DATA_FILE, e)


async def is_chat_admin(update: Update) -> bool:
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def cmd_require_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.is_topic_message:
        await message.reply_text(
            "Эту команду нужно отправить прямо в той теме (топике), "
            "которую вы хотите настроить."
        )
        return
    if not await is_chat_admin(update):
        await message.reply_text("Эта команда доступна только администраторам чата.")
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_data()
    topics = data.setdefault(chat_id, [])
    if thread_id in topics:
        await message.reply_text("В этой теме правило уже включено.")
        return

    topics.append(thread_id)
    save_data(data)
    await message.reply_text(
        "Готово ✅ Теперь в этой теме сообщения без символа # будут удаляться."
    )


async def cmd_unrequire_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.is_topic_message:
        await message.reply_text(
            "Эту команду нужно отправить прямо в той теме (топике), которую хотите отключить."
        )
        return
    if not await is_chat_admin(update):
        await message.reply_text("Эта команда доступна только администраторам чата.")
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_data()
    topics = data.get(chat_id, [])
    if thread_id not in topics:
        await message.reply_text("В этой теме правило и так не включено.")
        return

    topics.remove(thread_id)
    save_data(data)
    await message.reply_text("Правило отключено для этой темы.")


async def cmd_list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    topics = data.get(chat_id, [])
    if not topics:
        await update.message.reply_text("В этом чате нет тем с включённым правилом.")
        return
    text = "Темы с обязательным #\n(это внутренние ID тем, названия Telegram API не отдаёт):\n"
    text += ", ".join(str(t) for t in topics)
    await update.message.reply_text(text)


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or not message.is_topic_message:
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_data()
    topics = data.get(chat_id, [])
    if thread_id not in topics:
        return

    text = message.text or message.caption or ""
    if HASHTAG_WITH_WORD_RE.search(text):
        return

    try:
        await message.delete()
        logger.info(
            "Удалено сообщение без #слово в чате %s, теме %s, от пользователя %s",
            chat_id,
            thread_id,
            update.effective_user.id,
        )
    except Exception as e:
        logger.warning("Не удалось удалить сообщение: %s", e)


def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit(
            "Укажите токен бота: задайте переменную окружения BOT_TOKEN "
            "или впишите его в код."
        )

    # Принудительно создаем и регистрируем event loop для главного потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 1. Запуск встроенного веб-сервера для Render и UptimeRobot
    server_thread = threading.Thread(target=run_health_check_server, daemon=True)
    server_thread.start()

    # 2. Запуск бота через Long Polling
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("require_hashtag", cmd_require_hashtag))
    app.add_handler(CommandHandler("unrequire_hashtag", cmd_unrequire_hashtag))
    app.add_handler(CommandHandler("list_topics", cmd_list_topics))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, check_message))

    logger.info("Бот запущен, ожидаю сообщения...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
