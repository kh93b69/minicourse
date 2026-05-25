import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select

from bot import content
from bot.config import settings
from bot.db import User, async_session_maker

logger = logging.getLogger(__name__)


async def _send_follow_up_batch(bot: Bot) -> None:
    """Находим пользователей, у которых прошло >= FOLLOW_UP_DELAY_SECONDS с 3-го урока
    и которым ещё не слали дожим. Шлём, помечаем."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.follow_up_delay_seconds)

    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(
                User.lessons_watched == 3,
                User.lesson3_watched_at.is_not(None),
                User.lesson3_watched_at <= threshold,
                User.follow_up_sent_at.is_(None),
            )
        )
        users = result.scalars().all()

        for user in users:
            text = content.FOLLOW_UP_TEXT.format(site_url=settings.site_url)
            try:
                await bot.send_message(user.tg_id, text, disable_web_page_preview=False)
                user.follow_up_sent_at = datetime.now(timezone.utc)
            except TelegramForbiddenError:
                # пользователь заблокировал бота — больше не пытаемся
                logger.info("Follow-up: user %s blocked the bot", user.tg_id)
                user.follow_up_sent_at = datetime.now(timezone.utc)
            except TelegramBadRequest as e:
                logger.warning("Follow-up: bad request for user %s: %s", user.tg_id, e)
            except Exception:
                logger.exception("Follow-up: unexpected error for user %s", user.tg_id)

        await session.commit()


async def run_follow_up_loop(bot: Bot) -> None:
    """Бесконечный цикл проверки дожимов. Запускается из main как background task."""
    logger.info(
        "Scheduler started: check every %ss, delay %ss",
        settings.follow_up_check_interval,
        settings.follow_up_delay_seconds,
    )
    while True:
        try:
            await _send_follow_up_batch(bot)
        except Exception:
            logger.exception("Follow-up loop iteration failed")
        await asyncio.sleep(settings.follow_up_check_interval)
