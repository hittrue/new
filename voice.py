"""Join-to-Create: персональные временные голосовые каналы.

Логика:
  1. Пользователь заходит в канал-хаб → создаётся комната в категории временных комнат.
  2. Владелец получает права управления комнатой и переносится в неё.
  3. Когда комната пустеет, через CLEANUP_DELAY секунд она удаляется
     (если за это время никто не зашёл обратно).
  4. Состояние хранится в SQLite, поэтому переживает перезапуск бота.
"""
import asyncio
import logging
from collections import defaultdict

import disnake
from disnake.ext import commands

log = logging.getLogger("gamebot.voice")

CLEANUP_DELAY = 5  # секунд ожидания перед удалением пустой комнаты


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cleanup_tasks: dict[int, asyncio.Task] = {}
        # Блокировка на пользователя: защищает от создания двух комнат при быстрых повторных входах
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._synced = False

    def cog_unload(self) -> None:
        for task in self._cleanup_tasks.values():
            task.cancel()
        self._cleanup_tasks.clear()

    # ---------- события ----------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Однократная проверка после старта: удаляем записи о несуществующих и пустых комнатах
        if self._synced:
            return
        self._synced = True
        await self._sweep_stale_rooms()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: disnake.Member,
                                    before: disnake.VoiceState, after: disnake.VoiceState) -> None:
        if member.bot:
            return

        # Вход в канал
        if after.channel is not None and after.channel != before.channel:
            self._cancel_cleanup(after.channel.id)  # кто-то вернулся в комнату: отменяем удаление
            settings = await self.bot.db.get_guild_settings(member.guild.id)
            if settings and after.channel.id == settings["hub_channel_id"]:
                await self._create_room(member, settings["temp_category_id"])

        # Выход из канала
        if before.channel is not None and before.channel != after.channel:
            self._schedule_cleanup(before.channel)

    # ---------- создание комнаты ----------

    async def _create_room(self, member: disnake.Member, category_id: int | None) -> None:
        async with self._locks[member.id]:
            guild = member.guild

            # Если у пользователя уже есть комната, переносим его туда
            existing_id = await self.bot.db.find_owned_room(guild.id, member.id)
            if existing_id:
                existing = guild.get_channel(existing_id)
                if existing is not None:
                    await self._move(member, existing)
                    return
                await self.bot.db.remove_temp_room(existing_id)

            category = guild.get_channel(category_id) if category_id else None
            if not isinstance(category, disnake.CategoryChannel):
                log.error("Категория временных комнат не найдена на сервере %s", guild.id)
                return

            overwrites = {
                member: disnake.PermissionOverwrite(
                    manage_channels=True,   # переименование и лимит
                    move_members=True,      # перенос участников
                    mute_members=True,
                    deafen_members=True,
                    connect=True,
                    speak=True,
                ),
            }
            name = f"🔊 Комната | {member.display_name}"[:100]

            try:
                channel = await guild.create_voice_channel(
                    name, category=category, overwrites=overwrites,
                    reason=f"Join-to-Create для {member}",
                )
            except disnake.HTTPException as e:
                log.warning("Не удалось создать комнату для %s: %s", member, e)
                return

            await self.bot.db.add_temp_room(channel.id, guild.id, member.id)
            await self._move(member, channel)

    async def _move(self, member: disnake.Member, channel: disnake.VoiceChannel) -> None:
        try:
            await member.move_to(channel, reason="Join-to-Create")
        except disnake.HTTPException:
            # Участник успел выйти из голосового канала: комната останется пустой и будет удалена
            self._schedule_cleanup(channel)

    # ---------- удаление пустых комнат ----------

    def _schedule_cleanup(self, channel: disnake.abc.GuildChannel) -> None:
        if not isinstance(channel, disnake.VoiceChannel):
            return
        if channel.id in self._cleanup_tasks or channel.members:
            return
        self._cleanup_tasks[channel.id] = asyncio.create_task(
            self._cleanup_later(channel.guild.id, channel.id)
        )

    def _cancel_cleanup(self, channel_id: int) -> None:
        task = self._cleanup_tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()

    async def _cleanup_later(self, guild_id: int, channel_id: int) -> None:
        try:
            await asyncio.sleep(CLEANUP_DELAY)

            # Удаляем только временные комнаты, созданные ботом
            if not await self.bot.db.is_temp_room(channel_id):
                return

            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if channel is None:
                await self.bot.db.remove_temp_room(channel_id)
                return
            if channel.members:
                return  # в комнате снова есть люди

            await channel.delete(reason="Временная комната пуста")
            await self.bot.db.remove_temp_room(channel_id)
            log.info("Удалена пустая комната %s", channel_id)
        except disnake.NotFound:
            await self.bot.db.remove_temp_room(channel_id)
        except disnake.HTTPException as e:
            log.warning("Ошибка удаления комнаты %s: %s", channel_id, e)
        finally:
            # Удаляем запись только если это наша задача (защита от гонки с новой задачей)
            if self._cleanup_tasks.get(channel_id) is asyncio.current_task():
                self._cleanup_tasks.pop(channel_id, None)

    async def _sweep_stale_rooms(self) -> None:
        for row in await self.bot.db.list_temp_rooms():
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue  # бот не на этом сервере: не трогаем запись
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                await self.bot.db.remove_temp_room(row["channel_id"])
            elif isinstance(channel, disnake.VoiceChannel):
                self._schedule_cleanup(channel)

    # ---------- команды владельца комнаты ----------

    @commands.slash_command(name="room", description="Управление вашей временной комнатой")
    async def room(self, inter: disnake.ApplicationCommandInteraction):
        pass  # группа команд

    async def _owned_room(self, inter: disnake.ApplicationCommandInteraction) -> disnake.VoiceChannel | None:
        voice = inter.user.voice
        if voice is None or voice.channel is None:
            await inter.response.send_message("Сначала зайдите в вашу комнату.", ephemeral=True)
            return None
        owner = await self.bot.db.get_temp_owner(voice.channel.id)
        if owner != inter.user.id:
            await inter.response.send_message("Вы не являетесь владельцем этой комнаты.", ephemeral=True)
            return None
        return voice.channel

    @room.sub_command(name="limit", description="Лимит участников (0 — без ограничения)")
    async def room_limit(self, inter: disnake.ApplicationCommandInteraction,
                         value: commands.Range[0, 99]):
        channel = await self._owned_room(inter)
        if channel is None:
            return
        try:
            await channel.edit(user_limit=value, reason="Лимит задан владельцем")
        except disnake.HTTPException as e:
            await inter.response.send_message(f"❌ Не удалось изменить лимит: {e.text}", ephemeral=True)
            return
        await inter.response.send_message(
            f"✅ Лимит: {value if value else 'без ограничений'}", ephemeral=True
        )

    @room.sub_command(name="lock", description="Закрыть или открыть комнату для остальных")
    async def room_lock(self, inter: disnake.ApplicationCommandInteraction, locked: bool):
        channel = await self._owned_room(inter)
        if channel is None:
            return
        try:
            # None — вернуть право по умолчанию, False — запретить подключение
            await channel.set_permissions(
                channel.guild.default_role,
                connect=False if locked else None,
                reason="Владелец изменил доступ",
            )
        except disnake.HTTPException as e:
            await inter.response.send_message(f"❌ Ошибка: {e.text}", ephemeral=True)
            return
        await inter.response.send_message(
            "🔒 Комната закрыта." if locked else "🔓 Комната открыта.", ephemeral=True
        )


def setup(bot: commands.Bot):
    bot.add_cog(VoiceCog(bot))
