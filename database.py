"""
Async SQLite persistence layer.

Two tables:
  users   - Telegram users and their approval status / timezone preference
  objects - Facebook Graph API objects being monitored (posts, pages, etc.)
"""
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT,
    username    TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / APPROVED / REJECTED / BLOCKED
    timezone    TEXT NOT NULL DEFAULT 'Asia/Dhaka',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    object_id       TEXT NOT NULL,     -- the Graph API object id we query
    url             TEXT,              -- original url/input the user gave us
    name            TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE / DEAD / STOPPED
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    die_alert_sent  INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_checked    INTEGER
);
"""


@dataclass
class TrackedObject:
    id: int
    chat_id: int
    object_id: str
    url: Optional[str]
    name: Optional[str]
    note: Optional[str]
    status: str
    is_hidden: bool
    die_alert_sent: bool
    created_at: int
    updated_at: int
    last_checked: Optional[int]

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "TrackedObject":
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            object_id=row["object_id"],
            url=row["url"],
            name=row["name"],
            note=row["note"],
            status=row["status"],
            is_hidden=bool(row["is_hidden"]),
            die_alert_sent=bool(row["die_alert_sent"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_checked=row["last_checked"],
        )


@dataclass
class TrackedUser:
    user_id: int
    first_name: Optional[str]
    username: Optional[str]
    status: str
    timezone: str
    created_at: int

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "TrackedUser":
        return cls(
            user_id=row["user_id"],
            first_name=row["first_name"],
            username=row["username"],
            status=row["status"],
            timezone=row["timezone"],
            created_at=row["created_at"],
        )


async def init_db() -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_or_create_user(user_id: int, first_name: str, username: Optional[str]) -> TrackedUser:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row:
            return TrackedUser.from_row(row)

        status = "APPROVED" if user_id == config.ADMIN_ID else "PENDING"
        await db.execute(
            "INSERT INTO users (user_id, first_name, username, status, timezone, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, first_name, username, status, config.DEFAULT_TIMEZONE, _now()),
        )
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return TrackedUser.from_row(row)


async def get_user(user_id: int) -> Optional[TrackedUser]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return TrackedUser.from_row(row) if row else None


async def set_user_status(user_id: int, status: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()


async def set_user_timezone(user_id: int, timezone: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (timezone, user_id))
        await db.commit()


async def list_pending_users() -> list[TrackedUser]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE status = 'PENDING' ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [TrackedUser.from_row(r) for r in rows]


async def list_all_users() -> list[TrackedUser]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [TrackedUser.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Objects (monitored Facebook items)
# ---------------------------------------------------------------------------

async def add_object(chat_id: int, object_id: str, url: str, name: str, note: Optional[str],
                      status: str = "ACTIVE") -> TrackedObject:
    now = _now()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "INSERT INTO objects (chat_id, object_id, url, name, note, status, is_hidden, "
            "die_alert_sent, created_at, updated_at, last_checked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
            (chat_id, object_id, url, name, note, status, now, now, now),
        )
        await db.commit()
        new_id = cur.lastrowid
        async with db.execute("SELECT * FROM objects WHERE id = ?", (new_id,)) as c2:
            row = await c2.fetchone()
        return TrackedObject.from_row(row)


async def get_object(obj_id: int) -> Optional[TrackedObject]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM objects WHERE id = ?", (obj_id,)) as cur:
            row = await cur.fetchone()
        return TrackedObject.from_row(row) if row else None


async def list_objects_by_chat(chat_id: int) -> list[TrackedObject]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM objects WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [TrackedObject.from_row(r) for r in rows]


async def list_active_objects() -> list[TrackedObject]:
    """All objects currently ACTIVE, across all chats - used by the background worker."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM objects WHERE status = 'ACTIVE'") as cur:
            rows = await cur.fetchall()
        return [TrackedObject.from_row(r) for r in rows]


async def update_object_status(obj_id: int, status: str, name: Optional[str] = None) -> None:
    now = _now()
    async with aiosqlite.connect(config.DB_PATH) as db:
        if name is not None:
            await db.execute(
                "UPDATE objects SET status = ?, name = ?, updated_at = ?, last_checked = ? WHERE id = ?",
                (status, name, now, now, obj_id),
            )
        else:
            await db.execute(
                "UPDATE objects SET status = ?, updated_at = ?, last_checked = ? WHERE id = ?",
                (status, now, now, obj_id),
            )
        await db.commit()


async def touch_last_checked(obj_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE objects SET last_checked = ? WHERE id = ?", (_now(), obj_id))
        await db.commit()


async def set_object_hidden(obj_id: int, hidden: bool) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE objects SET is_hidden = ? WHERE id = ?", (int(hidden), obj_id))
        await db.commit()


async def set_die_alert_sent(obj_id: int, sent: bool) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE objects SET die_alert_sent = ? WHERE id = ?", (int(sent), obj_id))
        await db.commit()


async def set_note(obj_id: int, note: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE objects SET note = ?, updated_at = ? WHERE id = ?", (note, _now(), obj_id)
        )
        await db.commit()


async def stop_object(obj_id: int) -> None:
    await update_object_status(obj_id, "STOPPED")


async def resume_object(obj_id: int) -> None:
    """Continue monitoring: back to ACTIVE, alert flag reset."""
    now = _now()
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE objects SET status = 'ACTIVE', die_alert_sent = 0, updated_at = ? WHERE id = ?",
            (now, obj_id),
        )
        await db.commit()


async def delete_object(obj_id: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM objects WHERE id = ?", (obj_id,))
        await db.commit()
