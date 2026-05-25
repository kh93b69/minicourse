from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import content
from bot.db import User, async_session_maker

router = Router()

WATCHED_CALLBACK_PREFIX = "watched"


def watched_keyboard(lesson_number: int) -> InlineKeyboardMarkup:
    """Кнопка «Я просмотрел» с зашитым номером урока (1..3)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=content.WATCHED_BUTTON_TEXT,
                    callback_data=f"{WATCHED_CALLBACK_PREFIX}:{lesson_number}",
                )
            ]
        ]
    )


async def get_or_create_user(session: AsyncSession, tg_id: int, username: str | None) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=username, lessons_watched=0)
        session.add(user)
        await session.flush()
    elif user.username != username:
        user.username = username
    return user


async def send_lesson(bot: Bot, chat_id: int, lesson_index: int) -> None:
    """lesson_index — 0-based индекс в LESSONS."""
    lesson = content.LESSONS[lesson_index]
    await bot.send_message(
        chat_id=chat_id,
        text=lesson["text"],
        reply_markup=watched_keyboard(lesson_index + 1),
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    async with async_session_maker() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        watched = user.lessons_watched
        await session.commit()

    if watched == 0:
        await message.answer(content.WELCOME_TEXT)
        await send_lesson(bot, message.chat.id, 0)
    elif watched < 3:
        # курс в процессе — напомним текущий урок
        await message.answer(content.RESUME_TEXT)
        await send_lesson(bot, message.chat.id, watched)  # watched == номер уже просмотренного = индекс следующего
    else:
        # все 3 урока пройдены
        await message.answer(content.COURSE_COMPLETED_TEXT)


@router.callback_query(F.data.startswith(f"{WATCHED_CALLBACK_PREFIX}:"))
async def on_watched(callback: CallbackQuery, bot: Bot) -> None:
    try:
        lesson_number = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    if lesson_number not in (1, 2, 3):
        await callback.answer()
        return

    async with async_session_maker() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )

        # Идемпотентность: клик по кнопке принимаем только если она соответствует
        # текущему «следующему ожидаемому» уроку (lessons_watched + 1).
        if lesson_number != user.lessons_watched + 1:
            await callback.answer(content.STALE_BUTTON_TEXT, show_alert=False)
            # уберём кнопку у старого сообщения, чтобы по ней нельзя было кликать
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        user.lessons_watched = lesson_number
        if lesson_number == 3:
            user.lesson3_watched_at = datetime.now(timezone.utc)
        await session.commit()

    # Снимаем кнопку у предыдущего сообщения, чтобы не было повторных нажатий
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("Принято ✅")

    if lesson_number < 3:
        await send_lesson(bot, callback.message.chat.id, lesson_number)  # шлём следующий
    else:
        await bot.send_message(callback.message.chat.id, content.COURSE_COMPLETED_TEXT)
