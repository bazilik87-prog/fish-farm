import asyncio
import os
import json
import hashlib
import hmac
import secrets
import time as time_module
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
GAME_URL  = os.getenv("GAME_URL",  "https://ВАШ_НИК.github.io/fish-farm/")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
PORT      = int(os.getenv("PORT", "8080"))
PARTNER_API_KEY = os.getenv("PARTNER_API_KEY", "")  # секретный ключ для проверки заданий кросс-промо-партнёров
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # публичный URL сервиса в Railway, для webhook
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = secrets.token_hex(16)  # генерируется заново при каждом старте — это ок, т.к. используется вместе с set_webhook в этом же запуске
FIREBASE_DB_SECRET = os.getenv("FIREBASE_DB_SECRET", "")
# Добавляется ко всем запросам бота к Firebase — даёт админский доступ в обход правил безопасности,
# которые теперь можно спокойно ужесточать для обычных клиентов (игры в браузере), не боясь сломать бота.
FB_AUTH = ("?auth=" + FIREBASE_DB_SECRET) if FIREBASE_DB_SECRET else ""


def t(user, ru, en):
    """Возвращает нужный вариант текста по language_code игрока (не влияет на админские команды)."""
    lang = getattr(user, 'language_code', None)
    return ru if lang == 'ru' else en

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
}


def validate_init_data(init_data: str, max_age_seconds: int = 86400) -> dict | None:
    """
    Проверяет подпись Telegram WebApp initData по официальному алгоритму:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Возвращает распарсенные данные, если подпись верна и данные не протухли, иначе None.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None
        data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        auth_date = int(parsed.get('auth_date', 0))
        if max_age_seconds and (time_module.time() - auth_date) > max_age_seconds:
            return None
        return parsed
    except Exception:
        return None

BOOST_NAMES = {
    'doubleTap':       'Auto Boost +2 auto income per minute for 1 hour',
    'turboDry':        'Instant Dry - all drying packets finish instantly',
    'luckyRod':        'Lucky Rod +50pct for 30min',
    'turboSpeed':      'Turbo Speed x2 transport speed for 1 hour',
    'turboPack':       'Instant Packing - all packing finishes instantly',
    'instantDelivery': 'Instant Delivery - all active deliveries finish instantly',
    'repairAll':       'Repair All Transport - fully repairs all vehicles',
    'truckRental':      'Rent a Truck with Trailer - 200 capacity for 12 hours',
    'energyFull':       'Refill Energy - instantly fills your energy to max',
    'lottery':         'Lottery - spin the wheel and win coins or jackpot!',
    'weather_sunny':   'Weather Change - Sunny for 30 minutes',
    'weather_cloudy':  'Weather Change - Cloudy for 30 minutes',
    'weather_rain':    'Weather Change - Rain for 30 minutes',
    'weather_storm':   'Weather Change - Storm for 30 minutes',
    'weather_perfect': 'Weather Change - Perfect Fishing for 30 minutes',
}
BOOST_LABELS = {
    'doubleTap':       '⚡ Авто-буст (+2 автодобычи/мин на 1 час)',
    'turboDry':        '🌡 Мгновенная сушка',
    'luckyRod':        '🎣 Удачная рыбалка',
    'turboSpeed':      '🏎 Турбо скорость',
    'turboPack':       '📦 Мгновенная упаковка',
    'instantDelivery': '🚀 Мгновенная доставка',
    'repairAll':       '🔧 Ремонт всего транспорта',
    'truckRental':      '🚛 Грузовик с прицепом (аренда 12ч, вместимость 200)',
    'energyFull':       '⚡ Заполнить энергию',
    'lottery':         '🎰 Лотерея',
    'weather_sunny':   '☀️ Погода: Ясно',
    'weather_cloudy':  '🌥 Погода: Облачно',
    'weather_rain':    '🌧 Погода: Дождь',
    'weather_storm':   '⛈ Погода: Шторм',
    'weather_perfect': '🌟 Погода: Отличный клёв',
}
# Цена в Stars за буст — по умолчанию 1, для отдельных бустов можно переопределить
BOOST_PRICES = {
    'repairAll': 2,
    'truckRental': 5,
    'energyFull': 5,
}
PREMIUM_PRICE = 250  # ⭐/месяц

SUPPORT_GROUP_ID = -5478312122


async def is_premium(user_id):
    """Проверяет, активна ли премиум-подписка игрока прямо сейчас."""
    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/premium/tg_{user_id}.json{FB_AUTH}") as resp:
                until = await resp.json()
        return bool(until) and until > int(time.time() * 1000)
    except Exception:
        return False


LOCATION_MULT = {'pond': 1, 'river': 2, 'tropics': 5, 'deep': 15, 'space': 50}
LOCATION_ORDER = {'pond': 1, 'river': 2, 'tropics': 3, 'deep': 4, 'space': 5}

# ── Формулы экономики (зеркалят index.html) — используются ТОЛЬКО для расчёта
# верхнего "потолка" правдоподобного заработка на /sync, не для точной симуляции игры.
ROD_TAP = [0.1, 0.2, 0.3, 0.4, 0.5, 0.7]           # монет за улов по уровню удочки (0-5)
AUTO_PER_LEVEL = {'net': 0.1, 'boat': 0.3, 'sonar': 0.5}  # автодоход за уровень апгрейда
ENERGY_REGEN_SEC = 126                              # 1 энергия каждые 126 секунд
PREMIUM_AUTO_MULT = 1.25
MISC_BUFFER_PER_MIN = 200   # запас на доставку/лотерею/квесты/ежедневный бонус, * множитель локации

# Максимальная цена самой редкой/дорогой рыбы в каждой локации (BASE_PRICES из index.html) —
# используется для потолка дохода с РЫНКА (продажа улова), который раньше не учитывался
# и приводил к ложным срабатываниям /audit на честных активных игроках.
LOCATION_MAX_FISH_PRICE = {'pond': 6, 'river': 13, 'tropics': 28, 'deep': 60, 'space': 250}
MARKET_PRICE_MAX_MULT = 4    # рынок: цена может подскочить максимум до 4x от базовой
FILET_SELL_MULT = 5          # продажа филе — x5 к цене за штуку
# Продажа не мгновенна — ограничена вместимостью транспорта и временем доставки.
# 200 — разовый запас (грузовик 100 + водитель 100, отправленные почти одновременно),
# плюс постоянная пропускная способность ~0.083 рыбы/сек (два грузовика подряд).
IMMEDIATE_SELL_ALLOWANCE = 200
SELL_THROUGHPUT_PER_SEC = 100 / 1200


def _max_tap_power_and_auto(ulocs, upg_levels, is_premium):
    """
    Возвращает (max_tap_power, max_auto_per_sec, max_location_mult) — самые щедрые
    из ВСЕХ разлоченных локаций игрока (на случай, если он переключался между ними
    в течение периода между синками). Небольшой запас в пользу игрока — это ceiling,
    не точная симуляция.
    """
    ulocs = ulocs or ['pond']
    upg_levels = upg_levels or {}
    premium_mult = PREMIUM_AUTO_MULT if is_premium else 1
    max_tap = 0.0
    max_auto = 0.0
    max_mult = 1
    for loc_id in ulocs:
        mult = LOCATION_MULT.get(loc_id, 1)
        if mult > max_mult:
            max_mult = mult
        lv = upg_levels.get(loc_id, {}) if isinstance(upg_levels, dict) else {}
        rod_level = int(lv.get('rod', 0) or 0)
        rod_level = max(0, min(rod_level, len(ROD_TAP) - 1))
        tap = ROD_TAP[rod_level] * mult
        if tap > max_tap:
            max_tap = tap
        auto = 0.0
        for upg_id, per_level in AUTO_PER_LEVEL.items():
            lvl = int(lv.get(upg_id, 0) or 0)
            lvl = max(0, min(lvl, 5))
            auto += per_level * lvl
        auto = auto * mult * premium_mult
        if auto > max_auto:
            max_auto = auto
    return max_tap, max_auto, max_mult


def _best_unlocked_location(ulocs):
    """Локация с максимальным множителем среди разлоченных — используется и для тапа,
    и для потолка рыночной цены."""
    ulocs = ulocs or ['pond']
    best_loc, best_mult = 'pond', 1
    for loc_id in ulocs:
        m = LOCATION_MULT.get(loc_id, 1)
        if m > best_mult:
            best_mult = m
            best_loc = loc_id
    return best_loc


def compute_earning_ceiling(prev_save, is_premium, elapsed_ms):
    """
    Верхний потолок правдоподобного увеличения coins/caught за elapsed_ms с последнего
    подтверждённого сервером состояния. Специально щедрый (лучше не заблокировать
    честного игрока, чем поймать всех читеров разом) — это защита от грубой накрутки,
    а не точная симуляция игровой экономики.
    Учитывает ДВА источника: прямые монеты за тап/автодоход, И рыночную стоимость улова
    (каждая пойманная рыба может быть продана позже, в лучшем случае — как филе редкого
    вида по пиковой цене рынка).
    """
    elapsed_sec = max(0, elapsed_ms) / 1000
    ulocs = prev_save.get('ulocs') or ['pond']
    upg_levels = prev_save.get('upgLevels') or {}
    max_tap, max_auto, max_mult = _max_tap_power_and_auto(ulocs, upg_levels, is_premium)

    max_energy = 150 if is_premium else 100
    prev_energy = prev_save.get('energy')
    prev_energy = max_energy if prev_energy is None else min(float(prev_energy), max_energy)
    regen_energy = elapsed_sec / ENERGY_REGEN_SEC
    max_catches = (prev_energy + regen_energy) * 3

    best_loc = _best_unlocked_location(ulocs)
    max_fish_price = LOCATION_MAX_FISH_PRICE.get(best_loc, 6)
    max_sale_per_fish = max_fish_price * MARKET_PRICE_MAX_MULT * FILET_SELL_MULT
    sellable_fish = min(max_catches, IMMEDIATE_SELL_ALLOWANCE + elapsed_sec * SELL_THROUGHPUT_PER_SEC)
    market_ceiling = sellable_fish * max_sale_per_fish

    tap_ceiling = max_catches * max_tap
    auto_ceiling = max_auto * elapsed_sec / 60
    misc_buffer = MISC_BUFFER_PER_MIN * max_mult * (elapsed_sec / 60)

    coin_ceiling = tap_ceiling + market_ceiling + auto_ceiling + misc_buffer + 10  # +10 — запас на округления
    return coin_ceiling, max_catches


async def get_location_order(user_id):
    """
    Порядковый номер (1-5) самой продвинутой РАЗЛОЧЕННОЙ локации игрока —
    используется для цены билета лотереи (Пруд=1⭐ ... Космос=5⭐).
    Считаем на сервере, не доверяя клиенту.
    """
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    order = 1
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/tg_{user_id}/ulocs.json{FB_AUTH}") as resp:
                unlocked = await resp.json()
        if unlocked:
            for loc_id in unlocked:
                o = LOCATION_ORDER.get(loc_id, 1)
                if o > order:
                    order = o
    except Exception:
        pass
    return order



async def get_location_mult(user_id):
    """
    Множитель самой продвинутой РАЗЛОЧЕННОЙ локации игрока — считаем на сервере
    по данным его сохранения, а не доверяем тому, что мог бы прислать клиент
    (иначе курс/комиссию легко подделать).
    """
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    mult = 1
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/tg_{user_id}/ulocs.json{FB_AUTH}") as resp:
                unlocked = await resp.json()
        if unlocked:
            for loc_id in unlocked:
                m = LOCATION_MULT.get(loc_id, 1)
                if m > mult:
                    mult = m
    except Exception:
        pass
    return mult


async def get_exchange_rate(user_id, mult=None):
    """Курс обмена (сколько монет за 1 GRAM) растёт вместе с локацией игрока."""
    if mult is None:
        mult = await get_location_mult(user_id)
    return 100000 * mult


async def get_coin_balance(user_id):
    """Реальный баланс монет игрока из Firebase — сервер должен доверять только этому,
    а не числу coins, которое присылает клиент в запросе на обмен."""
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/tg_{user_id}/coins.json{FB_AUTH}") as resp:
                coins = await resp.json()
        return coins or 0
    except Exception:
        return 0


async def deduct_coin_balance(user_id, amount):
    """Атомарно списывает amount монет с баланса игрока в Firebase через transaction-подобную
    проверку (читаем-проверяем-пишем). Возвращает True, если списание прошло успешно."""
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/tg_{user_id}/coins.json{FB_AUTH}") as resp:
                current = await resp.json()
            current = current or 0
            if current < amount:
                return False
            new_balance = round((current - amount) * 100) / 100
            await session.put(f"{base}/saves/tg_{user_id}/coins.json{FB_AUTH}", json=new_balance)
        return True
    except Exception:
        return False


async def create_invoice(request):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        real_user_id = json.loads(verified.get('user', '{}')).get('id')
    except Exception:
        real_user_id = None
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    action = data.get('action')

    try:
        if action == 'exchange':
            coins   = int(data.get('coins', 0))
            wallet  = str(data.get('wallet', '')).strip()
            user_id = real_user_id  # берём из проверенной подписи, а не из тела запроса
            username = str(data.get('username', '')).strip()
            if coins < 1000 or coins > 50000 or not wallet:
                return web.json_response({'error': 'сумма должна быть от 1,000 до 50,000 монет'}, status=400, headers=CORS)
            # Проверяем реальный баланс в Firebase — не доверяем тому, что coins прислал клиент
            balance = await get_coin_balance(user_id)
            if coins > balance:
                return web.json_response({'error': 'недостаточно монет на балансе'}, status=400, headers=CORS)
            # Зашиваем данные прямо в payload — Telegram вернёт их при оплате,
            # так что рестарт бота между созданием счёта и оплатой ничего не потеряет.
            payload = f"ex:{user_id}:{coins}:{wallet}:{username}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'payload too long (кошелёк/имя слишком длинные)'}, status=400, headers=CORS)
            loc_mult = await get_location_mult(user_id)
            base_fee = 3 if await is_premium(user_id) else 5
            fee = base_fee * loc_mult
            link = await bot.create_invoice_link(
                title="GRAM Exchange",
                description=f"{coins} coins to GRAM",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Fee", amount=fee)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'boost':
            boost_id = str(data.get('boost', ''))
            user_id  = real_user_id  # берём из проверенной подписи, а не из тела запроса
            name = BOOST_NAMES.get(boost_id, 'Boost')
            if boost_id == 'lottery':
                price = await get_location_order(user_id)
            else:
                price = BOOST_PRICES.get(boost_id, 1)
            payload = f"bo:{boost_id}:{user_id}"
            link = await bot.create_invoice_link(
                title=name,
                description=name,
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Boost", amount=price)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'subscribe':
            user_id = real_user_id  # берём из проверенной подписи, а не из тела запроса
            payload = f"sub:{user_id}"
            try:
                link = await bot.create_invoice_link(
                    title="FishFarm Premium",
                    description="Премиум-подписка на 30 дней: +25% к автодоходу, бесплатный ежедневный ремонт транспорта, ⭐3 комиссия банка, защита стрика, бесплатная крутка лотереи в день, корона в лидерборде",
                    payload=payload,
                    currency="XTR",
                    prices=[LabeledPrice(label="Premium — 30 дней", amount=PREMIUM_PRICE)],
                    provider_token="",
                    subscription_period=2592000,
                )
            except TypeError:
                # Подписки Stars требуют свежую версию aiogram/Bot API — если сервер ещё не обновлён,
                # создаём разовый счёт без автопродления, чтобы функция не была полностью недоступна
                link = await bot.create_invoice_link(
                    title="FishFarm Premium (30 дней)",
                    description="Премиум на 30 дней без автопродления: +25% к автодоходу, бесплатный ежедневный ремонт транспорта, ⭐3 комиссия банка, защита стрика, бесплатная крутка лотереи в день, корона в лидерборде",
                    payload=payload,
                    currency="XTR",
                    prices=[LabeledPrice(label="Premium — 30 дней", amount=PREMIUM_PRICE)],
                    provider_token="",
                )
            return web.json_response({'link': link}, headers=CORS)

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'error': 'unknown'}, status=400, headers=CORS)


async def health(request):
    return web.json_response({'ok': True}, headers=CORS)


async def partner_check(request):
    """
    Кросс-промо-задание: партнёр спрашивает, поймал ли игрок 20 рыб.
    GET /api/check?apiKey=...&telegramId=12345
    """
    api_key = request.query.get('apiKey', '')
    telegram_id = request.query.get('telegramId', '')

    if not PARTNER_API_KEY or api_key != PARTNER_API_KEY:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not telegram_id or not telegram_id.isdigit():
        return web.json_response({'error': 'invalid telegramId'}, status=400, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/tg_{telegram_id}/caught.json{FB_AUTH}") as resp:
                caught = await resp.json()
        caught = caught or 0
        completed = caught >= 20
        return web.json_response({'completed': completed, 'caught': caught}, headers=CORS)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)


async def referral_notify(request):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        ref_user = json.loads(verified.get('user', '{}'))
    except Exception:
        ref_user = {}
    ref_username = ref_user.get('username')
    ref_first_name = ref_user.get('first_name')
    ref_name = f"@{ref_username}" if ref_username else (ref_first_name or 'Твой реферал')

    referrer_id = data.get('referrer_id')
    notify_type = data.get('type')

    if not referrer_id:
        return web.json_response({'error': 'no referrer_id'}, status=400, headers=CORS)

    try:
        if notify_type == 'rod2':
            await bot.send_message(
                int(referrer_id),
                f"🎣 {ref_name} купил(а) удочку 2-го уровня!\n\n"
                "🪙 +1000 монет уже ждут тебя в игре!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
                ]])
            )
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


# ── Точный учёт денежных действий (замена оценочного "потолка") ─────────────
# Клиент теперь шлёт КОНКРЕТНЫЕ действия (улов/продажа/апгрейд), а сервер считает
# их стоимость по тем же формулам, что и index.html, используя РЕАЛЬНЫЙ (не заявленный
# клиентом) уровень апгрейдов и РЕАЛЬНУЮ серверную цену рынка.

BASE_PRICES = {
    'Карась': 0.5, 'Красноперка': 1.2, 'Лещ': 2.5, 'Щука': 6,
    'Окунь': 0.8, 'Выдра': 2, 'Крокодил': 5, 'Гиппо': 13,
    'Тропик': 1.5, 'Попугай': 4, 'Змея': 10, 'Бабочка': 28,
    'Кальмар': 3, 'Осьминог': 8, 'Акула': 20, 'Кит': 60,
    'Пришелец': 10, 'НЛО': 30, 'Галактика': 80, 'Звезда': 250,
}
UPGRADE_COSTS = {
    'rod':   [200, 800, 3000, 10000, 30000],
    'net':   [500, 2000, 8000, 30000, 100000],
    'boat':  [1500, 10000, 25000, 80000, 250000],
    'sonar': [5000, 25000, 100000, 300000, 1000000],
}
MAX_UPGRADE_LEVEL = 5
DRIED_SELL_MULT = 3
FILET_SELL_MULT_EXACT = 5
PRICE_INTERVAL_MS = 30000
BULK_SELL_RATE = {'fresh': 0.01, 'filet': 0.02, 'dried': 0.03}  # плоская ставка за штуку, * множитель локации


async def get_market_prices():
    """
    Глобальные серверные цены рынка — общие для всех игроков, генерируются той же
    формулой случайного блуждания, что и раньше в index.html, но теперь на сервере,
    поэтому их нельзя подделать записью в собственное сохранение.
    Обновляются лениво, не чаще раза в 30с (PRICE_INTERVAL_MS).
    """
    import aiohttp, time, random
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/market/prices.json{FB_AUTH}") as resp:
            data = await resp.json()
        now_ms = int(time.time() * 1000)
        if data and isinstance(data, dict) and data.get('ts') and data.get('cur') \
                and now_ms - data['ts'] < PRICE_INTERVAL_MS:
            return data['cur']
        cur = (data or {}).get('cur') or {} if isinstance(data, dict) else {}
        new_cur = {}
        for name, base_price in BASE_PRICES.items():
            p = cur.get(name, base_price)
            chg = (random.random() - 0.48) * 0.3
            nv = max(base_price * 0.3, min(base_price * 3, p * (1 + chg)))
            nv = max(base_price * 0.3, min(base_price * 4, nv))
            new_cur[name] = round(nv * 100) / 100
        await session.put(f"{base}/market/prices.json{FB_AUTH}", json={'cur': new_cur, 'ts': now_ms})
        return new_cur


async def reset_progress(request):
    """
    Игрок нажал "Сбросить прогресс" в настройках. coins/caught/totalEarned/upgLevels/
    energy/unsoldCaught теперь пишет только сервер — клиент больше не может обнулить их
    напрямую в Firebase, поэтому нужен отдельный серверный сброс.
    """
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        real_user_id = json.loads(verified.get('user', '{}')).get('id')
    except Exception:
        real_user_id = None
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    now_ms = int(time.time() * 1000)

    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/saves/{pid}.json{FB_AUTH}", json={
                "coins": 0,
                "caught": 0,
                "totalEarned": 0,
                "upgLevels": {},
                "energy": 100,
                "lastEnergyUpdate": now_ms,
                "unsoldCaught": 0,
                "lastSeen": now_ms
            })
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def process_actions(request):
    """
    Принимает список конкретных действий (улов/продажа/покупка апгрейда) и считает
    их точную стоимость на сервере, используя реальный сохранённый уровень апгрейдов
    и реальную серверную цену рынка — а не оценку "могло ли быть".
    """
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        real_user_id = json.loads(verified.get('user', '{}')).get('id')
    except Exception:
        real_user_id = None
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    actions = data.get('actions')
    if not isinstance(actions, list) or len(actions) > 500:
        return web.json_response({'error': 'invalid actions'}, status=400, headers=CORS)

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as resp:
                sv = await resp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    sv = sv or {}
    coins = float(sv.get('coins', 0) or 0)
    caught = int(sv.get('caught', 0) or 0)
    total_earned = float(sv.get('totalEarned', 0) or 0)
    upg_levels = sv.get('upgLevels') or {}
    if not isinstance(upg_levels, dict):
        upg_levels = {}
    ulocs = sv.get('ulocs') or ['pond']
    is_prem = await is_premium(real_user_id)
    max_energy = 150 if is_prem else 100

    # Энергия — регенерируем по реальному прошедшему времени, а не верим клиенту.
    now_ms = int(time.time() * 1000)
    last_energy_update = sv.get('lastEnergyUpdate') or now_ms
    prev_energy = float(sv.get('energy', max_energy) if sv.get('energy') is not None else max_energy)
    regen_sec = max(0, (now_ms - last_energy_update) / 1000)
    energy = min(max_energy, prev_energy + regen_sec / ENERGY_REGEN_SEC)

    # Непроданный улов — нельзя продать больше рыбы, чем реально было поймано и ещё не продано.
    unsold = float(sv.get('unsoldCaught', 0) or 0)

    # Автодоход (Сеть/Лодка/Сонар) — начисляем по реальному прошедшему времени с последнего
    # обращения к /actions, используя ТЕКУЩУЮ локацию и реальный уровень апгрейдов —
    # раньше это (в отличие от тапов) считал только клиент, теперь тоже сервер.
    cur_loc = sv.get('loc') or 'pond'
    if cur_loc not in LOCATION_MULT:
        cur_loc = 'pond'
    last_seen = sv.get('lastSeen') or now_ms
    auto_elapsed_sec = max(0, (now_ms - last_seen) / 1000)
    if auto_elapsed_sec > 0 and auto_elapsed_sec < 3600 * 24:  # разумный потолок — не более суток за раз
        cur_lv = upg_levels.get(cur_loc, {}) if isinstance(upg_levels.get(cur_loc), dict) else {}
        auto_per_sec = 0
        for upg_id, per_level in AUTO_PER_LEVEL.items():
            lvl = max(0, min(int(cur_lv.get(upg_id, 0) or 0), MAX_UPGRADE_LEVEL))
            auto_per_sec += per_level * lvl
        auto_per_sec = auto_per_sec * LOCATION_MULT.get(cur_loc, 1) * (PREMIUM_AUTO_MULT if is_prem else 1)
        auto_earned = round(auto_per_sec * auto_elapsed_sec / 60 * 100) / 100
        if auto_earned > 0:
            coins += auto_earned
            total_earned += auto_earned

    prices_cache = None
    rejected = 0
    claim_pending_clear = False
    claim_result = None
    salt_delta = 0
    knife_delta = 0
    truck_tickets_delta = 0

    for act in actions:
        if not isinstance(act, dict):
            rejected += 1
            continue
        a_type = act.get('type')
        loc = act.get('loc', 'pond')
        if loc not in LOCATION_MULT or loc not in ulocs:
            rejected += 1
            continue
        mult = LOCATION_MULT.get(loc, 1)

        if a_type == 'catch':
            if energy < 1/3:
                rejected += 1
                continue
            energy -= 1/3
            lv = upg_levels.get(loc, {}) if isinstance(upg_levels.get(loc), dict) else {}
            rod_level = max(0, min(int(lv.get('rod', 0) or 0), len(ROD_TAP) - 1))
            earned = round(ROD_TAP[rod_level] * mult * 10) / 10
            coins += earned
            total_earned += earned
            caught += 1
            unsold += 1

        elif a_type == 'sell':
            name = str(act.get('name', ''))
            kind = act.get('kind', 'fresh')
            via_driver = bool(act.get('via') == 'driver')
            try:
                qty = int(act.get('qty', 0))
            except (TypeError, ValueError):
                qty = 0
            if name not in BASE_PRICES or qty < 1 or qty > 200:
                rejected += 1
                continue
            if qty > unsold + 0.001:  # небольшой допуск на округление энергии/улова
                rejected += 1
                continue
            unsold = max(0, unsold - qty)
            if prices_cache is None:
                prices_cache = await get_market_prices()
            unit = prices_cache.get(name, BASE_PRICES[name])
            if kind == 'dried':
                unit = unit * DRIED_SELL_MULT
            elif kind == 'filet':
                unit = unit * FILET_SELL_MULT_EXACT
            earned = round(unit * qty * 100) / 100
            if via_driver:
                earned = round(earned * 0.7 * 100) / 100  # водитель забирает 30%
            coins += earned
            total_earned += earned

        elif a_type == 'bulk_sell':
            kind = act.get('kind', 'fresh')
            rate = BULK_SELL_RATE.get(kind)
            try:
                qty = int(act.get('qty', 0))
            except (TypeError, ValueError):
                qty = 0
            if rate is None or qty < 1 or qty > 5000:
                rejected += 1
                continue
            if qty > unsold + 0.001:
                rejected += 1
                continue
            unsold = max(0, unsold - qty)
            earned = round(qty * rate * mult * 100) / 100
            coins += earned
            total_earned += earned

        elif a_type == 'claim_bonuses':
            # Реферальные бонусы и отложенные награды — теперь тоже начисляет только сервер,
            # а не клиент напрямую (это было единственным путём, где клиент писал coins в обход /actions).
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/ref_bonuses/{pid}.json{FB_AUTH}") as r1:
                        rb = await r1.json()
                    async with session.get(f"{base}/pending_rewards/{pid}.json{FB_AUTH}") as r2:
                        pr = await r2.json()
                claimed_total = 0
                claimed_details = []
                if isinstance(rb, dict):
                    for k, v in rb.items():
                        amt = (v or {}).get('amount', 0) if isinstance(v, dict) else 0
                        claimed_total += amt
                        claimed_details.append({'type': 'ref', 'from': (v or {}).get('from', '?'), 'amount': amt})
                if isinstance(pr, dict):
                    for k, v in pr.items():
                        amt = v if isinstance(v, (int, float)) else 0
                        claimed_total += amt
                        claimed_details.append({'type': 'reward', 'amount': amt})
                claimed_total = round(claimed_total * 100) / 100
                if claimed_total > 0:
                    coins += claimed_total
                    total_earned += claimed_total
                    claim_pending_clear = True
                    claim_result = {'total': claimed_total, 'details': claimed_details}
            except Exception:
                rejected += 1

        elif a_type == 'grant_fish':
            # Приз лотереи "рыба на склад" — клиент добавляет живую рыбу в инвентарь локально,
            # это сообщает серверу, сколько именно, чтобы unsoldCaught не разошёлся и продажа
            # этой рыбы потом не отклонялась как "продаёшь больше, чем поймал".
            # Потолок = round(100 * множитель локации) — точно как формула приза в index.html.
            try:
                qty = int(act.get('qty', 0))
            except (TypeError, ValueError):
                qty = 0
            max_fish_prize = round(100 * mult)
            if qty < 1 or qty > max_fish_prize:
                rejected += 1
                continue
            caught += qty
            unsold += qty

        elif a_type == 'lottery_coins':
            # Денежный приз лотереи (300/500 * множитель локации, 65% суммарный шанс) —
            # потолок = round(500 * множитель локации), точно как c2 в формуле приза.
            try:
                amount = float(act.get('amount', 0))
            except (TypeError, ValueError):
                amount = 0
            max_coin_prize = round(500 * mult)
            if amount <= 0 or amount > max_coin_prize:
                rejected += 1
                continue
            coins += amount
            total_earned += amount

        elif a_type == 'grant_salt':
            try:
                qty = int(act.get('qty', 0))
            except (TypeError, ValueError):
                qty = 0
            max_salt_prize = round(15 * mult)
            if qty < 1 or qty > max_salt_prize:
                rejected += 1
                continue
            salt_delta += qty

        elif a_type == 'grant_knife':
            try:
                qty = int(act.get('qty', 0))
            except (TypeError, ValueError):
                qty = 0
            max_knife_prize = round(15 * mult)
            if qty < 1 or qty > max_knife_prize:
                rejected += 1
                continue
            knife_delta += qty

        elif a_type == 'grant_truck_ticket':
            truck_tickets_delta += 1

        elif a_type == 'admin_grant':
            # Кнопки в скрытой админ-панели (видны только вам в игре) — начисление монет
            # только вашему собственному ID, проверяется на сервере, а не доверяется клиенту.
            if real_user_id != ADMIN_ID:
                rejected += 1
                continue
            try:
                amount = float(act.get('amount', 0))
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0 or amount > 10_000_000:
                rejected += 1
                continue
            coins += amount
            total_earned += amount

        elif a_type == 'buy_upgrade':
            upg_id = act.get('upg')
            costs = UPGRADE_COSTS.get(upg_id)
            if not costs:
                rejected += 1
                continue
            lv = upg_levels.get(loc)
            if not isinstance(lv, dict):
                lv = {}
                upg_levels[loc] = lv
            cur_level = int(lv.get(upg_id, 0) or 0)
            if cur_level >= MAX_UPGRADE_LEVEL:
                rejected += 1
                continue
            cost = round(costs[cur_level] * mult)
            if coins < cost:
                rejected += 1
                continue
            coins -= cost
            lv[upg_id] = cur_level + 1
            # Реферальная награда: реферер получает 1000 монет, когда его реферал впервые
            # прокачал удочку до ур.2 — начисляется здесь же на сервере, не клиентом.
            if upg_id == 'rod' and cur_level + 1 == 2:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{base}/referrals/used/{real_user_id}.json{FB_AUTH}") as r1:
                            referrer_id = await r1.json()
                        if referrer_id:
                            async with session.get(f"{base}/referrals/rod2_rewarded/{real_user_id}.json{FB_AUTH}") as r2:
                                already = await r2.json()
                            if not already:
                                await session.put(f"{base}/referrals/rod2_rewarded/{real_user_id}.json{FB_AUTH}", json=True)
                                async with session.get(f"{base}/saves/tg_{referrer_id}/coins.json{FB_AUTH}") as r3:
                                    ref_coins = await r3.json()
                                ref_coins = (ref_coins or 0) + 1000
                                await session.patch(f"{base}/saves/tg_{referrer_id}.json{FB_AUTH}", json={"coins": ref_coins})
                                try:
                                    await bot.send_message(int(referrer_id), "🎁 Реферальный бонус: +🪙1000! Твой реферал прокачал удочку до ур.2")
                                except Exception:
                                    pass
                except Exception:
                    pass

        else:
            rejected += 1

    coins = round(coins * 100) / 100
    total_earned = round(total_earned * 100) / 100
    now_ms = int(time.time() * 1000)
    # Уведомление об отклонённых действиях отключено по просьбе — слишком много шума.
    # Сама защита (отклонение подозрительных действий) продолжает работать как прежде.

    extra_fields = {}
    if salt_delta or knife_delta:
        salt_by_loc = sv.get('saltByLoc') or {}
        knife_by_loc = sv.get('knifeByLoc') or {}
        if not isinstance(salt_by_loc, dict):
            salt_by_loc = {}
        if not isinstance(knife_by_loc, dict):
            knife_by_loc = {}
        if salt_delta:
            salt_by_loc[cur_loc] = (salt_by_loc.get(cur_loc) or 0) + salt_delta
            extra_fields['saltByLoc'] = salt_by_loc
        if knife_delta:
            knife_by_loc[cur_loc] = (knife_by_loc.get(cur_loc) or 0) + knife_delta
            extra_fields['knifeByLoc'] = knife_by_loc
    if truck_tickets_delta:
        extra_fields['truckTickets'] = (sv.get('truckTickets') or 0) + truck_tickets_delta

    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/saves/{pid}.json{FB_AUTH}", json={
                "coins": coins,
                "caught": caught,
                "totalEarned": total_earned,
                "upgLevels": upg_levels,
                "energy": round(energy * 100) / 100,
                "lastEnergyUpdate": now_ms,
                "unsoldCaught": round(unsold * 100) / 100,
                "lastSeen": now_ms,
                **extra_fields
            })
            if claim_pending_clear:
                await session.delete(f"{base}/ref_bonuses/{pid}.json{FB_AUTH}")
                await session.delete(f"{base}/pending_rewards/{pid}.json{FB_AUTH}")
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({
        'ok': True,
        'coins': coins,
        'caught': caught,
        'totalEarned': total_earned,
        'upgLevels': upg_levels,
        'energy': round(energy * 100) / 100,
        'rejected': rejected,
        'claimed': claim_result
    }, headers=CORS)


async def sync_state(request):
    """
    Игрок присылает своё текущее состояние (coins/caught/totalEarned/energy и т.д.).
    Сервер сверяет с последним подтверждённым состоянием в Firebase и с реальным
    прошедшим временем — и если прирост превышает физически возможный потолок,
    обрезает его, а не пишет слепо. Это единственный путь, которым клиент теперь
    может менять денежные поля; прямая запись в Firebase для них закрыта правилами.
    """
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        real_user_id = json.loads(verified.get('user', '{}')).get('id')
    except Exception:
        real_user_id = None
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"

    try:
        req_coins = float(data.get('coins', 0) or 0)
        req_caught = int(data.get('caught', 0) or 0)
        req_total_earned = float(data.get('totalEarned', 0) or 0)
        req_energy = data.get('energy', None)
    except (TypeError, ValueError):
        return web.json_response({'error': 'invalid payload'}, status=400, headers=CORS)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as resp:
                prev = await resp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    now_ms = int(time.time() * 1000)

    if not prev:
        # Первый синк для этого игрока — просто фиксируем стартовое состояние без проверок
        # (по умолчанию у нового игрока и так coins:0, разгонять там нечего).
        prev = {}
        elapsed_ms = 0
    else:
        elapsed_ms = max(0, now_ms - (prev.get('lastSeen') or now_ms))

    prev_coins = float(prev.get('coins', 0) or 0)
    prev_caught = int(prev.get('caught', 0) or 0)
    prev_total_earned = float(prev.get('totalEarned', 0) or 0)

    is_prem = await is_premium(real_user_id)
    coin_ceiling, catch_ceiling = compute_earning_ceiling(prev, is_prem, elapsed_ms)

    coin_delta = req_coins - prev_coins
    earned_delta = req_total_earned - prev_total_earned
    catch_delta = req_caught - prev_caught

    suspicious = False
    final_coins = req_coins
    final_total_earned = req_total_earned
    final_caught = req_caught

    if coin_delta > coin_ceiling:
        suspicious = True
        final_coins = round((prev_coins + coin_ceiling) * 100) / 100
    if earned_delta > coin_ceiling:
        suspicious = True
        final_total_earned = round((prev_total_earned + coin_ceiling) * 100) / 100
    if catch_delta > catch_ceiling:
        suspicious = True
        final_caught = prev_caught + int(catch_ceiling)
    # Если игрок тратит монеты (например, купил апгрейд) — coin_delta отрицательный,
    # это всегда разрешено, потолок касается только РОСТА баланса.
    if coin_delta < 0:
        final_coins = req_coins

    max_energy = 150 if is_prem else 100
    final_energy = min(float(req_energy), max_energy) if req_energy is not None else prev.get('energy', max_energy)

    # Уведомление о срабатывании потолка /sync отключено по просьбе — слишком много шума.
    # Сама обрезка подозрительного прироста продолжает работать как прежде.

    # Пишем ТОЛЬКО денежные поля через защищённый серверный путь.
    # Остальные (drying, salting, upgLevels и т.д.) продолжает писать клиент напрямую —
    # это следующий шаг переноса, не в рамках этого эндпоинта.
    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/saves/{pid}.json{FB_AUTH}", json={
                "coins": final_coins,
                "caught": final_caught,
                "totalEarned": final_total_earned,
                "energy": final_energy,
                "lastSeen": now_ms
            })
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({
        'ok': True,
        'coins': final_coins,
        'caught': final_caught,
        'totalEarned': final_total_earned,
        'energy': final_energy,
        'clamped': suspicious
    }, headers=CORS)


async def jackpot_broadcast(request):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    verified = validate_init_data(data.get('init_data', ''))
    if not verified:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    try:
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    # Берём имя из проверенной подписи, а не из тела запроса — иначе можно было
    # разослать всем игрокам фейковое объявление о джекпоте с любым именем
    username = real_user.get('username') or real_user.get('first_name') or data.get('username', 'Игрок')
    amount = data.get('amount', 0)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/jackpot/amount.json{FB_AUTH}") as resp:
                current_jackpot = await resp.json()
    except Exception:
        current_jackpot = None
    current_jackpot = current_jackpot or 50
    # Сверяем заявленную сумму с реальным джекпотом в Firebase — не даём разослать
    # фейковое объявление о выигрыше суммы, которой на самом деле не было.
    if not isinstance(amount, (int, float)) or amount < 50 or abs(amount - current_jackpot) > 1:
        return web.json_response({'error': 'сумма не совпадает с текущим джекпотом'}, status=400, headers=CORS)
    # Сбрасываем джекпот на сервере — атомарно относительно проверки выше,
    # чтобы его нельзя было "выиграть" дважды параллельными запросами.
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/jackpot/amount.json{FB_AUTH}", json=50)
    except Exception:
        pass

    text = (
        f"🎰⭐ ДЖЕКПОТ ВЫИГРАН!\n\n"
        f"@{username} сорвал(а) джекпот и забрал(а) {amount:,}⭐ Stars в лотерее FishFarm! 🎉\n\n"
        f"Крути колесо и попробуй свою удачу!"
    )

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🎰⭐ ДЖЕКПОТ ВЫИГРАН!\n👤 @{username}\n💰 {amount:,}⭐ Stars\n\nНужно отправить звёзды вручную."
            )
        except Exception:
            pass

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    if not players:
        return web.json_response({'ok': True, 'sent': 0}, headers=CORS)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
    ]])

    sent = 0
    for v in players.values():
        user_id = v.get('userId')
        if not user_id:
            continue
        try:
            await bot.send_message(user_id, text, reply_markup=keyboard)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)

    return web.json_response({'ok': True, 'sent': sent}, headers=CORS)


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(message.from_user, "🎣 Играть", "🎣 Play"), web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text=t(message.from_user, "💬 Чат игроков", "💬 Player chat"), url="https://t.me/+cLBHDCmOkaA3NWQy")]
    ])
    await message.answer(
        t(message.from_user,
            "🐟 *Добро пожаловать в FishFarm!* 🎣\n\n"
            "Здесь можно:\n"
            "🎣 Ловить рыбу тапами — чем круче удочка, тем больше монет за улов\n"
            "🌍 Открывать локации от Пруда до Космоса — каждая выгоднее прошлой\n"
            "📦 Продавать улов на рынке (свежий, вяленый или филе) и нанимать водителя, чтобы возить рыбу, пока ты занят\n"
            "🎰 Крутить лотерею и ловить джекпот в Stars ⭐\n"
            "🏦 Обменивать монеты на GRAM в Банке\n"
            "🏆 Участвовать в турнирах недели с призами в Stars\n"
            "👥 Приглашать друзей — бонусы обоим\n"
            "💬 Общаться с другими игроками в чате\n\n"
            "Жми кнопку и закидывай удочку! 👇",
            "🐟 *Welcome to FishFarm!* 🎣\n\n"
            "Here you can:\n"
            "🎣 Catch fish by tapping — the better your rod, the more coins per catch\n"
            "🌍 Unlock locations from the Pond to Space — each more rewarding than the last\n"
            "📦 Sell your catch at the market (fresh, dried, or filet) and hire a driver to carry fish while you're busy\n"
            "🎰 Spin the lottery and win the jackpot in Stars ⭐\n"
            "🏦 Exchange coins for GRAM at the Bank\n"
            "🏆 Join weekly tournaments with Stars prizes\n"
            "👥 Invite friends — bonuses for both\n"
            "💬 Chat with other players\n\n"
            "Tap the button and cast your rod! 👇"),
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    user = message.from_user
    user_id = str(user.id)

    # Уведомляем админа о НОВОМ игроке — только если он правда жмёт /start впервые
    if ADMIN_ID and user.id != ADMIN_ID:
        import aiohttp
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        is_new = True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/known_starts/{user_id}.json{FB_AUTH}") as resp:
                    already_known = await resp.json()
                if already_known:
                    is_new = False
                else:
                    await session.put(f"{base}/known_starts/{user_id}.json{FB_AUTH}", json={
                        "registered_at": int(time_module.time() * 1000),
                        "username": user.username or ""
                    })
        except Exception:
            pass  # если Firebase недоступен — на всякий случай считаем новым, лучше лишнее уведомление чем пропуск

        if is_new:
            name = f"@{user.username}" if user.username else (user.first_name or 'Без имени')
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🆕 Новый игрок!\n👤 {name}\n🆔 {user.id}"
                )
            except Exception:
                pass

    # Обрабатываем реферальную ссылку
    args = message.text.split() if message.text else []
    ref_arg = args[1] if len(args) > 1 else ''
    if not ref_arg.startswith('ref_'):
        return

    referrer_id = ref_arg[4:]  # ID того кто пригласил
    if referrer_id == user_id:
        return  # нельзя пригласить самого себя

    import aiohttp
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"

        async with aiohttp.ClientSession() as session:
            # Проверяем что игрок новый (не использовал реферал раньше)
            async with session.get(f"{base}/referrals/used/{user_id}.json{FB_AUTH}") as resp:
                already_used = await resp.json()
            if already_used:
                return  # реферал уже был использован

            # Защита от циклических реферальных цепочек: A пригласил B, B пригласил A
            # (или длиннее: A→B→C→A) — идём вверх по цепочке рефереров от referrer_id
            # и проверяем, не встретится ли там user_id.
            chain_id = referrer_id
            for _ in range(15):  # разумный предел глубины цепочки
                if chain_id == user_id:
                    return  # цикл найден — регистрацию отклоняем молча
                async with session.get(f"{base}/referrals/used/{chain_id}.json{FB_AUTH}") as resp:
                    next_id = await resp.json()
                if not next_id:
                    break
                chain_id = str(next_id)

            # Сохраняем связь реферал → реферер
            await session.put(f"{base}/referrals/used/{user_id}.json{FB_AUTH}",
                              json=referrer_id)
            await session.put(f"{base}/referrals/by/{referrer_id}/{user_id}.json{FB_AUTH}",
                              json=True)

            # Начисляем +100 монет новому игроку
            await session.put(f"{base}/pending_rewards/tg_{user_id}/ref_bonus.json{FB_AUTH}",
                              json=100)

            # Начисляем +100 монет рефереру
            await session.put(f"{base}/pending_rewards/tg_{referrer_id}/ref_invite_{user_id}.json{FB_AUTH}",
                              json=100)

        # Уведомляем реферера
        ref_name = f"@{user.username}" if user.username else (user.first_name or 'Новый игрок')
        try:
            await bot.send_message(
                int(referrer_id),
                f"🎉 По твоей ссылке пришёл {ref_name}!\n\n"
                f"🪙 +100 монет уже ждут тебя в игре!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
                ]])
            )
        except Exception:
            pass

    except Exception:
        pass


@dp.message(Command('addcoins'))
async def addcoins_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n`/addcoins @username СУММА`\n\nПример:\n`/addcoins @nikolanaz 500`",
            parse_mode="Markdown"
        )
        return
    username = text[1].lstrip('@').lower()
    try:
        amount = int(text[2])
    except ValueError:
        await message.answer("❌ Сумма должна быть числом")
        return

    import aiohttp
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                data = await resp.json()

        user_id = None
        if data:
            for v in data.values():
                if str(v.get('username', '')).lower() == username:
                    user_id = str(v.get('userId'))
                    break

        if not user_id:
            found_names = [str(v.get('username', '')) for v in data.values() if v.get('username')] if data else []
            await message.answer(f"❌ Игрок @{username} не найден.\nИмена в базе: {', '.join(found_names[:10])}")
            return

        # Записываем в pending_rewards — игра заберёт при следующем входе
        async with aiohttp.ClientSession() as session:
            await session.put(
                f"{base}/pending_rewards/tg_{user_id}/admin_compensation.json{FB_AUTH}",
                json=amount
            )

        # Уведомляем игрока
        try:
            await bot.send_message(
                int(user_id),
                f"🎁 *Администратор начислил тебе {amount} монет!*\n\n"
                f"Зайди в игру чтобы получить их 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
                ]])
            )
        except Exception:
            pass

        await message.answer(f"✅ @{username} (ID: {user_id}) получит 🪙{amount} монет при следующем входе в игру")

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('referrals'))
async def referrals_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю данные реферальной системы...")
    import aiohttp
    from io import BytesIO
    from datetime import datetime, timezone
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        async with aiohttp.ClientSession() as session:
            # Получаем структуру referrals/by
            async with session.get(f"{base}/referrals/by.json{FB_AUTH}") as resp:
                by_data = await resp.json()
            # Получаем leaderboard для имён
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb_data = await resp.json()

        # Строим словарь userId -> username
        id_to_name = {}
        if lb_data:
            for v in lb_data.values():
                uid = str(v.get('userId', ''))
                username = v.get('username', '')
                first_name = v.get('firstName', '')
                if username:
                    id_to_name[uid] = f"@{username}"
                elif first_name:
                    id_to_name[uid] = first_name
                else:
                    id_to_name[uid] = f"ID:{uid}"

        lines = []
        total_refs = 0
        if by_data:
            # Сортируем по количеству рефералов
            sorted_refs = sorted(by_data.items(), key=lambda x: len(x[1]) if isinstance(x[1], dict) else 0, reverse=True)
            for referrer_id, referrals in sorted_refs:
                if not isinstance(referrals, dict):
                    continue
                referrer_name = id_to_name.get(referrer_id, f"ID:{referrer_id}")
                ref_list = []
                for ref_id in referrals.keys():
                    ref_name = id_to_name.get(ref_id, f"ID:{ref_id}")
                    ref_list.append(ref_name)
                total_refs += len(ref_list)
                lines.append(f"{referrer_name} ({len(ref_list)} реф.):")
                for r in ref_list:
                    lines.append(f"  └ {r}")
                lines.append("")

        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        header = f"FishFarm — Реферальная система\nДата: {now}\nВсего приглашений: {total_refs}\n{'='*40}\n\n"
        content = header + ("\n".join(lines) if lines else "Рефералов пока нет.")
        filename = f"fishfarm_referrals_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
        await message.answer_document(
            types.BufferedInputFile(content.encode('utf-8'), filename=filename),
            caption=f"👥 Всего приглашений: {total_refs}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('refcontest'))
async def refcontest_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю рейтинг реферального конкурса...")
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/ref_contest.json{FB_AUTH}") as resp:
                rc = await resp.json() or {}
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()

        scores = rc.get('scores') or {}
        ends_at = rc.get('endsAt', 0)
        now_ms = int(time.time() * 1000)
        is_active = bool(rc.get('active')) and ends_at > now_ms
        status = "🟢 Активен" if is_active else "🔴 Завершён"
        header = f"🏆 Реферальный конкурс — {status}"
        if is_active:
            ends_dt = datetime.fromtimestamp(ends_at / 1000, tz=timezone(timedelta(hours=3)))
            header += f"\nДо {ends_dt.strftime('%d.%m.%Y %H:%M')} МСК"

        if not scores:
            await message.answer(header + "\n\nПока нет результатов.")
            return

        # Строим словарь userId -> username
        id_to_name = {}
        if lb:
            for v in lb.values():
                uid = str(v.get('userId', ''))
                username = v.get('username', '')
                first_name = v.get('firstName', '')
                if username:
                    id_to_name[uid] = f"@{username}"
                elif first_name:
                    id_to_name[uid] = first_name
                else:
                    id_to_name[uid] = f"ID:{uid}"

        results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        lines = []
        medals = ['🥇', '🥈', '🥉']
        for i, (uid, count) in enumerate(results):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = id_to_name.get(uid, f"ID:{uid}")
            lines.append(f"{medal} {name} — {count} активных рефералов")

        text = header + "\n\n" + "\n".join(lines)
        await message.answer(text)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('startrefconcurs'))
async def startrefcontest_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Запускаю реферальный конкурс...")
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    started_at = int(time.time() * 1000)
    ends_at = started_at + 14 * 24 * 3600 * 1000  # 14 дней
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/ref_contest.json{FB_AUTH}", json={
                "active": True,
                "startedAt": started_at,
                "endsAt": ends_at,
                "scores": {}
            })
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()

        ends_dt = datetime.fromtimestamp(ends_at / 1000, tz=timezone(timedelta(hours=3)))
        await message.answer(
            f"✅ Реферальный конкурс запущен!\n"
            f"⏱ 14 дней — до {ends_dt.strftime('%d.%m.%Y %H:%M')} МСК\n"
            f"Через 14 дней он сам пометится как завершённый (или останови раньше через /stoprefconcurs).\n"
            f"Игроки увидят баннер с таймером и своим результатом прямо в игре."
        )

        if not players:
            return

        text = (
            "🎗️ РЕФЕРАЛЬНЫЙ КОНКУРС СТАРТУЕТ!\n\n"
            "14 дней на то, чтобы привести как можно больше активных друзей!\n\n"
            "Условие: очко засчитывается, когда твой реферал впервые запрашивает вывод от 1000 монет.\n\n"
            "⚠️ Важно: считается не сам факт приглашения, а именно активность реферала — просто зарегистрировавшийся друг очков не даёт. Приглашай тех, кто реально будет играть!\n\n"
            "Призы:\n"
            "🥇 1 место — 15 GRAM\n"
            "🥈 2 место — 10 GRAM\n"
            "🥉 3 место — 5 GRAM\n\n"
            "Скопируй свою реферальную ссылку в Настройках и зови друзей!\n"
            "Следи за своим прогрессом прямо в игре — там появится баннер конкурса."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
        ]])
        sent = 0
        for v in players.values():
            user_id = v.get('userId')
            if not user_id:
                continue
            try:
                await bot.send_message(user_id, text, reply_markup=keyboard)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await message.answer(f"📨 Анонс отправлен {sent} игрокам.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('stoprefconcurs'))
async def stoprefcontest_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/ref_contest.json{FB_AUTH}", json={"active": False})
        await message.answer("✅ Реферальный конкурс остановлен досрочно. Итоги — командой /refcontest")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('starttournament'))
async def starttournament_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Запускаю турнир...")
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    started_at = int(time.time() * 1000)
    ends_at = started_at + 48 * 3600 * 1000  # 48 часов

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()

            baseline = {}
            if players:
                for pid, v in players.items():
                    baseline[pid] = v.get('totalEarned', 0)

            await session.put(f"{base}/tournament.json{FB_AUTH}", json={
                "active": True,
                "startedAt": started_at,
                "endsAt": ends_at,
                "baseline": baseline
            })

        ends_dt = datetime.fromtimestamp(ends_at / 1000, tz=timezone(timedelta(hours=3)))
        await message.answer(
            f"✅ Турнир запущен!\n🏆 Гонка на 48 часов — до {ends_dt.strftime('%d.%m.%Y %H:%M')} МСК\n"
            f"Игроки увидят баннер и таблицу турнира в разделе 🏆 Лидеры."
        )

        if not players:
            return

        text = (
            "🏆 *ТУРНИР НЕДЕЛИ НАЧАЛСЯ!*\n\n"
            "Заработай как можно больше монет за 48 часов! 🪙⚡\n\n"
            "Призовой фонд — 200 Stars ⭐ на троих:\n"
            "🥇 1 место — 85⭐\n"
            "🥈 2 место — 65⭐\n"
            "🥉 3 место — 50⭐\n\n"
            "Заходи в игру и проверь свою позицию в разделе 🏆 Лидеры!"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
        ]])
        sent = 0
        for i, v in enumerate(players.values()):
            user_id = v.get('userId')
            if not user_id:
                continue
            try:
                await bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=keyboard)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await message.answer(f"📨 Анонс отправлен {sent} игрокам.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('stoptournament'))
async def stoptournament_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/tournament.json{FB_AUTH}", json={"active": False})
        await message.answer("✅ Турнир остановлен досрочно. Итоги — командой /tournamentstats")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('tournamentstats'))
async def tournamentstats_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю рейтинг турнира...")
    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/tournament.json{FB_AUTH}") as resp:
                t = await resp.json()
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()

        if not t or not players:
            await message.answer("Турнир ещё не запускался или нет игроков.")
            return

        baseline = t.get('baseline') or {}

        results = []
        for pid, v in players.items():
            uid = v.get('userId')
            if not uid:
                continue  # без настоящего Telegram ID — не легитимный игрок, пропускаем
            earned_now = v.get('totalEarned', 0)
            start_earned = baseline.get(pid, 0)
            delta = earned_now - start_earned
            if delta <= 0:
                continue
            username = v.get('username')
            first_name = v.get('firstName')
            if username:
                display = f"@{username}"
            elif first_name:
                display = first_name
            else:
                display = f"ID:{uid}"
            results.append((display, delta))

        if not results:
            await message.answer("Пока никто не поймал рыбу в рамках турнира.")
            return

        results.sort(key=lambda x: x[1], reverse=True)
        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, (name, delta) in enumerate(results[:15]):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {name} — {delta:,} монет")

        status = "🟢 Активен" if t.get('active') and t.get('endsAt', 0) > int(time.time() * 1000) else "🔴 Завершён"
        text = f"🏆 Турнир недели — {status}\n\n" + "\n".join(lines)
        await message.answer(text)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('startpromo'))
async def startpromo_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp, time
    ends_at = int((time.time() + 86400) * 1000)  # 24 часа
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/promo/net_bonus.json{FB_AUTH}", json={"endsAt": ends_at, "bonus": 500})
        from datetime import datetime, timezone, timedelta
        ends_dt = datetime.fromtimestamp(ends_at/1000, tz=timezone(timedelta(hours=3)))
        await message.answer(f"✅ Акция запущена!\n🎁 Бонус 500🪙 за покупку Сети активен до {ends_dt.strftime('%d.%m.%Y %H:%M')} МСК")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('stoppromo'))
async def stoppromo_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.delete(f"{base}/promo/net_bonus.json{FB_AUTH}")
        await message.answer("✅ Акция остановлена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('comm'))
async def comm_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 *Команды администратора:*\n\n"
        "/players — список всех игроков (файл .txt)\n"
        "/audit — найти аккаунты с накрученным балансом (сверка с реальным временем)\n"
        "/selftest — автопроверка формул экономики на сервере (без клика по игре)\n"
        "/referrals — реферальная система (файл .txt)\n"
        "/refcontest — рейтинг реферального конкурса\n"
        "/startrefconcurs — начать конкурс на 14 дней (сбрасывает счёт, рассылает анонс всем)\n"
        "/stoprefconcurs — остановить конкурс досрочно\n"
        "/addcoins @username СУММА — начислить монеты игроку\n"
        "/syncerrors — список игроков с ошибками синхронизации\n"
        "/playerinfo @username — развёрнутая статистика игрока\n"
        "/maintenance on|off — включить/выключить технические работы\n"
        "/premium @username [дни] — проверить/выдать/отозвать Premium\n"
        "/breakref @username — разорвать реферальную связь (для круговых цепочек)\n"
        "/delnum НОМЕР — удалить анонимную запись без username/ID (напр. «Рыбак #478»)\n"
        "/ban @username — удалить игрока и заблокировать вход\n"
        "/pay @username СУММА — уведомить игрока о выплате GRAM\n"
        "/paystars @username СУММА — уведомить о выплате Stars (джекпот)\n"
        "/broadcast ТЕКСТ — рассылка всем игрокам\n"
        "/pushcomeback ТЕКСТ — пуш только тем, кто заходил 1-3 дня назад\n"
        "/startpromo — запустить акцию +500🪙 за Сеть на 24ч\n"
        "/stoppromo — остановить акцию\n"
        "/starttournament — запустить турнир недели (48ч, рассылка всем)\n"
        "/stoptournament — остановить турнир досрочно\n"
        "/tournamentstats — рейтинг турнира\n"
        "/comm — список команд\n\n"
        "🎮 *Команды для всех:*\n\n"
        "/start — запустить игру\n\n"
        "💬 Чат игроков: https://t.me/+cLBHDCmOkaA3NWQy",
        parse_mode="Markdown"
    )


LOC_NAMES = {'pond': '🌿 Пруд', 'river': '🏞 Река', 'tropics': '🌴 Тропики', 'deep': '🌊 Глубины', 'space': '🚀 Космос'}
UPG_NAMES = {'rod': 'Удочка', 'net': 'Сеть', 'boat': 'Лодка', 'sonar': 'Сонар'}


@dp.message(Command('syncerrors'))
async def syncerrors_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю список ошибок синхронизации...")
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/sync_errors.json{FB_AUTH}") as resp:
                errors = await resp.json()
            if not errors:
                await message.answer("✅ Ошибок синхронизации нет — все игроки сохраняются нормально.")
                return

            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()

        id_to_name = {}
        if lb:
            for v in lb.values():
                pid_key = f"tg_{v.get('userId')}"
                username = v.get('username', '')
                first_name = v.get('firstName', '')
                if username:
                    id_to_name[pid_key] = f"@{username}"
                elif first_name:
                    id_to_name[pid_key] = first_name

        lines = [f"⚠️ Игроков с ошибками синхронизации: {len(errors)}\n"]
        items = sorted(errors.items(), key=lambda x: x[1].get('ts', 0), reverse=True)
        for pid, info in items[:30]:
            name = id_to_name.get(pid, pid)
            ts = info.get('ts', 0)
            msg = info.get('message', '—')
            if ts:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone(timedelta(hours=3)))
                time_str = dt.strftime('%d.%m %H:%M')
            else:
                time_str = '?'
            lines.append(f"👤 {name} — {time_str} МСК\n   {msg[:100]}")

        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('playerinfo'))
async def playerinfo_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer("Использование:\n`/playerinfo @username`", parse_mode="Markdown")
        return
    username = args[1].lstrip('@').lower()
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()
            uid = None
            lb_entry = None
            if lb:
                for v in lb.values():
                    if str(v.get('username', '')).lower() == username:
                        uid = v.get('userId')
                        lb_entry = v
                        break
            if not uid:
                await message.answer(f"❌ Игрок @{username} не найден в лидерборде.")
                return

            pid = f"tg_{uid}"
            async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as resp:
                sv = await resp.json()
            if not sv:
                await message.answer(f"❌ Нет сохранения для @{username} (ID: {uid}).")
                return

            async with session.get(f"{base}/referrals/used/{uid}.json{FB_AUTH}") as resp:
                referrer_id = await resp.json()

            async with session.get(f"{base}/known_starts/{uid}.json{FB_AUTH}") as resp:
                known_start = await resp.json()

            async with session.get(f"{base}/premium/{pid}.json{FB_AUTH}") as resp:
                premium_until = await resp.json()

        lines = [f"👤 @{username} (ID: {uid})"]

        registered_at = known_start.get('registered_at') if isinstance(known_start, dict) else None
        if registered_at:
            reg_dt = datetime.fromtimestamp(registered_at / 1000, tz=timezone(timedelta(hours=3)))
            lines.append(f"📅 Регистрация: {reg_dt.strftime('%d.%m.%Y %H:%M')} МСК")
        else:
            lines.append("📅 Регистрация: неизвестна (игрок зашёл до внедрения этого учёта)")

        loc = sv.get('loc', 'pond')
        ulocs = sv.get('ulocs', ['pond'])
        loc_names_str = ', '.join(LOC_NAMES.get(l, l) for l in ulocs)
        lines.append(f"📍 Сейчас: {LOC_NAMES.get(loc, loc)}")
        lines.append(f"🗺 Разлочено: {loc_names_str}")

        upg_levels = sv.get('upgLevels') or {}
        if upg_levels:
            lines.append("")
            lines.append("🎣 Прокачка по локациям:")
            for loc_id in ulocs:
                lv = upg_levels.get(loc_id, {})
                parts = [f"{UPG_NAMES.get(k,k)} {lv.get(k,0)}" for k in ['rod', 'net', 'boat', 'sonar']]
                lines.append(f"  {LOC_NAMES.get(loc_id, loc_id)}: " + ", ".join(parts))

        transport = sv.get('transport', 'bike')
        dur = sv.get('dur', {})
        dur_str = ", ".join(f"{k}:{v}%" for k, v in dur.items()) if dur else "—"
        lines.append("")
        lines.append(f"🚛 Транспорт: {transport} ({dur_str})")

        coins = sv.get('coins', 0)
        total_earned = sv.get('totalEarned', 0)
        caught = sv.get('caught', 0)
        lines.append(f"🪙 Баланс: {coins:,.0f} · Всего заработано: {total_earned:,.0f} · Поймано: {caught:,}")

        now_ms = int(time.time() * 1000)
        is_prem = bool(premium_until) and premium_until > now_ms
        if is_prem:
            until_dt = datetime.fromtimestamp(premium_until / 1000, tz=timezone(timedelta(hours=3)))
            lines.append(f"💎 Premium: активен до {until_dt.strftime('%d.%m.%Y %H:%M')} МСК")
        else:
            lines.append("💎 Premium: не активен")

        if referrer_id:
            lines.append(f"👥 Пришёл по рефералке от ID: {referrer_id}")
        else:
            lines.append("👥 Реферер: нет")

        daily_day = sv.get('dailyDay', 0)
        daily_last = sv.get('dailyLast', 0)
        if daily_last:
            days_ago = round((now_ms - daily_last) / 86400000, 1)
            lines.append(f"🎁 Стрик бонуса: день {daily_day} (последний раз {days_ago}д назад)")
        else:
            lines.append("🎁 Стрик бонуса: ни разу не забирал")

        quests = sv.get('quests') or []
        quest_progress = sv.get('questProgress') or {}
        done = sum(1 for q in quests if quest_progress.get(q, 0) > 0)
        lines.append(f"📋 Квесты сегодня: {len(quests)} заданий в списке")

        boosts = sv.get('boosts') or {}
        active_boosts = []
        for k, v in boosts.items():
            if isinstance(v, (int, float)) and v > now_ms:
                mins_left = round((v - now_ms) / 60000)
                active_boosts.append(f"{k} ({mins_left}мин)")
        lines.append(f"⚡ Активные бустеры: {', '.join(active_boosts) if active_boosts else 'нет'}")

        last_seen = sv.get('lastSeen', 0)
        if last_seen:
            last_dt = datetime.fromtimestamp(last_seen / 1000, tz=timezone(timedelta(hours=3)))
            lines.append(f"🕐 Последнее сохранение: {last_dt.strftime('%d.%m.%Y %H:%M')} МСК")

        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('maintenance'))
async def maintenance_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2 or args[1].lower() not in ('on', 'off'):
        await message.answer(
            "Использование:\n"
            "`/maintenance on` — включить технические работы (игра покажет заглушку)\n"
            "`/maintenance off` — выключить, игра снова доступна",
            parse_mode="Markdown"
        )
        return
    turn_on = args[1].lower() == 'on'
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/config/maintenance.json{FB_AUTH}", json={
                "on": turn_on,
                "message": "Мы недолго проводим технические работы. Игра скоро вернётся, прогресс сохранён!",
                "message_en": "We're running a short maintenance. The game will be back soon, your progress is safe!"
            })
        if turn_on:
            await message.answer("🔧 Технические работы ВКЛЮЧЕНЫ. Игроки увидят заглушку.")
        else:
            await message.answer("✅ Технические работы ВЫКЛЮЧЕНЫ. Игра снова доступна игрокам.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('clear_stale_bonuses'))
async def clear_stale_bonuses_command(message: types.Message):
    """
    Разовая очистка: старые ref_bonuses/pending_rewards, начисленные ДО фикса проверки
    реального баланса при обмене — признаны нечестными и не подлежат выплате.
    """
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            r1 = await session.delete(f"{base}/ref_bonuses.json{FB_AUTH}")
            r2 = await session.delete(f"{base}/pending_rewards.json{FB_AUTH}")
        await message.answer(
            f"🧹 Очищено:\n"
            f"ref_bonuses: {'✅' if r1.status == 200 else '❌ ' + str(r1.status)}\n"
            f"pending_rewards: {'✅' if r2.status == 200 else '❌ ' + str(r2.status)}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('premium'))
async def premium_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "`/premium @username` — проверить статус\n"
            "`/premium @username 30` — выдать/продлить на N дней вручную\n"
            "`/premium @username 0` — отозвать подписку",
            parse_mode="Markdown"
        )
        return
    username = args[1].lstrip('@').lower()
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()
            uid = None
            if lb:
                for v in lb.values():
                    if str(v.get('username', '')).lower() == username:
                        uid = v.get('userId')
                        break
            if not uid:
                await message.answer(f"❌ Игрок @{username} не найден.")
                return

            if len(args) >= 3:
                days = int(args[2])
                if days <= 0:
                    await session.delete(f"{base}/premium/tg_{uid}.json{FB_AUTH}")
                    await message.answer(f"✅ Подписка @{username} отозвана.")
                else:
                    until = int(time.time() * 1000) + days * 24 * 3600 * 1000
                    await session.put(f"{base}/premium/tg_{uid}.json{FB_AUTH}", json=until)
                    until_dt = datetime.fromtimestamp(until / 1000, tz=timezone(timedelta(hours=3)))
                    await message.answer(f"✅ @{username} получил Premium до {until_dt.strftime('%d.%m.%Y %H:%M')} МСК")
            else:
                async with session.get(f"{base}/premium/tg_{uid}.json{FB_AUTH}") as resp2:
                    until = await resp2.json()
                now_ms = int(time.time() * 1000)
                if until and until > now_ms:
                    until_dt = datetime.fromtimestamp(until / 1000, tz=timezone(timedelta(hours=3)))
                    await message.answer(f"💎 @{username} — Premium активен до {until_dt.strftime('%d.%m.%Y %H:%M')} МСК")
                else:
                    await message.answer(f"— @{username} не подписан на Premium")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('breakref'))
async def breakref_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n`/breakref @username`\n\n"
            "Убирает связь «кем был приглашён» у указанного игрока — "
            "используется для разрыва круговых реферальных цепочек (A пригласил B, B пригласил A).",
            parse_mode="Markdown"
        )
        return
    username = args[1].lstrip('@').lower()
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()

            target_uid = None
            if lb:
                for v in lb.values():
                    if str(v.get('username', '')).lower() == username:
                        target_uid = str(v.get('userId', ''))
                        break

            if not target_uid:
                await message.answer(f"❌ Игрок @{username} не найден в лидерборде.")
                return

            async with session.get(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}") as resp:
                old_referrer = await resp.json()

            if not old_referrer:
                await message.answer(f"— У @{username} и так нет реферера, разрывать нечего.")
                return

            await session.delete(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}")
            await session.delete(f"{base}/referrals/by/{old_referrer}/{target_uid}.json{FB_AUTH}")

        await message.answer(f"✅ Связь разорвана: @{username} больше не считается рефералом ID:{old_referrer}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('delnum'))
async def delnum_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(
            "Использование:\n`/delnum 478`\n\n"
            "Удаляет запись из лидерборда/сохранения по номеру (`num`) — "
            "для анонимных записей без username и userId (например, старые эксплойт-аккаунты типа «Рыбак #478»).",
            parse_mode="Markdown"
        )
        return
    target_num = int(args[1])
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()

            target_pid = None
            target_info = None
            if lb:
                for pid, v in lb.items():
                    if v.get('num') == target_num:
                        target_pid = pid
                        target_info = v
                        break

            if not target_pid:
                await message.answer(f"❌ Запись с номером {target_num} не найдена в лидерборде.")
                return

            await session.delete(f"{base}/leaderboard/{target_pid}.json{FB_AUTH}")
            await session.delete(f"{base}/saves/{target_pid}.json{FB_AUTH}")

        earned = target_info.get('totalEarned', 0) if target_info else 0
        await message.answer(f"✅ Запись #{target_num} удалена (было заработано: {earned:,} монет). Лидерборд и сохранение очищены.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('ban'))
async def ban_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer("Использование:\n`/ban @username`\n\nУдаляет игрока из лидерборда/турнира, стирает прогресс, убирает из рефералов и блокирует повторный вход.", parse_mode="Markdown")
        return
    username = args[1].lstrip('@').lower()
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                lb = await resp.json()

            target_pid = None
            target_uid = None
            if lb:
                for pid, v in lb.items():
                    if str(v.get('username', '')).lower() == username:
                        target_pid = pid
                        target_uid = str(v.get('userId', ''))
                        break

            if not target_pid:
                await message.answer(f"❌ Игрок @{username} не найден в лидерборде.")
                return

            # Удаляем из лидерборда и стираем прогресс
            await session.delete(f"{base}/leaderboard/{target_pid}.json{FB_AUTH}")
            await session.delete(f"{base}/saves/{target_pid}.json{FB_AUTH}")

            # Убираем из всех реферальных списков
            async with session.get(f"{base}/referrals/by.json{FB_AUTH}") as resp:
                by_data = await resp.json()
            if by_data and target_uid:
                for referrer_id, refs in by_data.items():
                    if isinstance(refs, dict) and target_uid in refs:
                        await session.delete(f"{base}/referrals/by/{referrer_id}/{target_uid}.json{FB_AUTH}")

            # Помечаем как забаненного — игра проверяет это при входе
            if target_uid:
                await session.put(f"{base}/banned/{target_uid}.json{FB_AUTH}", json=True)

        await message.answer(f"✅ @{username} удалён: лидерборд, прогресс, рефералы очищены. Повторный вход заблокирован.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('pay'))
async def pay_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n`/pay @username СУММА`\n\nПример:\n`/pay @Metelegram12 0.073`",
            parse_mode="Markdown"
        )
        return
    username = text[1].lstrip('@').lower()
    amount = text[2]
    import aiohttp
    try:
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json{FB_AUTH}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        user_id = None
        if data:
            for v in data.values():
                if str(v.get('username','')).lower() == username:
                    user_id = v.get('userId')
                    break
        if not user_id:
            found_names = [str(v.get('username','')) for v in data.values() if v.get('username')] if data else []
            await message.answer(f"❌ Игрок @{username} не найден.\nИмена в базе: {', '.join(found_names[:10])}")
            return
        await bot.send_message(
            user_id,
            f"✅ *Выплата выполнена!*\n\n"
            f"💎 {amount} GRAM отправлены на твой кошелёк.\n\n"
            f"Спасибо что играешь в FishFarm! 🎣",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
        await message.answer(f"✅ Уведомление отправлено @{username} (ID: {user_id}) о выплате {amount} GRAM")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('paystars'))
async def paystars_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n`/paystars @username СУММА`\n\nПример:\n`/paystars @Metelegram12 27`",
            parse_mode="Markdown"
        )
        return
    username = text[1].lstrip('@').lower()
    amount = text[2]
    import aiohttp
    try:
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json{FB_AUTH}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        user_id = None
        if data:
            for v in data.values():
                if str(v.get('username','')).lower() == username:
                    user_id = v.get('userId')
                    break
        if not user_id:
            found_names = [str(v.get('username','')) for v in data.values() if v.get('username')] if data else []
            await message.answer(f"❌ Игрок @{username} не найден.\nИмена в базе: {', '.join(found_names[:10])}")
            return
        await bot.send_message(
            user_id,
            f"✅ *Выплата выполнена!*\n\n"
            f"⭐ {amount} Stars отправлены тебе.\n\n"
            f"Спасибо что играешь в FishFarm! 🎣",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
        await message.answer(f"✅ Уведомление отправлено @{username} (ID: {user_id}) о выплате {amount}⭐ Stars")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('players'))
async def players_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю список игроков...")
    import aiohttp
    from io import BytesIO
    from datetime import datetime, timezone
    try:
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json{FB_AUTH}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        if not data:
            await message.answer("Пока нет игроков.")
            return
        players = sorted(data.values(), key=lambda x: x.get('caught',0), reverse=True)
        locs = {'pond':'🌿','river':'🏞','tropics':'🌴','deep':'🌊','space':'🚀'}
        lines = []
        for i, p in enumerate(players, 1):
            num = p.get('num','?')
            coins = p.get('coins', 0)
            caught = p.get('caught', 0)
            loc = locs.get(p.get('loc','pond'),'🌿')
            username = p.get('username','')
            first_name = p.get('firstName','')
            user_id = p.get('userId', 0)
            if username:
                identity = f"@{username}"
            elif first_name:
                identity = first_name
            elif user_id:
                identity = f"ID:{user_id}"
            else:
                identity = f"Рыбак #{num}"
            ts = p.get('ts', 0)
            if ts:
                last_seen = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            else:
                last_seen = 'неизвестно'
            total_earned = p.get('totalEarned', 0)
            lines.append(f"{i}. {loc} {identity} | total_earned:{total_earned:,} | coins:{coins:,} | caught:{caught} | last:{last_seen}")
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        header = f"FishFarm — Список игроков\nДата: {now}\nВсего: {len(players)}\n{'='*40}\n\n"
        content = header + "\n".join(lines)
        filename = f"fishfarm_players_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
        file_bytes = content.encode('utf-8')
        await message.answer_document(
            types.BufferedInputFile(file_bytes, filename=filename),
            caption=f"👥 Игроков: {len(players)}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('audit'))
async def audit_command(message: types.Message):
    """
    Сверяет totalEarned каждого игрока с абсолютным теоретическим потолком
    (лучший случай: Космос, все апгрейды макс., Premium) исходя из времени,
    прошедшего с его регистрации (known_starts). Флагует тех, у кого баланс
    физически не мог быть заработан честной игрой — вероятные жертвы старой
    дыры в правилах Firebase (запись напрямую в базу до фикса).
    """
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Провожу аудит балансов...")
    import aiohttp
    from datetime import datetime, timezone, timedelta
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()
            async with session.get(f"{base}/known_starts.json{FB_AUTH}") as resp:
                known_starts = await resp.json()

        if not players:
            await message.answer("Пока нет игроков.")
            return

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        flagged = []
        no_reg_date = []

        # Абсолютный потолок в секунду — лучший случай (Космос x50, все апгрейды 5 lvl, Premium)
        best_upg = {'rod': 5, 'net': 5, 'boat': 5, 'sonar': 5}
        best_save = {'ulocs': ['space'], 'upgLevels': {'space': best_upg}}

        for v in players.values():
            user_id = v.get('userId')
            if not user_id:
                continue
            total_earned = v.get('totalEarned', 0) or 0
            coins = v.get('coins', 0) or 0
            caught = v.get('caught', 0) or 0
            username = v.get('username', '')
            identity = f"@{username}" if username else f"ID:{user_id}"

            ks = (known_starts or {}).get(str(user_id))
            registered_at = ks.get('registered_at') if isinstance(ks, dict) else None

            if not registered_at:
                if total_earned > 10000:  # без даты регистрации — просто отмечаем крупные балансы отдельно
                    no_reg_date.append(f"{identity} | заработано:{total_earned:,.0f} | монет:{coins:,.0f}")
                continue

            elapsed_ms = max(0, now_ms - registered_at)
            ceiling, _ = compute_earning_ceiling(best_save, True, elapsed_ms)
            # 2х запас сверху — это ЛУЧШИЙ случай для нового игрока, реальный потолок ниже,
            # но лучше перебдеть и не пометить честного игрока как подозрительного
            ceiling = ceiling * 2

            if total_earned > ceiling:
                reg_dt = datetime.fromtimestamp(registered_at / 1000, tz=timezone(timedelta(hours=3)))
                age_min = elapsed_ms / 60000
                flagged.append(
                    f"{identity} (ID:{user_id})\n"
                    f"  📅 Регистрация: {reg_dt.strftime('%d.%m.%Y %H:%M')} МСК ({age_min:,.0f} мин назад)\n"
                    f"  🪙 Баланс: {coins:,.0f} · Заработано: {total_earned:,.0f} · Поймано: {caught}\n"
                    f"  ⚠️ Теоретический потолок: {ceiling:,.0f} — превышен в {(total_earned/max(ceiling,1)):.1f}x раз"
                )

        lines = []
        if flagged:
            lines.append(f"🚨 ПОДОЗРИТЕЛЬНЫЕ АККАУНТЫ ({len(flagged)}):\n")
            lines.extend(flagged)
        else:
            lines.append("✅ Явных превышений потолка не найдено.")

        if no_reg_date:
            lines.append(f"\n\nℹ️ Крупные балансы без даты регистрации ({len(no_reg_date)}) — проверить вручную:\n")
            lines.extend(no_reg_date)

        content = "\n".join(lines)
        if len(content) > 3500:
            filename = f"fishfarm_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
            await message.answer_document(
                types.BufferedInputFile(content.encode('utf-8'), filename=filename),
                caption=f"🚨 Подозрительных: {len(flagged)} · Без даты регистрации: {len(no_reg_date)}"
            )
        else:
            await message.answer(content)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('selftest'))
async def selftest_command(message: types.Message):
    """
    Автоматическая проверка формул экономики на сервере — без единого клика по игре.
    Не проверяет клиентский код (index.html) на предмет "забыл подключить queueAction()" —
    это архитектурный класс багов, для него нужен ручной аудит кода. Но ловит опечатки/регрессии
    в самих формулах (таблицы цен, стоимость апгрейдов, множители локаций) на сервере.
    """
    if message.from_user.id != ADMIN_ID:
        return
    checks = []

    def check(name, condition, detail=''):
        checks.append((name, bool(condition), detail))

    # Целостность таблиц
    check('BASE_PRICES: 20 видов рыбы', len(BASE_PRICES) == 20, f'сейчас {len(BASE_PRICES)}')
    check('ROD_TAP: 6 уровней (0-5)', len(ROD_TAP) == 6, f'сейчас {len(ROD_TAP)}')
    check('UPGRADE_COSTS: 4 апгрейда × 5 уровней', all(len(v) == 5 for v in UPGRADE_COSTS.values()) and len(UPGRADE_COSTS) == 4)
    check('LOCATION_MULT: 5 локаций', len(LOCATION_MULT) == 5, f'сейчас {len(LOCATION_MULT)}')
    check('AUTO_PER_LEVEL: 3 апгрейда (net/boat/sonar)', len(AUTO_PER_LEVEL) == 3)

    # Формула улова: Пруд, удочка ур.0 → 0.1 монет; Космос, удочка ур.5 → 35 монет
    catch_pond_lvl0 = round(ROD_TAP[0] * LOCATION_MULT['pond'] * 10) / 10
    check('Улов: Пруд + удочка 0 = 0.1', catch_pond_lvl0 == 0.1, f'получено {catch_pond_lvl0}')
    catch_space_lvl5 = round(ROD_TAP[5] * LOCATION_MULT['space'] * 10) / 10
    check('Улов: Космос + удочка 5 = 35', catch_space_lvl5 == 35, f'получено {catch_space_lvl5}')

    # Стоимость апгрейда: удочка ур.0 в Пруду = 200, в Космосе = 10000
    check('Апгрейд: удочка ур.0 в Пруду = 200⭐', UPGRADE_COSTS['rod'][0] * LOCATION_MULT['pond'] == 200)
    check('Апгрейд: удочка ур.0 в Космосе = 10000⭐', UPGRADE_COSTS['rod'][0] * LOCATION_MULT['space'] == 10000)

    # Продажа: сушёная x3, филе x5 от базовой цены
    base = BASE_PRICES['Карась']
    check('Продажа: сушёная = x3 от базовой', round(base * DRIED_SELL_MULT, 4) == round(base * 3, 4))
    check('Продажа: филе = x5 от базовой', round(base * FILET_SELL_MULT_EXACT, 4) == round(base * 5, 4))

    # Ставки массовой продажи (без транспорта)
    check('bulk_sell: fresh < filet < dried по ставке', BULK_SELL_RATE['fresh'] < BULK_SELL_RATE['filet'] < BULK_SELL_RATE['dried'])

    # Рыночные цены — генерируются и укладываются в границы [0.3x, 4x] от базовой
    try:
        prices = await get_market_prices()
        bad_prices = [n for n, p in prices.items() if not (BASE_PRICES[n]*0.3 - 0.01 <= p <= BASE_PRICES[n]*4 + 0.01)]
        check('Рыночные цены в границах 0.3x–4x', not bad_prices, f'вне границ: {bad_prices}' if bad_prices else '')
    except Exception as e:
        check('Рыночные цены генерируются без ошибок', False, str(e))

    passed = sum(1 for _, ok, _ in checks if ok)
    lines = [f"🧪 Самопроверка формул: {passed}/{len(checks)} пройдено\n"]
    for name, ok, detail in checks:
        icon = '✅' if ok else '❌'
        lines.append(f"{icon} {name}" + (f' — {detail}' if detail and not ok else ''))
    lines.append(
        '\n⚠️ Это не проверяет index.html на пропущенные вызовы queueAction() '
        '(как было с водителем/bulkSell) — только формулы на сервере.'
    )
    await message.answer('\n'.join(lines))


@dp.message(Command('broadcast'))
async def broadcast_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()[len('/broadcast'):].strip()
    if not text:
        await message.answer(
            "Использование:\n`/broadcast Текст сообщения`\n\nПример:\n`/broadcast 🎉 Новое обновление! Заходи в игру!`",
            parse_mode="Markdown"
        )
        return
    await message.answer("⏳ Рассылка начата...")
    import aiohttp
    from datetime import datetime, timezone
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"

        # Сохраняем новость в Firebase
        now = datetime.now(timezone.utc)
        news_key = now.strftime('%Y%m%d_%H%M%S')
        news_entry = {
            'text': text,
            'ts': int(now.timestamp() * 1000),
            'date': now.strftime('%d.%m.%Y %H:%M')
        }
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/news/{news_key}.json{FB_AUTH}", json=news_entry)

            # Оставляем только последние 5 новостей
            async with session.get(f"{base}/news.json?orderBy=\"ts\"") as resp:
                all_news = await resp.json()
            if all_news and len(all_news) > 5:
                sorted_keys = sorted(all_news.keys())
                for old_key in sorted_keys[:-5]:
                    await session.delete(f"{base}/news/{old_key}.json{FB_AUTH}")

        url = f"{base}/leaderboard.json{FB_AUTH}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        if not data:
            await message.answer("❌ Игроков не найдено.")
            return
        total = 0
        success = 0
        failed = 0
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
        ]])
        for v in data.values():
            user_id = v.get('userId')
            if not user_id:
                continue
            total += 1
            try:
                await bot.send_message(user_id, text, reply_markup=keyboard)
                success += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(
            f"✅ *Рассылка завершена*\n\n"
            f"📨 Отправлено: {success}\n"
            f"❌ Не доставлено: {failed}\n"
            f"👥 Всего: {total}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('pushcomeback'))
async def pushcomeback_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()[len('/pushcomeback'):].strip()
    if not text:
        await message.answer(
            "Использование:\n`/pushcomeback Текст сообщения`\n\n"
            "Отправит только игрокам, которые заходили 1-3 дня назад "
            "(лучший момент вернуть в игру, пока не забыли).\n\n"
            "Пример:\n`/pushcomeback 🐡 Редкая рыба уже в пруду! Успей поймать!`",
            parse_mode="Markdown"
        )
        return
    await message.answer("⏳ Ищу игроков, заходивших 1-3 дня назад...")
    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                data = await resp.json()
        if not data:
            await message.answer("❌ Игроков не найдено.")
            return

        now_ms = int(time.time() * 1000)
        day_ms = 24 * 3600 * 1000
        targets = []
        for v in data.values():
            user_id = v.get('userId')
            ts = v.get('ts', 0)
            if not user_id or not ts:
                continue
            age = now_ms - ts
            if day_ms <= age <= 3 * day_ms:
                targets.append(user_id)

        if not targets:
            await message.answer("Сейчас нет игроков в сегменте \"1-3 дня назад\".")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
        ]])
        sent = 0
        for user_id in targets:
            try:
                await bot.send_message(user_id, text, reply_markup=keyboard)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await message.answer(f"✅ Таргетированный пуш отправлен {sent} из {len(targets)} игроков (заходили 1-3 дня назад).")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload

    # Уведомление о любой оплате звёздами — независимо от того, за что платили
    if ADMIN_ID:
        try:
            amount = message.successful_payment.total_amount
            payer_username = message.from_user.username
            payer_name = f"@{payer_username}" if payer_username else (message.from_user.first_name or f"ID:{message.from_user.id}")
            label = BOOST_LABELS.get(payload.split(':')[1], payload) if payload.startswith('bo:') else \
                    ('Обмен на GRAM' if payload.startswith('ex:') else
                     'Premium подписка' if payload.startswith('sub:') else payload)
            await bot.send_message(
                ADMIN_ID,
                f"⭐ Новая оплата!\n👤 {payer_name}\n💰 {amount}⭐\n📦 {label}"
            )
        except Exception:
            pass

    if payload.startswith('bo:'):
        parts    = payload.split(':')
        boost_id = parts[1] if len(parts) > 1 else ''
        user_id  = parts[2] if len(parts) > 2 else str(message.from_user.id)
        label    = BOOST_LABELS.get(boost_id, 'Бустер')
        import aiohttp
        try:
            pid = f"tg_{user_id}"
            url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/pending_boosts/{pid}/{boost_id}.json{FB_AUTH}"
            import time
            async with aiohttp.ClientSession() as session:
                await session.put(url, json=int(time.time() * 1000))
        except Exception:
            pass

    elif payload.startswith('sub:'):
        # Срабатывает и на первую оплату, и на каждое ежемесячное автопродление —
        # каждый раз просто ставим срок действия на 30 дней вперёд от текущего момента.
        parts = payload.split(':')
        user_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        import aiohttp, time
        try:
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            until = int((time.time() + 30 * 24 * 3600) * 1000)
            async with aiohttp.ClientSession() as session:
                await session.put(f"{base}/premium/tg_{user_id}.json{FB_AUTH}", json=until)
        except Exception:
            pass
        try:
            await message.answer(
                t(message.from_user,
                    "🎉 Premium активирован на 30 дней!\n\n"
                    "✅ +25% к автодоходу\n"
                    "✅ Бесплатный ежедневный ремонт транспорта\n"
                    "✅ Комиссия банка снижена до ⭐3\n"
                    "✅ Защита стрика ежедневного бонуса\n"
                    "✅ Бесплатная крутка лотереи раз в день\n"
                    "✅ Корона рядом с именем в лидерборде\n\n"
                    "Подписка продлевается автоматически каждые 30 дней.",
                    "🎉 Premium activated for 30 days!\n\n"
                    "✅ +25% to auto-income\n"
                    "✅ Free daily transport repair\n"
                    "✅ Bank fee reduced to ⭐3\n"
                    "✅ Daily bonus streak protection\n"
                    "✅ One free lottery spin per day\n"
                    "✅ Crown next to your name on the leaderboard\n\n"
                    "The subscription renews automatically every 30 days.")
            )
        except Exception:
            pass

    elif payload.startswith('ex:'):
        parts = payload.split(':', 4)
        # ex:{user_id}:{coins}:{wallet}:{username}
        if len(parts) < 4:
            await message.answer(t(message.from_user,
                "✅ Оплата получена! Свяжись с администратором для получения GRAM.",
                "✅ Payment received! Contact the admin to get your GRAM."))
            return
        user_id  = parts[1]
        coins    = parts[2]
        wallet   = parts[3]
        username = parts[4] if len(parts) > 4 else ''

        # Критическая проверка: реально списываем монеты с баланса в Firebase.
        # Если у игрока не хватает монет (баланс изменился/был подделан с момента создания счёта) —
        # НЕ отправляем админу запрос на выплату GRAM (это самое важное — блокировка происходит
        # в любом случае). Уведомление вам в Telegram отключено по просьбе — слишком много шума.
        deducted = await deduct_coin_balance(user_id, int(coins))
        if not deducted:
            await message.answer(t(message.from_user,
                "❌ Недостаточно монет на балансе на момент оплаты. Звёзды за комиссию не возвращаются автоматически — напиши администратору.",
                "❌ Insufficient coin balance at payment time. Stars fee isn't auto-refunded — please contact the admin."))
            return

        try:
            rate = await get_exchange_rate(user_id)
            gram_amount = round(int(coins) / rate, 5)
        except ValueError:
            gram_amount = 0

        # Публичная лента выводов — для баннера "История выплат" в игре
        try:
            import aiohttp, time as time_mod
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            entry = {"amount": int(coins), "gram": gram_amount, "wallet": wallet, "ts": int(time_mod.time() * 1000)}
            async with aiohttp.ClientSession() as session:
                await session.post(f"{base}/withdrawals_log.json{FB_AUTH}", json=entry)
        except Exception:
            pass

        await message.answer(
            t(message.from_user,
                f"✅ Заявка принята!\n\n🪙 Монет: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n⏳ Отправим в течение 24 часов.",
                f"✅ Request accepted!\n\n🪙 Coins: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n⏳ We'll send it within 24 hours.")
        )
        if ADMIN_ID:
            ul = f"@{username}" if username else f"ID: {user_id}"
            try:
                sent = await bot.send_message(
                    ADMIN_ID,
                    f"💰 Новый обмен!\n👤 {ul}\n🪙 Монет: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n⭐ Отправь токены!"
                )
            except Exception:
                sent = None
            if sent:
                try:
                    await bot.pin_chat_message(ADMIN_ID, sent.message_id, disable_notification=True)
                except Exception:
                    pass
            try:
                await bot.send_message(
                    SUPPORT_GROUP_ID,
                    f"💰 Новый запрос на вывод!\n👤 {ul}\n🪙 Монет: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n⭐ Требует выплаты!"
                )
            except Exception:
                pass

        # Реферальный бонус: 50% от суммы вывода — начисляем на сервере, после того как
        # монеты реально списаны с проверенного баланса (а не по слову клиента).
        try:
            import aiohttp, time as time_mod
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/referrals/used/{user_id}.json{FB_AUTH}") as resp:
                    referrer_id = await resp.json()
                if referrer_id:
                    bonus = round(int(coins) * 0.5 * 100) / 100
                    key = f"ref_bonus_{user_id}_{int(time_mod.time() * 1000)}"
                    from_name = f"@{username}" if username else f"ID:{user_id}"
                    await session.put(f"{base}/ref_bonuses/tg_{referrer_id}/{key}.json{FB_AUTH}", json={
                        "amount": bonus,
                        "from": from_name
                    })
                    # Реферальный конкурс: очко засчитывается один раз — когда реферал впервые вывел от 1000 монет
                    async with session.get(f"{base}/ref_contest/withdrawal_done/{user_id}.json{FB_AUTH}") as resp2:
                        already_done = await resp2.json()
                    if not already_done:
                        await session.put(f"{base}/ref_contest/withdrawal_done/{user_id}.json{FB_AUTH}", json=True)
                        async with session.get(f"{base}/ref_contest/scores/{referrer_id}.json{FB_AUTH}") as resp3:
                            current_score = await resp3.json()
                        await session.put(f"{base}/ref_contest/scores/{referrer_id}.json{FB_AUTH}", json=(current_score or 0) + 1)
        except Exception:
            pass


@dp.message()
async def any_message(message: types.Message):
    if message.chat.type != 'private':
        return  # web_app-кнопки нельзя отправлять в группах — просто игнорируем
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        return  # админу не нужен этот фолбэк — он и так знает, что делать
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer("Нажми кнопку чтобы играть 👇", reply_markup=keyboard)


async def main():
    app = web.Application()
    app.router.add_post('/invoice', create_invoice)
    app.router.add_options('/invoice', create_invoice)
    app.router.add_post('/referral_notify', referral_notify)
    app.router.add_options('/referral_notify', referral_notify)
    app.router.add_post('/jackpot_broadcast', jackpot_broadcast)
    app.router.add_options('/jackpot_broadcast', jackpot_broadcast)
    app.router.add_post('/sync', sync_state)
    app.router.add_options('/sync', sync_state)
    app.router.add_post('/actions', process_actions)
    app.router.add_options('/actions', process_actions)
    app.router.add_post('/reset_progress', reset_progress)
    app.router.add_options('/reset_progress', reset_progress)
    app.router.add_get('/health', health)
    app.router.add_get('/api/check', partner_check)

    if PUBLIC_URL:
        # Webhook-режим: Telegram сам присылает апдейты на наш URL —
        # никакого long-polling, а значит и никакого TelegramConflictError
        # при пересечении старого и нового контейнера во время деплоя.
        webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()
        print(f"API запущен на порту {PORT}")

        try:
            await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True, request_timeout=10)
            print(f"Webhook установлен: {webhook_url}")
        except Exception as e:
            print(f"Не удалось установить webhook: {e}")

        print("Бот запущен!")
        await asyncio.Event().wait()  # держим процесс живым — всю работу делает aiohttp-сервер выше
    else:
        # Фолбэк на polling, если PUBLIC_URL не задан (например, при локальном тестировании)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()
        print(f"API запущен на порту {PORT}")
        try:
            await bot.delete_webhook(drop_pending_updates=True, request_timeout=10)
        except Exception as e:
            print(f"delete_webhook не удался, продолжаем без него: {e}")
        print("Бот запущен! (PUBLIC_URL не задан — используется polling)")
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
