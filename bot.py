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

# Клановая фича в разработке — видна и доступна только тестовым аккаунтам, пока не
# обкатана на живых данных. ADMIN_ID попадает в список тестеров автоматически, остальные
# добавляются через CLAN_TEST_USER_IDS (числовой Telegram id через запятую) ИЛИ через
# CLAN_TEST_USERNAMES (username через запятую, без @, регистр не важен) в переменных
# окружения Railway — без правки кода можно добавить/убрать тестера. Юзернеймы удобнее,
# когда числовой id тестера неизвестен — сверяются с username из проверенной подписи
# Telegram initData, а не из тела запроса. Когда фича готова для всех — убираем все
# проверки is_clan_tester(...) одним заходом по коду, ничего не переставляя местами.
CLAN_TESTERS = set()
if ADMIN_ID:
    CLAN_TESTERS.add(ADMIN_ID)
for _clan_tester_part in os.getenv("CLAN_TEST_USER_IDS", "").split(","):
    _clan_tester_part = _clan_tester_part.strip()
    if _clan_tester_part.isdigit():
        CLAN_TESTERS.add(int(_clan_tester_part))

CLAN_TEST_USERNAMES = set()
for _clan_tester_username in os.getenv("CLAN_TEST_USERNAMES", "").split(","):
    _clan_tester_username = _clan_tester_username.strip().lstrip("@").lower()
    if _clan_tester_username:
        CLAN_TEST_USERNAMES.add(_clan_tester_username)


def is_clan_tester(user_id, username=None) -> bool:
    try:
        if int(user_id) in CLAN_TESTERS:
            return True
    except (TypeError, ValueError):
        pass
    if username and str(username).strip().lstrip("@").lower() in CLAN_TEST_USERNAMES:
        return True
    return False


CLAN_NAME_MIN = 2
CLAN_NAME_MAX = 20


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
# Бусты, растущие в цене вместе с локацией — начиная с Тропиков (order>=3).
# repairAll: +2⭐ за локацию (Пруд/Река 2⭐, Тропики 4⭐, Глубины 6⭐, Космос 10⭐ — особое значение).
# Остальные обычные бусты: +1⭐ за локацию (Пруд/Река 1⭐, Тропики 2⭐, Глубины 3⭐, Космос 5⭐ — особое значение).
SCALING_BOOST_STEP = {
    'doubleTap': 1, 'turboDry': 1, 'luckyRod': 1, 'turboSpeed': 1,
    'turboPack': 1, 'instantDelivery': 1,
    'repairAll': 2,
}
# В Космосе (order=5) цена не по формуле база+step*3, а заданное вручную значение —
# обычные бусты ⭐5, ремонт ⭐10 (просьба Sasha).
SPACE_PRICE_OVERRIDE = {
    'doubleTap': 5, 'turboDry': 5, 'luckyRod': 5, 'turboSpeed': 5,
    'turboPack': 5, 'instantDelivery': 5,
    'repairAll': 10,
}
# Бусты с нелинейной прогрессией цены — фиксированная таблица по order (1..5):
# Пруд/Река 5⭐, Тропики 10⭐, Глубины 15⭐, Космос 25⭐.
CUSTOM_SCALED_BOOST_PRICES = {
    'truckRental': {1: 5, 2: 5, 3: 10, 4: 15, 5: 25},
    'energyFull':  {1: 5, 2: 5, 3: 10, 4: 15, 5: 25},
}

def scaled_boost_price(boost_id, base_price, location_order):
    """Базовая цена не меняется на Пруду/Реке (order 1-2). С Тропиков (order>=3)
    растёт на step⭐ за каждую локацию дальше Реки, а в Космосе (order=5) берётся
    заданное вручную значение из SPACE_PRICE_OVERRIDE. Считаем на сервере по
    location_order из get_location_order(), не доверяя клиенту."""
    table = CUSTOM_SCALED_BOOST_PRICES.get(boost_id)
    if table:
        return table.get(location_order, base_price)
    if location_order == 5 and boost_id in SPACE_PRICE_OVERRIDE:
        return SPACE_PRICE_OVERRIDE[boost_id]
    step = SCALING_BOOST_STEP.get(boost_id)
    if not step:
        return base_price
    extra_steps = max(0, location_order - 2)
    return base_price + step * extra_steps
PREMIUM_PRICE = 250  # ⭐/месяц
REFERRAL_MARKET_PRICE = 10  # ⭐ за право стать рефером игрока, зашедшего без ссылки

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
AUTO_PER_LEVEL = {'net': 0.1, 'boat': 0.3, 'sonar': 0.5}  # автодоход за уровень апгрейда, монет В МИНУТУ (не в секунду — см. index.html: autoPerMin, деление elapsed/1000/60)
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
    Возвращает (max_tap_power, max_auto_per_min, max_location_mult) — самые щедрые
    из ВСЕХ разлоченных локаций игрока (на случай, если он переключался между ними
    в течение периода между синками). Небольшой запас в пользу игрока — это ceiling,
    не точная симуляция.

    ВАЖНО: max_auto_per_min — монеты В МИНУТУ (как autoPerMin в index.html), не в
    секунду. Раньше здесь и в AUTO_PER_LEVEL было название "per_sec" при том, что вся
    игра всегда считала эту величину за минуту (elapsed/1000/60 в index.html) — сама
    формула ceiling (auto_ceiling = max_auto * elapsed_sec / 60 ниже) была верной, но
    название вводило в заблуждение при устных расчётах.
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
    """
    Атомарно списывает amount монет с баланса игрока — ETag-блокировка с retry, а не
    голое "прочитал-проверил-записал". Это путь банковского вывода в GRAM/Stars —
    самое чувствительное место в игре: без блокировки параллельный /actions мог
    переписать списание обратно (тот пишет ВЕСЬ saves/{pid} по своему более старому
    снимку), отменяя вычет уже ПОСЛЕ того, как GRAM/Stars реально отправлены игроку —
    по сути бесплатный вывод денег. Теперь пишем узкий путь saves/{pid}/coins.json
    с If-Match и перепроверкой достаточности баланса на каждой попытке.
    """
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    url = f"{base}/saves/tg_{user_id}/coins.json{FB_AUTH}"
    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(6):
                async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
                    etag = resp.headers.get("ETag")
                    current = await resp.json()
                current = current or 0
                if current < amount:
                    return False
                new_balance = round((current - amount) * 100) / 100
                headers = {"If-Match": etag} if etag else {}
                async with session.put(url, json=new_balance, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue  # баланс изменился между чтением и записью — перечитываем и проверяем заново
                    if put_resp.status not in (200, 204):
                        return False
                    return True
        return False  # исчерпали попытки — безопаснее отказать, чем списать вслепую
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
        real_user_verified = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user_verified = {}
    real_user_id = real_user_verified.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    action = data.get('action')

    try:
        if action == 'exchange':
            coins   = int(data.get('coins', 0))
            wallet  = str(data.get('wallet', '')).strip()
            user_id = real_user_id  # берём из проверенной подписи, а не из тела запроса
            username = str(data.get('username', '')).strip()
            loc_mult = await get_location_mult(user_id)
            max_withdraw = 50000 if loc_mult == 2 else 50000 * loc_mult * loc_mult  # Река —
            # исключение, потолок оставлен как на Пруду по отдельной просьбе, остальные
            # локации по формуле mult^2 (см. комментарий выше про Stars-за-GRAM).
            if coins < 1000 or coins > max_withdraw or not wallet:
                return web.json_response({'error': f'сумма должна быть от 1,000 до {max_withdraw:,} монет'}, status=400, headers=CORS)
            # Проверяем реальный баланс в Firebase — не доверяем тому, что coins прислал клиент
            balance = await get_coin_balance(user_id)
            if coins > balance:
                return web.json_response({'error': 'недостаточно монет на балансе'}, status=400, headers=CORS)
            # Зашиваем данные прямо в payload — Telegram вернёт их при оплате,
            # так что рестарт бота между созданием счёта и оплатой ничего не потеряет.
            payload = f"ex:{user_id}:{coins}:{wallet}:{username}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'payload too long (кошелёк/имя слишком длинные)'}, status=400, headers=CORS)
            # Комиссия фиксированная по локации (5⭐/10⭐/25⭐/75⭐/250⭐ на Пруду/Реке/
            # Тропиках/Глубинах/Космосе, Premium — 3/6/15/45/150⭐), НЕ зависит от суммы
            # вывода — один и тот же взнос что за 1,000 монет, что за весь потолок.
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
                base_price = BOOST_PRICES.get(boost_id, 1)
                if boost_id in SCALING_BOOST_STEP or boost_id in CUSTOM_SCALED_BOOST_PRICES:
                    location_order = await get_location_order(user_id)
                    price = scaled_boost_price(boost_id, base_price, location_order)
                else:
                    price = base_price
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

        elif action == 'buy_referral':
            # Биржа рефералов — покупка права стать рефером игроку, который зашёл без ссылки.
            target_uid = str(data.get('target_uid', '')).strip()
            if not target_uid or not target_uid.isdigit():
                return web.json_response({'error': 'invalid target'}, status=400, headers=CORS)
            if target_uid == str(real_user_id):
                return web.json_response({'error': 'нельзя купить самого себя'}, status=400, headers=CORS)
            import aiohttp as _aiohttp
            fb_base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            async with _aiohttp.ClientSession() as session:
                async with session.get(f"{fb_base}/referrals/used/{target_uid}.json{FB_AUTH}") as resp:
                    existing_ref = await resp.json()
            if existing_ref:
                return web.json_response({'error': 'этот игрок уже занят'}, status=400, headers=CORS)
            payload = f"rb:{real_user_id}:{target_uid}"
            link = await bot.create_invoice_link(
                title="Referral Market",
                description=f"Купить право на реферала (ID {target_uid})",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Referral", amount=REFERRAL_MARKET_PRICE)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'clan_buy_slot':
            # Платное расширение клана — капитан открывает 3-8 слот за Stars.
            # Цена по формуле 10 + 5*(n-2) для n=3..8, что упрощается до 5*n.
            user_id = real_user_id
            # Ник берём из проверенной подписи initData, а не из тела запроса — так же,
            # как во всех остальных клановых эндпоинтах (тело запроса клиенту доверять нельзя,
            # к тому же фронт этот параметр вообще не присылал — из-за этого и был баг).
            if not is_clan_tester(user_id, real_user_verified.get('username')):
                return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)
            import aiohttp as _aiohttp
            fb_base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{user_id}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(f"{fb_base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                    clan_id = await resp.json()
                if not clan_id:
                    return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
                async with session.get(f"{fb_base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                    clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != user_id:
                return web.json_response({'error': 'расширять клан может только капитан'}, status=403, headers=CORS)
            members_count = clan_data.get('membersCount', len(clan_data.get('members') or {}))
            max_members = clan_data.get('maxMembers', 2)
            if members_count < max_members:
                return web.json_response({'error': 'сначала заполни уже открытые места'}, status=400, headers=CORS)
            next_slot = max_members + 1
            if next_slot > 8:
                return web.json_response({'error': 'клан уже максимального размера (8)'}, status=400, headers=CORS)
            price = 5 * next_slot  # 15/20/25/30/35/40⭐ для слотов 3..8
            payload = f"cs:{user_id}:{clan_id}:{next_slot}"
            link = await bot.create_invoice_link(
                title=f"Слот клана #{next_slot}",
                description=f"Открыть {next_slot}-е место в клане «{clan_data.get('name','')}»",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Clan slot", amount=price)],
                provider_token="",
            )
            return web.json_response({'link': link, 'price': price}, headers=CORS)

        elif action == 'clan_tournament_create':
            # Капитан создаёт турнир и сразу же сам оплачивает первый взнос — запись в
            # clan_tournaments появляется только по факту оплаты (в successful_payment,
            # ветка ctc:), а не здесь, чтобы не плодить турниры-сироты от брошенных счетов.
            user_id = real_user_id
            if not is_clan_tester(user_id, real_user_verified.get('username')):
                return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)
            try:
                amount = int(data.get('amount', 0))
            except (TypeError, ValueError):
                amount = 0
            if amount < 50:
                return web.json_response({'error': 'минимальный взнос — 50⭐'}, status=400, headers=CORS)
            import aiohttp as _aiohttp
            fb_base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{user_id}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(f"{fb_base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                    clan_id = await resp.json()
                if not clan_id:
                    return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
                async with session.get(f"{fb_base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                    clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != user_id:
                return web.json_response({'error': 'создавать турнир может только капитан'}, status=403, headers=CORS)
            payload = f"ctc:{user_id}:{clan_id}:{amount}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'internal: payload too long'}, status=500, headers=CORS)
            link = await bot.create_invoice_link(
                title=f"Турнир клана «{clan_data.get('name', '')}»",
                description=f"Взнос капитана в банк турнира — {amount}⭐",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Tournament stake", amount=amount)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'clan_tournament_pay':
            # Участник клана вносит взнос в уже созданный турнир — либо своей стороной-
            # инициатором (funding), либо принимающей стороной, куда клан попал через accept
            # (matching). Сторона определяется тем, к какой роли относится СВОЙ клан прямо
            # сейчас — capitан её отдельно не выбирает.
            user_id = real_user_id
            if not is_clan_tester(user_id, real_user_verified.get('username')):
                return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)
            tournament_id = str(data.get('tournament_id', '')).strip()
            if not tournament_id:
                return web.json_response({'error': 'invalid tournament'}, status=400, headers=CORS)
            import aiohttp as _aiohttp
            fb_base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{user_id}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(f"{fb_base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                    my_clan_id = await resp.json()
                if not my_clan_id:
                    return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
                async with session.get(f"{fb_base}/clan_tournaments/{tournament_id}.json{FB_AUTH}") as tresp:
                    tdata = await tresp.json()
            if not isinstance(tdata, dict):
                return web.json_response({'error': 'турнир не найден'}, status=404, headers=CORS)
            status = tdata.get('status')
            if status == 'funding' and tdata.get('initiatorClanId') == my_clan_id:
                side = 'A'
                deadline = tdata.get('fundingDeadline', 0)
                already_paid = pid in (tdata.get('participantsA') or {})
            elif status == 'matching' and tdata.get('acceptedByClanId') == my_clan_id:
                side = 'B'
                deadline = tdata.get('matchingDeadline', 0)
                already_paid = pid in (tdata.get('participantsB') or {})
            else:
                return web.json_response({'error': 'сбор взносов для этого турнира сейчас недоступен'}, status=400, headers=CORS)
            if already_paid:
                return web.json_response({'error': 'ты уже внёс взнос'}, status=400, headers=CORS)
            if int(time_module.time() * 1000) >= deadline:
                return web.json_response({'error': 'время сбора истекло'}, status=400, headers=CORS)
            amount = tdata.get('amountPerPerson', 0)
            payload = f"ctp:{user_id}:{tournament_id}:{side}"
            link = await bot.create_invoice_link(
                title="Взнос в турнир клана",
                description=f"Твой взнос в банк турнира — {amount}⭐",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Tournament stake", amount=amount)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'clan_tournament_accept':
            # Капитан другого клана принимает открытый турнир — сам платит свою ставку
            # первым же взносом (символично зеркалит то, как капитан-инициатор платит первым
            # при создании). Запись фактически меняется только в successful_payment (ctа:),
            # чтобы брошенный счёт не переводил турнир в matching без реальной оплаты.
            user_id = real_user_id
            if not is_clan_tester(user_id, real_user_verified.get('username')):
                return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)
            tournament_id = str(data.get('tournament_id', '')).strip()
            if not tournament_id:
                return web.json_response({'error': 'invalid tournament'}, status=400, headers=CORS)
            import aiohttp as _aiohttp
            fb_base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{user_id}"
            async with _aiohttp.ClientSession() as session:
                async with session.get(f"{fb_base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                    my_clan_id = await resp.json()
                if not my_clan_id:
                    return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
                async with session.get(f"{fb_base}/clans/{my_clan_id}.json{FB_AUTH}") as cresp:
                    my_clan_data = await cresp.json()
                if not isinstance(my_clan_data, dict) or my_clan_data.get('captainId') != user_id:
                    return web.json_response({'error': 'принять турнир может только капитан'}, status=403, headers=CORS)
                async with session.get(f"{fb_base}/clan_tournaments/{tournament_id}.json{FB_AUTH}") as tresp:
                    tdata = await tresp.json()
                if not isinstance(tdata, dict) or tdata.get('status') != 'open':
                    return web.json_response({'error': 'этот турнир уже недоступен'}, status=400, headers=CORS)
                if tdata.get('initiatorClanId') == my_clan_id:
                    return web.json_response({'error': 'нельзя принять свой же турнир'}, status=400, headers=CORS)
                if int(time_module.time() * 1000) >= tdata.get('listExpiresAt', 0):
                    return web.json_response({'error': 'срок турнира истёк'}, status=400, headers=CORS)
                if await _clan_has_active_match(session, fb_base, my_clan_id, exclude_id=tournament_id):
                    return web.json_response({'error': 'у твоего клана уже есть активный турнир с соперником'}, status=400, headers=CORS)
                if await _clan_has_active_match(session, fb_base, tdata.get('initiatorClanId'), exclude_id=tournament_id):
                    return web.json_response({'error': 'клан-инициатор сейчас занят другим турниром'}, status=400, headers=CORS)
            amount = tdata.get('amountPerPerson', 0)
            payload = f"cta:{user_id}:{tournament_id}:{my_clan_id}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'internal: payload too long'}, status=500, headers=CORS)
            link = await bot.create_invoice_link(
                title="Принять турнир клана",
                description=f"Твой взнос как принимающего капитана — {amount}⭐",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Tournament stake", amount=amount)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'error': 'unknown'}, status=400, headers=CORS)


async def health(request):
    return web.json_response({'ok': True}, headers=CORS)


async def referral_market_list(request):
    """
    Биржа рефералов — список игроков, зашедших БЕЗ реферальной ссылки (значит их ещё
    можно "купить"). Показывает ник и базовую статистику, чтобы покупатель мог выбрать
    конкретного, а не тыкать вслепую.
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

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()
            async with session.get(f"{base}/referrals/used.json{FB_AUTH}") as resp2:
                used = await resp2.json()
            async with session.get(f"{base}/banned.json{FB_AUTH}") as resp3:
                banned = await resp3.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    used = used or {}
    banned = banned or {}
    players = players or {}
    result = []
    for v in players.values():
        uid = v.get('userId')
        if not uid or uid == real_user_id:
            continue
        if str(uid) in used:
            continue  # уже есть реферер — не продаётся
        if banned.get(str(uid)):
            continue  # забаненный игрок — не продаётся
        username = v.get('username') or ''
        if not username:
            continue  # без ника не на что смотреть покупателю
        result.append({
            'uid': uid,
            'username': username,
            'totalEarned': v.get('totalEarned', 0),
            'caught': v.get('caught', 0),
            'lastSeen': v.get('ts', 0),
        })
    result.sort(key=lambda p: p['lastSeen'], reverse=True)
    return web.json_response({'ok': True, 'players': result[:50], 'price': REFERRAL_MARKET_PRICE}, headers=CORS)


async def social_tasks_list(request):
    """
    Список активных «социальных» заданий (вступи в группу рекламодателя за монеты) —
    показывает игроку, что ещё не подтверждено на его аккаунте.
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

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/social_tasks.json{FB_AUTH}") as resp:
                tasks = await resp.json()
            async with session.get(f"{base}/saves/{pid}/socialClaimed.json{FB_AUTH}") as resp2:
                claimed = await resp2.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    tasks = tasks or {}
    claimed = claimed or {}
    result = []
    for task_id, t in tasks.items():
        if not isinstance(t, dict) or not t.get('active'):
            continue
        result.append({
            'id': task_id,
            'label': t.get('label', 'Задание'),
            'link': t.get('link', ''),
            'reward': t.get('reward', 0),
            'claimed': bool(claimed.get(task_id)),
        })
    return web.json_response({'ok': True, 'tasks': result}, headers=CORS)


async def claim_social_task(request):
    """
    Подтверждение выполнения социального задания — сервер сам проверяет через Telegram API,
    что игрок реально состоит в группе (getChatMember), а не просто верит клику "Подтвердить".
    Бот должен быть добавлен в группу рекламодателя, иначе проверка технически невозможна.
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

    task_id = str(data.get('task_id', '')).strip()
    if not task_id:
        return web.json_response({'error': 'invalid task'}, status=400, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/social_tasks/{task_id}.json{FB_AUTH}") as resp:
                task = await resp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    if not task or not isinstance(task, dict) or not task.get('active'):
        return web.json_response({'error': 'задание не найдено или неактивно'}, status=400, headers=CORS)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/socialClaimed/{task_id}.json{FB_AUTH}") as resp:
                already = await resp.json()
    except Exception:
        already = False
    if already:
        return web.json_response({'error': 'уже получено'}, status=400, headers=CORS)

    chat_id = task.get('chat_id')
    if task.get('type') == 'link':
        # Без проверки — считаем сам факт клика "Забрать" достаточным. Защита от
        # повторного клейма (socialClaimed) ниже точно такая же, как у остальных типов.
        pass
    elif task.get('type') == 'bot':
        # Задание за переход в бота-партнёра — проверяем через ЕГО API, не через getChatMember
        # (для ботов этот метод Telegram недоступен в принципе, только для групп/каналов).
        verify_url = task.get('verify_url')
        verify_key = task.get('verify_key')
        if not verify_url:
            return web.json_response({'error': 'задание настроено некорректно'}, status=400, headers=CORS)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(verify_url, params={'apiKey': verify_key, 'telegramId': real_user_id}) as resp:
                    result = await resp.json()
        except Exception as e:
            return web.json_response({'error': f'не удалось проверить у партнёра: {e}'}, status=400, headers=CORS)
        if not isinstance(result, dict) or not result.get('completed'):
            return web.json_response({'error': 'задание пока не выполнено у партнёра'}, status=400, headers=CORS)
    else:
        try:
            member = await bot.get_chat_member(chat_id, real_user_id)
            if member.status in ('left', 'kicked'):
                return web.json_response({'error': 'не в группе'}, status=400, headers=CORS)
        except Exception as e:
            return web.json_response({'error': f'не удалось проверить вступление: {e}'}, status=400, headers=CORS)

    reward = float(task.get('reward', 0))
    saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(6):
                async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                    etag = resp.headers.get("ETag")
                    sv = await resp.json()
                sv = sv or {}
                # Перепроверяем "уже получено" на СВЕЖИХ данных на каждой попытке — та же
                # защита, что и в lottery_spin: если конкурентный клик уже засчитал задание
                # между попытками, повтор это увидит и честно откажет, а не даст дубль.
                claimed = sv.get('socialClaimed') or {}
                if isinstance(claimed, dict) and claimed.get(task_id):
                    return web.json_response({'error': 'уже получено'}, status=400, headers=CORS)

                merged = dict(sv)
                merged['coins'] = round((float(sv.get('coins', 0) or 0) + reward) * 100) / 100
                merged['totalEarned'] = round((float(sv.get('totalEarned', 0) or 0) + reward) * 100) / 100
                new_claimed = dict(claimed) if isinstance(claimed, dict) else {}
                new_claimed[task_id] = True
                merged['socialClaimed'] = new_claimed

                headers = {"If-Match": etag} if etag else {}
                async with session.put(saves_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue
                    if put_resp.status not in (200, 204):
                        return web.json_response({'error': f'saves PUT failed: {put_resp.status}'}, status=500, headers=CORS)
                    break
            else:
                return web.json_response({'error': 'internal: too many conflicts'}, status=500, headers=CORS)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'reward': reward}, headers=CORS)


def _clan_members_list(members_dict):
    """Превращает словарь clans/{id}/members в отсортированный список для фронта — капитан первым."""
    result = []
    for mpid, m in (members_dict or {}).items():
        if not isinstance(m, dict):
            continue
        result.append({
            'pid': mpid,
            'userId': m.get('userId'),
            'name': m.get('name', ''),
            'username': m.get('username', ''),
            'role': m.get('role', 'member'),
            'joinedAt': m.get('joinedAt', 0),
        })
    result.sort(key=lambda x: (0 if x['role'] == 'captain' else 1, x['joinedAt']))
    return result


def _clan_public(clan_id, clan_data, real_user_id):
    """Единое представление клана для ответов /clan_status, /clan_create, /clan_invite_respond."""
    members = clan_data.get('members') or {}
    return {
        'id': clan_id,
        'name': clan_data.get('name', ''),
        'membersCount': clan_data.get('membersCount', len(members) or 1),
        'maxMembers': clan_data.get('maxMembers', 2),
        'captainId': clan_data.get('captainId'),
        'isCaptain': clan_data.get('captainId') == real_user_id,
        'createdAt': clan_data.get('createdAt', 0),
        'members': _clan_members_list(members),
    }


def _display_handle(real_user, sv=None):
    """Ник для показа другому игроку (чтобы можно было написать в личку) — @username в приоритете,
    иначе playerName/first_name, иначе просто id."""
    username = (real_user or {}).get('username')
    if username:
        return f"@{username}"
    player_name = (sv or {}).get('playerName')
    if player_name:
        return player_name
    first_name = (real_user or {}).get('first_name')
    if first_name:
        return first_name
    return f"ID:{(real_user or {}).get('id', '?')}"


async def _next_tournament_number(session, base):
    """
    Сквозная нумерация турниров через одиночный счётчик clan_tournament_seq — атомарно
    в той же манере ETag/If-Match+retry, что и остальные денежные операции в файле
    (настоящей Firebase-транзакции через REST API не делаем, но при ожидаемо низкой
    конкуренции на тестах ретраи с ETag дают тот же результат).
    """
    seq_url = f"{base}/clan_tournament_seq.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(seq_url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            cur = await resp.json()
        cur = int(cur or 0)
        new_val = cur + 1
        headers = {"If-Match": etag} if etag else {}
        async with session.put(seq_url, json=new_val, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status in (200, 204):
                return new_val
            return None
    return None


async def _fixate_tournament(session, base, tournament_id):
    """
    Переводит турнир funding -> open: фиксирует requiredCount по факту оплативших,
    присваивает сквозной номер и публикует в общий список на 7 дней. Используется и
    вручную (капитан жмёт «Зафиксировать сейчас» раньше срока), и автоматически фоновой
    задачей clan_tournament_loop по истечении 6-часового окна сбора. Возвращает свежие
    данные турнира при успехе, иначе None (турнир уже не в funding — гонка или не найден).
    """
    url = f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            t = await resp.json()
        if not isinstance(t, dict) or t.get('status') != 'funding':
            return None
        participants = t.get('participantsA') or {}
        if not participants:
            # При текущей конструкции (запись создаётся только вместе с первым же
            # оплаченным взносом капитана) это не должно происходить — подстраховка.
            return None
        number = await _next_tournament_number(session, base)
        if number is None:
            return None
        now_ms = int(time_module.time() * 1000)
        t['status'] = 'open'
        t['requiredCount'] = len(participants)
        t['number'] = number
        t['publishedAt'] = now_ms
        t['listExpiresAt'] = now_ms + 7 * 24 * 3600 * 1000
        headers = {"If-Match": etag} if etag else {}
        async with session.put(url, json=t, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status in (200, 204):
                return t
            return None
    return None


async def _clan_has_active_match(session, base, clan_id, exclude_id=None):
    """
    True, если у клана уже есть турнир в статусе matching/running — как инициатор, так и
    принимающая сторона считаются. Реализует правило «у клана может идти только 1 активный
    турнир с соперником одновременно», не мешая при этом иметь сколько угодно параллельных
    turnиров в funding/open (свой сбор без соперника — не ограничен).
    """
    if not clan_id:
        return False
    async with session.get(f"{base}/clan_tournaments.json{FB_AUTH}") as resp:
        all_t = await resp.json()
    all_t = all_t or {}
    for tid, t in all_t.items():
        if exclude_id and tid == exclude_id:
            continue
        if not isinstance(t, dict) or t.get('status') not in ('matching', 'running'):
            continue
        if t.get('initiatorClanId') == clan_id or t.get('acceptedByClanId') == clan_id:
            return True
    return False


async def _clan_unresolved_tournament_info(session, base, clan_id):
    """
    Защита ставок в клановых турнирах: капитан не может ни распустить клан, ни выгнать
    конкретного участника, пока у клана есть незавершённый турнир (funding/open/matching/
    running — то есть банк ещё не выплачен и не возвращён). Возвращает (has_unresolved,
    paid_pids): has_unresolved — есть ли у клана вообще такой турнир (для disband — неважно,
    чей именно взнос, важно что деньги клана в игре); paid_pids — множество pid участников
    ИМЕННО ЭТОГО клана с оплаченным взносом хоть в одном таком турнире (для точечной защиты
    при kick конкретного игрока). settled/expired не защищены — там банк уже посчитан либо
    подлежит ручному возврату, роспуск/удаление больше ничего не портит.
    """
    if not clan_id:
        return False, set()
    async with session.get(f"{base}/clan_tournaments.json{FB_AUTH}") as resp:
        all_t = await resp.json()
    all_t = all_t or {}
    has_unresolved = False
    paid_pids = set()
    for tid, t in all_t.items():
        if not isinstance(t, dict) or t.get('status') in ('settled', 'expired'):
            continue
        is_initiator = t.get('initiatorClanId') == clan_id
        is_acceptor = t.get('acceptedByClanId') == clan_id
        if not is_initiator and not is_acceptor:
            continue
        has_unresolved = True
        participants = t.get('participantsA') if is_initiator else t.get('participantsB')
        paid_pids.update((participants or {}).keys())
    return has_unresolved, paid_pids


async def _start_tournament_race(session, base, tournament_id):
    """
    Переводит турнир matching -> running, как только принимающая сторона полностью
    укомплектована (participantsB достиг requiredCount): берёт снэпшот caught каждого
    участника обеих команд (catchBaseline) и запускает 48-часовой таймер гонки. Вызывается
    синхронно сразу после последнего нужного взноса принимающей стороны; из фонового цикла
    вызывается ещё раз как идемпотентная подстраховка на случай, если синхронный вызов по
    какой-то причине не сработал (сам себя не запустит повторно — статус уже не matching).
    """
    url = f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            tdata = await resp.json()
        if not isinstance(tdata, dict) or tdata.get('status') != 'matching':
            return None
        required = tdata.get('requiredCount', 0)
        participants_a = tdata.get('participantsA') or {}
        participants_b = tdata.get('participantsB') or {}
        if not required or len(participants_b) < required:
            return None
        all_pids = list(participants_a.keys()) + list(participants_b.keys())

        async def _fetch_caught(p):
            async with session.get(f"{base}/leaderboard/{p}/caught.json{FB_AUTH}") as r:
                v = await r.json()
                return p, (v or 0)

        results = await asyncio.gather(*[_fetch_caught(p) for p in all_pids])
        baseline = {p: v for p, v in results}
        now_ms = int(time_module.time() * 1000)
        tdata['status'] = 'running'
        tdata['matchStartedAt'] = now_ms
        tdata['matchEndsAt'] = now_ms + 48 * 3600 * 1000
        tdata['catchBaseline'] = baseline
        headers = {"If-Match": etag} if etag else {}
        async with session.put(url, json=tdata, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status not in (200, 204):
                return None
            # Уведомляем всех участников обеих команд — без t()/bilingual, тем же
            # способом, что и остальные пуши третьим лицам без контекста initData
            # (например, "у тебя появился реферер" в ветке rb:).
            init_name = tdata.get('initiatorClanName', '')
            acc_name = tdata.get('acceptedByClanName', '')
            number = tdata.get('number')
            for p in all_pids:
                m = participants_a.get(p) or participants_b.get(p) or {}
                uid = m.get('userId')
                if not uid:
                    continue
                try:
                    await bot.send_message(uid,
                        f"🏁 Старт! Турнир #{number} между «{init_name}» и «{acc_name}» начался — 48 часов на улов рыбы. Следи за live-счётом во вкладке «Клан».")
                except Exception:
                    pass
            return tdata
    return None


async def _expire_matching_window(session, base, tournament_id):
    """
    Переводит турнир matching -> open обратно, если принимающая сторона не успела
    собрать полный состав за 2 часа (matchingDeadline истёк, а participantsB так и не
    дотянул до requiredCount). Турнир возвращается в публичный список — listExpiresAt
    НЕ сбрасывается (это первоначальные 7 дней публикации, а не окно на этот матч),
    так что если исходное окно уже истекло тоже, турнир просто больше никому не
    покажется в /clan_tournaments_open. Тех, кто уже успел заплатить как сторона B,
    нужно вернуть вручную — их список уходит админу и в саппорт-группу. Идемпотентна:
    статус уже не matching после первого успешного вызова, повторный вызов — no-op.
    """
    url = f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            tdata = await resp.json()
        if not isinstance(tdata, dict) or tdata.get('status') != 'matching':
            return None
        required = tdata.get('requiredCount', 0)
        participants_b = tdata.get('participantsB') or {}
        if required and len(participants_b) >= required:
            return None  # успел укомплектоваться — это уже не наш случай, займётся safety-net запуска
        refund_list = [
            (v.get('name', ''), v.get('username', ''), v.get('amount', 0))
            for v in participants_b.values() if isinstance(v, dict)
        ]
        failed_clan_name = tdata.get('acceptedByClanName', '')
        tdata['status'] = 'open'
        tdata.pop('acceptedByClanId', None)
        tdata.pop('acceptedByClanName', None)
        tdata.pop('acceptingCaptainId', None)
        tdata.pop('participantsB', None)
        tdata.pop('matchingDeadline', None)
        headers = {"If-Match": etag} if etag else {}
        async with session.put(url, json=tdata, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status not in (200, 204):
                return None
            if refund_list:
                lines = "\n".join(f"— {n or ('@' + u if u else 'без имени')} — {a}⭐" for n, u, a in refund_list)
                text = (f"⏳ Окно сбора соперника (2ч) истекло для турнира #{tdata.get('number')} "
                        f"(клан-претендент «{failed_clan_name}» не успел собрать состав). "
                        f"Турнир возвращён в список. Нужен ручной возврат звёзд:\n{lines}")
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, text)
                    except Exception:
                        pass
                if SUPPORT_GROUP_ID:
                    try:
                        await bot.send_message(SUPPORT_GROUP_ID, text)
                    except Exception:
                        pass
            return tdata
    return None


async def _tournament_live_catches(session, base, tdata):
    """
    Живой счёт гонки: для каждого участника турнира считает улов с момента старта —
    leaderboard/{pid}/caught минус его снэпшот в catchBaseline. Используется экраном
    «Клан-Турниры», пока турнир в статусе running (и не мешает settled — там счёт просто
    покажет финальное значение на момент последнего запроса, settlement его не трогает).
    """
    baseline = tdata.get('catchBaseline') or {}
    participants_a = tdata.get('participantsA') or {}
    participants_b = tdata.get('participantsB') or {}
    all_pids = list(participants_a.keys()) + list(participants_b.keys())
    if not all_pids:
        return {}

    async def _fetch(p):
        async with session.get(f"{base}/leaderboard/{p}/caught.json{FB_AUTH}") as r:
            v = await r.json()
            return p, max(0, (v or 0) - baseline.get(p, 0))

    results = await asyncio.gather(*[_fetch(p) for p in all_pids])
    return {p: v for p, v in results}


def _tour_payer_label(v):
    """Ник для строк в списках «на ручную выплату/возврат» — имя, иначе @username, иначе ID."""
    if not isinstance(v, dict):
        return '?'
    return v.get('name') or (('@' + v.get('username')) if v.get('username') else f"ID:{v.get('userId')}")


async def _settle_tournament(session, base, tournament_id):
    """
    Переводит турнир running -> settled по истечении 48-часового окна гонки: сравнивает
    суммарный улов команд (тот же способ подсчёта, что и у live-счёта на экране «Турниры»)
    и считает выплату по формуле «банк минус 10% комиссии, делённое на число победителей»,
    которая при равных составах и ставках алгебраически сворачивается в 1.8×ставка на
    человека (см. документацию). Рассылает участникам обеих команд итог (победа/поражение),
    а админу и в SUPPORT_GROUP_ID — список «ник — сумма» для ручной отправки звёзд
    победителям (выплаты в этом боте всегда ручные, как и вывод GRAM/джекпот).
    Ничьей по правилам не бывает (капитан подтвердил — составы и ставки одинаковые, но
    улов у команд разный практически всегда), однако на случай статистического совпадения
    гонка всё равно закрывается (иначе она бы висела в running вечно), просто без выплаты —
    админу уходит алерт на ручное решение по банку.
    """
    url = f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            tdata = await resp.json()
        if not isinstance(tdata, dict) or tdata.get('status') != 'running':
            return None
        participants_a = tdata.get('participantsA') or {}
        participants_b = tdata.get('participantsB') or {}
        try:
            live = await _tournament_live_catches(session, base, tdata)
        except Exception:
            live = {}
        score_a = sum(live.get(p, 0) for p in participants_a.keys())
        score_b = sum(live.get(p, 0) for p in participants_b.keys())
        amount = tdata.get('amountPerPerson', 0)
        payout = round(amount * 1.8 * 100) / 100
        is_tie = score_a == score_b
        winner_clan_id = None
        winner_participants = {}
        if not is_tie:
            winner_clan_id = tdata.get('initiatorClanId') if score_a > score_b else tdata.get('acceptedByClanId')
            winner_participants = participants_a if score_a > score_b else participants_b

        now_ms = int(time_module.time() * 1000)
        tdata['status'] = 'settled'
        tdata['settledAt'] = now_ms
        tdata['finalScoreA'] = score_a
        tdata['finalScoreB'] = score_b
        tdata['finalCatches'] = live  # замороженный улов каждого участника — для построчного отображения на экране
        tdata['winnerClanId'] = winner_clan_id
        tdata['payout'] = 0 if is_tie else payout
        tdata['tie'] = is_tie
        headers = {"If-Match": etag} if etag else {}
        async with session.put(url, json=tdata, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status not in (200, 204):
                return None

            init_name = tdata.get('initiatorClanName', '')
            acc_name = tdata.get('acceptedByClanName', '')
            number = tdata.get('number')

            if is_tie:
                bank = (len(participants_a) + len(participants_b)) * amount
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID,
                            f"⚖️ Турнир #{number} («{init_name}» vs «{acc_name}») завершился НИЧЬЕЙ ({score_a}:{score_b}) — по правилам такого быть не должно, выплата не посчитана. Реши вручную, что делать с банком {bank}⭐ (tournament_id: {tournament_id}).")
                    except Exception:
                        pass
                return tdata

            for p, v in participants_a.items():
                uid = v.get('userId') if isinstance(v, dict) else None
                if not uid:
                    continue
                won = winner_clan_id == tdata.get('initiatorClanId')
                try:
                    await bot.send_message(uid,
                        f"🏆 Турнир #{number} завершён — победа! «{init_name}» {score_a}:{score_b} «{acc_name}». Тебе начислят {payout}⭐ — жди звёзды от администратора."
                        if won else
                        f"😔 Турнир #{number} завершён — поражение. «{init_name}» {score_a}:{score_b} «{acc_name}». В следующий раз повезёт!")
                except Exception:
                    pass
            for p, v in participants_b.items():
                uid = v.get('userId') if isinstance(v, dict) else None
                if not uid:
                    continue
                won = winner_clan_id == tdata.get('acceptedByClanId')
                try:
                    await bot.send_message(uid,
                        f"🏆 Турнир #{number} завершён — победа! «{acc_name}» {score_b}:{score_a} «{init_name}». Тебе начислят {payout}⭐ — жди звёзды от администратора."
                        if won else
                        f"😔 Турнир #{number} завершён — поражение. «{acc_name}» {score_b}:{score_a} «{init_name}». В следующий раз повезёт!")
                except Exception:
                    pass

            winner_name = init_name if winner_clan_id == tdata.get('initiatorClanId') else acc_name
            lines = [f"— {_tour_payer_label(v)} — {payout}⭐" for v in winner_participants.values()]
            text = (f"🏆 Турнир #{number} завершён! Победил клан «{winner_name}» ({max(score_a, score_b)}:{min(score_a, score_b)}). "
                    f"Нужно вручную отправить звёзды победителям:\n" + "\n".join(lines))
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID, text)
                except Exception:
                    pass
            if SUPPORT_GROUP_ID:
                try:
                    await bot.send_message(SUPPORT_GROUP_ID, text)
                except Exception:
                    pass
            return tdata
    return None


async def _expire_open_tournament(session, base, tournament_id):
    """
    Переводит турнир open -> expired, если за 7 дней публикации в общем списке никто не
    принял вызов. Банк клана-инициатора нужно вернуть вручную — список «ник — сумма» уходит
    админу и в SUPPORT_GROUP_ID, тем же способом, что и остальные ручные выплаты/возвраты
    в этом боте.
    """
    url = f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}"
    for _ in range(6):
        async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            tdata = await resp.json()
        if not isinstance(tdata, dict) or tdata.get('status') != 'open':
            return None
        participants_a = tdata.get('participantsA') or {}
        now_ms = int(time_module.time() * 1000)
        tdata['status'] = 'expired'
        tdata['settledAt'] = now_ms
        headers = {"If-Match": etag} if etag else {}
        async with session.put(url, json=tdata, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status not in (200, 204):
                return None
            lines = [f"— {_tour_payer_label(v)} — {v.get('amount', tdata.get('amountPerPerson', 0))}⭐" for v in participants_a.values()]
            text = (f"⏳ Турнир #{tdata.get('number')} («{tdata.get('initiatorClanName','')}») истёк — за 7 дней его никто не принял. "
                    f"Нужен ручной возврат звёзд:\n" + "\n".join(lines))
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID, text)
                except Exception:
                    pass
            if SUPPORT_GROUP_ID:
                try:
                    await bot.send_message(SUPPORT_GROUP_ID, text)
                except Exception:
                    pass
            return tdata
    return None


async def _mutate_clan_members(session, base, clan_id, mutate_fn):
    """
    Читает clans/{clanId} с ETag, даёт mutate_fn изменить словарь members на месте (добавить
    или убрать участника), пересчитывает membersCount = len(members) и пишет обратно с retry
    при конфликте (412) — тот же приём оптимистичной блокировки, что уже используется для
    saves/{pid} в /actions и /sync. Возвращает свежий clan_data после успешной записи, либо
    None, если клана не существует или запись не удалась после всех попыток.
    """
    clan_url = f"{base}/clans/{clan_id}.json{FB_AUTH}"
    for _ in range(5):
        async with session.get(clan_url, headers={"X-Firebase-ETag": "true"}) as resp:
            etag = resp.headers.get("ETag")
            clan_data = await resp.json()
        if not isinstance(clan_data, dict):
            return None
        members = dict(clan_data.get('members') or {})
        mutate_fn(members)
        clan_data['members'] = members
        clan_data['membersCount'] = len(members)
        headers = {"If-Match": etag} if etag else {}
        async with session.put(clan_url, json=clan_data, headers=headers) as put_resp:
            if put_resp.status == 412:
                continue
            if put_resp.status in (200, 204):
                return clan_data
            return None
    return None


async def clan_status(request):
    """
    Тестовый эндпоинт клановой фичи. Говорит фронту, показывать ли вкладку "Клан"
    этому игроку (is_clan_tester) — обычным игрокам всегда возвращает isTester:false,
    так что даже прямой запрос к этому пути ничего не открывает раньше времени.
    Если у тестера уже есть клан — сразу отдаёт его данные, чтобы вкладка не мигала
    пустым состоянием при открытии. Если клана нет — вместо этого отдаёт список
    активных входящих приглашений (см. /clan_invite и /clan_invite_respond).
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'ok': True, 'isTester': False, 'clan': None, 'invites': []}, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    clan = None
    invites = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if clan_id:
                async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                    clan_data = await resp2.json()
                if isinstance(clan_data, dict):
                    clan = _clan_public(clan_id, clan_data, real_user_id)
                    # Флаг для фронта — блокировать/подсвечивать кнопку «Распустить клан» и
                    # «Удалить» у конкретных участников заранее, а не только по ошибке сервера.
                    has_unresolved, paid_pids = await _clan_unresolved_tournament_info(session, base, clan_id)
                    clan['hasUnresolvedTournament'] = has_unresolved
                    clan['tournamentLockedPids'] = list(paid_pids)
                    clan['iAmLocked'] = pid in paid_pids
            else:
                async with session.get(f"{base}/pending_clan_invites/{pid}.json{FB_AUTH}") as iresp:
                    raw_invites = await iresp.json()
                if isinstance(raw_invites, dict):
                    for cid, inv in raw_invites.items():
                        if not isinstance(inv, dict):
                            continue
                        invites.append({
                            'clanId': cid,
                            'clanName': inv.get('clanName', ''),
                            'fromCaptainId': inv.get('fromCaptainId'),
                            'fromUsername': inv.get('fromUsername', ''),
                            'sentAt': inv.get('sentAt', 0),
                        })
                    invites.sort(key=lambda x: x['sentAt'], reverse=True)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'isTester': True, 'clan': clan, 'invites': invites}, headers=CORS)


async def clan_create(request):
    """
    Создание клана — создатель сразу становится капитаном и единственным участником.
    Доступно только тестовым аккаунтам из CLAN_TESTERS/CLAN_TEST_USERNAMES; для остальных
    фронт даже не показывает кнопку, но и сам эндпоинт на всякий случай отказывает, если
    до него всё же достучаться напрямую.
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)

    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    name = str(data.get('name', '')).strip()
    # Только латинские буквы и цифры — без пробелов, кириллицы и спецсимволов.
    # isascii()+isalnum() вместе дают ровно A-Z/a-z/0-9 (isalnum() один пропустил бы
    # и юникодные буквы/цифры, поэтому нужны обе проверки).
    if not name.isascii() or not name.isalnum():
        return web.json_response(
            {'error': 'название — только латинские буквы и цифры, без пробелов'},
            status=400, headers=CORS
        )
    if len(name) < CLAN_NAME_MIN or len(name) > CLAN_NAME_MAX:
        return web.json_response(
            {'error': f'название должно быть от {CLAN_NAME_MIN} до {CLAN_NAME_MAX} символов'},
            status=400, headers=CORS
        )

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    now_ms = int(time.time() * 1000)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/banned/{real_user_id}.json{FB_AUTH}") as bresp:
                if await bresp.json():
                    return web.json_response({'error': 'account banned'}, status=403, headers=CORS)

            saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
            async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                etag = resp.headers.get("ETag")
                sv = await resp.json()
            sv = sv or {}
            if sv.get('clanId'):
                return web.json_response({'error': 'ты уже состоишь в клане'}, status=400, headers=CORS)

            async with session.get(f"{base}/clans.json{FB_AUTH}") as cresp:
                existing_clans = await cresp.json()
            existing_clans = existing_clans or {}
            name_lower = name.lower()
            for _cid, _cdata in existing_clans.items():
                if isinstance(_cdata, dict) and str(_cdata.get('name', '')).lower() == name_lower:
                    return web.json_response({'error': 'клан с таким названием уже есть'}, status=400, headers=CORS)

            player_name = sv.get('playerName') or real_user.get('username') or real_user.get('first_name') or f"Игрок {real_user_id}"
            player_username = real_user.get('username', '')
            if not player_username:
                # initData не всегда содержит username (например, если игрок открыл
                # мини-апп способом, где Telegram его не передаёт) — подстраховываемся
                # тем же полем leaderboard/{pid}/username, которым уже пользуется
                # /clan_referrals и которое туда пишет обычный игровой /sync.
                try:
                    async with session.get(f"{base}/leaderboard/{pid}/username.json{FB_AUTH}") as uresp:
                        player_username = (await uresp.json()) or ''
                except Exception:
                    pass
            clan_payload = {
                'name': name,
                'captainId': real_user_id,
                'captainPid': pid,
                'membersCount': 1,
                'maxMembers': 2,
                'members': {
                    pid: {'userId': real_user_id, 'name': player_name, 'username': player_username, 'role': 'captain', 'joinedAt': now_ms}
                },
                'createdAt': now_ms,
            }
            async with session.post(f"{base}/clans.json{FB_AUTH}", json=clan_payload) as presp:
                if presp.status not in (200, 201):
                    return web.json_response({'error': f'clan create failed: {presp.status}'}, status=500, headers=CORS)
                push_result = await presp.json()
            clan_id = push_result.get('name') if isinstance(push_result, dict) else None
            if not clan_id:
                return web.json_response({'error': 'internal: no clan id'}, status=500, headers=CORS)

            # Firebase REST API поддерживает If-Match ТОЛЬКО с PUT, не с PATCH (PATCH с
            # If-Match всегда возвращает 400) — поэтому мёржим изменения поверх уже
            # прочитанного sv на стороне Python и пишем целиком через PUT.
            save_headers = {"If-Match": etag} if etag else {}
            merged_sv = dict(sv)
            merged_sv['clanId'] = clan_id
            merged_sv['clanName'] = name
            async with session.put(saves_url, json=merged_sv, headers=save_headers) as presp2:
                if presp2.status == 412:
                    # Кто-то параллельно успел вступить/создать клан между чтением и записью —
                    # откатываем только что созданный клан, чтобы не плодить клан-сироту без игроков.
                    await session.delete(f"{base}/clans/{clan_id}.json{FB_AUTH}")
                    return web.json_response({'error': 'не удалось создать — попробуй ещё раз'}, status=409, headers=CORS)
                if presp2.status not in (200, 204):
                    await session.delete(f"{base}/clans/{clan_id}.json{FB_AUTH}")
                    return web.json_response({'error': f'saves PUT failed: {presp2.status}'}, status=500, headers=CORS)

            # Зеркалим clanId в публичный leaderboard — по нему строится список рефералов
            # для приглашения (там нельзя ходить в приватные saves/ на каждого реферала).
            # Лучшая попытка: если не получилось, сам процесс создания клана уже не откатываем.
            try:
                await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={'clanId': clan_id})
            except Exception:
                pass
            # Очищаем входящие приглашения — капитан своего только что созданного клана
            # больше не может принять чьё-то чужое приглашение.
            try:
                await session.delete(f"{base}/pending_clan_invites/{pid}.json{FB_AUTH}")
            except Exception:
                pass
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'clan': {
        'id': clan_id, 'name': name, 'membersCount': 1, 'maxMembers': 2,
        'captainId': real_user_id, 'isCaptain': True, 'createdAt': now_ms,
        'members': [{'pid': pid, 'userId': real_user_id, 'name': player_name, 'username': player_username, 'role': 'captain', 'joinedAt': now_ms}],
    }}, headers=CORS)


async def clan_referrals(request):
    """
    Список рефералов капитана для приглашения в клан — сводит referrals/by/{captainId}
    (кого капитан привёл) с публичным leaderboard (ник, улов, последняя активность) и
    отфильтровывает тех, кто уже состоит в каком-либо клане. Только капитан своего клана
    может звать — рядовым участникам кнопка на фронте не показывается, но и здесь проверяем.
    Пагинация — по 50 штук на страницу через offset.
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    try:
        offset = max(0, int(data.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    limit = 50

    import aiohttp, asyncio
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != real_user_id:
                return web.json_response({'error': 'приглашать может только капитан'}, status=403, headers=CORS)

            async with session.get(f"{base}/referrals/by/{real_user_id}.json{FB_AUTH}") as rresp:
                referred = await rresp.json()
            referred = referred or {}
            target_ids = [str(t) for t in referred.keys() if str(t) != str(real_user_id)]

            async def fetch_one(target_id):
                async with session.get(f"{base}/leaderboard/tg_{target_id}.json{FB_AUTH}") as lresp:
                    return target_id, await lresp.json()

            results = await asyncio.gather(*[fetch_one(t) for t in target_ids]) if target_ids else []
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    entries = []
    for target_id, lb in results:
        if not isinstance(lb, dict):
            continue
        if lb.get('clanId'):  # уже состоит в каком-то клане — не показываем в списке приглашения
            continue
        username = lb.get('username') or ''
        entries.append({
            'pid': f"tg_{target_id}",
            'userId': int(target_id) if str(target_id).isdigit() else target_id,
            'username': username,
            'displayName': f"@{username}" if username else (lb.get('firstName') or f"ID:{target_id}"),
            'caught': lb.get('caught', 0),
            'ts': lb.get('ts', 0),
        })
    entries.sort(key=lambda e: e['ts'], reverse=True)

    total = len(entries)
    page = entries[offset:offset + limit]
    return web.json_response({
        'ok': True, 'referrals': page, 'offset': offset, 'limit': limit,
        'total': total, 'hasMore': offset + limit < total,
    }, headers=CORS)


async def clan_invite(request):
    """Капитан приглашает конкретного реферала в свой клан — создаёт запись в pending_clan_invites."""
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
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    target_user_id = str(data.get('target_user_id', '')).strip()
    if not target_user_id or not target_user_id.isdigit():
        return web.json_response({'error': 'invalid target'}, status=400, headers=CORS)
    target_pid = f"tg_{target_user_id}"

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    now_ms = int(time.time() * 1000)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != real_user_id:
                return web.json_response({'error': 'приглашать может только капитан'}, status=403, headers=CORS)

            members_count = clan_data.get('membersCount', len(clan_data.get('members') or {}))
            max_members = clan_data.get('maxMembers', 2)
            if members_count >= max_members:
                return web.json_response({'error': 'в клане нет свободных мест'}, status=400, headers=CORS)

            async with session.get(f"{base}/referrals/by/{real_user_id}/{target_user_id}.json{FB_AUTH}") as fresp:
                is_referral = await fresp.json()
            if not is_referral:
                return web.json_response({'error': 'этот игрок не в списке твоих рефералов'}, status=400, headers=CORS)

            async with session.get(f"{base}/saves/{target_pid}/clanId.json{FB_AUTH}") as tresp:
                target_clan_id = await tresp.json()
            if target_clan_id:
                return web.json_response({'error': 'игрок уже состоит в клане'}, status=400, headers=CORS)

            invite_payload = {
                'fromCaptainId': real_user_id,
                'fromUsername': _display_handle(real_user),
                'clanName': clan_data.get('name', ''),
                'sentAt': now_ms,
            }
            await session.put(f"{base}/pending_clan_invites/{target_pid}/{clan_id}.json{FB_AUTH}", json=invite_payload)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def clan_invite_respond(request):
    """Приглашённый принимает или отклоняет конкретное входящее приглашение в клан."""
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
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    clan_id = str(data.get('clan_id', '')).strip()
    action = str(data.get('action', '')).strip()
    if not clan_id or action not in ('accept', 'decline'):
        return web.json_response({'error': 'invalid request'}, status=400, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            invite_url = f"{base}/pending_clan_invites/{pid}/{clan_id}.json{FB_AUTH}"
            async with session.get(invite_url) as iresp:
                invite = await iresp.json()
            if not isinstance(invite, dict):
                return web.json_response({'error': 'приглашение не найдено'}, status=404, headers=CORS)

            if action == 'decline':
                await session.delete(invite_url)
                return web.json_response({'ok': True}, headers=CORS)

            # accept
            saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
            async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                etag = resp.headers.get("ETag")
                sv = await resp.json()
            sv = sv or {}
            if sv.get('clanId'):
                return web.json_response({'error': 'ты уже состоишь в клане'}, status=400, headers=CORS)

            async with session.get(f"{base}/banned/{real_user_id}.json{FB_AUTH}") as bresp:
                if await bresp.json():
                    return web.json_response({'error': 'account banned'}, status=403, headers=CORS)

            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as cresp:
                clan_data_check = await cresp.json()
            if not isinstance(clan_data_check, dict):
                await session.delete(invite_url)
                return web.json_response({'error': 'этого клана больше не существует'}, status=400, headers=CORS)
            if clan_data_check.get('membersCount', 0) >= clan_data_check.get('maxMembers', 2):
                return web.json_response({'error': 'в клане уже нет свободных мест'}, status=400, headers=CORS)

            player_name = sv.get('playerName') or real_user.get('username') or real_user.get('first_name') or f"Игрок {real_user_id}"
            player_username = real_user.get('username', '')
            if not player_username:
                # См. аналогичную подстраховку в clan_create — initData не всегда
                # содержит username, подтягиваем его из leaderboard/{pid}/username.
                try:
                    async with session.get(f"{base}/leaderboard/{pid}/username.json{FB_AUTH}") as uresp:
                        player_username = (await uresp.json()) or ''
                except Exception:
                    pass

            def _add_member(members):
                members[pid] = {'userId': real_user_id, 'name': player_name, 'username': player_username, 'role': 'member', 'joinedAt': int(time_module.time() * 1000)}

            clan_data = await _mutate_clan_members(session, base, clan_id, _add_member)
            if clan_data is None:
                return web.json_response({'error': 'не удалось вступить — попробуй ещё раз'}, status=409, headers=CORS)

            # If-Match работает только с PUT, не с PATCH (PATCH+If-Match всегда 400) —
            # мёржим поверх уже прочитанного sv и пишем целиком.
            save_headers = {"If-Match": etag} if etag else {}
            merged_sv = dict(sv)
            merged_sv['clanId'] = clan_id
            merged_sv['clanName'] = clan_data.get('name', '')
            async with session.put(saves_url, json=merged_sv, headers=save_headers) as presp:
                if presp.status not in (200, 204):
                    # 412 (гонка) или любая другая ошибка — откатываем добавление в
                    # участники клана, вступление не подтвердилось.
                    def _remove_member(members):
                        members.pop(pid, None)
                    await _mutate_clan_members(session, base, clan_id, _remove_member)
                    return web.json_response({'error': 'не удалось вступить — попробуй ещё раз'}, status=409, headers=CORS)

            try:
                await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={'clanId': clan_id})
            except Exception:
                pass

            await session.delete(f"{base}/pending_clan_invites/{pid}.json{FB_AUTH}")
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'clan': _clan_public(clan_id, clan_data, real_user_id)}, headers=CORS)


async def clan_kick(request):
    """Капитан удаляет участника из своего клана — освобождённый слот остаётся оплаченным (maxMembers не меняется)."""
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
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    target_user_id = str(data.get('target_user_id', '')).strip()
    if not target_user_id or not target_user_id.isdigit():
        return web.json_response({'error': 'invalid target'}, status=400, headers=CORS)
    if target_user_id == str(real_user_id):
        return web.json_response({'error': 'нельзя удалить самого себя'}, status=400, headers=CORS)
    target_pid = f"tg_{target_user_id}"

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != real_user_id:
                return web.json_response({'error': 'удалять участников может только капитан'}, status=403, headers=CORS)

            members = clan_data.get('members') or {}
            if target_pid not in members:
                return web.json_response({'error': 'этот игрок не состоит в клане'}, status=400, headers=CORS)

            _, paid_pids = await _clan_unresolved_tournament_info(session, base, clan_id)
            if target_pid in paid_pids:
                return web.json_response(
                    {'error': 'у этого игрока есть оплаченный взнос в незавершённом турнире — дождись его завершения'},
                    status=400, headers=CORS
                )

            def _remove_member(m):
                m.pop(target_pid, None)

            new_clan_data = await _mutate_clan_members(session, base, clan_id, _remove_member)
            if new_clan_data is None:
                return web.json_response({'error': 'не удалось удалить — попробуй ещё раз'}, status=409, headers=CORS)

            async with session.get(f"{base}/saves/{target_pid}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as tresp:
                tetag = tresp.headers.get("ETag")
                target_sv = await tresp.json()
            target_sv = dict(target_sv or {})
            target_sv['clanId'] = None
            target_sv['clanName'] = None
            # If-Match работает только с PUT, не с PATCH (PATCH+If-Match всегда 400) —
            # мёржим поверх прочитанного target_sv и пишем целиком; при 412 (гонка —
            # игрок сам успел что-то сохранить) не ретраим — не критично, next /sync
            # игрока подтянет актуальное состояние клана через отдельные проверки.
            theaders = {"If-Match": tetag} if tetag else {}
            await session.put(f"{base}/saves/{target_pid}.json{FB_AUTH}", json=target_sv, headers=theaders)
            try:
                await session.patch(f"{base}/leaderboard/{target_pid}.json{FB_AUTH}", json={'clanId': None})
            except Exception:
                pass
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def clan_leave(request):
    """
    Обычный участник (не капитан) сам покидает клан — та же механика очистки, что и в
    clan_kick, только целью выступает сам вызывающий. Капитан выйти так не может — у клана
    не может остаться без капитана, ему остаётся только /clan_disband. Та же защита ставки,
    что и в clan_kick: если у игрока есть оплаченный взнос в незавершённом турнире, выйти
    нельзя, пока турнир не завершится (см. _clan_unresolved_tournament_info).
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                clan_data = await resp2.json()
            if not isinstance(clan_data, dict):
                return web.json_response({'error': 'клан не найден'}, status=400, headers=CORS)
            if clan_data.get('captainId') == real_user_id:
                return web.json_response(
                    {'error': 'капитан не может покинуть свой клан — вместо этого распусти его'},
                    status=400, headers=CORS
                )

            members = clan_data.get('members') or {}
            if pid not in members:
                return web.json_response({'error': 'ты не состоишь в этом клане'}, status=400, headers=CORS)

            _, paid_pids = await _clan_unresolved_tournament_info(session, base, clan_id)
            if pid in paid_pids:
                return web.json_response(
                    {'error': 'нельзя покинуть клан — у тебя есть оплаченный взнос в незавершённом турнире, дождись его завершения'},
                    status=400, headers=CORS
                )

            def _remove_member(m):
                m.pop(pid, None)

            new_clan_data = await _mutate_clan_members(session, base, clan_id, _remove_member)
            if new_clan_data is None:
                return web.json_response({'error': 'не удалось выйти — попробуй ещё раз'}, status=409, headers=CORS)

            async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as tresp:
                tetag = tresp.headers.get("ETag")
                target_sv = await tresp.json()
            target_sv = dict(target_sv or {})
            target_sv['clanId'] = None
            target_sv['clanName'] = None
            theaders = {"If-Match": tetag} if tetag else {}
            await session.put(f"{base}/saves/{pid}.json{FB_AUTH}", json=target_sv, headers=theaders)
            try:
                await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={'clanId': None})
            except Exception:
                pass
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def clan_disband(request):
    """
    Капитан распускает клан целиком — можно в любой момент, независимо от того, сколько
    оплаченных слотов уже куплено (это расходы капитана, он вправе ими не пользоваться).
    Все участники (включая самого капитана) автоматически освобождаются — их
    saves/{pid}.clanId/clanName очищаются, клан удаляется.
    Исключение — незавершённый турнир (funding/open/matching/running): пока в клан-турнирах
    есть чьи-то оплаченные звёздами взносы, роспуск заблокирован (см. _clan_unresolved_
    tournament_info) — иначе банк было бы некому вернуть. После settled/expired роспуск
    снова свободен.
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as resp2:
                clan_data = await resp2.json()
            if not isinstance(clan_data, dict) or clan_data.get('captainId') != real_user_id:
                return web.json_response({'error': 'распустить клан может только капитан'}, status=403, headers=CORS)

            has_unresolved, _ = await _clan_unresolved_tournament_info(session, base, clan_id)
            if has_unresolved:
                return web.json_response(
                    {'error': 'нельзя распустить клан — есть незавершённый турнир с оплаченными взносами, дождись его завершения'},
                    status=400, headers=CORS
                )

            members = clan_data.get('members') or {}
            for member_pid in members.keys():
                # Best effort по каждому участнику — одна неудачная запись (например,
                # конфликт версии с параллельным /sync игрока) не должна блокировать
                # ни остальных участников, ни сам роспуск клана.
                try:
                    async with session.get(f"{base}/saves/{member_pid}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as mresp:
                        metag = mresp.headers.get("ETag")
                        msv = await mresp.json()
                    msv = dict(msv or {})
                    msv['clanId'] = None
                    msv['clanName'] = None
                    mheaders = {"If-Match": metag} if metag else {}
                    await session.put(f"{base}/saves/{member_pid}.json{FB_AUTH}", json=msv, headers=mheaders)
                except Exception:
                    pass
                try:
                    await session.patch(f"{base}/leaderboard/{member_pid}.json{FB_AUTH}", json={'clanId': None})
                except Exception:
                    pass

            await session.delete(f"{base}/clans/{clan_id}.json{FB_AUTH}")
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def clan_tournament_fix(request):
    """
    Капитан-инициатор вручную фиксирует свой турнир раньше 6-часового окна сбора — в
    команду войдут только те, кто успел оплатить к этому моменту (вплоть до соло-состава
    из одного капитана). Автоматическая фиксация по истечении окна делает то же самое
    через clan_tournament_loop — оба пути используют общий хелпер _fixate_tournament.
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    tournament_id = str(data.get('tournament_id', '')).strip()
    if not tournament_id:
        return web.json_response({'error': 'invalid tournament'}, status=400, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}") as tresp:
                t = await tresp.json()
            if not isinstance(t, dict):
                return web.json_response({'error': 'турнир не найден'}, status=404, headers=CORS)
            if t.get('captainId') != real_user_id:
                return web.json_response({'error': 'фиксировать турнир может только капитан-инициатор'}, status=403, headers=CORS)
            if t.get('status') != 'funding':
                return web.json_response({'error': 'турнир уже зафиксирован'}, status=400, headers=CORS)
            result = await _fixate_tournament(session, base, tournament_id)
            if result is None:
                return web.json_response({'error': 'не удалось зафиксировать — попробуй ещё раз'}, status=409, headers=CORS)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def clan_tournaments_mine(request):
    """
    Список турниров своего клана — и как инициатора, и как принявшей стороны (любой статус) —
    экран «Клан-Турниры».
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            if not clan_id:
                return web.json_response({'error': 'у тебя нет клана'}, status=400, headers=CORS)
            async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as cresp:
                clan_data = await cresp.json()
            is_captain = isinstance(clan_data, dict) and clan_data.get('captainId') == real_user_id
            async with session.get(f"{base}/clan_tournaments.json{FB_AUTH}") as tresp:
                all_t = await tresp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    all_t = all_t or {}
    out = []
    try:
        async with aiohttp.ClientSession() as session:
            for tid, t in all_t.items():
                if not isinstance(t, dict):
                    continue
                is_initiator = t.get('initiatorClanId') == clan_id
                is_acceptor = t.get('acceptedByClanId') == clan_id
                if not is_initiator and not is_acceptor:
                    continue
                role = 'initiator' if is_initiator else 'acceptor'
                my_side = 'A' if is_initiator else 'B'
                participants_a = t.get('participantsA') or {}
                participants_b = t.get('participantsB') or {}
                my_participants = participants_a if is_initiator else participants_b

                # Живой счёт запрашиваем только для реально идущей гонки — не тратим лишние
                # запросы к leaderboard на турниры в funding/open/matching, где считать нечего.
                # Для settled счёт и построчный улов уже заморожены в finalScoreA/B и
                # finalCatches самим _settle_tournament — пересчитывать не нужно (и не совсем
                # корректно: улов после финиша мог продолжить расти в личном зачёте игрока).
                live_catches = {}
                if t.get('status') == 'running':
                    try:
                        live_catches = await _tournament_live_catches(session, base, t)
                    except Exception:
                        live_catches = {}
                    score_a = sum(live_catches.get(p, 0) for p in participants_a.keys())
                    score_b = sum(live_catches.get(p, 0) for p in participants_b.keys())
                elif t.get('status') == 'settled':
                    live_catches = t.get('finalCatches') or {}
                    score_a = t.get('finalScoreA', 0)
                    score_b = t.get('finalScoreB', 0)
                else:
                    score_a = 0
                    score_b = 0

                out.append({
                    'id': tid,
                    'number': t.get('number'),
                    'status': t.get('status'),
                    'role': role,
                    'mySide': my_side,
                    'amountPerPerson': t.get('amountPerPerson', 0),
                    'initiatorClanId': t.get('initiatorClanId'),
                    'initiatorClanName': t.get('initiatorClanName', ''),
                    'acceptedByClanId': t.get('acceptedByClanId'),
                    'acceptedByClanName': t.get('acceptedByClanName', ''),
                    'participantsA': [
                        {'pid': p, 'userId': v.get('userId'), 'name': v.get('name', ''), 'username': v.get('username', ''), 'caught': live_catches.get(p, 0)}
                        for p, v in participants_a.items() if isinstance(v, dict)
                    ],
                    'participantsB': [
                        {'pid': p, 'userId': v.get('userId'), 'name': v.get('name', ''), 'username': v.get('username', ''), 'caught': live_catches.get(p, 0)}
                        for p, v in participants_b.items() if isinstance(v, dict)
                    ],
                    'scoreA': score_a,
                    'scoreB': score_b,
                    'iPaid': pid in my_participants,
                    'requiredCount': t.get('requiredCount'),
                    'fundingDeadline': t.get('fundingDeadline'),
                    'publishedAt': t.get('publishedAt'),
                    'listExpiresAt': t.get('listExpiresAt'),
                    'matchingDeadline': t.get('matchingDeadline'),
                    'matchStartedAt': t.get('matchStartedAt'),
                    'matchEndsAt': t.get('matchEndsAt'),
                    'winnerClanId': t.get('winnerClanId'),
                    'payout': t.get('payout'),
                    'tie': t.get('tie', False),
                    'settledAt': t.get('settledAt'),
                    'createdAt': t.get('createdAt', 0),
                })
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)
    out.sort(key=lambda x: x['createdAt'], reverse=True)
    return web.json_response({'ok': True, 'isCaptain': is_captain, 'tournaments': out}, headers=CORS)


async def clan_tournaments_open(request):
    """
    Публичный список турниров в статусе open — доступных для приёма другими кланами.
    Возвращает isMine/isCaptain/myClanId, чтобы фронт мог решить, показывать ли кнопку «Принять».
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
    real_user_id = real_user.get('id')
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    if not is_clan_tester(real_user_id, real_user.get('username')):
        return web.json_response({'error': 'feature not available'}, status=403, headers=CORS)

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/saves/{pid}/clanId.json{FB_AUTH}") as resp:
                clan_id = await resp.json()
            is_captain = False
            if clan_id:
                async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as cresp:
                    clan_data = await cresp.json()
                is_captain = isinstance(clan_data, dict) and clan_data.get('captainId') == real_user_id
            async with session.get(f"{base}/clan_tournaments.json{FB_AUTH}") as tresp:
                all_t = await tresp.json()
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    all_t = all_t or {}
    now = int(time.time() * 1000)
    out = []
    for tid, t in all_t.items():
        if not isinstance(t, dict) or t.get('status') != 'open':
            continue
        if t.get('listExpiresAt') and now >= t.get('listExpiresAt'):
            continue
        participants_a = t.get('participantsA') or {}
        out.append({
            'id': tid,
            'number': t.get('number'),
            'amountPerPerson': t.get('amountPerPerson', 0),
            'requiredCount': t.get('requiredCount'),
            'initiatorClanId': t.get('initiatorClanId'),
            'initiatorClanName': t.get('initiatorClanName', ''),
            'participantsCount': len(participants_a),
            'publishedAt': t.get('publishedAt'),
            'listExpiresAt': t.get('listExpiresAt'),
            'isMine': t.get('initiatorClanId') == clan_id,
            'createdAt': t.get('createdAt', 0),
        })
    out.sort(key=lambda x: x['createdAt'], reverse=True)
    return web.json_response({'ok': True, 'isCaptain': is_captain, 'myClanId': clan_id, 'tournaments': out}, headers=CORS)


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
TRANSPORT_COST = {'bike': 0, 'moped': 5000, 'car': 50000, 'truck': 300000}
TRANSPORT_CAPACITY = {'bike': 5, 'moped': 15, 'car': 40, 'truck': 100, 'rentalTruck': 200}
TRANSPORT_REPAIR_COST = {'bike': 50, 'moped': 500, 'car': 5000, 'truck': 30000}
DAILY_REWARDS = [10, 25, 50, 100, 200, 400, 1000]
LOCATION_UNLOCK_COST = {'pond': 0, 'river': 100000, 'tropics': 3000000, 'deep': 9000000, 'space': 33000000}
LOCATION_ORDER_LIST = ['pond', 'river', 'tropics', 'deep', 'space']
QUEST_BONUS_BASE = 500
RARE_FISH_PRICE = 25
RARE_FISH_INTERVAL_MS = 2 * 60 * 60 * 1000  # раз в 2 часа
RARE_FISH_DURATION_MS = 10 * 60 * 1000      # живёт 10 минут


def rare_fish_status(now_ms=None):
    """
    Расписание редкой рыбы (Осетрина) — чистая детерминированная формула от текущего
    времени, без какого-либо состояния в Firebase. Раньше расписание жило в общем узле
    rare_fish, который ЛЮБОЙ клиент мог переписать (в том числе через консоль браузера) —
    этим объяснялся баг "осетрина активна ~6 часов": сбитые часы одного устройства (или
    просто гонка нескольких клиентов, независимо решающих "пора её заспавнить") сдвигали
    общее окно на часы сразу для ВСЕХ игроков, а заодно это был готовый вектор, чтобы
    держать 15%-шанс на монеты активным вечно. Теперь и сервер, и все клиенты вычисляют
    ОДНО и то же окно независимо (см. rareFishSchedule() в index.html) — переписывать
    и подделывать нечего.
    """
    if now_ms is None:
        now_ms = int(time_module.time() * 1000)
    cycle_start = (now_ms // RARE_FISH_INTERVAL_MS) * RARE_FISH_INTERVAL_MS
    ends_at = cycle_start + RARE_FISH_DURATION_MS
    next_at = cycle_start + RARE_FISH_INTERVAL_MS
    return {'active': now_ms < ends_at, 'endsAt': ends_at, 'nextAt': next_at}
DRIED_SELL_MULT = 3
FILET_SELL_MULT_EXACT = 5
PRICE_INTERVAL_MS = 30000
WEATHER_PRICE_MULT = {'sunny': 1.0, 'cloudy': 1.1, 'rain': 1.25, 'storm': 1.5, 'perfect': 0.9}
BULK_SELL_RATE = {'fresh': 0.01, 'filet': 0.02, 'dried': 0.03}  # плоская ставка за штуку, * множитель локации


def pick_lottery_prize(mult, jackpot):
    """
    Определяет приз лотереи — точная копия весов из index.html, но теперь единственное
    место, где это решается (сервер), а не клиент. Возвращает dict с полями:
    kind ('coins'|'fish'|'salt'|'knife'|'truck_ticket'|'jackpot'), amount, label.
    """
    import random
    c1 = round(300 * mult)
    c2 = round(500 * mult)
    f1 = round(100 * mult)
    s1 = round(15 * mult)
    k1 = round(15 * mult)
    prizes = [
        {'kind': 'coins', 'amount': c1, 'label': f'🪙 {c1:,} монет', 'weight': 40},
        {'kind': 'coins', 'amount': c2, 'label': f'🪙 {c2:,} монет', 'weight': 25},
        {'kind': 'fish', 'amount': f1, 'label': f'🐟 {f1:,} рыб на склад', 'weight': 45},
        {'kind': 'salt', 'amount': s1, 'label': f'🧂 {s1:,} соли на склад', 'weight': 10},
        {'kind': 'knife', 'amount': k1, 'label': f'🔪 {k1:,} ножей на склад', 'weight': 7},
        {'kind': 'truck_ticket', 'amount': 1, 'label': '🚛 Билет на аренду грузовика (12ч)', 'weight': 3},
        {'kind': 'boot', 'amount': 0, 'label': '👢 Дырявый сапог... в следующий раз повезёт!', 'weight': 30},
        {'kind': 'jackpot', 'amount': int(jackpot), 'label': f'⭐ ДЖЕКПОТ {int(jackpot)} Stars',
         'weight': 1.616162 if jackpot >= 200 else 0.16016},
    ]
    total = sum(p['weight'] for p in prizes)
    r = random.random() * total
    acc = 0
    for p in prizes:
        acc += p['weight']
        if r <= acc:
            return p
    return prizes[0]


async def apply_lottery_prize(pid, prize, mult, grow_jackpot, username='Игрок', via='unknown'):
    """
    Применяет уже выбранный сервером приз лотереи к реальным данным игрока в Firebase.
    Используется только для платной прокрутки за Stars (successful_payment) — бесплатные
    прокрутки (за рекламу/Premium) теперь атомарны прямо внутри lottery_spin, см. там же
    подробный комментарий про гонку. Здесь та же защита: ETag-блокировка с retry на
    saves/{pid}, а не отдельные незащищённые PATCH — иначе оплаченный Stars приз мог бы
    так же тихо потеряться при гонке с обычным /actions, как терялись квест/доставка
    у других игроков.
    """
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
    async with aiohttp.ClientSession() as session:
        for attempt in range(6):
            async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                etag = resp.headers.get("ETag")
                sv = await resp.json()
            sv = sv or {}

            merged = dict(sv)
            if prize['kind'] == 'coins':
                merged['coins'] = round((float(sv.get('coins', 0) or 0) + prize['amount']) * 100) / 100
                merged['totalEarned'] = round((float(sv.get('totalEarned', 0) or 0) + prize['amount']) * 100) / 100
            elif prize['kind'] == 'fish':
                merged['caught'] = int(sv.get('caught', 0) or 0) + prize['amount']
                merged['unsoldCaught'] = round((float(sv.get('unsoldCaught', 0) or 0) + prize['amount']) * 100) / 100
            elif prize['kind'] in ('salt', 'knife'):
                loc = sv.get('loc') or 'pond'
                field = 'saltByLoc' if prize['kind'] == 'salt' else 'knifeByLoc'
                by_loc = sv.get(field) or {}
                if not isinstance(by_loc, dict):
                    by_loc = {}
                by_loc[loc] = (by_loc.get(loc) or 0) + prize['amount']
                merged[field] = by_loc
            elif prize['kind'] == 'truck_ticket':
                merged['truckTickets'] = (sv.get('truckTickets') or 0) + 1
            # 'jackpot' начисляется отдельно ниже (глобальный путь jackpot/amount)

            stats = sv.get('lotteryStats')
            stats = stats if isinstance(stats, dict) else {}
            spins_key = f"{via}Spins"
            stats[spins_key] = (stats.get(spins_key) or 0) + 1
            wins = stats.get('wins') if isinstance(stats.get('wins'), dict) else {}
            wins[prize['kind']] = (wins.get(prize['kind']) or 0) + 1
            stats['wins'] = wins
            merged['lotteryStats'] = stats

            headers = {"If-Match": etag} if etag else {}
            try:
                async with session.put(saves_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue
                    break
            except Exception:
                break  # не смогли записать — лучше не начислить молча дважды, чем гадать

        # Приз "рыба" меняет caught в saves, но leaderboard.caught (откуда берёт данные
        # live-счёт клановых турниров) обновляет только /actions при обычной ловле — эта
        # ветка писала мимо него, и улов из лотереи "не считался" в турнире, пока игрок не
        # поймает ещё хоть одну рыбу вручную. Дублируем caught в leaderboard тем же best-effort
        # PATCH, что и везде (clanId и т.п.) — не блокирует начисление приза при сбое.
        if prize['kind'] == 'fish':
            try:
                await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={'caught': merged.get('caught', 0)})
            except Exception:
                pass

        # Диагностический лог — та же цель, что в lottery_spin: чтобы честные призы за
        # Stars не путались с подозрительными "скачками" при последующем аудите баланса.
        try:
            coins_before_log = round(float(sv.get('coins', 0) or 0) * 100) / 100
            coins_after_log = round(float(merged.get('coins', sv.get('coins', 0)) or 0) * 100) / 100
            await session.post(f"{base}/action_logs/{pid}.json{FB_AUTH}", json={
                "ts": int(time_module.time() * 1000),
                "coins_before": coins_before_log,
                "coins_after": coins_after_log,
                "n_actions": 1,
                "src": f"lottery_{via}",
                "prize": prize.get('kind')
            })
        except Exception:
            pass

        if prize['kind'] == 'jackpot':
            await session.put(f"{base}/jackpot/amount.json{FB_AUTH}", json=50)
            await broadcast_jackpot_win(username, prize['amount'])
        elif grow_jackpot:
            jackpot_url = f"{base}/jackpot/amount.json{FB_AUTH}"
            for j_attempt in range(6):
                async with session.get(jackpot_url, headers={"X-Firebase-ETag": "true"}) as jresp:
                    jetag = jresp.headers.get("ETag")
                    j = await jresp.json()
                j = j if (j and 50 <= j <= 1000) else 50
                jheaders = {"If-Match": jetag} if jetag else {}
                async with session.put(jackpot_url, json=round((j + 0.5) * 10) / 10, headers=jheaders) as jput:
                    if jput.status == 412:
                        continue
                    break


async def get_market_prices():
    """
    Глобальные серверные цены рынка — общие для всех игроков, генерируются той же
    формулой случайного блуждания, что и раньше в index.html, но теперь на сервере,
    поэтому их нельзя подделать записью в собственное сохранение.
    Обновляются лениво, не чаще раза в 30с (PRICE_INTERVAL_MS).
    Учитывают текущую погоду (тот же множитель, что был в клиентской версии).
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

        weather_mult = 1.0
        try:
            async with session.get(f"{base}/weather.json{FB_AUTH}") as wresp:
                w = await wresp.json()
            if w and isinstance(w, dict) and w.get('endsAt', 0) > now_ms:
                weather_mult = WEATHER_PRICE_MULT.get(w.get('id'), 1.0)
        except Exception:
            pass

        cur = (data or {}).get('cur') or {} if isinstance(data, dict) else {}
        new_cur = {}
        for name, base_price in BASE_PRICES.items():
            p = cur.get(name, base_price)
            chg = (random.random() - 0.48) * 0.3
            nv = max(base_price * 0.3, min(base_price * 3, p * (1 + chg)))
            nv = nv * weather_mult
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
                "ulocs": ["pond"],
                "unlockedTransports": ["bike"],
                "durability": {"bike": 100, "moped": 100, "car": 100, "truck": 100},
                "dailyDay": 0,
                "dailyLastClaim": 0,
                "comebackClaimedAt": 0,
                "questBonusDate": "",
                "lastAdLotterySpin": 0,
                "premiumFreeSpinDate": 0,
                "deliveryEscrow": 0,
                "deliveryEscrow2": 0,
                "lastSeen": now_ms
            })
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True}, headers=CORS)


async def refill_energy_ad(request):
    """
    +25 энергии за просмотр рекламы, не чаще раза в 10 минут. Раньше здесь были две дыры
    того же класса, что уже нашли и починили в лотерее/дневном бонусе:
    1) Кулдаун проверялся один раз по снимку sv из начала запроса — двойной тап мог дать
       +25 дважды за 10 минут.
    2) Запись энергии — абсолютным значением без ETag. Если в этот момент шёл обычный
       /actions (который тоже пишет energy, теперь с ETag-retry), одна из двух записей
       побеждала целиком, стирая начисление за рекламу или обычный расход энергии.
    Теперь — тот же паттерн: ETag-retry, кулдаун и реген пересчитываются на СВЕЖИХ данных
    при каждой попытке.
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
    saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
    now_ms = int(time.time() * 1000)
    is_prem = await is_premium(real_user_id)
    max_energy = 150 if is_prem else 100

    final_energy = None
    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(6):
                async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                    etag = resp.headers.get("ETag")
                    sv = await resp.json()
                sv = sv or {}

                last = sv.get('lastAdEnergyRefill') or 0
                if now_ms - last < 600000:
                    return web.json_response({'error': 'cooldown', 'retry_after_ms': 600000 - (now_ms - last)}, status=429, headers=CORS)

                last_energy_update = sv.get('lastEnergyUpdate') or now_ms
                prev_energy = float(sv.get('energy', max_energy) if sv.get('energy') is not None else max_energy)
                regen_sec = max(0, (now_ms - last_energy_update) / 1000)
                current_energy = min(max_energy, prev_energy + regen_sec / ENERGY_REGEN_SEC)
                final_energy = round(min(max_energy, current_energy + 25) * 100) / 100

                headers = {"If-Match": etag} if etag else {}
                merged = dict(sv)
                merged.update({
                    "energy": final_energy,
                    "lastEnergyUpdate": now_ms,
                    "lastAdEnergyRefill": now_ms
                })
                async with session.put(saves_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue  # кто-то записал раньше нас (обычный /actions или другой тап) — перечитываем
                    if put_resp.status not in (200, 204):
                        raise RuntimeError(f"refill_energy_ad PUT failed: {put_resp.status} {await put_resp.text()}")
                    break
            else:
                return web.json_response({'error': 'internal: too many conflicts'}, status=500, headers=CORS)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'energy': final_energy}, headers=CORS)


async def lottery_spin(request):
    """
    Бесплатные крутки лотереи (за рекламу — раз в час, или Premium — раз в день).
    Платная крутка за Stars обрабатывается отдельно, прямо в successful_payment.
    Сервер сам решает приз (pick_lottery_prize) — клиент только просит крутнуть
    и получает готовый результат, никакой рулетки на клиенте.

    ВСЯ логика — проверка кулдауна, начисление приза и обновление метки времени —
    теперь ОДНА атомарная запись с ETag-блокировкой (см. цикл ниже), а не три
    отдельных похода в Firebase, как раньше. Было две реальные дыры:
    1) Кулдаун проверялся без блокировки — два быстрых запроса подряд (двойной тап,
       повтор сети) оба читали одну и ту же старую метку времени, оба проходили
       проверку и оба получали приз — двойная прокрутка в обход "раз в час"/"раз в день".
    2) Начисление монет-приза писалось тем же незащищённым способом, что и прочие
       поля баланса — выигрыш мог тихо потеряться при гонке с обычным /actions,
       ровно как терялись квест/доставка у других игроков.
    Теперь при КАЖДОЙ попытке retry кулдаун проверяется заново на свежих данных —
    если конкурентная прокрутка уже прошла между попытками, повтор это увидит и
    честно отклонит дубль, а не тихо позволит оба.
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
        real_user = json.loads(verified.get('user', '{}'))
        real_user_id = real_user.get('id')
    except Exception:
        real_user = {}
        real_user_id = None
    if not real_user_id:
        return web.json_response({'error': 'unauthorized'}, status=401, headers=CORS)
    username = real_user.get('username') or real_user.get('first_name') or 'Игрок'

    via = data.get('via')
    if via not in ('ad', 'premium'):
        return web.json_response({'error': 'invalid via'}, status=400, headers=CORS)

    if via == 'premium' and not await is_premium(real_user_id):
        return web.json_response({'error': 'not premium'}, status=403, headers=CORS)

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    pid = f"tg_{real_user_id}"
    now_ms = int(time.time() * 1000)
    today = time.strftime('%Y-%m-%d', time.gmtime((now_ms + 3 * 3600000) / 1000))  # МСК, не UTC —
    # иначе "новый день" для бесплатной Premium-крутки наступал в 03:00 МСК вместо полуночи,
    # тот же класс несостыковки, что чинили для ежедневного бонуса.
    saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/jackpot/amount.json{FB_AUTH}") as resp:
                jackpot = await resp.json()
    except Exception:
        jackpot = 50
    jackpot = jackpot if (jackpot and 50 <= jackpot <= 1000) else 50

    prize = None
    grow_jackpot = (via == 'premium')

    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(6):
                async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as resp:
                    etag = resp.headers.get("ETag")
                    sv = await resp.json()
                sv = sv or {}

                if via == 'ad':
                    last = sv.get('lastAdLotterySpin') or 0
                    if now_ms - last < 3600000:
                        return web.json_response({'error': 'cooldown', 'retry_after_ms': 3600000 - (now_ms - last)}, status=429, headers=CORS)
                else:
                    if sv.get('premiumFreeSpinDate') == today:
                        return web.json_response({'error': 'already used today'}, status=429, headers=CORS)

                ulocs = sv.get('ulocs') or ['pond']
                mult = 1
                for loc_id in ulocs:
                    m = LOCATION_MULT.get(loc_id, 1)
                    if m > mult:
                        mult = m

                if prize is None:
                    prize = pick_lottery_prize(mult, jackpot)  # решаем приз один раз, не перевыбираем на retry

                merged = dict(sv)
                if prize['kind'] == 'coins':
                    merged['coins'] = round((float(sv.get('coins', 0) or 0) + prize['amount']) * 100) / 100
                    merged['totalEarned'] = round((float(sv.get('totalEarned', 0) or 0) + prize['amount']) * 100) / 100
                elif prize['kind'] == 'fish':
                    merged['caught'] = int(sv.get('caught', 0) or 0) + prize['amount']
                    merged['unsoldCaught'] = round((float(sv.get('unsoldCaught', 0) or 0) + prize['amount']) * 100) / 100
                elif prize['kind'] in ('salt', 'knife'):
                    loc = sv.get('loc') or 'pond'
                    field = 'saltByLoc' if prize['kind'] == 'salt' else 'knifeByLoc'
                    by_loc = sv.get(field) or {}
                    if not isinstance(by_loc, dict):
                        by_loc = {}
                    by_loc[loc] = (by_loc.get(loc) or 0) + prize['amount']
                    merged[field] = by_loc
                elif prize['kind'] == 'truck_ticket':
                    merged['truckTickets'] = (sv.get('truckTickets') or 0) + 1
                # 'jackpot' начисляется отдельно ниже (глобальный путь jackpot/amount, не per-player)

                stats = sv.get('lotteryStats')
                stats = stats if isinstance(stats, dict) else {}
                spins_key = f"{via}Spins"
                stats[spins_key] = (stats.get(spins_key) or 0) + 1
                wins = stats.get('wins') if isinstance(stats.get('wins'), dict) else {}
                wins[prize['kind']] = (wins.get(prize['kind']) or 0) + 1
                stats['wins'] = wins
                merged['lotteryStats'] = stats

                if via == 'ad':
                    merged['lastAdLotterySpin'] = now_ms
                else:
                    merged['premiumFreeSpinDate'] = today

                headers = {"If-Match": etag} if etag else {}
                async with session.put(saves_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue  # конкурентная прокрутка успела записать раньше — перечитываем и проверяем кулдаун заново
                    if put_resp.status not in (200, 204):
                        raise RuntimeError(f"lottery saves PUT failed: {put_resp.status} {await put_resp.text()}")
                    break
            else:
                return web.json_response({'error': 'internal: too many conflicts'}, status=500, headers=CORS)

            # Тот же фикс, что и в apply_lottery_prize (платная крутка): приз "рыба" меняет
            # caught в saves, но live-счёт клановых турниров читает leaderboard.caught, который
            # без этого обновляется только на следующем /actions при обычной ловле.
            if prize['kind'] == 'fish':
                try:
                    await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={'caught': merged.get('caught', 0)})
                except Exception:
                    pass

            # Диагностический лог — та же цель, что и action_logs в process_actions:
            # чтобы честные выигрыши лотереи не путались с подозрительными "скачками"
            # при последующем аудите (раньше /lottery_spin вообще не попадал в этот лог,
            # и любой реальный приз выглядел как необъяснимое расхождение — см. разбор
            # @griinn80, где +600/+300/+1000 оказались честными призами лотереи).
            # Обёрнуто в отдельный try — сбой записи лога не должен ломать сам розыгрыш.
            try:
                coins_before_log = round(float(sv.get('coins', 0) or 0) * 100) / 100
                coins_after_log = round(float(merged.get('coins', sv.get('coins', 0)) or 0) * 100) / 100
                await session.post(f"{base}/action_logs/{pid}.json{FB_AUTH}", json={
                    "ts": now_ms,
                    "coins_before": coins_before_log,
                    "coins_after": coins_after_log,
                    "n_actions": 1,
                    "src": f"lottery_{via}",
                    "prize": prize.get('kind')
                })
            except Exception:
                pass

            # Джекпот — отдельный глобальный путь, свой маленький retry на случай
            # одновременного роста/сброса от нескольких игроков разом.
            jackpot_url = f"{base}/jackpot/amount.json{FB_AUTH}"
            if prize['kind'] == 'jackpot':
                await session.put(jackpot_url, json=50)
                await broadcast_jackpot_win(username, prize['amount'])
            elif grow_jackpot:
                for j_attempt in range(6):
                    async with session.get(jackpot_url, headers={"X-Firebase-ETag": "true"}) as jresp:
                        jetag = jresp.headers.get("ETag")
                        j = await jresp.json()
                    j = j if (j and 50 <= j <= 1000) else 50
                    jheaders = {"If-Match": jetag} if jetag else {}
                    async with session.put(jackpot_url, json=round((j + 0.5) * 10) / 10, headers=jheaders) as jput:
                        if jput.status == 412:
                            continue
                        break
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({'ok': True, 'prize': prize}, headers=CORS)


async def resolve_escrow_ops(session, base, pid, escrow_ops, legacy_a=0.0, legacy_b=0.0):
    """
    Разрешает операции с эскроу доставки (add/collect) через ОТДЕЛЬНЫЙ путь в Firebase
    (escrow/{pid}, а не saves/{pid}) с оптимистичной блокировкой по ETag.

    Зачем отдельно от основного sv: доставка держит деньги в подвешенном состоянии
    ЧАСАМИ (1-2+ часа таймер), и всё это время любой другой параллельный /actions-запрос
    того же игрока (авто-доход, тап, другая продажа — что угодно) читает старую версию
    ВСЕГО sv-объекта целиком и, записывая свой результат, тихо перезаписывает уже
    добавленные в эскроу деньги. Реальный подтверждённый случай — доставка на 1500.2
    монет, стёртая ещё до того, как игрок успел её забрать.

    Механизм: читаем escrow/{pid} с ETag, ПРИМЕНЯЕМ операции этого запроса к СВЕЖЕМУ
    значению (а не к тому, что было в начале обработки — оно могло успеть устареть за
    время долгого таймера), пишем обратно с If-Match. Если кто-то успел записать
    между чтением и записью — Firebase вернёт 412, читаем заново и повторяем. Так
    гонка не пропадает молча, а честно разрешается в правильном порядке.

    Если escrow/{pid} ещё не существует (первый вызов после деплоя этого фикса) —
    разово мигрируем старые значения из sv.deliveryEscrow/deliveryEscrow2, чтобы никто
    не потерял уже накопленные в старом месте деньги при переходе на новую схему.

    Возвращает (credited, migrated) — начисленные монеты и флаг "миграция/запись
    подтверждённо прошла успешно" (вызывающий код должен обнулять старые sv-поля
    ТОЛЬКО если migrated=True, иначе можно стереть деньги, которые так и не
    переехали в новую схему из-за сетевой ошибки).
    """
    # ВАЖНО: не выходим рано только по признаку "нет операций в этом батче" — если у
    # игрока есть legacy-деньги (legacy_a/legacy_b), миграцию всё равно нужно выполнить
    # прямо сейчас, иначе вызывающий код обнулит старые поля БЕЗ переноса суммы.
    if not escrow_ops and legacy_a == 0 and legacy_b == 0:
        return 0.0, False

    url = f"{base}/escrow/{pid}.json{FB_AUTH}"
    credited = 0.0

    for attempt in range(6):
        try:
            async with session.get(url, headers={"X-Firebase-ETag": "true"}) as resp:
                etag = resp.headers.get("ETag")
                cur = await resp.json()
        except Exception:
            return credited, False  # Firebase недоступен — ничего не начисляем и не мигрируем,
            # старые sv-поля останутся нетронутыми, попробуем снова следующим запросом.

        if cur is None:
            a, b = legacy_a, legacy_b  # первая миграция со старой схемы хранения
        else:
            a = float((cur or {}).get('a', 0) or 0)
            b = float((cur or {}).get('b', 0) or 0)

        credited = 0.0
        for op, is_b, *rest in escrow_ops:
            if op == 'add':
                amount = rest[0]
                if is_b:
                    b += amount
                else:
                    a += amount
            elif op == 'collect':
                if is_b:
                    credited += b
                    b = 0.0
                else:
                    credited += a
                    a = 0.0

        headers = {"If-Match": etag} if etag else {}
        try:
            async with session.put(url, json={"a": round(a * 100) / 100, "b": round(b * 100) / 100}, headers=headers) as put_resp:
                if put_resp.status == 412:
                    continue  # кто-то записал раньше нас — перечитываем и повторяем
                return round(credited * 100) / 100, True
        except Exception:
            return 0.0, False  # не смогли записать — безопаснее ничего не начислить и не мигрировать

    return 0.0, False  # исчерпали попытки — крайне маловероятно, но не начисляем вслепую


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
        tg_user = json.loads(verified.get('user', '{}'))
        real_user_id = tg_user.get('id')
    except Exception:
        tg_user = {}
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
    orig_upg_levels = {loc_k: dict(lv) for loc_k, lv in upg_levels.items() if isinstance(lv, dict)}  # копия ДО
    # изменений этим запросом — нужна ниже, чтобы на retry сливать апгрейды через "бери
    # максимум", а не перезаписывать целиком (см. подтверждённый случай потери уровня
    # удочки у @Alexander5551 — деньги списались и остались, а сам уровень апгрейда
    # откатился, потому что upgLevels писался абсолютным значением без учёта параллельных
    # запросов, в отличие от coins/energy, которые уже были защищены).
    ulocs = sv.get('ulocs') or ['pond']
    is_prem = await is_premium(real_user_id)
    max_energy = 150 if is_prem else 100

    # Энергия — регенерируем по реальному прошедшему времени, а не верим клиенту.
    now_ms = int(time.time() * 1000)
    last_energy_update = sv.get('lastEnergyUpdate') or now_ms
    prev_energy = float(sv.get('energy', max_energy) if sv.get('energy') is not None else max_energy)
    regen_sec = max(0, (now_ms - last_energy_update) / 1000)
    energy = min(max_energy, prev_energy + regen_sec / ENERGY_REGEN_SEC)
    energy_after_regen_at_start = energy  # для дельты ниже — чистый расход энергии за
    # действия ЭТОГО запроса, отдельно от регенерации по времени (её пересчитываем заново
    # на каждой попытке записи против СВЕЖЕГО lastEnergyUpdate — иначе гонка с
    # /refill_energy_ad и покупкой energyFull за Stars, которые тоже пишут energy
    # независимо: без этого разделения покупка/реклама могли перезаписываться обратно).

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
    if auto_elapsed_sec > 0 and auto_elapsed_sec < 3600 * 24 * 30:  # запас на 30 дней — lastSeen пишет только сервер, подделать нельзя, поэтому длинный офлайн честный
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
    claim_result = None
    salt_delta = 0
    knife_delta = 0
    truck_tickets_delta = 0
    unlocked_transports = sv.get('unlockedTransports') or ['bike']
    if not isinstance(unlocked_transports, list):
        unlocked_transports = ['bike']
    durability = sv.get('durability') or {'bike': 100, 'moped': 100, 'car': 100, 'truck': 100}
    if not isinstance(durability, dict):
        durability = {'bike': 100, 'moped': 100, 'car': 100, 'truck': 100}
    current_transport = sv.get('transport') or 'bike'
    transport_changed = False
    ulocs_changed = False
    escrow_ops = []  # ('add', via_driver, amount) | ('collect', via_driver) — эскроу теперь
    # разрешается ОТДЕЛЬНО, через собственный retry-цикл с ETag ниже. Здесь только собираем
    # список операций в порядке появления в actions — сама сумма читается заново из
    # актуального Firebase-состояния в момент разрешения, а не из локальной переменной,
    # которая могла успеть устареть за долгое время ожидания таймера доставки.
    # "Разовые" бонусы (дневной/квест/комбэк/промо) — только флаг "запрошен в этом батче"
    # здесь, сама проверка права и начисление — в retry-цикле ниже на СВЕЖИХ данных (см.
    # комментарий там же про защиту от двойного тапа, аналогично фиксу лотереи).
    pending_daily_bonus = False
    pending_quest_bonus = False
    pending_comeback_bonus = False
    pending_net_promo_bonus = False
    pending_promo_ends_at = None
    pending_promo_amount = 0

    for act in actions:
        if not isinstance(act, dict):
            rejected += 1
            continue
        a_type = act.get('type')

        if a_type == 'unlock_location':
            # Разблокировка локации — раньше клиент списывал стоимость только локально,
            # следующая синхронизация откатывала баланс обратно, а локация оставалась открыта.
            # Отдельная ветка ДО общей проверки "локация уже разлочена", потому что тут
            # разблокируемая локация как раз ЕЩЁ не в списке открытых.
            target_loc = act.get('loc')
            unlock_cost = LOCATION_UNLOCK_COST.get(target_loc)
            if unlock_cost is None or target_loc in ulocs:
                rejected += 1
                continue
            if coins < unlock_cost:
                rejected += 1
                continue
            coins -= unlock_cost
            ulocs.append(target_loc)
            ulocs_changed = True
            continue

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
            # Продажа через доставку (транспорт/водитель) — деньги теперь начисляются НЕ сразу,
            # а откладываются в "эскроу" до прибытия доставки (queueAction 'collect_delivery').
            # Так честнее по смыслу игры: рыба физически едет, деньги приходят вместе с ней.
            # Инвентарь (unsoldCaught) списывается сразу — рыба реально уехала со склада.
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
                escrow_ops.append(('add', True, earned))
            else:
                escrow_ops.append(('add', False, earned))

        elif a_type == 'collect_delivery':
            # Доставка прибыла — клиент сам обнаруживает это по истечению таймера
            # (state.delivery.endsAt) и присылает этот сигнал. Сумма берётся из эскроу
            # на сервере — клиент не может указать сумму сам, только "забрать что скопилось".
            # via:'driver' указывает, какой из двух слотов доставки собирать.
            # Само списание/начисление происходит позже, в resolve_escrow_ops — там же,
            # где и 'add', на СВЕЖЕМ прочитанном значении, а не на том, что было в начале
            # этого запроса (деньги могли лежать в эскроу часами, за которые их мог
            # переписать любой другой параллельный запрос).
            via_driver_collect = bool(act.get('via') == 'driver')
            escrow_ops.append(('collect', via_driver_collect))

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
            # ETag-защита от двойного клейма: раньше сумма считалась один раз, а очистка
            # (delete) происходила без проверки — два параллельных запроса могли оба
            # прочитать одни и те же ref_bonuses/pending_rewards ДО того, как любой из них
            # их очистил, и оба получить одну и ту же сумму дважды. Теперь читаем с ETag и
            # удаляем с If-Match: кто не успел удалить первым (конфликт версии) — получает
            # 0 с этого источника вместо повторного начисления.
            try:
                async def claim_and_clear(url):
                    async with aiohttp.ClientSession() as s:
                        for _ in range(6):
                            async with s.get(url, headers={"X-Firebase-ETag": "true"}) as r:
                                etag = r.headers.get("ETag")
                                data = await r.json()
                            if not data:
                                return None
                            headers = {"If-Match": etag} if etag else {}
                            async with s.delete(url, headers=headers) as dresp:
                                if dresp.status == 412:
                                    continue  # кто-то уже забрал это между нашим чтением и удалением
                                if dresp.status not in (200, 204):
                                    return None  # DELETE не подтверждён — не начисляем вслепую,
                                    # лучше недодать в этот раз, чем задвоить, если удаление
                                    # на самом деле не прошло (например, кратковременный сбой Firebase)
                                return data
                    return None

                rb = await claim_and_clear(f"{base}/ref_bonuses/{pid}.json{FB_AUTH}")
                pr = await claim_and_clear(f"{base}/pending_rewards/{pid}.json{FB_AUTH}")
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
                    claim_result = {'total': claimed_total, 'details': claimed_details}
                # claim_pending_clear больше не нужен — очистка уже произошла выше, атомарно
                # вместе с чтением, а не отдельным шагом в конце запроса.
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

        elif a_type == 'daily_bonus':
            # Ежедневный бонус — сама сумма и день серии считаются здесь (зависят от
            # mult/премиума, не от гонки), но ПРОВЕРКА ПРАВА НА ПОЛУЧЕНИЕ и запись в
            # coins/totalEarned переносятся в retry-цикл ниже (см. pending_daily_bonus) —
            # иначе двойной тап по "Забрать" мог пройти дважды: два параллельных запроса
            # оба читают один и тот же daily_last_claim из НАЧАЛА обработки, оба проходят
            # проверку и оба получают бонус (ровно та дыра, что была в лотерее до фикса).
            pending_daily_bonus = True

        elif a_type == 'quest_bonus':
            # Бонус "все квесты выполнены" — сервер не проверяет полный прогресс квестов
            # (это потребовало бы дублировать весь генератор квестов), но жёстко ограничивает
            # ОДНИМ получением в день на реальном сервером времени. Проверка и начисление —
            # в retry-цикле ниже, по той же причине, что и daily_bonus (защита от двойного тапа).
            pending_quest_bonus = True

        elif a_type == 'comeback_bonus':
            # Бонус за возвращение после долгого отсутствия — сумма зависит от реального
            # времени отсутствия по lastSeen (не от заявленного клиентом), проверка права —
            # в retry-цикле ниже (та же защита от двойного тапа, что и остальные бонусы).
            pending_comeback_bonus = True

        elif a_type == 'net_promo_bonus':
            # Разовый промо-бонус +500 монет за покупку Сети (запускается /startpromo).
            # Конфиг акции (endsAt/сумма) читаем один раз здесь — это глобальный, редко
            # меняющийся админский параметр, не подверженный той же гонке, что личная
            # метка "уже получал". А вот саму проверку "уже получал именно за эту акцию"
            # и начисление — в retry-цикл ниже, по той же причине, что и остальные бонусы.
            try:
                async with aiohttp.ClientSession() as promo_session:
                    async with promo_session.get(f"{base}/promo/net_bonus.json{FB_AUTH}") as resp:
                        promo = await resp.json()
            except Exception:
                promo = None
            promo_ends_at = promo.get('endsAt') if isinstance(promo, dict) else None
            if not promo_ends_at or now_ms > promo_ends_at:
                rejected += 1
            else:
                pending_net_promo_bonus = True
                pending_promo_ends_at = promo_ends_at
                pending_promo_amount = float(promo.get('bonus', 500)) if isinstance(promo, dict) else 500

        elif a_type == 'rare_fish_catch':
            # Улов редкой рыбы (Осетрина) — сервер сверяет, что она РЕАЛЬНО была активна
            # именно сейчас, по той же детерминированной формуле от времени, что и клиент
            # (rare_fish_status/rareFishSchedule), а не верит заявлению клиента "я её
            # поймал". Раньше это сверялось с общим для всех игроков узлом rare_fish в
            # Firebase, который любой клиент мог переписать — не только баг "осетрина
            # активна ~6 часов", но и готовый вектор держать 15%-шанс на монеты активным
            # вечно. Теперь подделывать нечего — формула, не состояние.
            rf = rare_fish_status(now_ms)
            rf_active = now_ms < rf['endsAt'] + 5000
            # +5000мс запаса: клиент шлёт catch мгновенно (без debounce), но сетевая
            # задержка сама по себе может протолкнуть запрос за пределы endsAt, если
            # игрок поймал рыбу буквально в последние секунды 10-минутного окна — без
            # этого запаса честный улов у самой границы отклонялся бы, а игрок не видел
            # никакой причины (клиент молча откатывал уже показанные монеты).
            if not rf_active:
                rejected += 1
                continue
            reward = round(RARE_FISH_PRICE * mult)
            coins += reward
            total_earned += reward
            caught += 1
            unsold += 1

        elif a_type == 'buy_supplies':
            # Заказ соли/ножей при отправке доставки — раньше клиент списывал монеты только
            # локально, следующая же серверная синхронизация "не знала" об этой трате и
            # откатывала баланс обратно. Та же дыра, что была с транспортом.
            # Важно: сама соль/ножи начисляются КЛИЕНТОМ отдельно при ПРИБЫТИИ доставки
            # (не здесь) — если начислить их ещё и тут, получится задвоение. Эта проверка
            # только списывает реальную стоимость заказа, не трогая инвентарь.
            try:
                salt_qty = int(act.get('salt', 0))
                knife_qty = int(act.get('knife', 0))
            except (TypeError, ValueError):
                rejected += 1
                continue
            if salt_qty < 0 or knife_qty < 0:
                rejected += 1
                continue
            # Вместимость транспорта — соль и нож делят её пополам с исходящим грузом на
            # обратный путь (см. клиент: changeSaltOrder/changeKnifeOrder теперь тоже
            # проверяют это вместе). Раньше сервер вообще не знал о вместимости и разрешал
            # заказать хоть 10000 каждого — деньги игрока, но обходит игровое ограничение.
            # Транспорт не берём со слов клиента как есть — сверяем, что он реально
            # принадлежит игроку (или это платная аренда грузовика с активным бустом).
            claimed_transport = act.get('transport')
            owned = set(sv.get('unlockedTransports') or []) | {'bike'}
            if claimed_transport == 'rentalTruck':
                truck_rental_active = (sv.get('boosts') or {}).get('truckRental', 0) > now_ms
                capacity = TRANSPORT_CAPACITY['rentalTruck'] if truck_rental_active else TRANSPORT_CAPACITY['bike']
            elif claimed_transport in owned and claimed_transport in TRANSPORT_CAPACITY:
                capacity = TRANSPORT_CAPACITY[claimed_transport]
            else:
                capacity = TRANSPORT_CAPACITY['bike']  # неизвестный/непринадлежащий транспорт — минимальная вместимость
            if salt_qty + knife_qty > capacity:
                rejected += 1
                continue
            cost = round((salt_qty * 1 + knife_qty * 3) * mult * 100) / 100
            if cost > 0 and coins < cost:
                rejected += 1
                continue
            coins -= cost

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

        elif a_type == 'buy_transport':
            tr_id = act.get('transport')
            cost = TRANSPORT_COST.get(tr_id)
            if cost is None or tr_id in unlocked_transports:
                rejected += 1
                continue
            if coins < cost:
                rejected += 1
                continue
            coins -= cost
            unlocked_transports.append(tr_id)
            current_transport = tr_id
            transport_changed = True

        elif a_type == 'transport_wear':
            # Естественный износ транспорта (-10 за каждую доставку, кроме арендованного грузовика) —
            # не деньги, но пишется в то же заблокированное поле durability, поэтому тоже
            # нужен серверный путь, иначе транспорт никогда не будет "снашиваться" по-настоящему.
            tr_id = act.get('transport')
            if tr_id not in unlocked_transports or tr_id == 'rentalTruck':
                rejected += 1
                continue
            durability[tr_id] = max(0, (durability.get(tr_id, 100) or 100) - 10)
            transport_changed = True

        elif a_type == 'repair_transport':
            tr_id = act.get('transport')
            repair_cost = TRANSPORT_REPAIR_COST.get(tr_id)
            if repair_cost is None or tr_id not in unlocked_transports:
                rejected += 1
                continue
            if coins < repair_cost:
                rejected += 1
                continue
            coins -= repair_cost
            durability[tr_id] = 100
            transport_changed = True

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
                            # ETag-защита на флаге "уже награждён" — тот же принцип, что и
                            # everywhere else: два одновременных апгрейда до ур.2 (например,
                            # повтор сети) не должны дать двойную награду рефереру.
                            flag_url = f"{base}/referrals/rod2_rewarded/{real_user_id}.json{FB_AUTH}"
                            already_rewarded = False
                            for flag_attempt in range(6):
                                async with session.get(flag_url, headers={"X-Firebase-ETag": "true"}) as r2:
                                    fetag = r2.headers.get("ETag")
                                    already = await r2.json()
                                if already:
                                    already_rewarded = True
                                    break
                                fheaders = {"If-Match": fetag} if fetag else {}
                                async with session.put(flag_url, json=True, headers=fheaders) as fput:
                                    if fput.status == 412:
                                        continue
                                    break
                            if not already_rewarded:
                                # Начисление рефереру — узкий путь по coins+totalEarned с ETag+retry,
                                # тем же способом, что и в deduct_coin_balance: если реферер
                                # сам активно играет в этот момент, его собственный /actions
                                # не должен затереть этот бонус более старой копией баланса.
                                # ВАЖНО: пишем и coins, и totalEarned (не только coins, как было
                                # раньше) — иначе этот доход не учитывается в лидерборде/турнирах
                                # недели, которые считаются именно по totalEarned. Игроки, зарабатывающие
                                # в основном рефералами, не попадали в рейтинг турнира несмотря на
                                # реально растущий баланс.
                                ref_save_url = f"{base}/saves/tg_{referrer_id}.json{FB_AUTH}"
                                new_ref_total_earned = None
                                for coin_attempt in range(6):
                                    async with session.get(ref_save_url, headers={"X-Firebase-ETag": "true"}) as r3:
                                        cetag = r3.headers.get("ETag")
                                        ref_sv = await r3.json()
                                    ref_sv = ref_sv or {}
                                    ref_merged = dict(ref_sv)
                                    ref_merged['coins'] = round((float(ref_sv.get('coins', 0) or 0) + 1000) * 100) / 100
                                    new_ref_total_earned = round((float(ref_sv.get('totalEarned', 0) or 0) + 1000) * 100) / 100
                                    ref_merged['totalEarned'] = new_ref_total_earned
                                    cheaders = {"If-Match": cetag} if cetag else {}
                                    async with session.put(ref_save_url, json=ref_merged, headers=cheaders) as cput:
                                        if cput.status == 412:
                                            continue
                                        break
                                # Обновляем лидерборд рефереру тем же totalEarned — иначе бонус
                                # попадёт в saves, но не отразится в рейтинге до следующего личного
                                # /actions-запроса реферера (который может случиться нескоро, если
                                # он сам не ловит и не продаёт рыбу).
                                if new_ref_total_earned is not None:
                                    try:
                                        await session.patch(f"{base}/leaderboard/tg_{referrer_id}.json{FB_AUTH}", json={
                                            "totalEarned": round(new_ref_total_earned),
                                            "coins": round(float(ref_merged.get('coins', 0) or 0)),
                                            "userId": int(referrer_id)
                                        })
                                    except Exception:
                                        pass
                                try:
                                    async with session.get(f"{base}/leaderboard/tg_{real_user_id}.json{FB_AUTH}") as r4:
                                        ref_lb = await r4.json()
                                    ref_label = f"@{ref_lb.get('username')}" if ref_lb and ref_lb.get('username') else f"ID:{real_user_id}"
                                except Exception:
                                    ref_label = f"ID:{real_user_id}"
                                try:
                                    await bot.send_message(int(referrer_id), f"🎁 Реферальный бонус: +🪙1000! Твой реферал {ref_label} прокачал удочку до ур.2")
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
    if transport_changed:
        extra_fields['unlockedTransports'] = unlocked_transports
        extra_fields['durability'] = durability
        extra_fields['transport'] = current_transport
    if ulocs_changed:
        extra_fields['ulocs'] = ulocs
    # deliveryEscrow/deliveryEscrow2 больше НЕ пишутся сюда — эскроу теперь живёт в
    # отдельном пути escrow/{pid} с ETag-блокировкой (см. resolve_escrow_ops). Старые
    # поля обнуляем НИЖЕ, только после подтверждённой миграции — а не здесь заранее,
    # чтобы не потерять деньги при сетевой ошибке во время переноса.
    # dailyDay/dailyLastClaim/comebackClaimedAt/questBonusDate/netPromoClaimed больше
    # НЕ пишутся здесь абсолютным значением — они проверяются и начисляются внутри
    # retry-цикла ниже, на свежих данных при каждой попытке (защита от двойного тапа).

    response_daily_day = None
    response_daily_last_claim = None

    try:
        async with aiohttp.ClientSession() as session:
            # Разрешаем эскроу ОТДЕЛЬНОЙ ETag-защищённой операцией до основной записи —
            # legacy_a/legacy_b используются только один раз, если escrow/{pid} ещё не
            # существует (миграция со старой схемы хранения прямо в sv).
            credited, migrated = await resolve_escrow_ops(
                session, base, pid, escrow_ops,
                legacy_a=float(sv.get('deliveryEscrow', 0) or 0),
                legacy_b=float(sv.get('deliveryEscrow2', 0) or 0)
            )
            coins += credited
            total_earned += credited
            if migrated:
                # Подтверждённо перенесено (или уже было в новой схеме и просто обновлено) —
                # теперь безопасно обнулить старые поля, дублей не будет.
                extra_fields['deliveryEscrow'] = 0
                extra_fields['deliveryEscrow2'] = 0

            # ГЛАВНАЯ ЗАЩИТА ОТ ГОНКИ ЗАПИСИ БАЛАНСА. Раньше coins/caught/totalEarned
            # писались одним PATCH абсолютными значениями, посчитанными от sv, прочитанного
            # в НАЧАЛЕ этого запроса. Если два /actions-запроса от одного игрока идут почти
            # одновременно (например, обе доставки истекли за время, пока игра была
            # свёрнута, и клиент шлёт два отдельных запроса подряд в одном тике —
            # см. checkDelivery() в index.html), оба читают ОДИН И ТОТ ЖЕ стартовый баланс
            # и пишут результат независимо — тот, кто запишет ПОСЛЕДНИМ, стирает прибавку
            # первого. Подтверждённый реальный случай: @MaksimPlahovvv, 27.08, два запроса
            # с одинаковым ts, один посчитал +914, второй +34 от того же старта — выжили
            # только +34.
            # Фикс: считаем не абсолютные значения, а ДЕЛЬТЫ (прирост за этот запрос) и
            # применяем их к СВЕЖЕМУ прочитанному балансу с ETag-блокировкой и retry —
            # тем же способом, что уже проверен на эскроу доставки выше. energy — туда же:
            # реальный случай, когда покупка energyFull за Stars или /refill_energy_ad
            # (оба пишут energy НЕЗАВИСИМО отсюда) перезаписывались обратно обычным
            # /actions, случившимся почти одновременно — расход энергии за тап/улов
            # в ЭТОМ запросе (energy_action_delta) переносим на свежий реген при каждой
            # попытке, а не на реген, посчитанный в начале запроса.
            # upgLevels/unsold/extra_fields по-прежнему пишутся абсолютным значением —
            # по ним подтверждённого случая пропажи нет, расширять при необходимости.
            # unsoldCaught — ИСКЛЮЧЕНИЕ, выносим отдельно как дельту (см. ниже): это
            # подтверждённый случай гонки — при частом тапе (catch) параллельно с отправкой
            # доставки (sell) оба запроса читают один и тот же sv в начале, каждый сам по себе
            # честно считает свой unsold, но кто записывает ПОСЛЕДНИМ — стирает прибавку
            # первого точно так же, как раньше было с coins. Result: клиент показывает рыбу
            # по видам (liveFish/driedFish), которой сервер не видит в unsoldCaught, и при
            # следующей крупной продаже часть видов из партии сервер отклоняет как "нет в
            # наличии", хотя рыба была честно поймана.
            coins_delta = coins - float(sv.get('coins', 0) or 0)
            caught_delta = caught - float(sv.get('caught', 0) or 0)
            total_earned_delta = total_earned - float(sv.get('totalEarned', 0) or 0)
            unsold_delta = unsold - float(sv.get('unsoldCaught', 0) or 0)
            energy_action_delta = energy - energy_after_regen_at_start
            other_fields = {
                **extra_fields
            }
            saves_url = f"{base}/saves/{pid}.json{FB_AUTH}"
            bonus_rejected_counted = {'daily': False, 'quest': False, 'comeback': False, 'promo': False}
            for save_attempt in range(6):
                async with session.get(saves_url, headers={"X-Firebase-ETag": "true"}) as sresp:
                    setag = sresp.headers.get("ETag")
                    fresh_sv = await sresp.json()
                fresh_sv = fresh_sv or {}
                save_base_coins = float(fresh_sv.get('coins', 0) or 0)  # для точного диагностического
                # лога ниже — раньше там ошибочно использовался sv (снимок из НАЧАЛА запроса,
                # до всех retry), из-за чего /actionlog показывал ложные ⚠️ РАСХОЖДЕНИЕ при
                # успешном retry, хотя деньги на самом деле корректно складывались.
                coins = round((save_base_coins + coins_delta) * 100) / 100
                caught = (float(fresh_sv.get('caught', 0) or 0)) + caught_delta
                total_earned = round((float(fresh_sv.get('totalEarned', 0) or 0) + total_earned_delta) * 100) / 100
                unsold = max(0, round((float(fresh_sv.get('unsoldCaught', 0) or 0) + unsold_delta) * 100) / 100)
                fresh_last_energy_update = fresh_sv.get('lastEnergyUpdate') or now_ms
                fresh_prev_energy = float(fresh_sv.get('energy', max_energy) if fresh_sv.get('energy') is not None else max_energy)
                fresh_regen_sec = max(0, (now_ms - fresh_last_energy_update) / 1000)
                energy = max(0, min(max_energy, fresh_prev_energy + fresh_regen_sec / ENERGY_REGEN_SEC + energy_action_delta))

                bonus_fields = {}
                # "Разовые" бонусы — проверяем право на СВЕЖИХ данных каждую попытку, а не
                # на снимке из начала запроса. Если конкурентный запрос уже успел забрать
                # бонус между попытками, эта проверка честно это увидит и не даст второй раз
                # (та же защита, что в lottery_spin, — см. комментарий там же).
                if pending_daily_bonus:
                    fresh_daily_last_claim = fresh_sv.get('dailyLastClaim') or 0
                    fresh_daily_day = int(fresh_sv.get('dailyDay', 0) or 0)
                    msk_day_now = (now_ms + 3 * 3600000) // 86400000
                    msk_day_last = (fresh_daily_last_claim + 3 * 3600000) // 86400000 if fresh_daily_last_claim else None
                    gap_days = (msk_day_now - msk_day_last) if fresh_daily_last_claim else None
                    if fresh_daily_last_claim and gap_days < 1:
                        if not bonus_rejected_counted['daily']:
                            rejected += 1
                            bonus_rejected_counted['daily'] = True
                        # Отклонено — это КАК РАЗ момент, когда локальная копия клиента могла
                        # разойтись с реальной (см. жалобу: превью показало "День 3 · 50",
                        # а сервер верно начислил "День 6 · 800" — клиент никогда не подтягивал
                        # dailyDay/dailyLastClaim обратно от сервера). Возвращаем правду и здесь,
                        # не только при успешном начислении.
                        response_daily_day = fresh_daily_day
                        response_daily_last_claim = fresh_daily_last_claim
                    else:
                        miss_grace_days = 3 if is_prem else 1
                        effective_day = 0 if (not fresh_daily_last_claim or gap_days > miss_grace_days) else fresh_daily_day
                        effective_day = max(0, min(effective_day, len(DAILY_REWARDS) - 1))
                        daily_reward = round(DAILY_REWARDS[effective_day] * mult * 100) / 100
                        coins = round((coins + daily_reward) * 100) / 100
                        total_earned = round((total_earned + daily_reward) * 100) / 100
                        bonus_fields['dailyDay'] = effective_day + 1 if effective_day < 6 else 0
                        bonus_fields['dailyLastClaim'] = now_ms
                        response_daily_day = bonus_fields['dailyDay']
                        response_daily_last_claim = now_ms

                if pending_quest_bonus:
                    fresh_quest_bonus_date = fresh_sv.get('questBonusDate') or ''
                    today_str = time.strftime('%Y-%m-%d', time.gmtime())
                    if fresh_quest_bonus_date == today_str:
                        if not bonus_rejected_counted['quest']:
                            rejected += 1
                            bonus_rejected_counted['quest'] = True
                    else:
                        quest_reward = round(QUEST_BONUS_BASE * mult * 100) / 100
                        coins = round((coins + quest_reward) * 100) / 100
                        total_earned = round((total_earned + quest_reward) * 100) / 100
                        bonus_fields['questBonusDate'] = today_str

                if pending_comeback_bonus:
                    fresh_comeback_claimed_at = fresh_sv.get('comebackClaimedAt') or 0
                    fresh_last_seen_before = fresh_sv.get('lastSeen') or now_ms
                    days_away = max(0, (now_ms - fresh_last_seen_before) / 86400000)
                    if fresh_comeback_claimed_at and fresh_comeback_claimed_at >= fresh_last_seen_before:
                        if not bonus_rejected_counted['comeback']:
                            rejected += 1
                            bonus_rejected_counted['comeback'] = True
                    elif days_away >= 3:
                        comeback_reward = 1000 if days_away >= 14 else (500 if days_away >= 7 else 200)
                        coins = round((coins + comeback_reward) * 100) / 100
                        total_earned = round((total_earned + comeback_reward) * 100) / 100
                        bonus_fields['comebackClaimedAt'] = now_ms
                    elif not bonus_rejected_counted['comeback']:
                        rejected += 1
                        bonus_rejected_counted['comeback'] = True

                if pending_net_promo_bonus:
                    if fresh_sv.get('netPromoClaimed') == pending_promo_ends_at:
                        if not bonus_rejected_counted['promo']:
                            rejected += 1
                            bonus_rejected_counted['promo'] = True
                    else:
                        coins = round((coins + pending_promo_amount) * 100) / 100
                        total_earned = round((total_earned + pending_promo_amount) * 100) / 100
                        bonus_fields['netPromoClaimed'] = pending_promo_ends_at

                save_headers = {"If-Match": setag} if setag else {}
                # Firebase REST API поддерживает If-Match ТОЛЬКО с PUT, не с PATCH (PATCH с
                # If-Match всегда возвращает 400 "not supported") — из-за этого предыдущая
                # версия этого фикса вообще ничего не сохраняла (код не проверял статус
                # ответа, принимал любой не-412 за успех). Поэтому PUT — а раз PUT заменяет
                # ВЕСЬ узел целиком, а не сливает частично, как PATCH, сначала мёржим наши
                # изменения поверх свежепрочитанного fresh_sv на стороне Python, чтобы не
                # стереть остальные поля сейва (транспорт, кухню, рефералку и т.д.).
                merged = dict(fresh_sv)
                # upgLevels сливаем ПО МАКСИМУМУ уровня, а не перезаписываем целиком нашим
                # снимком из начала запроса — иначе конкурентный запрос (например, другая
                # покупка апгрейда в параллельной вкладке/повторе сети) мог откатить уже
                # купленный уровень обратно, пока сам баланс монет уже был защищён и
                # оставался списанным. Подтверждённый случай: @Alexander5551 — 60,000
                # монет списались и остались списанными, а купленный 5й уровень удочки
                # откатился обратно на 4й. Уровни апгрейдов только растут, поэтому "взять
                # максимум" всегда безопасно и корректно объединяет два параллельных
                # изменения, а не выбирает случайно то, что записалось последним.
                merged_upg = {lk: dict(lv) for lk, lv in (fresh_sv.get('upgLevels') or {}).items() if isinstance(lv, dict)}
                for loc_k, lv in upg_levels.items():
                    if not isinstance(lv, dict):
                        continue
                    orig_lv = orig_upg_levels.get(loc_k, {})
                    for upg_k, final_level in lv.items():
                        if final_level != orig_lv.get(upg_k):
                            if loc_k not in merged_upg:
                                merged_upg[loc_k] = {}
                            cur_fresh_level = merged_upg[loc_k].get(upg_k, 0)
                            merged_upg[loc_k][upg_k] = max(cur_fresh_level, final_level)
                merged.update({
                    "coins": coins,
                    "caught": caught,
                    "totalEarned": total_earned,
                    "unsoldCaught": unsold,
                    "energy": round(energy * 100) / 100,
                    "lastEnergyUpdate": now_ms,
                    "lastSeen": now_ms,
                    "upgLevels": merged_upg,
                    **other_fields,
                    **bonus_fields
                })
                async with session.put(saves_url, json=merged, headers=save_headers) as sput:
                    if sput.status == 412:
                        continue  # кто-то записал раньше нас — перечитываем свежий баланс и повторяем
                    if sput.status not in (200, 204):
                        raise RuntimeError(f"saves PUT failed: {sput.status} {await sput.text()}")
                    break

            # Таблица лидеров — теперь пишется СЕРВЕРОМ из уже провалидированных coins/
            # caught/totalEarned, а не клиентом напрямую в Firebase. Раньше клиент писал
            # это сам (свой firebaseDB.ref('leaderboard/'+pid).set(...) в index.html) с
            # троттлингом в 60с И только пока вкладка активна на переднем плане — из-за
            # этого лидерборд мог отставать от реального баланса на произвольное время
            # (например, если игрок свернул приложение). Плюс это был прямой клиентский
            # write в публичный путь — теоретически можно было подправить значения в
            # консоли браузера перед отправкой и накрутить себе позицию без реальной игры.
            # Теперь: пишем на каждый /actions-запрос, значения уже посчитаны сервером —
            # отставания и подмены больше нет. username/firstName берём из Telegram
            # initData (тоже подписано и проверено, не из тела запроса от игрока).
            # PATCH — частичное слияние, не трогает поля, которые пишет только клиент
            # (num, playerName — их присвоение самим себе не даёт денежного профита).
            premium_until = 0
            try:
                async with session.get(f"{base}/premium/{pid}.json{FB_AUTH}") as presp:
                    premium_until = await presp.json()
                    if not isinstance(premium_until, (int, float)):
                        premium_until = 0
            except Exception:
                premium_until = 0  # сбой чтения premium — не должен блокировать запись в лидерборд ниже
            try:
                await session.patch(f"{base}/leaderboard/{pid}.json{FB_AUTH}", json={
                    "coins": round(coins),
                    "caught": caught,
                    "totalEarned": round(total_earned),
                    "loc": cur_loc,
                    "ts": now_ms,
                    "userId": real_user_id,
                    "username": tg_user.get('username') or '',
                    "firstName": tg_user.get('first_name') or '',
                    "premiumUntil": premium_until
                })
            except Exception:
                pass  # лидерборд не должен ронять сам запрос игрока

            # Диагностический лог для поиска гонки записи (несколько параллельных /actions
            # читают один и тот же стартовый баланс и перезаписывают друг друга — см. жалобы
            # игроков на "пропадающие" монеты). Ничего не решает сам по себе, только даёт
            # возможность УВИДЕТЬ два запроса с почти одинаковым временем и понять, что один
            # из них перезаписал результат другого. Хранится по 200 последних записей на
            # игрока (list-like push через уникальный ключ Firebase), не влияет на игровую
            # логику и не может провалить запрос — обёрнуто в отдельный try.
            try:
                await session.post(f"{base}/action_logs/{pid}.json{FB_AUTH}", json={
                    "ts": now_ms,
                    "coins_before": round(save_base_coins * 100) / 100,
                    "coins_after": coins,
                    "n_actions": len(actions)
                })
            except Exception:
                pass
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500, headers=CORS)

    return web.json_response({
        'ok': True,
        'coins': coins,
        'caught': caught,
        'totalEarned': total_earned,
        'upgLevels': upg_levels,
        'energy': round(energy * 100) / 100,
        'unsoldCaught': round(unsold * 100) / 100,
        'dailyDay': response_daily_day,
        'dailyLastClaim': response_daily_last_claim,
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

    # Рост coins должен быть подкреплён соответствующим ростом totalEarned — честно
    # заработанные монеты ВСЕГДА увеличивают оба поля вместе (тап/автодоход/продажа
    # добавляют одинаковую сумму к обоим). Единственный легальный способ, которым coins
    # растёт, а totalEarned нет, отсутствует — поэтому если клиент прислал coin_delta
    # больше, чем реально подтверждённый earned_delta, урезаем coins до заявленного
    # earned_delta. Это отдельная проверка ДО общего потолка ceiling, потому что раньше
    # оба потолка проверялись независимо и позволяли раздувать coins, не трогая
    # totalEarned (см. кейс @pskenny — coins на десятки тысяч при totalEarned:0).
    if coin_delta > 0:
        backed_delta = max(0.0, earned_delta)
        if coin_delta > backed_delta + 0.01:  # небольшой допуск на округление
            suspicious = True
            coin_delta = backed_delta
            final_coins = round((prev_coins + coin_delta) * 100) / 100

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
    # lastSeen сюда НЕ пишем — этим полем управляет исключительно /actions (там значение
    # читается ОДИН РАЗ в начале обработки и используется для расчёта авто-дохода и
    # комбэк-бонуса за реальное время отсутствия, а обновляется на "сейчас" только в конце
    # того же запроса). Если писать lastSeen ещё и здесь, /sync срабатывает уже через
    # несколько секунд после открытия игры и обнуляет реальное время отсутствия ДО того,
    # как игрок успеет забрать комбэк-бонус или сервер посчитает офлайн-доход — оба
    # оказываются урезаны почти до нуля. Подтверждённый случай: ID 8824257585, 8 дней
    # отсутствия, бонус +500 показан клиентом, но отклонён сервером как "не прошло 3 дня".
    try:
        async with aiohttp.ClientSession() as session:
            await session.patch(f"{base}/saves/{pid}.json{FB_AUTH}", json={
                "coins": final_coins,
                "caught": final_caught,
                "totalEarned": final_total_earned,
                "energy": final_energy,
                "lastEnergyUpdate": now_ms
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


async def broadcast_jackpot_win(username, amount):
    """
    Оповещает вас лично и рассылает всем игрокам объявление о выигрыше джекпота.
    Сброс самого джекпота на 50 делает вызывающий код (apply_lottery_prize) —
    эта функция только оповещает, не трогает Firebase-джекпот.
    """
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
        await bot.send_message(
            SUPPORT_GROUP_ID,
            f"🎰⭐ ДЖЕКПОТ ВЫИГРАН!\n👤 @{username}\n💰 {amount:,}⭐ Stars\n\nТребует выплаты звёздами!"
        )
    except Exception:
        pass

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                players = await resp.json()
    except Exception:
        return 0
    if not players:
        return 0

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
    return sent


async def jackpot_broadcast(request):
    """
    Устаревший путь — раньше клиент сам решал, что джекпот выигран, и звал этот endpoint.
    Теперь решение принимает только сервер (pick_lottery_prize + apply_lottery_prize),
    который сам вызывает broadcast_jackpot_win() напрямую. Эндпоинт оставлен для
    обратной совместимости, но требует точного совпадения суммы с реальным джекпотом.
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
        real_user = json.loads(verified.get('user', '{}'))
    except Exception:
        real_user = {}
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
    if not isinstance(amount, (int, float)) or amount < 50 or abs(amount - current_jackpot) > 1:
        return web.json_response({'error': 'сумма не совпадает с текущим джекпотом — используй /lottery_spin'}, status=400, headers=CORS)
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/jackpot/amount.json{FB_AUTH}", json=50)
    except Exception:
        pass

    sent = await broadcast_jackpot_win(username, amount)
    return web.json_response({'ok': True, 'sent': sent}, headers=CORS)


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(message.from_user, "🎣 Играть", "🎣 Play"), web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text=t(message.from_user, "💬 Чат игроков", "💬 Player chat"), url="https://t.me/+cLBHDCmOkaA3NWQy")]
    ])
    await message.answer(
        t(message.from_user,
            "🐟 <b>Добро пожаловать в FishFarm!</b> 🎣\n\n"
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
            "🐟 <b>Welcome to FishFarm!</b> 🎣\n\n"
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
        parse_mode="HTML",
        reply_markup=keyboard
    )

    user = message.from_user
    user_id = str(user.id)

    # Обрабатываем реферальную ссылку (нужно определить ref_arg ДО уведомления админу,
    # чтобы сразу видеть в самом уведомлении, долетел ли параметр ?start=... вообще —
    # это помогает диагностировать случаи, когда трекинг-ссылка теряет параметр где-то
    # на стороне рекламной площадки/редиректа ещё до того, как Telegram передаст его боту).
    args = message.text.split() if message.text else []
    ref_arg = args[1] if len(args) > 1 else ''

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
            param_line = f"🔗 Параметр старта: <code>{ref_arg}</code>" if ref_arg else "🔗 Параметр старта: (пусто — пришёл без ?start=)"
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🆕 Новый игрок!\n👤 {name}\n🆔 {user.id}\n{param_line}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    if ref_arg.startswith('campaign_'):
        # Метка рекламного источника (?start=campaign_НАЗВАНИЕ) — отдельно от реферальной
        # системы, просто считает, сколько новых регистраций пришло с конкретной площадки.
        campaign_name = ref_arg[len('campaign_'):]
        if campaign_name:
            import aiohttp, time
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            try:
                async with aiohttp.ClientSession() as session:
                    await session.put(
                        f"{base}/campaign_sources/{campaign_name}/{user_id}.json{FB_AUTH}",
                        json=int(time.time() * 1000)
                    )
            except Exception:
                pass
        return

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
            "Использование:\n<code>/addcoins @username СУММА</code>\n\nПример:\n<code>/addcoins @nikolanaz 500</code>",
            parse_mode="HTML"
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
                f"🎁 <b>Администратор начислил тебе {amount} монет!</b>\n\n"
                f"Зайди в игру чтобы получить их 👇",
                parse_mode="HTML",
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
            "Заходи в игру и проверь свою позицию в разделе 🏆 Лидеры!\n\n"
            "🇬🇧 *WEEKLY TOURNAMENT HAS STARTED!*\n\n"
            "Earn as many coins as you can in 48 hours! 🪙⚡\n\n"
            "Prize pool — 200 Stars ⭐ for the top 3:\n"
            "🥇 1st place — 85⭐\n"
            "🥈 2nd place — 65⭐\n"
            "🥉 3rd place — 50⭐\n\n"
            "Open the game and check your spot in the 🏆 Leaders section!"
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
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=keyboard)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await message.answer(f"📨 Анонс отправлен {sent} игрокам.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('fixlbuid'))
async def fixlbuid_command(message: types.Message):
    """
    Точечный фикс: если запись leaderboard/{pid} существует, но в ней нет userId
    (например, из-за старого частичного PATCH, который создал узел только с одним
    полем) — /tournamentstats и другие места молча пропускают такого игрока.
    Эта команда просто дописывает userId, не трогая totalEarned/coins.
    Использование: /fixlbuid 8791844749
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование:\n<code>/fixlbuid 8791844749</code>", parse_mode="HTML")
        return
    target_id = args[1]
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            lb_url = f"{base}/leaderboard/tg_{target_id}.json{FB_AUTH}"
            async with session.get(lb_url) as resp:
                lb_entry = await resp.json()
            if not lb_entry:
                await message.answer(f"❌ Записи leaderboard/tg_{target_id} не существует вообще — тут нужен полноценный /actions от игрока, а не точечный фикс.")
                return
            if lb_entry.get('userId'):
                await message.answer(f"У {target_id} userId уже на месте ({lb_entry.get('userId')}), ничего не делал.")
                return
            await session.patch(lb_url, json={"userId": int(target_id)})
        await message.answer(f"✅ {target_id}: userId проставлен в leaderboard. totalEarned/coins не трогал.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('addtotalearned'))
async def addtotalearned_command(message: types.Message):
    """
    Ручное разовое доначисление totalEarned (и синхронизация leaderboard) на заданную
    сумму — для случаев, когда пропущенный доход посчитан вручную (например, по
    скриншотам чата), а не автоматически по флагам в базе. coins НЕ трогает.
    Использование: /addtotalearned 8791844749 2800
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 3 or not args[1].isdigit():
        await message.answer("Использование:\n<code>/addtotalearned 8791844749 2800</code>", parse_mode="HTML")
        return
    target_id = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        await message.answer("❌ Сумма должна быть числом.")
        return
    if amount <= 0 or amount > 1_000_000:
        await message.answer("❌ Некорректная сумма.")
        return

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            save_url = f"{base}/saves/tg_{target_id}.json{FB_AUTH}"
            new_total_earned = None
            for attempt in range(6):
                async with session.get(save_url, headers={"X-Firebase-ETag": "true"}) as r:
                    etag = r.headers.get("ETag")
                    sv = await r.json()
                sv = sv or {}
                merged = dict(sv)
                new_total_earned = round((float(sv.get('totalEarned', 0) or 0) + amount) * 100) / 100
                merged['totalEarned'] = new_total_earned
                headers = {"If-Match": etag} if etag else {}
                async with session.put(save_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue
                    if put_resp.status not in (200, 204):
                        await message.answer(f"❌ saves PUT failed: {put_resp.status}")
                        return
                    break

            try:
                await session.patch(f"{base}/leaderboard/tg_{target_id}.json{FB_AUTH}", json={
                    "totalEarned": round(new_total_earned),
                    "coins": round(float(merged.get('coins', 0) or 0)),
                    "userId": int(target_id)
                })
            except Exception:
                pass

        await message.answer(
            f"✅ {target_id}: totalEarned +{amount:,.0f} → теперь {new_total_earned:,.0f}\n"
            f"Лидерборд обновлён. coins не трогали."
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('fixmissedref'))
async def fixmissedref_command(message: types.Message):
    """
    Одноразовая ручная компенсация для игроков, задетых багом: бонус +1000 за прокачку
    удочки реферала до ур.2 раньше писался ТОЛЬКО в coins, не в totalEarned — из-за
    этого доход был реальным (баланс рос), но невидимым для лидерборда/турниров.
    Баг исправлен для новых начислений; эта команда добивает totalEarned+leaderboard
    задним числом для уже накопленных, но не учтённых бонусов.
    НЕ трогает coins (он уже корректен) — только totalEarned и leaderboard.
    Использование: /fixmissedref 8791844749
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование:\n<code>/fixmissedref 8791844749</code>", parse_mode="HTML")
        return
    referrer_id = args[1]
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            # Список рефералов этого игрока
            async with session.get(f"{base}/referrals/by/{referrer_id}.json{FB_AUTH}") as resp:
                refs = await resp.json()
            if not refs:
                await message.answer(f"У {referrer_id} нет рефералов в базе.")
                return

            # Считаем, сколько из них реально принесли бонус за удочку ур.2
            rewarded_count = 0
            for ref_uid in refs.keys():
                async with session.get(f"{base}/referrals/rod2_rewarded/{ref_uid}.json{FB_AUTH}") as resp:
                    flag = await resp.json()
                if flag:
                    rewarded_count += 1

            if rewarded_count == 0:
                await message.answer(f"У {referrer_id} нет начислений за удочку ур.2 — доначислять нечего.")
                return

            missing_amount = rewarded_count * 1000

            # Доначисляем totalEarned (ETag-защищённо, coins НЕ трогаем — он уже верный)
            save_url = f"{base}/saves/tg_{referrer_id}.json{FB_AUTH}"
            new_total_earned = None
            for attempt in range(6):
                async with session.get(save_url, headers={"X-Firebase-ETag": "true"}) as r:
                    etag = r.headers.get("ETag")
                    sv = await r.json()
                sv = sv or {}
                merged = dict(sv)
                new_total_earned = round((float(sv.get('totalEarned', 0) or 0) + missing_amount) * 100) / 100
                merged['totalEarned'] = new_total_earned
                headers = {"If-Match": etag} if etag else {}
                async with session.put(save_url, json=merged, headers=headers) as put_resp:
                    if put_resp.status == 412:
                        continue
                    if put_resp.status not in (200, 204):
                        await message.answer(f"❌ saves PUT failed: {put_resp.status}")
                        return
                    break

            # Синхронизируем лидерборд тем же значением
            try:
                await session.patch(f"{base}/leaderboard/tg_{referrer_id}.json{FB_AUTH}", json={
                    "totalEarned": round(new_total_earned),
                    "coins": round(float(merged.get('coins', 0) or 0)),
                    "userId": int(referrer_id)
                })
            except Exception:
                pass

        await message.answer(
            f"✅ Готово для {referrer_id}:\n"
            f"Найдено начислений за удочку ур.2: {rewarded_count} × 1000 = {missing_amount:,} монет\n"
            f"totalEarned доначислен, лидерборд обновлён.\n"
            f"coins не трогали — он уже был корректным."
        )
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


@dp.message(Command('getchatid'))
async def getchatid_command(message: types.Message):
    """
    Написать эту команду ПРЯМО В ГРУППЕ рекламодателя (после добавления туда бота) —
    покажет chat_id этой группы, нужен для /addsocial.
    """
    await message.answer(f"🆔 Chat ID этой группы: <code>{message.chat.id}</code>", parse_mode="HTML")


@dp.message(Command('addsocial'))
async def addsocial_command(message: types.Message):
    """
    Добавить социальное задание (вступи в группу за монеты).
    Формат: /addsocial ССЫЛКА|CHAT_ID|НАГРАДА|НАЗВАНИЕ
    Пример: /addsocial https://t.me/+abc123|-1001234567890|100|Крипто-канал XYZ
    Бот ОБЯЗАТЕЛЬНО должен уже быть добавлен в группу — иначе проверка вступления не сработает.
    """
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text[len('/addsocial'):].strip()
    parts = raw.split('|')
    if len(parts) < 4:
        await message.answer(
            "Использование:\n<code>/addsocial ССЫЛКА|CHAT_ID|НАГРАДА|НАЗВАНИЕ</code>\n\n"
            "Пример:\n<code>/addsocial https://t.me/+abc123|-1001234567890|100|Крипто-канал XYZ</code>\n\n"
            "CHAT_ID узнать командой /getchatid, написанной ПРЯМО В ГРУППЕ рекламодателя "
            "(бот должен быть туда уже добавлен).",
            parse_mode="HTML"
        )
        return
    link = parts[0].strip()
    try:
        chat_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ CHAT_ID должен быть числом (обычно отрицательным для групп). Используй /getchatid в группе.")
        return
    try:
        reward = float(parts[2].strip())
    except ValueError:
        await message.answer("❌ Награда должна быть числом.")
        return
    label = parts[3].strip()

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    task_id = f"task_{int(time.time())}"
    try:
        # Проверяем, что бот реально в этой группе, прежде чем сохранять задание
        await bot.get_chat(chat_id)
    except Exception as e:
        await message.answer(f"❌ Бот не может получить доступ к этой группе (добавлен ли он туда?): {e}")
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/social_tasks/{task_id}.json{FB_AUTH}", json={
                "link": link, "chat_id": chat_id, "reward": reward, "label": label, "active": True
            })
        await message.answer(f"✅ Задание добавлено: {label} (+{reward}🪙)\nID: <code>{task_id}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('addsocialbot'))
async def addsocialbot_command(message: types.Message):
    """
    Соц.задание за переход/старт в БОТЕ партнёра (взаимный кросс-промо), а не за
    вступление в группу/канал. getChatMember тут не подходит — Telegram не даёт узнать,
    нажимал ли произвольный пользователь /start у чужого бота. Поэтому проверка идёт
    через API партнёра: он должен поднять у себя эндпоинт в ТОЧНО ТОМ ЖЕ формате, что
    и наш собственный /api/check (GET ?apiKey=...&telegramId=... -> {"completed": bool}),
    и прислать нам его URL + ключ. Это симметрично тому, что мы уже даём партнёрам сами.
    Формат: /addsocialbot ССЫЛКА_НА_БОТА|VERIFY_URL|VERIFY_KEY|НАГРАДА|НАЗВАНИЕ
    Пример: /addsocialbot https://t.me/PartnerBot?start=fishfarm|https://partner.com/api/check|SECRET123|150|Партнёр XYZ
    """
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text[len('/addsocialbot'):].strip()
    parts = raw.split('|')
    if len(parts) < 5:
        await message.answer(
            "Использование:\n<code>/addsocialbot ССЫЛКА_НА_БОТА|VERIFY_URL|VERIFY_KEY|НАГРАДА|НАЗВАНИЕ</code>\n\n"
            "Пример:\n<code>/addsocialbot https://t.me/PartnerBot?start=fishfarm|https://partner.com/api/check|SECRET123|150|Партнёр XYZ</code>\n\n"
            "VERIFY_URL — эндпоинт ПАРТНЁРА, который отвечает {\"completed\": true/false} по\n"
            "GET-запросу ?apiKey=VERIFY_KEY&telegramId=ID — попроси у партнёра сделать так же,\n"
            "как наш собственный /api/check (он написан именно под такой обмен).",
            parse_mode="HTML"
        )
        return
    link = parts[0].strip()
    verify_url = parts[1].strip()
    verify_key = parts[2].strip()
    try:
        reward = float(parts[3].strip())
    except ValueError:
        await message.answer("❌ Награда должна быть числом.")
        return
    label = parts[4].strip()

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    task_id = f"task_{int(time.time())}"
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/social_tasks/{task_id}.json{FB_AUTH}", json={
                "type": "bot", "link": link, "verify_url": verify_url, "verify_key": verify_key,
                "reward": reward, "label": label, "active": True
            })
        await message.answer(f"✅ Бот-задание добавлено: {label} (+{reward}🪙)\nID: <code>{task_id}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('addsociallink'))
async def addsociallink_command(message: types.Message):
    """
    Простое кросс-промо БЕЗ проверки — считаем сам переход, а не подтверждённое
    выполнение условия у партнёра. Награда выдаётся сразу по клику "Забрать", один раз
    на игрока (защита от повторного клейма остаётся та же, что и у остальных соц.заданий —
    просто без шага верификации через getChatMember/API партнёра).
    Формат: /addsociallink ССЫЛКА|НАГРАДА|НАЗВАНИЕ
    Пример: /addsociallink https://t.me/PartnerBot|100|Партнёр XYZ
    """
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text[len('/addsociallink'):].strip()
    parts = raw.split('|')
    if len(parts) < 3:
        await message.answer(
            "Использование:\n<code>/addsociallink ССЫЛКА|НАГРАДА|НАЗВАНИЕ</code>\n\n"
            "Пример:\n<code>/addsociallink https://t.me/PartnerBot|100|Партнёр XYZ</code>\n\n"
            "Без проверки: награда выдаётся сразу по клику, один раз на игрока. "
            "Для проверки реального выполнения через API партнёра используй /addsocialbot.",
            parse_mode="HTML"
        )
        return
    link = parts[0].strip()
    try:
        reward = float(parts[1].strip())
    except ValueError:
        await message.answer("❌ Награда должна быть числом.")
        return
    label = parts[2].strip()

    import aiohttp, time
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    task_id = f"task_{int(time.time())}"
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(f"{base}/social_tasks/{task_id}.json{FB_AUTH}", json={
                "type": "link", "link": link, "reward": reward, "label": label, "active": True
            })
        await message.answer(f"✅ Задание-ссылка добавлено (без проверки): {label} (+{reward}🪙)\nID: <code>{task_id}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('campaignstats'))
async def campaignstats_command(message: types.Message):
    """
    Статистика по рекламным площадкам (ссылки вида ?start=campaign_НАЗВАНИЕ) —
    сколько новых регистраций реально пришло с конкретного источника, и сколько
    из них дошли до реальной игры (есть сохранение) и стали активными (есть уловы).
    Без аргумента — список всех кампаний с количеством. С аргументом — детали одной.
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/campaign_sources.json{FB_AUTH}") as resp:
                data = await resp.json()
            if not data:
                await message.answer("Пока нет данных ни по одной рекламной кампании.")
                return

            async def conversion_stats(users: dict) -> tuple[int, int]:
                # Заходов (стартов бота по ссылке) в campaign_sources всегда >= установок,
                # т.к. сюда падает КАЖДЫЙ /start с этим параметром, даже без открытия игры.
                # "Установил" = есть сохранение в saves/tg_{user_id}.
                # "Активен" = в сохранении есть хотя бы 1 улов ИЛИ totalEarned > 0.
                installed = 0
                active = 0
                for uid in users:
                    pid = f"tg_{uid}"
                    async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as r:
                        save = await r.json()
                    if not save:
                        continue
                    installed += 1
                    if (save.get('caught', 0) or 0) > 0 or (save.get('totalEarned', 0) or 0) > 0:
                        active += 1
                return installed, active

            if len(args) < 2:
                lines = ["📊 Рекламные кампании:\n"]
                for name, users in data.items():
                    if not isinstance(users, dict) or not users:
                        continue
                    cnt = len(users)
                    installed, active = await conversion_stats(users)
                    inst_pct = (installed / cnt * 100) if cnt else 0
                    act_pct = (active / cnt * 100) if cnt else 0
                    lines.append(
                        f"  `{name}` — {cnt} заходов → {installed} установок ({inst_pct:.0f}%) → {active} активных ({act_pct:.0f}%)"
                    )
                lines.append("\nПодробности: `/campaignstats НАЗВАНИЕ`")
                await message.answer("\n".join(lines), parse_mode="HTML")
            else:
                name = args[1]
                users = data.get(name)
                if not users or not isinstance(users, dict):
                    await message.answer(f"Кампания <code>{name}</code> не найдена.", parse_mode="HTML")
                    return
                cnt = len(users)
                installed, active = await conversion_stats(users)
                inst_pct = (installed / cnt * 100) if cnt else 0
                act_pct = (active / cnt * 100) if cnt else 0
                await message.answer(
                    f"📊 Кампания <code>{name}</code>\n"
                    f"  Заходов по ссылке: {cnt}\n"
                    f"  Установили игру: {installed} ({inst_pct:.0f}%)\n"
                    f"  Стали активны (есть улов/заработок): {active} ({act_pct:.0f}%)",
                    parse_mode="HTML"
                )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('listsocial'))
async def listsocial_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/social_tasks.json{FB_AUTH}") as resp:
                tasks = await resp.json()
        if not tasks:
            await message.answer("Нет социальных заданий.")
            return
        lines = ["📋 Социальные задания:\n"]
        for tid, t in tasks.items():
            status = "🟢" if t.get('active') else "🔴"
            kind = "🤖" if t.get('type') == 'bot' else "🔗" if t.get('type') == 'link' else "👥"
            lines.append(f"{status}{kind} `{tid}` — {t.get('label')} (+{t.get('reward')}🪙)")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('removesocial'))
async def removesocial_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer("Использование:\n<code>/removesocial task_ID</code>\n\nID смотри через /listsocial", parse_mode="HTML")
        return
    task_id = args[1]
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            await session.delete(f"{base}/social_tasks/{task_id}.json{FB_AUTH}")
        await message.answer(f"✅ Задание {task_id} удалено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('comm'))
async def comm_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Команды администратора:</b>\n\n"
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
        "/actionlog @username [ДД.MM] — журнал действий игрока (по МСК-дате, опционально)\n"
        "/maintenance on|off — включить/выключить технические работы\n"
        "/premium @username [дни] — проверить/выдать/отозвать Premium\n"
        "/breakref @username — разорвать реферальную связь (для круговых цепочек)\n"
        "/breakref_all @username — разорвать ВСЕ реферальные связи этого реферера разом\n"
        "/ban_referrals @username — забанить всех рефералов этого реферера разом (фермы ботов)\n"
        "/ban_ids 111 222 333 — забанить конкретный список ID (если рефералы вперемешку — боты и настоящие)\n"
        "/delnum НОМЕР — удалить анонимную запись без username/ID (напр. «Рыбак #478»)\n"
        "/ban @username — удалить игрока и заблокировать вход\n"
        "/pay @username СУММА — уведомить игрока о выплате GRAM\n"
        "/paystars @username СУММА — уведомить о выплате Stars (джекпот)\n"
        "/broadcast ТЕКСТ — рассылка всем игрокам\n"
        "/pushcomeback ТЕКСТ — пуш только тем, кто заходил 1-3 дня назад\n"
        "/startpromo — запустить акцию +500🪙 за Сеть на 24ч\n"
        "/stoppromo — остановить акцию\n"
        "/getchatid — узнать ID группы (написать прямо в группе рекламодателя)\n"
        "/addsocial ССЫЛКА|CHAT_ID|НАГРАДА|НАЗВАНИЕ — добавить соц.задание (группа/канал)\n"
        "/addsocialbot ССЫЛКА|VERIFY_URL|VERIFY_KEY|НАГРАДА|НАЗВАНИЕ — соц.задание за бота-партнёра\n"
        "/addsociallink ССЫЛКА|НАГРАДА|НАЗВАНИЕ — соц.задание-ссылка БЕЗ проверки (сразу по клику)\n"
        "/listsocial — список соц.заданий\n"
        "/removesocial ID — удалить соц.задание\n"
        "/campaignstats [НАЗВАНИЕ] — статистика по рекламным кампаниям\n"
        "/starttournament — запустить турнир недели (48ч, рассылка всем)\n"
        "/stoptournament — остановить турнир досрочно\n"
        "/tournamentstats — рейтинг турнира\n"
        "/comm — список команд\n\n"
        "🎮 <b>Команды для всех:</b>\n\n"
        "/start — запустить игру\n\n"
        "💬 Чат игроков: https://t.me/+cLBHDCmOkaA3NWQy",
        parse_mode="HTML"
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
        await message.answer("Использование:\n<code>/playerinfo @username</code> или <code>/playerinfo 123456789</code> (по ID)", parse_mode="HTML")
        return
    arg = args[1].lstrip('@')
    import aiohttp, time
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            if arg.isdigit():
                uid = int(arg)
                async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                    lb = await resp.json()
                lb_entry = None
                if lb:
                    for v in lb.values():
                        if v.get('userId') == uid:
                            lb_entry = v
                            break
                username = (lb_entry.get('username') if lb_entry and lb_entry.get('username') else None)
            else:
                username = arg.lower()
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
                    await message.answer(f"❌ Игрок @{username} не найден в лидерборде. Попробуй по ID.")
                    return

            pid = f"tg_{uid}"
            async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as resp:
                sv = await resp.json()
            if not sv:
                await message.answer(f"❌ Нет сохранения для ID {uid}.")
                return

            async with session.get(f"{base}/referrals/used/{uid}.json{FB_AUTH}") as resp:
                referrer_id = await resp.json()

            async with session.get(f"{base}/known_starts/{uid}.json{FB_AUTH}") as resp:
                known_start = await resp.json()

            async with session.get(f"{base}/premium/{pid}.json{FB_AUTH}") as resp:
                premium_until = await resp.json()

            async with session.get(f"{base}/referrals/by/{uid}.json{FB_AUTH}") as resp:
                referred_by_him = await resp.json()

        display_name = f"@{username}" if username else "без ника"
        lines = [f"👤 {display_name} (ID: {uid})"]

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
        dur = sv.get('durability', {})
        dur_str = ", ".join(f"{k}:{v}%" for k, v in dur.items()) if dur else "—"
        owned = sv.get('unlockedTransports', ['bike'])
        owned_str = ", ".join(owned) if owned else "bike"
        lines.append("")
        lines.append(f"🚛 Куплено: {owned_str}")
        lines.append(f"🚛 Сейчас выбран: {transport} ({dur_str})")

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
        ref_count = len(referred_by_him) if isinstance(referred_by_him, dict) else 0
        lines.append(f"👥 Сам пригласил рефералов: {ref_count}")

        lottery_stats = sv.get('lotteryStats') or {}
        if isinstance(lottery_stats, dict) and lottery_stats:
            ad_spins = lottery_stats.get('adSpins', 0)
            star_spins = lottery_stats.get('starSpins', 0)
            prem_spins = lottery_stats.get('premiumSpins', 0)
            total_spins = ad_spins + star_spins + prem_spins
            wins = lottery_stats.get('wins') or {}
            wins_str = ", ".join(f"{k}:{v}" for k, v in wins.items()) if wins else "—"
            lines.append(
                f"🎰 Лотерея: {total_spins} круток (реклама:{ad_spins}, ⭐:{star_spins}, premium:{prem_spins})\n"
                f"   Выигрыши по типам: {wins_str}"
            )

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


@dp.message(Command('actionlog'))
async def actionlog_command(message: types.Message):
    """
    Показывает записи action_logs/{pid} — снимки coins_before/coins_after на каждый
    вызов /actions. Нужно для поиска гонки записи (два параллельных запроса от одного
    игрока читают один и тот же стартовый баланс и перезаписывают друг друга — в логе
    это видно как два запроса с почти одинаковым ts, где coins_after одного не
    совпадает с coins_before следующего).
    Без даты — последние 40 записей. С датой (ДД.ММ, по МСК) — все записи за этот день.
    Примеры: /actionlog @username
             /actionlog @username 26.08
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n<code>/actionlog @username</code> или <code>/actionlog 123456789</code> (по ID)\n"
            "С датой (МСК): <code>/actionlog @username 26.08</code>",
            parse_mode="HTML"
        )
        return
    arg = args[1].lstrip('@')
    import aiohttp
    from datetime import datetime, timezone, timedelta
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"

    day_start_ms = day_end_ms = None
    if len(args) >= 3:
        try:
            day, month = args[2].split('.')
            year = datetime.now(timezone(timedelta(hours=3))).year
            day_start = datetime(year, int(month), int(day), 0, 0, 0, tzinfo=timezone(timedelta(hours=3)))
            day_end = day_start + timedelta(days=1)
            day_start_ms = int(day_start.timestamp() * 1000)
            day_end_ms = int(day_end.timestamp() * 1000)
        except Exception:
            await message.answer("❌ Дата должна быть в формате ДД.ММ, например 26.08")
            return

    try:
        async with aiohttp.ClientSession() as session:
            if arg.isdigit():
                uid = int(arg)
            else:
                username = arg.lower()
                async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                    lb = await resp.json()
                uid = None
                if lb:
                    for v in lb.values():
                        if str(v.get('username', '')).lower() == username:
                            uid = v.get('userId')
                            break
                if not uid:
                    await message.answer(f"❌ Игрок @{username} не найден в лидерборде. Попробуй по ID.")
                    return

            pid = f"tg_{uid}"
            async with session.get(f"{base}/action_logs/{pid}.json{FB_AUTH}") as resp:
                logs = await resp.json()

            if not logs:
                await message.answer(f"Логов по ID {uid} пока нет.")
                return

            items = sorted(logs.values(), key=lambda x: x.get('ts', 0))

            if day_start_ms is not None:
                # Ищем расхождения по ПОЛНОЙ истории (чтобы поймать и стык с предыдущим
                # днём), но показываем только записи за запрошенную дату.
                to_show = [e for e in items if day_start_ms <= e.get('ts', 0) < day_end_ms]
                if not to_show:
                    await message.answer(f"За {args[2]} записей по ID {uid} не найдено.")
                    return
                header = f"📜 Лог /actions для ID {uid} за {args[2]} ({len(to_show)} записей):\n"
            else:
                to_show = items[-40:]
                header = f"📜 Лог /actions для ID {uid} (последние {min(len(items), 40)} из {len(items)}):\n"

            # prev_after считаем от записи ПЕРЕД первой показанной — так стык суток
            # тоже проверяется на расхождение, а не только записи внутри одного дня.
            first_shown_ts = to_show[0].get('ts', 0)
            prev_after = None
            for e in items:
                if e.get('ts', 0) >= first_shown_ts:
                    break
                prev_after = e.get('coins_after')

            all_lines = []
            for entry in to_show:
                ts = entry.get('ts', 0)
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone(timedelta(hours=3)))
                before = entry.get('coins_before', 0)
                after = entry.get('coins_after', 0)
                n = entry.get('n_actions', '?')
                src = entry.get('src')  # 'lottery_ad'/'lottery_premium'/'lottery_star' — помечаем отдельно,
                # чтобы честные призы лотереи не путались с подозрительными скачками при аудите
                src_label = f" [{src}:{entry.get('prize')}]" if src else ""
                # Помечаем подозрительные случаи: этот запрос начал считать НЕ от того
                # баланса, на котором закончился предыдущий обработанный запрос — явный
                # признак гонки (второй запрос не увидел результат первого).
                mismatch = " ⚠️ РАСХОЖДЕНИЕ" if (prev_after is not None and abs(before - prev_after) > 0.01) else ""
                all_lines.append(f"{dt.strftime('%d.%m %H:%M:%S')} · было {before:,.0f} → стало {after:,.0f} ({n} действ.){src_label}{mismatch}")
                prev_after = after

            # Telegram режет сообщения длиннее 4096 символов — за активный день может
            # набраться и 500+ записей, что легко превышает лимит. Вместо обрезки (раньше
            # теряли часть дня молча/с ошибкой) разбиваем на несколько сообщений подряд —
            # видно ВСЁ, просто несколькими сообщениями.
            CHUNK_SIZE = 50
            chunks = [all_lines[i:i + CHUNK_SIZE] for i in range(0, len(all_lines), CHUNK_SIZE)]
            for i, chunk in enumerate(chunks):
                prefix = header if i == 0 else f"📜 (продолжение {i+1}/{len(chunks)})\n"
                await message.answer(prefix + "\n".join(chunk))
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
            "<code>/maintenance on</code> — включить технические работы (игра покажет заглушку)\n"
            "<code>/maintenance off</code> — выключить, игра снова доступна",
            parse_mode="HTML"
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


@dp.message(Command('fix_ref_index'))
async def fix_ref_index_command(message: types.Message):
    """
    Разовая починка: биржа рефералов писала связь только в referrals/used (кто чей реферал),
    но забывала дублировать в referrals/by (обратный индекс, "кого я пригласил") — из-за
    этого купленные на бирже рефералы не отображались у покупателя в списке друзей.
    Проходит по всем referrals/used и дописывает недостающие записи в referrals/by.
    """
    if message.from_user.id != ADMIN_ID:
        return
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/referrals/used.json{FB_AUTH}") as resp:
                used = await resp.json()
            async with session.get(f"{base}/referrals/by.json{FB_AUTH}") as resp2:
                by = await resp2.json()

            used = used or {}
            by = by or {}
            fixed = 0
            for target_id, referrer_id in used.items():
                if not referrer_id:
                    continue
                referrer_id = str(referrer_id)
                existing = by.get(referrer_id, {})
                if not isinstance(existing, dict) or target_id not in existing:
                    await session.put(f"{base}/referrals/by/{referrer_id}/{target_id}.json{FB_AUTH}", json=True)
                    fixed += 1

        await message.answer(f"🔧 Восстановлено недостающих связей в referrals/by: {fixed}")
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
            "<code>/premium @username</code> — проверить статус\n"
            "<code>/premium @username 30</code> — выдать/продлить на N дней вручную\n"
            "<code>/premium @username 0</code> — отозвать подписку",
            parse_mode="HTML"
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
            "Использование:\n<code>/breakref @username</code> или <code>/breakref 123456789</code> (по ID)\n\n"
            "Убирает связь «кем был приглашён» у указанного игрока — "
            "используется для разрыва круговых реферальных цепочек (A пригласил B, B пригласил A), "
            "или чтобы сделать игрока снова доступным на бирже рефералов.",
            parse_mode="HTML"
        )
        return
    arg = args[1].lstrip('@')
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            if arg.isdigit():
                target_uid = arg
                display_name = f"ID:{target_uid}"
            else:
                username = arg.lower()
                async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                    lb = await resp.json()
                target_uid = None
                if lb:
                    for v in lb.values():
                        if str(v.get('username', '')).lower() == username:
                            target_uid = str(v.get('userId', ''))
                            break
                if not target_uid:
                    await message.answer(f"❌ Игрок @{username} не найден в лидерборде. Попробуй по ID.")
                    return
                display_name = f"@{username}"

            async with session.get(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}") as resp:
                old_referrer = await resp.json()

            if not old_referrer:
                await message.answer(f"— У {display_name} и так нет реферера, разрывать нечего.")
                return

            await session.delete(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}")
            await session.delete(f"{base}/referrals/by/{old_referrer}/{target_uid}.json{FB_AUTH}")

        await message.answer(f"✅ Связь разорвана: {display_name} больше не считается рефералом ID:{old_referrer}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('breakref_all'))
async def breakref_all_command(message: types.Message):
    """
    Массовый разрыв ВСЕХ рефералов конкретного реферера — для случаев фермы ботов
    (сотни-тысячи фейковых аккаунтов, приведённых по одной ссылке). Разрывать по одному
    через /breakref в таком масштабе нереально.
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n<code>/breakref_all @username</code> или <code>/breakref_all 123456789</code> (по ID)\n\n"
            "⚠️ Разрывает ВСЕ реферальные связи указанного реферера разом — используй для ферм ботов, "
            "не для единичных случаев (там <code>/breakref</code>).",
            parse_mode="HTML"
        )
        return
    arg = args[1].lstrip('@')
    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            if arg.isdigit():
                referrer_uid = arg
            else:
                username = arg.lower()
                async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                    lb = await resp.json()
                referrer_uid = None
                if lb:
                    for v in lb.values():
                        if str(v.get('username', '')).lower() == username:
                            referrer_uid = str(v.get('userId', ''))
                            break
                if not referrer_uid:
                    await message.answer(f"❌ Игрок @{username} не найден в лидерборде. Попробуй по ID.")
                    return

            async with session.get(f"{base}/referrals/by/{referrer_uid}.json{FB_AUTH}") as resp:
                his_refs = await resp.json()

            if not his_refs or not isinstance(his_refs, dict):
                await message.answer(f"— У ID:{referrer_uid} нет рефералов, разрывать нечего.")
                return

            count = 0
            for target_uid in his_refs.keys():
                await session.delete(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}")
                count += 1
            await session.delete(f"{base}/referrals/by/{referrer_uid}.json{FB_AUTH}")

        await message.answer(f"✅ Разорвано {count} реферальных связей у ID:{referrer_uid}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('delnum'))
async def delnum_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(
            "Использование:\n<code>/delnum 478</code>\n\n"
            "Удаляет запись из лидерборда/сохранения по номеру (<code>num</code>) — "
            "для анонимных записей без username и userId (например, старые эксплойт-аккаунты типа «Рыбак #478»).",
            parse_mode="HTML"
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
        await message.answer("Использование:\n<code>/ban @username</code>\n\nУдаляет игрока из лидерборда/турнира, стирает прогресс, убирает из рефералов и блокирует повторный вход.", parse_mode="HTML")
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


@dp.message(Command('ban_ids'))
async def ban_ids_command(message: types.Message):
    """
    Точечный бан по СПИСКУ конкретных ID — для случая, когда у игрока часть рефералов
    боты, а часть настоящие (в отличие от /ban_referrals, который сносит ВСЕХ разом).
    Для каждого ID: определяет ЕГО реферера (у разных ID из списка он может отличаться),
    банит (лидерборд+прогресс+запрет входа), разрывает связь именно с ЕГО реферером —
    остальные рефералы этого реферера не трогает.
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split(None, 1)
    if len(args) < 2:
        await message.answer(
            "Использование:\n<code>/ban_ids 111 222 333</code>\n(ID через пробел, запятую или с новой строки)\n\n"
            "⚠️ Банит перечисленные аккаунты (лидерборд+прогресс+запрет входа) и разрывает "
            "связь КАЖДОГО именно с ЕГО реферером — остальных рефералов не трогает.",
            parse_mode="HTML"
        )
        return
    raw = args[1].replace(',', ' ').replace('\n', ' ')
    target_uids = [t for t in raw.split() if t.isdigit()]
    target_uids = list(dict.fromkeys(target_uids))  # без дублей, порядок сохраняем
    if not target_uids:
        await message.answer("❌ Не нашёл ни одного числового ID во входных данных.")
        return

    import aiohttp, asyncio as _asyncio
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    await message.answer(f"⏳ Баню {len(target_uids)} аккаунтов пачками, это может занять пару минут...")

    try:
        async with aiohttp.ClientSession() as session:
            async def ban_one(target_uid):
                pid = f"tg_{target_uid}"
                try:
                    async with session.get(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}") as resp:
                        referrer_id = await resp.json()
                    await session.delete(f"{base}/leaderboard/{pid}.json{FB_AUTH}")
                    await session.delete(f"{base}/saves/{pid}.json{FB_AUTH}")
                    await session.delete(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}")
                    if referrer_id:
                        await session.delete(f"{base}/referrals/by/{referrer_id}/{target_uid}.json{FB_AUTH}")
                    await session.put(f"{base}/banned/{target_uid}.json{FB_AUTH}", json=True)
                    return True
                except Exception:
                    return False

            banned_count = 0
            chunk_size = 30
            for i in range(0, len(target_uids), chunk_size):
                chunk = target_uids[i:i+chunk_size]
                results = await _asyncio.gather(*[ban_one(u) for u in chunk])
                banned_count += sum(1 for r in results if r)

        await message.answer(f"✅ Забанено {banned_count} из {len(target_uids)} аккаунтов из списка.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('ban_referrals'))
async def ban_referrals_command(message: types.Message):
    """
    Массовый бан ВСЕХ рефералов конкретного реферера — для ферм ботов (сотни-тысячи
    фейковых аккаунтов по одной ссылке). Банит каждого (лидерборд+прогресс+блокировка входа),
    разрывает связь с реферером. Обрабатывает пачками, чтобы не зависнуть на тысяче аккаунтов.
    """
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n<code>/ban_referrals @username</code> или <code>/ban_referrals 123456789</code> (по ID реферера)\n\n"
            "⚠️ Банит ВСЕХ рефералов этого игрока разом (лидерборд+прогресс+запрет входа) — для ферм ботов.",
            parse_mode="HTML"
        )
        return
    arg = args[1].lstrip('@')
    import aiohttp, asyncio as _asyncio
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            if arg.isdigit():
                referrer_uid = arg
            else:
                username = arg.lower()
                async with session.get(f"{base}/leaderboard.json{FB_AUTH}") as resp:
                    lb = await resp.json()
                referrer_uid = None
                if lb:
                    for v in lb.values():
                        if str(v.get('username', '')).lower() == username:
                            referrer_uid = str(v.get('userId', ''))
                            break
                if not referrer_uid:
                    await message.answer(f"❌ Игрок @{username} не найден в лидерборде. Попробуй по ID.")
                    return

            async with session.get(f"{base}/referrals/by/{referrer_uid}.json{FB_AUTH}") as resp:
                his_refs = await resp.json()

            if not his_refs or not isinstance(his_refs, dict):
                await message.answer(f"— У ID:{referrer_uid} нет рефералов, банить некого.")
                return

            target_uids = list(his_refs.keys())
            await message.answer(f"⏳ Баню {len(target_uids)} аккаунтов пачками, это может занять пару минут...")

            async def ban_one(target_uid):
                pid = f"tg_{target_uid}"
                try:
                    await session.delete(f"{base}/leaderboard/{pid}.json{FB_AUTH}")
                    await session.delete(f"{base}/saves/{pid}.json{FB_AUTH}")
                    await session.delete(f"{base}/referrals/used/{target_uid}.json{FB_AUTH}")
                    await session.put(f"{base}/banned/{target_uid}.json{FB_AUTH}", json=True)
                    return True
                except Exception:
                    return False

            banned_count = 0
            chunk_size = 30
            for i in range(0, len(target_uids), chunk_size):
                chunk = target_uids[i:i+chunk_size]
                results = await _asyncio.gather(*[ban_one(u) for u in chunk])
                banned_count += sum(1 for r in results if r)

            await session.delete(f"{base}/referrals/by/{referrer_uid}.json{FB_AUTH}")

        await message.answer(f"✅ Забанено {banned_count} из {len(target_uids)} аккаунтов. Связь с реферером ID:{referrer_uid} разорвана.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('pay'))
async def pay_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n<code>/pay @username СУММА</code>\n\nПример:\n<code>/pay @Metelegram12 0.073</code>",
            parse_mode="HTML"
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
            f"✅ <b>Выплата выполнена!</b>\n\n"
            f"💎 {amount} GRAM отправлены на твой кошелёк.\n\n"
            f"Спасибо что играешь в FishFarm! 🎣",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
        await message.answer(f"✅ Уведомление отправлено @{username} (ID: {user_id}) о выплате {amount} GRAM")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('msg'))
async def msg_command(message: types.Message):
    """
    Отправить игроку произвольный текст от имени бота — по нику или по ID.
    Удобно для случаев, когда игроку нужно что-то передать лично (например, попросить
    написать вам напрямую, чтобы получить выплату, если у него нет username).
    """
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.split(maxsplit=2)
    if len(text) < 3:
        await message.answer(
            "Использование:\n<code>/msg @username ТЕКСТ</code> или <code>/msg 123456789 ТЕКСТ</code> (по ID)\n\n"
            "Пример:\n<code>/msg 7659448624 Привет! Напиши, пожалуйста, @elbanderass, чтобы получить свои звёзды за джекпот 🎉</code>",
            parse_mode="HTML"
        )
        return
    arg = text[1].lstrip('@')
    custom_text = text[2]
    import aiohttp
    try:
        if arg.isdigit():
            user_id = int(arg)
            display = f"ID:{user_id}"
        else:
            username = arg.lower()
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
                await message.answer(f"❌ Игрок @{username} не найден. Попробуй по ID.")
                return
            display = f"@{username}"
        await bot.send_message(user_id, custom_text)
        await message.answer(f"✅ Сообщение отправлено {display} (ID: {user_id})")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command('paystars'))
async def paystars_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n<code>/paystars @username СУММА</code> или <code>/paystars 123456789 СУММА</code> (по ID)\n\nПример:\n<code>/paystars @Metelegram12 27</code>",
            parse_mode="HTML"
        )
        return
    arg = text[1].lstrip('@')
    amount = text[2]
    import aiohttp
    try:
        if arg.isdigit():
            user_id = int(arg)
            display = f"ID:{user_id}"
        else:
            username = arg.lower()
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
                await message.answer(f"❌ Игрок @{username} не найден. Попробуй по ID.\nИмена в базе: {', '.join(found_names[:10])}")
                return
            display = f"@{username}"
        await bot.send_message(
            user_id,
            f"✅ <b>Выплата выполнена!</b>\n\n"
            f"⭐ {amount} Stars отправлены тебе.\n\n"
            f"Спасибо что играешь в FishFarm! 🎣",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
        await message.answer(f"✅ Уведомление отправлено {display} (ID: {user_id}) о выплате {amount}⭐ Stars")
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
                loc = v.get('loc') or 'pond'

                if caught == 0:
                    # При caught=0 потолок "по уловам" формально равен нулю (0 * что угодно = 0),
                    # что превращает ЛЮБОЙ стартовый/реферальный/дневной бонус (10-300 монет без
                    # единого улова — это нормально для только что зашедшего игрока) в "превышение
                    # потолка в 100x/300x раз". Это не эксплойт, а особенность формулы — здесь нечего
                    # делить на количество уловов. Проверяем только явное расхождение coins/totalEarned,
                    # как и в ветке с известной датой регистрации.
                    if coins > 5000 and total_earned < coins * 0.1:
                        flagged.append(
                            f"{identity} (ID:{user_id}) — БЕЗ ДАТЫ РЕГИСТРАЦИИ, РАСХОЖДЕНИЕ coins/totalEarned\n"
                            f"  📍 Локация: {loc} · 🐟 Поймано: {caught}\n"
                            f"  🪙 Баланс: {coins:,.0f} · Заработано: {total_earned:,.0f}\n"
                            f"  ⚠️ Баланс в {(coins/max(total_earned,1)):.0f}x больше заработанного — похоже на sync_state-эксплойт"
                        )
                    elif total_earned > 10000:
                        no_reg_date.append(f"{identity} | заработано:{total_earned:,.0f} | монет:{coins:,.0f} | поймано:{caught}")
                    continue

                # Без даты регистрации не проверить по времени — но можно проверить по количеству
                # уловов: даже в лучшем случае (макс. апгрейды на его локации, каждая рыба продана
                # как филе редкого вида по пиковой цене) есть потолок дохода НА ОДИН улов.
                loc_mult = LOCATION_MULT.get(loc, 1)
                max_tap_per_catch = ROD_TAP[-1] * loc_mult
                max_fish_price = LOCATION_MAX_FISH_PRICE.get(loc, 6)
                max_sale_per_catch = max_fish_price * MARKET_PRICE_MAX_MULT * FILET_SELL_MULT_EXACT
                catches_ceiling = caught * (max_tap_per_catch + max_sale_per_catch) * 2  # x2 запас, как и выше
                if total_earned > catches_ceiling:
                    flagged.append(
                        f"{identity} (ID:{user_id}) — БЕЗ ДАТЫ РЕГИСТРАЦИИ\n"
                        f"  📍 Локация: {loc} · 🐟 Поймано: {caught}\n"
                        f"  🪙 Баланс: {coins:,.0f} · Заработано: {total_earned:,.0f}\n"
                        f"  ⚠️ Потолок по уловам: {catches_ceiling:,.0f} — превышен в {(total_earned/max(catches_ceiling,1)):.1f}x раз"
                    )
                elif coins > 5000 and total_earned < coins * 0.1:
                    flagged.append(
                        f"{identity} (ID:{user_id}) — БЕЗ ДАТЫ РЕГИСТРАЦИИ, РАСХОЖДЕНИЕ coins/totalEarned\n"
                        f"  📍 Локация: {loc} · 🐟 Поймано: {caught}\n"
                        f"  🪙 Баланс: {coins:,.0f} · Заработано: {total_earned:,.0f}\n"
                        f"  ⚠️ Баланс в {(coins/max(total_earned,1)):.0f}x больше заработанного — похоже на sync_state-эксплойт"
                    )
                elif total_earned > 10000:
                    no_reg_date.append(f"{identity} | заработано:{total_earned:,.0f} | монет:{coins:,.0f} | поймано:{caught}")
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
            elif coins > 5000 and total_earned < coins * 0.1:
                # Баланс сильно больше заработанного — при честной игре coins не может
                # надолго превышать totalEarned (заработанное растёт вместе с балансом
                # и не уменьшается при тратах). Такое расхождение — сигнатура эксплойта
                # sync_state, где coins раздували без соответствующего роста totalEarned
                # (см. кейс @pskenny), а не старой дыры с прямой записью в Firebase.
                reg_dt = datetime.fromtimestamp(registered_at / 1000, tz=timezone(timedelta(hours=3)))
                age_min = elapsed_ms / 60000
                flagged.append(
                    f"{identity} (ID:{user_id}) — РАСХОЖДЕНИЕ coins/totalEarned\n"
                    f"  📅 Регистрация: {reg_dt.strftime('%d.%m.%Y %H:%M')} МСК ({age_min:,.0f} мин назад)\n"
                    f"  🪙 Баланс: {coins:,.0f} · Заработано: {total_earned:,.0f} · Поймано: {caught}\n"
                    f"  ⚠️ Баланс в {(coins/max(total_earned,1)):.0f}x больше заработанного — похоже на sync_state-эксплойт"
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
    check('Апгрейд: удочка ур.0 в Пруду = 200🪙', UPGRADE_COSTS['rod'][0] * LOCATION_MULT['pond'] == 200)
    check('Апгрейд: удочка ур.0 в Космосе = 10000🪙', UPGRADE_COSTS['rod'][0] * LOCATION_MULT['space'] == 10000)

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
            "Использование:\n<code>/broadcast Текст сообщения</code>\n\nПример:\n<code>/broadcast 🎉 Новое обновление! Заходи в игру!</code>",
            parse_mode="HTML"
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
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"📨 Отправлено: {success}\n"
            f"❌ Не доставлено: {failed}\n"
            f"👥 Всего: {total}",
            parse_mode="HTML"
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
            "Использование:\n<code>/pushcomeback Текст сообщения</code>\n\n"
            "Отправит только игрокам, которые заходили 1-3 дня назад "
            "(лучший момент вернуть в игру, пока не забыли).\n\n"
            "Пример:\n<code>/pushcomeback 🐡 Редкая рыба уже в пруду! Успей поймать!</code>",
            parse_mode="HTML"
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
                     'Premium подписка' if payload.startswith('sub:') else
                     'Слот клана' if payload.startswith('cs:') else
                     'Создание турнира клана' if payload.startswith('ctc:') else
                     'Взнос в турнир клана' if payload.startswith('ctp:') else
                     'Принять турнир клана' if payload.startswith('cta:') else
                     'Биржа рефералов' if payload.startswith('rb:') else payload)
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
        pid = f"tg_{user_id}"
        if boost_id == 'lottery':
            # Платная крутка за Stars — сервер сам решает приз здесь же, а не через
            # общий pending_boosts-таймер (который раньше запускал рулетку на клиенте).
            try:
                base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/saves/{pid}.json{FB_AUTH}") as resp:
                        sv = await resp.json()
                    sv = sv or {}
                    ulocs = sv.get('ulocs') or ['pond']
                    mult = 1
                    for loc_id in ulocs:
                        m = LOCATION_MULT.get(loc_id, 1)
                        if m > mult:
                            mult = m
                    async with session.get(f"{base}/jackpot/amount.json{FB_AUTH}") as resp2:
                        jackpot = await resp2.json()
                    jackpot = jackpot if (jackpot and 50 <= jackpot <= 1000) else 50
                username = message.from_user.username or message.from_user.first_name or 'Игрок'
                prize = pick_lottery_prize(mult, jackpot)
                await apply_lottery_prize(pid, prize, mult, True, username, 'star')
                async with aiohttp.ClientSession() as session:
                    await session.put(f"{base}/pending_boosts/{pid}/lottery_result.json{FB_AUTH}", json=prize)
            except Exception:
                pass
        elif boost_id == 'energyFull':
            # Заполнение энергии за Stars — раньше клиент только показывал полную шкалу
            # у себя локально, а сервер (который теперь решает энергию для /actions)
            # об этом не узнавал: через пару тапов после покупки сервер видел старое
            # низкое значение и начинал отклонять уловы. Пишем реальную энергию сюда же.
            try:
                base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
                max_energy = 150 if await is_premium(user_id) else 100
                import time
                async with aiohttp.ClientSession() as session:
                    await session.patch(f"{base}/saves/{pid}.json{FB_AUTH}", json={
                        "energy": max_energy,
                        "lastEnergyUpdate": int(time.time() * 1000)
                    })
                    url = f"{base}/pending_boosts/{pid}/{boost_id}.json{FB_AUTH}"
                    await session.put(url, json=int(time.time() * 1000))
            except Exception:
                pass
        else:
            try:
                url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/pending_boosts/{pid}/{boost_id}.json{FB_AUTH}"
                import time
                async with aiohttp.ClientSession() as session:
                    await session.put(url, json=int(time.time() * 1000))
            except Exception:
                pass

    elif payload.startswith('rb:'):
        # Биржа рефералов — покупатель оплатил, привязываем реферальную связь.
        # Перепроверяем ещё раз (на случай, если кто-то параллельно перехватил того же
        # игрока между созданием счёта и оплатой) — атомарности тут по сути нет, поэтому
        # смотрим ещё раз перед записью, чтобы не перезаписать чужую уже установленную связь.
        import aiohttp
        parts = payload.split(':')
        buyer_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        target_id = parts[2] if len(parts) > 2 else None
        if target_id:
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/referrals/used/{target_id}.json{FB_AUTH}") as resp:
                        existing = await resp.json()
                    if existing:
                        await message.answer(t(message.from_user,
                            "❌ Этого игрока уже купил кто-то другой. Свяжись с администратором для возврата.",
                            "❌ This player was already claimed by someone else. Contact the admin for a refund."))
                    else:
                        await session.put(f"{base}/referrals/used/{target_id}.json{FB_AUTH}", json=buyer_id)
                        await session.put(f"{base}/referrals/by/{buyer_id}/{target_id}.json{FB_AUTH}", json=True)
                        await message.answer(t(message.from_user,
                            "✅ Реферал куплен! Он привязан к тебе — бонусы с его выводов теперь твои.",
                            "✅ Referral purchased! They're now linked to you — bonuses from their withdrawals are yours."))
                        try:
                            await bot.send_message(
                                int(target_id),
                                "👥 У тебя появился реферер! Кто-то из игроков FishFarm пригласил тебя (задним числом) — "
                                "теперь при выводе монет часть уйдёт ему как бонус за приглашение, это никак не влияет на твои собственные выплаты."
                            )
                        except Exception:
                            pass
            except Exception as e:
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при покупке реферала (buyer:{buyer_id}, target:{target_id}): {e}")
                    except Exception:
                        pass
                await message.answer(t(message.from_user,
                    "❌ Что-то пошло не так при покупке — напиши администратору, разберёмся.",
                    "❌ Something went wrong with the purchase — message the admin, we'll sort it out."))

    elif payload.startswith('cs:'):
        # Платное расширение клана — captain оплатил слот, увеличиваем maxMembers на 1.
        # Перепроверяем состояние клана прямо перед записью (ETag) на случай гонки
        # с другим одновременным изменением клана (кик/выход и т.п.).
        import aiohttp
        parts = payload.split(':')
        captain_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        clan_id = parts[2] if len(parts) > 2 else None
        try:
            next_slot = int(parts[3]) if len(parts) > 3 else None
        except ValueError:
            next_slot = None
        if clan_id and next_slot:
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            try:
                ok = False
                async with aiohttp.ClientSession() as session:
                    for attempt in range(6):
                        async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as resp:
                            etag = resp.headers.get("ETag")
                            clan_data = await resp.json()
                        if not isinstance(clan_data, dict) or clan_data.get('captainId') != int(captain_id):
                            break
                        cur_max = clan_data.get('maxMembers', 2)
                        if cur_max != next_slot - 1:
                            # Слот уже открыт (например, повторная доставка вебхука) — не открываем второй раз.
                            ok = (cur_max >= next_slot)
                            break
                        # If-Match работает только с PUT, не с PATCH (PATCH+If-Match всегда 400) —
                        # мёржим maxMembers в уже прочитанный clan_data и пишем целиком.
                        headers = {"If-Match": etag} if etag else {}
                        merged_clan = dict(clan_data)
                        merged_clan['maxMembers'] = next_slot
                        async with session.put(f"{base}/clans/{clan_id}.json{FB_AUTH}", json=merged_clan, headers=headers) as put_resp:
                            if put_resp.status == 412:
                                continue
                            ok = put_resp.status in (200, 204)
                            break
                if ok:
                    await message.answer(t(message.from_user,
                        f"✅ Слот №{next_slot} открыт! Теперь можно пригласить нового участника из вкладки «Клан».",
                        f"✅ Slot #{next_slot} unlocked! You can now invite a new member from the Clan tab."))
                else:
                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"⚠️ Оплата слота клана не применилась (captain:{captain_id}, clan:{clan_id}, slot:{next_slot}) — нужна ручная проверка/возврат.")
                        except Exception:
                            pass
                    await message.answer(t(message.from_user,
                        "❌ Не удалось открыть слот (клан мог измениться). Напиши администратору — сверим и вернём звёзды при необходимости.",
                        "❌ Couldn't open the slot (the clan may have changed). Message the admin — we'll check and refund if needed."))
            except Exception as e:
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при открытии слота клана (captain:{captain_id}, clan:{clan_id}, slot:{next_slot}): {e}")
                    except Exception:
                        pass
                await message.answer(t(message.from_user,
                    "❌ Что-то пошло не так при открытии слота — напиши администратору, разберёмся.",
                    "❌ Something went wrong opening the slot — message the admin, we'll sort it out."))

    elif payload.startswith('ctc:'):
        # Капитан оплатил создание турнира — запись в clan_tournaments появляется только
        # сейчас (не раньше), чтобы брошенные/неоплаченные счета не плодили турниры-сироты.
        parts = payload.split(':')
        captain_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        clan_id = parts[2] if len(parts) > 2 else None
        try:
            amount = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            amount = 0
        if clan_id and amount >= 50:
            import aiohttp, time
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{captain_id}"
            try:
                async with aiohttp.ClientSession() as session:
                    # Перепроверяем, что платящий всё ещё капитан этого клана — мог успеть
                    # распустить клан или потерять капитанство между счётом и оплатой.
                    async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as cresp:
                        clan_data = await cresp.json()
                    if not isinstance(clan_data, dict) or clan_data.get('captainId') != int(captain_id):
                        if ADMIN_ID:
                            try:
                                await bot.send_message(ADMIN_ID, f"⚠️ Оплата создания турнира не применилась — клан {clan_id} изменился (captain:{captain_id}). Нужен ручной возврат {amount}⭐.")
                            except Exception:
                                pass
                        await message.answer(t(message.from_user,
                            "❌ Не удалось создать турнир — клан изменился. Напиши администратору, вернём звёзды.",
                            "❌ Couldn't create the tournament — the clan changed. Message the admin, we'll refund you."))
                        return
                    now_ms = int(time.time() * 1000)
                    player_name = message.from_user.username or message.from_user.first_name or f"Игрок {captain_id}"
                    tournament_payload = {
                        'initiatorClanId': clan_id,
                        'initiatorClanName': clan_data.get('name', ''),
                        'captainId': int(captain_id),
                        'amountPerPerson': amount,
                        'participantsA': {
                            pid: {'userId': int(captain_id), 'name': player_name, 'username': message.from_user.username or '', 'paidAt': now_ms, 'amount': amount}
                        },
                        'status': 'funding',
                        'createdAt': now_ms,
                        'fundingDeadline': now_ms + 6 * 3600 * 1000,
                    }
                    async with session.post(f"{base}/clan_tournaments.json{FB_AUTH}", json=tournament_payload) as presp:
                        push_result = await presp.json()
                    tournament_id = push_result.get('name') if isinstance(push_result, dict) else None
                    if tournament_id:
                        await message.answer(t(message.from_user,
                            f"✅ Турнир создан! Взнос {amount}⭐ засчитан. У клана есть 6 часов, чтобы собрать взносы остальных — или можешь зафиксировать турнир раньше прямо из вкладки «Клан».",
                            f"✅ Tournament created! Your {amount}⭐ stake is in. Your clan has 6 hours to gather the rest — or lock it in early from the Clan tab."))
                        members = clan_data.get('members') or {}
                        for m_pid, m in members.items():
                            m_uid = m.get('userId') if isinstance(m, dict) else None
                            if not m_uid or m_uid == int(captain_id):
                                continue
                            try:
                                await bot.send_message(m_uid, t(message.from_user,
                                    f"🏆 Капитан вашего клана «{clan_data.get('name', '')}» создал турнир со ставкой {amount}⭐! Зайди во вкладку «Клан», чтобы внести свой взнос — у клана есть 6 часов.",
                                    f"🏆 Your clan captain «{clan_data.get('name', '')}» started a tournament with a {amount}⭐ stake! Open the Clan tab to chip in — your clan has 6 hours."))
                            except Exception:
                                pass
                    else:
                        if ADMIN_ID:
                            try:
                                await bot.send_message(ADMIN_ID, f"⚠️ Оплата турнира прошла, но запись не создалась (captain:{captain_id}, clan:{clan_id}). Нужен ручной возврат {amount}⭐.")
                            except Exception:
                                pass
            except Exception as e:
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при создании турнира (captain:{captain_id}, clan:{clan_id}): {e}")
                    except Exception:
                        pass
                await message.answer(t(message.from_user,
                    "❌ Что-то пошло не так при создании турнира — напиши администратору, разберёмся.",
                    "❌ Something went wrong creating the tournament — message the admin, we'll sort it out."))

    elif payload.startswith('ctp:'):
        # Участник клана оплатил свой взнос — либо стороной-инициатором (funding ->
        # participantsA), либо принимающей стороной (matching -> participantsB). Сторона
        # закодирована в payload на момент выставления счёта, но статус/принадлежность
        # перепроверяются заново прямо перед записью — на случай, если окно уже закрылось
        # или клан успел покинуть эту роль, пока счёт был открыт.
        parts = payload.split(':')
        payer_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        tournament_id = parts[2] if len(parts) > 2 else None
        side = parts[3] if len(parts) > 3 else 'A'
        if tournament_id:
            import aiohttp, time
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{payer_id}"
            try:
                ok = False
                already = False
                final_tdata = None
                field = 'participantsA' if side == 'A' else 'participantsB'
                expected_status = 'funding' if side == 'A' else 'matching'
                async with aiohttp.ClientSession() as session:
                    for attempt in range(6):
                        async with session.get(f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as resp:
                            etag = resp.headers.get("ETag")
                            tdata = await resp.json()
                        if not isinstance(tdata, dict) or tdata.get('status') != expected_status:
                            break
                        if pid in (tdata.get(field) or {}):
                            ok = True  # уже засчитан (повторная доставка вебхука) — не задваиваем
                            already = True
                            final_tdata = tdata
                            break
                        amount_paid = tdata.get('amountPerPerson', 0)
                        participants = dict(tdata.get(field) or {})
                        player_name = message.from_user.username or message.from_user.first_name or f"Игрок {payer_id}"
                        participants[pid] = {'userId': int(payer_id), 'name': player_name, 'username': message.from_user.username or '', 'paidAt': int(time.time() * 1000), 'amount': amount_paid}
                        tdata[field] = participants
                        headers = {"If-Match": etag} if etag else {}
                        async with session.put(f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}", json=tdata, headers=headers) as put_resp:
                            if put_resp.status == 412:
                                continue
                            ok = put_resp.status in (200, 204)
                            final_tdata = tdata
                            break
                    if ok and side == 'B' and not already and final_tdata is not None:
                        required = final_tdata.get('requiredCount', 0)
                        if required and len(final_tdata.get('participantsB') or {}) >= required:
                            try:
                                await _start_tournament_race(session, base, tournament_id)
                            except Exception as e:
                                print(f"Ошибка запуска гонки турнира {tournament_id}: {e}")
                if ok:
                    await message.answer(t(message.from_user,
                        "✅ Взнос в турнир клана засчитан!",
                        "✅ Your clan tournament stake is in!"))
                else:
                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"⚠️ Взнос в турнир не применился (payer:{payer_id}, tournament:{tournament_id}, side:{side}) — нужен ручной возврат.")
                        except Exception:
                            pass
                    await message.answer(t(message.from_user,
                        "❌ Не удалось засчитать взнос (окно сбора могло уже закрыться). Напиши администратору — вернём звёзды.",
                        "❌ Couldn't register your stake (the window may have just closed). Message the admin — we'll refund you."))
            except Exception as e:
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при взносе в турнир (payer:{payer_id}, tournament:{tournament_id}): {e}")
                    except Exception:
                        pass
                await message.answer(t(message.from_user,
                    "❌ Что-то пошло не так со взносом — напиши администратору, разберёмся.",
                    "❌ Something went wrong with your stake — message the admin, we'll sort it out."))

    elif payload.startswith('cta:'):
        # Капитан принял чужой турнир и оплатил свой взнос как принимающая сторона —
        # переводим турнир open -> matching, даём 2 часа на сбор такого же состава.
        parts = payload.split(':')
        captain_id = parts[1] if len(parts) > 1 else str(message.from_user.id)
        tournament_id = parts[2] if len(parts) > 2 else None
        clan_id = parts[3] if len(parts) > 3 else None
        if tournament_id and clan_id:
            import aiohttp, time
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            pid = f"tg_{captain_id}"
            try:
                ok = False
                error_reason = None
                clan_data = None
                final_tdata = None
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/clans/{clan_id}.json{FB_AUTH}") as cresp:
                        clan_data = await cresp.json()
                    if not isinstance(clan_data, dict) or clan_data.get('captainId') != int(captain_id):
                        error_reason = 'clan changed'
                    elif await _clan_has_active_match(session, base, clan_id, exclude_id=tournament_id):
                        error_reason = 'own clan busy'
                    else:
                        for attempt in range(6):
                            async with session.get(f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}", headers={"X-Firebase-ETag": "true"}) as tresp:
                                etag = tresp.headers.get("ETag")
                                tdata = await tresp.json()
                            if not isinstance(tdata, dict) or tdata.get('status') != 'open':
                                error_reason = 'tournament no longer open'
                                break
                            if tdata.get('initiatorClanId') == clan_id:
                                error_reason = 'own tournament'
                                break
                            if await _clan_has_active_match(session, base, tdata.get('initiatorClanId'), exclude_id=tournament_id):
                                error_reason = 'initiator busy'
                                break
                            now_ms = int(time.time() * 1000)
                            player_name = message.from_user.username or message.from_user.first_name or f"Игрок {captain_id}"
                            tdata['status'] = 'matching'
                            tdata['acceptedByClanId'] = clan_id
                            tdata['acceptedByClanName'] = clan_data.get('name', '')
                            tdata['acceptingCaptainId'] = int(captain_id)
                            tdata['participantsB'] = {
                                pid: {'userId': int(captain_id), 'name': player_name, 'username': message.from_user.username or '', 'paidAt': now_ms, 'amount': tdata.get('amountPerPerson', 0)}
                            }
                            tdata['matchingDeadline'] = now_ms + 2 * 3600 * 1000
                            headers = {"If-Match": etag} if etag else {}
                            async with session.put(f"{base}/clan_tournaments/{tournament_id}.json{FB_AUTH}", json=tdata, headers=headers) as put_resp:
                                if put_resp.status == 412:
                                    continue
                                ok = put_resp.status in (200, 204)
                                final_tdata = tdata
                                break
                    if ok and final_tdata is not None:
                        # Уведомляем остальных участников своего клана — приглашаем тоже
                        # внести взнос в оставшееся 2-часовое окно.
                        for m_pid, m in (clan_data.get('members') or {}).items():
                            m_uid = m.get('userId') if isinstance(m, dict) else None
                            if not m_uid or m_uid == int(captain_id):
                                continue
                            try:
                                await bot.send_message(m_uid,
                                    f"⚔️ Ваш клан «{clan_data.get('name','')}» принял вызов на турнир (ставка {final_tdata.get('amountPerPerson', 0)}⭐)! Зайди во вкладку «Клан», чтобы внести взнос — на сбор всего 2 часа.")
                            except Exception:
                                pass
                        # И клан-инициатора — что их турнир приняли.
                        async with session.get(f"{base}/clans/{final_tdata.get('initiatorClanId')}.json{FB_AUTH}") as iresp:
                            init_clan = await iresp.json()
                        if isinstance(init_clan, dict):
                            for m_pid, m in (init_clan.get('members') or {}).items():
                                m_uid = m.get('userId') if isinstance(m, dict) else None
                                if not m_uid:
                                    continue
                                try:
                                    await bot.send_message(m_uid,
                                        f"⚔️ Ваш турнир #{final_tdata.get('number')} принял клан «{clan_data.get('name','')}»! Идёт сбор их состава — 2 часа.")
                                except Exception:
                                    pass
                if ok and final_tdata is not None:
                    await message.answer(t(message.from_user,
                        f"✅ Турнир принят! Взнос {final_tdata.get('amountPerPerson', 0)}⭐ засчитан. У вашего клана есть 2 часа, чтобы собрать такой же состав.",
                        f"✅ Tournament accepted! Your {final_tdata.get('amountPerPerson', 0)}⭐ stake is in. Your clan has 2 hours to match the squad."))
                else:
                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"⚠️ Оплата принятия турнира не применилась ({error_reason}) — captain:{captain_id}, tournament:{tournament_id}, clan:{clan_id}. Нужен ручной возврат.")
                        except Exception:
                            pass
                    await message.answer(t(message.from_user,
                        "❌ Не удалось принять турнир (он мог уже стать недоступен). Напиши администратору — вернём звёзды.",
                        "❌ Couldn't accept the tournament (it may no longer be available). Message the admin — we'll refund you."))
            except Exception as e:
                if ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка при принятии турнира (captain:{captain_id}, tournament:{tournament_id}): {e}")
                    except Exception:
                        pass
                await message.answer(t(message.from_user,
                    "❌ Что-то пошло не так при принятии турнира — напиши администратору, разберёмся.",
                    "❌ Something went wrong accepting the tournament — message the admin, we'll sort it out."))

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
                await bot.send_message(
                    ADMIN_ID,
                    f"💰 Новый обмен!\n👤 {ul}\n🪙 Монет: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n⭐ Отправь токены!"
                )
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


async def price_regeneration_loop():
    """
    Фоновая задача — обновляет глобальные цены рынка раз в 30 секунд НЕЗАВИСИМО от того,
    продаёт ли кто-то прямо сейчас. Раньше цены пересчитывались только "по требованию"
    (лениво, в момент чьей-то продажи) — если торговли мало, цены и таймер на экране
    у игроков зависали на старом значении.
    """
    while True:
        try:
            await get_market_prices()
        except Exception as e:
            print(f"Ошибка фонового обновления цен: {e}")
        await asyncio.sleep(PRICE_INTERVAL_MS / 1000)


async def clan_tournament_loop():
    """
    Фоновая задача — раз в минуту проверяет турниры clan_tournaments на истёкшие окна.
    funding -> open по истечении 6-часового окна сбора (вручную капитан может
    зафиксировать раньше через /clan_tournament_fix). matching -> running как idempotent
    safety-net (на случай если синхронный запуск из successful_payment по какой-то причине
    не сработал), и matching -> open откат, если принимающая сторона не успела собрать
    состав за 2 часа. open -> expired, если за 7 дней публикации никто не принял вызов.
    running -> settled по истечении 48-часового окна гонки — считает победителя и рассылает
    итог + список на ручную выплату.
    """
    while True:
        try:
            import aiohttp
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            now_ms = int(time_module.time() * 1000)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/clan_tournaments.json{FB_AUTH}") as resp:
                    all_t = await resp.json()
                all_t = all_t or {}
                for tid, t in all_t.items():
                    if not isinstance(t, dict):
                        continue
                    if t.get('status') == 'funding' and now_ms >= t.get('fundingDeadline', 0):
                        try:
                            await _fixate_tournament(session, base, tid)
                        except Exception as e:
                            print(f"Ошибка автофиксации турнира {tid}: {e}")
                    elif t.get('status') == 'open' and now_ms >= t.get('listExpiresAt', 0):
                        try:
                            await _expire_open_tournament(session, base, tid)
                        except Exception as e:
                            print(f"Ошибка истечения турнира {tid}: {e}")
                    elif t.get('status') == 'matching':
                        required = t.get('requiredCount', 0)
                        participants_b = t.get('participantsB') or {}
                        if required and len(participants_b) >= required:
                            try:
                                await _start_tournament_race(session, base, tid)
                            except Exception as e:
                                print(f"Ошибка safety-net запуска турнира {tid}: {e}")
                        elif now_ms >= t.get('matchingDeadline', 0):
                            try:
                                await _expire_matching_window(session, base, tid)
                            except Exception as e:
                                print(f"Ошибка отката окна ожидания турнира {tid}: {e}")
                    elif t.get('status') == 'running' and now_ms >= t.get('matchEndsAt', 0):
                        try:
                            await _settle_tournament(session, base, tid)
                        except Exception as e:
                            print(f"Ошибка подведения итогов турнира {tid}: {e}")
        except Exception as e:
            print(f"Ошибка фоновой проверки турниров: {e}")
        await asyncio.sleep(60)


async def main():
    app = web.Application()
    app.router.add_post('/invoice', create_invoice)
    app.router.add_options('/invoice', create_invoice)
    app.router.add_post('/referral_notify', referral_notify)
    app.router.add_options('/referral_notify', referral_notify)
    app.router.add_post('/referral_market_list', referral_market_list)
    app.router.add_options('/referral_market_list', referral_market_list)
    app.router.add_post('/social_tasks_list', social_tasks_list)
    app.router.add_options('/social_tasks_list', social_tasks_list)
    app.router.add_post('/claim_social_task', claim_social_task)
    app.router.add_options('/claim_social_task', claim_social_task)
    app.router.add_post('/jackpot_broadcast', jackpot_broadcast)
    app.router.add_options('/jackpot_broadcast', jackpot_broadcast)
    app.router.add_post('/lottery_spin', lottery_spin)
    app.router.add_options('/lottery_spin', lottery_spin)
    app.router.add_post('/refill_energy_ad', refill_energy_ad)
    app.router.add_options('/refill_energy_ad', refill_energy_ad)
    app.router.add_post('/sync', sync_state)
    app.router.add_options('/sync', sync_state)
    app.router.add_post('/actions', process_actions)
    app.router.add_options('/actions', process_actions)
    app.router.add_post('/reset_progress', reset_progress)
    app.router.add_options('/reset_progress', reset_progress)
    app.router.add_post('/clan_status', clan_status)
    app.router.add_options('/clan_status', clan_status)
    app.router.add_post('/clan_create', clan_create)
    app.router.add_options('/clan_create', clan_create)
    app.router.add_post('/clan_referrals', clan_referrals)
    app.router.add_options('/clan_referrals', clan_referrals)
    app.router.add_post('/clan_invite', clan_invite)
    app.router.add_options('/clan_invite', clan_invite)
    app.router.add_post('/clan_invite_respond', clan_invite_respond)
    app.router.add_options('/clan_invite_respond', clan_invite_respond)
    app.router.add_post('/clan_kick', clan_kick)
    app.router.add_options('/clan_kick', clan_kick)
    app.router.add_post('/clan_leave', clan_leave)
    app.router.add_options('/clan_leave', clan_leave)
    app.router.add_post('/clan_disband', clan_disband)
    app.router.add_options('/clan_disband', clan_disband)
    app.router.add_post('/clan_tournament_fix', clan_tournament_fix)
    app.router.add_options('/clan_tournament_fix', clan_tournament_fix)
    app.router.add_post('/clan_tournaments_mine', clan_tournaments_mine)
    app.router.add_options('/clan_tournaments_mine', clan_tournaments_mine)
    app.router.add_post('/clan_tournaments_open', clan_tournaments_open)
    app.router.add_options('/clan_tournaments_open', clan_tournaments_open)
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
        asyncio.create_task(price_regeneration_loop())
        asyncio.create_task(clan_tournament_loop())
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
        asyncio.create_task(price_regeneration_loop())
        asyncio.create_task(clan_tournament_loop())
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
