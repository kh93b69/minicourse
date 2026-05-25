from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., alias="BOT_TOKEN")
    site_url: str = Field(..., alias="SITE_URL")
    database_url: str = Field("sqlite+aiosqlite:///./bot.db", alias="DATABASE_URL")
    follow_up_check_interval: int = Field(60, alias="FOLLOW_UP_CHECK_INTERVAL")
    follow_up_delay_seconds: int = Field(86400, alias="FOLLOW_UP_DELAY_SECONDS")

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        # Railway даёт DATABASE_URL вида postgres://... — переводим в asyncpg-формат
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://"):]
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v


settings = Settings()
