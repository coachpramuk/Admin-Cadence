# -*- coding: utf-8 -*-
"""
Telegram-бот спортивного клуба / беговых тренировок.
Короткие сообщения, кнопки, сценарии: запись, цены, адрес, форма, расписание.
"""

import logging
import re
from html import escape
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Состояния сценария записи: день → слот → [тренер для пн/ср] → уровень → контакт → подтверждение ---
REG_DAY, REG_SLOT, REG_TRAINER, REG_LEVEL, REG_CONTACT, REG_CONFIRM = range(6)

# --- Дни недели (кнопки при записи) ---
DAY_BUTTONS = [
    ("mon", "Понедельник"),
    ("tue", "Вторник"),
    ("wed", "Среда"),
    ("thu", "Четверг"),
    ("fri", "Пятница"),
    ("sun", "Воскресенье"),
]
# Подписи кнопок дней с эмодзи типа тренировки (🏃‍♂️ бег, 🏋️‍♂️ зал)
DAY_EMOJI_LABEL = {
    "mon": "🏃‍♂️ Понедельник",
    "tue": "🏃‍♂️ Вторник",
    "wed": "🏃‍♂️🏋️‍♂️ Среда",
    "thu": "🏃‍♂️ Четверг",
    "fri": "🏋️‍♂️ Пятница",
    "sun": "🏃‍♂️ Воскресенье",
}

# --- Слоты по дню: (slot_id, label). Только актуальные варианты. ---
# slot_id используется для определения адреса (run/gym/long)
SLOTS_BY_DAY = {
    "mon": [("mon_run", "🏃‍♂️ Беговая 19:20–20:50")],
    "tue": [
        ("tue_morning", "🏃‍♂️ Утро 07:30–09:00 (Виталик)"),
        ("tue_evening", "🏃‍♂️ Вечер 19:10–20:40 (Виталик)"),
    ],
    "wed": [
        ("wed_gym", "🏋️‍♂️ Силовая (зал) 07:30–08:40"),
        ("wed_run", "🏃‍♂️ Беговая 19:20–20:50"),
    ],
    "thu": [
        ("thu_morning", "🏃‍♂️ Утро 07:30–09:00 (Виталик)"),
        ("thu_evening", "🏃‍♂️ Вечер 19:10–20:40 (Виталик)"),
    ],
    "fri": [("fri_gym", "🏋️‍♂️ Силовая (зал) 19:10–20:20")],
    "sun": [("sun_long", "🏃‍♂️ Длительная беговая 09:00–10:30, Раубичи")],
}

# --- slot_id → тип адреса (run / gym / long) ---
SLOT_TO_ADDRESS_TYPE = {
    "mon_run": "run",
    "tue_morning": "run",
    "tue_evening": "run",
    "wed_gym": "gym",
    "wed_run": "run",
    "thu_morning": "run",
    "thu_evening": "run",
    "fri_gym": "gym",
    "sun_long": "long",
}

# --- slot_id → текст слота для подтверждения и финала (для mon_run/wed_run подставляется тренер) ---
SLOT_TO_LABEL = {
    "mon_run": "Понедельник — Беговая 19:20–20:50",  # + тренер после выбора
    "tue_morning": "Вторник — Беговая утро 07:30–09:00 (Виталик)",
    "tue_evening": "Вторник — Беговая вечер 19:10–20:40 (Виталик)",
    "wed_gym": "Среда — Силовая (зал) 07:30–08:40",
    "wed_run": "Среда — Беговая 19:20–20:50",  # + тренер после выбора
    "thu_morning": "Четверг — Беговая утро 07:30–09:00 (Виталик)",
    "thu_evening": "Четверг — Беговая вечер 19:10–20:40 (Виталик)",
    "fri_gym": "Пятница — Силовая (зал) 19:10–20:20",
    "sun_long": "Воскресенье — Длительная беговая 09:00–10:30, Раубичи",
}

# --- Адреса (без parse_mode). Беговые: Калиновского 111, затем Манеж-стадион. ---
ADDRESS_RUN = (
    "Адрес тренировки\n\n"
    "📍 Калиновского, 111\n"
    "Манеж-стадион"
)
ADDRESS_GYM = (
    "Адрес тренировки\n\n"
    "📍 Старовиленская, 131/1\n"
    "(зал)"
)
ADDRESS_LONG = (
    "Адрес тренировки\n\n"
    "📍 Раубичи\n"
    "длительная беговая тренировка (лонг)"
)

# --- Короткие адреса для финального сообщения (одна строка «📍 Локация: ...») ---
LOCATION_SHORT = {
    "run": "Калиновского, 111",
    "gym": "Старовиленская, 131/1",
    "long": "Раубичи",
}


def _location_geo_url(address: str) -> str:
    """Ссылка Google Maps: адрес + Минск, Беларусь (URL-кодирование)."""
    query = f"{address}, Минск, Беларусь"
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"

# --- Тип тренировки, время и тренер для финального сообщения ---
ADDRESS_TYPE_LABEL = {"run": "Беговая", "gym": "Силовая", "long": "Длительная"}
# Только тип для сообщения админу (день и время — отдельными строками)
ADMIN_TRAINING_LABEL = {"run": "Беговая", "gym": "Силовая (зал)", "long": "Длительная"}
# Тип (формат/место) для карточки и однострочного подтверждения
CARD_TRAINING_LABEL = {"run": "Беговая (улица)", "gym": "Силовая (зал)", "long": "Длительная"}
DAY_LABEL = dict(DAY_BUTTONS)  # mon → Понедельник, tue → Вторник, ...
SLOT_TO_TIME = {
    "mon_run": "19:20–20:50",
    "tue_morning": "07:30–09:00",
    "tue_evening": "19:10–20:40",
    "wed_gym": "07:30–08:40",
    "wed_run": "19:20–20:50",
    "thu_morning": "07:30–09:00",
    "thu_evening": "19:10–20:40",
    "fri_gym": "19:10–20:20",
    "sun_long": "09:00–10:30",
}
# Тренер по умолчанию для слота (для mon_run/wed_run подставляется r["trainer"] — Даша/Максим)
# Силовые (ср, пт) — всегда Виталик
SLOT_TO_TRAINER = {
    "tue_morning": "Виталик",
    "tue_evening": "Виталик",
    "thu_morning": "Виталик",
    "thu_evening": "Виталик",
    "wed_gym": "Виталик",
    "fri_gym": "Виталик",
    "sun_long": "—",
}

# --- Расписание (без дубликатов: каждый день один раз, утро/вечер в одной строке) ---
SCHEDULE_FULL = (
    "Расписание\n\n"
    "🏃‍♂️ БЕГОВЫЕ ТРЕНИРОВКИ — ВИТАЛИК\n"
    "📍 Калиновского, 111\n"
    "Манеж-стадион\n"
    "• Вторник — утро 07:30–09:00, вечер 19:10–20:40\n"
    "• Четверг — утро 07:30–09:00, вечер 19:10–20:40\n\n"
    "🏃‍♂️ БЕГОВЫЕ ТРЕНИРОВКИ — ДАША И МАКСИМ\n"
    "📍 Калиновского, 111\n"
    "Манеж-стадион\n"
    "• Понедельник — 19:20–20:50\n"
    "• Среда — 19:20–20:50\n\n"
    "🏋️‍♂️ СИЛОВЫЕ ТРЕНИРОВКИ (ЗАЛ) — ВИТАЛИК\n"
    "📍 Старовиленская, 131/1\n"
    "• Среда — 07:30–08:40\n"
    "• Пятница — 19:10–20:20\n\n"
    "🏃‍♂️ ДЛИТЕЛЬНАЯ БЕГОВАЯ ТРЕНИРОВКА\n"
    "• Воскресенье — 09:00–10:30\n"
    "📍 Раубичи\n"
    "длительная беговая тренировка (лонг)"
)

# --- Форма для зала (силовые): отдельный текст, без уличных рекомендаций ---
FORM_GYM = (
    "Что надеть в зал\n\n"
    "• Удобные шорты или леггинсы\n"
    "• Майка или футболка\n"
    "• Кроссовки для зала с хорошей фиксацией\n"
    "• При необходимости — бутылка воды и полотенце"
)

# --- Что надеть: Зал / Манеж / Улица (экран выбора) ---
FORM_WEAR_GYM = (
    "🏋️‍♂️ Что надеть в зал (силовая тренировка)\n\n"
    "• Удобная спортивная форма\n"
    "• Кроссовки для зала\n"
    "• Носки\n"
    "• Бутылка воды\n"
    "• Полотенце\n\n"
    "По желанию:\n"
    "• Перчатки для тренировок\n"
    "• Ремень или личная экипировка"
)

FORM_WEAR_MANEGE = (
    "🏃‍♂️ Что надеть в манеж (беговая тренировка)\n\n"
    "• Лёгкая спортивная форма\n"
    "• Кроссовки для бега по покрытию\n"
    "• Носки\n"
    "• Бутылка воды\n\n"
    "По желанию:\n"
    "• Лёгкая кофта для разминки\n"
    "• Часы или трекер"
)

FORM_WEAR_STREET_WARM = (
    "☀️ Что надеть, когда тепло\n\n"
    "• Футболка или майка\n"
    "• Шорты или тайтсы\n"
    "• Кроссовки для бега\n"
    "• Кепка\n"
    "• Вода обязательно"
)

FORM_WEAR_STREET_COOL = (
    "🧢 Что надеть, когда прохладно\n\n"
    "• Лонгслив или лёгкая кофта\n"
    "• Тайтсы или лёгкие штаны\n"
    "• Лёгкая ветровка\n"
    "• Кроссовки\n"
    "• Бафф или тонкая шапка — по желанию"
)

FORM_WEAR_STREET_COLD = (
    "🧥 Что надеть, когда холодно\n\n"
    "• Термобельё\n"
    "• Тёплый лонгслив или кофта\n"
    "• Ветровка\n"
    "• Тайтсы\n"
    "• Шапка и перчатки\n"
    "• Кроссовки по погоде"
)

FORM_WEAR_STREET_RAIN = (
    "🌧 Что надеть в дождь\n\n"
    "• Ветровка или дождевик\n"
    "• Быстросохнущая форма\n"
    "• Тайтсы или штаны\n"
    "• Кроссовки с хорошим сцеплением\n"
    "• Кепка"
)

# --- Финальные рекомендации ПОСЛЕ подтверждения записи (только после «✅ Да») ---
# Беговые (пн/вт/ср/чт): что взять с собой + душ
FORM_RUN_AFTER_CONFIRM = (
    "🏃‍♂️ Что взять с собой на тренировку\n\n"
    "• Бутылку воды\n"
    "• Кроссовки по погоде\n"
    "• Одежду по погоде\n\n"
    "🚿 После тренировки можно помыться — возьмите вещи для душа: полотенце, шампунь, гель."
)
# Силовые (зал): что взять + душ
FORM_GYM_AFTER_CONFIRM = (
    "🏋️‍♂️ Что взять с собой на тренировку\n\n"
    "• Удобную спортивную одежду для зала\n"
    "• Кроссовки для зала\n"
    "• Бутылку воды\n\n"
    "🚿 После тренировки можно помыться — возьмите вещи для душа: полотенце, шампунь, гель."
)

# --- Блок в конце финального подтверждения (все сценарии): вопросы → руководитель ---
FINAL_CONFIRM_FOOTER = "Если остались вопросы — напишите руководителю: @coach_pramuk"

# --- Кнопка приветствия (первый экран) ---
def start_welcome_keyboard():
    """Одна кнопка «Старт» — ведёт в основное меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Старт", callback_data="menu:start")],
    ])


# --- Кнопки основного меню (эмодзи + короткие названия) ---
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("🗓 Расписание", callback_data="menu:schedule"),
        ],
        [
            InlineKeyboardButton("💰 Цены", callback_data="menu:price"),
            InlineKeyboardButton("📍 Локации", callback_data="menu:locations"),
        ],
        [
            InlineKeyboardButton("❓ Задать вопрос", callback_data="menu:question"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


# --- Кнопка «Начать заново» (анти-тупик: всегда есть выход) ---
def restart_keyboard():
    """Одна кнопка «Начать заново» — сброс диалога и показ стартового экрана."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
    ])


def menu_and_restart_keyboard():
    """«⬅️ Назад в меню» и «Начать заново» — для экранов, где диалог может закончиться."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


# --- /start ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фиксированное приветствие + кнопка «🚀 Старт» при каждом /start."""
    text = (
        "Привет! 👋\n\n"
        "Я помогу записаться на тренировку и отвечу на вопросы.\n\n"
        "Нажмите кнопку ниже, чтобы начать 👇"
    )
    await update.message.reply_text(text, reply_markup=start_welcome_keyboard())
    return ConversationHandler.END


# --- /myid — показать пользователю его chat_id (для админа: подставить в config.ADMIN_CHAT_ID) ---
async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"Ваш chat_id: <code>{chat_id}</code>.\n\nЕсли вы админ — подставьте это число в config.ADMIN_CHAT_ID.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# --- Команды меню: /menu, /register, /prices, /schedule, /location, /question, /restart ---
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /menu — показать главное меню."""
    if not update.message:
        return ConversationHandler.END
    context.user_data.pop("reg", None)
    await update.message.reply_text(
        "Чем помочь?\n\nВыберите 👇",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cmd_register_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /register — вход в сценарий записи (день → слот → …). Используется как entry_point."""
    if not update.message:
        return ConversationHandler.END
    context.user_data["reg"] = {}
    await update.message.reply_text(
        "Выберите день недели 👇",
        reply_markup=_day_keyboard(),
    )
    return REG_DAY


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /prices — показать цены."""
    if not update.message:
        return
    context.user_data.pop("reg", None)
    text, keyboard = get_price_text_and_keyboard()
    await update.message.reply_text(text, reply_markup=keyboard)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /schedule — показать расписание."""
    if not update.message:
        return
    context.user_data.pop("reg", None)
    try:
        text = _build_schedule_text()
    except Exception as e:
        logger.exception("Ошибка в cmd_schedule: %s", e)
        text = "Расписание\n\nНе удалось загрузить данные. Попробуйте позже или напишите в чат 👇"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("📍 Адрес", callback_data="menu:locations"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=keyboard)


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /location — показать экран выбора локаций."""
    if not update.message:
        return
    context.user_data.pop("reg", None)
    text = "Адрес\n\nВыберите тип тренировки 👇"
    await update.message.reply_text(text, reply_markup=_locations_choice_keyboard())


async def cmd_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /question — показать кнопки тем (что надеть, что взять, как проходят, свой вопрос)."""
    if not update.message:
        return
    context.user_data.pop("reg", None)
    await update.message.reply_text(
        "Выберите тему 👇",
        reply_markup=_question_topics_keyboard(),
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /restart — стартовый экран (сброс диалога)."""
    if not update.message:
        return ConversationHandler.END
    context.user_data.pop("reg", None)
    text = (
        "Привет 👋\n\n"
        "• Запись на тренировку\n"
        "• Ответы на вопросы\n\n"
        "Нажмите кнопку ниже 👇"
    )
    await update.message.reply_text(text, reply_markup=start_welcome_keyboard())
    return ConversationHandler.END


# --- Пересылка входящих текстовых сообщений админу ---
async def notify_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет админу имя, username и текст сообщения пользователя. Вызывается после основных обработчиков (group=99)."""
    if not config.ADMIN_CHAT_ID:
        # TODO: подставьте свой chat_id в config.ADMIN_CHAT_ID (узнать через /myid)
        return
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    username = f"@{user.username}" if user.username else "—"
    text = update.message.text.strip()
    safe_name = escape(name)
    safe_username = escape(username)
    safe_text = escape(text)
    msg = (
        "📩 <b>От пользователя:</b>\n"
        f"Имя: {safe_name}\n"
        f"Username: {safe_username}\n"
        f"chat_id: {user.id}\n\n"
        f"Текст: {safe_text}"
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось отправить сообщение админу: %s", e)


# --- Сценарий: Записаться (день → время/слот → уровень → контакт → подтверждение) ---
def _day_keyboard():
    """Кнопки дней недели с эмодзи типа тренировки (🏃‍♂️ бег, 🏋️‍♂️ зал) + выход."""
    buttons = [
        [InlineKeyboardButton(DAY_EMOJI_LABEL.get(day, label), callback_data=f"reg:day:{day}")]
        for day, label in DAY_BUTTONS
    ]
    buttons.append([
        InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
        InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
    ])
    return InlineKeyboardMarkup(buttons)


def _slot_keyboard(day: str):
    """Кнопки слотов только для выбранного дня (без лишних вариантов)."""
    slots = SLOTS_BY_DAY.get(day, [])
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"reg:slot:{slot_id}")]
        for slot_id, label in slots
    ]
    buttons.append([
        InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
        InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
    ])
    return InlineKeyboardMarkup(buttons)


def _trainer_keyboard():
    """Кнопки выбора тренера для понедельника и среды (Даша / Максим)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Даша", callback_data="reg:trainer:dasha"),
            InlineKeyboardButton("Максим", callback_data="reg:trainer:maxim"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


def _level_keyboard():
    """Кнопки уровня + выход."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Новичок", callback_data="reg:level:newbie"),
            InlineKeyboardButton("Средний", callback_data="reg:level:medium"),
        ],
        [
            InlineKeyboardButton("Продвинутый", callback_data="reg:level:advanced"),
            InlineKeyboardButton("Не знаю", callback_data="reg:level:unknown"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


async def menu_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["reg"] = {}
    await query.edit_message_text(
        "Выберите день недели 👇",
        reply_markup=_day_keyboard(),
    )
    return REG_DAY


async def reg_choose_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = query.data.replace("reg:day:", "")
    context.user_data["reg"]["day"] = day
    keyboard = _slot_keyboard(day)
    await query.edit_message_text(
        "Выберите тренировку 👇",
        reply_markup=keyboard,
    )
    return REG_SLOT


async def reg_choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot_id = query.data.replace("reg:slot:", "")
    r = context.user_data["reg"]
    r["slot_id"] = slot_id
    r["slot"] = SLOT_TO_LABEL.get(slot_id, slot_id)
    # Понедельник и среда: сначала выбор тренера (Даша / Максим)
    if slot_id in ("mon_run", "wed_run"):
        await query.edit_message_text(
            "Выберите тренера 👇",
            reply_markup=_trainer_keyboard(),
        )
        return REG_TRAINER
    # Силовые (зал) и остальные слоты: сразу уровень (рекомендации по форме — только после подтверждения)
    await query.edit_message_text(
        "Ваш уровень?\n\nНажмите кнопку ниже 👇",
        reply_markup=_level_keyboard(),
    )
    return REG_LEVEL


async def reg_choose_trainer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранить тренера (Даша/Максим) для пн/ср и перейти к уровню."""
    query = update.callback_query
    await query.answer()
    trainer = query.data.replace("reg:trainer:", "")  # dasha | maxim
    trainer_label = "Даша" if trainer == "dasha" else "Максим"
    r = context.user_data["reg"]
    r["trainer"] = trainer_label
    base = SLOT_TO_LABEL.get(r.get("slot_id", ""), "")
    r["slot"] = f"{base}, {trainer_label}"
    await query.edit_message_text(
        "Ваш уровень?\n\nНажмите кнопку ниже 👇",
        reply_markup=_level_keyboard(),
    )
    return REG_LEVEL


async def reg_choose_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    level_map = {"newbie": "Новичок", "medium": "Средний", "advanced": "Продвинутый", "unknown": "Не знаю"}
    part = query.data.replace("reg:level:", "")
    context.user_data["reg"]["level"] = level_map.get(part, part)
    await query.edit_message_text(
        "Контакт для связи\n\n"
        "• Имя и телефон или @ник в Telegram\n\n"
        "Напишите одним сообщением 👇",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
        ]),
    )
    return REG_CONTACT


def _build_confirmation_line(r: dict) -> str:
    """Одна строка подтверждения: день • тип (формат/место) • время • уровень (без эмодзи)."""
    day_label = DAY_LABEL.get(r.get("day", ""), "—")
    slot_id = r.get("slot_id", "")
    address_type = SLOT_TO_ADDRESS_TYPE.get(slot_id, "run")
    card_label = CARD_TRAINING_LABEL.get(address_type, "—")
    time_str = SLOT_TO_TIME.get(slot_id, "—")
    level = r.get("level", "—")
    return f"{day_label} • {card_label} • {time_str} • {level}"


def _build_check_message(r: dict, user) -> str:
    """Сообщение проверки для клиента: без строки-резюме, карточка + навигатор (HTML-ссылка)."""
    slot_id = r.get("slot_id", "")
    address_type = SLOT_TO_ADDRESS_TYPE.get(slot_id, "run")
    location_line = LOCATION_SHORT.get(address_type, "Калиновского, 111")
    day_label = DAY_LABEL.get(r.get("day", ""), "—")
    card_label = CARD_TRAINING_LABEL.get(address_type, "—")
    time_str = SLOT_TO_TIME.get(slot_id, "—")
    level = r.get("level", "—")
    contact = r.get("contact", "—")
    name_part = (user.first_name or "").strip()
    if user.last_name:
        name_part = (name_part + " " + (user.last_name or "").strip()).strip()
    if not name_part and user.username:
        name_part = f"@{user.username}"
    if not name_part:
        name_part = "—"
    geo_url = _location_geo_url(location_line)
    geo_url_escaped = geo_url.replace("&", "&amp;")
    navigator_line = f'🧭 Навигатор: <a href="{geo_url_escaped}">Открыть локацию</a>'
    lines = [
        "Проверьте, пожалуйста, правильно ли заполнены данные:",
        "",
        "📝 Новая запись на тренировку",
        "",
        f"👤 Имя: {escape(name_part)}",
        f"📞 Контакт: {escape(contact)}",
        f"📅 День: {escape(day_label)}",
        f"🏃‍♂️ Тренировка: {escape(card_label)}",
        f"⏰ Время: {escape(time_str)}",
        f"🎯 Уровень: {escape(level)}",
        f"📍 Локация: {escape(location_line)}",
        navigator_line,
        "",
        "Всё верно? 👇",
    ]
    return "\n".join(lines)


async def reg_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "Напишите имя и контакт одним сообщением 👇",
            reply_markup=restart_keyboard(),
        )
        return REG_CONTACT
    context.user_data["reg"]["contact"] = update.message.text.strip()
    r = context.user_data["reg"]
    user = update.effective_user
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="reg:confirm:yes")],
        [InlineKeyboardButton("Изменить", callback_data="reg:confirm:change")],
        [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
    ])
    text = _build_check_message(r, user)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    return REG_CONFIRM


def _build_admin_registration_text(r: dict, user, location_line: str, address_type: str, slot_id: str) -> str:
    """Формирует текст формы записи для отправки администратору (без parse_mode).
    День и время — отдельными строками; в строке «Тренировка» только тип (Беговая / Силовая (зал) / Длительная).
    """
    name_part = (user.first_name or "").strip()
    if user.last_name:
        name_part = (name_part + " " + (user.last_name or "").strip()).strip()
    if not name_part and user.username:
        name_part = f"@{user.username}"
    if not name_part:
        name_part = "—"
    day_label = DAY_LABEL.get(r.get("day", ""), "—")
    training_label = ADMIN_TRAINING_LABEL.get(address_type, "—")
    time_str = SLOT_TO_TIME.get(slot_id, "—")
    lines = [
        "📝 Новая запись на тренировку",
        "",
        f"👤 Имя: {name_part}",
        f"📞 Контакт: {r.get('contact', '—')}",
        f"📅 День: {day_label}",
        f"🏃‍♂️ Тренировка: {training_label}",
        f"⏰ Время: {time_str}",
        f"🎯 Уровень: {r.get('level', '—')}",
        f"📍 Локация: {location_line}",
    ]
    return "\n".join(lines)


async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "reg:confirm:change":
        await query.edit_message_text(
            "Выберите день недели 👇",
            reply_markup=_day_keyboard(),
        )
        return REG_DAY
    # Да — одно финальное сообщение: подтверждение + локация + «что взять» (адрес отдельно не отправляем)
    r = context.user_data["reg"]
    slot_id = r.get("slot_id", "")
    address_type = SLOT_TO_ADDRESS_TYPE.get(slot_id, "run")
    location_line = LOCATION_SHORT.get(address_type, "Калиновского, 111")

    # Тихо отправить копию формы администратору (пользователь не видит)
    if config.ADMIN_CHAT_ID:
        try:
            user = update.effective_user
            admin_text = _build_admin_registration_text(r, user, location_line, address_type, slot_id)
            await context.bot.send_message(chat_id=config.ADMIN_CHAT_ID, text=admin_text)
        except Exception as e:
            logger.exception("Не удалось отправить форму записи админу: %s", e)

    day_label = DAY_LABEL.get(r.get("day", ""), "—")
    card_training_label = CARD_TRAINING_LABEL.get(address_type, "—")
    time_raw = SLOT_TO_TIME.get(slot_id, "—")
    time_display = ("с " + time_raw.replace("–", " до ", 1)) if "–" in time_raw else time_raw
    trainer_name = r.get("trainer") or SLOT_TO_TRAINER.get(slot_id, "—")
    level = r.get("level", "—")

    geo_url = _location_geo_url(location_line)
    geo_url_escaped = geo_url.replace("&", "&amp;")
    navigator_line = f'🧭 Навигатор: <a href="{geo_url_escaped}">Открыть локацию</a>'

    lines = [
        "Записали вас ✅",
        "",
        f"📅 День: {escape(day_label)}",
        f"🏃‍♂️ Тренировка: {escape(card_training_label)}",
        f"⏰ Время: {escape(time_display)}",
        f"🎯 Уровень: {escape(level)}",
        f"📍 Локация: {escape(location_line)}",
        navigator_line,
        f"👤 Тренер: {escape(trainer_name)}",
        "",
    ]
    if address_type == "gym":
        lines.append(FORM_GYM_AFTER_CONFIRM)
    else:
        lines.append(FORM_RUN_AFTER_CONFIRM)
    lines.append("")
    lines.append(FINAL_CONFIRM_FOOTER)
    if config.PAYMENT_INFO:
        lines.append(f"Оплата: {config.PAYMENT_INFO}")
    if config.CONTACT_ADMIN:
        lines.append(f"Контакт: {config.CONTACT_ADMIN}")
    final_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=final_keyboard, parse_mode="HTML")
    context.user_data.pop("reg", None)
    return ConversationHandler.END


# --- Цены: выбор тренера (Максим | Даша / Виталик) ---
def _price_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Максим | Даша", callback_data="price:maksim_dasha")],
        [InlineKeyboardButton("Виталик", callback_data="price:vitalik")],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


PRICE_TEXT_MAKSIM_DASHA = (
    "💰 Цены на тренировки\n\n"
    "Максим\n"
    "────────\n"
    "• Разовое занятие — 30 BYN\n"
    "• Абонемент на 4 занятия — 100 BYN\n"
    "• Абонемент на 8 занятий — 180 BYN\n\n"
    "Даша\n"
    "────────\n"
    "• Разовое занятие — 30 BYN\n"
    "• Абонемент на 4 занятия — 100 BYN\n"
    "• Абонемент на 8 занятий — 180 BYN"
)

VITALIK_INFO_TEXT = (
    "ℹ️ Информация о тренировках\n\n"
    "Стоимость и возможность записи на тренировки к Виталику\n"
    "уточняются индивидуально и зависят от наличия свободных мест.\n\n"
    "Для уточнения актуальной информации напишите в Telegram:\n"
    "👉 @coach_pramuk"
)


def _price_maksim_dasha_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
        ],
        [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
    ])


async def menu_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        "Выберите тренера 👇",
        reply_markup=_price_choice_keyboard(),
    )
    return ConversationHandler.END


async def price_maksim_dasha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        PRICE_TEXT_MAKSIM_DASHA,
        reply_markup=_price_maksim_dasha_keyboard(),
    )
    return ConversationHandler.END


async def price_vitalik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        VITALIK_INFO_TEXT,
        reply_markup=_price_maksim_dasha_keyboard(),
    )
    return ConversationHandler.END


def get_price_text_and_keyboard():
    """Для триггера по тексту «цена» и /prices: показываем выбор тренера (то же, что menu_price)."""
    text = "Выберите тренера 👇"
    keyboard = _price_choice_keyboard()
    return text, keyboard


# --- Сценарий: Адрес ---
async def menu_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reg", None)
    await _reply_address(update, is_callback=True)
    return ConversationHandler.END


async def address_transport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "addr:car":
        msg = (
            "Парковка\n\n"
            "• У места старта\n"
            "• Геоточку или подсказку — напишите, скину или передам админу\n\n"
            "Записать на тренировку? 👇"
        )
    else:
        msg = (
            "Пешком / транспорт\n\n"
            "• Маршрут от метро/остановки — у админа или скину гео\n"
            "• Напишите район — подскажу\n\n"
            "Записать на тренировку? 👇"
        )
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
                InlineKeyboardButton("📍 Адрес", callback_data="menu:locations"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
                InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
            ],
        ]),
    )
    return ConversationHandler.END


# --- Сценарий: Что надеть (Зал / Манеж / Улица) ---
def _form_place_keyboard():
    """Три кнопки: Зал, Манеж, Улица."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Зал", callback_data="form:gym")],
        [InlineKeyboardButton("Манеж", callback_data="form:manege")],
        [InlineKeyboardButton("Улица", callback_data="form:street")],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


def _form_weather_keyboard():
    """Четыре кнопки погоды для «Улица»."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Тепло", callback_data="form:weather:warm"),
            InlineKeyboardButton("Прохладно", callback_data="form:weather:cool"),
        ],
        [
            InlineKeyboardButton("Холодно", callback_data="form:weather:cold"),
            InlineKeyboardButton("Дождь", callback_data="form:weather:rain"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


def _form_result_keyboard():
    """Кнопки после показа текста «Что надеть» — только навигация, без адреса и ссылок (раздел исключительно информационный)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


async def menu_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reg", None)
    await _reply_form(update, is_callback=True)
    return ConversationHandler.END


async def form_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора: Зал / Манеж / Улица."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    data = query.data.replace("form:", "")
    if data == "gym":
        await query.edit_message_text(
            FORM_WEAR_GYM,
            reply_markup=_form_result_keyboard(),
        )
    elif data == "manege":
        await query.edit_message_text(
            FORM_WEAR_MANEGE,
            reply_markup=_form_result_keyboard(),
        )
    else:
        # Улица — показать выбор погоды
        await query.edit_message_text(
            "Погода у вас? 👇",
            reply_markup=_form_weather_keyboard(),
        )
    return ConversationHandler.END


async def form_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора погоды для «Улица»: Тепло / Прохладно / Холодно / Дождь."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    key = query.data.replace("form:weather:", "")
    texts = {
        "warm": FORM_WEAR_STREET_WARM,
        "cool": FORM_WEAR_STREET_COOL,
        "cold": FORM_WEAR_STREET_COLD,
        "rain": FORM_WEAR_STREET_RAIN,
    }
    text = texts.get(key, FORM_WEAR_STREET_WARM)
    await query.edit_message_text(
        text,
        reply_markup=_form_result_keyboard(),
    )
    return ConversationHandler.END


# --- Сценарий: Расписание ---
async def menu_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reg", None)
    await _reply_schedule(update, is_callback=True)
    return ConversationHandler.END


# --- Сценарий: Локации ---
async def menu_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reg", None)
    await _reply_locations(update, is_callback=True)
    return ConversationHandler.END


# --- Кнопка «Старт»: показать основное меню ---
async def menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """По нажатию «Старт» — показать основное меню. Сбрасывает активный диалог (fallback)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Чем помочь?\n\nВыберите 👇",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# --- Кнопка «Начать заново»: стартовый экран (сброс любого диалога) ---
async def menu_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """По нажатию «Начать заново» — сброс диалога и показ приветствия + кнопка «Старт»."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    text = (
        "Привет 👋\n\n"
        "• Запись на тренировку\n"
        "• Ответы на вопросы\n\n"
        "Нажмите кнопку ниже 👇"
    )
    await query.edit_message_text(text, reply_markup=start_welcome_keyboard())
    return ConversationHandler.END


# --- Возврат в главное меню ---
async def menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        "Чем помочь?\n\nВыберите 👇",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# --- Задать вопрос: сразу кнопки тем ---
def _question_topics_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Что надеть?", callback_data="question:form")],
        [InlineKeyboardButton("Что взять с собой?", callback_data="question:what_to_take")],
        [InlineKeyboardButton("Как проходят тренировки?", callback_data="question:how")],
        [InlineKeyboardButton("Задать свой вопрос", callback_data="question:custom")],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


async def menu_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        "Выберите тему 👇",
        reply_markup=_question_topics_keyboard(),
    )
    return ConversationHandler.END


# Текст для темы «Как проходят тренировки»
# Тексты для темы «Как проходят тренировки» (Беговые / Силовые / Длительные)
QUESTION_HOW_RUN = (
    "🏃‍♂️ БЕГОВЫЕ ТРЕНИРОВКИ\n"
    "────────────────────\n\n"
    "Тренировки проходят на стадионе или на улице и выстроены\n"
    "по полной структуре:\n\n"
    "• разминка\n"
    "• основная часть\n"
    "• заминка\n\n"
    "В процессе уделяется внимание:\n"
    "• общей физической подготовке\n"
    "• общеразвивающим упражнениям\n"
    "• беговым упражнениям и технике\n\n"
    "Тренировки методически структурированы и адаптируются\n"
    "под индивидуальные цели и уровень каждого участника.\n\n"
    "────────────────────"
)

QUESTION_HOW_STRENGTH = (
    "🏋️‍♂️ СИЛОВЫЕ ТРЕНИРОВКИ\n"
    "────────────────────\n\n"
    "Тренировки проходят в зале и имеют чёткую структуру занятия.\n\n"
    "Основной акцент делается на:\n"
    "• развитие силы\n"
    "• развитие силовой выносливости\n\n"
    "Дополнительно развиваются:\n"
    "• координация\n"
    "• мобильность\n"
    "• общая физическая подготовка\n\n"
    "Упражнения подбираются с учётом уровня подготовки\n"
    "и индивидуальных целей.\n\n"
    "────────────────────"
)

QUESTION_HOW_LONG = (
    "🏃‍♂️ ДЛИТЕЛЬНЫЕ ВЫЕЗДНЫЕ БЕГОВЫЕ\n"
    "────────────────────\n\n"
    "Это совместная длительная пробежка на природе\n"
    "(выездные локации, например Раубичи).\n\n"
    "Цель тренировки:\n"
    "• развитие сердечно-сосудистой системы\n"
    "• повышение выносливости\n"
    "• комфортный, спокойный темп\n\n"
    "После тренировки:\n"
    "☕ чай, кофе\n"
    "🥐 завтраки, пирожные\n"
    "и приятное общение.\n\n"
    "────────────────────"
)


def _question_how_keyboard():
    """Первый уровень: Беговые / Силовые / Длительные + Назад в меню (🏃‍♂️ бег, длительные; 🏋️‍♂️ силовые)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏃‍♂️ Беговые", callback_data="how:run")],
        [InlineKeyboardButton("🏋️‍♂️ Силовые", callback_data="how:strength")],
        [InlineKeyboardButton("🏃‍♂️ Длительные", callback_data="how:long")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main")],
    ])


def _question_how_result_keyboard():
    """Второй уровень: после текста — Назад в меню и Начать заново."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])

# Текст для темы «Что взять с собой»
QUESTION_WHAT_TO_TAKE_TEXT = (
    "🎒 Что взять с собой на тренировку\n\n"
    "✅ Обязательно:\n"
    "• Спортивная форма (по формату тренировки)\n"
    "• Спортивная обувь:\n"
    "  — для зала\n"
    "  — для бега (улица / манеж)\n"
    "• Бутылка воды\n"
    "• Полотенце\n\n"
    "🚿 Если планируете принять душ:\n"
    "• Сланцы\n"
    "• Средства для душа\n"
    "• Сменная одежда\n\n"
    "➕ Дополнительно (по желанию):\n"
    "• Резинка для волос\n"
    "• Личная экипировка\n"
    "• Небольшой рюкзак или сумка\n\n"
    "ℹ️ Важно:\n"
    "Форму и обувь подбирайте с учётом погодных условий\n"
    "и типа тренировки: зал / улица / манеж"
)

# Текст для «Задать свой вопрос»
QUESTION_CUSTOM_PROMPT = (
    "✍️ Задайте свой вопрос\n\n"
    "Напишите ваш вопрос сообщением,\n"
    "и мы обязательно вам ответим."
)


async def question_topic_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await _reply_form(update, is_callback=True)
    return ConversationHandler.END


async def question_topic_how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«Как проходят тренировки» — показать три кнопки: Беговые / Силовые / Длительные."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        "Выберите тип тренировки 👇",
        reply_markup=_question_how_keyboard(),
    )
    return ConversationHandler.END


async def question_how_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать текст по типу: Беговые / Силовые / Длительные + Назад в меню, Начать заново."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    key = query.data.replace("how:", "")
    texts = {
        "run": QUESTION_HOW_RUN,
        "strength": QUESTION_HOW_STRENGTH,
        "long": QUESTION_HOW_LONG,
    }
    text = texts.get(key, QUESTION_HOW_RUN)
    await query.edit_message_text(text, reply_markup=_question_how_result_keyboard())
    return ConversationHandler.END


async def question_topic_what_to_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
        ],
        [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
    ])
    await query.edit_message_text(QUESTION_WHAT_TO_TAKE_TEXT, reply_markup=keyboard)
    return ConversationHandler.END


# --- «Задать свой вопрос»: показать приглашение, затем принять сообщение и переслать админу ---
ASK_QUESTION = 0


def _ask_question_prompt_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


async def ask_question_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    await query.edit_message_text(
        QUESTION_CUSTOM_PROMPT,
        reply_markup=_ask_question_prompt_keyboard(),
    )
    return ASK_QUESTION


async def ask_question_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return ConversationHandler.END
    text = update.message.text.strip()
    user = update.effective_user
    if config.ADMIN_CHAT_ID:
        try:
            name_part = (user.first_name or "").strip()
            if user.last_name:
                name_part = (name_part + " " + (user.last_name or "").strip()).strip()
            if not name_part and user.username:
                name_part = f"@{user.username}"
            if not name_part:
                name_part = "—"
            username = f"@{user.username}" if user.username else "—"
            safe_name = escape(name_part)
            safe_username = escape(username)
            safe_text = escape(text)
            msg = (
                "📩 <b>Вопрос от пользователя:</b>\n"
                f"Имя: {safe_name}\n"
                f"Username: {safe_username}\n"
                f"chat_id: {user.id}\n\n"
                f"Текст: {safe_text}"
            )
            await context.bot.send_message(
                chat_id=config.ADMIN_CHAT_ID,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Не удалось переслать вопрос админу: %s", e)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])
    await update.message.reply_text(
        "Спасибо, ваш вопрос передан. Мы ответим в ближайшее время.",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


# --- Обработка текста: триггеры и свободный вопрос ---
TRIGGERS = {
    "register": (r"(?i)(записаться|хочу\s+на\s+тренировку|записать|запиши)", "menu:register"),
    "price": (r"(?i)(цена|сколько\s+стоит|стоимость)", "menu:price"),
    "address": (r"(?i)(адрес|где\s+находится|как\s+добраться)", "menu:address"),
    "locations": (r"(?i)(локаци[ия]|локации|адреса)", "menu:locations"),
    "form": (r"(?i)(форма|что\s+надеть|экипировка|кроссовки)", "menu:form"),
    "schedule": (r"(?i)(расписание|когда\s+тренировки)", "menu:schedule"),
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовое сообщение: проверка триггеров или общий ответ."""
    if not update.message or not update.message.text:
        return ConversationHandler.END
    text = update.message.text.strip()
    for name, (pattern, callback_data) in TRIGGERS.items():
        if re.search(pattern, text):
            if callback_data == "menu:register":
                # Обрабатывается ConversationHandler (entry_point по тексту)
                return ConversationHandler.END
            if callback_data == "menu:price":
                t, k = get_price_text_and_keyboard()
                await update.message.reply_text(t, reply_markup=k)
                return ConversationHandler.END
            if callback_data == "menu:address":
                await _reply_address(update, is_callback=False)
                return ConversationHandler.END
            if callback_data == "menu:locations":
                await _reply_locations(update, is_callback=False)
                return ConversationHandler.END
            if callback_data == "menu:form":
                await _reply_form(update, is_callback=False)
                return ConversationHandler.END
            if callback_data == "menu:schedule":
                await _reply_schedule(update, is_callback=False)
                return ConversationHandler.END

    # Сообщение не подошло ни под один сценарий — анти-тупик
    reply = "Похоже, я не понял. Давайте продолжим через меню 👇"
    await update.message.reply_text(
        reply,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Записаться", callback_data="menu:register"), InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main")],
            [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
        ]),
    )
    return ConversationHandler.END


async def _reply_address(update: Update, is_callback: bool):
    """Отправить текст и кнопки сценария «Адрес» (callback или message). Без parse_mode."""
    try:
        if config.ADDRESS:
            text = "Адрес\n\n" + str(config.ADDRESS)
            if getattr(config, "MAP_LINK", None):
                text += "\n\nКарта: " + str(config.MAP_LINK)
            text += "\n\nНа машине или пешком/транспорт? 👇"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("На машине", callback_data="addr:car")],
                [InlineKeyboardButton("Пешком/транспорт", callback_data="addr:walk")],
                [
                    InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
                    InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
                ],
            ])
        else:
            text = (
                "Адрес\n\n"
                "• Пока не указан\n"
                "• Напишите город/район — подскажу контакт админа или скину гео\n\n"
                "Нажмите кнопку ниже 👇"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Записаться", callback_data="menu:register")],
                [
                    InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
                    InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
                ],
            ])
    except Exception as e:
        logger.exception("Ошибка при формировании адреса: %s", e)
        text = "Адрес\n\nНе удалось загрузить данные. Напишите в чат — подскажу 👇"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main")],
            [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
        ])
    if is_callback:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def _reply_form(update: Update, is_callback: bool):
    """Отправить текст и три кнопки: Зал, Манеж, Улица."""
    text = "Что надеть\n\nВыберите тип тренировки 👇"
    keyboard = _form_place_keyboard()
    if is_callback:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


def _build_schedule_text():
    """Собирает текст расписания (без parse_mode). При ошибке — заглушка + лог."""
    try:
        return SCHEDULE_FULL + "\n\nЗаписаться на удобный день? 👇"
    except Exception as e:
        logger.exception("Ошибка при формировании расписания: %s", e)
        return (
            "Расписание\n\n"
            "• Данные временно недоступны\n"
            "• Напишите в чат — подскажу дни и время\n\n"
            "Нажмите кнопку ниже 👇"
        )


async def _reply_schedule(update: Update, is_callback: bool):
    """Отправить текст и кнопки сценария «Расписание»."""
    try:
        text = _build_schedule_text()
    except Exception as e:
        logger.exception("Ошибка в _reply_schedule: %s", e)
        text = "Расписание\n\nНе удалось загрузить данные. Попробуйте позже или напишите в чат 👇"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("📍 Адрес", callback_data="menu:locations"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])
    if is_callback:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


def _locations_choice_keyboard():
    """Клавиатура выбора типа: Беговые / Силовые / Длительная."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏃‍♂️ Беговые тренировки", callback_data="loc:run")],
        [InlineKeyboardButton("🏋️‍♂️ Силовые тренировки", callback_data="loc:gym")],
        [InlineKeyboardButton("🏃‍♂️ Длительная (Раубичи)", callback_data="loc:long")],
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
        ],
        [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
    ])


def _address_nav_keyboard():
    """Клавиатура после показа адреса: Записаться, Адрес, Назад в меню, Начать заново."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("📍 Адрес", callback_data="menu:locations"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


def _address_keyboard_with_geo(geo_url: str):
    """Клавиатура после показа адреса: инлайн-кнопка с URL навигатора + Записаться, Адрес, Назад в меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 Открыть локацию", url=geo_url)],
        [
            InlineKeyboardButton("📝 Записаться", callback_data="menu:register"),
            InlineKeyboardButton("📍 Адрес", callback_data="menu:locations"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart"),
        ],
    ])


async def _reply_locations(update: Update, is_callback: bool):
    """Первый экран «Адрес»: выбор типа тренировки (Беговые / Силовые)."""
    text = (
        "Адрес\n\n"
        "Выберите тип тренировки 👇"
    )
    keyboard = _locations_choice_keyboard()
    if is_callback:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def location_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать адрес по типу (loc:run / loc:gym / loc:long): только текст 📍 Локация + инлайн-кнопка с URL."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("reg", None)
    try:
        loc_type = "run" if query.data == "loc:run" else ("long" if query.data == "loc:long" else "gym")
        address = LOCATION_SHORT.get(loc_type, "Калиновского, 111")
        geo_url = _location_geo_url(address)
        text = f"📍 Локация: {address}"
        keyboard = _address_keyboard_with_geo(geo_url)
    except Exception as e:
        logger.exception("Ошибка при показе адреса: %s", e)
        text = "Адрес\n\nНе удалось загрузить данные. Напишите в чат — подскажу 👇"
        keyboard = _address_nav_keyboard()
        await query.edit_message_text(text, reply_markup=keyboard)
        return ConversationHandler.END
    await query.edit_message_text(text, reply_markup=keyboard)
    return ConversationHandler.END


# --- Fallback: неожиданный текст внутри сценария записи (защита от тупика) ---
async def fallback_unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если пользователь отправил текст, когда ожидался выбор по кнопкам — короткий ответ + выход."""
    if update.message:
        await update.message.reply_text(
            "Похоже, я не понял. Давайте продолжим через меню 👇",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main")],
                [InlineKeyboardButton("🔄 Начать заново", callback_data="menu:restart")],
            ]),
        )
    return ConversationHandler.END


# --- Вход в сценарий записи по тексту (записаться / ближайшая / сегодняшняя и т.д.) ---
async def start_register_by_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return ConversationHandler.END
    context.user_data["reg"] = {}
    await update.message.reply_text(
        "Выберите день недели 👇",
        reply_markup=_day_keyboard(),
    )
    return REG_DAY


# --- ConversationHandler для «Задать свой вопрос» (показать приглашение → принять сообщение → переслать админу) ---
def build_ask_question_conv():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_question_start, pattern="^question:custom$"),
        ],
        states={
            ASK_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_question_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(menu_main, pattern="^menu:main$"),
            CallbackQueryHandler(menu_restart, pattern="^menu:restart$"),
            CommandHandler("start", cmd_start),
            CommandHandler("menu", cmd_menu),
            CommandHandler("restart", cmd_restart),
        ],
        name="ask_question",
        persistent=False,
    )


# --- ConversationHandler для записи (день → слот → уровень → контакт → подтверждение) ---
def build_register_conv():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(menu_register, pattern="^menu:register$"),
            CommandHandler("register", cmd_register_entry),
            MessageHandler(filters.Regex(re.compile(TRIGGERS["register"][0], re.I)), start_register_by_text),
        ],
        states={
            REG_DAY: [
                CallbackQueryHandler(reg_choose_day, pattern="^reg:day:(mon|tue|wed|thu|fri|sun)$"),
            ],
            REG_SLOT: [
                CallbackQueryHandler(reg_choose_slot, pattern=r"^reg:slot:[a-z_]+$"),
            ],
            REG_TRAINER: [
                CallbackQueryHandler(reg_choose_trainer, pattern="^reg:trainer:(dasha|maxim)$"),
            ],
            REG_LEVEL: [
                CallbackQueryHandler(reg_choose_level, pattern="^reg:level:"),
            ],
            REG_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_contact),
            ],
            REG_CONFIRM: [
                CallbackQueryHandler(reg_confirm, pattern="^reg:confirm:"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(menu_restart, pattern="^menu:restart$"),
            CallbackQueryHandler(menu_start, pattern="^menu:start$"),
            CallbackQueryHandler(menu_main, pattern="^menu:main$"),
            CallbackQueryHandler(menu_price, pattern="^menu:price$"),
            CallbackQueryHandler(menu_address, pattern="^menu:address$"),
            CallbackQueryHandler(menu_form, pattern="^menu:form$"),
            CallbackQueryHandler(form_place, pattern="^form:(gym|manege|street)$"),
            CallbackQueryHandler(menu_schedule, pattern="^menu:schedule$"),
            CallbackQueryHandler(menu_question, pattern="^menu:question$"),
            CallbackQueryHandler(question_topic_form, pattern="^question:form$"),
            CallbackQueryHandler(question_topic_what_to_take, pattern="^question:what_to_take$"),
            CallbackQueryHandler(question_topic_how, pattern="^question:how$"),
            CallbackQueryHandler(question_how_type, pattern="^how:(run|strength|long)$"),
            CallbackQueryHandler(ask_question_start, pattern="^question:custom$"),
            CallbackQueryHandler(menu_locations, pattern="^menu:locations$"),
            CallbackQueryHandler(price_maksim_dasha, pattern="^price:maksim_dasha$"),
            CallbackQueryHandler(price_vitalik, pattern="^price:vitalik$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_unexpected_text),
            CommandHandler("start", cmd_start),
            CommandHandler("menu", cmd_menu),
            CommandHandler("restart", cmd_restart),
        ],
        name="register",
        persistent=False,
    )


def main():
    if not config.BOT_TOKEN:
        logger.error("Заполните BOT_TOKEN в config.py")
        return
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Команды — регистрируем ПЕРЕД ConversationHandler
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("question", cmd_question))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # Сценарий записи (ConversationHandler; /register — entry_point внутри)
    app.add_handler(build_register_conv())

    # «Задать свой вопрос» (ConversationHandler: приглашение → принять сообщение → переслать админу)
    app.add_handler(build_ask_question_conv())

    # Кнопки меню (обработчики вне ConversationHandler — когда диалог не активен)
    app.add_handler(CallbackQueryHandler(menu_start, pattern="^menu:start$"))
    app.add_handler(CallbackQueryHandler(menu_restart, pattern="^menu:restart$"))
    app.add_handler(CallbackQueryHandler(menu_price, pattern="^menu:price$"))
    app.add_handler(CallbackQueryHandler(menu_address, pattern="^menu:address$"))
    app.add_handler(CallbackQueryHandler(menu_form, pattern="^menu:form$"))
    app.add_handler(CallbackQueryHandler(form_place, pattern="^form:(gym|manege|street)$"))
    app.add_handler(CallbackQueryHandler(menu_schedule, pattern="^menu:schedule$"))
    app.add_handler(CallbackQueryHandler(menu_locations, pattern="^menu:locations$"))
    app.add_handler(CallbackQueryHandler(menu_question, pattern="^menu:question$"))
    app.add_handler(CallbackQueryHandler(question_topic_form, pattern="^question:form$"))
    app.add_handler(CallbackQueryHandler(question_topic_what_to_take, pattern="^question:what_to_take$"))
    app.add_handler(CallbackQueryHandler(question_topic_how, pattern="^question:how$"))
    app.add_handler(CallbackQueryHandler(question_how_type, pattern="^how:(run|strength|long)$"))
    app.add_handler(CallbackQueryHandler(menu_main, pattern="^menu:main$"))
    app.add_handler(CallbackQueryHandler(price_maksim_dasha, pattern="^price:maksim_dasha$"))
    app.add_handler(CallbackQueryHandler(price_vitalik, pattern="^price:vitalik$"))

    # Адрес: машина/пешком
    app.add_handler(CallbackQueryHandler(address_transport, pattern="^addr:(car|walk)$"))

    # Локации: показать адрес по типу (Беговые / Силовые / Длительная)
    app.add_handler(CallbackQueryHandler(location_show, pattern="^loc:(run|gym|long)$"))

    # Форма: погода
    app.add_handler(CallbackQueryHandler(form_weather, pattern="^form:weather:"))

    # Текст (триггеры и свободный вопрос)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Пересылка всех входящих текстовых сообщений админу (низкий приоритет, после остальных)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, notify_admin), group=99)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
