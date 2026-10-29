"""Слой работы с базой данных SQLite (aiosqlite)."""
from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id            INTEGER PRIMARY KEY,
    hub_channel_id      INTEGER,
    temp_category_id    INTEGER,
    log_channel_id      INTEGER,
    control_channel_id  INTEGER
);

CREATE TABLE IF NOT EXISTS temp_rooms (
    channel_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    owner_id    INTEGER NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    feed_url    TEXT NOT NULL,
    channel_id  INTEGER,
    UNIQUE (guild_id, feed_url)
);

-- Защита от дубликатов: ключ = "<source_id>:<guid записи>"
CREATE TABLE IF NOT EXISTS seen_news (
    key        TEXT PRIMARY KEY,
    source_id  INTEGER NOT NULL,
    seen_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Поля, которые разрешено менять через update_guild_settings (защита от SQL-инъекций по именам колонок)
SETTINGS_FIELDS = {"hub_channel_id", "temp_category_id", "log_channel_id", "control_channel_id"}


class Database:
    """Одно соединение на всё приложение; операции выполняются последовательно через aiosqlite."""

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---------- низкоуровневые помощники ----------

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        async with self._conn.execute(sql, params) as cur:
            last_id = cur.lastrowid
        await self._conn.commit()
        return last_id

    # ---------- настройки сервера ----------

    async def get_guild_settings(self, guild_id: int) -> dict | None:
        return await self._fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))

    async def update_guild_settings(self, guild_id: int, **fields: int) -> None:
        unknown = set(fields) - SETTINGS_FIELDS
        if unknown:
            raise ValueError(f"Недопустимые поля настроек: {unknown}")
        await self._execute(
            "INSERT INTO guild_settings (guild_id) VALUES (?) ON CONFLICT(guild_id) DO NOTHING",
            (guild_id,),
        )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            await self._execute(
                f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
                (*fields.values(), guild_id),
            )

    # ---------- временные комнаты (Join-to-Create) ----------

    async def add_temp_room(self, channel_id: int, guild_id: int, owner_id: int) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO temp_rooms (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
            (channel_id, guild_id, owner_id),
        )

    async def remove_temp_room(self, channel_id: int) -> None:
        await self._execute("DELETE FROM temp_rooms WHERE channel_id = ?", (channel_id,))

    async def is_temp_room(self, channel_id: int) -> bool:
        return await self._fetchone(
            "SELECT 1 FROM temp_rooms WHERE channel_id = ?", (channel_id,)
        ) is not None

    async def get_temp_owner(self, channel_id: int) -> int | None:
        row = await self._fetchone("SELECT owner_id FROM temp_rooms WHERE channel_id = ?", (channel_id,))
        return row["owner_id"] if row else None

    async def find_owned_room(self, guild_id: int, owner_id: int) -> int | None:
        row = await self._fetchone(
            "SELECT channel_id FROM temp_rooms WHERE guild_id = ? AND owner_id = ?",
            (guild_id, owner_id),
        )
        return row["channel_id"] if row else None

    async def list_temp_rooms(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM temp_rooms")

    # ---------- источники новостей ----------

    async def add_source(self, guild_id: int, name: str, feed_url: str, channel_id: int | None) -> int | None:
        """Возвращает id нового источника или None, если такая лента уже есть."""
        try:
            return await self._execute(
                "INSERT INTO news_sources (guild_id, name, feed_url, channel_id) VALUES (?, ?, ?, ?)",
                (guild_id, name, feed_url, channel_id),
            )
        except aiosqlite.IntegrityError:
            return None

    async def remove_source(self, source_id: int) -> None:
        await self._execute("DELETE FROM seen_news WHERE source_id = ?", (source_id,))
        await self._execute("DELETE FROM news_sources WHERE id = ?", (source_id,))

    async def list_sources(self, guild_id: int | None = None) -> list[dict]:
        if guild_id is None:
            return await self._fetchall("SELECT * FROM news_sources ORDER BY id")
        return await self._fetchall(
            "SELECT * FROM news_sources WHERE guild_id = ? ORDER BY id", (guild_id,)
        )

    async def set_source_channel(self, source_id: int, channel_id: int) -> None:
        await self._execute("UPDATE news_sources SET channel_id = ? WHERE id = ?", (channel_id, source_id))

    async def source_has_seen(self, source_id: int) -> bool:
        return await self._fetchone(
            "SELECT 1 FROM seen_news WHERE source_id = ? LIMIT 1", (source_id,)
        ) is not None

    async def is_seen(self, key: str) -> bool:
        return await self._fetchone("SELECT 1 FROM seen_news WHERE key = ?", (key,)) is not None

    async def mark_seen(self, key: str, source_id: int) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO seen_news (key, source_id) VALUES (?, ?)", (key, source_id)
        )
