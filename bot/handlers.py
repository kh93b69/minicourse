from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import content
from bot.config import settings
from bot.db import User, async_session_maker

router = Router()

WATCHED_CALLBACK_PREFIX = "watched"
CTA_CALLBACK = "cta:course"

MESSAGE_CHAR_LIMIT = 3500  # запас до telegram-лимита 4096


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


# ──────────────────────────────── admin commands ─────────────────────────────


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _format_user_status(u: User) -> str:
    if u.lessons_watched == 0:
        return "не начал"
    if u.lessons_watched == 1:
        return "урок 1"
    if u.lessons_watched == 2:
        return "урок 2, ⏰ дожим" if u.follow_up_sent_at else "урок 2"
    return "урок 3, 🔥 CTA"


def _format_user_line(idx: int, u: User) -> str:
    who = f"@{u.username}" if u.username else "—"
    return f"{idx}. {who} <code>{u.tg_id}</code> — {_format_user_status(u)}"


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Возвращает Telegram ID отправителю. Публичная команда — нужна для того,
    чтобы будущие админы узнали свой ID."""
    uname = f"@{message.from_user.username}" if message.from_user.username else "—"
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>\n"
        f"Username: {uname}",
        parse_mode="HTML",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return  # тихо игнорируем не-админов

    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(User.tg_id)))).scalar_one()
        l1 = (await session.execute(
            select(func.count(User.tg_id)).where(User.lessons_watched >= 1)
        )).scalar_one()
        l2 = (await session.execute(
            select(func.count(User.tg_id)).where(User.lessons_watched >= 2)
        )).scalar_one()
        cta = (await session.execute(
            select(func.count(User.tg_id)).where(User.lessons_watched == 3)
        )).scalar_one()
        auto_follow_up = (await session.execute(
            select(func.count(User.tg_id)).where(
                User.lessons_watched == 2,
                User.follow_up_sent_at.is_not(None),
            )
        )).scalar_one()

    got_link = cta + auto_follow_up
    overall_conv = (got_link / total * 100) if total else 0.0
    cta_conv = (cta / l2 * 100) if l2 else 0.0

    text = (
        "📊 <b>Статистика курса</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🎬 Посмотрели урок 1: <b>{l1}</b>\n"
        f"🎬 Посмотрели урок 2: <b>{l2}</b> (значит получили урок 3)\n"
        f"🔥 Нажали «Хочу на курс»: <b>{cta}</b>\n"
        f"⏰ Получили автоматический дожим: <b>{auto_follow_up}</b>\n\n"
        f"📈 Конверсия в ссылку (CTA + дожим): <b>{got_link} / {total}</b> ({overall_conv:.1f}%)\n"
        f"📈 CTA-конверсия (от получивших урок 3): <b>{cta} / {l2}</b> ({cta_conv:.1f}%)"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    async with async_session_maker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        users = result.scalars().all()

    if not users:
        await message.answer("Пока никого нет.")
        return

    header = f"👥 <b>Все пользователи ({len(users)}):</b>\n\n"
    current = header
    for idx, u in enumerate(users, 1):
        line = _format_user_line(idx, u) + "\n"
        if len(current) + len(line) > MESSAGE_CHAR_LIMIT:
            await message.answer(current, parse_mode="HTML")
            current = ""
        current += line
    if current:
        await message.answer(current, parse_mode="HTML")
