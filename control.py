"""Модераторская панель (#control-panel).

Кнопки панели — persistent: timeout=None и фиксированные custom_id, поэтому они
продолжают работать после перезапуска бота (view регистрируется через add_view).
Выпадающие списки создаются при каждом нажатии и отправляются как ephemeral-сообщения,
так как их варианты (источники, каналы) меняются со временем.
"""
import logging

import disnake
from disnake.ext import commands

log = logging.getLogger("gamebot.control")

STAFF_ROLE_NAMES = {"Administrator", "Moderator"}
MAX_OPTIONS = 25  # ограничение Discord на количество вариантов в Select


def is_staff(member: disnake.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or any(role.name in STAFF_ROLE_NAMES for role in member.roles)


def control_embed() -> disnake.Embed:
    return disnake.Embed(
        title="🛠️ Панель управления новостями",
        description=(
            "➕ **Добавить источник** — RSS/Atom-лента игры или темы\n"
            "🗑️ **Удалить источник** — убрать ленту из отслеживания\n"
            "🔗 **Привязать к каналу** — выбрать канал для публикаций\n"
            "🔄 **Проверить сейчас** — принудительный опрос всех лент\n"
            "📋 **Список** — текущие источники"
        ),
        colour=disnake.Colour.dark_purple(),
    )


class PickView(disnake.ui.View):
    """Одноразовое меню выбора. После выбора вызывается handler(inter, value)."""

    def __init__(self, options: list[disnake.SelectOption], placeholder: str, handler):
        super().__init__(timeout=120)
        self.handler = handler
        select = disnake.ui.Select(placeholder=placeholder, options=options[:MAX_OPTIONS])
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, inter: disnake.MessageInteraction):
        await self.handler(inter, inter.values[0])


def _channel_view(guild: disnake.Guild, source_id: int) -> PickView:
    options = [
        disnake.SelectOption(label=f"#{c.name}"[:100], value=str(c.id))
        for c in guild.text_channels
    ]

    async def handler(inter: disnake.MessageInteraction, value: str):
        await inter.bot.db.set_source_channel(source_id, int(value))
        await inter.response.edit_message(content=f"✅ Публикации направлены в <#{value}>.", view=None)

    return PickView(options, "Выберите канал для публикаций", handler)


def _source_view(sources: list[dict], placeholder: str, handler) -> PickView:
    options = [
        disnake.SelectOption(label=s["name"][:100], value=str(s["id"]), description=s["feed_url"][:100])
        for s in sources
    ]
    return PickView(options, placeholder, handler)


async def _remove_handler(inter: disnake.MessageInteraction, value: str):
    await inter.bot.db.remove_source(int(value))
    await inter.response.edit_message(content="🗑️ Источник удалён.", view=None)


async def _bind_handler(inter: disnake.MessageInteraction, value: str):
    # Шаг 2 для привязки: выбираем канал для уже выбранного источника
    await inter.response.edit_message(
        content="Выберите канал для публикаций:",
        view=_channel_view(inter.guild, int(value)),
    )


class AddSourceModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="Новый источник новостей",
            custom_id="cp:modal_add",
            components=[
                disnake.ui.TextInput(label="Название (игра или тема)", custom_id="name", max_length=50),
                disnake.ui.TextInput(
                    label="RSS / Atom-ссылка",
                    custom_id="url",
                    placeholder="https://www.reddit.com/r/FPS/.rss",
                    max_length=300,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_staff(inter.user):
            await inter.response.send_message("⛔ Нет доступа.", ephemeral=True)
            return

        name = inter.text_values["name"].strip()
        url = inter.text_values["url"].strip()
        if not url.startswith(("https://", "http://")):
            await inter.response.send_message("❌ Ссылка должна начинаться с http:// или https://", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        source_id = await inter.bot.db.add_source(inter.guild_id, name, url, None)
        if source_id is None:
            await inter.followup.send("⚠️ Эта лента уже отслеживается.", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ «{name}» добавлен. Выберите канал для публикаций:",
            view=_channel_view(inter.guild, source_id),
            ephemeral=True,
        )


class ControlPanelView(disnake.ui.View):
    """Persistent-панель. Все проверки прав выполняются в каждом обработчике."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _guard(self, inter: disnake.MessageInteraction) -> bool:
        if not is_staff(inter.user):
            await inter.response.send_message(
                "⛔ Нет доступа: нужна роль Moderator или Administrator.", ephemeral=True
            )
            return False
        return True

    @disnake.ui.button(label="Добавить источник", emoji="➕",
                       style=disnake.ButtonStyle.success, custom_id="cp:add")
    async def add_source(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not await self._guard(inter):
            return
        await inter.response.send_modal(AddSourceModal())

    @disnake.ui.button(label="Удалить источник", emoji="🗑️",
                       style=disnake.ButtonStyle.danger, custom_id="cp:remove")
    async def remove_source(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not await self._guard(inter):
            return
        sources = await inter.bot.db.list_sources(inter.guild_id)
        if not sources:
            await inter.response.send_message("Список источников пуст.", ephemeral=True)
            return
        await inter.response.send_message(
            "Выберите источник для удаления:",
            view=_source_view(sources, "Источник для удаления", _remove_handler),
            ephemeral=True,
        )

    @disnake.ui.button(label="Привязать к каналу", emoji="🔗",
                       style=disnake.ButtonStyle.primary, custom_id="cp:bind")
    async def bind_channel(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not await self._guard(inter):
            return
        sources = await inter.bot.db.list_sources(inter.guild_id)
        if not sources:
            await inter.response.send_message("Сначала добавьте источник.", ephemeral=True)
            return
        await inter.response.send_message(
            "Выберите источник:",
            view=_source_view(sources, "Источник", _bind_handler),
            ephemeral=True,
        )

    @disnake.ui.button(label="Проверить сейчас", emoji="🔄",
                       style=disnake.ButtonStyle.secondary, custom_id="cp:refresh")
    async def refresh(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not await self._guard(inter):
            return
        await inter.response.defer(ephemeral=True)  # проверка может занять несколько секунд
        news = inter.bot.get_cog("NewsCog")
        if news is None:
            await inter.followup.send("❌ Новостной модуль не загружен.", ephemeral=True)
            return
        posted = await news.check_all()
        await inter.followup.send(f"🔄 Проверка завершена. Новых публикаций: {posted}.", ephemeral=True)

    @disnake.ui.button(label="Список", emoji="📋",
                       style=disnake.ButtonStyle.secondary, custom_id="cp:list")
    async def show_list(self, button: disnake.ui.Button, inter: disnake.MessageInteraction):
        if not await self._guard(inter):
            return
        sources = await inter.bot.db.list_sources(inter.guild_id)
        if sources:
            lines = [
                f"• **{s['name']}** → " + (f"<#{s['channel_id']}>" if s["channel_id"] else "_канал не задан_")
                for s in sources
            ]
            text = "\n".join(lines)[:4000]
        else:
            text = "Источников пока нет."
        embed = disnake.Embed(title="📋 Отслеживаемые источники", description=text,
                              colour=disnake.Colour.dark_purple())
        await inter.response.send_message(embed=embed, ephemeral=True)


class ControlCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Регистрируем persistent-кнопки, чтобы они работали после перезапуска
        bot.add_view(ControlPanelView())


def setup(bot: commands.Bot):
    bot.add_cog(ControlCog(bot))
