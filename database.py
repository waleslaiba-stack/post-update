"""
Database module for Facebook Link Monitor Telegram Bot.
Includes User Access Management (Approved, Pending, Rejected) & Timezone settings.
"""
import aiosqlite
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    status TEXT DEFAULT 'PENDING',  -- 'APPROVED', 'PENDING', 'REJECTED'
    timezone TEXT DEFAULT 'Asia/Dhaka',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitored_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    uid TEXT NOT NULL,
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT DEFAULT 'None',
    status TEXT DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checked_at TEXT,
    die_alert_sent INTEGER DEFAULT 0,
    is_hidden INTEGER DEFAULT 0,
    UNIQUE(chat_id, url)
);
"""

class Database:
    def __init__(self, db_path: str = "monitor.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(CREATE_TABLES_SQL)
            await db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    # ================= User Management =================
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def add_or_update_request(self, user_id: int, first_name: str, username: Optional[str]) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
            existing = await cursor.fetchone()
            now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

            if existing:
                return existing["status"]

            await db.execute(
                """
                INSERT INTO users (user_id, first_name, username, status, timezone, created_at)
                VALUES (?, ?, ?, 'PENDING', 'Asia/Dhaka', ?)
                """,
                (user_id, first_name, username or "", now)
            )
            await db.commit()
            return "PENDING"

    async def set_user_status(self, user_id: int, status: str, first_name: str = "", username: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            exists = await cursor.fetchone()
            now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            if exists:
                await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
            else:
                await db.execute(
                    "INSERT INTO users (user_id, first_name, username, status, timezone, created_at) VALUES (?, ?, ?, ?, 'Asia/Dhaka', ?)",
                    (user_id, first_name, username, status, now)
                )
            await db.commit()

    async def set_user_timezone(self, user_id: int, tz_str: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz_str, user_id))
            await db.commit()

    async def get_all_users(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                cursor = await db.execute("SELECT * FROM users WHERE status = ? ORDER BY user_id DESC", (status,))
            else:
                cursor = await db.execute("SELECT * FROM users ORDER BY user_id DESC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ================= Links Management =================
    async def add_link(self, chat_id: int, uid: str, url: str, name: str, note: str = "None") -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM monitored_links WHERE chat_id = ? AND url = ?", (chat_id, url))
            existing = await cursor.fetchone()
            if existing:
                await db.execute(
                    "UPDATE monitored_links SET status = 'ACTIVE', name = ?, updated_at = ?, die_alert_sent = 0 WHERE id = ?",
                    (name, now, existing["id"]),
                )
                await db.commit()
                c2 = await db.execute("SELECT * FROM monitored_links WHERE id = ?", (existing["id"],))
                row = await c2.fetchone()
                return dict(row) if row else None

            cursor = await db.execute(
                """
                INSERT INTO monitored_links (chat_id, uid, url, name, note, status, created_at, updated_at, die_alert_sent, is_hidden)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, 0, 0)
                """,
                (chat_id, uid, url, name, note, now, now),
            )
            await db.commit()
            new_id = cursor.lastrowid
            c2 = await db.execute("SELECT * FROM monitored_links WHERE id = ?", (new_id,))
            row = await c2.fetchone()
            return dict(row) if row else None

    async def get_active_links(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM monitored_links WHERE status = 'ACTIVE'")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_links_by_chat_id(self, chat_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM monitored_links WHERE chat_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (chat_id, limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_link_by_id(self, link_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM monitored_links WHERE id = ?", (link_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_link_by_uid(self, chat_id: int, uid: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM monitored_links WHERE chat_id = ? AND uid = ?", (chat_id, uid))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_status(self, link_id: int, status: str, die_alert_sent: Optional[int] = None) -> bool:
        now = datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            if die_alert_sent is not None:
                await db.execute(
                    "UPDATE monitored_links SET status = ?, updated_at = ?, die_alert_sent = ? WHERE id = ?",
                    (status, now, die_alert_sent, link_id),
                )
            else:
                await db.execute("UPDATE monitored_links SET status = ?, updated_at = ? WHERE id = ?", (status, now, link_id))
            await db.commit()
            return True

    async def update_last_checked(self, link_id: int) -> None:
        now = datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE monitored_links SET last_checked_at = ? WHERE id = ?", (now, link_id))
            await db.commit()

    async def update_note(self, link_id: int, note: str) -> bool:
        now = datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE monitored_links SET note = ?, updated_at = ? WHERE id = ?", (note, now, link_id))
            await db.commit()
            return True

    async def toggle_hidden(self, link_id: int, target_state: Optional[int] = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            if target_state is None:
                cursor = await db.execute("SELECT is_hidden FROM monitored_links WHERE id = ?", (link_id,))
                row = await cursor.fetchone()
                current = row[0] if row else 0
                target_state = 0 if current == 1 else 1
            await db.execute("UPDATE monitored_links SET is_hidden = ? WHERE id = ?", (target_state, link_id))
            await db.commit()
            return target_state

    async def delete_link(self, link_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM monitored_links WHERE id = ?", (link_id,))
            await db.commit()
            return True

    async def get_stats(self, chat_id: Optional[int] = None) -> Dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            if chat_id is not None:
                cursor = await db.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) as active,
                        SUM(CASE WHEN status = 'DEAD' THEN 1 ELSE 0 END) as dead,
                        SUM(CASE WHEN status = 'STOPPED' THEN 1 ELSE 0 END) as stopped
                    FROM monitored_links WHERE chat_id = ?
                    """,
                    (chat_id,),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) as active,
                        SUM(CASE WHEN status = 'DEAD' THEN 1 ELSE 0 END) as dead,
                        SUM(CASE WHEN status = 'STOPPED' THEN 1 ELSE 0 END) as stopped
                    FROM monitored_links
                    """
                )
            row = await cursor.fetchone()
            if row:
                return {
                    "total": row[0] or 0,
                    "active": row[1] or 0,
                    "dead": row[2] or 0,
                    "stopped": row[3] or 0,
                }
            return {"total": 0, "active": 0, "dead": 0, "stopped": 0}
