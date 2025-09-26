# meetbot.py
# Python 3.8+ (tested with python-telegram-bot v20+)
# pip install python-telegram-bot

import os
import sqlite3
import logging
import configparser
import traceback
import hashlib
import binascii
from datetime import datetime, timedelta
from functools import wraps
import random
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, CallbackContext,
    CallbackQueryHandler, ConversationHandler
)

# ----------------- Конфигурация и логирование -----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "bin", "data")
DB_PATH = os.path.join(DATA_DIR, "meetbot.db")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
INI_PATH = os.path.join(BASE_DIR, "settings.ini")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PHOTOS_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Отключение внутреннего логирования, чтобы не было дублирования
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)

# ----------------- Глобальные переменные для анти-спама -----------------
last_search_time = {}
SEARCH_COOLDOWN_SECONDS = 5  # Минимальный интервал между поисками


# ----------------- Вспомогательные функции для чтения INI -----------------
def get_config():
    """Получает объект конфигурации из файла settings.ini."""
    config = configparser.ConfigParser()
    # Проверяем, существует ли файл перед попыткой чтения
    if not os.path.exists(INI_PATH):
        raise FileNotFoundError(f"Configuration file not found at {INI_PATH}")
    config.read(INI_PATH, encoding='utf-8')
    return config


def get_telegram_token():
    """Читает и возвращает токен Telegram из файла конфигурации."""
    try:
        config = get_config()
        token = config['Settings']['TelegramToken']
        if not token:
            raise ValueError("TelegramToken is empty in settings.ini")
        return token
    except KeyError as e:
        raise ValueError(f"Required key not found in settings.ini: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to read settings.ini: {e}")


# ----------------- Вспомогательные функции для БД -----------------
def get_conn():
    """Получает соединение с базой данных."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Инициализирует базу данных, создавая необходимые таблицы."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
              CREATE TABLE IF NOT EXISTS users
              (
                  id
                  INTEGER
                  PRIMARY
                  KEY
                  AUTOINCREMENT,
                  tg_id
                  INTEGER
                  UNIQUE
                  NOT
                  NULL,
                  name
                  TEXT,
                  age
                  INTEGER,
                  description
                  TEXT,
                  photo_path
                  TEXT,
                  created_at
                  TEXT
                  DEFAULT
                  CURRENT_TIMESTAMP,
                  frozen_until
                  TEXT
                  DEFAULT
                  NULL,
                  banned
                  INTEGER
                  DEFAULT
                  0,
                  ban_reason
                  TEXT
                  DEFAULT
                  NULL
              )""")
    c.execute("""
              CREATE TABLE IF NOT EXISTS viewed
              (
                  viewer_id
                  INTEGER
                  NOT
                  NULL,
                  viewed_id
                  INTEGER
                  NOT
                  NULL,
                  UNIQUE
              (
                  viewer_id,
                  viewed_id
              )
                  )""")
    c.execute("""
              CREATE TABLE IF NOT EXISTS likes
              (
                  from_id
                  INTEGER
                  NOT
                  NULL,
                  to_id
                  INTEGER
                  NOT
                  NULL,
                  created_at
                  TEXT
                  DEFAULT
                  CURRENT_TIMESTAMP,
                  UNIQUE
              (
                  from_id,
                  to_id
              )
                  )""")
    c.execute("""
              CREATE TABLE IF NOT EXISTS reports
              (
                  id
                  INTEGER
                  PRIMARY
                  KEY
                  AUTOINCREMENT,
                  reporter_id
                  INTEGER
                  NOT
                  NULL,
                  target_id
                  INTEGER
                  NOT
                  NULL,
                  reason
                  TEXT
                  NOT
                  NULL,
                  created_at
                  TEXT
                  DEFAULT
                  CURRENT_TIMESTAMP,
                  processed
                  INTEGER
                  DEFAULT
                  0
              )""")
    c.execute("""
              CREATE TABLE IF NOT EXISTS admins
              (
                  id
                  INTEGER
                  PRIMARY
                  KEY
                  AUTOINCREMENT,
                  username
                  TEXT
                  UNIQUE
                  NOT
                  NULL,
                  password_hash
                  BLOB
                  NOT
                  NULL,
                  salt
                  BLOB
                  NOT
                  NULL,
                  is_super
                  INTEGER
                  DEFAULT
                  0
              )""")
    conn.commit()
    conn.close()


# ----------------- Хеширование пароля для админов -----------------
PBKDF2_ITERATIONS = 200_000
HASH_NAME = 'sha256'
SALT_SIZE = 16


def hash_password(password: str, salt: bytes = None):
    """Хеширует пароль."""
    if salt is None:
        salt = os.urandom(SALT_SIZE)
    pwd_hash = hashlib.pbkdf2_hmac(HASH_NAME, password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return salt, pwd_hash


def verify_password(password: str, salt: bytes, pwd_hash: bytes) -> bool:
    """Проверяет пароль."""
    check = hashlib.pbkdf2_hmac(HASH_NAME, password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return hashlib.compare_digest(check, pwd_hash)


# ----------------- Утилиты для админов -----------------
admin_sessions = {}  # telegram_user_id -> {username, expires, is_super, db_admin_id}
ADMIN_TTL = 3600  # секунды


def get_super_admin_id():
    """Читает и возвращает ID суперадмина из файла конфигурации."""
    try:
        config = get_config()
        return int(config['Settings']['PythonIns'])  # Assuming PythonIns is the user's ID
    except (KeyError, ValueError) as e:
        logger.error(f"Super admin ID not found or is invalid in settings.ini: {e}")
        return None


def add_admin_to_db(username: str, password: str, is_super=False):
    """Добавляет нового администратора в базу данных."""
    conn = get_conn()
    c = conn.cursor()
    salt, pwd_hash = hash_password(password)
    c.execute("INSERT OR IGNORE INTO admins (username, password_hash, salt, is_super) VALUES (?, ?, ?, ?)",
              (username, sqlite3.Binary(pwd_hash), sqlite3.Binary(salt), 1 if is_super else 0))
    conn.commit()
    conn.close()
    logger.info(f"New admin '{username}' added to DB.")


def get_admin_by_username(username: str):
    """Находит администратора по имени пользователя."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash, salt, is_super FROM admins WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "salt": row["salt"],
        "is_super": bool(row["is_super"])
    }


def admin_required(func):
    """Декоратор для проверки прав администратора."""

    @wraps(func)
    async def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        sess = admin_sessions.get(user_id)
        if not sess or sess['expires'] < datetime.utcnow().timestamp():
            await update.effective_message.reply_text(
                "Вы не авторизованы как администратор. Используйте /admin <username> <password> в личных сообщениях боту.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def super_admin_required(func):
    """Декоратор для проверки прав суперадминистратора."""

    @wraps(func)
    async def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        SUPER_ADMIN_ID = get_super_admin_id()
        if user_id != SUPER_ADMIN_ID:
            await update.effective_message.reply_text("У вас нет прав для выполнения этой команды.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


# ----------------- Функции для пользователей / БД -----------------
def get_user_by_tg_id(tg_id: int):
    """Получает данные пользователя по его Telegram ID."""
    conn = get_conn()
    c = conn.cursor()
    # Защита от SQL-инъекций: используется параметризованный запрос
    c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = c.fetchone()
    conn.close()
    return row


def create_or_update_user(tg_id: int, name: Optional[str] = None, age: Optional[int] = None,
                          description: Optional[str] = None, photo_path: Optional[str] = None):
    """Создает новый профиль или обновляет существующий."""
    conn = get_conn()
    c = conn.cursor()
    existing = get_user_by_tg_id(tg_id)
    if existing:
        c.execute("""
                  UPDATE users
                  SET name        = COALESCE(?, name),
                      age         = COALESCE(?, age),
                      description = COALESCE(?, description),
                      photo_path  = COALESCE(?, photo_path)
                  WHERE tg_id = ?
                  """, (name, age, description, photo_path, tg_id))
    else:
        c.execute("""
                  INSERT INTO users (tg_id, name, age, description, photo_path)
                  VALUES (?, ?, ?, ?, ?)
                  """, (tg_id, name, age, description, photo_path))
    conn.commit()
    conn.close()


def mark_viewed(viewer_id: int, viewed_id: int):
    """Отмечает, что пользователь просмотрел анкету."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO viewed (viewer_id, viewed_id) VALUES (?, ?)", (viewer_id, viewed_id))
        conn.commit()
        logger.info(f"User {viewer_id} viewed profile {viewed_id}.")
    finally:
        conn.close()


def add_like(from_id: int, to_id: int):
    """Добавляет 'лайк' от одного пользователя другому."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?, ?)", (from_id, to_id))
        conn.commit()
    finally:
        conn.close()


def check_mutual_like(a: int, b: int) -> bool:
    """Проверяет наличие взаимного 'лайка'."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?", (a, b))
        if c.fetchone():
            c.execute("SELECT 1 FROM likes WHERE from_id = ? AND to_id = ?", (b, a))
            return c.fetchone() is not None
        return False
    finally:
        conn.close()


async def get_next_profile(current_tg_id: int):
    """
    Получает следующий профиль для показа, который не был просмотрен.
    Используется сложный SQL-запрос для исключения уже просмотренных,
    замороженных, забаненных и самого пользователя.
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
                  SELECT *
                  FROM users
                  WHERE tg_id != ?
            AND tg_id NOT IN (
                SELECT viewed_id FROM viewed WHERE viewer_id = ?
            )
            AND (frozen_until IS NULL OR datetime(frozen_until) < datetime('now'))
            AND banned = 0
                  ORDER BY RANDOM()
                      LIMIT 1
                  """, (current_tg_id, current_tg_id))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_matches(tg_id: int):
    """Получает список взаимных 'лайков'."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
              SELECT DISTINCT u.tg_id, u.name, u.photo_path
              FROM likes l1
                       JOIN likes l2 ON l1.from_id = l2.to_id AND l1.to_id = l2.from_id
                       JOIN users u ON u.tg_id = l2.from_id
              WHERE l1.from_id = ?
              """, (tg_id,))
    matches = c.fetchall()
    conn.close()
    return matches


def get_profiles_to_moderate():
    """Получает список профилей для модерации (для админов)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE frozen_until IS NOT NULL")
    profiles = c.fetchall()
    conn.close()
    return profiles


def get_user_by_id(user_id: int):
    """Получает данные пользователя по внутреннему ID."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def unfreeze_user(user_id: int):
    """Снимает заморозку с профиля."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET frozen_until = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Admin unfroze user with internal ID: {user_id}")


def ban_user(user_id: int, reason: str):
    """Банит пользователя."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 1, ban_reason = ? WHERE id = ?", (reason, user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Admin banned user with internal ID: {user_id} for reason: {reason}")


def unban_user(user_id: int):
    """Разбанивает пользователя."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 0, ban_reason = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Admin unbanned user with internal ID: {user_id}")


def get_all_banned_users():
    """Получает список всех забаненных пользователей."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE banned = 1")
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_reports():
    """Получает все непросмотренные жалобы."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM reports WHERE processed = 0")
    rows = c.fetchall()
    conn.close()
    return rows


def process_report(report_id: int):
    """Отмечает жалобу как обработанную."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE reports SET processed = 1 WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()
    logger.info(f"Admin processed report with ID: {report_id}")


def add_report(reporter_id: int, target_id: int, reason: str):
    """Добавляет жалобу в базу данных."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO reports (reporter_id, target_id, reason) VALUES (?, ?, ?)", (reporter_id, target_id, reason))
    conn.commit()
    conn.close()
    logger.info(f"New report submitted by {reporter_id} against {target_id}. Reason: {reason}")


# ----------------- Состояния для ConversationHandler -----------------
(
    CHOOSING,
    EDITING_NAME,
    EDITING_AGE,
    EDITING_DESC,
    EDITING_PHOTO,
    WAITING_REPORT_REASON,
    AWAITING_NEW_ADMIN_PASSWORD,
    AWAITING_ADMIN_DELETE,
    AWAITING_BAN_REASON,
    AWAITING_UNBAN
) = range(10)


# ----------------- Обработчики команд и сообщений -----------------

async def start(update: Update, context: CallbackContext) -> None:
    """Обработчик команды /start."""
    user = update.effective_user
    db_user = get_user_by_tg_id(user.id)
    keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("Найти пару")],
        [KeyboardButton("Мой профиль"), KeyboardButton("Лайки")]
    ], resize_keyboard=True)

    if not db_user:
        logger.info(f"New user started the bot: ID {user.id}, Username: {user.username}")
        await update.message.reply_text(
            f"Привет, {user.full_name}! 👋\nЭто бот для знакомств.\n\n"
            "Давай создадим твою анкету, чтобы другие пользователи могли тебя найти.",
            reply_markup=keyboard
        )
        return await my_profile(update, context)
    else:
        logger.info(f"Existing user returned: ID {user.id}, Username: {user.username}")
        await update.message.reply_text(
            f"С возвращением, {db_user['name']}! Что хочешь сделать?",
            reply_markup=keyboard
        )


async def my_profile(update: Update, context: CallbackContext) -> int:
    """Обработчик команды /my_profile или кнопки 'Мой профиль'."""
    user = update.effective_user
    db_user = get_user_by_tg_id(user.id)
    if not db_user:
        await update.message.reply_text("Похоже, у тебя ещё нет анкеты. Давай её создадим!")
        create_or_update_user(user.id)
        db_user = get_user_by_tg_id(user.id)
    
    logger.info(f"User {user.id} viewed their profile.")

    profile_text = format_profile(db_user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Изменить имя", callback_data="edit_name"),
            InlineKeyboardButton("Изменить возраст", callback_data="edit_age")
        ],
        [
            InlineKeyboardButton("Изменить описание", callback_data="edit_desc"),
            InlineKeyboardButton("Изменить фото", callback_data="edit_photo")
        ]
    ])

    if db_user['photo_path']:
        await update.message.reply_photo(
            photo=db_user['photo_path'],
            caption=profile_text,
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            f"Твоя анкета:\n{profile_text}\n\nЧтобы закончить её, нужно добавить фото.",
            reply_markup=keyboard
        )

    return CHOOSING


def format_profile(user_data):
    """Форматирует текст профиля."""
    name = user_data['name'] or "Не указано"
    age = user_data['age'] or "Не указан"
    description = user_data['description'] or "Не указано"
    return f"✨ *Имя*: {name}\n🎂 *Возраст*: {age}\n📝 *О себе*: {description}\n\nЧтобы другие тебя увидели, заполни все поля и добавь фото."


# --- Ветвь изменения профиля ---
async def edit_profile_callback(update: Update, context: CallbackContext) -> int:
    """Обработчик нажатия на кнопку 'Изменить...'."""
    query = update.callback_query
    await query.answer()
    
    logger.info(f"User {query.from_user.id} chose to edit profile with action: {query.data}")

    choice = query.data
    if choice == "edit_name":
        await query.message.reply_text("Введите ваше имя:")
        return EDITING_NAME
    elif choice == "edit_age":
        await query.message.reply_text("Введите ваш возраст (только число):")
        return EDITING_AGE
    elif choice == "edit_desc":
        await query.message.reply_text("Напишите короткое описание о себе:")
        return EDITING_DESC
    elif choice == "edit_photo":
        await query.message.reply_text("Отправьте фото, которое будет в вашей анкете:")
        return EDITING_PHOTO


async def save_name(update: Update, context: CallbackContext) -> int:
    """Сохраняет новое имя."""
    name = update.message.text.strip()
    if not name or len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Попробуйте еще раз:")
        return EDITING_NAME
    create_or_update_user(update.effective_user.id, name=name)
    logger.info(f"User {update.effective_user.id} updated their name to: {name}")
    await update.message.reply_text("Имя успешно обновлено!")
    await my_profile(update, context)
    return ConversationHandler.END


async def save_age(update: Update, context: CallbackContext) -> int:
    """Сохраняет новый возраст."""
    try:
        age = int(update.message.text.strip())
        if not 16 <= age <= 99:
            await update.message.reply_text("Возраст должен быть от 16 до 99. Попробуйте еще раз:")
            return EDITING_AGE
        create_or_update_user(update.effective_user.id, age=age)
        logger.info(f"User {update.effective_user.id} updated their age to: {age}")
        await update.message.reply_text("Возраст успешно обновлен!")
        await my_profile(update, context)
        return ConversationHandler.END
    except (ValueError, TypeError):
        await update.message.reply_text("Пожалуйста, введите возраст числом. Попробуйте еще раз:")
        return EDITING_AGE


async def save_description(update: Update, context: CallbackContext) -> int:
    """Сохраняет новое описание."""
    description = update.message.text.strip()
    if not description or len(description) < 10:
        await update.message.reply_text("Описание слишком короткое. Пожалуйста, напишите больше.")
        return EDITING_DESC
    create_or_update_user(update.effective_user.id, description=description)
    logger.info(f"User {update.effective_user.id} updated their description.")
    await update.message.reply_text("Описание успешно обновлено!")
    await my_profile(update, context)
    return ConversationHandler.END


async def save_photo(update: Update, context: CallbackContext) -> int:
    """Сохраняет новое фото."""
    photo_file = update.message.photo[-1]
    user_id = update.effective_user.id
    photo_path = os.path.join(PHOTOS_DIR, f"{user_id}.jpg")

    try:
        await photo_file.download_to_drive(photo_path)
        create_or_update_user(user_id, photo_path=photo_path)
        logger.info(f"User {user_id} updated their profile photo.")
        await update.message.reply_text("Фото успешно обновлено!")
    except Exception as e:
        logger.error(f"Error saving photo for user {user_id}: {e}")
        await update.message.reply_text("Не удалось сохранить фото. Пожалуйста, попробуйте еще раз.")

    await my_profile(update, context)
    return ConversationHandler.END


async def cancel_profile_edit(update: Update, context: CallbackContext) -> int:
    """Отменяет изменение профиля."""
    logger.info(f"User {update.effective_user.id} cancelled profile edit.")
    await update.message.reply_text(
        "Изменение профиля отменено.",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("Найти пару")],
            [KeyboardButton("Мой профиль"), KeyboardButton("Лайки")]
        ], resize_keyboard=True)
    )
    return ConversationHandler.END


# --- Поиск анкет ---
async def find_pair(update: Update, context: CallbackContext) -> None:
    """Обработчик для кнопки 'Найти пару'."""
    user_id = update.effective_user.id

    # Анти-спам: проверка времени последнего поиска
    now = datetime.now()
    if user_id in last_search_time and (now - last_search_time[user_id]).total_seconds() < SEARCH_COOLDOWN_SECONDS:
        logger.warning(f"User {user_id} hit search cooldown.")
        await update.message.reply_text("Подождите немного перед следующим поиском.")
        return

    db_user = get_user_by_tg_id(user_id)
    if not db_user or not db_user['name'] or not db_user['age'] or not db_user['description'] or not db_user[
        'photo_path']:
        await update.message.reply_text("Чтобы искать, тебе нужно полностью заполнить свою анкету!")
        await my_profile(update, context)
        return

    profile = await get_next_profile(user_id)
    if not profile:
        logger.info(f"No new profiles found for user {user_id}.")
        await update.message.reply_text("Анкет для показа больше нет. Попробуй позже.")
        return

    last_search_time[user_id] = now
    logger.info(f"User {user_id} is viewing profile {profile['tg_id']}.")

    context.user_data['current_profile'] = profile['tg_id']

    profile_text = format_profile(profile)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤ Лайк", callback_data="like"),
            InlineKeyboardButton("➡️ Далее", callback_data="next")
        ],
        [InlineKeyboardButton("🚫 Жалоба", callback_data="report_user")]
    ])

    try:
        if 'last_profile_message_id' in context.user_data:
            # Редактируем последнее отправленное сообщение
            await context.bot.edit_message_media(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['last_profile_message_id'],
                media=InputMediaPhoto(media=profile['photo_path'], caption=profile_text),
                reply_markup=keyboard
            )
        else:
            # Отправляем новое сообщение
            message = await update.message.reply_photo(
                photo=profile['photo_path'],
                caption=profile_text,
                reply_markup=keyboard
            )
            context.user_data['last_profile_message_id'] = message.message_id
    except Exception as e:
        logger.error(f"Failed to send/edit profile for user {user_id}: {e}")
        # Если редактирование не удалось (например, пользователь удалил сообщение), отправляем новое
        message = await update.message.reply_photo(
            photo=profile['photo_path'],
            caption=profile_text,
            reply_markup=keyboard
        )
        context.user_data['last_profile_message_id'] = message.message_id

    mark_viewed(user_id, profile['tg_id'])


async def profile_actions(update: Update, context: CallbackContext) -> None:
    """Обработчик действий с анкетой (лайк, далее)."""
    query = update.callback_query
    await query.answer()

    action = query.data
    current_profile_id = context.user_data.get('current_profile')

    if not current_profile_id:
        logger.warning(f"User {query.from_user.id} tried to act on an old profile.")
        await query.edit_message_caption(caption="Анкета устарела. Нажмите 'Найти пару' ещё раз.")
        return

    viewer_id = query.from_user.id

    if action == "like":
        add_like(viewer_id, current_profile_id)
        logger.info(f"User {viewer_id} liked profile {current_profile_id}.")
        if check_mutual_like(viewer_id, current_profile_id):
            logger.info(f"Mutual match found between {viewer_id} and {current_profile_id}!")
            await context.bot.send_message(
                chat_id=current_profile_id,
                text=f"🎉 У вас взаимная симпатия с {query.from_user.full_name}!\n"
                     f"Вы можете начать общение: t.me/{query.from_user.username}"
            )
            await query.message.reply_text(
                f"🎉 У вас взаимная симпатия с пользователем! "
                f"Вы можете начать общение: t.me/{get_user_by_tg_id(current_profile_id)['name']}"
            )
        else:
            await query.message.reply_text("Вы поставили лайк пользователю.")
    elif action == "next":
        logger.info(f"User {viewer_id} skipped profile {current_profile_id}.")

    await find_pair(update, context)


async def handle_report_callback(update: Update, context: CallbackContext) -> int:
    """Обработчик нажатия на кнопку 'Жалоба'."""
    query = update.callback_query
    await query.answer()

    target_id = context.user_data.get('current_profile')
    if not target_id:
        await query.message.reply_text("Анкета, на которую вы жалуетесь, уже неактивна.")
        return ConversationHandler.END

    logger.info(f"User {query.from_user.id} started reporting profile {target_id}.")
    context.user_data['report_target_id'] = target_id
    await query.message.reply_text("Пожалуйста, опишите причину вашей жалобы:")
    return WAITING_REPORT_REASON


async def save_report(update: Update, context: CallbackContext) -> int:
    """Сохраняет жалобу."""
    reporter_id = update.effective_user.id
    target_id = context.user_data.get('report_target_id')
    reason = update.message.text

    if not target_id:
        logger.error(f"Error saving report: target_id missing for user {reporter_id}.")
        await update.message.reply_text("Произошла ошибка. Попробуйте еще раз.")
        return ConversationHandler.END

    add_report(reporter_id, target_id, reason)
    await update.message.reply_text("Спасибо, ваша жалоба отправлена на рассмотрение.")
    return ConversationHandler.END


# --- Показ взаимных лайков ---
async def show_matches(update: Update, context: CallbackContext) -> None:
    """Обработчик для кнопки 'Лайки'."""
    user_id = update.effective_user.id
    logger.info(f"User {user_id} checked their matches.")
    matches = get_matches(user_id)

    if not matches:
        await update.message.reply_text("У вас пока нет взаимных лайков.")
        return

    match_list = "\n".join([f"- @{match['name']}" for match in matches])
    await update.message.reply_text(f"Список взаимных лайков:\n{match_list}")


# ----------------- Админ-панель -----------------

async def admin_login(update: Update, context: CallbackContext) -> None:
    """Обработчик для входа в админ-панель."""
    if update.effective_chat.type != 'private':
        await update.message.reply_text("Пожалуйста, используйте эту команду в личных сообщениях с ботом.")
        return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text("Использование: /admin <имя_пользователя> <пароль>")
        return

    username, password = context.args
    admin = get_admin_by_username(username)

    if not admin or not verify_password(password, admin['salt'], admin['password_hash']):
        logger.warning(f"Failed admin login attempt for username: {username}")
        await update.message.reply_text("Неверное имя пользователя или пароль.")
        return

    admin_sessions[update.effective_user.id] = {
        'username': username,
        'expires': datetime.utcnow().timestamp() + ADMIN_TTL,
        'is_super': admin['is_super'],
        'db_admin_id': admin['id']
    }
    logger.info(f"Admin login successful for username: {username} (ID: {update.effective_user.id})")
    await update.message.reply_text("Вы успешно авторизовались как администратор.")
    await show_admin_menu(update, context)


async def show_admin_menu(update: Update, context: CallbackContext) -> None:
    """Показывает главное меню админ-панели."""
    sess = admin_sessions.get(update.effective_user.id)
    if not sess:
        await update.effective_message.reply_text("Вы не авторизованы.")
        return

    logger.info(f"Admin {update.effective_user.id} opened the admin menu.")
    keyboard_rows = [
        [InlineKeyboardButton("Модерация профилей", callback_data="admin_moderate_profiles")],
        [InlineKeyboardButton("Просмотр жалоб", callback_data="admin_view_reports")],
        [InlineKeyboardButton("Просмотр забаненных", callback_data="admin_view_banned")]
    ]
    if sess['is_super']:
        keyboard_rows.append([InlineKeyboardButton("Управление админами", callback_data="admin_manage_admins")])

    keyboard_rows.append([InlineKeyboardButton("Выйти", callback_data="admin_logout")])

    reply_text = f"Добро пожаловать в админ-панель, {sess['username']}!"
    await update.effective_message.reply_text(reply_text, reply_markup=InlineKeyboardMarkup(keyboard_rows))


@admin_required
async def handle_admin_callbacks(update: Update, context: CallbackContext) -> int:
    """Обработчик нажатий на кнопки в админ-панели."""
    query = update.callback_query
    await query.answer()

    action = query.data
    admin_id = update.effective_user.id

    if action == "admin_logout":
        if admin_id in admin_sessions:
            del admin_sessions[admin_id]
            await query.edit_message_text("Вы вышли из админ-панели.")
            logger.info(f"Admin {admin_id} logged out.")
        return ConversationHandler.END

    if action == "admin_moderate_profiles":
        logger.info(f"Admin {admin_id} requested to moderate profiles.")
        profiles = get_profiles_to_moderate()
        if not profiles:
            await query.message.reply_text("Нет профилей для модерации.")
            return ConversationHandler.END

        for profile in profiles:
            profile_text = format_profile(profile)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Разморозить", callback_data=f"admin_unfreeze:{profile['id']}")],
                [InlineKeyboardButton("Забанить", callback_data=f"admin_ban:{profile['id']}")]
            ])
            await query.message.reply_photo(
                photo=profile['photo_path'],
                caption=profile_text,
                reply_markup=keyboard
            )
        return CHOOSING

    if action.startswith("admin_unfreeze:"):
        profile_id = int(action.split(':')[1])
        unfreeze_user(profile_id)
        await query.message.reply_text("Профиль успешно разморожен.")
        return CHOOSING

    if action.startswith("admin_ban:"):
        profile_id = int(action.split(':')[1])
        context.user_data['ban_target_id'] = profile_id
        await query.message.reply_text("Пожалуйста, укажите причину бана:")
        return AWAITING_BAN_REASON
    
    if action == "admin_view_reports":
        logger.info(f"Admin {admin_id} requested to view reports.")
        reports = get_all_reports()
        if not reports:
            await query.message.reply_text("Новых жалоб нет.")
            return ConversationHandler.END

        for report in reports:
            reporter = get_user_by_tg_id(report['reporter_id'])
            target = get_user_by_id(report['target_id'])
            if not target:
                continue

            report_text = (
                f"**Жалоба №{report['id']}**\n"
                f"От: @{reporter['name']} (ID: {reporter['tg_id']})\n"
                f"На: @{target['name']} (ID: {target['tg_id']})\n"
                f"Причина: {report['reason']}\n"
                f"Дата: {report['created_at']}"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Забанить", callback_data=f"admin_ban:{target['id']}"),
                    InlineKeyboardButton("Отметить как просмотренную", callback_data=f"admin_process_report:{report['id']}")
                ]
            ])
            await query.message.reply_text(report_text, reply_markup=keyboard)
        return CHOOSING
    
    if action.startswith("admin_process_report:"):
        report_id = int(action.split(':')[1])
        process_report(report_id)
        await query.message.reply_text("Жалоба отмечена как просмотренная.")
        return CHOOSING

    if action == "admin_view_banned":
        logger.info(f"Admin {admin_id} requested to view banned users.")
        banned_users = get_all_banned_users()
        if not banned_users:
            await query.message.reply_text("Нет забаненных пользователей.")
            return ConversationHandler.END
        
        for user in banned_users:
            user_text = (
                f"**Забаненный пользователь**\n"
                f"Имя: {user['name']}\n"
                f"ID: {user['tg_id']}\n"
                f"Причина бана: {user['ban_reason'] or 'Не указана'}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Разбанить", callback_data=f"admin_unban:{user['id']}")]
            ])
            await query.message.reply_text(user_text, reply_markup=keyboard)
        return CHOOSING
    
    if action.startswith("admin_unban:"):
        user_id = int(action.split(':')[1])
        unban_user(user_id)
        await query.message.reply_text("Пользователь разбанен.")
        return CHOOSING
    
    if action == "admin_manage_admins":
        logger.info(f"Super admin {admin_id} entered admin management.")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Добавить админа", callback_data="admin_add_new")],
            [InlineKeyboardButton("Удалить админа", callback_data="admin_delete_existing")],
            [InlineKeyboardButton("Назад", callback_data="admin_menu")]
        ])
        await query.message.reply_text("Управление администраторами:", reply_markup=keyboard)
        return CHOOSING

    if action == "admin_add_new":
        await query.message.reply_text("Введите имя пользователя для нового админа:")
        context.user_data['new_admin_username'] = True
        return AWAITING_NEW_ADMIN_PASSWORD
    
    if action == "admin_delete_existing":
        await query.message.reply_text("Введите имя пользователя админа, которого нужно удалить:")
        return AWAITING_ADMIN_DELETE

    if action == "admin_menu":
        await show_admin_menu(update, context)
        return CHOOSING
    
    return CHOOSING

# --- Ветви для админ-панели ---
async def ban_user_reason(update: Update, context: CallbackContext) -> int:
    """Сохраняет причину бана и банит пользователя."""
    reason = update.message.text
    user_id = context.user_data.get('ban_target_id')
    
    if not user_id:
        await update.message.reply_text("Ошибка: ID пользователя не найден. Попробуйте снова.")
        return ConversationHandler.END
        
    ban_user(user_id, reason)
    await update.message.reply_text("Пользователь успешно забанен.")
    return ConversationHandler.END


async def add_new_admin(update: Update, context: CallbackContext) -> int:
    """Добавляет нового админа."""
    if 'new_admin_username' in context.user_data:
        context.user_data['temp_username'] = update.message.text
        await update.message.reply_text("Теперь введите пароль для нового админа:")
        del context.user_data['new_admin_username']
        return AWAITING_NEW_ADMIN_PASSWORD
    
    username = context.user_data.get('temp_username')
    password = update.message.text
    
    if not username or not password:
        await update.message.reply_text("Ошибка. Попробуйте снова.")
        return ConversationHandler.END
        
    add_admin_to_db(username, password)
    await update.message.reply_text(f"Администратор {username} успешно добавлен.")
    del context.user_data['temp_username']
    return ConversationHandler.END


def delete_admin_from_db(username: str):
    """Удаляет администратора из базы данных."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    logger.info(f"Admin '{username}' deleted from DB.")


async def delete_admin_by_name(update: Update, context: CallbackContext) -> int:
    """Удаляет админа по имени пользователя."""
    username = update.message.text
    admin_to_delete = get_admin_by_username(username)
    
    if not admin_to_delete:
        await update.message.reply_text("Админ с таким именем не найден.")
        return AWAITING_ADMIN_DELETE

    # Проверка, чтобы суперадмин не мог удалить себя
    if admin_to_delete['id'] == admin_sessions[update.effective_user.id]['db_admin_id']:
        await update.message.reply_text("Вы не можете удалить самого себя.")
        return AWAITING_ADMIN_DELETE
        
    delete_admin_from_db(username)
    await update.message.reply_text(f"Админ {username} успешно удален.")
    return ConversationHandler.END


# ----------------- Основная функция -----------------

def main() -> None:
    """Запуск бота."""
    try:
        telegram_token = get_telegram_token()
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        logger.critical(f"Failed to get Telegram token: {e}")
        return

    init_db()
    
    # Добавление суперадмина из INI, если он ещё не существует
    super_admin_id = get_super_admin_id()
    if super_admin_id:
        add_admin_to_db(f"admin_{super_admin_id}", "superadmin_password", is_super=True)
        logger.info(f"Super admin user 'admin_{super_admin_id}' ensured to exist.")
    else:
        logger.warning("Super admin ID not configured in settings.ini. Admin management commands will not work.")

    application = Application.builder().token(telegram_token).build()

    # Ветвь для создания и редактирования профиля
    profile_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("my_profile", my_profile),
            MessageHandler(filters.Regex("^Мой профиль$"), my_profile)
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(edit_profile_callback, pattern="^edit_"),
            ],
            EDITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
            EDITING_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_age)],
            EDITING_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_description)],
            EDITING_PHOTO: [MessageHandler(filters.PHOTO & ~filters.COMMAND, save_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel_profile_edit)]
    )

    # Ветвь для жалоб
    report_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_report_callback, pattern="^report_user$")],
        states={
            WAITING_REPORT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_report)],
        },
        fallbacks=[CommandHandler("cancel", cancel_profile_edit)]
    )
    
    # Ветвь для админ-панели
    admin_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("admin_panel", show_admin_menu),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_menu$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_moderate_profiles$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_view_reports$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_view_banned$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_manage_admins$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_add_new$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_delete_existing$"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_unfreeze:"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_ban:"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_process_report:"),
            CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_unban:"),
        ],
        states={
            CHOOSING: [CallbackQueryHandler(handle_admin_callbacks)],
            AWAITING_BAN_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user_reason)],
            AWAITING_NEW_ADMIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_new_admin)],
            AWAITING_ADMIN_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_admin_by_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel_profile_edit)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(profile_conv_handler)
    application.add_handler(report_conv_handler)
    application.add_handler(admin_conv_handler)
    application.add_handler(CommandHandler("admin", admin_login))
    application.add_handler(CommandHandler("add_admin", add_new_admin))
    application.add_handler(CommandHandler("delete_admin", delete_admin_by_name))
    application.add_handler(MessageHandler(filters.Regex("^Найти пару$"), find_pair))
    application.add_handler(MessageHandler(filters.Regex("^Лайки$"), show_matches))
    application.add_handler(CallbackQueryHandler(profile_actions, pattern="^(like|next)$"))
    application.add_handler(CallbackQueryHandler(profile_actions, pattern="^next$"))

    # Запускаем бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
