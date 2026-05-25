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
    """Шлём дожим тем, кто получил 3-й урок, но не нажал «Хочу на курс»
    за FOLLOW_UP_DELAY_SECONDS. lessons_watched == 2 означает «урок 3 отправлен,
    CTA не нажат». follow_up_sent_at IS NULL — дожим ещё не уходил."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.follow_up_delay_seconds)

    async with async_session_maker() as session:
        result = await session.execute(
            select(User).where(
                User.lessons_watched == 2,
                User.lesson3_sent_at.is_not(None),
                User.lesson3_sent_at <= threshold,
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
