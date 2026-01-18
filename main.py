"""
Smart SPB Bot - Media Content Manager
Author: Dmitry Kolomenkin
Date: 2026-01-18
Description: Telegram bot for organizing media content with tagging and gallery features.
"""

import sqlite3
import re
import threading
from datetime import datetime, timedelta

import telebot
from telebot import types

# --- Configuration & Constants ---

BOT_TOKEN = '8253588312:AAFOlUSuSskO9DHiVcQekawhF1bwrcornHg'
DB_NAME = 'smart_spb.db'

try:
    import pymorphy3
    morph = pymorphy3.MorphAnalyzer()
except ImportError:
    print("Warning: pymorphy3 not installed. AI tagging will be disabled.")
    morph = None

bot = telebot.TeleBot(BOT_TOKEN)

# --- Global States ---

# Buffers for handling media groups (albums) and user states
album_buffer = {}
upload_states = {}
edit_media_mode = {}


# --- Database Manager ---

class DBManager:
    """Context manager for safe database connections."""
    def __init__(self, db_name=DB_NAME):
        self.db_name = db_name

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            print(f"Database Error: {exc_val}")
        self.conn.commit()
        self.conn.close()


def init_database():
    """Initializes the database schema."""
    with DBManager() as cursor:
        cursor.execute("PRAGMA foreign_keys = ON")
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                description TEXT,
                timestamp TEXT,
                iso_date TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER,
                file_id TEXT,
                file_type TEXT,
                FOREIGN KEY(content_id) REFERENCES content(id) ON DELETE CASCADE
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                tag_name TEXT UNIQUE, 
                tag_type TEXT DEFAULT 'user'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content_tags (
                content_id INTEGER, 
                tag_id INTEGER,
                FOREIGN KEY(content_id) REFERENCES content(id) ON DELETE CASCADE
            )
        ''')


# --- Core Logic: Tagging & processing ---

def extract_tags(text):
    """Extracts explicit tags (#tag) and AI-generated tags (nouns) from text."""
    if not text:
        return set(), set()
    
    user_tags = set(re.findall(r'#(\w+)', text.lower()))
    
    ai_tags = set()
    if morph:
        # Remove punctuation and split
        clean_text = re.sub(r'[^\w\s]', ' ', text).split()
        for word in clean_text:
            word = word.lower()
            if len(word) < 3: 
                continue
            
            parsed = morph.parse(word)[0]
            if 'NOUN' in parsed.tag or word.isdigit():
                normal_form = parsed.normal_form
                if normal_form not in user_tags:
                    ai_tags.add(normal_form)
                    
    return user_tags, ai_tags


def save_content_entry(user_id, files, description):
    """Saves a new content entry with media and tags."""
    now = datetime.now()
    timestamp_str = now.strftime("%d.%m.%Y %H:%M")
    iso_date = now.strftime("%Y-%m-%d %H:%M:%S")

    with DBManager() as cursor:
        cursor.execute(
            "INSERT INTO content (user_id, description, timestamp, iso_date) VALUES (?,?,?,?)",
            (user_id, description, timestamp_str, iso_date)
        )
        content_id = cursor.lastrowid

        for f in files:
            cursor.execute(
                "INSERT INTO media (content_id, file_id, file_type) VALUES (?,?,?)",
                (content_id, f['id'], f['type'])
            )
        
        # Update Tags
        _update_tags_transaction(cursor, content_id, description)

        # Get the sequential number for the user
        cursor.execute("SELECT COUNT(*) FROM content WHERE user_id = ?", (user_id,))
        order_num = cursor.fetchone()[0]

    return order_num


def _update_tags_transaction(cursor, content_id, description):
    """Helper function to update tags within an existing transaction."""
    cursor.execute("DELETE FROM content_tags WHERE content_id = ?", (content_id,))
    if not description:
        return

    user_tags, ai_tags = extract_tags(description)
    
    for tag_set, tag_type in [(user_tags, 'user'), (ai_tags, 'ai')]:
        for tag in tag_set:
            tag_name = f"#{tag}" if tag_type == 'user' else tag
            
            cursor.execute(
                "INSERT OR IGNORE INTO tags (tag_name, tag_type) VALUES (?, ?)", 
                (tag_name, tag_type)
            )
            
            cursor.execute("SELECT id FROM tags WHERE tag_name = ?", (tag_name,))
            res = cursor.fetchone()
            if res:
                cursor.execute(
                    "INSERT INTO content_tags (content_id, tag_id) VALUES (?, ?)", 
                    (content_id, res[0])
                )


def update_content_description(content_id, new_description):
    with DBManager() as cursor:
        cursor.execute("UPDATE content SET description = ? WHERE id = ?", (new_description, content_id))
        _update_tags_transaction(cursor, content_id, new_description)


def delete_content(content_id):
    with DBManager() as cursor:
        cursor.execute("DELETE FROM content WHERE id = ?", (content_id,))


# --- Keyboards & UI ---

def kb_main():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📤 Загрузить", "🖼 Галерея")
    markup.add("📂 Все ваши загрузки", "🏷 Теги")
    markup.add("🔍 Поиск")
    return markup


def kb_cancel():
    return types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 В главное меню")


# --- Bot Handlers: General ---

@bot.message_handler(commands=['start', 'help'])
def handler_start(message):
    init_database()
    welcome_text = (
        "🤖 Бот для формирования медиаконтента готов к работе.\n\n"
        "Команды:\n/upload - Загрузить контент\n/cancel - Отменить действие"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=kb_main())


@bot.message_handler(commands=['cancel'])
@bot.message_handler(func=lambda m: m.text == "🏠 В главное меню")
def handler_cancel(message):
    upload_states.pop(message.from_user.id, None)
    edit_media_mode.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "❌ Действие отменено.", reply_markup=kb_main())


# --- Bot Handlers: Upload Flow ---

@bot.message_handler(func=lambda m: m.text in ["📤 Загрузить", "/upload"])
def handler_upload_start(message):
    upload_states.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "Шаг 1: Отправьте фото или видео.", reply_markup=kb_cancel())


@bot.message_handler(content_types=['audio', 'document', 'voice', 'sticker', 'contact', 'location'])
def handler_invalid_content(message):
    if message.chat.type == 'private':
        bot.send_message(
            message.chat.id, 
            "⚠️ Ошибка: Бот принимает только фото или видео.", 
            reply_markup=kb_main()
        )


@bot.message_handler(content_types=['photo', 'video'])
def handler_media_upload(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Handle Edit Mode
    if user_id in edit_media_mode:
        process_edit_mode_upload(message, user_id, chat_id)
        return

    # Handle New Upload
    file_id = message.photo[-1].file_id if message.content_type == 'photo' else message.video.file_id
    file_info = {'id': file_id, 'type': message.content_type, 'caption': message.caption}

    if message.media_group_id:
        process_album_upload(message, user_id, chat_id, file_info)
    else:
        if message.caption:
            # Immediate save if single file with caption
            num = save_content_entry(user_id, [file_info], message.caption)
            bot.send_message(chat_id, f"✅ Сохранено под номером: {num}", reply_markup=kb_main())
        else:
            # Wait for caption
            upload_states[user_id] = [file_info]
            bot.send_message(chat_id, "Шаг 2: Введите описание к контенту:", reply_markup=kb_cancel())
            bot.register_next_step_handler(message, step_upload_finalize)


def process_album_upload(message, user_id, chat_id, file_info):
    mg_id = message.media_group_id
    if mg_id not in album_buffer:
        album_buffer[mg_id] = []
        # Wait a bit for other files in the album to arrive
        threading.Timer(
            0.8, 
            finish_album_processing, 
            args=[chat_id, user_id, mg_id]
        ).start()
    
    album_buffer[mg_id].append(file_info)


def finish_album_processing(chat_id, user_id, mg_id):
    if mg_id not in album_buffer:
        return
    
    files = album_buffer.pop(mg_id)
    caption = next((f['caption'] for f in files if f['caption']), None)

    if caption:
        num = save_content_entry(user_id, files, caption)
        bot.send_message(chat_id, f"✅ Альбом сохранен под номером: {num}", reply_markup=kb_main())
    else:
        upload_states[user_id] = files
        bot.send_message(chat_id, "📥 Альбом получен. Теперь введите описание:", reply_markup=kb_cancel())
        bot.register_next_step_handler_by_chat_id(chat_id, step_upload_finalize)


def step_upload_finalize(message):
    if message.text in ["🏠 В главное меню", "/cancel"]:
        return handler_cancel(message)
    
    if not message.text:
        bot.send_message(message.chat.id, "Пожалуйста, отправьте текстовое описание.")
        return bot.register_next_step_handler(message, step_upload_finalize)
    
    files = upload_states.pop(message.from_user.id, None)
    if files:
        num = save_content_entry(message.from_user.id, files, message.text)
        bot.send_message(message.chat.id, f"✅ Успешно сохранено под номером: {num}", reply_markup=kb_main())


# --- Bot Handlers: Gallery & Search ---

@bot.message_handler(func=lambda m: m.text == "🖼 Галерея")
def cmd_gallery(message):
    render_gallery(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == "📂 Все ваши загрузки")
def cmd_list_all(message):
    render_list(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == "🏷 Теги")
def cmd_tags(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👤 Мои теги", callback_data="choose_user"),
               types.InlineKeyboardButton("🤖 AI теги", callback_data="choose_ai"))
    bot.send_message(message.chat.id, "Выберите категорию:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "🔍 Поиск")
def cmd_search(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📅 За N дней", "📅 Диапазон", "🆔 По ID", "🏠 В главное меню")
    bot.send_message(message.chat.id, "Выберите способ поиска:", reply_markup=markup)


# --- Search Implementations ---

@bot.message_handler(func=lambda m: m.text == "📅 За N дней")
def search_days(message):
    msg = bot.send_message(message.chat.id, "Введите число дней:", reply_markup=kb_cancel())
    bot.register_next_step_handler(msg, process_search_days)

def process_search_days(message):
    if message.text == "🏠 В главное меню": return handler_cancel(message)
    if not message.text.isdigit():
        return bot.send_message(message.chat.id, "Введите число.")
    
    limit = (datetime.now() - timedelta(days=int(message.text))).strftime("%Y-%m-%d")
    today = datetime.now().strftime('%Y-%m-%d')
    render_list(message.chat.id, message.from_user.id, mode="range", search_val=f"{limit}|{today}")


@bot.message_handler(func=lambda m: m.text == "📅 Диапазон")
def search_range(message):
    msg = bot.send_message(message.chat.id, "Формат: 01.01.2024-10.01.2024", reply_markup=kb_cancel())
    bot.register_next_step_handler(msg, process_search_range)

def process_search_range(message):
    if message.text == "🏠 В главное меню": return handler_cancel(message)
    try:
        d1, d2 = message.text.split("-")
        date1 = datetime.strptime(d1.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
        date2 = datetime.strptime(d2.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
        render_list(message.chat.id, message.from_user.id, mode="range", search_val=f"{date1}|{date2}")
    except ValueError:
        bot.send_message(message.chat.id, "Ошибка формата даты.")


@bot.message_handler(func=lambda m: m.text == "🆔 По ID")
def search_id(message):
    msg = bot.send_message(message.chat.id, "Введите номер поста:", reply_markup=kb_cancel())
    bot.register_next_step_handler(msg, process_search_id)

def process_search_id(message):
    if message.text == "🏠 В главное меню": return handler_cancel(message)
    if not message.text.isdigit():
        return bot.send_message(message.chat.id, "Введите число.")
    render_gallery(message.chat.id, message.from_user.id, post_num=int(message.text))


# --- Rendering Functions (Gallery & List) ---

def render_gallery(chat_id, user_id, post_num=None, photo_index=0, call=None):
    with DBManager() as cursor:
        cursor.execute("SELECT id, description, timestamp FROM content WHERE user_id = ? ORDER BY id ASC", (user_id,))
        posts = cursor.fetchall()
        
        if not posts:
            bot.send_message(chat_id, "База данных пуста.", reply_markup=kb_main())
            return

        total_posts = len(posts)
        post_num = int(post_num) if post_num is not None else total_posts
        post_num = max(1, min(post_num, total_posts))
        
        db_id, desc, ts = posts[post_num - 1]

        cursor.execute("SELECT file_id, file_type FROM media WHERE content_id = ? ORDER BY id ASC", (db_id,))
        media_list = cursor.fetchall()

    total_photos = len(media_list)
    photo_index = max(0, min(photo_index, total_photos - 1))
    
    file_id, file_type = (None, None)
    if total_photos > 0:
        file_id, file_type = media_list[photo_index]

    caption = f"<b>📦 Запись №{post_num}</b> (ID: {db_id})\n⏰ {ts}\n\n{desc or '...'}"
    
    markup = types.InlineKeyboardMarkup()
    
    # Navigation buttons (Previous/Next Photo in album)
    if total_photos > 1:
        btn_prev = types.InlineKeyboardButton("⏪", callback_data=f"gal_{post_num}_{photo_index - 1}") if photo_index > 0 else types.InlineKeyboardButton("⛔️", callback_data="none")
        btn_count = types.InlineKeyboardButton(f"{photo_index + 1}/{total_photos}", callback_data="none")
        btn_next = types.InlineKeyboardButton("⏩", callback_data=f"gal_{post_num}_{photo_index + 1}") if photo_index < total_photos - 1 else types.InlineKeyboardButton("⛔️", callback_data="none")
        markup.row(btn_prev, btn_count, btn_next)

    # Edit/Delete buttons
    markup.row(
        types.InlineKeyboardButton("🗑 Удалить", callback_data=f"confdel_{db_id}_{post_num}"),
        types.InlineKeyboardButton("📝 Редактировать", callback_data=f"preedit_{db_id}_{post_num}")
    )

    # Global Navigation (Next/Prev Post)
    btn_post_next = types.InlineKeyboardButton("След. ➡️", callback_data=f"gal_{post_num + 1}_0") if post_num < total_posts else types.InlineKeyboardButton("⛔️", callback_data="none")
    btn_post_prev = types.InlineKeyboardButton("⬅️ Пред.", callback_data=f"gal_{post_num - 1}_0") if post_num > 1 else types.InlineKeyboardButton("⛔️", callback_data="none")
    
    markup.row(btn_post_next, btn_post_prev)
    markup.add(types.InlineKeyboardButton("🏠 МЕНЮ", callback_data="to_main"))

    # Send or Edit message
    try:
        if call:
            media = types.InputMediaPhoto(file_id, caption=caption, parse_mode="HTML") if file_type == 'photo' else types.InputMediaVideo(file_id, caption=caption, parse_mode="HTML")
            bot.edit_message_media(media, chat_id, call.message.message_id, reply_markup=markup)
        else:
            if file_type == 'photo':
                bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_video(chat_id, file_id, caption=caption, reply_markup=markup, parse_mode="HTML")
    except Exception:
        # Fallback if editing fails (e.g., media type change)
        if call:
            bot.delete_message(chat_id, call.message.message_id)
        if file_type == 'photo':
            bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup, parse_mode="HTML")
        elif file_type == 'video':
            bot.send_video(chat_id, file_id, caption=caption, reply_markup=markup, parse_mode="HTML")


def render_list(chat_id, target_user_id, page=1, mode="all", search_val="", call=None):
    per_page = 10
    offset = (page - 1) * per_page
    
    condition = "user_id = ?"
    params = [target_user_id]
    
    if mode == "tag":
        condition += " AND id IN (SELECT content_id FROM content_tags WHERE tag_id IN (SELECT id FROM tags WHERE tag_name = ? OR tag_name = ?))"
        params.extend([search_val, f"#{search_val}"])
    elif mode == "range":
        d1, d2 = search_val.split("|")
        condition += " AND iso_date BETWEEN ? AND ?"
        params.extend([f"{d1} 00:00:00", f"{d2} 23:59:59"])

    with DBManager() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM content WHERE {condition}", params)
        total = cursor.fetchone()[0]
        
        cursor.execute(
            f"SELECT id, description, timestamp FROM content WHERE {condition} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, per_page, offset)
        )
        records = cursor.fetchall()

    if not records:
        bot.send_message(chat_id, "Ничего не найдено.")
        return

    total_pages = (total + per_page - 1) // per_page
    text_lines = [f"<b>📂 Найдено записей: {total} (Стр. {page}/{total_pages})</b>\n"]
    
    markup = types.InlineKeyboardMarkup(row_width=5)
    btns = []

    with DBManager() as cursor:
        for r in records:
            cursor.execute("SELECT COUNT(*) FROM content WHERE user_id = ? AND id <= ?", (target_user_id, r[0]))
            real_num = cursor.fetchone()[0]
            clean_desc = r[1][:25].replace('\n', ' ') if r[1] else '...'
            text_lines.append(f"<b>{real_num}.</b> {r[2]} | {clean_desc}")
            btns.append(types.InlineKeyboardButton(text=str(real_num), callback_data=f"gal_{real_num}_0"))

    markup.add(*btns)
    
    # Pagination
    nav_row = []
    if page > 1:
        nav_row.append(types.InlineKeyboardButton("⬅️", callback_data=f"pg_{mode}_{page - 1}_{search_val}"))
    if offset + per_page < total:
        nav_row.append(types.InlineKeyboardButton("➡️", callback_data=f"pg_{mode}_{page + 1}_{search_val}"))
    if nav_row:
        markup.row(*nav_row)
        
    markup.add(types.InlineKeyboardButton("🏠 МЕНЮ", callback_data="to_main"))

    full_text = "\n".join(text_lines)
    
    if call:
        bot.edit_message_text(full_text, chat_id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(chat_id, full_text, reply_markup=markup, parse_mode="HTML")


# --- Callback Query Handler (The Router) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if call.data == "none":
        return bot.answer_callback_query(call.id)
    
    if call.data == "to_main":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=kb_main())

    parts = call.data.split("_")
    action = parts[0]

    # Gallery Navigation
    if action == "gal":
        bot.answer_callback_query(call.id)
        render_gallery(call.message.chat.id, call.from_user.id, parts[1], int(parts[2]), call=call)

    # Edit Menu
    elif action == "preedit":
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📝 Текст", callback_data=f"edesc_{parts[1]}_{parts[2]}"),
            types.InlineKeyboardButton("🖼 Медиа", callback_data=f"emedia_{parts[1]}_{parts[2]}")
        )
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_del_{parts[2]}"))
        bot.edit_message_caption("Что редактируем?", call.message.chat.id, call.message.message_id, reply_markup=markup)

    # Edit Media Trigger
    elif action == "emedia":
        edit_media_mode[call.from_user.id] = (parts[1], parts[2])
        bot.send_message(call.message.chat.id, "Пришлите новые файлы:", reply_markup=kb_cancel())

    # Edit Description Trigger
    elif action == "edesc":
        msg = bot.send_message(call.message.chat.id, "Введите новое описание:", reply_markup=kb_cancel())
        bot.register_next_step_handler(msg, lambda m: finalize_edit_desc(m, parts[1], parts[2]))

    # Delete Confirmation
    elif action == "confdel":
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🗑 Подтвердить", callback_data=f"realdel_{parts[1]}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_del_{parts[2]}")
        )
        bot.edit_message_caption("Удалить запись навсегда?", call.message.chat.id, call.message.message_id, reply_markup=markup)

    # Perform Deletion
    elif action == "realdel":
        delete_content(parts[1])
        bot.answer_callback_query(call.id, "Запись удалена")
        render_gallery(call.message.chat.id, call.from_user.id)

    # Cancel Action
    elif action == "cancel":
        render_gallery(call.message.chat.id, call.from_user.id, parts[2], 0, call=call)

    # Pagination
    elif action == "pg":
        # Reconstruct search value in case it contained underscores
        search_val = "_".join(parts[3:]) if len(parts) > 3 else ""
        render_list(call.message.chat.id, call.from_user.id, int(parts[2]), parts[1], search_val, call=call)

    # View Specific Tag
    elif action == "tagview":
        search_val = call.data[8:] # Slice string to handle tags with underscores
        render_list(call.message.chat.id, call.from_user.id, mode="tag", search_val=search_val)

    # Choose Tag Category
    elif action == "choose":
        tag_type = parts[1]
        with DBManager() as cursor:
            cursor.execute('''
                SELECT DISTINCT t.tag_name 
                FROM tags t 
                JOIN content_tags ct ON t.id = ct.tag_id 
                JOIN content c ON ct.content_id = c.id 
                WHERE c.user_id = ? AND t.tag_type = ?
            ''', (call.from_user.id, tag_type))
            rows = cursor.fetchall()
            
        markup = types.InlineKeyboardMarkup(row_width=2)
        for r in rows:
            clean = r[0][1:] if r[0].startswith("#") else r[0]
            markup.add(types.InlineKeyboardButton(text=r[0], callback_data=f"tagview_{clean}"))
        bot.edit_message_text("Выберите тег:", call.message.chat.id, call.message.message_id, reply_markup=markup)


# --- Helper Functions for Edits ---

def finalize_edit_desc(message, content_id, post_num):
    if message.text in ["🏠 В главное меню", "/cancel"]:
        return handler_cancel(message)
    
    update_content_description(content_id, message.text)
    bot.send_message(message.chat.id, "✅ Описание обновлено")
    render_gallery(message.chat.id, message.from_user.id, int(post_num), 0)


def process_edit_mode_upload(message, user_id, chat_id):
    db_id, post_num = edit_media_mode[user_id]
    
    if message.media_group_id:
        if message.media_group_id not in album_buffer:
            album_buffer[message.media_group_id] = []
            threading.Timer(
                0.8, 
                process_edit_album_finish, 
                args=[chat_id, user_id, message.media_group_id, db_id, post_num]
            ).start()
        
        file_id = message.photo[-1].file_id if message.content_type == 'photo' else message.video.file_id
        album_buffer[message.media_group_id].append({'id': file_id, 'type': message.content_type})
    else:
        file_id = message.photo[-1].file_id if message.content_type == 'photo' else message.video.file_id
        finalize_media_edit(user_id, db_id, [{'id': file_id, 'type': message.content_type}], chat_id, post_num)


def process_edit_album_finish(chat_id, user_id, mg_id, db_id, post_num):
    if mg_id not in album_buffer: return
    files = album_buffer.pop(mg_id)
    finalize_media_edit(user_id, db_id, files, chat_id, post_num)


def finalize_media_edit(user_id, db_id, files, chat_id, post_num):
    with DBManager() as cursor:
        cursor.execute("DELETE FROM media WHERE content_id = ?", (db_id,))
        for f in files:
            cursor.execute(
                "INSERT INTO media (content_id, file_id, file_type) VALUES (?,?,?)",
                (db_id, f['id'], f['type'])
            )
            
    edit_media_mode.pop(user_id, None)
    bot.send_message(chat_id, "✅ Медиа обновлено!")
    render_gallery(chat_id, user_id, post_num=int(post_num))


# --- Main Entry Point ---

if __name__ == '__main__':
    print("Starting Smart SPB Bot...")
    init_database()
    print("Database initialized.")
    bot.infinity_polling()
