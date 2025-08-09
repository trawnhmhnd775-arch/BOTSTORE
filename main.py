# main.py
# Telegram Bot (Polling) - كامل يدعم:
# - إدارة أزرار من داخل البوت (إضافة/حذف/تعديل)
# - تعديل زر رئيسي: إضافة نص، إضافة صورة (رابط أو رفع صورة)، تحويل لطلب معلومات
# - أشكال عرض أزرار: vertical/horizontal/grid
# - أسعار بالدولار داخل النص (مثال: "خدمة (1$)") وتتحول تلقائياً لليرة السورية إذا وضع الأدمن سعر الصرف
# - ملفات JSON: config.json, services.json, buttons.json, users.json, orders.json
# - تشغيل بالـ polling
#
# تثبيت الحزم المطلوبة:
# pip install pyTelegramBotAPI APScheduler

import os
import json
import logging
import uuid
import re
from datetime import datetime
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---------------- log ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- files & defaults ----------------
LOCK = Lock()

CONFIG_FILE = "config.json"
SERVICES_FILE = "services.json"   # optional list of services structured
BUTTONS_FILE = "buttons.json"
USERS_FILE = "users.json"
ORDERS_FILE = "orders.json"

DEFAULT_CONFIG = {
    "BOT_TOKEN": "PUT_YOUR_BOT_TOKEN_HERE",
    "ADMIN_IDS": [],             # ضع ID الأدمن هنا
    "BOT_STATUS": "on",
    "ALLOW_LINKS": False,
    "EXCHANGE_RATE": None,       # سعر الصرف: مثال 15000
    "CURRENCY_DEFAULT": "AUTO",  # "USD","SYP","AUTO"
    "BUTTON_LAYOUT": {"type": "vertical", "grid_columns": 2}
}

# default buttons structure (main_menu is list)
DEFAULT_BUTTONS = {
    "main_menu": [
        {
            "id": "services",
            "text": "🎮 خدمات الألعاب",
            "type": "submenu",
            "submenu": [
                {"id": "pubg", "text": "شحن شدات PUBG (1$)", "type": "request_info", "info_request": "أرسل ID اللعبة + الباقة المطلوبة"},
                {"id": "ff", "text": "شحن Free Fire (2.5$)", "type": "request_info", "info_request": "أرسل ID اللعبة + الباقة المطلوبة"}
            ],
            "image": "",           # رابط أو file_id
            "description": ""      # نص إضافي يظهر تحت الصورة عند الحاجة
        },
        {"id": "contact", "text": "📩 تواصل مع الأدمن", "type": "contact_admin", "image": "", "description": ""}
    ]
}

DEFAULT_SERVICES = []  # optional, not strictly required
DEFAULT_USERS = {}
DEFAULT_ORDERS = []
DEFAULT_ADMINS = {"admins": []}

# ---------------- file helpers ----------------
def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)

def load_json(path, default=None):
    with LOCK:
        if not os.path.exists(path):
            if default is not None:
                ensure_file(path, default)
                return json.loads(json.dumps(default))
            return {} if default is None else default
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception as e:
                logger.exception("Failed to load %s: %s", path, e)
                return default if default is not None else {}

def save_json(path, data):
    with LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ensure files exist
ensure_file(CONFIG_FILE, DEFAULT_CONFIG)
ensure_file(BUTTONS_FILE, DEFAULT_BUTTONS)
ensure_file(SERVICES_FILE, DEFAULT_SERVICES)
ensure_file(USERS_FILE, DEFAULT_USERS)
ensure_file(ORDERS_FILE, DEFAULT_ORDERS)

# load data
CONFIG = load_json(CONFIG_FILE, DEFAULT_CONFIG)
BUTTONS = load_json(BUTTONS_FILE, DEFAULT_BUTTONS)
SERVICES = load_json(SERVICES_FILE, DEFAULT_SERVICES)
USERS = load_json(USERS_FILE, DEFAULT_USERS)
ORDERS = load_json(ORDERS_FILE, DEFAULT_ORDERS)
ADMINS = load_json("admins.json", DEFAULT_ADMINS) if os.path.exists("admins.json") else DEFAULT_ADMINS

BOT_TOKEN = CONFIG.get("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
    logger.error("ضع BOT_TOKEN في config.json ثم أعد التشغيل.")
    raise SystemExit("BOT_TOKEN missing in config.json")

# runtime vars
ADMIN_IDS = set(CONFIG.get("ADMIN_IDS", []))
EXCHANGE_RATE = CONFIG.get("EXCHANGE_RATE")
BUTTON_LAYOUT = CONFIG.get("BUTTON_LAYOUT", {"type":"vertical","grid_columns":2})

# initialize bot and scheduler
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
scheduler = BackgroundScheduler()
scheduler.start()

# admin sessions for multi-step flows
admin_sessions = {}  # admin_id -> {action:, temp:...}

# regex for price like 1$ or 2.5$
PRICE_PATTERN = re.compile(r"(-?\d+(?:\.\d+)?)\s*\$")

# ---------------- utility functions ----------------
def format_number(n):
    try:
        if abs(n - int(n)) < 0.001:
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except Exception:
        return str(n)

def convert_text_prices(text, target_currency, rate):
    def repl(m):
        val = float(m.group(1))
        if target_currency == "USD":
            return f"{int(val)}$" if val.is_integer() else f"{val}$"
        if target_currency == "SYP":
            if rate is None:
                return f"{int(val)}$" if val.is_integer() else f"{val}$"
            converted = val * float(rate)
            return f"{format_number(converted)} ل.س"
        return m.group(0)
    return PRICE_PATTERN.sub(repl, text)

def user_currency(uid_str):
    u = USERS.get(uid_str, {})
    pref = u.get("currency_pref")
    if not pref:
        pref = CONFIG.get("CURRENCY_DEFAULT", "AUTO")
    if pref == "AUTO":
        return "SYP" if CONFIG.get("EXCHANGE_RATE") else "USD"
    return pref

def is_admin_user(user_id):
    if user_id in set(CONFIG.get("ADMIN_IDS", [])):
        return True
    for a in ADMINS.get("admins", []):
        if a.get("id") == user_id:
            return True
    return False

def find_button_by_id(bid, btn_list=None):
    if btn_list is None:
        btn_list = BUTTONS.get("main_menu", [])
    for b in btn_list:
        if b.get("id") == bid or b.get("text") == bid:
            return b
        if b.get("type") == "submenu":
            found = find_button_by_id(bid, b.get("submenu", []))
            if found:
                return found
    return None

def build_keyboard_from_buttons(btn_list, uid_str=None):
    layout = CONFIG.get("BUTTON_LAYOUT", {"type":"vertical","grid_columns":2})
    ltype = layout.get("type", "vertical")
    cols = int(layout.get("grid_columns", 2) or 2)
    kb = InlineKeyboardMarkup()
    pref = user_currency(uid_str) if uid_str else CONFIG.get("CURRENCY_DEFAULT","AUTO")
    rate = CONFIG.get("EXCHANGE_RATE")
    displayed = []
    for b in btn_list:
        displayed_text = convert_text_prices(b.get("text",""), pref, rate)
        displayed.append((b.get("id"), displayed_text))
    if ltype == "vertical":
        for bid, text in displayed:
            kb.add(InlineKeyboardButton(text, callback_data=f"BTN|{bid}"))
    elif ltype == "horizontal":
        # one row
        row = [InlineKeyboardButton(text, callback_data=f"BTN|{bid}") for bid,text in displayed]
        if row:
            kb.row(*row)
    elif ltype == "grid":
        row = []
        count = 0
        for bid,text in displayed:
            row.append(InlineKeyboardButton(text, callback_data=f"BTN|{bid}"))
            count += 1
            if count % cols == 0:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
    # always add toggle currency + admin shortcut if admin
    kb.add(InlineKeyboardButton("🔄 تبديل العملة", callback_data="NAV|toggle_currency"))
    kb.add(InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home"))
    return kb

def build_main_menu(uid_str=None):
    return build_keyboard_from_buttons(BUTTONS.get("main_menu", []), uid_str)

def build_submenu_kb(submenu, uid_str=None):
    return build_keyboard_from_buttons(submenu, uid_str)

def notify_admins(text):
    admin_ids = set(CONFIG.get("ADMIN_IDS", [])) | set([a.get("id") for a in ADMINS.get("admins", [])])
    sent = 0
    for aid in admin_ids:
        try:
            bot.send_message(aid, text)
            sent += 1
        except Exception:
            pass
    return sent

# ---------------- Start / Help ----------------
WELCOME_HTML = "<b>🎮 أهلاً بك</b>\nاختر الخدمة من القائمة."

@bot.message_handler(commands=["start","help"])
def cmd_start(m):
    uid = str(m.chat.id)
    if uid not in USERS:
        USERS[uid] = {"id": m.chat.id, "name": m.from_user.full_name or m.from_user.first_name,
                      "first_seen": datetime.now().isoformat(), "awaiting": None, "currency_pref": "AUTO"}
        save_json(USERS_FILE, USERS)
    if CONFIG.get("BOT_STATUS","on") == "off" and not is_admin_user(m.chat.id):
        bot.send_message(m.chat.id, "🚫 البوت متوقف حالياً.")
        return
    kb = build_main_menu(uid)
    bot.send_message(m.chat.id, WELCOME_HTML, reply_markup=kb)

# ---------------- catch all (block free text unless awaiting) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','photo'])
def catch_all(m):
    uid = str(m.chat.id)
    # admin session flows
    if is_admin_user(m.chat.id):
        if admin_sessions.get(m.chat.id):
            handle_admin_session_input(m, admin_sessions[m.chat.id])
            return
    # if user awaiting info
    user = USERS.get(uid)
    if user and user.get("awaiting"):
        awaiting = user["awaiting"]
        if not CONFIG.get("ALLOW_LINKS", False) and m.content_type == 'text':
            txt = m.text or ""
            if txt.startswith("http://") or txt.startswith("https://"):
                bot.send_message(m.chat.id, "🚫 الروابط غير مسموحة. أعد الإرسال بدون رابط.", reply_markup=build_main_menu(uid))
                return
        if m.content_type == 'photo':
            file_id = m.photo[-1].file_id
            info = {"type":"photo","file_id":file_id}
        else:
            info = {"type":"text","text": m.text}
        order = {
            "order_id": str(uuid.uuid4()),
            "user_id": m.chat.id,
            "user_name": user.get("name"),
            "button_id": awaiting.get("button_id"),
            "button_text": awaiting.get("button_text"),
            "info": info,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }
        ORDERS.append(order)
        save_json(ORDERS_FILE, ORDERS)
        USERS[uid]["awaiting"] = None
        save_json(USERS_FILE, USERS)
        bot.send_message(m.chat.id, "✅ طلبك قيد المراجعة سيتم إعلامك بالنتيجة قريبًا.")
        pretty = f"📥 طلب جديد\n👤 {order['user_name']} (ID:{order['user_id']})\n📦 {order['button_text']}\nOrderID: {order['order_id']}\n"
        if info["type"] == "text":
            pretty += f"📝 {info['text']}"
        else:
            pretty += f"🖼 صورة (file_id:{info['file_id']})"
        notify_admins(pretty)
        return
    # otherwise block free messages
    if not (user and user.get("awaiting")):
        if not is_admin_user(m.chat.id):
            bot.send_message(m.chat.id, "⚠️ الرجاء استخدام الأزرار فقط.", reply_markup=build_main_menu(uid))
            return

# ---------------- callback handling ----------------
@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    data = call.data
    uid = call.from_user.id
    uid_str = str(uid)

    # navigation
    if data == "NAV|home":
        try:
            bot.edit_message_text(WELCOME_HTML, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=build_main_menu(uid_str))
        except Exception:
            bot.send_message(call.message.chat.id, WELCOME_HTML, reply_markup=build_main_menu(uid_str))
        bot.answer_callback_query(call.id)
        return
    if data == "NAV|toggle_currency":
        u = USERS.get(uid_str, {})
        pref = u.get("currency_pref","AUTO")
        # cycle AUTO -> USD -> SYP -> AUTO
        new = "USD" if pref == "AUTO" else ("SYP" if pref == "USD" else "AUTO")
        USERS.setdefault(uid_str, {})["currency_pref"] = new
        save_json(USERS_FILE, USERS)
        bot.answer_callback_query(call.id, f"تم تغيير العرض إلى: {new}")
        try:
            bot.edit_message_text(WELCOME_HTML, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=build_main_menu(uid_str))
        except Exception:
            bot.send_message(call.message.chat.id, WELCOME_HTML, reply_markup=build_main_menu(uid_str))
        return

    # admin inline
    if data.startswith("ADMIN|"):
        if not is_admin_user(uid):
            bot.answer_callback_query(call.id, "⛔ للأدمن فقط")
            return
        action = data.split("|",1)[1]
        handle_admin_action(call, action)
        bot.answer_callback_query(call.id)
        return

    # special ADMIN editing of specific main button
    if data.startswith("ADMIN_EDIT|"):
        # ADMIN_EDIT|<main_btn_id>|action_name
        parts = data.split("|")
        if len(parts) >= 3:
            main_btn_id = parts[1]
            action = parts[2]
            handle_admin_edit_main_button(call, main_btn_id, action)
            bot.answer_callback_query(call.id)
            return

    # contact
    if data.startswith("CONTACT|"):
        sub = data.split("|",1)[1]
        if sub == "send":
            bot.send_message(call.message.chat.id, "✉️ أرسل رسالتك الآن (نص أو صورة):")
            bot.register_next_step_handler(call.message, user_send_message_to_admin)
            bot.answer_callback_query(call.id)
            return

    # order admin operations
    if data.startswith("ORDER|"):
        parts = data.split("|")
        if len(parts) >= 3:
            order_id = parts[1]
            action = parts[2]
            admin_order_action(call, order_id, action)
            bot.answer_callback_query(call.id)
            return

    # normal button
    if data.startswith("BTN|"):
        bid = data.split("|",1)[1]
        btn = find_button_by_id(bid)
        if not btn:
            bot.answer_callback_query(call.id, "⚠️ الزر غير موجود")
            return
        btype = btn.get("type")
        pref = user_currency(uid_str)
        rate = CONFIG.get("EXCHANGE_RATE")
        if btype == "submenu":
            submenu = btn.get("submenu", [])
            # if main button has image/description, send it first (image above text)
            main_image = btn.get("image","")
            desc = btn.get("description","")
            header = convert_text_prices(btn.get("text",""), pref, rate)
            try:
                if main_image:
                    # try send photo with caption header + desc
                    caption = header
                    if desc:
                        caption += "\n\n" + convert_text_prices(desc, pref, rate)
                    bot.edit_message_text(caption, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML", reply_markup=build_submenu_kb(submenu, uid_str))
                else:
                    bot.edit_message_text(f"<b>{header}</b>\n{convert_text_prices(desc, pref, rate)}", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML", reply_markup=build_submenu_kb(submenu, uid_str))
            except Exception:
                # fallback send as new message
                if main_image:
                    try:
                        bot.send_photo(call.message.chat.id, main_image, caption=header + ("\n\n"+convert_text_prices(desc,pref,rate) if desc else ""), parse_mode="HTML", reply_markup=build_submenu_kb(submenu, uid_str))
                    except Exception:
                        bot.send_message(call.message.chat.id, header + ("\n\n"+desc if desc else ""), parse_mode="HTML", reply_markup=build_submenu_kb(submenu, uid_str))
                else:
                    bot.send_message(call.message.chat.id, f"<b>{header}</b>\n{convert_text_prices(desc,pref,rate)}", parse_mode="HTML", reply_markup=build_submenu_kb(submenu, uid_str))
            bot.answer_callback_query(call.id)
            return
        if btype == "content":
            text = convert_text_prices(btn.get("content",""), pref, rate)
            image = btn.get("image","")
            if image:
                try:
                    bot.send_photo(call.message.chat.id, image, caption=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home")]]))
                except Exception:
                    bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home")]]))
            else:
                bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home")]]))
            bot.answer_callback_query(call.id)
            return
        if btype == "request_info":
            USERS.setdefault(uid_str, {"id": uid, "name": call.from_user.full_name, "first_seen": datetime.now().isoformat(), "awaiting": None, "currency_pref":"AUTO"})
            USERS[uid_str]["awaiting"] = {"button_id": btn.get("id"), "button_text": btn.get("text"), "prompt": btn.get("info_request", "أرسل المعلومات المطلوبة")}
            save_json(USERS_FILE, USERS)
            prompt = USERS[uid_str]["awaiting"]["prompt"]
            prompt = convert_text_prices(prompt, user_currency(uid_str), CONFIG.get("EXCHANGE_RATE"))
            bot.send_message(call.message.chat.id, prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home")]]))
            bot.answer_callback_query(call.id)
            return
        if btype == "contact_admin":
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("✉️ إرسال رسالة للأدمن", callback_data="CONTACT|send"))
            kb.add(InlineKeyboardButton("🏠 الرئيسية", callback_data="NAV|home"))
            bot.send_message(call.message.chat.id, "اختر:", reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

    bot.answer_callback_query(call.id, "حدث خطأ أو الزر غير معروف.")

# ---------------- user->admin message ----------------
def user_send_message_to_admin(m):
    if m.content_type == 'photo':
        file_id = m.photo[-1].file_id
        for aid in set(CONFIG.get("ADMIN_IDS", [])) | set([a.get("id") for a in ADMINS.get("admins", [])]):
            try:
                bot.send_photo(aid, file_id, caption=f"📩 رسالة من {m.from_user.full_name} (ID:{m.from_user.id})")
            except Exception:
                pass
        bot.send_message(m.chat.id, "✅ تم إرسال الرسالة.")
    else:
        for aid in set(CONFIG.get("ADMIN_IDS", [])) | set([a.get("id") for a in ADMINS.get("admins", [])]):
            try:
                bot.send_message(aid, f"📩 رسالة من {m.from_user.full_name} (ID:{m.from_user.id}):\n\n{m.text}")
            except Exception:
                pass
        bot.send_message(m.chat.id, "✅ تم إرسال الرسالة.")

# ---------------- admin orders actions ----------------
def admin_order_action(call, order_id, action):
    order = next((o for o in ORDERS if o.get("order_id")==order_id), None)
    if not order:
        bot.send_message(call.message.chat.id, "❌ لم أجد الطلب.")
        return
    if action == "view":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ موافقة", callback_data=f"ORDER|{order_id}|approve"))
        kb.add(InlineKeyboardButton("❌ رفض", callback_data=f"ORDER|{order_id}|reject"))
        kb.add(InlineKeyboardButton("✏️ طلب تعديل", callback_data=f"ORDER|{order_id}|askmore"))
        info = order.get("info")
        info_text = info.get("text") if isinstance(info, dict) and info.get("type")=="text" else ("صورة" if isinstance(info, dict) and info.get("type")=="photo" else str(info))
        bot.send_message(call.message.chat.id, f"📦 {order_id}\n👤 {order.get('user_name')} ({order.get('user_id')})\n📌 {order.get('button_text')}\n📝 {info_text}\nالحالة: {order.get('status')}", reply_markup=kb)
        return
    if action == "approve":
        order["status"] = "approved"
        order["handled_at"] = datetime.now().isoformat()
        save_json(ORDERS_FILE, ORDERS)
        try:
            bot.send_message(order["user_id"], f"✅ تمت الموافقة على طلبك (OrderID:{order_id}). سيتم إتمامه قريبًا.")
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "تمت الموافقة.")
        return
    if action == "reject":
        order["status"] = "rejected"
        order["handled_at"] = datetime.now().isoformat()
        save_json(ORDERS_FILE, ORDERS)
        try:
            bot.send_message(order["user_id"], f"❌ تم رفض طلبك (OrderID:{order_id}). تواصل مع الأدمن.")
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "تم الرفض.")
        return
    if action == "askmore":
        order["status"] = "needs_more"
        save_json(ORDERS_FILE, ORDERS)
        bot.send_message(call.message.chat.id, "✏️ أرسل نص السؤال/الطلب الإضافي للمستخدم:")
        admin_sessions[call.from_user.id] = {"action":"askmore_input","order_id":order_id}
        return

# ---------------- admin session flows (edit main button features) ----------------
def handle_admin_session_input(message, session):
    aid = message.from_user.id
    act = session.get("action")
    try:
        # askmore_input: admin wrote extra question -> send to user
        if act == "askmore_input":
            order_id = session.get("order_id")
            order = next((o for o in ORDERS if o.get("order_id")==order_id), None)
            if not order:
                bot.send_message(aid, "لم أجد الطلب.")
                admin_sessions.pop(aid, None)
                return
            bot.send_message(order["user_id"], f"✏️ من الأدمن: {message.text}\n\nالرجاء الرد هنا.")
            bot.send_message(aid, "تم إرسال الطلب الإضافي للمستخدم.")
            admin_sessions.pop(aid, None)
            return

        # adding a main button - multi step
        if act == "add_button_step1":
            session["temp"] = {"text": message.text.strip()}
            bot.send_message(aid, "أدخل معرف الزر (id باللغة الإنجليزية، بدون مسافات):")
            session["action"] = "add_button_step2"
            return
        if act == "add_button_step2":
            session["temp"]["id"] = message.text.strip()
            bot.send_message(aid, "نوع الزر؟ اكتب: submenu / request_info / content / contact_admin")
            session["action"] = "add_button_step3"
            return
        if act == "add_button_step3":
            kind = message.text.strip()
            session["temp"]["type"] = kind
            if kind == "submenu":
                session["temp"]["submenu"] = []
                bot.send_message(aid, "أرسل عناصر الفرعية بالصيغة id|text|type ثم 'done' عند الانتهاء.")
                session["action"] = "add_button_submenu"
                return
            if kind == "request_info":
                bot.send_message(aid, "أدخل نص الطلب الذي سيشاهده المستخدم:")
                session["action"] = "add_button_finish_request"
                return
            if kind == "content":
                bot.send_message(aid, "أدخل نص المحتوى (HTML مسموح):")
                session["action"] = "add_button_finish_content"
                return
            if kind == "contact_admin":
                tmp = session.get("temp")
                newb = {"id": tmp["id"], "text": tmp["text"], "type": "contact_admin", "image":"", "description":""}
                BUTTONS.setdefault("main_menu", []).append(newb)
                save_json(BUTTONS_FILE, BUTTONS)
                bot.send_message(aid, "✅ تم إضافة زر تواصل مع الأدمن.")
                admin_sessions.pop(aid, None)
                return
            bot.send_message(aid, "نوع غير معروف. ألغيت العملية.")
            admin_sessions.pop(aid, None)
            return

        if act == "add_button_submenu":
            txt = message.text.strip()
            if txt.lower() == "done":
                tmp = session.get("temp")
                BUTTONS.setdefault("main_menu", []).append(tmp)
                save_json(BUTTONS_FILE, BUTTONS)
                bot.send_message(aid, "✅ تم إضافة الزر مع العناصر الفرعية.")
                admin_sessions.pop(aid, None)
                return
            parts = txt.split("|")
            if len(parts) < 3:
                bot.send_message(aid, "خطأ في الصيغة، استخدم id|text|type")
                return
            sid, stext, stype = parts[0].strip(), parts[1].strip(), parts[2].strip()
            item = {"id": sid, "text": stext, "type": stype}
            if stype == "request_info":
                bot.send_message(aid, f"أدخل نص الطلب للمستخدم للعنصر {stext}:")
                session.setdefault("pending", []).append(item)
                session["action"] = "add_button_submenu_prompt"
                return
            else:
                session.setdefault("temp", {}).setdefault("submenu", []).append(item)
                bot.send_message(aid, "تم إضافة العنصر الفرعي. أرسل عنصر آخر أو 'done'.")
                return
        if act == "add_button_submenu_prompt":
            prompt = message.text
            pending = session.get("pending", [])
            if not pending:
                bot.send_message(aid, "خطأ داخلي.")
                admin_sessions.pop(aid, None)
                return
            item = pending.pop(-1)
            item["info_request"] = prompt
            session.setdefault("temp", {}).setdefault("submenu", []).append(item)
            session["action"] = "add_button_submenu"
            bot.send_message(aid, "تم حفظ العنصر الفرعي.")
            return

        if act == "add_button_finish_request":
            prompt = message.text
            tmp = session.get("temp")
            newb = {"id": tmp["id"], "text": tmp["text"], "type": "request_info", "info_request": prompt, "image":"", "description":""}
            BUTTONS.setdefault("main_menu", []).append(newb)
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, "✅ تم إضافة زر request_info.")
            admin_sessions.pop(aid, None)
            return

        if act == "add_button_finish_content":
            tmp = session.get("temp", {})
            tmp["content"] = message.text
            bot.send_message(aid, "أدخل رابط الصورة أو اكتب 'no' للتخطي:")
            session["action"] = "add_button_finish_content_img"
            return
        if act == "add_button_finish_content_img":
            tmp = session.get("temp", {})
            img = message.text.strip()
            if img.lower() == "no":
                img = ""
            newb = {"id": tmp["id"], "text": tmp["text"], "type": "content", "content": tmp.get("content",""), "image": img, "description": ""}
            BUTTONS.setdefault("main_menu", []).append(newb)
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, "✅ تم إضافة زر المحتوى.")
            admin_sessions.pop(aid, None)
            return

        # delete button
        if act == "del_button_step1":
            bid = message.text.strip()
            removed = False
            for i,b in enumerate(BUTTONS.get("main_menu", [])):
                if b.get("id")==bid or b.get("text")==bid:
                    BUTTONS["main_menu"].pop(i)
                    removed = True
                    break
            if removed:
                save_json(BUTTONS_FILE, BUTTONS)
                bot.send_message(aid, f"✅ تم حذف {bid}")
            else:
                bot.send_message(aid, "لم أجد هذا المعرف.")
            admin_sessions.pop(aid, None)
            return

        # set exchange rate
        if act == "set_rate_step1":
            try:
                rate = float(message.text.strip())
            except:
                bot.send_message(aid, "قيمة غير صحيحة. أرسل رقم مثل: 15000")
                admin_sessions.pop(aid, None)
                return
            CONFIG["EXCHANGE_RATE"] = rate
            save_json(CONFIG_FILE, CONFIG)
            bot.send_message(aid, f"✅ تم حفظ سعر الصرف: {rate} ل.س لكل $1")
            # notify users about update (optional)
            for uid in list(USERS.keys()):
                try:
                    bot.send_message(int(uid), f"🔁 تم تحديث سعر الصرف إلى {rate} ل.س لكل $1")
                except Exception:
                    pass
            admin_sessions.pop(aid, None)
            return

        # set layout columns
        if act == "set_layout_columns":
            try:
                cols = int(message.text.strip())
                if cols < 1:
                    raise ValueError()
            except:
                bot.send_message(aid, "أدخل رقمًا صحيحًا للأعمدة.")
                admin_sessions.pop(aid, None)
                return
            CONFIG.setdefault("BUTTON_LAYOUT", {})["grid_columns"] = cols
            save_json(CONFIG_FILE, CONFIG)
            bot.send_message(aid, f"✅ تم تحديث أعمدة الشبكة إلى: {cols}")
            admin_sessions.pop(aid, None)
            return

        # add admin
        if act == "add_admin_step1":
            try:
                new_id = int(message.text.strip())
            except:
                bot.send_message(aid, "ID غير صالح.")
                admin_sessions.pop(aid, None)
                return
            ADMINS.setdefault("admins", []).append({"id": new_id, "name": message.from_user.full_name, "perms": ["all"]})
            save_json("admins.json", ADMINS)
            bot.send_message(aid, f"✅ تم إضافة الأدمن {new_id}")
            admin_sessions.pop(aid, None)
            return

        # edit main button - add text or set description
        if act == "edit_add_text":
            main_id = session.get("temp", {}).get("main_id")
            main_btn = find_button_by_id(main_id)
            if not main_btn:
                bot.send_message(aid, "لم أجد الزر الرئيسي.")
                admin_sessions.pop(aid, None)
                return
            # save description (text under image)
            main_btn["description"] = message.text
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, f"✅ تم إضافة/تعديل الوصف للنقطة الرئيسية ({main_btn.get('id')}).")
            admin_sessions.pop(aid, None)
            return

        # edit main button - add image by URL
        if act == "edit_add_image_url":
            main_id = session.get("temp", {}).get("main_id")
            main_btn = find_button_by_id(main_id)
            if not main_btn:
                bot.send_message(aid, "لم أجد الزر الرئيسي.")
                admin_sessions.pop(aid, None)
                return
            url = message.text.strip()
            main_btn["image"] = url
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, f"✅ تم إضافة/تحديث صورة الزر الرئيسي ({main_btn.get('id')}).")
            admin_sessions.pop(aid, None)
            return

        # edit main button - add image by upload (admin uploaded a photo)
        if act == "edit_add_image_upload":
            if message.content_type != 'photo':
                bot.send_message(aid, "أرسل صورة (Photo) لرفعها كصورة الزر.")
                return
            main_id = session.get("temp", {}).get("main_id")
            main_btn = find_button_by_id(main_id)
            if not main_btn:
                bot.send_message(aid, "لم أجد الزر الرئيسي.")
                admin_sessions.pop(aid, None)
                return
            file_id = message.photo[-1].file_id
            main_btn["image"] = file_id  # store file_id
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, f"✅ تم رفع الصورة وحفظها كصورة للزر ({main_btn.get('id')}).")
            admin_sessions.pop(aid, None)
            return

        # edit main button - set to request_info (make main button itself request_info)
        if act == "edit_set_request_info":
            main_id = session.get("temp", {}).get("main_id")
            main_btn = find_button_by_id(main_id)
            if not main_btn:
                bot.send_message(aid, "لم أجد الزر الرئيسي.")
                admin_sessions.pop(aid, None)
                return
            # ask admin for the prompt question text to show user
            main_btn["type"] = "request_info"
            bot.send_message(aid, "أدخل نص الطلب (السؤال) الذي سيظهر للمستخدم عند ضغط الزر:")
            session["action"] = "edit_set_request_info_prompt"
            return

        if act == "edit_set_request_info_prompt":
            main_id = session.get("temp", {}).get("main_id")
            main_btn = find_button_by_id(main_id)
            if not main_btn:
                bot.send_message(aid, "لم أجد الزر الرئيسي.")
                admin_sessions.pop(aid, None)
                return
            prompt = message.text
            main_btn["info_request"] = prompt
            save_json(BUTTONS_FILE, BUTTONS)
            bot.send_message(aid, f"✅ تم تحويل الزر الرئيسي إلى طلب معلومات مع النص المحدد.")
            admin_sessions.pop(aid, None)
            return

    except Exception as e:
        logger.exception("admin session error: %s", e)
        bot.send_message(aid, "حدث خطأ خلال الجلسة. تم إلغاؤها.")
        admin_sessions.pop(aid, None)

# ---------------- admin command /admin ----------------
@bot.message_handler(commands=["admin","panel"])
def cmd_admin(m):
    if not is_admin_user(m.chat.id):
        bot.reply_to(m, "⛔ ليس لديك صلاحية الوصول للوحة الأدمن.")
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🧭 إدارة الأزرار", callback_data="ADMIN|manage_buttons"))
    kb.add(InlineKeyboardButton("📦 الطلبات", callback_data="ADMIN|manage_orders"))
    kb.add(InlineKeyboardButton("📢 بث", callback_data="ADMIN|broadcast"))
    kb.add(InlineKeyboardButton("💱 تعيين سعر الصرف", callback_data="ADMIN|set_rate"))
    kb.add(InlineKeyboardButton("🔲 شكل الأزرار", callback_data="ADMIN|set_layout"))
    kb.add(InlineKeyboardButton("👥 إدارة المشرفين", callback_data="ADMIN|manage_admins"))
    kb.add(InlineKeyboardButton("📊 إحصائيات", callback_data="ADMIN|stats"))
    kb.add(InlineKeyboardButton("⏯ تشغيل/إيقاف البوت", callback_data="ADMIN|toggle"))
    bot.send_message(m.chat.id, "لوحة الأدمن — اختر:", reply_markup=kb)

def handle_admin_action(call, action):
    aid = call.from_user.id
    if action == "manage_buttons":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("➕ إضافة زر", callback_data="ADMIN|add_button"))
        kb.add(InlineKeyboardButton("✏️ تعديل زر رئيسي", callback_data="ADMIN|edit_main_list"))
        kb.add(InlineKeyboardButton("🗑 حذف زر", callback_data="ADMIN|del_button"))
        kb.add(InlineKeyboardButton("🔁 عرض القوائم", callback_data="ADMIN|show_buttons"))
        kb.add(InlineKeyboardButton("🏠 رجوع", callback_data="NAV|home"))
        bot.send_message(aid, "إدارة الأزرار:", reply_markup=kb)
        return
    if action == "manage_orders":
        if not ORDERS:
            bot.send_message(aid, "لا توجد طلبات حالياً.")
            return
        kb = InlineKeyboardMarkup()
        for o in ORDERS[-40:][::-1]:
            kb.add(InlineKeyboardButton(f"{o.get('button_text')} - {o.get('user_name')}", callback_data=f"ORDER|{o.get('order_id')}|view"))
        bot.send_message(aid, "قائمة الطلبات:", reply_markup=kb)
        return
    if action == "broadcast":
        bot.send_message(aid, "✏️ أرسل نص البث (HTML مسموح):")
        admin_sessions[aid] = {"action":"broadcast_step1"}
        return
    if action == "set_rate":
        bot.send_message(aid, "أرسل سعر الصرف الآن (مثال: 15000):")
        admin_sessions[aid] = {"action":"set_rate_step1"}
        return
    if action == "set_layout":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("عمودي (vertical)", callback_data="ADMIN|layout_vertical"))
        kb.add(InlineKeyboardButton("أفقي (horizontal)", callback_data="ADMIN|layout_horizontal"))
        kb.add(InlineKeyboardButton("شبكة (grid) - تحديد الأعمدة", callback_data="ADMIN|layout_grid"))
        bot.send_message(aid, "اختر شكل عرض الأزرار:", reply_markup=kb)
        return
    if action == "manage_admins":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("➕ إضافة أدمن", callback_data="ADMIN|add_admin"))
        kb.add(InlineKeyboardButton("🗑 حذف أدمن", callback_data="ADMIN|del_admin"))
        bot.send_message(aid, "إدارة المشرفين:", reply_markup=kb)
        return
    if action == "stats":
        users_count = len(USERS)
        orders_count = len(ORDERS)
        counts = {}
        for o in ORDERS:
            key = o.get("button_text","unknown")
            counts[key] = counts.get(key,0)+1
        most_used = max(counts.items(), key=lambda x:x[1])[0] if counts else "لا يوجد"
        bot.send_message(aid, f"📊 إحصائيات:\n👥 المستخدمين: {users_count}\n📦 الطلبات: {orders_count}\n⭐ الأكثر استخدامًا: {most_used}")
        return
    if action == "toggle":
        CONFIG["BOT_STATUS"] = "off" if CONFIG.get("BOT_STATUS","on")=="on" else "on"
        save_json(CONFIG_FILE, CONFIG)
        bot.send_message(aid, f"🔁 تم تغيير حالة البوت إلى: {CONFIG['BOT_STATUS']}")
        return
    if action == "add_button":
        bot.send_message(aid, "🔰 أدخل نص الزر (سيظهر للمستخدم):")
        admin_sessions[aid] = {"action":"add_button_step1"}
        return
    if action == "del_button":
        bot.send_message(aid, "🗑 أرسل معرف الزر (id) أو نصه لحذفه:")
        admin_sessions[aid] = {"action":"del_button_step1"}
        return
    if action == "show_buttons":
        lines = ["قائمة الأزرار الحالية:"]
        for b in BUTTONS.get("main_menu", []):
            lines.append(f"- {b.get('id')} | {b.get('text')} | {b.get('type')} | image:{'yes' if b.get('image') else 'no'}")
            if b.get("type") == "submenu":
                for s in b.get("submenu", []):
                    lines.append(f"   • {s.get('id')} | {s.get('text')} | {s.get('type')}")
        bot.send_message(aid, "\n".join(lines))
        return
    if action == "add_admin":
        bot.send_message(aid, "أرسل ID الأدمن الجديد (رقم):")
        admin_sessions[aid] = {"action":"add_admin_step1"}
        return
    if action == "del_admin":
        bot.send_message(aid, "أرسل ID الأدمن للحذف:")
        admin_sessions[aid] = {"action":"del_admin_step1"}
        return

# ---------------- admin inline to edit main list ----------------
@bot.callback_query_handler(func=lambda c: c.data == "ADMIN|edit_main_list")
def admin_edit_main_list(call):
    aid = call.from_user.id
    if not is_admin_user(aid):
        bot.answer_callback_query(call.id, "⛔ للأدمن فقط")
        return
    kb = InlineKeyboardMarkup()
    for m in BUTTONS.get("main_menu", []):
        kb.add(InlineKeyboardButton(m.get("text"), callback_data=f"ADMIN_EDIT|{m.get('id')}|menu"))
    kb.add(InlineKeyboardButton("🏠 رجوع", callback_data="NAV|home"))
    bot.send_message(aid, "اختر الزر الرئيسي الذي تريد تعديله:", reply_markup=kb)
    bot.answer_callback_query(call.id)

def handle_admin_edit_main_button(call, main_id, action):
    aid = call.from_user.id
    main_btn = find_button_by_id(main_id)
    if not main_btn:
        bot.send_message(aid, "❌ الزر غير موجود.")
        return
    # if action is 'menu' -> show edit options for this main button
    if action == "menu":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✏️ إضافة/تعديل وصف النص أسفل الصورة", callback_data=f"ADMIN_EDIT|{main_id}|add_text"))
        kb.add(InlineKeyboardButton("🖼 إضافة/تعديل صورة (رابط)", callback_data=f"ADMIN_EDIT|{main_id}|add_image_url"))
        kb.add(InlineKeyboardButton("📤 رفع صورة جديدة (أرسلها الآن)", callback_data=f"ADMIN_EDIT|{main_id}|add_image_upload"))
        kb.add(InlineKeyboardButton("📥 تحويل الزر إلى طلب معلومات (request_info)", callback_data=f"ADMIN_EDIT|{main_id}|set_request_info"))
        kb.add(InlineKeyboardButton("🔁 عرض العناصر الفرعية", callback_data=f"ADMIN_EDIT|{main_id}|show_subs"))
        kb.add(InlineKeyboardButton("🏠 رجوع", callback_data="NAV|home"))
        bot.send_message(aid, f"تحكم بالزر الرئيسي: {main_btn.get('text')}", reply_markup=kb)
        return
    if action == "add_text":
        bot.send_message(aid, "أرسل نص الوصف الذي تريد إضافته/تعديله للزر الرئيسي (سيظهر تحت الصورة):")
        admin_sessions[aid] = {"action":"edit_add_text", "temp":{"main_id": main_id}}
        return
    if action == "add_image_url":
        bot.send_message(aid, "أرسل رابط الصورة (URL) ليتم حفظه كصورة للزر الرئيسي:")
        admin_sessions[aid] = {"action":"edit_add_image_url", "temp":{"main_id": main_id}}
        return
    if action == "add_image_upload":
        bot.send_message(aid, "الآن أرسل صورة (Photo) لرفعها وتخزين file_id كصورة للزر الرئيسي:")
        admin_sessions[aid] = {"action":"edit_add_image_upload", "temp":{"main_id": main_id}}
        return
    if action == "set_request_info":
        # action will take next step to ask for prompt text
        bot.send_message(aid, "سيتم تحويل الزر الرئيسي إلى زر يطلب معلومات من المستخدم. أرسل الآن نص الطلب (سيشاهده المستخدم).")
        admin_sessions[aid] = {"action":"edit_set_request_info", "temp":{"main_id": main_id}}
        return
    if action == "show_subs":
        subs = main_btn.get("submenu", [])
        if not subs:
            bot.send_message(aid, "لا توجد عناصر فرعية لهذا الزر.")
            return
        lines = [f"عناصر فرعية لزر {main_btn.get('text')}:"]
        for s in subs:
            lines.append(f"- {s.get('id')} | {s.get('text')} | {s.get('type')}")
        bot.send_message(aid, "\n".join(lines))
        return

# ---------------- layout selection handlers ----------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("ADMIN|layout_"))
def layout_handlers(call):
    action = call.data.split("|",1)[1]
    aid = call.from_user.id
    if not is_admin_user(aid):
        bot.answer_callback_query(call.id, "⛔ للأدمن فقط")
        return
    if action == "layout_vertical":
        CONFIG.setdefault("BUTTON_LAYOUT", {})["type"] = "vertical"
        save_json(CONFIG_FILE, CONFIG)
        bot.send_message(aid, "✅ تم تعيين شكل العرض: vertical")
    elif action == "layout_horizontal":
        CONFIG.setdefault("BUTTON_LAYOUT", {})["type"] = "horizontal"
        save_json(CONFIG_FILE, CONFIG)
        bot.send_message(aid, "✅ تم تعيين شكل العرض: horizontal")
    elif action == "layout_grid":
        CONFIG.setdefault("BUTTON_LAYOUT", {})["type"] = "grid"
        save_json(CONFIG_FILE, CONFIG)
        bot.send_message(aid, "أرسل عدد الأعمدة للشبكة (مثال: 2):")
        admin_sessions[aid] = {"action":"set_layout_columns"}
    bot.answer_callback_query(call.id)

# ---------------- admin session helpers (broadcast etc.) ----------------
# (already handled in handle_admin_session_input) - no duplication here

# ---------------- start polling ----------------
def save_all():
    save_json(CONFIG_FILE, CONFIG)
    save_json(BUTTONS_FILE, BUTTONS)
    save_json(SERVICES_FILE, SERVICES)
    save_json(USERS_FILE, USERS)
    save_json(ORDERS_FILE, ORDERS)
    save_json("admins.json", ADMINS)

def restore_schedules():
    # placeholder if you want to add schedule restore behavior
    return

def main():
    save_all()
    restore_schedules()
    logger.info("Starting polling...")
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    main()
