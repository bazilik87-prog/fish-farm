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
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # публичный URL сервиса в Railway, для webhook
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = secrets.token_hex(16)  # генерируется заново при каждом старте — это ок, т.к. используется вместе с set_webhook в этом же запуске
FIREBASE_DB_SECRET = os.getenv("FIREBASE_DB_SECRET", "")
# Добавляется ко всем запросам бота к Firebase — даёт админский доступ в обход правил безопасности,
# которые теперь можно спокойно ужесточать для обычных клиентов (игры в браузере), не боясь сломать бота.
FB_AUTH = ("?auth=" + FIREBASE_DB_SECRET) if FIREBASE_DB_SECRET else ""

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
}
PREMIUM_PRICE = 50  # ⭐/месяц
PREMIUM_BOOST_DISCOUNT = 0.2  # -20% на бустеры для подписчиков

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
            if coins < 1 or not wallet:
                return web.json_response({'error': 'invalid'}, status=400, headers=CORS)
            # Зашиваем данные прямо в payload — Telegram вернёт их при оплате,
            # так что рестарт бота между созданием счёта и оплатой ничего не потеряет.
            payload = f"ex:{user_id}:{coins}:{wallet}:{username}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'payload too long (кошелёк/имя слишком длинные)'}, status=400, headers=CORS)
            fee = 1 if await is_premium(user_id) else 3
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
            price = BOOST_PRICES.get(boost_id, 1)
            if await is_premium(user_id):
                price = max(1, round(price * (1 - PREMIUM_BOOST_DISCOUNT)))
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
                    description="Премиум-подписка на 30 дней: +25% к автодоходу, скидка 20% на бустеры, ⭐1 комиссия банка, защита стрика, бесплатная крутка лотереи в день, корона в лидерборде",
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
                    description="Премиум на 30 дней без автопродления: +25% к автодоходу, скидка 20% на бустеры, ⭐1 комиссия банка, защита стрика, бесплатная крутка лотереи в день, корона в лидерборде",
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

    text = (
        f"🎰⭐ ДЖЕКПОТ ВЫИГРАН!\n\n"
        f"@{username} сорвал(а) джекпот и забрал(а) {amount:,}⭐ Stars в лотерее FishFarm! 🎉\n\n"
        f"Крути колесо и попробуй свою удачу!"
    )

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
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
        [InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text="💬 Чат игроков", url="https://t.me/+cLBHDCmOkaA3NWQy")]
    ])
    await message.answer(
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
                    await session.put(f"{base}/known_starts/{user_id}.json{FB_AUTH}", json=True)
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
        "/referrals — реферальная система (файл .txt)\n"
        "/refcontest — рейтинг реферального конкурса\n"
        "/startrefconcurs — начать конкурс на 14 дней (сбрасывает счёт, рассылает анонс всем)\n"
        "/stoprefconcurs — остановить конкурс досрочно\n"
        "/addcoins @username СУММА — начислить монеты игроку\n"
        "/premium @username [дни] — проверить/выдать/отозвать Premium\n"
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
        "/start — запустить игру\n"
        "/boost — купить бустер за ⭐\n\n"
        "💬 Чат игроков: https://t.me/+cLBHDCmOkaA3NWQy",
        parse_mode="Markdown"
    )


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


@dp.message(Command('boost'))
async def boost_command(message: types.Message):
    buttons = [[InlineKeyboardButton(text=label, callback_data=f"boost:{bid}")]
               for bid, label in BOOST_LABELS.items()]
    buttons.append([InlineKeyboardButton(text="🎣 В игру", web_app=WebAppInfo(url=GAME_URL))])
    await message.answer(
        "⚡ *Бустеры за ⭐ Stars*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith('boost:'))
async def boost_callback(callback: types.CallbackQuery):
    boost_id = callback.data.split(':')[1]
    name  = BOOST_NAMES.get(boost_id, 'Boost')
    label = BOOST_LABELS.get(boost_id, name)
    price = BOOST_PRICES.get(boost_id, 1)
    user_id = callback.from_user.id
    link = await bot.create_invoice_link(
        title=name, description=name,
        payload=f"bo:{boost_id}:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Boost", amount=price)],
        provider_token="",
    )
    stars_word = 'звезду' if price == 1 else ('звезды' if price in (2,3,4) else 'звёзд')
    await callback.message.answer(
        f"⚡ *{label}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"⭐ Купить за {price} {stars_word}", url=link)
        ]])
    )
    await callback.answer()


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
                "🎉 Premium активирован на 30 дней!\n\n"
                "✅ +25% к автодоходу\n"
                "✅ -20% на все бустеры\n"
                "✅ Комиссия банка снижена до ⭐1\n"
                "✅ Защита стрика ежедневного бонуса\n"
                "✅ Бесплатная крутка лотереи раз в день\n"
                "✅ Корона рядом с именем в лидерборде\n\n"
                "Подписка продлевается автоматически каждые 30 дней."
            )
        except Exception:
            pass

    elif payload.startswith('ex:'):
        parts = payload.split(':', 4)
        # ex:{user_id}:{coins}:{wallet}:{username}
        if len(parts) < 4:
            await message.answer("✅ Оплата получена! Свяжись с администратором для получения GRAM.")
            return
        user_id  = parts[1]
        coins    = parts[2]
        wallet   = parts[3]
        username = parts[4] if len(parts) > 4 else ''
        try:
            gram_amount = round(int(coins) / 100000, 5)
        except ValueError:
            gram_amount = 0

        # Публичная лента выводов — для баннера "История выплат" в игре
        try:
            import aiohttp, time as time_mod
            base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
            entry = {"amount": int(coins), "wallet": wallet, "ts": int(time_mod.time() * 1000)}
            async with aiohttp.ClientSession() as session:
                await session.post(f"{base}/withdrawals_log.json{FB_AUTH}", json=entry)
        except Exception:
            pass

        await message.answer(
            f"✅ Заявка принята!\n\n"
            f"🪙 Монет: {coins}\n💎 GRAM: {gram_amount}\n👛 {wallet}\n\n"
            f"⏳ Отправим в течение 24 часов."
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
    app.router.add_get('/health', health)

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
