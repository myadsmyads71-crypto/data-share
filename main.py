"""
main.py - Telegram Membership Verification + Unique PIN + Admin Chat Bot
=========================================================================
What this bot does
  1. /start        -> welcome + 2 channel Join buttons + Verify button
  2. Verify        -> checks membership in BOTH channels (getChatMember)
                      then gives the user ONE permanent unique 6-char PIN
  3. Admin alerts  -> admin gets a message when a NEW user verifies
  4. Admin tools   -> /admin /stats /user /broadcast /chat /endchat
  5. Two-way chat  -> verified users and the admin can talk through the bot
                      (text, photos, videos, files, voice... everything)

Environment variables (keep them SECRET, never put them in the code):
  BOT_TOKEN             token from @BotFather
  ADMIN_ID              your numeric Telegram ID
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  service_role / secret key (server-side only!)
  CHANNEL_1_USERNAME    optional, default: SNYPER_DEVIL
  CHANNEL_2_USERNAME    optional, default: BLACK_DEVIL_z_x
  PORT                  set automatically by Render
"""

import asyncio
import functools
import html
import logging
import os
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from postgrest.exceptions import APIError
from supabase import Client, create_client
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Hide noisy HTTP logs (they can contain the bot token in the URL)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("pinbot")


# ---------------------------------------------------------------------
# Configuration (all secrets come from environment variables)
# ---------------------------------------------------------------------
def require_env(name: str) -> str:
    """Return an environment variable or stop with a clear message."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"❌ Missing environment variable: {name}")
    return value


BOT_TOKEN = require_env("BOT_TOKEN")
SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require_env("SUPABASE_SERVICE_KEY")
try:
    ADMIN_ID = int(require_env("ADMIN_ID"))
except ValueError:
    raise SystemExit("❌ ADMIN_ID must be a number (your Telegram user ID).")


def make_channel(username: str) -> dict:
    """Build the channel info used for buttons and membership checks."""
    username = username.strip().lstrip("@")
    return {
        "chat_id": f"@{username}",
        "url": f"https://t.me/{username}",
        "title": f"@{username}",
    }


CHANNELS = [
    make_channel(os.environ.get("CHANNEL_1_USERNAME", "SNYPER_DEVIL")),
    make_channel(os.environ.get("CHANNEL_2_USERNAME", "BLACK_DEVIL_z_x")),
]

USERS_TABLE = "telegram_users"
MAP_TABLE = "admin_message_map"
PIN_LENGTH = 6
PIN_ALPHABET = string.ascii_uppercase + string.digits  # A-Z and 0-9
MAX_PIN_ATTEMPTS = 15
IST = timezone(timedelta(hours=5, minutes=30))  # India time for admin alerts

# Server-side Supabase client (service key = full access, so never share it)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------
async def run_db(func, *args):
    """Run a (blocking) Supabase function in a thread so the bot stays fast."""
    return await asyncio.to_thread(func, *args)


def generate_pin() -> str:
    """Create a secure random 6-character PIN (A-Z, 0-9), e.g. A7K2P9."""
    return "".join(secrets.choice(PIN_ALPHABET) for _ in range(PIN_LENGTH))


def parse_dt(value) -> datetime | None:
    """Convert a database timestamp string into an IST datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(IST)
    except ValueError:
        return None


def fmt_dt(value) -> str:
    """Nice date+time text for admin messages."""
    dt = parse_dt(value)
    return dt.strftime("%d %b %Y, %I:%M:%S %p IST") if dt else "-"


def name_of(row: dict) -> str:
    return html.escape(row.get("first_name") or "No name")


def username_of(row: dict) -> str:
    return f"@{html.escape(row['username'])}" if row.get("username") else "no username"


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def admin_only(func):
    """Decorator: only the admin (ADMIN_ID) can run this handler."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            if update.callback_query:
                await update.callback_query.answer("⛔ Admin only", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ Admin only command.")
            return
        await func(update, context)

    return wrapper


async def safe_edit(query, text: str, reply_markup=None):
    """Edit a message; ignore the harmless 'message is not modified' error."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit_message_text failed: %s", e)


# ---------------------------------------------------------------------
# Database functions (blocking - always call them through run_db)
# ---------------------------------------------------------------------
def db_get_user(telegram_id: int) -> dict | None:
    res = (
        supabase.table(USERS_TABLE)
        .select("*")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def db_get_user_by_pin(pin: str) -> dict | None:
    res = (
        supabase.table(USERS_TABLE).select("*").eq("pin", pin).limit(1).execute()
    )
    return res.data[0] if res.data else None


def db_create_or_get_user(telegram_id: int, username: str | None, first_name: str | None):
    """
    Return (row, created).
      created=True  -> this call created the user + PIN (new user)
      created=False -> user already existed, existing PIN returned

    Safety against duplicates / simultaneous requests:
      * telegram_id is UNIQUE  -> a user can never get two rows/PINs
      * pin is UNIQUE          -> two users can never share a PIN
      If an insert fails with a unique violation we check again:
        - row for this telegram_id exists -> another request won, return it
        - otherwise the PIN collided     -> generate a new PIN and retry
    """
    existing = db_get_user(telegram_id)
    if existing:
        # Keep name/username fresh (does NOT touch the PIN)
        if existing.get("username") != username or existing.get("first_name") != first_name:
            try:
                supabase.table(USERS_TABLE).update(
                    {"username": username, "first_name": first_name}
                ).eq("telegram_id", telegram_id).execute()
                existing["username"], existing["first_name"] = username, first_name
            except APIError as e:
                logger.warning("Could not refresh user info: %s", e)
        return existing, False

    for attempt in range(1, MAX_PIN_ATTEMPTS + 1):
        pin = generate_pin()
        try:
            res = (
                supabase.table(USERS_TABLE)
                .insert(
                    {
                        "telegram_id": telegram_id,
                        "username": username,
                        "first_name": first_name,
                        "pin": pin,
                        "verified": True,
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .execute()
            )
            return res.data[0], True
        except APIError as e:
            if getattr(e, "code", None) != "23505":  # 23505 = unique violation
                raise  # a real database problem, not a duplicate
            winner = db_get_user(telegram_id)
            if winner:  # simultaneous request already created this user
                return winner, False
            logger.info("PIN collision (attempt %s) - generating a new PIN", attempt)

    raise RuntimeError("Could not generate a unique PIN, please try again.")


def db_count_verified() -> int:
    res = (
        supabase.table(USERS_TABLE)
        .select("id", count="exact")
        .eq("verified", True)
        .limit(1)
        .execute()
    )
    return res.count or 0


def db_recent_users(limit: int = 5) -> list:
    res = (
        supabase.table(USERS_TABLE)
        .select("*")
        .order("verified_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def db_all_verified_ids() -> list:
    """All verified Telegram IDs (paged, because Supabase returns max 1000 rows)."""
    ids, start, page = [], 0, 1000
    while True:
        res = (
            supabase.table(USERS_TABLE)
            .select("telegram_id")
            .eq("verified", True)
            .order("id")
            .range(start, start + page - 1)
            .execute()
        )
        rows = res.data or []
        ids.extend(r["telegram_id"] for r in rows)
        if len(rows) < page:
            break
        start += page
    return ids


def db_save_map(admin_message_id: int, user_telegram_id: int) -> None:
    supabase.table(MAP_TABLE).upsert(
        {"admin_message_id": admin_message_id, "user_telegram_id": user_telegram_id}
    ).execute()


def db_lookup_map(admin_message_id: int) -> int | None:
    res = (
        supabase.table(MAP_TABLE)
        .select("user_telegram_id")
        .eq("admin_message_id", admin_message_id)
        .limit(1)
        .execute()
    )
    return res.data[0]["user_telegram_id"] if res.data else None


# ---------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------
def join_keyboard(only: list | None = None) -> InlineKeyboardMarkup:
    """Join buttons (all channels, or only the missing ones) + Verify button."""
    channels = only if only is not None else CHANNELS
    rows = [
        [InlineKeyboardButton(f"📢 Join {ch['title']}", url=ch["url"])]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton("✅ Verify Membership", callback_data="verify")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------
# USER FLOW: /start and Verify
# ---------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 <b>Welcome, {html.escape(user.first_name or 'friend')}!</b>\n\n"
        "To get your <b>unique PIN</b>, please join both channels below, "
        "then tap <b>✅ Verify Membership</b>."
    )
    if user.id == ADMIN_ID:
        text += "\n\n🛠 <i>You are the admin. Use /admin for the dashboard.</i>"
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=join_keyboard()
    )


def is_channel_member(member) -> bool:
    """True if the ChatMember object means the user is inside the channel."""
    if member.status in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    ):
        return True
    # A "restricted" user is still a member if is_member is True
    return member.status == ChatMemberStatus.RESTRICTED and getattr(
        member, "is_member", False
    )


async def notify_admin_new_user(context: ContextTypes.DEFAULT_TYPE, row: dict):
    """Tell the admin about a NEW verified user (called only once per user)."""
    dt = parse_dt(row.get("verified_at")) or datetime.now(IST)
    text = (
        "🆕 <b>New Verified User</b>\n\n"
        f"👤 Name: {name_of(row)}\n"
        f"🔗 Username: {username_of(row)}\n"
        f"🆔 Telegram ID: <code>{row['telegram_id']}</code>\n"
        f"🔑 PIN: <code>{row['pin']}</code>\n"
        f"📅 Date: {dt.strftime('%d %b %Y')}\n"
        f"🕒 Time: {dt.strftime('%I:%M:%S %p')} IST"
    )
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.error("Could not notify admin: %s", e)


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer("Checking membership...")

    # 1) Check both channels
    missing, errors = [], []
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(ch["chat_id"], user.id)
            if not is_channel_member(member):
                missing.append(ch)
        except TelegramError as e:
            logger.error("getChatMember failed for %s: %s", ch["chat_id"], e)
            errors.append(f"{ch['title']}: {e}")

    # 2) Telegram/API problem (usually: bot is not admin in that channel)
    if errors:
        await safe_edit(
            query,
            "⚠️ Verification is temporarily unavailable.\n"
            "The admin has been notified. Please try again later.",
            join_keyboard(),
        )
        try:
            await context.bot.send_message(
                ADMIN_ID,
                "⚠️ Verification error!\nMake sure the bot is ADMIN in both channels "
                "and the channel usernames are correct.\n\n" + "\n".join(errors),
            )
        except TelegramError:
            pass
        return

    # 3) User has not joined everything -> show which channel(s) are missing
    if missing:
        names = "\n".join(f"• {ch['title']}" for ch in missing)
        await safe_edit(
            query,
            f"❌ <b>You haven't joined:</b>\n{names}\n\n"
            "Join using the button(s) below, then tap <b>✅ Verify Membership</b> again.",
            join_keyboard(only=missing),
        )
        return

    # 4) All good -> get or create the permanent PIN
    try:
        row, created = await run_db(
            db_create_or_get_user, user.id, user.username, user.first_name
        )
    except Exception as e:  # database / network problem
        logger.exception("Database error while verifying %s", user.id)
        await safe_edit(
            query,
            "⚠️ Database error. Please tap Verify again in a moment.",
            join_keyboard(),
        )
        # Send the real reason to the admin only (never to the user)
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ Database error while verifying user {user.id}\n\n"
                f"{type(e).__name__}: {str(e)[:900]}",
            )
        except TelegramError:
            pass
        return

    pin = row["pin"]
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Copy PIN", copy_text=CopyTextButton(text=pin))]]
    )
    await safe_edit(
        query,
        "🎉 <b>Membership Verified!</b>\n\n"
        f"🔑 Your unique PIN:\n<code>{pin}</code>\n\n"
        "Tap the button below to copy it. This PIN is <b>permanent</b>.\n\n"
        "💬 You can now send messages, photos or files here - the admin will reply.",
        keyboard,
    )

    # Notify admin ONLY for brand-new users (no duplicates on re-verify)
    if created:
        await notify_admin_new_user(context, row)


# ---------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------
@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
                InlineKeyboardButton("🕒 Recent users", callback_data="adm_recent"),
            ]
        ]
    )
    await update.message.reply_text(
        "🛠 <b>Admin Dashboard</b>\n\n"
        "/stats - total verified users\n"
        "/user &lt;ID or PIN&gt; - look up a user\n"
        "/broadcast &lt;text&gt; - announce to all users\n"
        "   (or reply to any message/photo/file with /broadcast)\n"
        "/chat &lt;ID&gt; - start chatting with a user\n"
        "/endchat - stop chat mode\n\n"
        "💬 <b>Chat tip:</b> just <i>reply</i> to a user's message to answer them.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        total = await run_db(db_count_verified)
    except Exception as e:
        logger.error("stats failed: %s", e)
        await update.message.reply_text("⚠️ Database error.")
        return
    await update.message.reply_text(
        f"📊 <b>Total verified users:</b> {total}", parse_mode=ParseMode.HTML
    )


@admin_only
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        if query.data == "adm_stats":
            total = await run_db(db_count_verified)
            await query.message.reply_text(
                f"📊 <b>Total verified users:</b> {total}", parse_mode=ParseMode.HTML
            )
        elif query.data == "adm_recent":
            rows = await run_db(db_recent_users, 5)
            if not rows:
                await query.message.reply_text("No users yet.")
                return
            lines = [
                f"• {name_of(r)} ({username_of(r)}) - <code>{r['telegram_id']}</code> - "
                f"<code>{r['pin']}</code>"
                for r in rows
            ]
            await query.message.reply_text(
                "🕒 <b>Last 5 verified users</b>\n\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error("admin callback failed: %s", e)
        await query.message.reply_text("⚠️ Database error.")


@admin_only
async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /user <Telegram ID>\n(You can also use a 6-character PIN.)"
        )
        return
    arg = context.args[0].strip()
    try:
        row = None
        if arg.isdigit():
            row = await run_db(db_get_user, int(arg))
        if row is None and len(arg) == PIN_LENGTH:  # maybe it is a PIN
            row = await run_db(db_get_user_by_pin, arg.upper())
    except Exception as e:
        logger.error("user lookup failed: %s", e)
        await update.message.reply_text("⚠️ Database error.")
        return

    if not row:
        await update.message.reply_text("❌ No verified user found.")
        return
    await update.message.reply_text(
        "👤 <b>User Record</b>\n\n"
        f"Name: {name_of(row)}\n"
        f"Username: {username_of(row)}\n"
        f"Telegram ID: <code>{row['telegram_id']}</code>\n"
        f"PIN: <code>{row['pin']}</code>\n"
        f"Verified: {'Yes ✅' if row['verified'] else 'No ❌'}\n"
        f"Verified at: {fmt_dt(row.get('verified_at'))}\n"
        f"Created at: {fmt_dt(row.get('created_at'))}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chat <ID> - every normal message you send goes to that user."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /chat <Telegram ID>")
        return
    target = int(context.args[0])
    try:
        row = await run_db(db_get_user, target)
    except Exception as e:
        logger.error("chat lookup failed: %s", e)
        await update.message.reply_text("⚠️ Database error.")
        return
    if not row:
        await update.message.reply_text("❌ That user is not verified.")
        return
    context.bot_data["active_chat"] = target
    await update.message.reply_text(
        f"💬 Chat mode ON with {name_of(row)} (<code>{target}</code>).\n"
        "Everything you send now goes to this user. Use /endchat to stop.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def endchat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.pop("active_chat", None)
    await update.message.reply_text("✅ Chat mode OFF.")


# ---------------------------------------------------------------------
# BROADCAST (with confirmation)
# ---------------------------------------------------------------------
@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /broadcast Your text here          -> text announcement
    (reply to any message with /broadcast) -> copies that message (photo, file...)
    """
    msg = update.message
    if msg.reply_to_message:
        payload = {
            "kind": "copy",
            "from_chat_id": msg.chat_id,
            "message_id": msg.reply_to_message.message_id,
        }
    elif context.args:
        payload = {"kind": "text", "text": msg.text.split(None, 1)[1]}
    else:
        await update.message.reply_text(
            "Usage:\n/broadcast Your announcement text\n"
            "or reply to a message/photo/file with /broadcast"
        )
        return

    try:
        total = await run_db(db_count_verified)
    except Exception as e:
        logger.error("broadcast count failed: %s", e)
        await update.message.reply_text("⚠️ Database error.")
        return

    context.bot_data["pending_broadcast"] = payload

    # Show a preview first
    if payload["kind"] == "text":
        await msg.reply_text("👀 Preview:\n\n" + payload["text"])
    else:
        await context.bot.copy_message(
            ADMIN_ID, payload["from_chat_id"], payload["message_id"]
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ Send to {total} users", callback_data="bc_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="bc_no"),
            ]
        ]
    )
    await msg.reply_text("Send this announcement?", reply_markup=keyboard)


@admin_only
async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    payload = context.bot_data.pop("pending_broadcast", None)

    if query.data == "bc_no" or not payload:
        await query.edit_message_text(
            "❌ Broadcast cancelled." if query.data == "bc_no" else "⌛ Nothing pending."
        )
        return

    await query.edit_message_text("📤 Sending broadcast... please wait.")
    try:
        ids = await run_db(db_all_verified_ids)
    except Exception as e:
        logger.error("broadcast ids failed: %s", e)
        await query.edit_message_text("⚠️ Database error. Broadcast not sent.")
        return

    sent = failed = 0
    for telegram_id in ids:
        for _ in range(2):  # one retry if Telegram says "slow down"
            try:
                if payload["kind"] == "text":
                    await context.bot.send_message(telegram_id, payload["text"])
                else:
                    await context.bot.copy_message(
                        telegram_id, payload["from_chat_id"], payload["message_id"]
                    )
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (Forbidden, BadRequest):  # user blocked the bot / chat gone
                failed += 1
                break
            except TelegramError as e:
                logger.warning("broadcast error for %s: %s", telegram_id, e)
                failed += 1
                break
        await asyncio.sleep(0.05)  # stay under Telegram's rate limit

    await query.edit_message_text(
        f"✅ Broadcast finished.\n\nDelivered: {sent}\nFailed/blocked: {failed}"
    )


# ---------------------------------------------------------------------
# TWO-WAY CHAT (user <-> admin, any message type)
# ---------------------------------------------------------------------
async def react_ok(message):
    """Add a 👍 reaction as 'delivered' (ignore if not possible)."""
    try:
        await message.set_reaction("👍")
    except Exception:
        pass


async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A normal user sends something -> forward it to the admin."""
    msg = update.effective_message
    user = update.effective_user

    try:
        row = await run_db(db_get_user, user.id)
    except Exception as e:
        logger.error("db error in user_message: %s", e)
        await msg.reply_text("⚠️ Please try again in a moment.")
        return

    if not row:
        await msg.reply_text(
            "🔒 Please verify first.\nSend /start, join both channels and tap Verify. "
            "After that you can chat with the admin here."
        )
        return

    try:
        # Show a small header only if this user was silent for 60+ seconds
        # (so photo albums don't create many headers)
        last_header = context.bot_data.setdefault("last_header", {})
        if time.monotonic() - last_header.get(user.id, 0) > 60:
            header = await context.bot.send_message(
                ADMIN_ID,
                f"📩 <b>{name_of(row)}</b> ({username_of(row)})\n"
                f"🆔 <code>{user.id}</code> • 🔑 <code>{row['pin']}</code>\n"
                "<i>Reply to the message below to answer.</i>",
                parse_mode=ParseMode.HTML,
            )
            last_header[user.id] = time.monotonic()
            await run_db(db_save_map, header.message_id, user.id)

        # copy_message works for text, photo, video, document, voice, sticker...
        copied = await context.bot.copy_message(ADMIN_ID, msg.chat_id, msg.message_id)
        await run_db(db_save_map, copied.message_id, user.id)
        await react_ok(msg)
    except TelegramError as e:
        logger.error("Could not forward user message: %s", e)
        await msg.reply_text("⚠️ Could not deliver your message. Please try later.")
    except Exception as e:
        logger.error("Unexpected error forwarding message: %s", e)
        await msg.reply_text("⚠️ Something went wrong. Please try later.")


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The admin sends something -> deliver it to the right user."""
    msg = update.effective_message
    target = None

    # Way 1: admin REPLIES to a forwarded user message
    if msg.reply_to_message:
        try:
            target = await run_db(db_lookup_map, msg.reply_to_message.message_id)
        except Exception as e:
            logger.error("map lookup failed: %s", e)

    # Way 2: admin used /chat <ID>
    if target is None:
        target = context.bot_data.get("active_chat")

    if target is None:
        await msg.reply_text(
            "ℹ️ Reply to a user's message to answer, or use /chat <ID> first."
        )
        return

    try:
        await context.bot.copy_message(target, msg.chat_id, msg.message_id)
        await react_ok(msg)
    except Forbidden:
        await msg.reply_text("⚠️ This user has blocked the bot.")
    except TelegramError as e:
        logger.error("Could not send admin message: %s", e)
        await msg.reply_text(f"⚠️ Could not send: {e}")


async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route every non-command private message to the user or admin handler."""
    if update.effective_user.id == ADMIN_ID:
        await admin_message(update, context)
    else:
        await user_message(update, context)


# ---------------------------------------------------------------------
# Error handler + tiny health web server (for Render + cron-job.org)
# ---------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


class HealthHandler(BaseHTTPRequestHandler):
    """Answers 'OK' so cron-job.org can ping the app and keep Render awake."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # keep the console quiet
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server running on port %s", port)


# ---------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------
def main():
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    private = filters.ChatType.PRIVATE

    # Commands
    app.add_handler(CommandHandler("start", start_command, filters=private))
    app.add_handler(CommandHandler("admin", admin_command, filters=private))
    app.add_handler(CommandHandler("stats", stats_command, filters=private))
    app.add_handler(CommandHandler("user", user_command, filters=private))
    app.add_handler(CommandHandler("broadcast", broadcast_command, filters=private))
    app.add_handler(CommandHandler("chat", chat_command, filters=private))
    app.add_handler(CommandHandler("endchat", endchat_command, filters=private))

    # Button clicks
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern="^bc_"))

    # Any other private message (text, photo, video, file, voice...) = chat
    app.add_handler(
        MessageHandler(
            private & ~filters.COMMAND & ~filters.StatusUpdate.ALL, private_message
        )
    )

    app.add_error_handler(error_handler)

    # Python 3.12+ (and 3.14 on Render) no longer creates an event loop
    # automatically, so we create one ourselves before polling starts.
    asyncio.set_event_loop(asyncio.new_event_loop())

    logger.info("Bot started ✅")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
