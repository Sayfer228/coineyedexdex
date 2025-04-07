import asyncio
import aiohttp
import json
import logging
import time
import os
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime
from aiogram.enums import ParseMode


# Константи
TELEGRAM_BOT_TOKEN = "7708249697:AAFaUcc2mvAi3afWEKHDbOfmHD3oWmX6-T8"
USER_CHAT_ID = "802714713"
MIN_DIFF_PERCENT = 1
MIN_LIQUIDITY = 10_000
last_alert_time = 0  # 🕒 змінна для відстеження часу останнього алерту

TOKENS_FILE = Path("tokens.json")
ALERTS_FILE = Path("alerts.json")
monitor_tasks: dict[str, asyncio.Task] = {}
TOKENS: dict[int, list] = {}
AUTHORIZED_USERS = []
PASSWORD = 'f9a2DUFe63'

# ✅ Форсуємо конфігурацію логів — навіть якщо щось її вже налаштувало
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    force=True
)

# ✅ Основний логер для всього проєкту
logger = logging.getLogger("coin-eye")

logger.debug("✅ DEBUG працює!")




# FSM Стан для додавання токена по адресах
class AddTokenFSM(StatesGroup):
    waiting_for_bsc = State()
    waiting_for_bsc_pool = State()
    waiting_for_eth = State()
    waiting_for_eth_pool = State()

class AddTokenStates(StatesGroup):
    waiting_for_first_address = State()
    waiting_for_first_pool = State()        # ✅ додаємо цей стан
    waiting_for_second_address = State()
    waiting_for_second_pool = State()       # ✅ і цей стан

class EditTokenStates(StatesGroup):
    waiting_for_threshold = State()

class EditThreshold(StatesGroup):
    waiting_for_value = State()

class AuthState(StatesGroup):
    waiting_for_password = State()

TOKENS_FILE = Path("tokens.json")



# Ініціалізація бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def is_solana_address(address: str) -> bool:
    return len(address) >= 32 and not address.lower().startswith("0x")


def load_alerts() -> list:
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


@dp.callback_query(AddTokenStates.waiting_for_first_pool)
async def select_first_pool(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pools = data.get("first_pools", [])
    idx = int(callback.data.replace("select_first_", ""))
    if idx >= len(pools):
        await callback.answer("❌ Невірний вибір.")
        return

    selected_pool = pools[idx]
    await state.update_data(selected_first_pool=selected_pool)

    # Виводимо пули для другої адреси
    await callback.message.edit_text(
    f"✅ Обрано пул у першій мережі: <b>{selected_pool['baseToken']['symbol']}/{selected_pool['quoteToken']['symbol']}</b>\n\n"
    f"🔹 Тепер введи <b>адресу токена</b> в другій мережі:",
    parse_mode="HTML"
)
    await state.set_state(AddTokenStates.waiting_for_second_address)


@dp.callback_query(AddTokenStates.waiting_for_second_pool)
async def select_second_pool(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pools = data.get("second_pools", [])
    idx = int(callback.data.replace("select_second_", ""))
    if idx >= len(pools):
        await callback.answer("❌ Невірний вибір.")
        return

    selected_first = data.get("selected_first_pool")
    selected_second = pools[idx]

    symbol = selected_first["baseToken"]["symbol"]

    token_data = {
        "symbol": symbol,
        "networks": {
            selected_first["chainId"]: selected_first["pairAddress"],
            selected_second["chainId"]: selected_second["pairAddress"]
        },
        "active": True
    }

    user_id = callback.from_user.id
    TOKENS.setdefault(user_id, []).append(token_data)
    save_tokens(user_id, TOKENS[user_id])
    await start_monitoring_for_token(user_id, token_data)

    await callback.message.edit_text(
        f"✅ Токен <b>{symbol}</b> додано для моніторингу між "
        f"<b>{selected_first['chainId'].upper()}</b> та <b>{selected_second['chainId'].upper()}</b>!",
        parse_mode="HTML"
    )
    await state.clear()


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Додати токен")],
            [KeyboardButton(text="✏️ Редагувати токени")],
            [KeyboardButton(text="📋 Список токенів")],
            [KeyboardButton(text="📜 Історія сповіщень")]
        ],
        resize_keyboard=True
    )


@dp.callback_query(lambda c: c.data == "back_to_tokens")
async def back_to_tokens(callback: CallbackQuery, state: FSMContext):
    await list_tokens(callback.message, state)


@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id in AUTHORIZED_USERS:
        # Якщо користувач вже авторизований
        TOKENS[user_id] = load_tokens(user_id)  # Завантажуємо токени користувача
        await message.answer("👁‍🗨 CoinEye запущено. Обери дію:", reply_markup=main_menu())
    else:
        # Якщо користувач не авторизований, запитуємо пароль
        await message.answer("🔐 Для доступу до бота введіть пароль.")
        await state.set_state(AuthState.waiting_for_password)



def back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back")]
    ])

@dp.message(lambda msg: msg.text == "➕ Додати токен")
async def add_token_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔹 Введи адресу токена в першій мережі (наприклад, BSC/SOL):", reply_markup=back_button())
    await state.set_state(AddTokenStates.waiting_for_first_address)  # ✅ правильний стан

async def get_pools(url: str, session: aiohttp.ClientSession) -> list:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                return []
            data = await resp.json()
            return [p for p in data.get("pairs", []) if p.get("liquidity", {}).get("usd", 0) > MIN_LIQUIDITY]
    except Exception as e:
        logger.exception(f"[ERROR] Помилка при запиті до {url}: {e}")
        return []

# Визначимо функцію для перевірки авторизації користувача
@dp.message(AuthState.waiting_for_password)
async def password_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if message.text == PASSWORD:
        # Якщо пароль правильний
        AUTHORIZED_USERS.append(user_id)  # Додаємо користувача в список авторизованих
        TOKENS[user_id] = load_tokens(user_id)  # Завантажуємо токени користувача
        await message.answer("✅ Ви успішно авторизовані! Ви можете почати користуватися ботом.")
        await message.answer("👁‍🗨 CoinEye запущено. Обери дію:", reply_markup=main_menu())
        await state.clear()  # Очищаємо стан FSM
    else:
        # Якщо пароль неправильний
        await message.answer("❌ Невірний пароль. Спробуйте ще раз.")




@dp.message(AddTokenStates.waiting_for_first_address)
async def handle_first_address(message: Message, state: FSMContext):
    address = message.text.strip()
    await state.update_data(first_address=address)
    url = f"https://api.dexscreener.com/latest/dex/search?q={address}"

    if is_solana_address(address):
        logger.debug(f"[Solana] Обробляється Solana токен: {address}")
        await bot.send_message(message.chat.id, "🧬 Схоже, це токен у мережі Solana.")

    # Викликаємо get_pools для отримання пулів
    async with aiohttp.ClientSession() as session:
        pools = await get_pools(url, session)  # Використовуємо нашу функцію get_pools

    if not pools:
        await bot.send_message(message.chat.id, "❌ Не знайдено пулів з ліквідністю > $10,000. Введи іншу адресу або натисни ⬅️ Назад.")
        return

    await state.update_data(first_pools=pools)

    kb = InlineKeyboardBuilder()
    for i, p in enumerate(pools[:10]):
        symbol = p["baseToken"]["symbol"]
        quote = p["quoteToken"]["symbol"]
        price = float(p["priceUsd"])
        kb.add(InlineKeyboardButton(
            text=f"{symbol}/{quote} (${price:.4f})",
            callback_data=f"select_first_{i}"
        ))

    await bot.send_message(message.chat.id, "🔽 Обери пул для першої мережі:", reply_markup=kb.adjust(1).as_markup())
    await state.set_state(AddTokenStates.waiting_for_first_pool)




@dp.callback_query(AddTokenStates.waiting_for_first_pool)
async def handle_first_pool(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pools = data.get("first_pools", [])

    try:
        idx = int(callback.data.replace("select_first_", ""))
        selected_pool = pools[idx]
    except (ValueError, IndexError):
        await callback.answer("⚠️ Некоректний вибір.")
        return

    await state.update_data(first_selected=selected_pool)
    await callback.message.edit_text(
        f"✅ Обрано пул у першій мережі: <b>{selected_pool['baseToken']['symbol']}/{selected_pool['quoteToken']['symbol']}</b>",
        parse_mode="HTML"
    )

    # ⬇️ Ось це правильне завершення
    await bot.send_message(callback.from_user.id, "🔹 Тепер введи адресу токена в другій мережі:")
    await state.set_state(AddTokenStates.waiting_for_second_address)



@dp.message(AddTokenStates.waiting_for_second_address)
async def handle_second_address(message: Message, state: FSMContext):
    address = message.text.strip()
    await state.update_data(second_address=address)
    url = f"https://api.dexscreener.com/latest/dex/search?q={address}"


    if is_solana_address(address):
        logger.debug(f"[Solana] Обробляється Solana токен: {address}")
        await message.answer("🧬 Схоже, це токен у мережі Solana.")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                await message.answer("❌ Сталася помилка при отриманні даних. Спробуйте ще раз.")
                return
            data = await resp.json()

    pools = [
        p for p in data.get("pairs", [])
        if p.get("liquidity", {}).get("usd", 0) > 10_000
    ]

    if not pools:
        await message.answer("❌ Не знайдено пулів з ліквідністю > $10,000. Введи іншу адресу або натисни ⬅️ Назад.")
        return

    await state.update_data(second_pools=pools)

    kb = InlineKeyboardBuilder()
    for i, p in enumerate(pools[:10]):
        symbol = p["baseToken"]["symbol"]
        quote = p["quoteToken"]["symbol"]
        price = float(p["priceUsd"])
        kb.add(InlineKeyboardButton(
            text=f"{symbol}/{quote} (${price:.4f})",
            callback_data=f"select_second_{i}"
        ))

    await message.answer("🔽 Обери пул для другої мережі:", reply_markup=kb.adjust(1).as_markup())
    await state.set_state(AddTokenStates.waiting_for_second_pool)


@dp.callback_query(AddTokenStates.waiting_for_second_pool)
async def handle_second_pool(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pools = data.get("second_pools", [])

    try:
        idx = int(callback.data.replace("select_second_", ""))
        second_pool = pools[idx]
    except (ValueError, IndexError):
        await callback.answer("⚠️ Некоректний вибір.")
        return

    first_pool = data.get("first_selected")

    symbol = first_pool["baseToken"]["symbol"]
    first_chain = first_pool["chainId"]
    second_chain = second_pool["chainId"]
    first_token_address = first_pool["baseToken"]["address"]
    second_token_address = second_pool["baseToken"]["address"]

    token_data = {
        "symbol": symbol,
        "addresses": {
            first_chain: first_token_address,
            second_chain: second_token_address
        },
        "active": True,
        "threshold": MIN_DIFF_PERCENT
    }

    user_id = callback.from_user.id
    TOKENS.setdefault(user_id, []).append(token_data)
    save_tokens(user_id, TOKENS[user_id])
    await start_monitoring_for_token(user_id, token_data)

    await callback.message.edit_text(
        f"✅ Токен <b>{symbol}</b> додано для моніторингу між "
        f"<b>{first_chain.upper()}</b> та <b>{second_chain.upper()}</b>!",
        parse_mode="HTML"
    )
    await state.clear()



async def show_pools_for_addresses(state: FSMContext, message: Message):
    data = await state.get_data()
    addr1 = data["first_address"]
    addr2 = data["second_address"]

    pools_by_address = {}

    async with aiohttp.ClientSession() as session:
        for i, addr in enumerate([addr1, addr2]):
            url = f"https://api.dexscreener.com/latest/dex/search?q={addr}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                    await message.answer("❌ Сталася помилка при отриманні даних. Спробуйте ще раз.")
                    return
                data = await resp.json()

            pools = [p for p in data.get("pairs", []) if float(p.get("liquidity", {}).get("usd", 0)) > 10000]
            key = "first_pools" if i == 0 else "second_pools"
            pools_by_address[key] = pools

    if not pools_by_address["first_pools"] or not pools_by_address["second_pools"]:
        await message.answer("❌ Не знайдено пулів з ліквідністю > $10,000 для однієї з адрес.")
        await state.clear()
        return

    await state.update_data(**pools_by_address)

    # Виводимо пули для першої адреси
    kb = InlineKeyboardBuilder()
    text = "🔽 Обери пул для першої мережі:\n\n"

    for i, p in enumerate(pools_by_address["first_pools"][:10]):
        text += (
            f"{i + 1}. 🌐 {p['chainId'].upper()} | {p['dexId'].capitalize()}\n"
            f"💵 ${float(p['priceUsd']):.4f} | 💧 ${int(float(p['liquidity']['usd'])):,}\n\n"
        )
        kb.add(InlineKeyboardButton(text=f"Обрати #{i + 1}", callback_data=f"select_first_{i}"))

    await message.answer(text, reply_markup=kb.adjust(1).as_markup())
    await state.set_state(AddTokenStates.waiting_for_first_pool)

def save_alert_to_history(user_id: int, symbol: str, prices: dict, diff: float):
    path = get_user_alerts_path(user_id)
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "prices": prices,
        "diff": round(diff, 2)
    }
    history = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
    history.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history[-50:], f, indent=2, ensure_ascii=False)

@dp.message(lambda msg: msg.text == "📋 Список токенів")
async def list_tokens(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_tokens = load_tokens(user_id)
    TOKENS[user_id] = user_tokens  # оновити глобальний кеш

    if not user_tokens:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back"))
        await message.answer("📭 Список токенів порожній.", reply_markup=kb.as_markup())
        return

    kb = InlineKeyboardBuilder()
    for idx, token in enumerate(user_tokens):
        symbol = token.get("symbol") or token.get("name")
        chains = list(token.get("addresses", {}).keys())
        active = "🟢" if token.get("active", True) else "🔴"
        kb.row(InlineKeyboardButton(  # ← ЗМІНА ТУТ
            text=f"{active} {symbol.upper()} ({', '.join(chains)})",
            callback_data=f"manage_{idx}"
        ))

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back"))
    await message.answer("📋 Обери токен:", reply_markup=kb.as_markup())

async def toggle_token_monitoring(user_id: int, token: dict):
    """
    Перемикає моніторинг токена: включає або вимикає його.
    """
    symbol = token["symbol"]
    task_key = f"{user_id}:{symbol}"

    if token.get("active", True):  # Токен активний
        logger.info(f"Вимикаємо моніторинг для {symbol}.")
        token["active"] = False  # Вимикаємо моніторинг

        # Зупиняємо завдання моніторингу
        if task_key in monitor_tasks:
            logger.info(f"Зупиняємо моніторинг для {symbol}. Завдання буде скасовано.")
            monitor_tasks[task_key].cancel()
            del monitor_tasks[task_key]
        else:
            logger.warning(f"Немає активного моніторингу для токена {symbol}.")
    else:  # Токен неактивний
        logger.info(f"Вмикаємо моніторинг для {symbol}.")
        token["active"] = True  # Вмикаємо моніторинг
        await start_monitoring_for_token(user_id, token)

    # Зберігаємо оновлені дані токенів
    save_tokens(user_id, TOKENS[user_id])



@dp.message(lambda msg: msg.text == "✏️ Редагувати токени")
async def edit_tokens(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    tokens = TOKENS.get(user_id, [])

    if not tokens:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back"))
        await message.answer("📭 Список токенів порожній.", reply_markup=kb.as_markup())
        return

    kb = InlineKeyboardBuilder()
    for idx, token in enumerate(tokens):
        symbol = token.get("symbol") or token.get("name")
        chains = list(token.get("addresses", {}).keys())
        kb.add(InlineKeyboardButton(
            text=f"🗑 {symbol.upper()} ({', '.join(chains)})",
            callback_data=f"delete_{idx}"
        ))

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back"))
    await message.answer("🧹 Обери токен для видалення:", reply_markup=kb.as_markup())




@dp.callback_query(lambda c: c.data.startswith("manage_"))
async def manage_token(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    idx = int(callback.data.replace("manage_", ""))

    user_tokens = TOKENS.get(user_id, [])
    if idx >= len(user_tokens):
        await callback.answer("❌ Невірний індекс токена.")
        return

    token = user_tokens[idx]
    symbol = token["symbol"]

    await state.update_data(prev_menu="tokens", editing_idx=idx)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔧 Змінити поріг", callback_data=f"edit_threshold_{idx}"))
    kb.row(InlineKeyboardButton(text="⏯ Вкл/Викл моніторинг", callback_data=f"toggle_{idx}"))
    kb.row(InlineKeyboardButton(text="❌ Видалити токен", callback_data=f"delete_{idx}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="go_back"))

    await callback.message.edit_text(
        f"🔍 <b>{symbol.upper()}</b> — обери дію:",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


async def set_threshold(message: Message, state: FSMContext):
    try:
        value = float(message.text.strip().replace(",", "."))
        if not 0 < value < 100:
            raise ValueError("Поріг має бути в діапазоні від 0 до 100.")

        data = await state.get_data()
        idx = data["edit_idx"]
        token = TOKENS[data["user_id"]][idx]  # Отримуємо токен із глобального кешу

        # Якщо поріг змінився, зупиняємо старе завдання моніторингу і запускаємо нове
        old_threshold = token["threshold"]
        if value != old_threshold:
            logger.info(f"Змінюємо поріг для токена {token['symbol']} з {old_threshold}% на {value}%")
            token["threshold"] = value  # Оновлюємо поріг

            save_tokens(data["user_id"], TOKENS[data["user_id"]])  # Зберігаємо новий поріг у файл

            # Зупиняємо старе завдання моніторингу, якщо воно є
            task_key = f"{data['user_id']}:{token['symbol']}"
            if task_key in monitor_tasks:
                monitor_tasks[task_key].cancel()
                del monitor_tasks[task_key]  # Видаляємо старе завдання

            # Запускаємо нове завдання моніторингу з оновленим порогом
            await start_monitoring_for_token(data['user_id'], token)

        symbol = token["symbol"]
        await message.answer(f"✅ Новий поріг для <b>{symbol.upper()}</b>: {value:.2f}%", parse_mode="HTML")
        await list_tokens(message, state)

    except ValueError as e:
        await message.answer(f"⚠️ Помилка: {e}")



async def add_token_to_monitoring(user_id: int, token_data: dict):
    logger.info(f"Додавання токена для моніторингу: {token_data['symbol']}")
    TOKENS.setdefault(user_id, []).append(token_data)
    save_tokens(user_id, TOKENS[user_id])
    await start_monitoring_for_token(user_id, token_data)


@dp.callback_query(lambda c: c.data.startswith("edit_threshold_"))
async def start_edit_threshold(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.replace("edit_threshold_", ""))
    await state.update_data(editing_idx=idx)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await callback.message.answer("✏️ Введи новий поріг у відсотках (наприклад, 1.5):", reply_markup=kb)
    await state.set_state(EditThreshold.waiting_for_value)


@dp.callback_query(lambda c: c.data == "go_back")
async def go_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("👁 CoinEye чекає твоєї команди. Обирай:", reply_markup=main_menu())


@dp.message(EditThreshold.waiting_for_value)
async def save_new_threshold(message: Message, state: FSMContext):
    user_id = message.from_user.id
    tokens = TOKENS.get(user_id, [])

    if message.text.strip() == "⬅️ Назад":
        await state.clear()
        await message.answer("👁 CoinEye чекає твоєї команди. Обирай:", reply_markup=main_menu())
        return

    try:
        percent = float(message.text.strip().replace(",", "."))
        if percent <= 0 or percent > 100:
            raise ValueError

        data = await state.get_data()
        idx = data.get("editing_idx")
        token = tokens[idx]
        token["threshold"] = percent

        save_tokens(user_id, tokens)
        TOKENS[user_id] = tokens

        # Зупиняємо старий таск моніторингу (якщо існує)
        symbol = token["symbol"]
        task_key = f"{user_id}:{symbol}"
        if task_key in monitor_tasks:
            monitor_tasks[task_key].cancel()
            del monitor_tasks[task_key]

        # Запускаємо новий з оновленим порогом
        await start_monitoring_for_token(user_id, token)

        await message.answer(
            f"✅ Новий поріг для <b>{symbol.upper()}</b>: <b>{percent}%</b>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        await state.clear()

    except ValueError:
        await message.answer("⚠️ Введи коректне число від 0 до 100 (наприклад, 1.5):")



@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_token(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    idx = int(callback.data.replace("delete_", ""))

    try:
        tokens = TOKENS.get(user_id, [])
        if idx >= len(tokens):
            await callback.answer("⚠️ Токен не знайдено.")
            return

        deleted = tokens.pop(idx)
        symbol = deleted.get("name") or deleted.get("symbol")
        task_key = f"{user_id}:{symbol}"

        # 🛑 Зупиняємо моніторинг, якщо активний
        if task_key in monitor_tasks:
            monitor_tasks[task_key].cancel()
            del monitor_tasks[task_key]

        # Зберігаємо оновлений список токенів
        save_tokens(user_id, tokens)
        TOKENS[user_id] = tokens  # оновлюємо кеш

        await callback.message.edit_text(
            f"❌ Токен <b>{symbol.upper()}</b> видалено.",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.exception(f"[ERROR] delete_token: {e}")
        await callback.answer("⚠️ Помилка при видаленні.")




MIN_DIFF_PERCENT = 1  # Мінімальна різниця в % для сповіщення
MIN_LIQUIDITY = 10_000

import time  # У верхній частині файлу, якщо ще не імпортовано

async def monitor_token_by_address(user_id: int, token: dict, session: aiohttp.ClientSession, bot: Bot):
    symbol = token["symbol"]
    addresses = token["addresses"]
    last_alert_time = 0

    # Якщо токен не активний — припиняємо моніторинг
    if not token.get("active", True):
        logger.info(f"[MONITORING] [{user_id}] {symbol} неактивний. Пропускаємо моніторинг.")
        return

    logger.info(f"[MONITORING] [{user_id}] {symbol} → {list(addresses.keys())} | ACTIVE")

    while token.get("active", True):  # Потрібно перевіряти активність токена кожного разу
        prices = {}
        now = time.time()

        for chain, address in addresses.items():
            url = f"https://api.dexscreener.com/latest/dex/search?q={address}"
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                        await bot.send_message(user_id, "❌ Сталася помилка при отриманні даних. Спробуйте ще раз.")
                        return
                    data = await resp.json()

                best_price = None
                max_liquidity = 0
                for p in data.get("pairs", []):
                    if p["chainId"].lower().startswith(chain.lower()):
                        liquidity = float(p.get("liquidity", {}).get("usd", 0))
                        price = float(p.get("priceUsd", 0))
                        if liquidity > MIN_LIQUIDITY and liquidity > max_liquidity:
                            max_liquidity = liquidity
                            best_price = price

                if best_price:
                    prices[chain] = best_price

            except Exception:
                logger.exception(f"[ERROR monitor_by_address {symbol}]")

        if len(prices) >= 2:
            v = list(prices.values())
            diff = abs(v[0] - v[1]) / ((v[0] + v[1]) / 2) * 100
            current_threshold = token.get("threshold", MIN_DIFF_PERCENT)

            logger.debug(f"[MONITORING] Різниця цін для {symbol}: {diff:.2f}% | Поріг: {current_threshold}%")

            if diff >= current_threshold and now - last_alert_time >= 60:
                msg = (
                    f"⚠️ <b>{symbol.upper()}</b> має різницю цін між мережами!\n\n" +
                    "\n".join([f"🌐 {c.upper()}: ${prices[c]:.4f}" for c in prices]) +
                    f"\n\n📊 Різниця: <b>{diff:.2f}%</b>"
                )
                await bot.send_message(user_id, msg)
                save_alert_to_history(user_id, symbol, prices, diff)
                last_alert_time = now

        await asyncio.sleep(5)



async def start_monitoring_for_token(user_id: int, token: dict):
    """
    Запускає моніторинг для конкретного токена.
    """
    symbol = token.get("symbol") or token.get("name")

    if "addresses" in token:
        task_key = f"{user_id}:{symbol}"

        # Якщо завдання вже є, то не запускаємо нове
        if task_key in monitor_tasks:
            logger.info(f"Завдання моніторингу для токена {symbol} вже активне.")
            return

        # Створюємо завдання для моніторингу
        task = asyncio.create_task(monitor_token_by_address(user_id, token, session, bot))
        monitor_tasks[task_key] = task  # Додаємо завдання до словника
    else:
        logger.warning(f"Пропущено токен без адреси для моніторингу: {symbol}")


import time

async def monitor_token(user_id: int, token: dict, session: aiohttp.ClientSession, bot: Bot):
    symbol = token.get("name") or token.get("symbol")
    chains = token.get("chains")
    last_alert_time = 0  # Встановлюємо початковий час останнього сповіщення на 0

    logger.info(f"[MONITORING] [{user_id}] {symbol} → {chains} | ACTIVE")

    while token.get("active", True):
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                    await bot.send_message(user_id, "❌ Сталася помилка при отриманні даних. Спробуйте ще раз.")
                    return
                data = await resp.json()

            best_prices = {}
            for pair in data.get("pairs", []):
                chain = pair.get("chainId")
                liquidity = float(pair.get("liquidity", {}).get("usd", 0))
                price = float(pair.get("priceUsd", 0))

                if chain in chains and liquidity >= MIN_LIQUIDITY:
                    if chain not in best_prices or liquidity > best_prices[chain][1]:
                        best_prices[chain] = (price, liquidity)

            prices = {c: p[0] for c, p in best_prices.items()}
            if len(prices) >= 2:
                values = list(prices.values())
                diff = abs(values[0] - values[1]) / ((values[0] + values[1]) / 2) * 100

                # Отримуємо поріг з токена (якщо є, якщо ні — за замовчуванням MIN_DIFF_PERCENT)
                threshold = token.get("threshold", MIN_DIFF_PERCENT)

                now = time.time()
                # Перевірка, чи різниця перевищує поріг і чи час між сповіщеннями більше 60 секунд
                if diff >= threshold and now - last_alert_time >= 60:
                    msg = (
                        f"⚠️ <b>{symbol.upper()}</b> має різницю цін між мережами!\n\n" +
                        "\n".join([f"🌐 {c.upper()}: ${prices[c]:.4f}" for c in prices]) +
                        f"\n\n📊 Різниця: <b>{diff:.2f}%</b>"
                    )
                    await bot.send_message(user_id, msg)
                    save_alert_to_history(user_id, symbol, prices, diff)
                    last_alert_time = now  # Оновлюємо час останнього сповіщення
        except Exception as e:
            logger.exception(f"[ERROR monitor_token {symbol}]: {e}")

        await asyncio.sleep(5)


async def start_monitoring_for_token(user_id: int, token: dict):
    symbol = token.get("symbol") or token.get("name")
    task_key = f"{user_id}:{symbol}"

    # Якщо завдання для цього токена вже є, зупиняємо його і запускаємо нове
    if task_key in monitor_tasks:
        logger.info(f"Завдання моніторингу для токена {symbol} вже є, зупиняємо старе завдання.")
        monitor_tasks[task_key].cancel()
        del monitor_tasks[task_key]  # Видаляємо старе завдання

    # Створюємо нове завдання для моніторингу
    task = asyncio.create_task(monitor_token_by_address(user_id, token, session, bot))
    monitor_tasks[task_key] = task  # Додаємо нове завдання до словника



async def get_prices_by_name(session: aiohttp.ClientSession, symbol: str, chains: list[str], bot: Bot, user_id: int) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
    best = {}

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.error(f"Помилка при запиті до {url}, статус: {resp.status}")
                await bot.send_message(user_id, "❌ Сталася помилка при отриманні даних. Спробуйте ще раз.")
                return
            data = await resp.json()

            print(f"[DEBUG] [{symbol.upper()}] data keys: {list(data.keys())}")

            for pair in data.get("pairs", []):
                chain = pair.get("chainId")
                price = pair.get("priceUsd")
                liquidity = pair.get("liquidity", {}).get("usd")

                if chain in chains and price and liquidity and float(liquidity) >= MIN_LIQUIDITY:
                    if chain not in best or float(liquidity) > best[chain][1]:
                        best[chain] = (float(price), float(liquidity))

        return {chain: p[0] for chain, p in best.items()}

    except Exception as e:
        print(f"[ERROR get_prices_by_name {symbol}]: {e}")
        return {}


@dp.message(Command("history"))
async def history_command(message: Message):
    user_id = message.from_user.id
    path = get_user_alerts_path(user_id)

    if not path.exists():
        await message.answer("ℹ️ Історія сповіщень порожня.")
        return

    with path.open("r", encoding="utf-8") as f:
        alerts = json.load(f)

    if not alerts:
        await message.answer("ℹ️ Історія сповіщень порожня.")
        return

    last_alerts = alerts[-5:]  # Останні 5 записів
    text = "<b>📜 Останні сповіщення:</b>\n\n"
    for a in last_alerts:
        prices = "\n".join([f"🌐 {c.upper()}: ${p:.4f}" for c, p in a['prices'].items()])
        text += (
            f"🔹 <b>{a['symbol'].upper()}</b>\n"
            f"{prices}\n"
            f"📊 Різниця: <b>{a['diff']}%</b>\n"
            f"🕒 {a['timestamp']}\n\n"
        )

    await message.answer(text, parse_mode="HTML")


def get_user_alerts_path(user_id: int) -> Path:
    return get_user_data_dir(user_id) / "alerts.json"

@dp.message(lambda msg: msg.text == "📜 Історія сповіщень")
async def show_alert_history(message: Message):
    user_id = message.from_user.id
    path = get_user_alerts_path(user_id)

    if not path.exists():
        await message.answer("⛔ Історія порожня.")
        return

    with open(path, "r", encoding="utf-8") as f:
        history = json.load(f)

    if not history:
        await message.answer("⛔ Історія порожня.")
        return

    text = "📜 <b>Останні сповіщення:</b>\n\n"
    for entry in reversed(history[-10:]):
        prices = "\n".join([f"🌐 {k.upper()}: ${v:.4f}" for k, v in entry["prices"].items()])
        text += (
            f"🕒 <i>{entry['timestamp']}</i>\n"
            f"🔹 <b>{entry['symbol'].upper()}</b>\n"
            f"{prices}\n"
            f"📊 Різниця: <b>{entry['diff']}%</b>\n\n"
        )

    await message.answer(text, parse_mode="HTML")






def get_user_data_dir(user_id: int) -> Path:
    user_dir = Path("data") / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir

def get_user_tokens_path(user_id: int) -> Path:
    return get_user_data_dir(user_id) / "tokens.json"

def load_tokens(user_id: int) -> list:
    path = get_user_tokens_path(user_id)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return []

def save_tokens(user_id: int, tokens: list):
    path = get_user_tokens_path(user_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2, ensure_ascii=False)
    TOKENS[user_id] = tokens  # Оновлюємо кеш токенів, щоб зміни були доступні негайно

@dp.callback_query(lambda c: c.data.startswith("toggle_"))
async def toggle_token(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    idx = int(callback.data.replace("toggle_", ""))

    tokens = TOKENS.get(user_id, [])
    if idx >= len(tokens):
        await callback.answer("⚠️ Токен не знайдено.")
        return

    token = tokens[idx]
    symbol = token["symbol"]

    # Викликаємо функцію для перемикання моніторингу
    await toggle_token_monitoring(user_id, token)

    # Відповідаємо користувачу
    if token["active"]:
        await callback.answer("✅ Моніторинг увімкнено.")
    else:
        await callback.answer("⛔ Моніторинг вимкнено.")

    # Оновлюємо кеш
    TOKENS[user_id] = tokens
    save_tokens(user_id, tokens)


@dp.message(Command("help"))
async def help_command(message: Message):
    text = (
        "<b>👁 CoinEye — бот для моніторингу різниці цін між мережами.</b>\n\n"
        "🛠 Доступні команди:\n"
        "/start — Запустити бота\n"
        "/help — Побачити цю довідку\n"
        "/history — Переглянути останні сповіщення\n\n"
        "📍 У головному меню можна:\n"
        "➕ Додати токен\n"
        "✏️ Редагувати або видалити токени\n"
        "📋 Подивитися список токенів\n\n"
        "ℹ️ Після додавання токена бот автоматично моніторить ціни\n"
        "і надсилає сповіщення при різниці понад заданий поріг (від 1%)."
    )
    await message.answer(text, parse_mode="HTML")


async def main():
    global session
    session = aiohttp.ClientSession()

    # 🔧 Додаємо список доступних команд для меню
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Запустити бота"),
        types.BotCommand(command="help", description="Довідка"),
        types.BotCommand(command="history", description="Останні сповіщення"),
    ])

    # Отримуємо список користувачів
    tokens_path = Path("data")
    users = []
    if tokens_path.exists():
        users = [int(p.name) for p in tokens_path.iterdir() if p.is_dir() and (p / "tokens.json").exists()]

    for user_id in users:
        tokens = load_tokens(user_id)
        for token in tokens:
            if token.get("active", True):
                symbol = token.get("symbol") or token.get("name")
                task_key = f"{user_id}:{symbol}"
                try:
                    # Перевіряємо наявність мереж і адресу токена
                    if "networks" in token and "addresses" not in token:
                        token["addresses"] = token["networks"]

                    # 🟢 Завжди використовуй monitor_token_by_address
                    if "addresses" in token:
                        task = asyncio.create_task(monitor_token_by_address(user_id, token, session, bot))
                        # Додаємо завдання до monitor_tasks по ключу task_key
                        monitor_tasks.setdefault(user_id, {})[task_key] = task
                    else:
                        logger.warning(f"⛔ Пропущено токен без addresses: {symbol}")

                except Exception as e:
                    logger.exception(f"[ERROR] Failed to start monitor for {symbol} (user {user_id}): {e}")

    logger.info("🚀 Бот запущено.")
    await dp.start_polling(bot)
    await session.close()



if __name__ == "__main__":
    asyncio.run(main())
