"""
Facebook Post Monitor Telegram Bot (Production Ready)
- Auto Admin Approval Workflow (Interactive Alert Cards)
- Anti-Spam protection on reject
- Direct Admin /block and /unblock commands
- Per-User Local Timezone Engine (Default: Asia/Dhaka)
- Telegram Spoiler Tag Support for Hide Info
- Instant DEAD Alert on Invalid/Dead Links
"""
import os
import re
import html
import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Tuple
import pytz
import aiohttp
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from database import Database
from checker import check_facebook_link, extract_fb_uid

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("FBMonitorBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip()) if os.getenv("ADMIN_ID", "").strip().isdigit() else 0
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "2.0"))
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "5"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "monitor.db")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

db = Database(db_path=DATABASE_PATH)
user_states = {}

FB_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.|m\.|web\.|mobile\.)?(?:facebook\.com|fb\.watch|fb\.me)/[^\s]+",
    re.IGNORECASE,
)

# Timezone converter helper
async def get_user_now(chat_id: int) -> Tuple[datetime, str]:
    user = await db.get_user(chat_id)
    tz_str = user["timezone"] if user and user.get("timezone") else "Asia/Dhaka"
    try:
        tz = pytz.timezone(tz_str)
    except Exception:
        tz = pytz.timezone("Asia/Dhaka")
    return datetime.now(tz), tz_str

def convert_utc_to_user_str(utc_dt_str: str, tz_str: str, out_fmt: str) -> str:
    try:
        dt = datetime.strptime(utc_dt_str, "%d-%m-%Y %H:%M:%S")
        utc_dt = pytz.utc.localize(dt)
        local_tz = pytz.timezone(tz_str)
        return utc_dt.astimezone(local_tz).strftime(out_fmt)
    except Exception:
        return utc_dt_str

def format_processing_time(created_utc_str: str, updated_utc_str: str) -> str:
    try:
        t1 = datetime.strptime(created_utc_str, "%d-%m-%Y %H:%M:%S")
    except Exception:
        t1 = datetime.utcnow()
    try:
        t2 = datetime.strptime(updated_utc_str, "%d-%m-%Y %H:%M:%S")
    except Exception:
        t2 = datetime.utcnow()

    diff = t2 - t1
    total_seconds = max(int(diff.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} hours")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} minutes")
    parts.append(f"{seconds} seconds")
    return " ".join(parts)

# Access Control Verification
async def verify_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False

    # Admin is permanently authorized
    if ADMIN_ID and user.id == ADMIN_ID:
        return True

    user_data = await db.get_user(user.id)
    status = user_data["status"] if user_data else None

    if status == "APPROVED":
        return True

    if status == "REJECTED":
        await update.effective_message.reply_text(
            "⛔ <b>Access Denied</b>\nYour account has been restricted from using this bot by the administrator.",
            parse_mode=ParseMode.HTML
        )
        return False

    if status == "PENDING":
        await update.effective_message.reply_text(
            "⏳ <b>Approval Pending</b>\nYour request has already been submitted to the Admin. Please wait patiently until it is reviewed.",
            parse_mode=ParseMode.HTML
        )
        return False

    # New user: create record & notify Admin
    await db.add_or_update_request(user.id, user.first_name, user.username)
    await update.effective_message.reply_text(
        "👋 <b>Welcome!</b>\nThis bot is private. An access request has been sent to the Admin. You will receive a notification as soon as you are approved.",
        parse_mode=ParseMode.HTML
    )

    if ADMIN_ID:
        uname_display = f"@{user.username}" if user.username else "None"
        admin_card = (
            f"🔔 <b>New Access Request!</b>\n\n"
            f"👤 <b>Name:</b> {html.escape(user.first_name)}\n"
            f"🏷️ <b>Username:</b> {uname_display}\n"
            f"🆔 <b>Telegram ID:</b> <code>{user.id}</code>\n\n"
            f"<i>Would you like to approve this user to monitor links?</i>"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 Approve", callback_data=f"adm_appr_{user.id}"),
                InlineKeyboardButton("🔴 Reject", callback_data=f"adm_rejc_{user.id}"),
            ]
        ])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_card,
                reply_markup=markup,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to alert admin: {e}")

    return False

async def build_active_message(link_data: dict, chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(chat_id)
    tz_str = user["timezone"] if user and user.get("timezone") else "Asia/Dhaka"

    uid = html.escape(str(link_data.get("uid", "")))
    url = link_data.get("url", "")
    name = html.escape(str(link_data.get("name", "Facebook Post")))
    note = html.escape(str(link_data.get("note", "None") or "None"))
    created_raw = link_data.get("created_at", "")
    created_formatted = convert_utc_to_user_str(created_raw, tz_str, "%H:%M:%S %d-%m-%Y")

    text = (
        f"🔔 UID: {uid} - <a href=\"{html.escape(url)}\">Link Facebook</a>\n"
        f"🟢 Status: ACTIVE ✅\n"
        f"👤 Name: {name}\n"
        f"📝 Note: {note}\n"
        f"⏱️ Created Time: {created_formatted}\n"
        f"🔄 Progress: Monitoring, waiting for DIE ❌"
    )

    link_id = link_data.get("id")
    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{link_id}"),
            InlineKeyboardButton("📋 List", callback_data="list_0"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    return text, InlineKeyboardMarkup(keyboard)

async def build_dead_message(link_data: dict, chat_id: int, is_hidden: bool = False) -> Tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(chat_id)
    tz_str = user["timezone"] if user and user.get("timezone") else "Asia/Dhaka"

    raw_uid = str(link_data.get("uid", ""))
    raw_name = str(link_data.get("name", "Facebook Post"))
    raw_note = str(link_data.get("note", "None") or "None")

    if is_hidden:
        uid = f"<tg-spoiler>{html.escape(raw_uid)}</tg-spoiler>"
        name = f"<tg-spoiler>{html.escape(raw_name)}</tg-spoiler>"
        note = f"<tg-spoiler>{html.escape(raw_note)}</tg-spoiler>"
    else:
        uid = html.escape(raw_uid)
        name = html.escape(raw_name)
        note = html.escape(raw_note)

    created_raw = link_data.get("created_at", "")
    updated_raw = link_data.get("updated_at", "")

    created_str = convert_utc_to_user_str(created_raw, tz_str, "%d-%m-%Y %H:%M:%S")
    updated_str = convert_utc_to_user_str(updated_raw, tz_str, "%d-%m-%Y %H:%M:%S")
    processing_time = format_processing_time(created_raw, updated_raw)

    text = (
        f"🔔 UID: {uid}\n"
        f"🔴 Status: DEAD ❌\n"
        f"👤 Name: {name}\n"
        f"📝 Note: {note}\n"
        f"⏱️ Created: {created_str}\n"
        f"⏰ Updated: {updated_str}\n"
        f"⏳ Processing Time: {processing_time}"
    )

    link_id = link_data.get("id")
    keyboard = [
        [
            InlineKeyboardButton("🙈 Hide Info", callback_data=f"hide_{link_id}"),
            InlineKeyboardButton("🐵 Show Info", callback_data=f"show_{link_id}"),
        ],
        [
            InlineKeyboardButton("🟢 Continue Monitoring", callback_data=f"continue_{link_id}"),
            InlineKeyboardButton("🔴 Stop Monitoring", callback_data=f"stop_{link_id}"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)

# ================= Commands =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    stats = await db.get_stats(chat_id)
    _, user_tz = await get_user_now(chat_id)

    text = (
        f"🚀 Welcome, <b>{html.escape(user.first_name)}</b>!\n\n"
        f"🔍 <b>Facebook Post & Link DIE Monitor</b>\n"
        f"I asynchronously track Facebook posts and alert you instantly the moment "
        f"a link is deleted, removed, or becomes inaccessible.\n\n"
        f"📊 <b>Your Dashboard:</b>\n"
        f"• Total Links: <b>{stats['total']}</b>\n"
        f"• Active: <b>{stats['active']}</b>\n"
        f"• Dead: <b>{stats['dead']}</b>\n"
        f"• Stopped: <b>{stats['stopped']}</b>\n"
        f"• Local Timezone: <code>{user_tz}</code>\n\n"
        f"Send or paste any Facebook link directly to start monitoring, "
        f"or use the quick actions below:\n\n"
        f"👑 <b>Owner:</b> <a href=\"https://t.me/tmmusa73\">—͞Tᴍ Mᴜsᴀ ⚡</a>"
    )
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Links", callback_data="cmd_add"),
            InlineKeyboardButton("📋 List Links", callback_data="list_0"),
        ],
        [
            InlineKeyboardButton("🛠️ Tools", callback_data="cmd_tools"),
            InlineKeyboardButton("🔄 Refresh", callback_data="main_menu"),
        ],
    ]
    if ADMIN_ID and user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👥 Manage Users (Admin)", callback_data="adm_list_users")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return

    user = update.effective_user
    user_states[user.id] = "AWAITING_LINKS"
    text = (
        "➕ <b>Add Links to Monitor</b>\n\n"
        "Please send one or multiple Facebook links in a single message "
        "(separated by line breaks or spaces).\n\n"
        "<b>Supported formats:</b>\n"
        "• <code>https://www.facebook.com/.../posts/...</code>\n"
        "• <code>https://www.facebook.com/permalink.php?story_fbid=...</code>\n"
        "• <code>https://www.facebook.com/watch/?v=...</code>\n"
        "• <code>https://fb.watch/...</code>\n"
        "• <code>https://www.facebook.com/reel/...</code>\n\n"
        "Send /cancel at any time to abort."
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cmd_cancel")]]
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )

async def render_list_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    page_size: int = 5,
    edit_existing: bool = False,
) -> None:
    chat_id = update.effective_chat.id
    offset = page * page_size
    links = await db.get_links_by_chat_id(chat_id, limit=page_size + 1, offset=offset)
    has_next = len(links) > page_size
    current_page_items = links[:page_size]

    if not current_page_items:
        text = "📋 <b>Monitored Links:</b>\n\nYou have not added any links yet. Use /add to begin!"
        keyboard = [
            [InlineKeyboardButton("➕ Add New Link", callback_data="cmd_add")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
        if edit_existing and update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
            )
        return

    text = f"📋 <b>Monitored Links (Page {page + 1}):</b>\n\n"
    keyboard = []
    for item in current_page_items:
        status_icon = "🟢" if item["status"] == "ACTIVE" else ("🔴" if item["status"] == "DEAD" else "⏸️")
        uid = html.escape(str(item["uid"]))
        name = html.escape(str(item["name"])[:25])
        text += (
            f"{status_icon} <b>UID:</b> {uid}\n"
            f"👤 <b>Name:</b> {name}\n"
            f"🔗 <a href=\"{html.escape(item['url'])}\">Facebook Link</a>\n"
            f"----------------------------------------\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"🔍 {uid}", callback_data=f"view_{item['id']}"),
            InlineKeyboardButton("🗑️ Remove", callback_data=f"remove_{item['id']}"),
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_{page - 1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton("➕ Add More", callback_data="cmd_add"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"),
    ])

    if edit_existing and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return
    await render_list_page(update, context, page=0)

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return
    args = context.args
    if args:
        target_uid = args[0].strip()
        record = await db.get_link_by_uid(update.effective_chat.id, target_uid)
        if record:
            await db.delete_link(record["id"])
            await update.message.reply_text(
                f"🗑️ Successfully removed UID: <code>{html.escape(target_uid)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        else:
            await update.message.reply_text(
                f"❌ Link with UID <code>{html.escape(target_uid)}</code> not found.",
                parse_mode=ParseMode.HTML,
            )
            return
    await update.message.reply_text("Select an item below to remove it:")
    await render_list_page(update, context, page=0)

async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    stats = await db.get_stats(chat_id)
    _, tz_str = await get_user_now(chat_id)

    text = (
        "🛠️ <b>Monitor Tools & Utilities:</b>\n\n"
        f"⏱️ <b>Scan Interval:</b> Every {CHECK_INTERVAL_SECONDS}s\n"
        f"⏳ <b>Rate Limit Delay:</b> {REQUEST_DELAY_SECONDS}s between requests\n"
        f"🌐 <b>Current Timezone:</b> <code>{tz_str}</code>\n"
        f"🟢 <b>Active Targets:</b> {stats['active']}\n"
        f"🔴 <b>Dead Detections:</b> {stats['dead']}\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("⚡ Instant Check All", callback_data="tools_check_now"),
            InlineKeyboardButton("🧹 Clear Dead Links", callback_data="tools_clean_dead"),
        ],
        [InlineKeyboardButton("🌐 Change Timezone", callback_data="tools_change_tz")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and user.id in user_states:
        del user_states[user.id]
    await update.message.reply_text("❌ Action cancelled. Returning to normal mode.")

# ================= Admin Commands =================
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/block &lt;Telegram_ID&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target_id = context.args[0].strip()
    if not target_id.isdigit():
        await update.message.reply_text("Please enter a valid numeric Telegram ID.")
        return

    uid = int(target_id)
    await db.set_user_status(uid, "REJECTED")
    await update.message.reply_text(f"🚫 User <code>{uid}</code> has been BLOCKED.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text="⛔ <b>Notice:</b> Your access to this bot has been revoked by the administrator.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/unblock &lt;Telegram_ID&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target_id = context.args[0].strip()
    if not target_id.isdigit():
        await update.message.reply_text("Please enter a valid numeric Telegram ID.")
        return

    uid = int(target_id)
    await db.set_user_status(uid, "APPROVED")
    await update.message.reply_text(f"✅ User <code>{uid}</code> has been APPROVED / UNBLOCKED.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text="🎉 <b>Your access has been approved!</b>\nYou can now send Facebook links to monitor.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ================= Text Message Processing =================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_user_access(update, context):
        return

    user = update.effective_user
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    state = user_states.get(user.id)
    if isinstance(state, dict) and state.get("action") == "EDIT_NOTE":
        link_id = state["link_id"]
        del user_states[user.id]
        await db.update_note(link_id, text)
        link = await db.get_link_by_id(link_id)
        if link:
            msg_text, reply_markup = await build_active_message(link, chat_id)
            await update.message.reply_text(
                "✅ Note updated successfully!\n\n" + msg_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return

    raw_urls = FB_URL_REGEX.findall(text)
    if not raw_urls:
        lines = [line.strip() for line in text.splitlines() if "facebook.com" in line or "fb.watch" in line]
        raw_urls = lines

    if not raw_urls:
        if user.id in user_states and user_states[user.id] == "AWAITING_LINKS":
            await update.message.reply_text(
                "⚠️ No valid Facebook links detected. Send a link containing facebook.com or fb.watch, or /cancel."
            )
        return

    if user.id in user_states:
        del user_states[user.id]

    status_msg = await update.message.reply_text(
        f"⏳ Found {len(raw_urls)} Facebook link(s). Checking status..."
    )

    async with aiohttp.ClientSession() as session:
        for url in raw_urls:
            clean_url = url.strip()
            if not clean_url.startswith(("http://", "https://")):
                clean_url = "https://" + clean_url

            uid = extract_fb_uid(clean_url)
            check_result = await check_facebook_link(
                clean_url,
                session=session,
                custom_user_agent=USER_AGENT,
            )

            record = await db.add_link(
                chat_id=chat_id,
                uid=uid,
                url=clean_url,
                name=check_result.title,
                note="None",
            )

            if record:
                # If already DEAD, alert directly as DEAD
                if not check_result.is_alive or check_result.status == "DEAD":
                    await db.update_status(record["id"], status="DEAD", die_alert_sent=1)
                    updated_record = await db.get_link_by_id(record["id"])
                    dead_text, dead_markup = await build_dead_message(updated_record, chat_id, is_hidden=False)
                    await update.message.reply_text(
                        dead_text,
                        reply_markup=dead_markup,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                else:
                    msg_text, reply_markup = await build_active_message(record, chat_id)
                    await update.message.reply_text(
                        msg_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

            if len(raw_urls) > 1:
                await asyncio.sleep(1.0)

    try:
        await status_msg.delete()
    except Exception:
        pass

# ================= Inline Button Callbacks =================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Admin Approval Handlers
    if data.startswith("adm_appr_") or data.startswith("adm_rejc_"):
        if user.id != ADMIN_ID:
            await query.answer("Unauthorized.", show_alert=True)
            return

        target_uid = int(data.split("_")[2])
        if data.startswith("adm_appr_"):
            await db.set_user_status(target_uid, "APPROVED")
            await query.edit_message_text(f"✅ Approved User <code>{target_uid}</code> successfully!", parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text="🎉 <b>Your access request has been APPROVED!</b>\nYou can now use all commands and send Facebook links to monitor.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        else:
            await db.set_user_status(target_uid, "REJECTED")
            await query.edit_message_text(f"🚫 Rejected User <code>{target_uid}</code> successfully!", parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text="❌ <b>Notice:</b> Your request has been declined by the administrator.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        return

    # Check permission
    if not await verify_user_access(update, context):
        return

    # Admin User Management List
    if data == "adm_list_users":
        if user.id != ADMIN_ID:
            return
        users = await db.get_all_users()
        text = "👥 <b>Registered Users Management:</b>\n\n"
        keyboard = []
        for u in users[:15]:
            status_emoji = "🟢" if u["status"] == "APPROVED" else ("⏳" if u["status"] == "PENDING" else "🚫")
            text += f"{status_emoji} <b>{html.escape(u['first_name'] or 'User')}</b> | ID: <code>{u['user_id']}</code> | {u['status']}\n"
            action_btn = (
                InlineKeyboardButton("🚫 Block", callback_data=f"adm_block_{u['user_id']}")
                if u["status"] == "APPROVED"
                else InlineKeyboardButton("🟢 Approve", callback_data=f"adm_appr_{u['user_id']}")
            )
            keyboard.append([action_btn])
        keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_block_"):
        if user.id != ADMIN_ID:
            return
        target_uid = int(data.split("_")[2])
        await db.set_user_status(target_uid, "REJECTED")
        await query.answer("User blocked.")
        await handle_callback_query(update, context)
        return

    # Timezone Change Menu
    if data == "tools_change_tz":
        text = "🌐 <b>Select Your Local Timezone:</b>\nChoose one of the common timezones below:"
        keyboard = [
            [
                InlineKeyboardButton("🇧🇩 Bangladesh (UTC+6)", callback_data="set_tz_Asia/Dhaka"),
                InlineKeyboardButton("🇮🇳 India (UTC+5:30)", callback_data="set_tz_Asia/Kolkata"),
            ],
            [
                InlineKeyboardButton("🇸🇦 Saudi Arabia (UTC+3)", callback_data="set_tz_Asia/Riyadh"),
                InlineKeyboardButton("🇦🇪 UAE / Dubai (UTC+4)", callback_data="set_tz_Asia/Dubai"),
            ],
            [
                InlineKeyboardButton("🇬🇧 UK / London (GMT)", callback_data="set_tz_Europe/London"),
                InlineKeyboardButton("🇺🇸 USA / New York (EST)", callback_data="set_tz_America/New_York"),
            ],
            [InlineKeyboardButton("⬅️ Back to Tools", callback_data="cmd_tools")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if data.startswith("set_tz_"):
        tz_selected = data.replace("set_tz_", "")
        await db.set_user_timezone(chat_id, tz_selected)
        await query.answer(f"Timezone updated to {tz_selected}!")
        await query.edit_message_text(
            f"✅ <b>Timezone Updated!</b>\nYour reports and timestamps will now display in <code>{tz_selected}</code>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
            parse_mode=ParseMode.HTML
        )
        return

    # Navigation & General
    if data == "main_menu":
        stats = await db.get_stats(chat_id)
        _, user_tz = await get_user_now(chat_id)
        text = (
            f"🔍 <b>Facebook Post & Link DIE Monitor</b>\n\n"
            f"📊 <b>Current Monitoring Status:</b>\n"
            f"• Total Monitored: <b>{stats['total']}</b>\n"
            f"• Active: <b>{stats['active']}</b>\n"
            f"• Dead: <b>{stats['dead']}</b>\n"
            f"• Stopped: <b>{stats['stopped']}</b>\n"
            f"• Timezone: <code>{user_tz}</code>\n\n"
            f"Choose an action below:\n\n"
            f"👑 <b>Owner:</b> <a href=\"https://t.me/tmmusa73\">—͞Tᴍ Mᴜsᴀ ⚡</a>"
        )
        keyboard = [
            [
                InlineKeyboardButton("➕ Add Links", callback_data="cmd_add"),
                InlineKeyboardButton("📋 List Links", callback_data="list_0"),
            ],
            [InlineKeyboardButton("🛠️ Tools", callback_data="cmd_tools")],
        ]
        if ADMIN_ID and user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("👥 Manage Users (Admin)", callback_data="adm_list_users")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    if data == "cmd_add":
        user_states[user.id] = "AWAITING_LINKS"
        text = (
            "➕ <b>Add Links to Monitor</b>\n\n"
            "Paste one or multiple Facebook URLs in your next message.\n"
            "Send /cancel to abort."
        )
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cmd_cancel")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if data == "cmd_cancel":
        if user.id in user_states:
            del user_states[user.id]
        await query.edit_message_text("❌ Action cancelled.")
        return

    if data == "cmd_tools":
        stats = await db.get_stats(chat_id)
        _, tz_str = await get_user_now(chat_id)
        text = (
            "🛠️ <b>Monitor Tools & Utilities:</b>\n\n"
            f"⏱️ <b>Scan Interval:</b> Every {CHECK_INTERVAL_SECONDS}s\n"
            f"⏳ <b>Rate Limit Delay:</b> {REQUEST_DELAY_SECONDS}s between requests\n"
            f"🌐 <b>Current Timezone:</b> <code>{tz_str}</code>\n"
            f"🟢 <b>Active Targets:</b> {stats['active']}\n"
            f"🔴 <b>Dead Detections:</b> {stats['dead']}\n"
        )
        keyboard = [
            [
                InlineKeyboardButton("⚡ Instant Check All", callback_data="tools_check_now"),
                InlineKeyboardButton("🧹 Clear Dead Links", callback_data="tools_clean_dead"),
            ],
            [InlineKeyboardButton("🌐 Change Timezone", callback_data="tools_change_tz")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if data == "tools_check_now":
        await query.answer("⚡ Running immediate check on all active links...", show_alert=True)
        asyncio.create_task(run_single_monitoring_cycle(context.application, target_chat_id=chat_id))
        return

    if data == "tools_clean_dead":
        links = await db.get_links_by_chat_id(chat_id, limit=500)
        cleaned = 0
        for l in links:
            if l["status"] == "DEAD":
                await db.delete_link(l["id"])
                cleaned += 1
        await query.answer(f"🧹 Removed {cleaned} dead link(s)!", show_alert=True)
        await cmd_tools(update, context)
        return

    if data.startswith("list_"):
        page = int(data.split("_")[1])
        await render_list_page(update, context, page=page, edit_existing=True)
        return

    if data.startswith("view_"):
        link_id = int(data.split("_")[1])
        link = await db.get_link_by_id(link_id)
        if not link:
            await query.answer("Link not found or already deleted.", show_alert=True)
            return
        if link["status"] == "DEAD":
            text, markup = await build_dead_message(link, chat_id, is_hidden=bool(link.get("is_hidden", 0)))
        else:
            text, markup = await build_active_message(link, chat_id)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    if data.startswith("edit_"):
        link_id = int(data.split("_")[1])
        link = await db.get_link_by_id(link_id)
        if not link:
            await query.answer("Link not found.", show_alert=True)
            return
        user_states[user.id] = {"action": "EDIT_NOTE", "link_id": link_id}
        await query.edit_message_text(
            f"✏️ <b>Edit Note for UID:</b> <code>{html.escape(str(link['uid']))}</code>\n\n"
            f"Current Note: <i>{html.escape(str(link['note']))}</i>\n\n"
            f"Please send the new note text in your next message, or /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("hide_"):
        link_id = int(data.split("_")[1])
        await db.toggle_hidden(link_id, target_state=1)
        link = await db.get_link_by_id(link_id)
        if link:
            text, markup = await build_dead_message(link, chat_id, is_hidden=True)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                pass
        return

    if data.startswith("show_"):
        link_id = int(data.split("_")[1])
        await db.toggle_hidden(link_id, target_state=0)
        link = await db.get_link_by_id(link_id)
        if link:
            text, markup = await build_dead_message(link, chat_id, is_hidden=False)
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                pass
        return

    if data.startswith("continue_"):
        link_id = int(data.split("_")[1])
        link = await db.get_link_by_id(link_id)
        if not link:
            await query.answer("Link not found.", show_alert=True)
            return
        await db.update_status(link_id, "ACTIVE", die_alert_sent=0)
        link = await db.get_link_by_id(link_id)
        msg_text, markup = await build_active_message(link, chat_id)
        await query.edit_message_text(
            "🟢 <b>Monitoring resumed!</b>\n\n" + msg_text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await query.answer("Monitoring re-enabled!", show_alert=False)
        return

    if data.startswith("stop_"):
        link_id = int(data.split("_")[1])
        await db.update_status(link_id, "STOPPED")
        await query.answer("Monitoring stopped for this link.", show_alert=True)
        link = await db.get_link_by_id(link_id)
        if link:
            keyboard = [
                [
                    InlineKeyboardButton("🟢 Resume Monitoring", callback_data=f"continue_{link_id}"),
                    InlineKeyboardButton("🗑️ Remove Permanently", callback_data=f"remove_{link_id}"),
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]
            await query.edit_message_text(
                f"⏸️ <b>Monitoring paused for UID:</b> <code>{html.escape(str(link['uid']))}</code>\nStatus: STOPPED",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("remove_"):
        link_id = int(data.split("_")[1])
        link = await db.get_link_by_id(link_id)
        await db.delete_link(link_id)
        await query.answer("Link removed.", show_alert=False)
        if link:
            await query.edit_message_text(
                f"🗑️ <b>Deleted:</b> UID <code>{html.escape(str(link['uid']))}</code> has been removed from monitoring.\n\n"
                f"Use the buttons below to manage remaining links:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 View Links", callback_data="list_0")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        else:
            await render_list_page(update, context, page=0, edit_existing=True)
        return

# ================= Background Engine =================
async def run_single_monitoring_cycle(app: Application, target_chat_id: Optional[int] = None) -> None:
    active_links = await db.get_active_links()
    if target_chat_id is not None:
        active_links = [l for l in active_links if l["chat_id"] == target_chat_id]

    if not active_links:
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    async with aiohttp.ClientSession() as session:
        async def check_item(link_data: dict):
            async with semaphore:
                link_id = link_data["id"]
                chat_id = link_data["chat_id"]
                url = link_data["url"]

                result = await check_facebook_link(
                    url,
                    session=session,
                    custom_user_agent=USER_AGENT,
                )
                await db.update_last_checked(link_id)

                if not result.is_alive and result.status == "DEAD":
                    logger.warning(f"Link {url} (ID: {link_id}) detected as DIE/DEAD!")
                    await db.update_status(link_id, status="DEAD", die_alert_sent=1)
                    updated_link = await db.get_link_by_id(link_id)

                    if updated_link and not link_data.get("die_alert_sent", 0):
                        alert_text, markup = await build_dead_message(updated_link, chat_id, is_hidden=False)
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=alert_text,
                                reply_markup=markup,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )
                        except Exception as e:
                            logger.error(f"Failed to send DEAD alert to {chat_id}: {e}")

                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        tasks = [asyncio.create_task(check_item(l)) for l in active_links]
        await asyncio.gather(*tasks, return_exceptions=True)

async def background_monitoring_worker(app: Application) -> None:
    while True:
        try:
            await run_single_monitoring_cycle(app)
        except Exception as e:
            logger.error(f"Error in background monitoring: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

async def post_init(app: Application) -> None:
    await db.init_db()
    commands = [
        BotCommand("start", "🚀 Start Dashboard"),
        BotCommand("add", "➕ Add New Link to Monitor"),
        BotCommand("list", "📋 List of Added Links"),
        BotCommand("remove", "🗑️ Remove Added Link"),
        BotCommand("tools", "🛠️ Utilities / Tools"),
        BotCommand("cancel", "❌ Cancel Current Action"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Could not set commands: {e}")

    asyncio.create_task(background_monitoring_worker(app))

def main() -> None:
    if not BOT_TOKEN:
        logger.error("CRITICAL: BOT_TOKEN is not set in environment variables!")
        raise SystemExit("Missing BOT_TOKEN environment variable.")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Standard Commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("tools", cmd_tools))
    application.add_handler(CommandHandler("cancel", cmd_cancel))

    # Admin Exclusive Commands
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CommandHandler("unblock", cmd_unblock))
    application.add_handler(CommandHandler("approve", cmd_unblock))

    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    logger.info("Bot started successfully. Listening for updates...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
