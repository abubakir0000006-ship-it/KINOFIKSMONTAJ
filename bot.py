"""
KinoFiksMontaj Bot
Принимает видео в Telegram, режет его на короткую нарезку (трейлер) и накладывает субтитры.

Пайплайн:
1. Пользователь шлёт видеофайл боту
2. Видео скачивается локально
3. PySceneDetect находит "смены сцен" -> получаем список интересных таймкодов
4. Из этих сцен собирается короткий клип (через ffmpeg, без перекодирования звука лишний раз)
5. Whisper (faster-whisper) распознаёт речь -> получаем субтитры (.srt)
6. ffmpeg "вшивает" субтитры в финальное видео
7. Готовый файл отправляется юзеру обратно
"""

import os
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode

from video_processor import build_trailer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kinofiks_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в переменных окружения. Добавь его в .env или в настройки Render.")

# Список Telegram ID админов (через запятую в переменной окружения ADMIN_IDS)
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# Ограничения, чтобы не убить сервер на бесплатном тарифе Render
MAX_INPUT_DURATION_SEC = int(os.environ.get("MAX_INPUT_DURATION_SEC", 60 * 30))  # макс 30 минут входного видео
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 500))
TARGET_CLIP_SECONDS = int(os.environ.get("TARGET_CLIP_SECONDS", 60))  # длина итогового трейлера

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Простая защита: храним id юзеров, у которых сейчас уже идёт обработка,
# чтобы не запускали по 5 видео параллельно и не положили сервер
busy_users = set()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я нарезаю короткие трейлеры из видео.\n\n"
        "Просто пришли мне видеофайл (как Document, чтобы избежать сжатия Telegram), "
        "и я:\n"
        "1. Найду самые «насыщенные» по смене сцен моменты\n"
        "2. Соберу из них короткий клип\n"
        "3. Распознаю речь и наложу субтитры\n"
        "4. Пришлю готовый файл обратно\n\n"
        f"Лимиты сейчас: длительность исходника до {MAX_INPUT_DURATION_SEC // 60} мин, "
        f"размер файла до {MAX_FILE_SIZE_MB} МБ.\n"
        "Команда /status — посмотреть лимиты, /cancel — отменить свою текущую обработку."
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(
        f"Макс. длительность исходника: {MAX_INPUT_DURATION_SEC // 60} мин\n"
        f"Макс. размер файла: {MAX_FILE_SIZE_MB} МБ\n"
        f"Целевая длина трейлера: {TARGET_CLIP_SECONDS} сек\n"
        f"Сейчас в обработке: {len(busy_users)} пользователь(ей)"
    )


@dp.message(F.video | F.document)
async def handle_video(message: Message):
    user_id = message.from_user.id

    if user_id in busy_users:
        await message.answer("У тебя уже обрабатывается одно видео, дождись его завершения 🙏")
        return

    file_obj = message.video or message.document
    if file_obj is None:
        return

    # Проверка по mime-типу для документов (если это не видео файл - откажем)
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Это не похоже на видеофайл. Пришли .mp4 / .mov / .mkv и т.п.")
        return

    file_size_mb = (file_obj.file_size or 0) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await message.answer(
            f"Файл слишком большой ({file_size_mb:.0f} МБ). Лимит сейчас {MAX_FILE_SIZE_MB} МБ."
        )
        return

    busy_users.add(user_id)
    status_msg = await message.answer("Скачиваю видео…")

    workdir = Path(tempfile.mkdtemp(prefix=f"kf_{user_id}_"))
    input_path = workdir / "input.mp4"
    output_path = workdir / "trailer.mp4"

    try:
        # Скачиваем файл от Telegram на диск
        tg_file = await bot.get_file(file_obj.file_id)
        await bot.download_file(tg_file.file_path, destination=input_path)

        await status_msg.edit_text("Анализирую сцены и звук…")

        def progress_callback(text: str):
            # синхронный callback из video_processor, отправляем апдейт в Telegram
            asyncio.create_task(safe_edit(status_msg, text))

        await build_trailer(
            input_path=input_path,
            output_path=output_path,
            target_seconds=TARGET_CLIP_SECONDS,
            max_duration_sec=MAX_INPUT_DURATION_SEC,
            progress_callback=progress_callback,
        )

        await status_msg.edit_text("Готово! Отправляю файл…")
        await message.answer_video(
            FSInputFile(output_path),
            caption=f"Готовый трейлер ({TARGET_CLIP_SECONDS} сек) с субтитрами 🎬"
        )
        await status_msg.delete()

    except Exception as e:
        log.exception("Ошибка обработки видео")
        await status_msg.edit_text(f"Что-то пошло не так: {e}")

    finally:
        busy_users.discard(user_id)
        shutil.rmtree(workdir, ignore_errors=True)


async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass  # игнорируем "message not modified" и подобные ошибки телеграма


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    user_id = message.from_user.id
    if user_id in busy_users:
        busy_users.discard(user_id)
        await message.answer("Обработка отменена (файл доделается в фоне, но результат не пришлю).")
    else:
        await message.answer("У тебя нет активной обработки.")


async def main():
    log.info("Бот запускается…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
