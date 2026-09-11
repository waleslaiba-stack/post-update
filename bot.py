"""
bot.py — Facebook Link Health Monitor Telegram Bot
python-telegram-bot v21 (async) + Playwright + aiosqlite
"""

from __future__ import annotations

import os
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import pytz
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest

import database as db
from checker import check_facebook_link, is_facebook_url, normalise_facebook_url

load_dotenv()

# Configuration
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "50"))  # seconds

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Timezone options
TIMEZONES: Dict[str, str] = {
    "Asia/Dhaka":     "🇧🇩 Bangladesh (UTC+6)",
    "Asia/Kolkata":   "🇮🇳 India (UTC+5:30)",
    "Asia/Riyadh":    "🇸🇦 Saudi Arabia (UTC+3)",
    "Asia/Dubai":     "🇦🇪 UAE (UTC+4)",
    "Europe/London":  "🇬🇧 UK (GMT/BST)",
    "America/New_York": "🇺🇸 US Eastern",
    "America/Los_Angeles": "🇺🇸 US Pacific",
    "UTC":            "🌐 UTC",
}

# Conversation states
EDIT_NAME, EDIT_NOTE = range(2)
BROADCAST_MSG = 10


# Helper: format time in user's timezone
def fmt_time(iso_str: Optional[str], tz_name: str = "Asia/Dhaka") -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
        tz = pytz.timezone(tz_name)
        local = dt.astimezone(tz)
        return local.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return iso_str


def calc_duration(created: Optional[str], updated: Optional[str]) -> str:
    if not created or not updated:
        return "N/A"
    try:
        c = datetime.fromisoformat(created)
        u = datetime.fromisoformat(updated)
        diff = u - c
        total_seconds = int(diff.total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        hours, rem = divmod(total_seconds, 3600)
        mins, secs = divmod(rem, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if mins:
            parts.append(f"{mins}m")
        parts.append(f"{secs}s")
        return " ".join(parts) if parts else "0s"
    except Exception:
        return "N/A"


# Access control decorator
def require_approved(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        record = await db.get_user(user.id)
        if not record:
            # Auto-register and notify admin
            await db.upsert_user(user.id, user.first_name, user.username)
            await notify_admin_new_user(context, user.id, user.first_name, user.username)
            await update.effective_message.reply_text(
                "⏳ **Access Pending**\n\n"
                "Your access request has been sent to the admin.\n"
                "You'll be notified once approved.",
                parse_mode=ParseMode.HTML,
            )
            return
        if record["status"] == "PENDING":
            await update.effective_message.reply_text(
                "⏳ **Still Pending**\n\nYour request is under review.",
                parse_mode=ParseMode.HTML,
            )
            return
        if record["status"] in ("REJECTED", "BLOCKED"):
            await update.effective_message.reply_text(
                "🚫 **Access Denied**\n\nYou are not authorised to use this bot.",
                parse_mode=ParseMode.HTML,
            )
            return
        return await handler(update, context)
    return wrapper


async def notify_admin_new_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    first_name: str,
    username: Optional[str],
) -> None:
    uname = f"@{username}" if username else "No username"
    text = (
        f"🔔 **New Access Request**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"📛 Name: **{first_name}**\n"
        f"🏷 Username: {uname}\n\n"
        f"Grant access?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve:{user_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"admin_reject:{user_id}"),
        ]
    ])
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        logger.error("Failed to notify admin: %s", e)


# Card builders
def active_card(link: Dict, tz_name: str = "Asia/Dhaka") -> str:
    return (
        f"🔹 **UID:** `{link['uid']}`\n"
        f"🟢 **Status:** ACTIVE\n"
        f"📌 **Name:** {_esc(link.get('name') or 'N/A')}\n"
        f"🔗 **URL:** {link['url']}\n"
        f"📝 **Note:** {_esc(link.get('note') or '—')}\n"
        f"📅 **Created:** {fmt_time(link.get('created_at'), tz_name)}\n"
        f"⏳ **Progress:** Monitoring, waiting for DIE 🔴"
    )


def dead_card(link: Dict, tz_name: str = "Asia/Dhaka", spoiler: bool = False) -> str:
    uid_val  = f"`{link['uid']}`"
    name_val = _esc(link.get("name") or "N/A")
    note_val = _esc(link.get("note") or "—")
    if spoiler:
        uid_val  = f"{uid_val}"
        name_val = f"{name_val}"
        note_val = f"{note_val}"
    duration = calc_duration(link.get("created_at"), link.get("updated_at"))
    return (
        f"⚠️ **LINK DIED!**\n\n"
        f"🔹 **UID:** {uid_val}\n"
        f"🔴 **Status:** DEAD\n"
        f"📌 **Name:** {name_val}\n"
        f"🔗 **URL:** {link['url']}\n"
        f"📝 **Note:** {note_val}\n"
        f"📅 **Created:** {fmt_time(link.get('created_at'), tz_name)}\n"
        f"⏱ **Updated:** {fmt_time(link.get('updated_at'), tz_name)}\n"
        f"⏳ **Processing Time:** {duration}"
    )


def stopped_card(link: Dict, tz_name: str = "Asia/Dhaka") -> str:
    return (
        f"🔹 **UID:** `{link['uid']}`\n"
        f"⏸ **Status:** STOPPED\n"
        f"📌 **Name:** {_esc(link.get('name') or 'N/A')}\n"
        f"🔗 **URL:** {link['url']}\n"
        f"📝 **Note:** {_esc(link.get('note') or '—')}\n"
        f"📅 **Created:** {fmt_time(link.get('created_at'), tz_name)}\n"
        f"⏱ **Updated:** {fmt_time(link.get('updated_at'), tz_name)}"
    )


def _esc(text: str) -> str:
    """Minimal HTML escape for display text."""
    return (
        str(text)
        .replace("&", "&")
        .replace("<", "<")
        .replace(">", ">")
    )


def active_card_buttons(link_uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{link_uid}"),
            InlineKeyboardButton("📋 List",    callback_data="list:1"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def dead_card_buttons(link_uid: str, is_hidden: bool) -> InlineKeyboardMarkup:
    hide_label = "👁 Show Info" if is_hidden else "🙈 Hide Info"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(hide_label, callback_data=f"toggle_hide:{link_uid}"),
        ],
        [
            InlineKeyboardButton("🔄 Continue", callback_data=f"continue:{link_uid}"),
            InlineKeyboardButton("🛑 Stop",     callback_data=f"stop:{link_uid}"),
        ],
    ])


# /start
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    record = await db.upsert_user(user.id, user.first_name, user.username)
    if record.get("status") == "PENDING":
        await notify_admin_new_user(context, user.id, user.first_name, user.username)
        await update.message.reply_text(
            "⏳ **Welcome!**\n\n"
            "Your access request has been sent to the admin.\n"
            "You'll be notified once approved.",
            parse_mode=ParseMode.HTML,
        )
        return
    if record.get("status") in ("REJECTED", "BLOCKED"):
        await update.message.reply_text("🚫 Access denied.", parse_mode=ParseMode.HTML)
        return
    await send_main_menu(update, context)


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    stats = await db.get_link_stats(user.id)
    text = (
        f"🤖 **FB Link Monitor**\n\n"
        f"👋 Hello, **{_esc(user.first_name)}**!\n\n"
        f"📊 **Your Stats:**\n"
        f"  🟢 Active:  **{stats['active']}**\n"
        f"  🔴 Dead:    **{stats['dead']}**\n"
        f"  ⏸ Stopped: **{stats['stopped']}**\n"
        f"  📁 Total:   **{stats['total']}**\n\n"
        f"🔗 **Send any Facebook URL to start monitoring!**\n\n"
        f"*Supported: Posts, Reels, Videos, Profiles, Groups, Share links*"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 My Links",    callback_data="list:1"),
            InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
        ],
        [InlineKeyboardButton("❓ Help",           callback_data="help")],
    ])
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        try:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass


# URL message handler — add new link
@require_approved
async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    urls = _extract_facebook_urls(text)
    if not urls:
        await update.message.reply_text(
            "⚠️ Please send a valid Facebook URL.\n\n"
            "Examples:\n"
            "• facebook.com/someuser\n"
            "• facebook.com/permalink/...\n"
            "• facebook.com/share/p/...\n"
            "• facebook.com/reel/..."
        )
        return

    for url in urls:
        await _process_single_url(update, context, url)


def _extract_facebook_urls(text: str) -> List[str]:
    """Extract all Facebook URLs from a message."""
    pattern = r"https?://(?:www\.|m\.|mbasic\.)?facebook\.com/\S+"
    found = re.findall(pattern, text)
    bare = re.findall(r"(?:www\.)?facebook\.com/\S+", text)
    results = []
    seen = set()
    for u in found + bare:
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            results.append(u)
    return results


async def _process_single_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
) -> None:
    user = update.effective_user
    url = normalise_facebook_url(url)
    processing_msg = await update.message.reply_text(
        f"🔍 **Checking link...**\n`{url[:80]}`",
        parse_mode=ParseMode.HTML,
    )
    result = await check_facebook_link(url)
    record = await db.get_user(user.id)
    tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"

    if result.status == "ACTIVE":
        link = await db.create_link(
            chat_id=user.id,
            url=url,
            name=result.name[:120] if result.name else "",
            note="",
            status="ACTIVE",
        )
        card_text = active_card(link, tz_name)
        kb = active_card_buttons(link["uid"])
        try:
            await processing_msg.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await update.message.reply_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif result.status == "DEAD":
        link = await db.create_link(
            chat_id=user.id,
            url=url,
            name=result.name[:120] if result.name else "",
            note="",
            status="DEAD",
        )
        card_text = dead_card(link, tz_name, spoiler=False)
        kb = dead_card_buttons(link["uid"], is_hidden=False)
        try:
            await processing_msg.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await update.message.reply_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    else:
        # ERROR — transient, save as ACTIVE (anti-glitch protection)
        link = await db.create_link(
            chat_id=user.id,
            url=url,
            name="",
            note="",
            status="ACTIVE",
        )
        card_text = (
            f"⚠️ **Verification inconclusive**\n\n"
            f"The link has been saved as **ACTIVE** for monitoring.\n"
            f"*Reason: {_esc(result.reason)}*\n\n"
        ) + active_card(link, tz_name)
        kb = active_card_buttons(link["uid"])
        try:
            await processing_msg.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await update.message.reply_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)


# /list command
@require_approved
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_link_list(update, context, page=1)


async def _send_link_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    edit: bool = False,
) -> None:
    user = update.effective_user
    if not user:
        return
    record = await db.get_user(user.id)
    tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
    links, total = await db.get_links_for_user(user.id, page=page, per_page=5)
    total_pages = max(1, (total + 4) // 5)

    if not links:
        text = "📭 **No links found.**\n\nSend a Facebook URL to start monitoring!"
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])
    else:
        lines = [f"📋 **My Links** — Page {page}/{total_pages} ({total} total)\n"]
        for lnk in links:
            status_icon = {"ACTIVE": "🟢", "DEAD": "🔴", "STOPPED": "⏸"}.get(lnk["status"], "⚪")
            name = _esc(lnk.get("name") or lnk["url"][:40])
            lines.append(
                f"{status_icon} `{lnk['uid']}` — {name}\n"
                f"    📅 {fmt_time(lnk.get('created_at'), tz_name)}"
            )
        text = "\n".join(lines)

        nav_row: List[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{page-1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list:{page+1}"))

        link_rows = []
        for lnk in links:
            link_rows.append([
                InlineKeyboardButton(f"🔍 {lnk['uid']}", callback_data=f"view:{lnk['uid']}"),
            ])

        kb_rows = link_rows
        if nav_row:
            kb_rows.append(nav_row)
        kb_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
        kb = InlineKeyboardMarkup(kb_rows)

    msg = None
    if update.callback_query:
        msg = update.callback_query.message
    elif update.message:
        msg = update.message

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
    elif msg:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# /status command
@require_approved
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /status ")
        return
    uid = args[0].upper()
    link = await db.get_link_by_uid(uid)
    if not link or link["chat_id"] != update.effective_user.id:
        await update.message.reply_text("❌ Link not found.")
        return
    record = await db.get_user(update.effective_user.id)
    tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
    if link["status"] == "ACTIVE":
        text = active_card(link, tz_name)
        kb = active_card_buttons(link["uid"])
    elif link["status"] == "DEAD":
        text = dead_card(link, tz_name)
        kb = dead_card_buttons(link["uid"], bool(link["is_hidden"]))
    else:
        text = stopped_card(link, tz_name)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# /delete command
@require_approved
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /delete ")
        return
    uid = args[0].upper()
    link = await db.get_link_by_uid(uid)
    if not link or link["chat_id"] != update.effective_user.id:
        await update.message.reply_text("❌ Link not found.")
        return
    await db.delete_link(link["id"])
    await update.message.reply_text(f"🗑 Link `{uid}` deleted.", parse_mode=ParseMode.HTML)


# Admin commands
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    text = (
        "👑 **Admin Panel**\n\n"
        "Commands:\n"
        "/block <user_id> — Block a user\n"
        "/unblock <user_id> — Unblock a user\n"
        "/users — List all users\n"
        "/broadcast — Broadcast a message\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="admin_users:1")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /block ")
        return
    try:
        uid = int(args[0])
        await db.set_user_status(uid, "BLOCKED")
        await update.message.reply_text(f"🚫 User `{uid}` blocked.", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("Invalid user ID.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /unblock ")
        return
    try:
        uid = int(args[0])
        await db.set_user_status(uid, "APPROVED")
        await update.message.reply_text(f"✅ User `{uid}` unblocked.", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(uid, "✅ Your access has been restored!")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("Invalid user ID.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await _send_user_list(update, context, page=1)


async def _send_user_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    edit: bool = False,
) -> None:
    users, total = await db.get_all_users(page=page, per_page=8)
    total_pages = max(1, (total + 7) // 8)
    STATUS_ICONS = {"APPROVED": "✅", "PENDING": "⏳", "REJECTED": "❌", "BLOCKED": "🚫"}
    lines = [f"👥 **All Users** — Page {page}/{total_pages}\n"]
    for u in users:
        icon = STATUS_ICONS.get(u["status"], "⚪")
        uname = f"@{u['username']}" if u.get("username") else "—"
        lines.append(f"{icon} `{u['user_id']}` | {_esc(u['first_name'])} ({uname})")
    text = "\n".join(lines)

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_users:{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_users:{page+1}"))

    kb_rows = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# /settings
@require_approved
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_settings(update, context)


async def _send_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    user = update.effective_user
    record = await db.get_user(user.id)
    tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
    tz_label = TIMEZONES.get(tz_name, tz_name)
    text = (
        f"⚙️ **Settings**\n\n"
        f"🕒 **Timezone:** {tz_label}\n\n"
        f"Select your timezone:"
    )
    kb_rows = []
    for tz_key, tz_display in TIMEZONES.items():
        active_mark = " ✅" if tz_key == tz_name else ""
        kb_rows.append([InlineKeyboardButton(tz_display + active_mark, callback_data=f"set_tz:{tz_key}")])
    kb_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(kb_rows)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# /help
@require_approved
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 **How to use FB Link Monitor**\n\n"
        "**Add a link:**\n"
        "Simply send any Facebook URL to start monitoring.\n\n"
        "**Commands:**\n"
        "/start — Main menu\n"
        "/list — View all your monitored links\n"
        "/status <UID> — Check a specific link\n"
        "/delete <UID> — Remove a link\n"
        "/settings — Change timezone\n"
        "/help — This message\n\n"
        "**Supported URL types:**\n"
        "• User profiles: facebook.com/username\n"
        "• Profile IDs: facebook.com/profile.php?id=...\n"
        "• Posts: facebook.com/.../posts/...\n"
        "• Reels: facebook.com/reel/...\n"
        "• Videos: facebook.com/watch?v=...\n"
        "• Share links: facebook.com/share/p/... or /share/v/...\n"
        "• Group posts: facebook.com/groups/.../permalink/...\n\n"
        "**Status icons:**\n"
        "• 🟢 ACTIVE — Link is live and being monitored\n"
        "• 🔴 DEAD — Link has been removed/deleted\n"
        "• ⏸ STOPPED — Monitoring paused\n\n"
        "**Dead alert buttons:**\n"
        "• 🙈 Hide Info — Mask UID/Name/Note with spoiler\n"
        "• 👁 Show Info — Reveal hidden info\n"
        "• 🔄 Continue — Resume monitoring (reset to ACTIVE)\n"
        "• 🛑 Stop — Stop monitoring this link"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# Callback query router
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = update.effective_user

    # Admin approval
    if data.startswith("admin_approve:"):
        target_id = int(data.split(":")[1])
        await db.set_user_status(target_id, "APPROVED")
        await query.edit_message_text(
            f"✅ User `{target_id}` has been **approved**.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                target_id,
                "🎉 **Access Granted!**\n\nYou can now use the bot. Send /start to begin.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if data.startswith("admin_reject:"):
        target_id = int(data.split(":")[1])
        await db.set_user_status(target_id, "REJECTED")
        await query.edit_message_text(
            f"❌ User `{target_id}` has been **rejected**.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                target_id,
                "🚫 **Access Denied.**\n\nYour request was not approved.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    # Admin user list
    if data.startswith("admin_users:"):
        if user.id != ADMIN_ID:
            return
        page = int(data.split(":")[1])
        await _send_user_list(update, context, page=page, edit=True)
        return

    # List navigation
    if data.startswith("list:"):
        page = int(data.split(":")[1])
        await _send_link_list(update, context, page=page, edit=True)
        return

    # Main menu
    if data == "main_menu":
        await send_main_menu(update, context)
        return

    # Help
    if data == "help":
        text = (
            "📖 **How to use FB Link Monitor**\n\n"
            "Send any Facebook URL to start monitoring.\n\n"
            "Use /help for full instructions."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Settings
    if data == "settings":
        await _send_settings(update, context, edit=True)
        return

    if data.startswith("set_tz:"):
        tz_key = data.split(":", 1)[1]
        if tz_key in TIMEZONES:
            await db.set_user_timezone(user.id, tz_key)
            await _send_settings(update, context, edit=True)
        return

    # View link detail
    if data.startswith("view:"):
        uid = data.split(":")[1]
        link = await db.get_link_by_uid(uid)
        if not link or link["chat_id"] != user.id:
            await query.answer("Link not found.", show_alert=True)
            return
        record = await db.get_user(user.id)
        tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
        if link["status"] == "ACTIVE":
            text = active_card(link, tz_name)
            kb = active_card_buttons(link["uid"])
        elif link["status"] == "DEAD":
            text = dead_card(link, tz_name, spoiler=bool(link["is_hidden"]))
            kb = dead_card_buttons(link["uid"], bool(link["is_hidden"]))
        else:
            text = stopped_card(link, tz_name)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Resume", callback_data=f"continue:{uid}")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ])
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        return

    # Edit link
    if data.startswith("edit:"):
        uid = data.split(":")[1]
        link = await db.get_link_by_uid(uid)
        if not link or link["chat_id"] != user.id:
            await query.answer("Link not found.", show_alert=True)
            return
        context.user_data["editing_uid"] = uid
        text = (
            f"✏️ **Edit Link** `{uid}`\n\n"
            f"Current Name: {_esc(link.get('name') or '—')}\n"
            f"Current Note: {_esc(link.get('note') or '—')}\n\n"
            f"Reply with: `Name | Note`\n"
            f"Example: `My Post | Birthday video`\n\n"
            f"Send /cancel to abort."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"view:{uid}")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # Toggle hide/show
    if data.startswith("toggle_hide:"):
        uid = data.split(":")[1]
        link = await db.get_link_by_uid(uid)
        if not link or link["chat_id"] != user.id:
            await query.answer("Link not found.", show_alert=True)
            return
        new_hidden = not bool(link["is_hidden"])
        await db.set_link_hidden(link["id"], new_hidden)
        link = await db.get_link_by_uid(uid)
        record = await db.get_user(user.id)
        tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
        text = dead_card(link, tz_name, spoiler=new_hidden)
        kb   = dead_card_buttons(uid, new_hidden)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        return

    # Continue monitoring
    if data.startswith("continue:"):
        uid = data.split(":")[1]
        link = await db.get_link_by_uid(uid)
        if not link or link["chat_id"] != user.id:
            await query.answer("Link not found.", show_alert=True)
            return
        await db.update_link_status(link["id"], "ACTIVE", die_alert_sent=0)
        link = await db.get_link_by_uid(uid)
        record = await db.get_user(user.id)
        tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
        text = active_card(link, tz_name)
        kb = active_card_buttons(uid)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        await query.answer("🔄 Monitoring resumed!", show_alert=False)
        return

    # Stop monitoring
    if data.startswith("stop:"):
        uid = data.split(":")[1]
        link = await db.get_link_by_uid(uid)
        if not link or link["chat_id"] != user.id:
            await query.answer("Link not found.", show_alert=True)
            return
        await db.update_link_status(link["id"], "STOPPED")
        link = await db.get_link_by_uid(uid)
        record = await db.get_user(user.id)
        tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
        text = stopped_card(link, tz_name)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Resume", callback_data=f"continue:{uid}")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ])
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        await query.answer("🛑 Monitoring stopped.", show_alert=False)
        return


# Edit handler (inline text editing via user_data state)
@require_approved
async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = context.user_data.get("editing_uid")
    if not uid:
        return
    text = (update.message.text or "").strip()
    if text.lower() == "/cancel":
        context.user_data.pop("editing_uid", None)
        await update.message.reply_text("❌ Edit cancelled.")
        return

    parts = text.split("|", 1)
    name = parts[0].strip()[:120]
    note = parts[1].strip()[:500] if len(parts) > 1 else ""

    link = await db.get_link_by_uid(uid)
    if not link or link["chat_id"] != update.effective_user.id:
        context.user_data.pop("editing_uid", None)
        await update.message.reply_text("❌ Link not found.")
        return

    await db.update_link_meta(link["id"], name, note)
    context.user_data.pop("editing_uid", None)
    link = await db.get_link_by_uid(uid)
    record = await db.get_user(update.effective_user.id)
    tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
    text_card = active_card(link, tz_name) if link["status"] == "ACTIVE" else dead_card(link, tz_name)
    kb = active_card_buttons(uid) if link["status"] == "ACTIVE" else dead_card_buttons(uid, bool(link["is_hidden"]))
    await update.message.reply_text(
        f"✅ Updated!\n\n{text_card}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# Background scanner (runs every CHECK_INTERVAL seconds)
async def background_scanner(app: Application) -> None:
    """Continuously scan all active links and fire die alerts."""
    logger.info("Background scanner started (interval=%ds)", CHECK_INTERVAL)
    await asyncio.sleep(10)  # Warm-up delay
    while True:
        try:
            active_links = await db.get_all_active_links()
            logger.info("Scanner: checking %d active link(s)", len(active_links))
            for link in active_links:
                try:
                    result = await check_facebook_link(link["url"])
                    if result.status == "DEAD":
                        # Mark as DEAD and send alert (if not already sent)
                        await db.update_link_status(link["id"], "DEAD", die_alert_sent=1)
                        refreshed = await db.get_link_by_uid(link["uid"])
                        if refreshed and not link.get("die_alert_sent"):
                            await _send_die_alert(app, refreshed)
                    elif result.status == "ACTIVE":
                        # Still alive — just update last_checked
                        await db.update_link_last_checked(link["id"])
                    # ERROR — anti-glitch: do nothing (link stays ACTIVE)
                except Exception as exc:
                    logger.exception("Scanner error for link %s: %s", link.get("uid"), exc)
                # Small delay between checks to avoid hammering
                await asyncio.sleep(2)
        except Exception as exc:
            logger.exception("Scanner loop error: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL)


async def _send_die_alert(app: Application, link: Dict) -> None:
    """Send a die-alert message to the link's owner."""
    chat_id = link["chat_id"]
    try:
        record = await db.get_user(chat_id)
        tz_name = record.get("timezone", "Asia/Dhaka") if record else "Asia/Dhaka"
        text = (
            f"🚨 **LINK DIED!**\n\n"
            + dead_card(link, tz_name, spoiler=bool(link.get("is_hidden", False)))
        )
        kb = dead_card_buttons(link["uid"], bool(link.get("is_hidden", False)))
        await app.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        logger.info("Die alert sent for UID=%s to chat_id=%s", link["uid"], chat_id)
    except TelegramError as exc:
        logger.error("Failed to send die alert for UID=%s: %s", link.get("uid"), exc)


# Post-init: register commands, start scanner
async def post_init(app: Application) -> None:
    await db.init_db()
    commands = [
        BotCommand("start",    "Main menu"),
        BotCommand("list",     "List your monitored links"),
        BotCommand("status",   "Check a link by UID"),
        BotCommand("delete",   "Remove a link by UID"),
        BotCommand("settings", "Change timezone"),
        BotCommand("help",     "Show help"),
    ]
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    # Launch background scanner as a non-blocking task
    asyncio.create_task(background_scanner(app))
    logger.info("Bot initialised successfully.")


# Main entry point
def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("delete",   cmd_delete))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CommandHandler("block",    cmd_block))
    app.add_handler(CommandHandler("unblock",  cmd_unblock))
    app.add_handler(CommandHandler("users",    cmd_users))

    # Callback handler
    app.add_handler(CallbackQueryHandler(handle_callback))

    # URL message handler
    url_filter = filters.TEXT & filters.Regex(r"facebook\.com")
    app.add_handler(MessageHandler(url_filter, handle_url_message))

    # Edit input handler (name|note via text)
    edit_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"facebook\.com")
    app.add_handler(MessageHandler(edit_filter, handle_edit_input))

    logger.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
