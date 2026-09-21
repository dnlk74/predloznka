# -*- coding: utf-8 -*-

import telebot
import time
import json
import os
import threading
from telebot import types
from pathlib import Path
from dotenv import load_dotenv
from telebot.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=True, encoding='utf-8-sig')

# ==================== НАСТРОЙКИ ====================
API_TOKEN = os.getenv('API_TOKEN')
EDITOR_IDS = []
EDITOR_NAMES = {}

_editors_raw = os.getenv('EDITORS', '')
for pair in _editors_raw.split(','):
    pair = pair.strip()
    if not pair:
        continue
    if ':' in pair:
        id_str, name = pair.split(':', 1)
        eid = int(id_str.strip())
        EDITOR_IDS.append(eid)
        EDITOR_NAMES[eid] = name.strip()
    else:
        # если имя не указано — только ID
        eid = int(pair)
        EDITOR_IDS.append(eid)
        EDITOR_NAMES[eid] = "???"

CHANNEL_URL = os.getenv('CHANNEL_URL')
CHANNEL_NAME = os.getenv('CHANNEL_NAME')

COOLDOWN_SECONDS = 30
BANS_PER_PAGE = 10
BLACKLIST_FILE = 'blacklist.json'

# ==================== АНТИСПАМ ====================
MESSAGES_WARN = 10            # предупреждение на 10-м сообщении
MESSAGES_BAN = 15             # автобан на 20-м сообщении
MESSAGES_WINDOW = 3600        # окно в секундах (1 час)
# ==================== ИНИЦИАЛИЗАЦИЯ ====================
EDITOR_IDS_SET = set(EDITOR_IDS)

bot = telebot.TeleBot(API_TOKEN)
last_submission_time = {}

# Антиспам-состояние
message_times = {}            # {user_id: [timestamp, ...]}
warned_users = {}             # {user_id: True}

awaiting_ban = {}
pending_ban_requests = {}
media_groups = {}
MEDIA_GROUP_DELAY = 1.0
ban_request_counter = 0


# ==================== РАБОТА С JSON ====================
def load_json(filename):
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[ERROR] Не удалось прочитать {filename}: {e}")
    return {}


def save_json(filename, data):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Не удалось записать {filename}: {e}")


blacklist = load_json(BLACKLIST_FILE)


# ==================== АНТИСПАМ ====================
def check_message_limit(user_id):
    """
    Считает сообщения за последний час.
    Возвращает (status, count):
        'ok'   — всё хорошо
        'warn' — предупреждение (10-е сообщение)
        'ban'  — автобан (20-е сообщение)
    """
    now = time.time()

    times = message_times.get(user_id, [])
    times = [t for t in times if now - t < MESSAGES_WINDOW]
    times.append(now)
    message_times[user_id] = times

    count = len(times)

    # Сброс флага предупреждения, если окно очистилось
    if count < MESSAGES_WARN:
        warned_users.pop(user_id, None)

    if count >= MESSAGES_BAN:
        return 'ban', count

    if count >= MESSAGES_WARN and not warned_users.get(user_id):
        warned_users[user_id] = True
        return 'warn', count

    return 'ok', count


def cleanup_message_times():
    """Чистит устаревшие записи, чтобы память не росла."""
    now = time.time()
    for uid in list(message_times.keys()):
        times = [t for t in message_times[uid] if now - t < MESSAGES_WINDOW]
        if times:
            message_times[uid] = times
        else:
            del message_times[uid]
            warned_users.pop(uid, None)


def periodic_cleanup():
    """Запускает чистку каждые 10 минут."""
    cleanup_message_times()
    threading.Timer(600, periodic_cleanup).start()


def auto_ban(user_id, username, count):
    """Автобан + уведомление редакторов."""
    username_str = ("@" + username) if username else "—"

    blacklist[str(user_id)] = {
        "username": username_str,
        "reason": f"Автобан: {count} сообщений за час",
        "banned_at": time.strftime("%d.%m.%Y %H:%M"),
        "banned_by": "system"
    }
    save_json(BLACKLIST_FILE, blacklist)

    try:
        bot.send_message(
            user_id,
            f"🚫 <b>Вы автоматически заблокированы.</b>\n\n"
            f"📄 Причина: спам.\n\n",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[INFO] Не удалось уведомить {user_id}: {e}")

    for editor_id in EDITOR_IDS:
        try:
            bot.send_message(
                editor_id,
                f"🤖 <b>Автобан</b>\n\n"
                f"👤 Пользователь: {username_str}\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"📨 Сообщений за час: <b>{count}</b>\n\n"
                f"🔓 Разбанить: /unban {user_id}",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[ERROR] Не удалось уведомить редактора {editor_id}: {e}")

def handle_media_group(message):
    """Собирает все сообщения альбома в буфер и откладывает обработку."""
    group_id = message.media_group_id

    if group_id not in media_groups:
        media_groups[group_id] = {"messages": [], "timer": None}

    media_groups[group_id]["messages"].append(message)

    # Отменяем старый таймер и запускаем новый
    if media_groups[group_id]["timer"]:
        media_groups[group_id]["timer"].cancel()

    timer = threading.Timer(MEDIA_GROUP_DELAY, process_media_group, args=[group_id])
    media_groups[group_id]["timer"] = timer
    timer.start()


def process_media_group(group_id):
    """Обрабатывает весь альбом разом: 1 уведомление редактору + N forwards."""
    data = media_groups.pop(group_id, None)
    if not data:
        return

    messages = data["messages"]
    if not messages:
        return

    first_msg = messages[0]
    user_id = first_msg.from_user.id

    # Забаненные — молчание
    if str(user_id) in blacklist:
        return

    # Редактор (на всякий случай — альбом от редактора не должен обрабатываться)
    if user_id in EDITOR_IDS_SET:
        return

    # АНТИСПАМ: альбом считаем за одно сообщение
    status, count = check_message_limit(user_id)

    if status == 'ban':
        auto_ban(user_id, first_msg.from_user.username or "", count)
        return

    if status == 'warn':
        bot.send_message(
            first_msg.chat.id,
            f"⚠️ <b>Предупреждение</b>\n\n"
            f"Вы отправили {count} сообщений за последний час. "
            f"При повторных нарушениях бот автоматически заблокирует вас.",
            parse_mode='HTML'
        )
        return

    # КУЛДАУН
    now = time.time()
    if user_id in last_submission_time:
        elapsed = now - last_submission_time[user_id]
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed)
            minutes, seconds = divmod(remaining, 60)
            bot.send_message(
                first_msg.chat.id,
                f"⏳ Подождите ещё <b>{minutes} мин. {seconds} сек.</b>",
                parse_mode='HTML'
            )
            return

    # Автор
    username = first_msg.from_user.username
    if username:
        author = f"@{username}"
    else:
        author = f"{first_msg.from_user.first_name} {first_msg.from_user.last_name or ''}".strip()
    author += f" (ID: <code>{user_id}</code>)"

    # ОДНО уведомление редакторам + forwards все фото альбома
    sent_to = 0
    n = len(messages)
    info_text = f"📰 <b>Новая новость</b>\n👤 От: {author}\n📎 <i>Альбом: {n} медиа</i>"

    for editor_id in EDITOR_IDS:
        try:
            token = register_ban_request(user_id, first_msg.from_user.username or "", editor_id)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(
                "🔨 Забанить автора",
                callback_data=f"ban_{token}"
            ))
            bot.send_message(editor_id, info_text, parse_mode='HTML', reply_markup=markup)
            for m in messages:
                bot.forward_message(editor_id, m.chat.id, m.message_id)
            sent_to += 1
        except Exception as e:
            print(f"[ERROR] Не удалось отправить редактору {editor_id}: {e}")

    if sent_to == 0:
        bot.send_message(first_msg.chat.id, "❌ Не удалось доставить новость редакторам. Попробуйте позже.")
        return

    last_submission_time[user_id] = now

    # ОДНО подтверждение пользователю
    bot.send_message(
        first_msg.chat.id,
        f"✅ Новость отправлена редактору. Спасибо!"
    )

# ==================== ТЕКСТЫ ====================
WELCOME_TEXT = (
    f"👋 Привет! Это бот-предложка канала "
    f"<a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>.\n\n"
    "Просто отправьте мне новость — я мгновенно передам её редактору. "
    "В дальнейшем сюда можно писать новости просто как сообщение. "
    "Никуда нажимать не нужно.\n\n"
    "📚 <b>Перед первой отправкой советую прочитать разделы ниже</b> — "
    "это займёт минуту и поможет редактору быстрее опубликовать вашу новость."
)

SECTION_WHAT_TO_WRITE = (
    "📝 <b>Что указать в сообщении</b>\n\n"
    "📄 <b>Описание случившегося</b> — что случилось (подробно)\n"
    "📎 <b>Медиа</b> — фото или видео ситуации\n"
    "📍 <b>Место</b> — где это произошло\n"
    "🕒 <b>Время</b> — когда это произошло\n"
    "👤 <b>Анонимность</b> — по умолчанию новость анонимная.\n"
    "Если хотите, чтобы был указан ваш юзернейм — напишите в тексте "
    "новости <b>НЕ АНОНИМНО</b>. Если не хотите — напишите <b>АНОНИМНО</b> "
    "(или ничего не пишите)."
)

SECTION_EXAMPLE = (
    "✍️ <b>Пример правильной новости</b>\n\n"
    "📄 На дороге прорвало трубу, фонтан воды бьёт на высоту "
    "нескольких метров, проезжая часть затоплена. Движение "
    "перекрыто, на месте работают коммунальщики. <b>АНОНИМНО</b>.\n\n"
    "📷 <i>Фото/видео фонтана</i>\n\n"
    "🕒 14:30, сегодня\n\n"
    "📍 ул. Пушкина, 15, у главного входа в Сбербанк"
)

SECTION_RULES = (
    "❗ <b>Правила</b>\n\n"
    "⏳ Отправлять сообщение боту можно <b>не чаще одного раза в час</b>. "
    "Старайтесь максимально чётко и подробно описать вашу новость, "
    "чтобы не пришлось её редактировать и уточнять какие-то детали.\n\n"
    "⚠️ Отправляйте только <b>достоверную информацию</b>. Она будет проверяться!\n\n"
    "❌ За предоставление <b>недостоверной / шуточной / неактуальной / "
    "оскорбительной / разжигающей ненависть / содержащей личные данные "
    "и призывы к незаконным действиям / содержащей 18+ материалы / "
    "рекламной / чужой (без указания источника) / содержащей покупку "
    "или продажу товаров</b> информации, а также за <b>спам / флуд</b> "
    "и др. пользователь вносится в чёрный список бота "
    "<b>без возможности разблокировки</b>.\n\n"
    "⁉️ Редактор может опубликовать, отредактировать или отклонить "
    "новость <b>без объяснения причин</b>. Не нужно писать боту и "
    "спрашивать, будет ли опубликована новость. Также не стоит редактировать отправленную новость, "
    "редактор получает только изначальный её вариант. Для уточнения "
    "информации редактор/админ может связаться с вами в ЛС."
)

EDITOR_MENU_TEXT = (
    "👋 Вы вошли как <b>редактор</b>.\n\n"
    "🔨 <b>Что можно делать:</b>\n"
    "Кнопка под новостью — забанить автора\n"
    "/blacklist — список всех банов. Кнопки ниже — для быстрого разбана.\n"
    "/unban &lt;ID&gt; — разблокировать пользователя\n"
    "/ban &lt;ID&gt; &lt;причина&gt; — забанить вручную\n"
    "/cancel — отменить начатое действие\n\n"
    "📋 Все команды доступны через меню <b>/</b>."
)


# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard():
    """Inline-меню для подписчика (только в /start)."""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📝 Что написать", callback_data="section_what"),
        types.InlineKeyboardButton("✍️ Пример поста", callback_data="section_example")
    )
    markup.add(types.InlineKeyboardButton("❗ Правила", callback_data="section_rules"))
    return markup


def back_keyboard():
    """Кнопка 'Назад' для разделов инструкции."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="section_back"))
    return markup


def get_editor_name(editor_id):
    if editor_id == "system":
        return "🤖 бот"
    try:
        return EDITOR_NAMES.get(int(editor_id), "???")
    except (ValueError, TypeError):
        return "???"


# ==================== РЕНДЕР СПИСКА БАНОВ ====================
def render_bans_page(page: int):
    """Возвращает (text, markup) для указанной страницы чёрного списка."""
    if not blacklist:
        return "📋 <b>Чёрный список пуст.</b>", None

    items = sorted(
        blacklist.items(),
        key=lambda kv: kv[1].get("banned_at", ""),
        reverse=True
    )

    total = len(items)
    total_pages = (total + BANS_PER_PAGE - 1) // BANS_PER_PAGE
    page = max(0, min(page, total_pages - 1))

    start = page * BANS_PER_PAGE
    chunk = items[start:start + BANS_PER_PAGE]

    lines = [f"📋 <b>Чёрный список</b> ({total} чел.) — стр. {page + 1}/{total_pages}\n"]
    for i, (uid, data) in enumerate(chunk, start=start + 1):
        username = data.get("username", "—")
        reason = data.get("reason", "без причины")
        banned_at = data.get("banned_at", "")
        banned_by = data.get("banned_by", "")
        editor_name = get_editor_name(banned_by)

        entry = (
            f"<b>{i}.</b> 👤 {username}, <code>{uid}</code>\n"
            f"     Причина: {reason}\n"
            f"     <i>{banned_at}</i>"
        )
        if banned_by:
            entry += f"\n     Забанил: {editor_name}"

        lines.append(entry)

    text = "\n\n".join(lines)

    markup = types.InlineKeyboardMarkup()

    # Кнопка разбана для каждого пользователя на странице
    for uid, data in chunk:
        username = data.get("username", "—")
        if username and username != "—":
            btn_text = f"🔓 Разбанить {username}"
        else:
            btn_text = f"🔓 Разбанить {uid}"
        if len(btn_text) > 40:
            btn_text = btn_text[:37] + "..."
        markup.add(types.InlineKeyboardButton(
            btn_text,
            callback_data=f"unban_{uid}_{page}"
        ))

    # Пагинация
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"bans_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"bans_page_{page + 1}"))
    if nav:
        markup.add(*nav)

    return text, markup


# ==================== ТОКЕНЫ ДЛЯ БАНА ====================
def register_ban_request(target_id, username, editor_id):
    global ban_request_counter
    ban_request_counter += 1
    token = str(ban_request_counter)

    pending_ban_requests[token] = {
        "target_id": str(target_id),
        "username": ("@" + username) if username else "—",
        "editor_id": editor_id
    }

    if len(pending_ban_requests) > 1000:
        keys_to_remove = list(pending_ban_requests.keys())[:500]
        for k in keys_to_remove:
            pending_ban_requests.pop(k, None)

    return token


# ==================== ОБЩАЯ ФУНКЦИЯ РАЗБАНА ====================
def perform_unban(target_id: str):
    """
    Разбанивает пользователя. Возвращает (ok: bool, message: str).
    """
    if target_id not in blacklist:
        return False, "не найден в чёрном списке"

    del blacklist[target_id]
    save_json(BLACKLIST_FILE, blacklist)

    try:
        bot.send_message(
            int(target_id),
            "✅ Ваша блокировка снята. Вы снова можете присылать новости."
        )
    except Exception as e:
        print(f"[INFO] Не удалось уведомить {target_id}: {e}")

    return True, "разблокирован"


# ==================== КОМАНДЫ ОБЩИЕ ====================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id

    if str(user_id) in blacklist:
        return

    if user_id in EDITOR_IDS_SET:
        bot.send_message(
            message.chat.id,
            EDITOR_MENU_TEXT,
            parse_mode='HTML'
        )
        return

    bot.send_message(
        message.chat.id,
        WELCOME_TEXT,
        parse_mode='HTML',
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True
    )


# ==================== КОМАНДЫ ПОДПИСЧИКОВ (без кнопок) ====================
@bot.message_handler(commands=['rules'])
def cmd_rules(message):
    if message.from_user.id in EDITOR_IDS_SET:
        return
    if str(message.from_user.id) in blacklist:
        return
    bot.send_message(message.chat.id, SECTION_RULES, parse_mode='HTML')


@bot.message_handler(commands=['example'])
def cmd_example(message):
    if message.from_user.id in EDITOR_IDS_SET:
        return
    if str(message.from_user.id) in blacklist:
        return
    bot.send_message(message.chat.id, SECTION_EXAMPLE, parse_mode='HTML')


@bot.message_handler(commands=['what'])
def cmd_what(message):
    if message.from_user.id in EDITOR_IDS_SET:
        return
    if str(message.from_user.id) in blacklist:
        return
    bot.send_message(message.chat.id, SECTION_WHAT_TO_WRITE, parse_mode='HTML')


# ==================== КОМАНДЫ РЕДАКТОРОВ ====================
@bot.message_handler(commands=['blacklist'])
def cmd_blacklist(message):
    if message.from_user.id not in EDITOR_IDS_SET:
        return
    text, markup = render_bans_page(0)
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.message_handler(commands=['unban'])
def cmd_unban(message):
    if message.from_user.id not in EDITOR_IDS_SET:
        return

    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: <code>/unban &lt;ID&gt;</code>", parse_mode='HTML')
        return

    target_id = parts[1].strip()
    ok, status = perform_unban(target_id)

    if ok:
        bot.reply_to(
            message,
            f"✅ Пользователь <code>{target_id}</code> разблокирован.",
            parse_mode='HTML'
        )
    else:
        bot.reply_to(
            message,
            f"⚠️ Пользователь <code>{target_id}</code> {status}.",
            parse_mode='HTML'
        )


@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    editor_id = message.from_user.id
    if editor_id not in EDITOR_IDS_SET:
        return

    if editor_id in awaiting_ban:
        awaiting_ban.pop(editor_id, None)
        bot.reply_to(message, "✅ Ожидание бана отменено.")
    else:
        bot.reply_to(message, "Нечего отменять.")


@bot.message_handler(commands=['ban'])
def cmd_ban(message):
    if message.from_user.id not in EDITOR_IDS_SET:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(
            message,
            "Использование: <code>/ban &lt;ID&gt; &lt;причина&gt;</code>",
            parse_mode='HTML'
        )
        return

    target_id = parts[1].strip()
    reason = parts[2].strip()

    if not target_id.isdigit():
        bot.reply_to(message, "⚠️ ID должен состоять только из цифр.")
        return

    if target_id in blacklist:
        bot.reply_to(message, "⚠️ Пользователь уже в чёрном списке.")
        return

    blacklist[target_id] = {
        "username": "—",
        "reason": reason,
        "banned_at": time.strftime("%d.%m.%Y %H:%M"),
        "banned_by": message.from_user.id
    }
    save_json(BLACKLIST_FILE, blacklist)

    bot.reply_to(
        message,
        f"✅ Пользователь <code>{target_id}</code> забанен.\nПричина: {reason}",
        parse_mode='HTML'
    )

    try:
        bot.send_message(
            int(target_id),
            f"🚫 <b>Вы заблокированы в боте.</b>\n\n"
            f"📄 Причина: {reason}\n\n",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[INFO] Не удалось уведомить {target_id}: {e}")


# ==================== CALLBACK: РАЗДЕЛЫ ИНСТРУКЦИИ ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('section_'))
def callback_sections(call):
    if call.data == "section_back":
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=WELCOME_TEXT,
                parse_mode='HTML',
                reply_markup=main_menu_keyboard(),
                disable_web_page_preview=True
            )
        except Exception:
            bot.send_message(
                call.message.chat.id,
                WELCOME_TEXT,
                parse_mode='HTML',
                reply_markup=main_menu_keyboard(),
                disable_web_page_preview=True
            )
        bot.answer_callback_query(call.id)
        return

    texts = {
        "section_what": SECTION_WHAT_TO_WRITE,
        "section_example": SECTION_EXAMPLE,
        "section_rules": SECTION_RULES,
    }
    text = texts.get(call.data)
    if text is None:
        bot.answer_callback_query(call.id)
        return

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode='HTML',
            reply_markup=back_keyboard()
        )
    except Exception as e:
        print(f"[INFO] edit skipped: {e}")

    bot.answer_callback_query(call.id)


# ==================== CALLBACK: БАН (нажатие кнопки под новостью) ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('ban_'))
def callback_ban(call):
    editor_id = call.from_user.id

    if editor_id not in EDITOR_IDS_SET:
        bot.answer_callback_query(call.id, "У вас нет прав.")
        return

    token = call.data.split('_', 1)[1]
    data = pending_ban_requests.get(token)

    if data is None:
        bot.answer_callback_query(
            call.id,
            "⚠️ Кнопка устарела. Используйте /ban <ID> <причина>."
        )
        return

    target_id = data["target_id"]
    username = data["username"]

    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception:
        pass

    if target_id in blacklist:
        bot.answer_callback_query(call.id, "Пользователь уже в чёрном списке.")
        return

    awaiting_ban[editor_id] = {
        "target_id": target_id,
        "username": username
    }

    bot.send_message(
        editor_id,
        f"✍️ Напишите причину бана для пользователя <code>{target_id}</code> "
        f"<b>следующим сообщением</b>.\n\n"
        f"Чтобы отменить — введите /cancel.",
        parse_mode='HTML'
    )

    bot.answer_callback_query(call.id, "Ожидаю причину бана...")


# ==================== CALLBACK: РАЗБАН ИЗ СПИСКА ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('unban_'))
def callback_unban(call):
    if call.from_user.id not in EDITOR_IDS_SET:
        bot.answer_callback_query(call.id, "У вас нет прав.")
        return

    parts = call.data.split('_')
    if len(parts) < 2:
        bot.answer_callback_query(call.id, "Ошибка формата.")
        return

    target_id = parts[1]
    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0

    ok, status = perform_unban(target_id)

    if ok:
        bot.answer_callback_query(call.id, "✅ Разблокирован")
    else:
        bot.answer_callback_query(call.id, f"⚠️ {status.capitalize()}")

    text, markup = render_bans_page(page)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception:
        pass


# ==================== CALLBACK: ПАГИНАЦИЯ СПИСКА ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('bans_page_'))
def callback_bans(call):
    if call.from_user.id not in EDITOR_IDS_SET:
        bot.answer_callback_query(call.id, "У вас нет прав.")
        return

    page = int(call.data.split('_')[-1])
    text, markup = render_bans_page(page)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)


# ==================== ОБРАБОТКА НОВОСТЕЙ ====================
@bot.message_handler(content_types=['text', 'photo', 'video', 'document',
                                    'audio', 'voice', 'video_note'])
def handle_news(message):
    user_id = message.from_user.id

    # 1. Забаненные — молчание
    if str(user_id) in blacklist:
        return

    # 2. Редакторы — отдельная логика
    if user_id in EDITOR_IDS_SET:
        handle_editor_message(message)
        return

    # 3. АЛЬБОМ — буферизация и отдельная обработка
    if message.media_group_id:
        handle_media_group(message)
        return

    # 4. АНТИСПАМ (только для одиночных сообщений)
    status, count = check_message_limit(user_id)

    if status == 'ban':
        auto_ban(user_id, message.from_user.username or "", count)
        return

    if status == 'warn':
        bot.reply_to(
            message,
            f"⚠️ <b>Предупреждение</b>\n\n"
            f"Вы отправили {count} сообщений за последний час. "
            f"При продолжении спама бот автоматически забокирует вас.",
            parse_mode='HTML'
        )
        return

    # 5. КУЛДАУН (только для одиночных)
    now = time.time()
    if user_id in last_submission_time:
        elapsed = now - last_submission_time[user_id]
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed)
            minutes, seconds = divmod(remaining, 60)
            bot.reply_to(
                message,
                f"⏳ Подождите ещё <b>{minutes} мин. {seconds} сек.</b>",
                parse_mode='HTML'
            )
            return

    # 6. Отправка одиночного сообщения редакторам
    username = message.from_user.username
    if username:
        author = f"@{username}"
    else:
        author = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    author += f" (ID: <code>{user_id}</code>)"

    info_text = f"📰 <b>Новая новость</b>\n👤 От: {author}"

    sent_to = 0
    for editor_id in EDITOR_IDS:
        try:
            token = register_ban_request(user_id, message.from_user.username or "", editor_id)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(
                "🔨 Забанить автора",
                callback_data=f"ban_{token}"
            ))
            bot.send_message(editor_id, info_text, parse_mode='HTML', reply_markup=markup)
            bot.forward_message(editor_id, message.chat.id, message.message_id)
            sent_to += 1
        except Exception as e:
            print(f"[ERROR] Не удалось отправить редактору {editor_id}: {e}")

    if sent_to == 0:
        bot.reply_to(message, "❌ Не удалось доставить новость редакторам. Попробуйте позже.")
        return

    last_submission_time[user_id] = now
    bot.reply_to(message, "✅ Новость отправлена редактору. Спасибо!")

# ==================== ОБРАБОТКА СООБЩЕНИЙ РЕДАКТОРА ====================
def handle_editor_message(message):
    editor_id = message.from_user.id

    if editor_id in awaiting_ban and message.text:
        if message.text.startswith('/'):
            bot.reply_to(
                message,
                "⚠️ Это похоже на команду. Напишите причину текстом или /cancel для отмены."
            )
            return

        reason = message.text.strip()
        if not reason:
            bot.reply_to(message, "⚠️ Причина не может быть пустой.")
            return

        data = awaiting_ban.pop(editor_id)
        target_id = data["target_id"]
        username = data.get("username", "—")

        blacklist[target_id] = {
            "username": username,
            "reason": reason,
            "banned_at": time.strftime("%d.%m.%Y %H:%M"),
            "banned_by": editor_id
        }
        save_json(BLACKLIST_FILE, blacklist)

        bot.reply_to(
            message,
            f"✅ Пользователь <code>{target_id}</code> забанен.\n"
            f"Причина: {reason}",
            parse_mode='HTML'
        )

        try:
            bot.send_message(
                int(target_id),
                f"🚫 <b>Вы заблокированы в боте.</b>\n\n"
                f"📄 Причина: {reason}\n\n",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[INFO] Не удалось уведомить {target_id}: {e}")
        return


# ==================== МЕНЮ КОМАНД ====================
def set_commands():
    try:
        bot.set_my_commands(
            commands=[
                BotCommand("start", "🏠 Главное меню"),
                BotCommand("rules", "📖 Правила"),
                BotCommand("example", "✍️ Пример новости"),
                BotCommand("what", "📝 Что писать"),
            ],
            scope=BotCommandScopeDefault()
        )
    except Exception as e:
        print(f"[WARN] set_my_commands (default): {e}")

    editor_commands = [
        BotCommand("start", "🏠 Меню редактора"),
        BotCommand("blacklist", "📋 Список банов"),
        BotCommand("unban", "🔓 Разблокировать"),
        BotCommand("ban", "🔨 Забанить вручную"),
        BotCommand("cancel", "❌ Отменить действие"),
    ]
    for editor_id in EDITOR_IDS:
        try:
            bot.set_my_commands(
                commands=editor_commands,
                scope=BotCommandScopeChat(chat_id=editor_id)
            )
        except Exception as e:
            print(f"[WARN] set_my_commands для {editor_id}: {e}")


# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    print(f"Бот запущен. Редакторов: {len(EDITOR_IDS)}")
    set_commands()

    for editor_id in EDITOR_IDS:
        try:
            chat = bot.get_chat(editor_id)
            name = getattr(chat, 'first_name', None) or getattr(chat, 'title', '—')
            print(f"  ✓ Редактор {editor_id} — {name}")
        except Exception as e:
            print(f"  ✗ Редактор {editor_id} недоступен: {e}")
            print(f"     → Убедитесь, что он нажал /start у бота.")

    # Запускаем периодическую чистку антиспам-словаря
    periodic_cleanup()

    bot.infinity_polling()