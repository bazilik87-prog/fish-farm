import asyncio
import os
import json
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
    'doubleTap':   'Auto Boost +2 auto income per minute for 1 hour',
    'turboDry':    'Instant Dry - all drying packets finish instantly',
    'luckyRod':    'Lucky Rod +50pct for 30min',
    'turboSpeed':  'Turbo Speed x2 transport speed for 1 hour',
    'turboPack':   'Instant Packing - all packing finishes instantly',
}
BOOST_LABELS = {
    'doubleTap':  '⚡ Авто-буст (+2 автодобычи/мин на 1 час)',
    'turboDry':   '🌡 Мгновенная сушка',
    'luckyRod':   '🎣 Удачная рыбалка',
    'turboSpeed': '🏎 Турбо скорость',
    'turboPack':  '📦 Мгновенная упаковка',
}

# Хранилище pending обменов: user_id -> {coins, wallet}
pending = {}


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
            if coins < 1 or not wallet:
                return web.json_response({'error': 'invalid'}, status=400, headers=CORS)
            # Сохраняем данные обмена
            pending[str(user_id)] = {'coins': coins, 'wallet': wallet,
                                     'username': str(data.get('username', ''))}
            # Короткий payload: тип:user_id
            payload = f"ex:{user_id}"
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
    # Уведомляем админа о новом игроке
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        user = message.from_user
        name = f"@{user.username}" if user.username else f"{user.first_name or 'Без имени'}"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 *Новый игрок!*\n👤 {name}\n🆔 `{user.id}`",
                parse_mode="Markdown"
            )
        except Exception:
            pass


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
    # Ищем user_id в Firebase
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


@dp.message(Command('players'))
async def players_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⏳ Загружаю список игроков...")
    import aiohttp
    from io import BytesIO
    from datetime import datetime
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
            lines.append(f"{i}. {loc} {identity} | coins:{coins:,} | caught:{caught}")
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        header = f"FishFarm — Список игроков\nДата: {now}\nВсего: {len(players)}\n{'='*40}\n\n"
        content = header + "\n".join(lines)
        filename = f"fishfarm_players_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.txt"
        file_bytes = content.encode('utf-8')
        await message.answer_document(
            types.BufferedInputFile(file_bytes, filename=filename),
            caption=f"👥 Игроков: {len(players)}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def bank_command(message: types.Message):
    text = message.text.strip().split()
    if len(text) < 3:
        await message.answer(
            "💱 Использование:\n`/bank СУММА КОШЕЛЁК`\n\nПример:\n`/bank 100 UQD...`",
            parse_mode="Markdown"
        )
        return
    try:
        coins  = int(text[1])
        wallet = text[2]
    except Exception:
        await message.answer("❌ Пример: `/bank 100 UQD...`", parse_mode="Markdown")
        return

    user_id = message.from_user.id
    pending[str(user_id)] = {'coins': coins, 'wallet': wallet,
                             'username': message.from_user.username or ''}
    link = await bot.create_invoice_link(
        title="TON Fish Exchange",
        description=f"{coins} coins to TON Fish",
        payload=f"ex:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Fee", amount=1)],
        provider_token="",
    )
    await message.answer(
        f"💱 *Обмен {coins} монет → {coins} TON Fish*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⭐ Оплатить 1 звезду", url=link)
        ]])
    )


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
    try:
        url = "https://fishfarm-3a4f8-default-rtdb.firebaseio.com/leaderboard.json"
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
            await asyncio.sleep(0.05)  # защита от flood
        await message.answer(
            f"✅ *Рассылка завершена*\n\n"
            f"📨 Отправлено: {success}\n"
            f"❌ Не доставлено: {failed}\n"
            f"👥 Всего: {total}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")



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
        # Записываем буст в Firebase чтобы игра подхватила при входе
        import aiohttp
        try:
            pid = f"tg_{user_id}"
            url = f"https://fishfarm-3a4f8-default-rtdb.firebaseio.com/pending_boosts/{pid}/{boost_id}.json"
            import time
            async with aiohttp.ClientSession() as session:
                await session.put(url, json=int(time.time() * 1000))
        except Exception:
            pass
        await message.answer(
            f"✅ *{label} активирован!*\n\nВернись в игру 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )

    elif payload.startswith('ex:'):
        user_id = payload.split(':')[1]
        info    = pending.pop(user_id, None)
        if not info:
            await message.answer("✅ Оплата получена! Свяжись с администратором для получения TON Fish.")
            return
        coins    = info['coins']
        wallet   = info['wallet']
        username = info['username']
        await message.answer(
            f"✅ *Заявка принята!*\n\n"
            f"🪙 Монет: `{coins}`\n🐟 TON Fish: `{coins}`\n👛 `{wallet}`\n\n"
            f"⏳ Отправим в течение 24 часов.",
            parse_mode="Markdown"
        )
        if ADMIN_ID:
            ul = f"@{username}" if username else f"ID: {user_id}"
            await bot.send_message(
                ADMIN_ID,
                f"💰 *Новый обмен!*\n👤 {ul}\n🪙 `{coins}`\n👛 `{wallet}`\n\n⭐ Отправь токены!",
                parse_mode="Markdown"
            )


@dp.message()
async def any_message(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer("Нажми кнопку чтобы играть 👇", reply_markup=keyboard)


async def main():
    app = web.Application()
    app.router.add_post('/invoice', create_invoice)
    app.router.add_options('/invoice', create_invoice)
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    print(f"API запущен на порту {PORT}")
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
