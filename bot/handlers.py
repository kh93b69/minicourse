from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import content
from bot.config import settings
from bot.db import User, async_session_maker

router = Router()

WATCHED_CALLBACK_PREFIX = "watched"
CTA_CALLBACK = "cta:course"


def watched_keyboard(lesson_number: int) -> InlineKeyboardMarkup:
    """Кнопка «Я просмотрел» для уроков 1 и 2."""
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


def cta_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Хочу на курс» под 3-м уроком."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=content.CTA_BUTTON_TEXT, callback_data=CTA_CALLBACK)]
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
    """lesson_index — 0-based индекс в LESSONS.
    Для уроков 1, 2 — кнопка «Я просмотрел». Для урока 3 — «Хочу на курс»."""
    lesson = content.LESSONS[lesson_index]
    markup = cta_keyboard() if lesson_index == 2 else watched_keyboard(lesson_index + 1)
    await bot.send_message(
        chat_id=chat_id,
        text=lesson["text"],
        reply_markup=markup,
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
        # Если /start приходит впервые и юзер сразу попадает на «отправляем урок 3» —
        # это случай возврата на стейт 2 после рестарта; lesson3_sent_at уже должен быть
        # установлен. Если по какой-то причине нет — поставим сейчас.
        if watched == 2 and user.lesson3_sent_at is None:
            user.lesson3_sent_at = datetime.now(timezone.utc)
        await session.commit()

    if watched == 0:
        await message.answer(content.WELCOME_TEXT)
        await send_lesson(bot, message.chat.id, 0)
    elif watched == 1:
        await message.answer(content.RESUME_TEXT)
        await send_lesson(bot, message.chat.id, 1)
    elif watched == 2:
        await message.answer(content.RESUME_TEXT)
        await send_lesson(bot, message.chat.id, 2)
    else:  # watched == 3, CTA уже нажат
        await message.answer(
            content.ALREADY_JOINED_TEXT.format(site_url=settings.site_url),
            disable_web_page_preview=False,
        )


@router.callback_query(F.data.startswith(f"{WATCHED_CALLBACK_PREFIX}:"))
async def on_watched(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает кнопку «Я просмотрел» для уроков 1 и 2."""
    try:
        lesson_number = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    if lesson_number not in (1, 2):
        await callback.answer()
        return

    async with async_session_maker() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )

        # Идемпотентность: принимаем клик только если он соответствует
        # ожидаемому переходу (lessons_watched + 1).
        if lesson_number != user.lessons_watched + 1:
            await callback.answer(content.STALE_BUTTON_TEXT, show_alert=False)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        user.lessons_watched = lesson_number
        # Когда юзер кликнул «Я просмотрел» на уроке 2, мы сейчас отправим урок 3 —
        # фиксируем момент отправки для 24-часового дожима.
        if lesson_number == 2:
            user.lesson3_sent_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("Принято ✅")
    await send_lesson(bot, callback.message.chat.id, lesson_number)


@router.callback_query(F.data == CTA_CALLBACK)
async def on_cta(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает «Хочу на курс» с 3-го урока — моментально шлёт ссылку,
    блокирует автоматический 24ч-дожим."""
    async with async_session_maker() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.username
        )
        if user.lessons_watched < 3:
            user.lessons_watched = 3
        if user.follow_up_sent_at is None:
            user.follow_up_sent_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("🔥")
    await bot.send_message(
        callback.message.chat.id,
        content.CTA_ACCEPTED_TEXT.format(site_url=settings.site_url),
        disable_web_page_preview=False,
    )
