"""Новостной модуль: опрос RSS/Atom-лент, защита от дублей и публикация Embed.

Источники хранятся в базе данных и управляются через панель #control-panel.
Reddit, игровые блоги и многие сайты отдают RSS, поэтому отдельный API-ключ не нужен.
"""
import asyncio
import html
import logging
import re

import aiohttp
import disnake
import feedparser
from disnake.ext import commands, tasks

log = logging.getLogger("gamebot.news")

# Reddit блокирует запросы без осмысленного User-Agent
USER_AGENT = "GameNewsBot/1.0 (Discord bot; RSS reader)"
MAX_POSTS_PER_SOURCE = 5   # ограничиваем спам при первом появлении большой порции новостей
PAUSE_BETWEEN_POSTS = 1.5  # секунд между публикациями (Rate Limits)
PAUSE_BETWEEN_FEEDS = 2.0  # секунд между лентами

IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str) -> str:
    """Убирает HTML-теги и обрезает текст для описания Embed."""
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    return " ".join(text.split())[:300]


def extract_image(entry, raw_html: str) -> str | None:
    """Ищет картинку: media-теги, enclosure, затем <img> внутри описания."""
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media and media[0].get("url"):
            return media[0]["url"]
    for enc in entry.get("enclosures", []):
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    match = IMG_RE.search(raw_html or "")
    return match.group(1) if match else None


def build_embed(item: dict, source_name: str) -> disnake.Embed:
    embed = disnake.Embed(
        title=item["title"][:256],
        url=item["link"] or None,
        description=item["summary"] or None,
        colour=disnake.Colour.blurple(),
    )
    if item["image"]:
        embed.set_image(url=item["image"])  # большая обложка
        embed.set_thumbnail(url=item["image"])
    embed.set_author(name=source_name[:256])
    footer = f"Источник: {source_name}"
    if item["published"]:
        footer += f" • {item['published']}"
    embed.set_footer(text=footer[:2048])
    return embed


class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._run_lock = asyncio.Lock()  # автоматическая и ручная проверка не должны идти одновременно
        self.news_loop.change_interval(seconds=bot.settings.news_interval)

    def cog_unload(self) -> None:
        self.news_loop.cancel()
        if self.session is not None and not self.session.closed:
            asyncio.create_task(self.session.close())

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self.session

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.news_loop.is_running():
            self.news_loop.start()

    @tasks.loop(seconds=900)
    async def news_loop(self) -> None:
        await self.check_all()

    @news_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ---------- основная логика ----------

    async def check_all(self) -> int:
        """Опрашивает все источники. Возвращает количество опубликованных записей."""
        posted = 0
        async with self._run_lock:
            for source in await self.bot.db.list_sources():
                try:
                    posted += await self._process_source(source)
                except Exception:  # один сломанный источник не должен останавливать остальные
                    log.exception("Сбой при обработке источника %s", source["name"])
                await asyncio.sleep(PAUSE_BETWEEN_FEEDS)
        log.info("Проверка новостей завершена, опубликовано: %d", posted)
        return posted

    async def _process_source(self, source: dict) -> int:
        if not source["channel_id"]:
            return 0  # канал не привязан — пропускаем

        channel = await self._resolve_channel(source["channel_id"])
        if channel is None:
            log.warning("Канал %s для «%s» недоступен", source["channel_id"], source["name"])
            return 0

        items = await self._fetch(source["feed_url"])
        if not items:
            return 0

        # Защита от дубликатов: отбираем записи, которых ещё не видели
        fresh = []
        for item in items:
            key = f"{source['id']}:{item['guid']}"
            if not await self.bot.db.is_seen(key):
                fresh.append((key, item))
        if not fresh:
            return 0

        # Первый опрос ленты: запоминаем текущие записи без публикации, чтобы не заспамить канал
        if not await self.bot.db.source_has_seen(source["id"]):
            for key, _ in fresh:
                await self.bot.db.mark_seen(key, source["id"])
            log.info("Лента «%s» инициализирована (%d записей)", source["name"], len(fresh))
            return 0

        posted = 0
        # Публикуем самые новые записи, в хронологическом порядке (от старых к новым)
        for _, item in reversed(fresh[:MAX_POSTS_PER_SOURCE]):
            try:
                await channel.send(embed=build_embed(item, source["name"]))
                posted += 1
                await asyncio.sleep(PAUSE_BETWEEN_POSTS)
            except disnake.HTTPException as e:
                log.warning("Не удалось отправить запись в «%s»: %s", source["name"], e)

        # Все найденные записи помечаем как просмотренные (включая пропущенные по лимиту)
        for key, _ in fresh:
            await self.bot.db.mark_seen(key, source["id"])
        return posted

    async def _fetch(self, url: str) -> list[dict]:
        try:
            async with self._get_session().get(url) as resp:
                if resp.status != 200:
                    log.warning("%s вернул HTTP %s", url, resp.status)
                    return []
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Ошибка загрузки %s: %s", url, e)
            return []

        # feedparser синхронный и может быть тяжёлым, поэтому выполняем в отдельном потоке
        parsed = await asyncio.to_thread(feedparser.parse, text)

        items = []
        for entry in parsed.entries[:20]:
            guid = entry.get("id") or entry.get("link")
            if not guid:
                continue
            raw = entry.get("summary", "")
            items.append({
                "guid": guid,
                "title": entry.get("title") or "Без названия",
                "link": entry.get("link"),
                "summary": strip_html(raw),
                "image": extract_image(entry, raw),
                "published": entry.get("published", ""),
            })
        return items

    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except disnake.HTTPException:
                return None
        return channel


def setup(bot: commands.Bot):
    bot.add_cog(NewsCog(bot))
