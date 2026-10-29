"""Команда /setup-server: создаёт роли, категории и каналы, публикует панели.

Команда идемпотентна: повторный запуск не создаёт дубликаты, а находит
существующие объекты по имени и обновляет настройки в базе данных.
"""
import asyncio
import logging

import disnake
from disnake.ext import commands

from cogs.control import ControlPanelView, control_embed

log = logging.getLogger("gamebot.setup")

PLATFORM_ROLES = ["PC", "PlayStation", "Xbox", "Nintendo Switch", "Mobile"]
GENRE_ROLES = ["RPG", "FPS", "Стратегии", "Инди", "MOBA"]

# (название канала, отображаемое имя раздела, RSS-лента).
# Reddit отдаёт RSS у любого сабреддита; ленты можно заменить на любые другие.
NEWS_PRESETS = [
    ("новости-pc", "PC", "https://www.reddit.com/r/pcgaming/.rss"),
    ("новости-консоли", "Консоли", "https://www.reddit.com/r/PlayStation/.rss"),
    ("новости-рпг", "RPG", "https://www.reddit.com/r/CRPG/.rss"),
    ("новости-шутеры", "Шутеры", "https://www.reddit.com/r/FPS/.rss"),
]

PAUSE = 0.5  # пауза между созданием объектов: мягкое соблюдение Rate Limits


def _staff_overwrites(guild, admin, mod) -> dict:
    """Скрытая категория: видна только администраторам и модераторам."""
    return {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        admin: disnake.PermissionOverwrite(view_channel=True, send_messages=True),
        mod: disnake.PermissionOverwrite(view_channel=True, send_messages=True),
    }


def _readonly_overwrites(guild, admin, mod) -> dict:
    """Информационные каналы: читать могут все, писать — только персонал."""
    return {
        guild.default_role: disnake.PermissionOverwrite(send_messages=False),
        admin: disnake.PermissionOverwrite(send_messages=True),
        mod: disnake.PermissionOverwrite(send_messages=True),
    }


class RolePickSelect(disnake.ui.Select):
    """Выбор платформ и жанров. Options фиксированы, поэтому view можно сделать persistent."""

    def __init__(self):
        names = PLATFORM_ROLES + GENRE_ROLES
        super().__init__(
            custom_id="roles:select",
            placeholder="Выберите платформы и жанры",
            min_values=0,
            max_values=len(names),
            options=[disnake.SelectOption(label=n) for n in names],
        )

    async def callback(self, inter: disnake.MessageInteraction):
        managed = set(PLATFORM_ROLES + GENRE_ROLES)
        chosen = set(self.values)
        member = inter.user
        guild_roles = {r.name: r for r in inter.guild.roles}

        to_add = [guild_roles[n] for n in chosen if n in guild_roles and guild_roles[n] not in member.roles]
        to_remove = [r for r in member.roles if r.name in managed and r.name not in chosen]
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Выбор ролей")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Выбор ролей")
        except disnake.HTTPException as e:
            log.warning("Не удалось обновить роли %s: %s", member, e)
            await inter.response.send_message("❌ Не удалось обновить роли, попробуйте позже.", ephemeral=True)
            return
        await inter.response.send_message("✅ Ваши роли обновлены.", ephemeral=True)


class RolePickView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RolePickSelect())


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Регистрируем persistent-view, чтобы выбор ролей работал после перезапуска
        bot.add_view(RolePickView())

    @commands.slash_command(
        name="setup-server",
        description="Создать структуру сервера (только для администраторов)",
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def setup_server(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message("⛔ Команда доступна только администраторам.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)  # создание занимает время
        try:
            await self._build(inter.guild)
        except disnake.HTTPException as e:
            log.exception("Ошибка /setup-server")
            await inter.followup.send(f"❌ Ошибка Discord API ({e.status}): {e.text}", ephemeral=True)
            return
        await inter.followup.send("✅ Сервер настроен: роли, категории, каналы и панели.", ephemeral=True)

    # ---------- вспомогательные методы (поиск или создание) ----------

    async def _get_or_create_role(self, guild: disnake.Guild, name: str, **kwargs) -> disnake.Role:
        role = disnake.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, reason="/setup-server", **kwargs)
            await asyncio.sleep(PAUSE)
        return role

    async def _get_or_create_category(self, guild, name, overwrites=None) -> disnake.CategoryChannel:
        category = disnake.utils.get(guild.categories, name=name)
        if category is None:
            category = await guild.create_category(name, overwrites=overwrites or {}, reason="/setup-server")
            await asyncio.sleep(PAUSE)
        return category

    async def _get_or_create_text(self, category, name, topic=None) -> disnake.TextChannel:
        channel = disnake.utils.get(category.text_channels, name=name)
        if channel is None:
            channel = await category.guild.create_text_channel(
                name, category=category, topic=topic, reason="/setup-server"
            )
            await asyncio.sleep(PAUSE)
        return channel

    async def _get_or_create_voice(self, category, name) -> disnake.VoiceChannel:
        channel = disnake.utils.get(category.voice_channels, name=name)
        if channel is None:
            channel = await category.guild.create_voice_channel(name, category=category, reason="/setup-server")
            await asyncio.sleep(PAUSE)
        return channel

    # ---------- сборка структуры ----------

    async def _build(self, guild: disnake.Guild) -> None:
        # Роли персонала и участников
        admin = await self._get_or_create_role(
            guild, "Administrator", colour=disnake.Colour.red(), hoist=True,
            permissions=disnake.Permissions(administrator=True),
        )
        mod = await self._get_or_create_role(
            guild, "Moderator", colour=disnake.Colour.blue(), hoist=True,
            permissions=disnake.Permissions(manage_messages=True, kick_members=True,
                                            mute_members=True, deafen_members=True, move_members=True),
        )
        await self._get_or_create_role(guild, "Member", colour=disnake.Colour.green())
        for name in PLATFORM_ROLES + GENRE_ROLES:
            await self._get_or_create_role(guild, name, mentionable=True)

        # 📌 Информация
        info = await self._get_or_create_category(guild, "📌 Информация", _readonly_overwrites(guild, admin, mod))
        await self._get_or_create_text(info, "правила")
        await self._get_or_create_text(info, "новости-сервера")
        roles_channel = await self._get_or_create_text(info, "выбор-ролей")

        # 🎮 Игровые новости: канал + RSS-источник, привязанный к нему
        news = await self._get_or_create_category(guild, "🎮 Игровые новости", _readonly_overwrites(guild, admin, mod))
        for channel_name, label, feed_url in NEWS_PRESETS:
            channel = await self._get_or_create_text(news, channel_name, topic=f"Новости: {label}")
            await self.bot.db.add_source(guild.id, label, feed_url, channel.id)  # дубли игнорируются

        # 💬 Общение
        chat = await self._get_or_create_category(guild, "💬 Общение")
        for name in ("общий-чат", "мемы", "поиск-пати"):
            await self._get_or_create_text(chat, name)

        # 🔊 Голосовые каналы: хаб Join-to-Create и категория для временных комнат
        voice = await self._get_or_create_category(guild, "🔊 Голосовые каналы")
        hub = await self._get_or_create_voice(voice, "➕ Создать комнату")
        await self._get_or_create_voice(voice, "Общий войс")
        temp = await self._get_or_create_category(guild, "⏳ Временные комнаты")

        # 🛠️ Администрация (скрытая)
        hidden = await self._get_or_create_category(guild, "🛠️ Администрация", _staff_overwrites(guild, admin, mod))
        log_channel = await self._get_or_create_text(hidden, "log-bot")
        control = await self._get_or_create_text(hidden, "control-panel")

        # Сохраняем ID объектов в базе данных
        await self.bot.db.update_guild_settings(
            guild.id,
            hub_channel_id=hub.id,
            temp_category_id=temp.id,
            log_channel_id=log_channel.id,
            control_channel_id=control.id,
        )

        # Публикуем панели; старые сообщения бота удаляем, чтобы не плодить дубликаты
        await roles_channel.purge(limit=20, check=lambda m: m.author == guild.me)
        await roles_channel.send("Выберите платформы и жанры, чтобы получить соответствующие роли:",
                                 view=RolePickView())
        await control.purge(limit=50, check=lambda m: m.author == guild.me)
        await control.send(embed=control_embed(), view=ControlPanelView())
        await log_channel.send("✅ Структура сервера создана или обновлена.")


def setup(bot: commands.Bot):
    bot.add_cog(SetupCog(bot))
