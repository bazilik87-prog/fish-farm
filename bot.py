import asyncio
import os
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
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

CORS = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Content-Type'}

BOOST_NAMES = {
    'doubleTap':  '⚡ Двойной улов (×2 монеты на 1 час)',
    'turboDlv':   '🚀 Турбо доставка (мгновенная доставка)',
    'luckyRod':   '🎣 Удачная рыбалка (+50% шанс на 30 мин)',
    'autoRepair': '🔧 Авторемонт (мгновенный ремонт транспорта)',
}


# ── HTTP API ──────────────────────────────────────────
async def create_invoice(request):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers={**CORS, 'Access-Control-Allow-Methods': 'POST, OPTIONS'})
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400, headers=CORS)

    action = data.get('action')

    if action == 'exchange':
        coins  = int(data.get('coins', 0))
        wallet = data.get('wallet', '').strip()
        if coins < 1 or not wallet:
            return web.json_response({'error': 'invalid'}, status=400, headers=CORS)
        link = await bot.create_invoice_link(
            title="TON Fish Exchange",
            description=f"{coins} coins to {coins} TON Fish",
            payload=json.dumps({"type": "exchange", "coins": coins, "wallet": wallet,
                                "user_id": data.get('user_id', 0), "username": data.get('username', '')}),
            currency="XTR",
            prices=[LabeledPrice(label="Exchange fee", amount=1)],
            provider_token="",
        )
        return web.json_response({'link': link}, headers=CORS)

    elif action == 'boost':
        boost_id = data.get('boost', '')
        name = BOOST_NAMES.get(boost_id, 'Бустер')
        link = await bot.create_invoice_link(
            title=name,
            description=name,
            payload=json.dumps({"type": "boost", "boost": boost_id, "user_id": data.get('user_id', 0)}),
            currency="XTR",
            prices=[LabeledPrice(label="⭐ Бустер", amount=1)],
            provider_token="",
        )
        return web.json_response({'link': link}, headers=CORS)

    return web.json_response({'error': 'unknown'}, status=400, headers=CORS)


async def health(request):
    return web.json_response({'ok': True}, headers=CORS)


# ── Telegram бот ──────────────────────────────────────
@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer(
        "🐟 *Добро пожаловать на Рыбную ферму!*\n\n"
        "Лови рыбу, улучшай снаряжение, открывай локации.\n"
        "💡 Банк — обмен монет на TON Fish за ⭐\n"
        "⚡ Бустеры — усиления за ⭐\n\n"
        "Нажми кнопку чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    try:
        payload = json.loads(message.successful_payment.invoice_payload)
        ptype   = payload.get('type', 'exchange')
    except Exception:
        await message.answer("❌ Ошибка обработки.")
        return

    if ptype == 'boost':
        boost_id = payload.get('boost', '')
        name = BOOST_NAMES.get(boost_id, 'Бустер')
        await message.answer(
            f"✅ *{name} активирован!*\n\nВернись в игру 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )
    else:
        coins    = payload.get('coins', 0)
        wallet   = payload.get('wallet', '')
        user_id  = payload.get('user_id', message.from_user.id)
        username = payload.get('username', '')
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


# ── Запуск ────────────────────────────────────────────
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
