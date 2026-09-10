import os
import re
import html
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
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
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "2.0"))
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "5"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "monitor.db")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
ALLOWED_USER_IDS_STR = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = (
    [int(uid.strip()) for uid in ALLOWED_USER_IDS_STR.split(",") if uid.strip().isdigit()]
    if ALLOWED_USER_IDS_STR
    else []
)

db = Database(db_path=DATABASE_PATH)
user_states = {}

FB_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.|m\.|web\.|mobile\.)?(?:facebook\.com|fb\.watch|fb\.me)/[^\s]+",
    re.IGNORECASE,
)

def is_user_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS

def format_processing_time(created_str: str, updated_str: str) -> str:
    formats = ["%d-%m-%Y %H:%M:%S", "%H:%M:%S %d-%m-%Y"]
    t1 = None
    t2 = None
    for fmt in formats:
        try:
            t1 = datetime.strptime(created_str, fmt)
            break
        except ValueError:
            continue
    for fmt in formats:
        try:
            t2 = datetime.strptime(updated_str, fmt)
            break
        except ValueError:
            continue

    if not t1:
        t1 = datetime.now() - timedelta(minutes=5)
    if not t2:
        t2 = datetime.now()

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

def build_active_message(link_data: dict) -> Tuple[str, InlineKeyboardMarkup]:
    uid = html.escape(str(link_data.get("uid", "")))
    url = link_data.get("url", "")
    name = html.escape(str(link_data.get("name", "Facebook Post")))
    note = html.escape(str(link_data.get("note", "None") or "None"))
    created_raw = link_data.get("created_at", "")

    try:
        dt = datetime.strptime(created_raw, "%d-%m-%Y %H:%M:%S")
        created_formatted = dt.strftime("%H:%M:%S %d-%m-%Y")
    except Exception:
        created_formatted = created_raw or datetime.now().strftime("%H:%M:%S %d-%m-%Y")

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

def build_dead_message(link_data: dict, is_hidden: bool = False) -> Tuple[str, InlineKeyboardMarkup]:
    raw_uid = str(link_data.get("uid", ""))
    raw_name = str(link_data.get("name", "Facebook Post"))
    raw_note = str(link_data.get("note", "None") or "None")

    uid = "**********" if is_hidden else html.escape(raw_uid)
    name = "**********" if is_hidden else html.escape(raw_name)
    note = "**********" if is_hidden else html.escape(raw_note)

    created_raw = link_data.get("created_at", "")
    updated_raw = link_data.get("updated_at", "") or datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    try:
        dt1 = datetime.strptime(created_raw, "%d-%m-%Y %H:%M:%S")
        created_str = dt1.strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        created_str = created_raw or datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    try:
        dt2 = datetime.strptime(updated_raw, "%d-%m-%Y %H:%M:%S")
        updated_str = dt2.strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        updated_str = updated_raw or datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    processing_time = format_processing_time(created_str, updated_str)

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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    stats = await db.get_stats(update.effective_chat.id)
    text = (
        f"🚀 Welcome, <b>{html.escape(user.first_name)}</b>!\n\n"
        f"🔍 <b>Facebook Post & Link DIE Monitor</b>\n"
        f"I asynchronously track Facebook posts and alert you instantly the moment "
        f"a link is deleted, removed, or becomes inaccessible.\n\n"
        f"📊 <b>Your Dashboard:</b>\n"
        f"• Total Links: <b>{stats['total']}</b>\n"
        f"• Active: <b>{stats['active']}</b>\n"
        f"• Dead: <b>{stats['dead']}</b>\n"
        f"• Stopped: <b>{stats['stopped']}</b>\n\n"
        f"Send or paste any Facebook link directly to start monitoring, "
        f"or use the quick actions below:"
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
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

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

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    await render_list_page(update, context, page=0)

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

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
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
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    stats = await db.get_stats(update.effective_chat.id)
    text = (
        "🛠️ <b>Monitor Tools & Utilities:</b>\n\n"
        f"⏱️ <b>Scan Interval:</b> Every {CHECK_INTERVAL_SECONDS}s\n"
        f"⏳ <b>Rate Limit Delay:</b> {REQUEST_DELAY_SECONDS}s between requests\n"
        f"💾 <b>Database:</b> SQLite (Asynchronous)\n"
        f"🟢 <b>Active Targets:</b> {stats['active']}\n"
        f"🔴 <b>Dead Detections:</b> {stats['dead']}\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("⚡ Instant Check All", callback_data="tools_check_now"),
            InlineKeyboardButton("🧹 Clear Dead Links", callback_data="tools_clean_dead"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if user.id in user_states:
        del user_states[user.id]
    await update.message.reply_text("❌ Action cancelled. Returning to normal mode.")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    state = user_states.get(user.id)
    if isinstance(state, dict) and state.get("action") == "EDIT_NOTE":
        link_id = state["link_id"]
        del user_states[user.id]
        await db.update_note(link_id, text)
        link = await db.get_link_by_id(link_id)
        if link:
            msg_text, reply_markup = build_active_message(link)
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
                "⚠️ No valid Facebook links detected in your message. "
                "Please send a link starting with facebook.com or fb.watch, or type /cancel."
            )
        return

    if user.id in user_states:
        del user_states[user.id]

    status_msg = await update.message.reply_text(
        f"⏳ Found {len(raw_urls)} Facebook link(s). Resolving details and initiating monitoring..."
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
                msg_text, reply_markup = build_active_message(record)
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

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_user_allowed(user.id):
        await query.answer("Unauthorized", show_alert=True)
        return

    if data == "main_menu":
        stats = await db.get_stats(chat_id)
        text = (
            f"🔍 <b>Facebook Post & Link DIE Monitor</b>\n\n"
            f"📊 <b>Current Monitoring Status:</b>\n"
            f"• Total Monitored: <b>{stats['total']}</b>\n"
            f"• Active: <b>{stats['active']}</b>\n"
            f"• Dead: <b>{stats['dead']}</b>\n"
            f"• Stopped: <b>{stats['stopped']}</b>\n\n"
            f"Choose an action below:"
        )
        keyboard = [
            [
                InlineKeyboardButton("➕ Add Links", callback_data="cmd_add"),
                InlineKeyboardButton("📋 List Links", callback_data="list_0"),
            ],
            [
                InlineKeyboardButton("🛠️ Tools", callback_data="cmd_tools"),
            ],
        ]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
        return

    if data == "cmd_add":
        user_states[user.id] = "AWAITING_LINKS"
        text = (
            "➕ <b>Add Links to Monitor</b>\n\n"
            "Paste one or multiple Facebook URLs in your next message.\n"
            "Send /cancel to abort."
        )
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cmd_cancel")]]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
        return

    if data == "cmd_cancel":
        if user.id in user_states:
            del user_states[user.id]
        await query.edit_message_text("❌ Action cancelled.")
        return

    if data == "cmd_tools":
        stats = await db.get_stats(chat_id)
        text = (
            "🛠️ <b>Monitor Tools & Utilities:</b>\n\n"
            f"⏱️ <b>Scan Interval:</b> Every {CHECK_INTERVAL_SECONDS}s\n"
            f"⏳ <b>Rate Limit Delay:</b> {REQUEST_DELAY_SECONDS}s between requests\n"
            f"💾 <b>Database:</b> SQLite (Asynchronous)\n"
            f"🟢 <b>Active Targets:</b> {stats['active']}\n"
            f"🔴 <b>Dead Detections:</b> {stats['dead']}\n"
        )
        keyboard = [
            [
                InlineKeyboardButton("⚡ Instant Check All", callback_data="tools_check_now"),
                InlineKeyboardButton("🧹 Clear Dead Links", callback_data="tools_clean_dead"),
            ],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
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
            text, markup = build_dead_message(link, is_hidden=bool(link.get("is_hidden", 0)))
        else:
            text, markup = build_active_message(link)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
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
            text, markup = build_dead_message(link, is_hidden=True)
            try:
                await query.edit_message_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
            except Exception:
                pass
        return

    if data.startswith("show_"):
        link_id = int(data.split("_")[1])
        await db.toggle_hidden(link_id, target_state=0)
        link = await db.get_link_by_id(link_id)
        if link:
            text, markup = build_dead_message(link, is_hidden=False)
            try:
                await query.edit_message_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
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
        msg_text, markup = build_active_message(link)
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
                f"⏸️ <b>Monitoring paused for UID:</b> <code>{html.escape(str(link['uid']))}</code>\n"
                f"Status: STOPPED",
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

async def run_single_monitoring_cycle(app: Application, target_chat_id: Optional[int] = None) -> None:
    active_links = await db.get_active_links()
    if target_chat_id is not None:
        active_links = [l for l in active_links if l["chat_id"] == target_chat_id]

    if not active_links:
        return

    logger.info(f"Running monitoring cycle for {len(active_links)} active link(s)...")
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
                    logger.warning(f"Link {url} (ID: {link_id}) detected as DIE/DEAD! Reason: {result.reason}")
                    await db.update_status(link_id, status="DEAD", die_alert_sent=1)
                    updated_link = await db.get_link_by_id(link_id)

                    if updated_link and not link_data.get("die_alert_sent", 0):
                        alert_text, markup = build_dead_message(updated_link, is_hidden=False)
                        try:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=alert_text,
                                reply_markup=markup,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )
                            logger.info(f"Sent DEAD alert to chat_id {chat_id} for link {link_id}")
                        except Exception as e:
                            logger.error(f"Failed to send DEAD alert to {chat_id}: {e}")

                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        tasks = [asyncio.create_task(check_item(l)) for l in active_links]
        await asyncio.gather(*tasks, return_exceptions=True)

async def background_monitoring_worker(app: Application) -> None:
    logger.info(f"Background monitoring worker started. Interval: {CHECK_INTERVAL_SECONDS}s")
    while True:
        try:
            await run_single_monitoring_cycle(app)
        except Exception as e:
            logger.error(f"Error in background monitoring cycle: {e}", exc_info=True)
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
        logger.info("Successfully registered Telegram bot menu commands (/setcommands)")
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    asyncio.create_task(background_monitoring_worker(app))

def main() -> None:
    if not BOT_TOKEN:
        logger.error("CRITICAL: BOT_TOKEN is not set in environment variables!")
        raise SystemExit("Missing BOT_TOKEN environment variable.")

    logger.info("Building Telegram Application...")
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("tools", cmd_tools))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    logger.info("Bot started successfully. Listening for updates...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
