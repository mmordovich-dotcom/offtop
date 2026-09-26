"""
Telegram-бот:
1. Удаление сообщений без хэштега (#слово) в выбранных темах форума.
2. ИИ-судья DeepSeek по команде /verdict на основе промпта из /prompt.
3. Встроенный веб-сервер для Render и пингов UptimeRobot.
"""

import asyncio
import json
import logging
import os
import re
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from openai import AsyncOpenAI
from telegram import MessageEntity, Update
from telegram.constants import ChatAction, ChatMemberStatus
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

# --- Конфигурация и переменные окружения ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
AI_API_KEY = os.environ.get("AI_API_KEY", "PASTE_YOUR_OPENROUTER_OR_DEEPSEEK_KEY")

# URL API (для OpenRouter: https://openrouter.ai/api/v1, для прямого DeepSeek: https://api.deepseek.com)
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://openrouter.ai/api/v1")
# Модель (например: deepseek/deepseek-chat:free в OpenRouter или deepseek-chat в DeepSeek)
AI_MODEL = os.environ.get("AI_MODEL", "deepseek/deepseek-chat:free")

BASE_DIR = Path(__file__).parent
TOPICS_FILE = BASE_DIR / "topics.json"
PROMPTS_FILE = BASE_DIR / "prompts.json"

DEFAULT_SYSTEM_PROMPT = (
    "Ты беспристрастный экономический и юридический ИИ-судья. "
    "Проанализируй предоставленный проект/действие, укажи сильные и слабые стороны "
    "и вынеси четкий, аргументированный вердикт."
)

HASHTAG_WITH_WORD_RE = re.compile(r"#\w+", re.UNICODE)
ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\ufeff\u2060\u061c]")

# Инициализация асинхронного клиента OpenAI/DeepSeek
ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)


# --- 1. HTTP-сервер для Render и UptimeRobot ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK: Bot is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Health-check HTTP сервер запущен на порту %s", port)
    server.serve_forever()


# --- 2. Работа с хранилищем данных (JSON) ---
def load_json(filepath: Path) -> dict:
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", filepath, e)
    return {}


def save_json(filepath: Path, data: dict) -> None:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка записи в %s: %s", filepath, e)


async def is_chat_admin(update: Update) -> bool:
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


# --- 3. Модуль обязательных хэштегов ---
def normalize_text(raw_text: str) -> str:
    text = unicodedata.normalize("NFKC", raw_text)
    return ZERO_WIDTH_RE.sub("", text)


def has_valid_hashtag(message) -> bool:
    entities = (message.entities or []) + (message.caption_entities or [])
    if any(e.type == MessageEntity.HASHTAG for e in entities):
        return True

    raw_text = message.text or message.caption or ""
    if not raw_text:
        return False

    return bool(HASHTAG_WITH_WORD_RE.search(normalize_text(raw_text)))


async def cmd_require_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.is_topic_message:
        await message.reply_text("Команду нужно вызвать внутри темы (топика).")
        return
    if not await is_chat_admin(update):
        await message.reply_text("Команда доступна только администраторам чата.")
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_json(TOPICS_FILE)
    topics = data.setdefault(chat_id, [])
    if thread_id in topics:
        await message.reply_text("В этой теме правило уже включено.")
        return

    topics.append(thread_id)
    save_json(TOPICS_FILE, data)
    await message.reply_text("Готово ✅ Сообщения без символа # в этой теме удаляются.")


async def cmd_unrequire_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.is_topic_message:
        await message.reply_text("Команду нужно вызвать внутри темы (топика).")
        return
    if not await is_chat_admin(update):
        await message.reply_text("Команда доступна только администраторам чата.")
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_json(TOPICS_FILE)
    topics = data.get(chat_id, [])
    if thread_id not in topics:
        await message.reply_text("В этой теме правило не было включено.")
        return

    topics.remove(thread_id)
    save_json(TOPICS_FILE, data)
    await message.reply_text("Правило отключено для этой темы.")


async def cmd_list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_json(TOPICS_FILE)
    topics = data.get(chat_id, [])
    if not topics:
        await update.message.reply_text("В этом чате нет тем с включённым правилом.")
        return
    await update.message.reply_text("ID тем с обязательным #:\n" + ", ".join(str(t) for t in topics))


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None or not message.is_topic_message:
        return

    if message.from_user and message.from_user.is_bot and message.from_user.id == context.bot.id:
        return

    chat_id = str(update.effective_chat.id)
    thread_id = message.message_thread_id

    data = load_json(TOPICS_FILE)
    topics = data.get(chat_id, [])
    if thread_id not in topics:
        return

    if has_valid_hashtag(message):
        return

    try:
        await message.delete()
        logger.info("Удалено сообщение без хэштега в теме %s чата %s", thread_id, chat_id)
    except Exception as e:
        logger.warning("Не удалось удалить сообщение: %s", e)


# --- 4. Модуль ИИ-судьи (DeepSeek) ---
async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает системный промпт для текущей темы форума."""
    message = update.message
    if update.effective_chat.type == "private" or not message.is_topic_message:
        await message.reply_text("Эту команду можно использовать только внутри темы форума.")
        return

    if not await is_chat_admin(update):
        await message.reply_text("Настраивать промпт судьи могут только администраторы.")
        return

    key = f"{update.effective_chat.id}:{message.message_thread_id}"
    prompts_data = load_json(PROMPTS_FILE)
    new_prompt = " ".join(context.args).strip()

    if not new_prompt:
        current = prompts_data.get(key, DEFAULT_SYSTEM_PROMPT)
        await message.reply_text(
            f"Текущий системный промпт судьи для этой темы:\n\n«{current}»\n\n"
            "Чтобы изменить его, напишите:\n/prompt "
        )
        return

    prompts_data[key] = new_prompt
    save_json(PROMPTS_FILE, prompts_data)
    await message.reply_text("Системный промпт судьи для этой темы успешно обновлен! ✅")


async def cmd_verdict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выносит вердикт по проекту через нейросеть."""
    message = update.message

    # В личных сообщениях игнорируем команду
    if update.effective_chat.type == "private":
        return

    if not message.is_topic_message:
        await message.reply_text("Команда /verdict работает только внутри тем форума.")
        return

    if not AI_API_KEY or AI_API_KEY.startswith("PASTE_"):
        await message.reply_text("API ключ нейросети не настроен в переменных окружения.")
        return

    # Получаем текст проекта: либо из аргументов команды, либо из сообщения, на которое ответили
    target_text = ""
    if context.args:
        target_text = " ".join(context.args).strip()
    elif message.reply_to_message:
        target_text = message.reply_to_message.text or message.reply_to_message.caption or ""

    if not target_text:
        await message.reply_text(
            "Как использовать команду:\n"
            "1. Ответьте (Reply) командой /verdict на сообщение с проектом.\n"
            "2. Или напишите текст сразу: `/verdict Описание реформы/проекта`"
        )
        return

    key = f"{update.effective_chat.id}:{message.message_thread_id}"
    prompts_data = load_json(PROMPTS_FILE)
    system_instruction = prompts_data.get(key, DEFAULT_SYSTEM_PROMPT)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
        message_thread_id=message.message_thread_id,
    )

    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": target_text},
            ],
            max_tokens=2000,
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("Ошибка запроса к ИИ")
        await message.reply_text(f"⚠️ Ошибка при обращении к нейросети: {e}")
        return

    # Разбивка на части, если ответ длиннее лимита Telegram (4096 символов)
    for chunk in [answer[i : i + 4000] for i in range(0, len(answer), 4000)]:
        await message.reply_text(chunk)


# --- 5. Запуск приложения ---
def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit("Укажите токен бота в переменной BOT_TOKEN.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Запуск фонового веб-сервера
    threading.Thread(target=run_health_check_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Хэндлеры хэштегов
    app.add_handler(CommandHandler("require_hashtag", cmd_require_hashtag))
    app.add_handler(CommandHandler("unrequire_hashtag", cmd_unrequire_hashtag))
    app.add_handler(CommandHandler("list_topics", cmd_list_topics))

    # Хэндлеры ИИ (с поддержкой обоих вариантов написания: /prompt и /promt)
    app.add_handler(CommandHandler(["prompt", "promt"], cmd_prompt))
    app.add_handler(CommandHandler("verdict", cmd_verdict))

    # Проверка сообщений на хэштег
    content_filter = filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL
    app.add_handler(MessageHandler(content_filter, check_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & content_filter, check_message))

    logger.info("Бот запущен, ожидаю сообщения...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
