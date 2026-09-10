"""
Facebook Content Health Monitor - Telegram Bot

Monitors your OWN Facebook objects (pages, posts, videos...) via the official
Graph API and alerts you the moment one becomes inaccessible. See README.md
for setup (you must supply your own long-lived FB_ACCESS_TOKEN).
"""
import asyncio
import logging
from datetime import datetime
from html import escape as h
from typing import Optional

import aiohttp
import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database as db
import graph_checker

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("fb-monitor-bot")

# Per-chat state for the tiny "waiting for a note" conversation step.
# {chat_id: object_id_awaiting_note}
_AWAITING_NOTE: dict[int, int] = {}

MAIN_MENU_TEXT = (
    "🤖 <b>Facebook Content Health Monitor</b>\n\n"
    "Send me a Facebook URL, page username, or numeric object ID and I'll "
    "start monitoring it for you. I'll alert you the moment it becomes "
    "inaccessible.\n\n"
    '👑 Owner: <a href="https://t.me/tmmusa73">—͞Tᴍ Mᴜsᴀ ⚡</a>'
)


# ---------------------------------------------------------------------------
# Helpers: access control, time formatting
# ---------------------------------------------------------------------------

async def _ensure_approved(update: Update) -> Optional["db.TrackedUser"]:
    """Registers the user if new, and returns them if approved, else None
    (after sending the appropriate pending/rejected message)."""
    tg_user = update.effective_user
    user = await db.get_or_create_user(tg_user.id, tg_user.first_name or "", tg_user.username)

    if user.status == "APPROVED":
        return user

    if user.status == "PENDING":
        await update.effective_message.reply_text(
            "⏳ Your access request is pending admin approval. You'll be notified once approved."
        )
        await _notify_admin_new_user(update, tg_user.id)
        return None

    await update.effective_message.reply_text("🚫 You do not have access to this bot.")
    return None


async def _notify_admin_new_user(update: Update, user_id: int) -> None:
    """Sends (or re-sends) the approve/reject prompt to the admin for a pending user."""
    tg_user = update.effective_user
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 Approve", callback_data=f"admin_approve:{user_id}"),
            InlineKeyboardButton("🔴 Reject", callback_data=f"admin_reject:{user_id}"),
        ]]
    )
    text = (
        f"🆕 <b>New access request</b>\n"
        f"Name: {h(tg_user.first_name or '')}\n"
        f"Username: @{h(tg_user.username) if tg_user.username else 'N/A'}\n"
        f"User ID: <code>{user_id}</code>"
    )
    try:
        await update.get_bot().send_message(
            config.ADMIN_ID, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
    except Exception:
        logger.exception("Failed to notify admin of new user %s", user_id)


def _fmt_time(ts: Optional[int], tz_name: str) -> str:
    if not ts:
        return "N/A"
    tz = pytz.timezone(tz_name)
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _spoiler(text: str, hidden: bool) -> str:
    text = h(text)
    return f"<tg-spoiler>{text}</tg-spoiler>" if hidden else text


def render_active_card(obj: "db.TrackedObject", tz_name: str) -> str:
    name = _spoiler(obj.name or "Unknown", obj.is_hidden)
    note = _spoiler(obj.note or "None", obj.is_hidden)
    uid = _spoiler(obj.object_id, obj.is_hidden)
    link = h(obj.url or obj.object_id)
    return (
        f"🔔 UID: {uid} - <a href=\"{link}\">Open</a>\n"
        f"🟢 Status: ACTIVE ✅\n"
        f"👤 Name: {name}\n"
        f"📝 Note: {note}\n"
        f"⏱️ Created Time: {_fmt_time(obj.created_at, tz_name)}\n"
        f"🔄 Progress: Monitoring, waiting for DIE ❌"
    )


def render_dead_card(obj: "db.TrackedObject", tz_name: str) -> str:
    name = _spoiler(obj.name or "Unknown", obj.is_hidden)
    note = _spoiler(obj.note or "None", obj.is_hidden)
    uid = _spoiler(obj.object_id, obj.is_hidden)
    elapsed = _fmt_duration((obj.updated_at or obj.created_at) - obj.created_at)
    return (
        f"🔔 UID: {uid}\n"
        f"🔴 Status: DEAD ❌\n"
        f"👤 Name: {name}\n"
        f"📝 Note: {note}\n"
        f"⏱️ Created: {_fmt_time(obj.created_at, tz_name)}\n"
        f"⏰ Updated: {_fmt_time(obj.updated_at, tz_name)}\n"
        f"⏳ Processing Time: {elapsed}"
    )


def active_card_keyboard(obj_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{obj_id}"),
                InlineKeyboardButton("📋 List", callback_data="list:0"),
            ],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")],
        ]
    )


def dead_card_keyboard(obj_id: int, hidden: bool) -> InlineKeyboardMarkup:
    hide_btn = (
        InlineKeyboardButton("🐵 Show Info", callback_data=f"show:{obj_id}")
        if hidden
        else InlineKeyboardButton("🙈 Hide Info", callback_data=f"hide:{obj_id}")
    )
    return InlineKeyboardMarkup(
        [
            [hide_btn],
            [
                InlineKeyboardButton("🟢 Continue", callback_data=f"continue:{obj_id}"),
                InlineKeyboardButton("🔴 Stop", callback_data=f"stop:{obj_id}"),
            ],
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 My Links", callback_data="list:0")],
            [InlineKeyboardButton("🌐 Timezone", callback_data="tz_menu")],
        ]
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _ensure_approved(update)
    if not user:
        return
    await update.effective_message.reply_text(
        MAIN_MENU_TEXT, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != config.ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /block <user_id>")
        return
    target = int(context.args[0])
    await db.set_user_status(target, "REJECTED")
    await update.effective_message.reply_text(f"🔴 User {target} blocked.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != config.ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unblock <user_id>")
        return
    target = int(context.args[0])
    await db.set_user_status(target, "APPROVED")
    await update.effective_message.reply_text(f"🟢 User {target} unblocked/approved.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != config.ADMIN_ID:
        return
    users = await db.list_all_users()
    if not users:
        await update.effective_message.reply_text("No users yet.")
        return
    lines = [
        f"{'🟢' if u.status == 'APPROVED' else '🟡' if u.status == 'PENDING' else '🔴'} "
        f"<code>{u.user_id}</code> @{h(u.username) if u.username else '-'} ({h(u.status)})"
        for u in users
    ]
    await update.effective_message.reply_text(
        "<b>Users</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


# ---------------------------------------------------------------------------
# Adding a new link (plain text message = "add this")
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _ensure_approved(update)
    if not user:
        return

    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip()

    # If we're waiting for a note for a specific object, treat this as that.
    if chat_id in _AWAITING_NOTE:
        obj_id = _AWAITING_NOTE.pop(chat_id)
        note = None if text.lower() in ("none", "-", "clear") else text
        await db.set_note(obj_id, note or "None")
        obj = await db.get_object(obj_id)
        await _send_card_for(update.effective_message, obj, user.timezone)
        return

    urls = [t for t in text.split() if t]
    if not urls:
        return

    for raw in urls:
        await _add_and_report(update, context, raw, user.timezone)


async def _add_and_report(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str, tz_name: str) -> None:
    object_id = graph_checker.extract_object_id(raw)
    status_msg = await update.effective_message.reply_text(f"🔎 Checking {h(object_id)} ...", parse_mode=ParseMode.HTML)

    async with aiohttp.ClientSession() as session:
        result = await graph_checker.check_object(session, object_id)

    if result.is_auth_error:
        await status_msg.edit_text(
            "⚠️ Facebook rejected our access token while checking this object "
            f"(<code>{h(result.detail or 'auth error')}</code>). "
            "Ask the bot owner to refresh FB_ACCESS_TOKEN.",
            parse_mode=ParseMode.HTML,
        )
        return

    if result.status == graph_checker.STATUS_UNKNOWN:
        # Still save it as ACTIVE so the background worker keeps retrying -
        # a single transient failure shouldn't block adding a link.
        result_status = "ACTIVE"
        name = object_id
    elif result.status == graph_checker.STATUS_DEAD:
        result_status = "DEAD"
        name = result.name or object_id
    else:
        result_status = "ACTIVE"
        name = result.name or object_id

    obj = await db.add_object(
        chat_id=update.effective_chat.id,
        object_id=object_id,
        url=raw,
        name=name,
        note=None,
        status=result_status,
    )
    await status_msg.delete()
    await _send_card_for(update.effective_message, obj, tz_name)


async def _send_card_for(message, obj: "db.TrackedObject", tz_name: str) -> None:
    if obj.status == "DEAD":
        await message.reply_text(
            render_dead_card(obj, tz_name),
            reply_markup=dead_card_keyboard(obj.id, obj.is_hidden),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text(
            render_active_card(obj, tz_name),
            reply_markup=active_card_keyboard(obj.id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# Listing / pagination
# ---------------------------------------------------------------------------

PAGE_SIZE = 5


async def _render_list(chat_id: int, page: int, tz_name: str) -> tuple[str, InlineKeyboardMarkup]:
    objs = await db.list_objects_by_chat(chat_id)
    if not objs:
        return "📭 You aren't monitoring any links yet. Send me a Facebook URL to get started.", main_menu_keyboard()

    total_pages = max(1, (len(objs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = objs[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    icon = {"ACTIVE": "🟢", "DEAD": "🔴", "STOPPED": "⏹️"}
    lines = [f"📋 <b>Your Links</b> (page {page + 1}/{total_pages})\n"]
    for o in chunk:
        lines.append(f"{icon.get(o.status, '⚪')} #{o.id} — {h(o.name or o.object_id)} ({o.status})")

    buttons = [[InlineKeyboardButton(f"#{o.id} details", callback_data=f"view:{o.id}")] for o in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Callback query handler (all inline buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    tg_user = update.effective_user
    user = await db.get_user(tg_user.id)

    # Admin-only actions work even if the admin's own status lookup is odd.
    if data.startswith("admin_approve:") or data.startswith("admin_reject:"):
        if tg_user.id != config.ADMIN_ID:
            return
        target_id = int(data.split(":", 1)[1])
        if data.startswith("admin_approve:"):
            await db.set_user_status(target_id, "APPROVED")
            await query.edit_message_text("🟢 User approved.")
            try:
                await context.bot.send_message(target_id, "✅ You've been approved! Send /start to begin.")
            except Exception:
                pass
        else:
            await db.set_user_status(target_id, "REJECTED")
            await query.edit_message_text("🔴 User rejected.")
        return

    if not user or user.status != "APPROVED":
        await query.edit_message_text("🚫 You do not have access to this bot.")
        return

    tz_name = user.timezone

    if data == "menu":
        await query.edit_message_text(
            MAIN_MENU_TEXT, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data.startswith("list:"):
        page = int(data.split(":", 1)[1])
        text, kb = await _render_list(update.effective_chat.id, page, tz_name)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "tz_menu":
        rows = [
            [InlineKeyboardButton(label, callback_data=f"tz_set:{tzname}")]
            for label, tzname in config.SUPPORTED_TIMEZONES.items()
        ]
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu")])
        await query.edit_message_text("🌐 Choose your timezone:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("tz_set:"):
        tzname = data.split(":", 1)[1]
        await db.set_user_timezone(tg_user.id, tzname)
        await query.edit_message_text(f"✅ Timezone set to {h(tzname)}.", reply_markup=main_menu_keyboard())
        return

    # Everything below operates on a specific object id
    if ":" not in data:
        return
    action, obj_id_str = data.split(":", 1)
    try:
        obj_id = int(obj_id_str)
    except ValueError:
        return
    obj = await db.get_object(obj_id)
    if not obj or obj.chat_id != update.effective_chat.id:
        await query.edit_message_text("⚠️ This link no longer exists.")
        return

    if action == "view":
        if obj.status == "DEAD":
            await query.edit_message_text(
                render_dead_card(obj, tz_name), reply_markup=dead_card_keyboard(obj.id, obj.is_hidden),
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                render_active_card(obj, tz_name), reply_markup=active_card_keyboard(obj.id),
                parse_mode=ParseMode.HTML,
            )
        return

    if action == "edit":
        _AWAITING_NOTE[update.effective_chat.id] = obj.id
        await query.edit_message_text(
            f"✏️ Send the new note for <code>{h(obj.object_id)}</code> (or 'none' to clear).",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "hide":
        await db.set_object_hidden(obj.id, True)
        obj = await db.get_object(obj.id)
        await query.edit_message_text(
            render_dead_card(obj, tz_name), reply_markup=dead_card_keyboard(obj.id, obj.is_hidden),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "show":
        await db.set_object_hidden(obj.id, False)
        obj = await db.get_object(obj.id)
        await query.edit_message_text(
            render_dead_card(obj, tz_name), reply_markup=dead_card_keyboard(obj.id, obj.is_hidden),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "continue":
        await db.resume_object(obj.id)
        obj = await db.get_object(obj.id)
        await query.edit_message_text(
            render_active_card(obj, tz_name), reply_markup=active_card_keyboard(obj.id),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "stop":
        await db.stop_object(obj.id)
        await query.edit_message_text(f"⏹️ Stopped monitoring #{obj.id}.", reply_markup=main_menu_keyboard())
        return


# ---------------------------------------------------------------------------
# Background monitoring worker
# ---------------------------------------------------------------------------

async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    active_objs = await db.list_active_objects()
    if not active_objs:
        return

    async with aiohttp.ClientSession() as session:
        for obj in active_objs:
            try:
                result = await graph_checker.check_object(session, obj.object_id)
            except Exception:
                logger.exception("Unhandled error checking object %s", obj.id)
                continue

            if result.is_auth_error:
                # Don't touch the object's status - alert the admin once and move on.
                logger.warning("Auth error checking object %s: %s", obj.id, result.detail)
                continue

            if result.status == graph_checker.STATUS_UNKNOWN:
                await db.touch_last_checked(obj.id)
                continue  # transient - keep ACTIVE, no alert (anti-glitch protection)

            if result.status == graph_checker.STATUS_ACTIVE:
                if result.name and result.name != obj.name:
                    await db.update_object_status(obj.id, "ACTIVE", name=result.name)
                else:
                    await db.touch_last_checked(obj.id)
                continue

            # result.status == DEAD
            await db.update_object_status(obj.id, "DEAD", name=result.name or obj.name)
            if not obj.die_alert_sent:
                await db.set_die_alert_sent(obj.id, True)
                fresh = await db.get_object(obj.id)
                user = await db.get_user(obj.chat_id)
                tz_name = user.timezone if user else config.DEFAULT_TIMEZONE
                try:
                    await context.bot.send_message(
                        obj.chat_id,
                        render_dead_card(fresh, tz_name),
                        reply_markup=dead_card_keyboard(fresh.id, fresh.is_hidden),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Failed to send DIE alert for object %s", obj.id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    asyncio.run(db.init_db())

    application = Application.builder().token(config.BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.job_queue.run_repeating(
        monitor_job, interval=config.CHECK_INTERVAL_SECONDS, first=10
    )

    logger.info("Bot starting (check interval: %ss)", config.CHECK_INTERVAL_SECONDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


import asyncio

if __name__ == '__main__':
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    
    main()
