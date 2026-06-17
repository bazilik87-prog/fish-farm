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
    'doubleTap':  'Double Catch x2 for 1 hour',
    'turboDlv':   'Turbo Delivery instant',
    'luckyRod':   'Lucky Rod +50pct for 30min',
    'autoRepair': 'Auto Repair vehicle',
}
BOOST_LABELS = {
    'doubleTap':  '⚡ Двойной улов',
    'turboDlv':   '🚀 Турбо доставка',
    'luckyRod':   '🎣 Удачная рыбалка',
    'autoRepair': '🔧 Авторемонт',
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
        "Лови рыбу, улучшай снаряжение, открывай локации.\n\n"
        "💡 /bank СУММА КОШЕЛЁК — обмен монет\n"
        "⚡ /boost — купить бустер\n\n"
        "Нажми кнопку чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.message(Command('bank'))
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
        label    = BOOST_LABELS.get(boost_id, 'Бустер')
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
