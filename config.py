"""Загрузка настроек из переменных окружения (.env)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int | None
    db_path: str
    news_interval: int
    log_level: str


def load_settings() -> Settings:
    """Читает .env и переменные окружения. Бросает ошибку, если токен не задан."""
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Переменная DISCORD_TOKEN не задана (проверьте файл .env)")

    guild = os.getenv("GUILD_ID", "").strip()
    return Settings(
        token=token,
        guild_id=int(guild) if guild else None,
        db_path=os.getenv("DB_PATH", "bot.db"),
        # Не даём поставить слишком частый опрос RSS: это нагрузка на сторонние сервисы
        news_interval=max(60, int(os.getenv("NEWS_CHECK_INTERVAL", "900"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
