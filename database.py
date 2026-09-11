"""
database.py — Async SQLite CRUD layer via aiosqlite
"""

from __future__ import annotations

import os
import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = os.getenv("DB_PATH", "./data/fb_monitor.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT    NOT NULL DEFAULT '',
    username    TEXT    DEFAULT NULL,
    status      TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED | BLOCKED
    timezone    TEXT    NOT NULL DEFAULT 'Asia/Dhaka',
    created_at  TEXT    NOT NULL
);
"""

CREATE_LINKS_SQL = """
CREATE TABLE IF NOT EXISTS links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    uid             TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | DEAD | STOPPED
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    die_alert_sent  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_checked    TEXT    DEFAULT NULL
);
"""

CREATE_IDX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_links_chat_id ON links(chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_links_status  ON links(status)",
    "CREATE INDEX IF NOT EXISTS idx_links_uid     ON links(uid)",
]


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

async def get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_LINKS_SQL)
        for idx_sql in CREATE_IDX_SQL:
            await db.execute(idx_sql)
        await db.commit()


def _now() -> str:
    return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------------------
# User operations
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, first_name: str, username: Optional[str]) -> Dict[str, Any]:
    """Insert or ignore user record. Returns the row."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now = _now()
        await db.execute(
            """
            INSERT INTO users (user_id, first_name, username, status, created_at)
            VALUES (?, ?, ?, 'PENDING', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username   = excluded.username
            """,
            (user_id, first_name, username, now),
        )
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_user_status(user_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()


async def set_user_timezone(user_id: int, timezone: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (timezone, user_id))
        await db.commit()


async def get_all_users(page: int = 1, per_page: int = 10) -> tuple[List[Dict], int]:
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return rows, total


async def get_approved_users() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE status = 'APPROVED'") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Link operations
# ---------------------------------------------------------------------------

def _gen_uid() -> str:
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


async def create_link(
    chat_id: int,
    url: str,
    name: str = "",
    note: str = "",
    status: str = "ACTIVE",
) -> Dict[str, Any]:
    now = _now()
    uid = _gen_uid()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        while True:
            async with db.execute("SELECT id FROM links WHERE uid = ?", (uid,)) as cur:
                if not await cur.fetchone():
                    break
            uid = _gen_uid()

        await db.execute(
            """
            INSERT INTO links (chat_id, uid, url, name, note, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, uid, url, name, note, status, now, now),
        )
        await db.commit()
        async with db.execute("SELECT * FROM links WHERE uid = ?", (uid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_link_by_id(link_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM links WHERE id = ?", (link_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_link_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM links WHERE uid = ?", (uid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_links_for_user(
    chat_id: int,
    status_filter: Optional[str] = None,
    page: int = 1,
    per_page: int = 5,
) -> tuple[List[Dict], int]:
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_filter:
            count_q = "SELECT COUNT(*) FROM links WHERE chat_id = ? AND status = ?"
            fetch_q = "SELECT * FROM links WHERE chat_id = ? AND status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
            async with db.execute(count_q, (chat_id, status_filter)) as cur:
                total = (await cur.fetchone())[0]
            async with db.execute(fetch_q, (chat_id, status_filter, per_page, offset)) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        else:
            count_q = "SELECT COUNT(*) FROM links WHERE chat_id = ?"
            fetch_q = "SELECT * FROM links WHERE chat_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
            async with db.execute(count_q, (chat_id,)) as cur:
                total = (await cur.fetchone())[0]
            async with db.execute(fetch_q, (chat_id, per_page, offset)) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        return rows, total


async def get_all_active_links() -> List[Dict[str, Any]]:
    """Used by the background scanner."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM links WHERE status = 'ACTIVE' ORDER BY last_checked ASC NULLS FIRST"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_link_status(
    link_id: int,
    status: str,
    die_alert_sent: Optional[int] = None,
) -> None:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        if die_alert_sent is not None:
            await db.execute(
                "UPDATE links SET status = ?, die_alert_sent = ?, updated_at = ?, last_checked = ? WHERE id = ?",
                (status, die_alert_sent, now, now, link_id),
            )
        else:
            await db.execute(
                "UPDATE links SET status = ?, updated_at = ?, last_checked = ? WHERE id = ?",
                (status, now, now, link_id),
            )
        await db.commit()


async def update_link_last_checked(link_id: int) -> None:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE links SET last_checked = ? WHERE id = ?",
            (now, link_id),
        )
        await db.commit()


async def update_link_meta(link_id: int, name: str, note: str) -> None:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE links SET name = ?, note = ?, updated_at = ? WHERE id = ?",
            (name, note, now, link_id),
        )
        await db.commit()


async def set_link_hidden(link_id: int, is_hidden: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE links SET is_hidden = ? WHERE id = ?",
            (1 if is_hidden else 0, link_id),
        )
        await db.commit()


async def delete_link(link_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM links WHERE id = ?", (link_id,))
        await db.commit()


async def get_link_stats(chat_id: int) -> Dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                SUM(CASE WHEN status='ACTIVE'  THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN status='DEAD'    THEN 1 ELSE 0 END) as dead,
                SUM(CASE WHEN status='STOPPED' THEN 1 ELSE 0 END) as stopped,
                COUNT(*) as total
            FROM links WHERE chat_id = ?
            """,
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "active":  row["active"]  or 0,
                    "dead":    row["dead"]    or 0,
                    "stopped": row["stopped"] or 0,
                    "total":   row["total"]   or 0,
                }
            return {"active": 0, "dead": 0, "stopped": 0, "total": 0}
