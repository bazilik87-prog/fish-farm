import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# Вставь сюда токен своего бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "8874283510:AAFo-jmFierWIg4xwlf0Y8JRTOJTytEoUWM")

# URL где хостится твоя игра (после деплоя на GitHub Pages / Vercel)
GAME_URL = os.getenv("GAME_URL", "https://bazilik87-prog.github.io/fish-farm/")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎣 Играть в Рыбную ферму",
            web_app=WebAppInfo(url=GAME_URL)
        )
    ]])
    await message.answer(
        "🐟 *Добро пожаловать на Рыбную ферму!*\n\n"
        "Тапай по воде, лови рыбу, улучшай снаряжение.\n"
        "Зарабатывай криптомонеты в блокчейне TON.\n"
        "Открывай новые виды рыб и стань лучшим рыбаком!\n\n"
        "Нажми кнопку ниже чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.message()
async def any_message(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎣 Открыть игру",
            web_app=WebAppInfo(url=GAME_URL)
        )
    ]])
    await message.answer("Нажми кнопку чтобы играть 👇", reply_markup=keyboard)


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
