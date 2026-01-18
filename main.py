import telebot
import sqlite3
import re
import pymorphy3
import threading
from datetime import datetime, timedelta
from telebot import types

# 1. КОНФИГУРАЦИЯ
TOKEN = '8253588312:AAFOlUSuSskO9DHiVcQekawhF1bwrcornHg'
bot = telebot.TeleBot(TOKEN)
morph = pymorphy3.MorphAnalyzer()

# Состояния
album_buffer = {}
upload_states = {}
edit_media_mode = {}


# 2. БД
def init_db():
    conn = sqlite3.connect('smart_spb.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute('''CREATE TABLE IF NOT EXISTS content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        description TEXT,
        timestamp TEXT,
        iso_date TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_id INTEGER,
        file_id TEXT,
        file_type TEXT,
        FOREIGN KEY(content_id) REFERENCES content(id) ON DELETE CASCADE
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tag_name TEXT UNIQUE, tag_type TEXT DEFAULT 'user'
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS content_tags (
        content_id INTEGER, tag_id INTEGER,
        FOREIGN KEY(content_id) REFERENCES content(id) ON DELETE CASCADE
    )''')
    conn.commit()
    conn.close()


# --- ЛОГИКА ТЕГОВ ---
def get_hybrid_tags(text):
    if not text: return set(), set()
    user_tags = set(re.findall(r'#(\w+)', text.lower()))
    clean_text = re.sub(r'[^\w\s]', ' ', text).split()
    ai_tags = set()
    for word in clean_text:
        word = word.lower()
        if len(word) < 3: continue
        parsed = morph.parse(word)[0]
        if 'NOUN' in parsed.tag or word.isdigit():
            normal_form = parsed.normal_form
            if normal_form not in user_tags: ai_tags.add(normal_form)
    return user_tags, ai_tags


def update_tags_in_db(cursor, content_id, description):
    cursor.execute("DELETE FROM content_tags WHERE content_id = ?", (content_id,))
    if not description: return
    u_tags, a_tags = get_hybrid_tags(description)
    for t_list, t_type in [(u_tags, 'user'), (a_tags, 'ai')]:
        for t in t_list:
            t_name = f"#{t}" if t_type == 'user' else t
            cursor.execute("INSERT OR IGNORE INTO tags (tag_name, tag_type) VALUES (?, ?)", (t_name, t_type))
            cursor.execute("SELECT id FROM tags WHERE tag_name = ?", (t_name,))
            res = cursor.fetchone()
            if res: cursor.execute("INSERT INTO content_tags (content_id, tag_id) VALUES (?, ?)", (content_id, res[0]))


# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📤 Загрузить", "🖼 Галерея")
    markup.add("📂 Все ваши загрузки", "🏷 Теги")
    markup.add("🔍 Поиск")
    return markup


def get_back_keyboard():
    return types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 В главное меню")


# --- СОХРАНЕНИЕ ---
def save_full_entry(user_id, files, description):
    conn = sqlite3.connect('smart_spb.db')
    cur = conn.cursor()
    now = datetime.now()
    cur.execute(
        "INSERT INTO content (user_id, description, timestamp, iso_date) VALUES (?,?,?,?)",
        (user_id, description, now.strftime("%d.%m.%Y %H:%M"), now.strftime("%Y-%m-%d %H:%M:%S")))
    content_id = cur.lastrowid
    for f in files:
        cur.execute("INSERT INTO media (content_id, file_id, file_type) VALUES (?,?,?)",
                    (content_id, f['id'], f['type']))
    update_tags_in_db(cur, content_id, description)

    cur.execute("SELECT COUNT(*) FROM content WHERE user_id = ?", (user_id,))
    order_num = cur.fetchone()[0]

    conn.commit()
    conn.close()
    return order_num


# --- КОМАНДЫ ---
@bot.message_handler(commands=['start', 'help'])
def start(m):
    init_db()
    bot.send_message(m.chat.id, "🤖 Бот для формирования медиаконтента готов к работе.\n\n"
                                "Команды:\n/upload - Загрузить контент\n/cancel - Отменить действие",
                     reply_markup=get_main_keyboard())


@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    upload_states.pop(m.from_user.id, None)
    edit_media_mode.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "❌ Действие отменено.", reply_markup=get_main_keyboard())


# --- ЗАГРУЗКА ---
@bot.message_handler(func=lambda m: m.text == "📤 Загрузить" or m.text == "/upload")
def cmd_upload(m):
    upload_states.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "Шаг 1: Отправьте фото или видео.", reply_markup=get_back_keyboard())


# Исправление: обработка текстовой кнопки во время ожидания медиа
@bot.message_handler(func=lambda m: m.text == "🏠 В главное меню", content_types=['text'])
def handle_menu_during_upload(m):
    cmd_cancel(m)


@bot.message_handler(content_types=['audio', 'document', 'voice', 'sticker', 'contact', 'location'])
def invalid_content(m):
    if m.chat.type == 'private':
        bot.send_message(m.chat.id, "⚠️ Ошибка: Бот принимает только фото или видео. Загрузка прервана.",
                         reply_markup=get_main_keyboard())
        upload_states.pop(m.from_user.id, None)


@bot.message_handler(content_types=['photo', 'video'])
def upload_handler(m):
    # Если нажата кнопка меню вместо файла
    if m.content_type == 'text' and m.text == "🏠 В главное меню":
        return cmd_cancel(m)

    if m.from_user.id in edit_media_mode:
        db_id, post_num = edit_media_mode[m.from_user.id]
        if m.media_group_id:
            if m.media_group_id not in album_buffer:
                album_buffer[m.media_group_id] = []
                threading.Timer(0.8, process_edit_album,
                                args=[m.chat.id, m.from_user.id, m.media_group_id, db_id, post_num]).start()
            fid = m.photo[-1].file_id if m.content_type == 'photo' else m.video.file_id
            album_buffer[m.media_group_id].append({'id': fid, 'type': m.content_type})
        else:
            fid = m.photo[-1].file_id if m.content_type == 'photo' else m.video.file_id
            finalize_media_edit(m.from_user.id, db_id, [{'id': fid, 'type': m.content_type}], m.chat.id, post_num)
        return

    if m.media_group_id:
        if m.media_group_id not in album_buffer:
            album_buffer[m.media_group_id] = []
            threading.Timer(0.8, process_album_completion, args=[m.chat.id, m.from_user.id, m.media_group_id]).start()
        fid = m.photo[-1].file_id if m.content_type == 'photo' else m.video.file_id
        album_buffer[m.media_group_id].append({'id': fid, 'type': m.content_type, 'caption': m.caption})
    else:
        fid = m.photo[-1].file_id if m.content_type == 'photo' else m.video.file_id
        if m.caption:
            num = save_full_entry(m.from_user.id, [{'id': fid, 'type': m.content_type}], m.caption)
            bot.send_message(m.chat.id, f"✅ Сохранено под номером: {num}", reply_markup=get_main_keyboard())
        else:
            upload_states[m.from_user.id] = [{'id': fid, 'type': m.content_type}]
            bot.send_message(m.chat.id, "Шаг 2: Введите описание к контенту:", reply_markup=get_back_keyboard())
            bot.register_next_step_handler(m, finalize_upload_process)


def finalize_upload_process(m):
    if m.text == "🏠 В главное меню" or m.text == "/cancel":
        return cmd_cancel(m)
    if not m.text:
        bot.send_message(m.chat.id, "Пожалуйста, отправьте именно текст.")
        return bot.register_next_step_handler(m, finalize_upload_process)
    files = upload_states.pop(m.from_user.id, None)
    if files:
        num = save_full_entry(m.from_user.id, files, m.text)
        bot.send_message(m.chat.id, f"✅ Успешно сохранено под номером: {num}", reply_markup=get_main_keyboard())


def process_album_completion(chat_id, user_id, mg_id):
    if mg_id not in album_buffer: return
    files = album_buffer.pop(mg_id)
    caption = next((f['caption'] for f in files if f['caption']), None)
    if caption:
        num = save_full_entry(user_id, files, caption)
        bot.send_message(chat_id, f"✅ Альбом сохранен под номером: {num}", reply_markup=get_main_keyboard())
    else:
        upload_states[user_id] = files
        bot.send_message(chat_id, "📥 Альбом получен. Теперь введите описание:", reply_markup=get_back_keyboard())
        bot.register_next_step_handler_by_chat_id(chat_id, finalize_upload_process)


# --- ГАЛЕРЕЯ ---
def render_gallery(chat_id, user_id, post_num=None, photo_index=0, call=None):
    conn = sqlite3.connect('smart_spb.db');
    cursor = conn.cursor()
    cursor.execute("SELECT id, description, timestamp FROM content WHERE user_id = ? ORDER BY id ASC", (user_id,))
    posts = cursor.fetchall()
    if not posts:
        bot.send_message(chat_id, "База данных пуста.", reply_markup=get_main_keyboard())
        return conn.close()

    total_posts = len(posts)
    post_num = int(post_num) if post_num is not None else total_posts
    post_num = max(1, min(post_num, total_posts))
    db_id, desc, ts = posts[post_num - 1]

    cursor.execute("SELECT file_id, file_type FROM media WHERE content_id = ? ORDER BY id ASC", (db_id,))
    media_list = cursor.fetchall();
    conn.close()

    total_photos = len(media_list)
    photo_index = max(0, min(photo_index, total_photos - 1))
    f_id, f_type = media_list[photo_index]

    caption = f"<b>📦 Запись №{post_num}</b> (ID: {db_id})\n⏰ {ts}\n\n{desc or '...'}"
    markup = types.InlineKeyboardMarkup()

    if total_photos > 1:
        markup.row(
            types.InlineKeyboardButton("⏪",
                                       callback_data=f"gal_{post_num}_{photo_index - 1}") if photo_index > 0 else types.InlineKeyboardButton(
                "⛔️", callback_data="none"),
            types.InlineKeyboardButton(f"{photo_index + 1}/{total_photos}", callback_data="none"),
            types.InlineKeyboardButton("⏩",
                                       callback_data=f"gal_{post_num}_{photo_index + 1}") if photo_index < total_photos - 1 else types.InlineKeyboardButton(
                "⛔️", callback_data="none")
        )

    markup.row(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"confdel_{db_id}_{post_num}"),
               types.InlineKeyboardButton("📝 Редактировать", callback_data=f"preedit_{db_id}_{post_num}"))

    # Инверсия по запросу: След (слева), Пред (справа)
    markup.row(
        types.InlineKeyboardButton("След. ➡️",
                                   callback_data=f"gal_{post_num + 1}_0") if post_num < total_posts else types.InlineKeyboardButton(
            "⛔️", callback_data="none"),
        types.InlineKeyboardButton("⬅️ Пред.",
                                   callback_data=f"gal_{post_num - 1}_0") if post_num > 1 else types.InlineKeyboardButton(
            "⛔️", callback_data="none")
    )
    markup.add(types.InlineKeyboardButton("🏠 МЕНЮ", callback_data="to_main"))

    if call:
        media = types.InputMediaPhoto(f_id, caption=caption,
                                      parse_mode="HTML") if f_type == 'photo' else types.InputMediaVideo(f_id,
                                                                                                         caption=caption,
                                                                                                         parse_mode="HTML")
        bot.edit_message_media(media, chat_id, call.message.message_id, reply_markup=markup)
    else:
        if f_type == 'photo':
            bot.send_photo(chat_id, f_id, caption=caption, reply_markup=markup, parse_mode="HTML")
        else:
            bot.send_video(chat_id, f_id, caption=caption, reply_markup=markup, parse_mode="HTML")


# --- ВСПОМОГАТЕЛЬНОЕ ---
def process_edit_album(chat_id, user_id, mg_id, db_id, post_num):
    if mg_id not in album_buffer: return
    files = album_buffer.pop(mg_id)
    finalize_media_edit(user_id, db_id, files, chat_id, post_num)


def finalize_media_edit(user_id, db_id, files, chat_id, post_num):
    conn = sqlite3.connect('smart_spb.db');
    cur = conn.cursor()
    cur.execute("DELETE FROM media WHERE content_id = ?", (db_id,))
    for f in files: cur.execute("INSERT INTO media (content_id, file_id, file_type) VALUES (?,?,?)",
                                (db_id, f['id'], f['type']))
    conn.commit();
    conn.close()
    edit_media_mode.pop(user_id, None)
    bot.send_message(chat_id, "✅ Медиа обновлено!")
    render_gallery(chat_id, user_id, post_num=int(post_num))


@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    if call.data == "to_main":
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=get_main_keyboard())
    if call.data == "none": return bot.answer_callback_query(call.id)
    data = call.data.split("_")

    if data[0] == "gal":
        bot.answer_callback_query(call.id)
        render_gallery(call.message.chat.id, call.from_user.id, data[1], data[2], call=call)
    elif data[0] == "preedit":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 Текст", callback_data=f"edesc_{data[1]}_{data[2]}"),
                   types.InlineKeyboardButton("🖼 Медиа", callback_data=f"emedia_{data[1]}_{data[2]}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_del_{data[2]}"))
        bot.edit_message_caption("Что редактируем?", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif data[0] == "emedia":
        edit_media_mode[call.from_user.id] = (data[1], data[2])
        bot.send_message(call.message.chat.id, "Пришлите новые файлы:", reply_markup=get_back_keyboard())
    elif data[0] == "edesc":
        msg = bot.send_message(call.message.chat.id, "Введите новое описание:", reply_markup=get_back_keyboard())
        bot.register_next_step_handler(msg, lambda m: finalize_edit_desc(m, data[1], data[2]))
    elif data[0] == "confdel":
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"realdel_{data[1]}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_del_{data[2]}"))
        bot.edit_message_caption("Удалить запись?", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif data[0] == "realdel":
        conn = sqlite3.connect('smart_spb.db');
        cur = conn.cursor()
        cur.execute("DELETE FROM content WHERE id = ?", (data[1],))
        conn.commit();
        conn.close()
        bot.answer_callback_query(call.id, "Удалено")
        render_gallery(call.message.chat.id, call.from_user.id)
    elif data[0] == "cancel" and data[1] == "del":
        render_gallery(call.message.chat.id, call.from_user.id, data[2], 0, call=call)
    elif data[0] == "pg":
        render_list(call.message.chat.id, call.from_user.id, int(data[2]), data[1], (data[3] if len(data) > 3 else ""),
                    call=call)
    elif data[0] == "tagview":
        render_list(call.message.chat.id, call.from_user.id, mode="tag", search_val=data[1])
    elif data[0] == "choose":
        conn = sqlite3.connect('smart_spb.db');
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT t.tag_name FROM tags t JOIN content_tags ct ON t.id = ct.tag_id JOIN content c ON ct.content_id = c.id WHERE c.user_id = ? AND t.tag_type = ?",
            (call.from_user.id, data[1]))
        rows = cur.fetchall();
        conn.close()
        markup = types.InlineKeyboardMarkup(row_width=2)
        for r in rows:
            clean = r[0][1:] if r[0].startswith("#") else r[0]
            markup.add(types.InlineKeyboardButton(text=r[0], callback_data=f"tagview_{clean}"))
        bot.edit_message_text("Выберите тег:", call.message.chat.id, call.message.message_id, reply_markup=markup)


# --- ПОИСК ---
@bot.message_handler(func=lambda m: m.text == "🔍 Поиск")
def cmd_search(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("📅 За N дней", "📅 Диапазон", "🆔 По ID",
                                                                 "🏠 В главное меню")
    bot.send_message(m.chat.id, "Выберите способ поиска:", reply_markup=markup)


def render_list(chat_id, target_user_id, page=1, mode="all", search_val="", call=None):
    per_page = 10;
    offset = (page - 1) * per_page
    conn = sqlite3.connect('smart_spb.db');
    cursor = conn.cursor()
    condition = "user_id = ?";
    params = [target_user_id]
    if mode == "tag":
        condition += " AND id IN (SELECT content_id FROM content_tags WHERE tag_id IN (SELECT id FROM tags WHERE tag_name = ? OR tag_name = ?))"
        params.extend([search_val, f"#{search_val}"])
    elif mode == "range":
        d1, d2 = search_val.split("|")
        condition += " AND iso_date BETWEEN ? AND ?"
        params.extend([f"{d1} 00:00:00", f"{d2} 23:59:59"])

    cursor.execute(f"SELECT COUNT(*) FROM content WHERE {condition}", params)
    total = cursor.fetchone()[0];
    total_pages = (total + per_page - 1) // per_page
    cursor.execute(
        f"SELECT id, description, timestamp FROM content WHERE {condition} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, per_page, offset))
    records = cursor.fetchall()

    if not records:
        bot.send_message(chat_id, "Ничего не найдено.")
        return conn.close()

    text = f"<b>📂 Найдено записей: {total} (Стр. {page}/{total_pages})</b>\n\n"
    markup = types.InlineKeyboardMarkup(row_width=5);
    btns = []
    for r in records:
        cursor.execute("SELECT COUNT(*) FROM content WHERE user_id = ? AND id <= ?", (target_user_id, r[0]))
        real_num = cursor.fetchone()[0]
        text += f"<b>{real_num}.</b> {r[2]} | {r[1][:25] if r[1] else '...'}\n"
        btns.append(types.InlineKeyboardButton(text=str(real_num), callback_data=f"gal_{real_num}_0"))

    markup.add(*btns)
    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"pg_{mode}_{page - 1}_{search_val}"))
    if offset + per_page < total: nav.append(
        types.InlineKeyboardButton("➡️", callback_data=f"pg_{mode}_{page + 1}_{search_val}"))
    if nav: markup.row(*nav)
    markup.add(types.InlineKeyboardButton("🏠 МЕНЮ", callback_data="to_main"))

    if call:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    conn.close()


def finalize_edit_desc(m, db_id, num):
    if m.text == "🏠 В главное меню" or m.text == "/cancel": return cmd_cancel(m)
    conn = sqlite3.connect('smart_spb.db');
    cur = conn.cursor()
    cur.execute("UPDATE content SET description = ? WHERE id = ?", (m.text, db_id))
    update_tags_in_db(cur, db_id, m.text);
    conn.commit();
    conn.close()
    bot.send_message(m.chat.id, "✅ Описание обновлено")
    render_gallery(m.chat.id, m.from_user.id, int(num), 0)


@bot.message_handler(func=lambda m: m.text == "🖼 Галерея")
def cmd_gallery(m): render_gallery(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text == "📂 Все ваши загрузки")
def cmd_all(m): render_list(m.chat.id, m.from_user.id)


@bot.message_handler(func=lambda m: m.text == "🏷 Теги")
def cmd_tags(m):
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("👤 Мои теги", callback_data="choose_user"),
                                              types.InlineKeyboardButton("🤖 AI теги", callback_data="choose_ai"))
    bot.send_message(m.chat.id, "Выберите категорию:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "📅 За N дней")
def s_days(m):
    msg = bot.send_message(m.chat.id, "Введите число дней:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(msg, p_days)


def p_days(m):
    if m.text == "🏠 В главное меню": return cmd_cancel(m)
    if not m.text.isdigit(): return bot.send_message(m.chat.id, "Введите число.")
    limit = (datetime.now() - timedelta(days=int(m.text))).strftime("%Y-%m-%d")
    render_list(m.chat.id, m.from_user.id, mode="range", search_val=f"{limit}|{datetime.now().strftime('%Y-%m-%d')}")


@bot.message_handler(func=lambda m: m.text == "📅 Диапазон")
def s_range(m):
    msg = bot.send_message(m.chat.id, "Формат: 01.01.2024-10.01.2024", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(msg, p_range)


def p_range(m):
    if m.text == "🏠 В главное меню": return cmd_cancel(m)
    try:
        d1, d2 = m.text.split("-")
        date1 = datetime.strptime(d1.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
        date2 = datetime.strptime(d2.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
        render_list(m.chat.id, m.from_user.id, mode="range", search_val=f"{date1}|{date2}")
    except:
        bot.send_message(m.chat.id, "Ошибка формата.")


@bot.message_handler(func=lambda m: m.text == "🆔 По ID")
def s_id(m):
    msg = bot.send_message(m.chat.id, "Введите номер поста:", reply_markup=get_back_keyboard())
    bot.register_next_step_handler(msg, p_id)


def p_id(m):
    if m.text == "🏠 В главное меню": return cmd_cancel(m)
    if not m.text.isdigit(): return bot.send_message(m.chat.id, "Введите число.")
    render_gallery(m.chat.id, m.from_user.id, post_num=int(m.text))


@bot.message_handler(func=lambda m: m.text == "🏠 В главное меню")
def back_home(m): cmd_cancel(m)


if __name__ == '__main__':
    init_db()
    bot.polling(none_stop=True)