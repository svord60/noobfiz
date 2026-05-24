import asyncio
import logging
import re
import os
from datetime import datetime, timedelta
from decimal import Decimal

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, ParseMode, CallbackQuery,
                           Message, LabeledPrice, ContentTypes)
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import (SessionPasswordNeededError,
                             PhoneCodeExpiredError,
                             PhoneCodeInvalidError)
from telethon.sessions import StringSession
from tinydb import TinyDB, Query
from tinydb.storages import JSONStorage

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SECOND_BOT_TOKEN = os.getenv("SECOND_BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
USERBOT_PHONE = os.getenv("USERBOT_PHONE")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")
API_SECRET = os.getenv("API_SECRET", "super_secret_key_123")

CHANNEL_ID = -1002308793392
CHANNEL_LINK = "https://t.me/+Y88DiqFlqBRiNjIy"
SUPPORT_USERNAME = "swordSar"
RULES_LINK = "https://telegra.ph/PhysicHub--Pravila-polzovaniya-servisom-05-06"
REVIEWS_LINK = "https://t.me/c/2308793392/8548"
BOT_USERNAME = "PhysicHubFiz_Bot"
STARS_BOT_USERNAME = "StarsPaPhuchic_Bot"

STARS_RATE = Decimal('2')
ITEMS_PER_PAGE = 10

# Базы данных
db = TinyDB("bot_data.json", storage=JSONStorage, indent=4, ensure_ascii=False)
users_table = db.table("users")
accounts_table = db.table("accounts")
orders_table = db.table("orders")
pending_payments = db.table("pending_payments")
sold_accounts_table = db.table("sold_accounts")
referrals_table = db.table("referrals")
banned_table = db.table("banned")
promocodes_table = db.table("promocodes")

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
second_bot = Bot(token=SECOND_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp_second = Dispatcher(second_bot, storage=storage)

# ==================== HTTP СЕРВЕР ====================
app = web.Application()

async def handle_topup(request):
    try:
        data = await request.json()
        if data.get("secret") != API_SECRET:
            return web.json_response({"status": "error", "message": "Неверный ключ"})
        
        user_id = int(data["user_id"])
        amount = float(data["amount"])
        
        user = users_table.get(Query().user_id == user_id)
        if user:
            old_balance = user.get("balance", 0)
            new_balance = old_balance + amount
            
            users_table.update({"balance": new_balance}, Query().user_id == user_id)
            db.storage.write(db.storage.read())
            
            try:
                await bot.send_message(user_id, f"✅ Баланс пополнен на {format_price(amount)} через Stars!")
            except:
                pass
            
            return web.json_response({"status": "ok", "new_balance": new_balance})
        return web.json_response({"status": "error", "message": "Юзер не найден"})
    except Exception as e:
        logging.error(f"Topup error: {e}")
        return web.json_response({"status": "error", "message": str(e)})

app.router.add_post("/topup", handle_topup)

# ==================== FSM ====================
class AddAccount(StatesGroup):
    waiting_country = State()
    waiting_type = State()
    waiting_year = State()
    waiting_price = State()
    waiting_phone = State()
    waiting_password = State()
    waiting_code = State()

class Broadcast(StatesGroup):
    waiting_text = State()

class TopUp(StatesGroup):
    waiting_crypto_amount = State()
    waiting_stars_amount = State()

class BanUser(StatesGroup):
    waiting_ban_id = State()

class PromoCode(StatesGroup):
    waiting_promo = State()

class TransferMoney(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

# ==================== ПРОВЕРКА БАНА ====================
def is_banned(user_id):
    banned = banned_table.get(Query().user_id == user_id)
    return banned is not None

async def check_ban_and_answer(message: Message):
    if is_banned(message.from_user.id):
        await message.answer("🚫 Ваш тикет закрыт.")
        return True
    return False

# ==================== ФУНКЦИИ ====================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user(user_id):
    return users_table.get(Query().user_id == user_id)

def create_user_if_not(user_id, username=None, referrer_id=None):
    user = get_user(user_id)
    if not user:
        users_table.insert({
            "user_id": user_id,
            "username": username or "Неизвестный",
            "balance": 0.0,
            "purchases": 0,
            "referrer_id": referrer_id,
            "created_at": datetime.now().isoformat()
        })
        if referrer_id and referrer_id != user_id:
            existing = referrals_table.get(
                (Query().user_id == referrer_id) & (Query().invited_user_id == user_id)
            )
            if not existing:
                referrals_table.insert({
                    "user_id": referrer_id,
                    "invited_user_id": user_id,
                    "has_purchased": False,
                    "reward_claimed": False
                })
    else:
        users_table.update({"username": username or user.get("username", "Неизвестный")}, Query().user_id == user_id)

def update_balance(user_id, new_balance):
    users_table.update({"balance": new_balance}, Query().user_id == user_id)
    db.storage.write(db.storage.read())

def format_price(price):
    return f"{Decimal(str(price)).quantize(Decimal('0.01'))} ₽"

def get_referral_count(user_id):
    return len(referrals_table.search(Query().user_id == user_id))

# ==================== ПАГИНАЦИЯ ====================
def get_country_keyboard_page(acc_type, page=0):
    keyboard = InlineKeyboardMarkup(row_width=2)
    accounts = accounts_table.search(Query().acc_type == acc_type)
    unique_countries = list(set(a["country_code"] for a in accounts))
    
    if not unique_countries:
        keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="main_menu"))
        return keyboard
    
    unique_countries.sort()
    
    total_pages = (len(unique_countries) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_countries = unique_countries[start:end]
    
    for country in page_countries:
        account = accounts_table.get(Query().country_code == country)
        if account:
            keyboard.insert(InlineKeyboardButton(
                f"{account['country_flag']} {account['country_name']}",
                callback_data=f"country_{acc_type}_{country}"
            ))
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("[ ← Назад ]", callback_data=f"page_{acc_type}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("[ → Вперёд ]", callback_data=f"page_{acc_type}_{page+1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)
    
    # Кнопка Назад (в меню или к выбору типа)
    if page == 0:
        keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="main_menu"))
    else:
        keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="main_menu"))
    
    return keyboard

def admin_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add"),
        InlineKeyboardButton("📊 Остатки", callback_data="admin_stock")
    )
    keyboard.add(
        InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
        InlineKeyboardButton("💳 Пополнить юзеру", callback_data="admin_topup_user")
    )
    keyboard.add(
        InlineKeyboardButton("📋 Рефералы", callback_data="admin_referrals"),
        InlineKeyboardButton("🚫 Бан юзера", callback_data="admin_ban")
    )
    keyboard.add(InlineKeyboardButton("🎫 Создать промокод", callback_data="admin_promo"))
    keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="main_menu"))
    return keyboard

def main_menu_keyboard(user_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        InlineKeyboardButton("[👛] Купить аккаунт", callback_data="buy_regular"),
        InlineKeyboardButton("[👝] Аккаунт с отлегой", callback_data="buy_aged")
    )
    keyboard.row(InlineKeyboardButton("[🎩] Мой профиль", callback_data="profile"))
    keyboard.row(
        InlineKeyboardButton("[🗞] Правила", url=RULES_LINK),
        InlineKeyboardButton("[📓] Отзывы", url=REVIEWS_LINK)
    )
    if is_admin(user_id):
        keyboard.row(InlineKeyboardButton("🛠 Админ-панель", callback_data="admin_panel"))
    return keyboard

async def show_main_menu(user_id):
    caption = (
        "Добро пожаловать ✈️\n\n"
        "<i>Чем мы лучше других сервисов</i>\n"
        "<blockquote>Моментальная выдача аккаунта.\n"
        "Большой ассортимент аккаунтов.\n"
        "Лучшее качество аккаунтов.</blockquote>"
    )
    await bot.send_photo(user_id, "https://iili.io/BQyyE22.jpg", caption=caption, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(user_id))

def get_years_keyboard(country_code):
    keyboard = InlineKeyboardMarkup(row_width=3)
    accounts = accounts_table.search((Query().country_code == country_code) & (Query().acc_type == "отлега"))
    years = sorted(list(set(a["year"] for a in accounts if a.get("year") is not None)), reverse=True)
    if not years:
        keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="buy_aged"))
        return keyboard
    for year in years:
        keyboard.insert(InlineKeyboardButton(str(year), callback_data=f"year_{country_code}_{year}"))
    keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="buy_aged"))
    return keyboard

def get_available_account(acc_type, country_code, year=None):
    search_q = (Query().acc_type == acc_type) & (Query().country_code == country_code)
    accounts = accounts_table.search(search_q)
    
    if year is not None:
        for acc in accounts:
            acc_year = acc.get("year")
            if acc_year is not None and str(acc_year) == str(year):
                return acc
        return None
    
    return accounts[0] if accounts else None

# ==================== CRYPTO BOT ====================
async def get_usdt_rate_from_crypto_bot():
    try:
        url = "https://pay.crypt.bot/api/getExchangeRates"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                result = await resp.json()
                if result.get("ok"):
                    for rate in result["result"]:
                        if rate["source"] == "USDT" and rate["target"] == "RUB" and rate["is_valid"]:
                            return Decimal('1') / Decimal(rate["rate"])
        return Decimal('0.011')
    except:
        return Decimal('0.011')

async def create_crypto_invoice(amount_rub):
    try:
        rub_to_usdt = await get_usdt_rate_from_crypto_bot()
        amount_usd = Decimal(str(amount_rub)) * rub_to_usdt
        amount_usd = amount_usd.quantize(Decimal('0.01'))
        if amount_usd < Decimal('0.1'):
            amount_usd = Decimal('0.1')
        url = "https://pay.crypt.bot/api/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        data = {"asset": "USDT", "amount": str(amount_usd), "description": "Пополнение баланса", "expires_in": 1800}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as resp:
                result = await resp.json()
                if result.get("ok"):
                    return result["result"]["bot_invoice_url"], result["result"]["invoice_id"]
        return None, None
    except:
        return None, None

async def check_crypto_payment(invoice_id):
    try:
        url = "https://pay.crypt.bot/api/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        data = {"invoice_ids": invoice_id}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as resp:
                result = await resp.json()
                if result.get("ok") and result["result"].get("items"):
                    return result["result"]["items"][0]
    except:
        pass
    return None

# ==================== ЮЗЕРБОТ ====================
main_userbot = None
userbot_clients = {}

async def start_main_userbot():
    global main_userbot
    try:
        main_userbot = TelegramClient(StringSession(), API_ID, API_HASH)
        await main_userbot.connect()
        if not await main_userbot.is_user_authorized():
            await main_userbot.send_code_request(USERBOT_PHONE)
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, f"🔐 <b>Регистрация юзербота</b>\n\nНомер: {USERBOT_PHONE}\nОтправь код подтверждения.", parse_mode=ParseMode.HTML)
            return False
        else:
            logging.info("Юзербот уже авторизован")
            return True
    except Exception as e:
        logging.error(f"Main userbot error: {e}")
        return False

async def login_userbot_with_code(code):
    global main_userbot
    try:
        await main_userbot.sign_in(USERBOT_PHONE, code)
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, "✅ Юзербот авторизован!")
        return True
    except SessionPasswordNeededError:
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, "⚠️ Нужен облачный пароль!")
        return False
    except Exception as e:
        logging.error(f"Login error: {e}")
        return False

async def create_userbot_for_account(phone, account_id):
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
        userbot_clients[phone] = {"client": client, "account_id": account_id, "awaiting_code": True}
        
        @client.on(events.NewMessage(from_users=777000))
        async def code_handler(event):
            message_text = event.message.message
            code_match = re.search(r'\b(\d{5})\b', message_text)
            if code_match:
                code = code_match.group(1)
                orders = orders_table.search((Query().phone == phone) & (Query().status == "ожидает_код"))
                if orders:
                    order = orders[0]
                    await bot.send_message(order["buyer_id"], f"🔑 Код: <code>{code}</code>", parse_mode=ParseMode.HTML)
                    orders_table.update({"status": "завершен", "code": code}, Query().doc_id == order.doc_id)
                    
                    account = accounts_table.get(doc_id=account_id)
                    if account and account.get("password"):
                        await asyncio.sleep(1)
                        await bot.send_message(
                            order["buyer_id"],
                            f"🔐 Облачный пароль: <code>{account['password']}</code>",
                            parse_mode=ParseMode.HTML
                        )
        return client
    except Exception as e:
        logging.error(f"Create userbot error: {e}")
        return None

async def login_account_userbot(phone, code):
    if phone not in userbot_clients:
        return False
    
    client = userbot_clients[phone]["client"]
    account_id = userbot_clients[phone]["account_id"]
    
    try:
        await client.sign_in(phone, code)
        accounts_table.update({"has_session": True}, Query().doc_id == account_id)
        userbot_clients[phone]["awaiting_code"] = False
        logging.info(f"Юзербот вошёл в {phone} (без пароля)")
        return True
    except SessionPasswordNeededError:
        account = accounts_table.get(doc_id=account_id)
        password = account.get("password") if account else None
        
        if password:
            try:
                await client.sign_in(password=password)
                accounts_table.update({"has_session": True}, Query().doc_id == account_id)
                userbot_clients[phone]["awaiting_code"] = False
                logging.info(f"Юзербот вошёл в {phone} (с паролем из базы)")
                return True
            except Exception as e:
                logging.error(f"Password error for {phone}: {e}")
                for admin_id in ADMIN_IDS:
                    await bot.send_message(
                        admin_id,
                        f"🔐 <b>Нужен облачный пароль для {phone}!</b>\n"
                        f"Пароль в базе неверный.\n"
                        f"Отправь: <code>/pass {phone} пароль</code>",
                        parse_mode=ParseMode.HTML
                    )
                return False
        else:
            for admin_id in ADMIN_IDS:
                await bot.send_message(
                    admin_id,
                    f"🔐 <b>Нужен облачный пароль для {phone}!</b>\n"
                    f"Пароль не указан в базе.\n"
                    f"Отправь: <code>/pass {phone} пароль</code>",
                    parse_mode=ParseMode.HTML
                )
            return False
    except Exception as e:
        logging.error(f"Account login error: {e}")
        return False

# ==================== КОМАНДА /pass ====================
@dp.message_handler(commands=['pass'])
async def set_password(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: <code>/pass +79991234567 пароль</code>", parse_mode=ParseMode.HTML)
        return
    
    phone = parts[1]
    password = parts[2]
    
    if phone not in userbot_clients:
        await message.answer("❌ Юзербот не найден для этого номера.")
        return
    
    client = userbot_clients[phone]["client"]
    account_id = userbot_clients[phone]["account_id"]
    
    try:
        await client.sign_in(password=password)
        accounts_table.update({"has_session": True, "password": password}, Query().doc_id == account_id)
        userbot_clients[phone]["awaiting_code"] = False
        await message.answer(f"✅ Облачный пароль для {phone} принят! Аккаунт готов к продаже.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ==================== ОБРАБОТЧИК КОДОВ ====================
@dp.message_handler(lambda msg: is_admin(msg.from_user.id) and msg.text and len(msg.text) == 5 and msg.text.isdigit())
async def catch_code(message: Message):
    if await check_ban_and_answer(message):
        return
    code = message.text
    if main_userbot and not await main_userbot.is_user_authorized():
        success = await login_userbot_with_code(code)
        if success:
            await message.answer("✅ Юзербот авторизован!")
        return
    for phone, data in userbot_clients.items():
        if data.get("awaiting_code"):
            success = await login_account_userbot(phone, code)
            if success:
                await message.answer(f"✅ Вход для {phone} выполнен!")
                return

# ==================== /start ====================
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    if await check_ban_and_answer(message):
        return
    
    args = message.get_args()
    referrer_id = None
    if args.startswith("ref_"):
        try:
            referrer_id = int(args.replace("ref_", ""))
        except:
            pass
    
    create_user_if_not(message.from_user.id, message.from_user.username, referrer_id)
    
    try:
        member = await bot.get_chat_member(CHANNEL_ID, message.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            await show_main_menu(message.from_user.id)
        else:
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton("🔔 Подписаться", url=CHANNEL_LINK))
            keyboard.add(InlineKeyboardButton("♻️ Проверить подписку", callback_data="check_sub"))
            await bot.send_photo(message.from_user.id, "https://iili.io/BQyyE22.jpg", caption="<b>Для использования бота подпишитесь на канал!</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except:
        await message.answer("❌ Ошибка")

# ==================== CALLBACK: ПОДПИСКА ====================
@dp.callback_query_handler(text="check_sub")
async def check_subscription(call: CallbackQuery):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, call.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            await call.message.delete()
            await show_main_menu(call.from_user.id)
        else:
            await call.answer("❌ Вы не подписаны!", show_alert=True)
    except:
        await call.answer("❌ Ошибка!")
    await call.answer()

# ==================== CALLBACK: ГЛАВНОЕ МЕНЮ ====================
@dp.callback_query_handler(text="main_menu")
async def back_to_main(call: CallbackQuery):
    try:
        await call.message.delete()
    except:
        pass
    await show_main_menu(call.from_user.id)
    await call.answer()

# ==================== ПАГИНАЦИЯ CALLBACK ====================
@dp.callback_query_handler(lambda call: call.data.startswith("page_"))
async def page_navigation(call: CallbackQuery):
    parts = call.data.split("_")
    acc_type = parts[1]
    page = int(parts[2])
    
    keyboard = get_country_keyboard_page(acc_type, page)
    if acc_type == "обычный":
        await call.message.edit_text("<b>Покупка аккаунта</b>\n\nВыберите страну:", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif acc_type == "отлега":
        await call.message.edit_text("<b>Аккаунты с отлегой</b>\n\nВыберите страну:", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

# ==================== CALLBACK: ПРОФИЛЬ ====================
@dp.callback_query_handler(text="profile")
async def show_profile(call: CallbackQuery):
    try:
        user = users_table.get(Query().user_id == call.from_user.id)
        
        if not user:
            await call.answer("❌ Вы не зарегистрированы. Напишите /start", show_alert=True)
            return
        
        balance = user.get('balance', 0)
        username = user.get('username', 'Неизвестный')
        purchases = user.get('purchases', 0)
        
        text = (
            "Профиль\n"
            "——————————————————\n"
            f"Имя пользователя: @{username}\n"
            f"Идентификатор: {call.from_user.id}\n"
            "——————————————————\n"
            f"👛 Баланс: {format_price(balance)}\n"
            f"Покупок: {purchases}"
        )
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("[👜] Пополнить баланс", callback_data="top_up"))
        keyboard.add(InlineKeyboardButton("[🎫] Промокод", callback_data="promo_code"))
        keyboard.add(InlineKeyboardButton("[🗄] Перевести деньги", callback_data="transfer_money"))
        keyboard.add(InlineKeyboardButton("[⛓️] Реф программа", callback_data="referral"))
        keyboard.add(InlineKeyboardButton("[🦺] Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"))
        keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="main_menu"))
        await call.message.delete()
        await bot.send_photo(call.from_user.id, "https://iili.io/BZzNhN9.jpg", caption=text, reply_markup=keyboard)
        await call.answer()
    except Exception as e:
        logging.error(f"Profile error: {e}")
        await call.answer("❌ Ошибка загрузки профиля", show_alert=True)

# ==================== ПРОМОКОДЫ ====================
@dp.callback_query_handler(text="promo_code")
async def promo_code_start(call: CallbackQuery):
    await call.message.delete()
    await bot.send_message(call.from_user.id, "🎫 Введите промокод:")
    await PromoCode.waiting_promo.set()
    await call.answer()

@dp.message_handler(state=PromoCode.waiting_promo)
async def promo_code_check(message: Message, state: FSMContext):
    if await check_ban_and_answer(message):
        await state.finish()
        return
    
    code = message.text.strip().upper()
    promo = promocodes_table.get(Query().code == code)
    
    if not promo:
        await message.answer("❌ Промокод не найден.")
        await state.finish()
        return
    
    if promo.get("exhausted_at"):
        exhausted_time = datetime.fromisoformat(promo["exhausted_at"])
        if datetime.now() > exhausted_time + timedelta(minutes=5):
            promocodes_table.remove(Query().code == code)
            await message.answer("❌ Промокод уже недействителен.")
            await state.finish()
            return
        else:
            await message.answer("❌ Промокод уже использован.")
            await state.finish()
            return
    
    activations_left = promo.get("activations", 0)
    if activations_left <= 0:
        promocodes_table.update({"exhausted_at": datetime.now().isoformat()}, Query().code == code)
        await message.answer("❌ Промокод уже использован.")
        await state.finish()
        return
    
    amount = promo["amount"]
    user = get_user(message.from_user.id)
    new_balance = user["balance"] + amount
    update_balance(message.from_user.id, new_balance)
    
    new_activations = activations_left - 1
    if new_activations <= 0:
        promocodes_table.update({"activations": 0, "exhausted_at": datetime.now().isoformat()}, Query().code == code)
    else:
        promocodes_table.update({"activations": new_activations}, Query().code == code)
    
    await message.answer(f"✅ Промокод активирован! На баланс зачислено {format_price(amount)}.")
    await state.finish()

# ==================== ПЕРЕВОД ДЕНЕГ ====================
@dp.callback_query_handler(text="transfer_money")
async def transfer_money_start(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.delete()
    text = (
        "<b>Перевод средств</b>\n\n"
        f"Доступно для перевода: {format_price(user.get('balance', 0))}\n\n"
        "Отправьте Telegram ID получателя (число).\n"
        "Узнать свой ID можно в профиле."
    )
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML)
    await TransferMoney.waiting_user_id.set()
    await call.answer()

@dp.message_handler(state=TransferMoney.waiting_user_id)
async def transfer_money_user_id(message: Message, state: FSMContext):
    if await check_ban_and_answer(message):
        await state.finish()
        return
    
    try:
        target_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите корректный Telegram ID (число).")
        return
    
    if target_id == message.from_user.id:
        await message.answer("❌ Нельзя переводить деньги самому себе.")
        await state.finish()
        return
    
    target_user = get_user(target_id)
    if not target_user:
        await message.answer("❌ Пользователь с таким ID не найден в боте.")
        await state.finish()
        return
    
    await state.update_data(target_id=target_id)
    await message.answer(f"Введите сумму для перевода пользователю @{target_user.get('username', target_id)}:")
    await TransferMoney.waiting_amount.set()

@dp.message_handler(state=TransferMoney.waiting_amount)
async def transfer_money_amount(message: Message, state: FSMContext):
    if await check_ban_and_answer(message):
        await state.finish()
        return
    
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше нуля.")
            return
    except:
        await message.answer("❌ Введите корректную сумму.")
        return
    
    data = await state.get_data()
    target_id = data["target_id"]
    
    sender = get_user(message.from_user.id)
    receiver = get_user(target_id)
    
    if sender["balance"] < amount:
        await message.answer("❌ Недостаточно средств на балансе.")
        await state.finish()
        return
    
    update_balance(message.from_user.id, sender["balance"] - amount)
    update_balance(target_id, receiver["balance"] + amount)
    
    await message.answer(f"✅ Переведено {format_price(amount)} пользователю @{receiver.get('username', target_id)}.")
    
    try:
        await bot.send_message(
            target_id,
            f"💰 Вам перевели {format_price(amount)} от @{sender.get('username', message.from_user.id)}."
        )
    except:
        pass
    
    await state.finish()

# ==================== РЕФЕРАЛЬНАЯ ПРОГРАММА ====================
@dp.callback_query_handler(text="referral")
async def show_referral(call: CallbackQuery):
    ref_count = get_referral_count(call.from_user.id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{call.from_user.id}"
    
    text = (
        "<b>⛓️ Реферальная программа</b>\n\n"
        "<blockquote>Условия получения аккаунта: необходимо пригласить 15 уникальных пользователей. "
        "При условии, что один из приглашённых совершит покупку любого товара в боте, "
        "вы имеете право на получение любого аккаунта из ассортимента.</blockquote>\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"📪 Вы пригласили: <b>{ref_count}</b> чел."
    )
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="profile"))
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

# ==================== ПОПОЛНЕНИЕ ====================
@dp.callback_query_handler(text="top_up")
async def top_up_menu(call: CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        InlineKeyboardButton("[💳] СБП", callback_data="topup_sbp"),
        InlineKeyboardButton("[🔏] Crypto Bot", callback_data="topup_crypto")
    )
    keyboard.row(InlineKeyboardButton("[🪄] Звезды", callback_data="topup_stars"))
    keyboard.row(InlineKeyboardButton("[ ← Назад ]", callback_data="profile"))
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>Выберите способ пополнения:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="topup_sbp")
async def sbp_info(call: CallbackQuery):
    text = (
        "<b>Пополнение через СБП:</b>\n\n"
        "1. <b>Перевод по СБП</b>\n"
        "2. В поиске <b>ЮMoney</b>\n"
        "3. Номер: <code>+79164783786</code>\n"
        f"4. Комментарий: <code>{call.from_user.id}</code>\n"
        "5. Скрин чека в поддержку"
    )
    keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("[ ← Назад ]", callback_data="top_up"))
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="topup_crypto")
async def crypto_amount(call: CallbackQuery):
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите сумму в рублях:")
    await TopUp.waiting_crypto_amount.set()
    await call.answer()

@dp.message_handler(state=TopUp.waiting_crypto_amount)
async def crypto_invoice(message: Message, state: FSMContext):
    if await check_ban_and_answer(message):
        await state.finish()
        return
    try:
        amount = Decimal(message.text.replace(",", "."))
        if amount < 10:
            await message.answer("Минимальная сумма: 10 ₽")
            return
        await message.answer("⏳ Создаю счёт...")
        invoice_url, invoice_id = await create_crypto_invoice(amount)
        if invoice_url:
            pending_payments.insert({"user_id": message.from_user.id, "amount": float(amount), "invoice_id": invoice_id, "status": "pending", "created_at": datetime.now().isoformat()})
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("💳 Оплатить", url=invoice_url))
            keyboard.add(InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_crypto_{invoice_id}"))
            keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data="top_up"))
            await message.answer(f"<b>Счёт на {format_price(amount)} создан!</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await message.answer("❌ Не удалось создать счёт.")
    except:
        await message.answer("❌ Введите корректную сумму!")
    finally:
        await state.finish()

@dp.callback_query_handler(lambda call: call.data.startswith("check_crypto_"))
async def check_crypto_payment_handler(call: CallbackQuery):
    invoice_id = call.data.replace("check_crypto_", "")
    invoice_data = await check_crypto_payment(invoice_id)
    if invoice_data and invoice_data["status"] == "paid":
        payment = pending_payments.get(Query().invoice_id == invoice_id)
        if payment:
            user = get_user(payment["user_id"])
            if user:
                new_balance = user["balance"] + payment["amount"]
                update_balance(payment["user_id"], new_balance)
                await call.message.delete()
                await bot.send_message(call.from_user.id, f"✅ Баланс пополнен на {format_price(payment['amount'])}!")
                await call.answer("✅ Оплата прошла!")
                return
    await call.answer("❌ Оплата не прошла!", show_alert=True)

# ==================== STARS ====================
@dp.callback_query_handler(text="topup_stars")
async def stars_amount(call: CallbackQuery):
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        f"<b>Введите сумму в рублях для пополнения через Stars:</b>\n\n"
        f"⚠️ <b>ВАЖНО:</b> отправьте любое слово боту @{STARS_BOT_USERNAME}, "
        f"а затем введите сумму для пополнения.",
        parse_mode=ParseMode.HTML
    )
    await TopUp.waiting_stars_amount.set()
    await call.answer()

@dp.message_handler(state=TopUp.waiting_stars_amount)
async def stars_invoice(message: Message, state: FSMContext):
    if await check_ban_and_answer(message):
        await state.finish()
        return
    try:
        amount_rub = float(message.text)
        if amount_rub < 10:
            await message.answer("Минимальная сумма: 10 руб.")
            return
        amount_stars = int(amount_rub * 2)
        
        try:
            await second_bot.send_message(message.from_user.id, f"💫 Счёт на {amount_stars} Stars")
            await asyncio.sleep(0.3)
        except:
            pass
        
        await second_bot.send_invoice(
            chat_id=message.from_user.id,
            title="Пополнение баланса",
            description=f"Пополнение на {format_price(amount_rub)}",
            payload=f"topup_{message.from_user.id}_{amount_rub}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Пополнение", amount=amount_stars)]
        )
        
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🤖 Перейти к оплате", url=f"https://t.me/{STARS_BOT_USERNAME}"))
        
        await message.answer(
            f"⭐ Счёт на {amount_stars} Stars создан!\n\n"
            f"<b>Для оплаты:</b>\n"
            f"1. Нажмите кнопку ниже и перейдите в чат с ботом.\n"
            f"2. Нажмите <b>Оплатить</b> под сообщением со счётом.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await state.finish()

# ==================== ОБРАБОТЧИКИ ВТОРОГО БОТА ====================
@dp_second.pre_checkout_query_handler(lambda query: True)
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await second_bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp_second.message_handler(content_types=ContentTypes.SUCCESSFUL_PAYMENT)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split('_')
    user_id = int(parts[1])
    amount_rub = float(parts[2])
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "http://localhost:8080/topup",
                json={"user_id": user_id, "amount": amount_rub, "secret": API_SECRET}
            ) as resp:
                result = await resp.json()
                if result["status"] == "ok":
                    await message.answer("✅ Оплата получена! Баланс пополнен.")
                else:
                    await message.answer(f"❌ Ошибка: {result.get('message')}")
        except Exception as e:
            await message.answer(f"❌ Ошибка связи: {e}")

# ==================== ПОКУПКА ====================
@dp.callback_query_handler(text="buy_regular")
async def buy_regular(call: CallbackQuery):
    keyboard = get_country_keyboard_page("обычный", 0)
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>Покупка аккаунта</b>\n\nВыберите страну:", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="buy_aged")
async def buy_aged(call: CallbackQuery):
    accounts = accounts_table.search(Query().acc_type == "отлега")
    if not accounts:
        await call.answer("❌ Нет доступных!", show_alert=True)
        return
    keyboard = get_country_keyboard_page("отлега", 0)
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>Аккаунты с отлегой</b>\n\nВыберите страну:", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("aged_country_"))
async def aged_country_select(call: CallbackQuery):
    country_code = call.data.replace("aged_country_", "")
    keyboard = get_years_keyboard(country_code)
    if not keyboard.inline_keyboard:
        await call.answer("❌ Нет годов!", show_alert=True)
        return
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>Выберите год:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("country_"))
async def country_select(call: CallbackQuery):
    parts = call.data.split("_")
    acc_type = parts[1]
    country_code = parts[2]
    account = get_available_account(acc_type, country_code)
    if not account:
        await call.answer("❌ Нет доступных!", show_alert=True)
        keyboard = get_country_keyboard_page(acc_type, 0)
        try:
            await call.message.edit_text("<b>Выберите страну:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except:
            pass
        return
    
    # Показываем год для отлеги
    year_text = ""
    if acc_type == "отлега" and account.get("year"):
        year_text = f" {account.get('year')} года"
    
    text = (
        f"<b>{'Обычный аккаунт' if acc_type == 'обычный' else 'Аккаунт с отлегой'}{year_text}</b>\n\n"
        f"📍 Страна: {account['country_flag']} {account['country_name']}\n"
        f"💵 Цена: {format_price(account['price'])}"
    )
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💰 Купить", callback_data=f"purchase_{account.doc_id}"))
    keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data=f"buy_{acc_type}"))
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except:
        await call.message.delete()
        await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("year_"))
async def year_select(call: CallbackQuery):
    parts = call.data.split("_")
    country_code = parts[1]
    year = parts[2]
    account = get_available_account("отлега", country_code, year)
    if not account:
        await call.answer("❌ Нет доступных!", show_alert=True)
        keyboard = get_years_keyboard(country_code)
        try:
            await call.message.edit_text("<b>Выберите год:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except:
            pass
        return
    text = (
        f"<b>Аккаунт с отлегой {year} года</b>\n\n"
        f"📍 Страна: {account['country_flag']} {account['country_name']}\n"
        f"📅 Год: {year}\n"
        f"💵 Цена: {format_price(account['price'])}"
    )
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💰 Купить", callback_data=f"purchase_{account.doc_id}"))
    keyboard.add(InlineKeyboardButton("[ ← Назад ]", callback_data=f"aged_country_{country_code}"))
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except:
        await call.message.delete()
        await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda call: call.data.startswith("purchase_"))
async def purchase_account(call: CallbackQuery):
    account_id = int(call.data.replace("purchase_", ""))
    account = accounts_table.get(doc_id=account_id)
    user = get_user(call.from_user.id)
    if not account:
        await call.answer("❌ Не найден!", show_alert=True)
        return
    if user["balance"] < account["price"]:
        await call.answer("❌ Недостаточно средств!", show_alert=True)
        return
    new_balance = user["balance"] - account["price"]
    update_balance(call.from_user.id, new_balance)
    users_table.update({"purchases": user["purchases"] + 1}, Query().user_id == call.from_user.id)
    
    phone = account["phone"]
    price = account["price"]
    country_flag = account["country_flag"]
    country_name = account["country_name"]
    password = account.get("password")
    
    user_data = get_user(call.from_user.id)
    if user_data and user_data.get("referrer_id"):
        referrals_table.update(
            {"has_purchased": True},
            (Query().user_id == user_data["referrer_id"]) & (Query().invited_user_id == call.from_user.id)
        )
    
    orders_table.insert({"phone": phone, "buyer_id": call.from_user.id, "status": "ожидает_код", "created_at": datetime.now().isoformat()})
    accounts_table.remove(doc_ids=[account_id])
    sold_accounts_table.insert({**account, "buyer_id": call.from_user.id, "sold_at": datetime.now().isoformat()})
    await call.message.delete()
    
    result_text = (
        f"✅ <b>Аккаунт куплен!</b>\n\n"
        f"📱 Номер: <code>{phone}</code>\n"
        f"🌍 Страна: {country_flag} {country_name}\n"
        f"💰 Цена: {format_price(price)}\n\n"
        f"🔑 <b>Введите номер в Telegram.</b>\n"
        f"Код подтверждения придёт автоматически."
    )
    if password:
        result_text += f"\n\n🔐 <b>Облачный пароль:</b> <code>{password}</code>"
    
    await bot.send_message(call.from_user.id, result_text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(call.from_user.id))
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"💰 Продан {phone}\n👤 @{call.from_user.username}\n💵 {format_price(price)}")
        except:
            pass
    await call.answer("✅ Куплен!", show_alert=True)

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query_handler(text="admin_panel")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа!", show_alert=True)
        return
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>🛠 Админ-панель</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query_handler(text="admin_add")
async def admin_add_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.delete()
    await bot.send_message(call.from_user.id, "<b>➕ Добавление аккаунта</b>\n\nВведите страну с флагом (например: 🇺🇸США):", parse_mode=ParseMode.HTML)
    await AddAccount.waiting_country.set()
    await call.answer()

@dp.message_handler(state=AddAccount.waiting_country)
async def admin_add_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("Обычный", callback_data="type_обычный"), InlineKeyboardButton("Отлега", callback_data="type_отлега"))
    await message.answer("Выберите тип:", reply_markup=keyboard)
    await AddAccount.waiting_type.set()

@dp.callback_query_handler(lambda call: call.data.startswith("type_"), state=AddAccount.waiting_type)
async def admin_add_type(call: CallbackQuery, state: FSMContext):
    acc_type = call.data.replace("type_", "")
    await state.update_data(acc_type=acc_type)
    if acc_type == "отлега":
        await call.message.delete()
        await bot.send_message(call.from_user.id, "Введите год регистрации:")
        await AddAccount.waiting_year.set()
    else:
        await call.message.delete()
        await bot.send_message(call.from_user.id, "Введите цену в рублях:")
        await AddAccount.waiting_price.set()
    await call.answer()

@dp.message_handler(state=AddAccount.waiting_year)
async def admin_add_year(message: Message, state: FSMContext):
    try:
        year = int(message.text)
        await state.update_data(year=year)
        await message.answer("Введите цену в рублях:")
        await AddAccount.waiting_price.set()
    except:
        await message.answer("❌ Введите год цифрами!")

@dp.message_handler(state=AddAccount.waiting_price)
async def admin_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
        await state.update_data(price=price)
        await message.answer("Введите номер телефона:")
        await AddAccount.waiting_phone.set()
    except:
        await message.answer("❌ Введите цену цифрами!")

@dp.message_handler(state=AddAccount.waiting_phone)
async def admin_add_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text
    country_input = data["country"]
    if len(country_input) >= 2 and ord(country_input[0]) > 127:
        flag = country_input[:2]
        name = country_input[2:].strip()
    else:
        flag = ""
        name = country_input
    await state.update_data(flag=flag, name=name, phone=phone)
    await message.answer("Введите облачный пароль (или напишите <b>нет</b> если его нет):", parse_mode=ParseMode.HTML)
    await AddAccount.waiting_password.set()

@dp.message_handler(state=AddAccount.waiting_password)
async def admin_add_password(message: Message, state: FSMContext):
    password = message.text.strip()
    if password.lower() == "нет":
        password = None
    
    data = await state.get_data()
    
    account_data = {
        "country_name": data["name"],
        "country_flag": data["flag"],
        "country_code": data["name"],
        "acc_type": data["acc_type"],
        "price": data["price"],
        "phone": data["phone"],
        "password": password,
        "has_session": False,
        "added_at": datetime.now().isoformat(),
        "year": data.get("year")
    }
    
    acc_id = accounts_table.insert(account_data)
    client = await create_userbot_for_account(data["phone"], acc_id)
    
    if client:
        await message.answer(
            f"📱 Номер: {data['phone']}\n🔐 <b>Введи код подтверждения:</b>",
            parse_mode=ParseMode.HTML
        )
        await state.update_data(phone=data["phone"], acc_id=acc_id)
        await AddAccount.waiting_code.set()
    else:
        await message.answer("❌ Ошибка создания юзербота")
        await state.finish()

@dp.message_handler(state=AddAccount.waiting_code)
async def admin_add_code(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    code = message.text
    success = await login_account_userbot(phone, code)
    if success:
        await message.answer(
            f"✅ Аккаунт {phone} готов!\n"
            f"🏳️ Страна: {data.get('flag', '')} {data.get('name', '')}\n"
            f"📦 Тип: {data.get('acc_type', '')}\n"
            f"💵 Цена: {format_price(data.get('price', 0))}"
            + (f"\n🔐 Пароль: {data.get('password', 'нет')}" if data.get("password") else "")
        )
    else:
        await message.answer("❌ Неверный код или нужен облачный пароль! Проверьте пароль или отправьте /pass")
        return
    await state.finish()

@dp.callback_query_handler(text="admin_stock")
async def admin_stock(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    accounts = accounts_table.all()
    text = "<b>📊 Остатки:</b>\n\n"
    if not accounts:
        text += "Нет доступных."
    else:
        for country in set(a["country_name"] for a in accounts):
            flag_data = accounts_table.get(Query().country_name == country)
            flag = flag_data["country_flag"] if flag_data else ""
            regular = len(accounts_table.search((Query().country_name == country) & (Query().acc_type == "обычный")))
            aged = len(accounts_table.search((Query().country_name == country) & (Query().acc_type == "отлега")))
            text += f"{flag} {country}: {regular} обычных, {aged} с отлегой\n"
    keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("[ ← Назад ]", callback_data="admin_panel"))
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="admin_broadcast")
async def admin_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите текст для рассылки:")
    await Broadcast.waiting_text.set()
    await call.answer()

@dp.message_handler(state=Broadcast.waiting_text, content_types=ContentTypes.ANY)
async def broadcast_send(message: Message, state: FSMContext):
    users = users_table.all()
    success = 0
    failed = 0
    for user in users:
        try:
            await message.copy_to(user["user_id"])
            success += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Рассылка завершена!\nУспешно: {success}\nНеудачно: {failed}")
    await state.finish()

@dp.callback_query_handler(text="admin_topup_user")
async def admin_topup_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите ID и сумму:\n<code>123456789 100</code>", parse_mode=ParseMode.HTML)
    
    @dp.message_handler(lambda msg: is_admin(msg.from_user.id) and len(msg.text.split()) == 2)
    async def process_topup(msg: Message):
        try:
            parts = msg.text.split()
            user_id = int(parts[0])
            amount = float(parts[1])
            user = get_user(user_id)
            if user:
                new_balance = user["balance"] + amount
                update_balance(user_id, new_balance)
                await msg.answer(f"✅ Баланс {user_id} пополнен на {format_price(amount)}")
            else:
                await msg.answer("❌ Не найден!")
        except:
            await msg.answer("❌ Неверный формат!")
    await call.answer()

# ==================== АДМИН: РЕФЕРАЛЫ ====================
@dp.callback_query_handler(text="admin_referrals")
async def admin_referrals(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    all_refs = referrals_table.all()
    if not all_refs:
        await call.answer("Нет рефералов", show_alert=True)
        return
    
    stats = {}
    for ref in all_refs:
        uid = ref["user_id"]
        if uid not in stats:
            stats[uid] = {"total": 0, "purchased": 0}
        stats[uid]["total"] += 1
        if ref.get("has_purchased"):
            stats[uid]["purchased"] += 1
    
    text = "<b>📋 Статистика рефералов:</b>\n\n"
    for uid, data in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
        user = get_user(uid)
        username = user["username"] if user else "Неизвестный"
        text += f"@{username} (ID: {uid})\nПригласил: {data['total']} | Купили: {data['purchased']}\n\n"
    
    keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("[ ← Назад ]", callback_data="admin_panel"))
    await call.message.delete()
    await bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await call.answer()

# ==================== АДМИН: БАН ====================
@dp.callback_query_handler(text="admin_ban")
async def admin_ban_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    await call.message.delete()
    await bot.send_message(call.from_user.id, "Введите ID или @username для бана:")
    await BanUser.waiting_ban_id.set()
    await call.answer()

@dp.message_handler(state=BanUser.waiting_ban_id)
async def admin_ban_user(message: Message, state: FSMContext):
    target = message.text.strip()
    
    user = None
    if target.startswith("@"):
        user = users_table.get(Query().username == target[1:])
    else:
        try:
            uid = int(target)
            user = get_user(uid)
        except:
            pass
    
    if not user:
        await message.answer("❌ Пользователь не найден в базе.")
        await state.finish()
        return
    
    uid = user["user_id"]
    
    if not is_banned(uid):
        banned_table.insert({"user_id": uid, "banned_at": datetime.now().isoformat()})
        await message.answer(f"🚫 Пользователь {uid} забанен.")
        try:
            await bot.send_message(uid, "🚫 Ваш тикет закрыт.")
        except:
            pass
    else:
        banned_table.remove(Query().user_id == uid)
        await message.answer(f"✅ Пользователь {uid} разбанен.")
    
    await state.finish()

# ==================== АДМИН: ПРОМОКОДЫ ====================
@dp.callback_query_handler(text="admin_promo")
async def admin_promo_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    
    await call.message.delete()
    await bot.send_message(
        call.from_user.id,
        "🎫 <b>Создание промокода</b>\n\nВведите данные в формате:\n<code>КОД СУММА АКТИВАЦИИ</code>\n\nПример: <code>START50 50 10</code>",
        parse_mode=ParseMode.HTML
    )
    
    @dp.message_handler(lambda msg: is_admin(msg.from_user.id) and len(msg.text.split()) == 3)
    async def process_promo(msg: Message):
        try:
            parts = msg.text.split()
            code = parts[0].upper()
            amount = float(parts[1])
            activations = int(parts[2])
            
            existing = promocodes_table.get(Query().code == code)
            if existing:
                await msg.answer("❌ Промокод с таким названием уже существует.")
                return
            
            promocodes_table.insert({
                "code": code,
                "amount": amount,
                "activations": activations,
                "created_at": datetime.now().isoformat(),
                "exhausted_at": None
            })
            
            await msg.answer(
                f"✅ Промокод <b>{code}</b> создан!\n"
                f"💰 Сумма: {format_price(amount)}\n"
                f"👥 Активаций: {activations}",
                parse_mode=ParseMode.HTML
            )
        except:
            await msg.answer("❌ Неверный формат!")
    
    await call.answer()

# ==================== ЗАПУСК ====================
async def on_startup(dp):
    logging.info("Запуск...")
    success = await start_main_userbot()
    if success:
        logging.info("Юзербот авторизован")
    else:
        logging.info("Ожидание кода...")
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    logging.info("HTTP сервер запущен на порту 8080")

async def main():
    await on_startup(dp)
    await asyncio.gather(
        dp.start_polling(),
        dp_second.start_polling()
    )

if __name__ == '__main__':
    asyncio.run(main())