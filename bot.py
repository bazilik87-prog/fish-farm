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
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))  # Твой Telegram ID

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎣 Играть", web_app=WebAppInfo(url=GAME_URL))
    ]])
    await message.answer(
        "🐟 *Добро пожаловать на Рыбную ферму!*\n\n"
        "Лови рыбу, улучшай снаряжение, открывай локации.\n"
        "💡 В банке можно обменять монеты на TON Fish за ⭐\n\n"
        "Нажми кнопку чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    """Получаем запрос на обмен из игры"""
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return

    if data.get('action') == 'exchange':
        coins  = int(data.get('coins', 0))
        wallet = data.get('wallet', '').strip()
        user   = message.from_user

        if coins < 1 or not wallet:
            await message.answer("❌ Некорректные данные обмена.")
            return

        await bot.send_invoice(
            chat_id=message.chat.id,
            title="🐟 Обмен монет на TON Fish",
            description=f"Обмен {coins} монет → {coins} TON Fish на кошелёк {wallet[:12]}...",
            payload=json.dumps({"type":"exchange","coins": coins, "wallet": wallet, "user_id": user.id, "username": user.username or ""}),
            currency="XTR",
            prices=[LabeledPrice(label="⭐ Комиссия за обмен", amount=1)],
            provider_token="",
        )

    elif data.get('action') == 'boost':
        boost_id = data.get('boost','')
        user = message.from_user
        boost_names = {
            'doubleTap':  '⚡ Двойной улов (×2 монеты на 1 час)',
            'turboDlv':   '🚀 Турбо доставка (мгновенная доставка)',
            'luckyRod':   '🎣 Удачная рыбалка (+50% шанс на 30 мин)',
            'autoRepair': '🔧 Авторемонт (мгновенный ремонт транспорта)',
        }
        boost_name = boost_names.get(boost_id, 'Бустер')
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=boost_name,
            description=boost_name,
            payload=json.dumps({"type":"boost","boost":boost_id,"user_id":user.id}),
            currency="XTR",
            prices=[LabeledPrice(label="⭐ Бустер", amount=1)],
            provider_token="",
        )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    try:
        payload = json.loads(message.successful_payment.invoice_payload)
        ptype   = payload.get('type','exchange')
    except Exception:
        await message.answer("❌ Ошибка обработки.")
        return

    if ptype == 'boost':
        boost_id = payload.get('boost','')
        boost_names = {
            'doubleTap':  '⚡ Двойной улов активирован на 1 час!',
            'turboDlv':   '🚀 Турбо доставка активирована!',
            'luckyRod':   '🎣 Удачная рыбалка активирована на 30 мин!',
            'autoRepair': '🔧 Авторемонт активирован!',
        }
        msg = boost_names.get(boost_id, '✅ Бустер активирован!')
        # Отправляем данные обратно в игру через sendMessage с web_app_data не работает
        # Поэтому просто уведомляем — игрок должен вернуться в игру
        await message.answer(
            f"✅ *{msg}*\n\n"
            f"Вернись в игру — бустер активируется автоматически!\n\n"
            f"_Открой игру через кнопку /start_",
            parse_mode="Markdown"
        )
        # TODO: через Firebase можно передавать активацию бустера в игру напрямую

    else:  # exchange
        coins   = payload.get('coins', 0)
        wallet  = payload.get('wallet', '')
        user_id = payload.get('user_id', message.from_user.id)
        username = payload.get('username', '')

        await message.answer(
            f"✅ *Заявка на обмен принята!*\n\n"
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
                     f"👤 Игрок: {user_link}\n"
                     f"🆔 ID: `{user_id}`\n"
                     f"🪙 Монет: `{coins}`\n"
                     f"🐟 TON Fish: `{coins}`\n"
                     f"👛 Кошелёк:\n`{wallet}`\n\n"
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
