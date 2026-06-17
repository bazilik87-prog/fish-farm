import asyncio
import os
import json
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
GAME_URL   = os.getenv("GAME_URL",  "https://ВАШ_НИК.github.io/fish-farm/")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

BOOST_NAMES = {
    'doubleTap':  '⚡ Двойной улов (×2 монеты на 1 час)',
    'turboDlv':   '🚀 Турбо доставка (мгновенная доставка)',
    'luckyRod':   '🎣 Удачная рыбалка (+50% шанс на 30 мин)',
    'autoRepair': '🔧 Авторемонт (мгновенный ремонт транспорта)',
}


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer(
        "🐟 *Добро пожаловать на Рыбную ферму!*\n\n"
        "Лови рыбу, улучшай снаряжение, открывай локации.\n"
        "💡 В банке — обмен монет на TON Fish за ⭐\n"
        "⚡ В бустерах — усиления за ⭐\n\n"
        "Нажми кнопку чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return

    action = data.get('action')
    user = message.from_user

    # Создаём invoice link и отправляем обратно в игру
    if action == 'exchange':
        coins  = int(data.get('coins', 0))
        wallet = data.get('wallet', '').strip()
        if coins < 1 or not wallet:
            await message.answer("❌ Некорректные данные.")
            return
        link = await bot.create_invoice_link(
            title="🐟 Обмен монет на TON Fish",
            description=f"{coins} монет → {coins} TON Fish · Кошелёк: {wallet[:16]}...",
            payload=json.dumps({"type":"exchange","coins":coins,"wallet":wallet,"user_id":user.id,"username":user.username or ""}),
            currency="XTR",
            prices=[LabeledPrice(label="⭐ Комиссия", amount=1)],
            provider_token="",
        )
        await message.answer(
            f"💱 *Обмен {coins} монет → {coins} TON Fish*\n\nНажми кнопку для оплаты ⭐",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⭐ Оплатить 1 звезду", url=link)
            ]])
        )

    elif action == 'boost':
        boost_id = data.get('boost', '')
        name = BOOST_NAMES.get(boost_id, 'Бустер')
        link = await bot.create_invoice_link(
            title=name,
            description=name,
            payload=json.dumps({"type":"boost","boost":boost_id,"user_id":user.id}),
            currency="XTR",
            prices=[LabeledPrice(label="⭐ Бустер", amount=1)],
            provider_token="",
        )
        await message.answer(
            f"⚡ *{name}*\n\nНажми кнопку для оплаты ⭐",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⭐ Купить за 1 звезду", url=link)
            ]])
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
            f"✅ *{name} активирован!*\n\n"
            f"Вернись в игру — бустер будет активен автоматически!\n\n"
            f"_Открой игру через /start_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
            ]])
        )

    else:  # exchange
        coins    = payload.get('coins', 0)
        wallet   = payload.get('wallet', '')
        user_id  = payload.get('user_id', message.from_user.id)
        username = payload.get('username', '')

        await message.answer(
            f"✅ *Заявка принята!*\n\n"
            f"🪙 Монет: `{coins}`\n"
            f"🐟 TON Fish: `{coins}`\n"
            f"👛 Кошелёк: `{wallet}`\n\n"
            f"⏳ Токены будут отправлены в течение 24 часов.",
            parse_mode="Markdown"
        )
        if ADMIN_ID:
            user_link = f"@{username}" if username else f"[user](tg://user?id={user_id})"
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"💰 *Новый обмен!*\n\n"
                     f"👤 {user_link}\n"
                     f"🆔 `{user_id}`\n"
                     f"🪙 Монет: `{coins}`\n"
                     f"🐟 TON Fish: `{coins}`\n"
                     f"👛 `{wallet}`\n\n"
                     f"⭐ Звезда получена — отправь токены!",
                parse_mode="Markdown"
            )


@dp.message()
async def any_message(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Открыть игру", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer("Нажми кнопку чтобы играть 👇", reply_markup=keyboard)


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
