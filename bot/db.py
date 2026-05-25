import logging
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import BigInteger, DateTime, Integer, String, func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bot.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 0 — не начал, 1 — урок 1 просмотрен, 2 — урок 2 просмотрен (урок 3 отправлен),
    # 3 — пользователь нажал «Хочу на курс» на 3-м уроке.
    lessons_watched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Момент, когда боту отправил 3-й урок. От него считаем 24-часовой дожим.
    lesson3_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Когда отправили ссылку на сайт (по клику CTA или по 24ч-таймеру). Любое значение
    # блокирует повторную отправку дожима.
    follow_up_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Inline-миграция: если таблица была создана старой версией кода с колонкой
        # lesson3_watched_at, добавляем новую lesson3_sent_at, не трогая старую.
        dialect = conn.dialect.name
        if dialect == "postgresql":
            stmt = "ALTER TABLE users ADD COLUMN IF NOT EXISTS lesson3_sent_at TIMESTAMP WITH TIME ZONE"
        else:
            # SQLite >= 3.35 поддерживает IF NOT EXISTS
            stmt = "ALTER TABLE users ADD COLUMN IF NOT EXISTS lesson3_sent_at DATETIME"
        try:
            await conn.execute(text(stmt))
        except Exception as e:
            logger.warning("Schema patch skipped: %s", e)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
