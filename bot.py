"""Точка входа: создание бота, Intents и подключение Cogs."""
import inspect
import logging

import disnake
from disnake.ext import commands

from config import load_settings
from database import Database

COGS = ("cogs.setup", "cogs.voice", "cogs.news", "cogs.control")
log = logging.getLogger("gamebot")


class GameBot(commands.Bot):
    def __init__(self, settings):
        # Intents: Guilds (структура сервера) и Voice State (Join-to-Create).
        # Привилегированные Intents (members, message_content, presence) не требуются.
        intents = disnake.Intents.none()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",  # бот работает только через slash-команды
            intents=intents,
            # Для GUILD_ID команды появляются мгновенно; без него синхронизация глобальная (до часа)
            test_guilds=[settings.guild_id] if settings.guild_id else None,
        )
        self.settings = settings
        self.db = Database(settings.db_path)

    async def setup_hook(self) -> None:
        await self.db.connect()
        for ext in COGS:
            # Совместимость: load_extension может быть как синхронным, так и асинхронным
            result = self.load_extension(ext)
            if inspect.isawaitable(result):
                await result
        log.info("Модули загружены: %s", ", ".join(COGS))

    async def on_ready(self) -> None:
        log.info("Бот запущен: %s (серверов: %d)", self.user, len(self.guilds))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    GameBot(settings).run(settings.token)


if __name__ == "__main__":
    main()
