import asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
GAME_URL  = os.getenv("GAME_URL",  "https://ВАШ_НИК.github.io/fish-farm/")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
PORT      = int(os.getenv("PORT", "8080"))

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
}

BOOST_NAMES = {
    'doubleTap':       'Auto Boost +2 auto income per minute for 1 hour',
    'turboDry':        'Instant Dry - all drying packets finish instantly',
    'luckyRod':        'Lucky Rod +50pct for 30min',
    'turboSpeed':      'Turbo Speed x2 transport speed for 1 hour',
    'turboPack':       'Instant Packing - all packing finishes instantly',
    'instantDelivery': 'Instant Delivery - all active deliveries finish instantly',
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
    'lottery':         '🎰 Лотерея',
    'weather_sunny':   '☀️ Погода: Ясно',
    'weather_cloudy':  '🌥 Погода: Облачно',
    'weather_rain':    '🌧 Погода: Дождь',
    'weather_storm':   '⛈ Погода: Шторм',
    'weather_perfect': '🌟 Погода: Отличный клёв',
}

SUPPORT_GROUP_ID = -5478312122




async def create_invoice(request):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=CORS)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    action = data.get('action')

    try:
        if action == 'exchange':
            coins   = int(data.get('coins', 0))
            wallet  = str(data.get('wallet', '')).strip()
            user_id = int(data.get('user_id', 0))
            username = str(data.get('username', '')).strip()
            if coins < 1 or not wallet:
                return web.json_response({'error': 'invalid'}, status=400, headers=CORS)
            # Зашиваем данные прямо в payload — Telegram вернёт их при оплате,
            # так что рестарт бота между созданием счёта и оплатой ничего не потеряет.
            payload = f"ex:{user_id}:{coins}:{wallet}:{username}"
            if len(payload.encode('utf-8')) > 128:
                return web.json_response({'error': 'payload too long (кошелёк/имя слишком длинные)'}, status=400, headers=CORS)
            link = await bot.create_invoice_link(
                title="TON Fish Exchange",
                description=f"{coins} coins to TON Fish",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Fee", amount=1)],
                provider_token="",
            )
            return web.json_response({'link': link}, headers=CORS)

        elif action == 'boost':
            boost_id = str(data.get('boost', ''))
            user_id  = int(data.get('user_id', 0))
            name = BOOST_NAMES.get(boost_id, 'Boost')
            payload = f"bo:{boost_id}:{user_id}"
            link = await bot.create_invoice_link(
                title=name,
                description=name,
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label="Boost", amount=1)],
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

    referrer_id = data.get('referrer_id')
    notify_type = data.get('type')

    if not referrer_id:
        return web.json_response({'error': 'no referrer_id'}, status=400, headers=CORS)

    try:
        if notify_type == 'rod2':
            await bot.send_message(
                int(referrer_id),
                "🎣 *Твой реферал купил удочку 2-го уровня!*\n\n"
                "🪙 +1000 монет уже ждут тебя в игре!",
                parse_mode="Markdown",
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

    username = data.get('username', 'Игрок')
    amount = data.get('amount', 0)

    def esc_md(s):
        if not s:
            return s
        for ch in ('_', '*', '`', '['):
            s = s.replace(ch, '\\' + ch)
        return s

    text = (
        f"🎰⭐ *ДЖЕКПОТ ВЫИГРАН!*\n\n"
        f"@{esc_md(username)} сорвал(а) джекпот и забрал(а) {amount:,}⭐ Stars в лотерее FishFarm! 🎉\n\n"
        f"Крути колесо и попробуй свою удачу!"
    )

    import aiohttp
    base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/leaderboard.json") as resp:
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
            await bot.send_message(user_id, text, parse_mode="Markdown", reply_markup=keyboard)
            sent += 1
        except Exception:
            try:
                await bot.send_message(user_id, text.replace('*', ''), reply_markup=keyboard)
                sent += 1
            except Exception:
                pass
        await asyncio.sleep(0.05)

    return web.json_response({'ok': True, 'sent': sent}, headers=CORS)


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer(
        "🐟 *Добро пожаловать на Рыбную ферму!*\n\n"
        "Лови рыбу, улучшай снаряжение, открывай локации.\n"
        "💡 Банк — обмен монет на TON Fish за ⭐️\n"
        "⚡️ Бустеры — усиления за ⭐️\n"
        "🛟 Поддержка — @elbanderass\n\n"
        "Нажми кнопку чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    user = message.from_user
    user_id = str(user.id)

    # Уведомляем админа о новом игроке
    if ADMIN_ID and user.id != ADMIN_ID:
        def esc_md(s):
            if not s:
                return s
            for ch in ('_', '*', '`', '['):
                s = s.replace(ch, '\\' + ch)
            return s
        name = f"@{esc_md(user.username)}" if user.username else esc_md(user.first_name or 'Без имени')
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 *Новый игрок!*\n👤 {name}\n🆔 `{user.id}`",
                parse_mode="Markdown"
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
            async with session.get(f"{base}/referrals/used/{user_id}.json") as resp:
                already_used = await resp.json()
            if already_used:
                return  # реферал уже был использован

            # Сохраняем связь реферал → реферер
            await session.put(f"{base}/referrals/used/{user_id}.json",
                              json=referrer_id)
            await session.put(f"{base}/referrals/by/{referrer_id}/{user_id}.json",
                              json=True)

            # Начисляем +100 монет новому игроку
            await session.put(f"{base}/pending_rewards/tg_{user_id}/ref_bonus.json",
                              json=100)

            # Начисляем +100 монет рефереру
            await session.put(f"{base}/pending_rewards/tg_{referrer_id}/ref_invite_{user_id}.json",
                              json=100)

        # Уведомляем реферера
        def esc_md2(s):
            if not s:
                return s
            for ch in ('_', '*', '`', '['):
                s = s.replace(ch, '\\' + ch)
            return s
        ref_name = f"@{esc_md2(user.username)}" if user.username else esc_md2(user.first_name or 'Новый игрок')
        try:
            await bot.send_message(
                int(referrer_id),
                f"🎉 *По твоей ссылке пришёл {ref_name}!*\n\n"
                f"🪙 +100 монет уже ждут тебя в игре!",
                parse_mode="Markdown",
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
            async with session.get(f"{base}/leaderboard.json") as resp:
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
                f"{base}/pending_rewards/tg_{user_id}/admin_compensation.json",
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
            async with session.get(f"{base}/referrals/by.json") as resp:
                by_data = await resp.json()
            # Получаем leaderboard для имён
            async with session.get(f"{base}/leaderboard.json") as resp:
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
    import aiohttp
    try:
        base = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/ref_contest/scores.json") as resp:
                scores = await resp.json()
            async with session.get(f"{base}/leaderboard.json") as resp:
                lb = await resp.json()

        if not scores:
            await message.answer("Пока нет результатов.")
            return

        # Строим словарь userId -> username
        def esc_md(s):
            if not s:
                return s
            for ch in ('_', '*', '`', '['):
                s = s.replace(ch, '\\' + ch)
            return s

        id_to_name = {}
        if lb:
            for v in lb.values():
                uid = str(v.get('userId', ''))
                username = v.get('username', '')
                first_name = v.get('firstName', '')
                if username:
                    id_to_name[uid] = f"@{esc_md(username)}"
                elif first_name:
                    id_to_name[uid] = esc_md(first_name)
                else:
                    id_to_name[uid] = f"ID:{uid}"

        results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        lines = []
        medals = ['🥇', '🥈', '🥉']
        for i, (uid, count) in enumerate(results):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = id_to_name.get(uid, f"ID:{uid}")
            lines.append(f"{medal} {name} — {count} активных рефералов")

        text = "🏆 *Реферальный конкурс — текущий рейтинг:*\n\n" + "\n".join(lines)
        try:
            await message.answer(text, parse_mode="Markdown")
        except Exception:
            await message.answer(text.replace('*', ''))
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
            async with session.get(f"{base}/leaderboard.json") as resp:
                players = await resp.json()

            baseline = {}
            if players:
                for pid, v in players.items():
                    baseline[pid] = v.get('totalEarned', 0)

            await session.put(f"{base}/tournament.json", json={
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
            "🥇 1 место — 100⭐\n"
            "🥈 2 место — 60⭐\n"
            "🥉 3 место — 40⭐\n\n"
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
            await session.patch(f"{base}/tournament.json", json={"active": False})
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
            async with session.get(f"{base}/tournament.json") as resp:
                t = await resp.json()
            async with session.get(f"{base}/leaderboard.json") as resp:
                players = await resp.json()

        if not t or not players:
            await message.answer("Турнир ещё не запускался или нет игроков.")
            return

        baseline = t.get('baseline') or {}

        def esc_md(s):
            if not s:
                return s
            for ch in ('_', '*', '`', '['):
                s = s.replace(ch, '\\' + ch)
            return s

        results = []
        for pid, v in players.items():
            earned_now = v.get('totalEarned', 0)
            start_earned = baseline.get(pid, 0)
            delta = earned_now - start_earned
            if delta <= 0:
                continue
            username = v.get('username')
            first_name = v.get('firstName')
            if username:
                display = f"@{esc_md(username)}"
            elif first_name:
                display = esc_md(first_name)
            else:
                display = f"ID:{v.get('userId', '?')}"
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
        text = f"🏆 *Турнир недели — {status}*\n\n" + "\n".join(lines)
        try:
            await message.answer(text, parse_mode="Markdown")
        except Exception:
            await message.answer(text.replace('*', ''))
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
            await session.put(f"{base}/promo/net_bonus.json", json={"endsAt": ends_at, "bonus": 500})
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
            await session.delete(f"{base}/promo/net_bonus.json")
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
        "/addcoins @username СУММА — начислить монеты игроку\n"
        "/pay @username СУММА — уведомить игрока о выплате TON Fish\n"
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
        "/boost — купить бустер за ⭐",
        parse_mode="Markdown"
    )


@dp.message(Command('pay'))
async def pay_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "Использование:\n`/pay @username СУММА`\n\nПример:\n`/pay @Metelegram12 127`",
            parse_mode="Markdown"
        )
        return
    username = text[1].lstrip('@').lower()
    amount = text[2]
    import aiohttp
    try:
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json"
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
            f"🐟 {amount} TON Fish отправлены на твой кошелёк.\n\n"
            f"Спасибо что играешь в FishFarm! 🎣",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
        await message.answer(f"✅ Уведомление отправлено @{username} (ID: {user_id}) о выплате {amount} TON Fish")
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
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json"
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
        url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json"
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
            await session.put(f"{base}/news/{news_key}.json", json=news_entry)

            # Оставляем только последние 5 новостей
            async with session.get(f"{base}/news.json?orderBy=\"ts\"") as resp:
                all_news = await resp.json()
            if all_news and len(all_news) > 5:
                sorted_keys = sorted(all_news.keys())
                for old_key in sorted_keys[:-5]:
                    await session.delete(f"{base}/news/{old_key}.json")

        url = f"{base}/leaderboard.json"
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
            async with session.get(f"{base}/leaderboard.json") as resp:
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
    user_id = callback.from_user.id
    link = await bot.create_invoice_link(
        title=name, description=name,
        payload=f"bo:{boost_id}:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Boost", amount=1)],
        provider_token="",
    )
    await callback.message.answer(
        f"⚡ *{label}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⭐ Купить за 1 звезду", url=link)
        ]])
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload

    if payload.startswith('bo:'):
        parts    = payload.split(':')
        boost_id = parts[1] if len(parts) > 1 else ''
        user_id  = parts[2] if len(parts) > 2 else str(message.from_user.id)
        label    = BOOST_LABELS.get(boost_id, 'Бустер')
        import aiohttp
        try:
            pid = f"tg_{user_id}"
            url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/pending_boosts/{pid}/{boost_id}.json"
            import time
            async with aiohttp.ClientSession() as session:
                await session.put(url, json=int(time.time() * 1000))
        except Exception:
            pass

    elif payload.startswith('ex:'):
        parts = payload.split(':', 4)
        # ex:{user_id}:{coins}:{wallet}:{username}
        if len(parts) < 4:
            await message.answer("✅ Оплата получена! Свяжись с администратором для получения TON Fish.")
            return
        user_id  = parts[1]
        coins    = parts[2]
        wallet   = parts[3]
        username = parts[4] if len(parts) > 4 else ''

        def esc_md(s):
            if not s:
                return s
            for ch in ('_', '*', '`', '['):
                s = s.replace(ch, '\\' + ch)
            return s

        username_safe = esc_md(str(username)) if username else None

        await message.answer(
            f"✅ *Заявка принята!*\n\n"
            f"🪙 Монет: `{coins}`\n🐟 TON Fish: `{coins}`\n👛 `{wallet}`\n\n"
            f"⏳ Отправим в течение 24 часов.",
            parse_mode="Markdown"
        )
        if ADMIN_ID:
            ul = f"@{username_safe}" if username_safe else f"ID: {user_id}"
            try:
                sent = await bot.send_message(
                    ADMIN_ID,
                    f"💰 *Новый обмен!*\n👤 {ul}\n🪙 `{coins}`\n👛 `{wallet}`\n\n⭐ Отправь токены!",
                    parse_mode="Markdown"
                )
            except Exception:
                sent = await bot.send_message(
                    ADMIN_ID,
                    f"💰 Новый обмен!\n👤 {ul}\n🪙 {coins}\n👛 {wallet}\n\n⭐ Отправь токены!"
                )
            try:
                await bot.pin_chat_message(ADMIN_ID, sent.message_id, disable_notification=True)
            except Exception:
                pass
            try:
                await bot.send_message(
                    SUPPORT_GROUP_ID,
                    f"💰 *Новый запрос на вывод!*\n👤 {ul}\n🪙 `{coins}`\n👛 `{wallet}`\n\n⭐ Требует выплаты!",
                    parse_mode="Markdown"
                )
            except Exception:
                try:
                    await bot.send_message(
                        SUPPORT_GROUP_ID,
                        f"💰 Новый запрос на вывод!\n👤 {ul}\n🪙 {coins}\n👛 {wallet}\n\n⭐ Требует выплаты!"
                    )
                except Exception:
                    pass


@dp.message()
async def any_message(message: types.Message):
    if message.chat.type != 'private':
        return  # web_app-кнопки нельзя отправлять в группах — просто игнорируем
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
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    print(f"API запущен на порту {PORT}")
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.sleep(3)
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
