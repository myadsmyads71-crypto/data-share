"""
Telegram Verify + PIN Bot  (python-telegram-bot >= 21.7, supabase-py)

ENV VARS (Render me daalna):
  BOT_TOKEN, ADMIN_ID, SUPABASE_URL, SUPABASE_KEY (service_role key),
  CH1 (@channel1 username), CH2 (@channel2 username)
  PORT (Render khud deta hai)

Supabase SQL (SQL Editor me chalao):
  create table if not exists bot_users (
    user_id bigint primary key,
    name text,
    username text,
    pin text unique not null,
    created_at timestamptz default now()
  );
  alter table bot_users enable row level security;  -- service_role key RLS bypass karti hai
"""
import os, asyncio, secrets, string, threading, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

from supabase import create_client
from telegram import (Update, InlineKeyboardButton as Btn,
                      InlineKeyboardMarkup as Markup, CopyTextButton)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CH1 = os.environ["CH1"]  # e.g. @mychannel1
CH2 = os.environ["CH2"]  # e.g. @mychannel2
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
IST = ZoneInfo("Asia/Kolkata")


# ---------- Supabase helpers (blocking -> thread) ----------
def _get_user(uid):
    r = sb.table("bot_users").select("*").eq("user_id", uid).limit(1).execute()
    return r.data[0] if r.data else None


def _pin_exists(pin):
    r = sb.table("bot_users").select("pin").eq("pin", pin).limit(1).execute()
    return bool(r.data)


def _gen_pin():
    chars = string.ascii_uppercase + string.digits
    while True:
        pin = "".join(secrets.choice(chars) for _ in range(6))
        # kam se kam 1 number + 1 letter
        if any(c.isdigit() for c in pin) and any(c.isalpha() for c in pin) \
                and not _pin_exists(pin):
            return pin


def _save_user(uid, name, username, pin):
    sb.table("bot_users").insert({
        "user_id": uid, "name": name, "username": username, "pin": pin
    }).execute()


# ---------- Helpers ----------
def join_markup():
    return Markup([
        [Btn("📢 Join Channel 1", url=f"https://t.me/{CH1.lstrip('@')}")],
        [Btn("📢 Join Channel 2", url=f"https://t.me/{CH2.lstrip('@')}")],
        [Btn("✅ Verify", callback_data="verify")],
    ])


async def is_joined(bot, uid):
    for ch in (CH1, CH2):
        try:
            m = await bot.get_chat_member(ch, uid)
            if m.status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            logging.warning("check fail %s: %s", ch, e)
            return False
    return True


def pin_markup(pin):
    return Markup([[Btn("📋 Copy PIN", copy_text=CopyTextButton(text=pin))]])


# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    row = await asyncio.to_thread(_get_user, u.id)
    if row:
        await update.message.reply_text(
            f"✅ Aap already verified ho.\n\n🔑 Your PIN: <code>{row['pin']}</code>\n\n"
            "Ab aap yahan message, photo ya video bhej sakte ho.",
            parse_mode=ParseMode.HTML, reply_markup=pin_markup(row["pin"]))
        return
    await update.message.reply_text(
        f"👋 Hello <b>{u.first_name}</b>!\n\nPehle neeche ke <b>2 channels</b> join karo, "
        "phir <b>Verify</b> dabao 👇",
        parse_mode=ParseMode.HTML, reply_markup=join_markup())


async def verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user

    if not await is_joined(ctx.bot, u.id):
        await q.answer("❌ Pehle dono channel join karo!", show_alert=True)
        return

    row = await asyncio.to_thread(_get_user, u.id)
    new = row is None
    if new:
        pin = await asyncio.to_thread(_gen_pin)
        await asyncio.to_thread(_save_user, u.id, u.full_name, u.username, pin)
    else:
        pin = row["pin"]

    await q.answer("✅ Verified!")
    await q.edit_message_text(
        f"🎉 <b>Verification Successful!</b>\n\n🔑 Your PIN: <code>{pin}</code>\n\n"
        "Ab aap message, photo ya video bhej sakte ho.",
        parse_mode=ParseMode.HTML, reply_markup=pin_markup(pin))

    if new:
        now = datetime.now(IST)
        await ctx.bot.send_message(
            ADMIN_ID,
            "🆕 <b>New Verified User</b>\n\n"
            f"👤 Name: {u.full_name}\n"
            f"🆔 ID: <code>{u.id}</code>\n"
            f"🔗 Username: @{u.username or 'N/A'}\n"
            f"🔑 PIN: <code>{pin}</code>\n"
            f"🕒 Time: {now.strftime('%I:%M %p')}\n"
            f"📅 Date: {now.strftime('%d-%m-%Y')}",
            parse_mode=ParseMode.HTML)


async def forward_to_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id == ADMIN_ID:
        return
    row = await asyncio.to_thread(_get_user, u.id)
    if not row:
        await update.message.reply_text("⚠️ Pehle /start karke verify karo.")
        return
    await ctx.bot.send_message(
        ADMIN_ID,
        f"📩 From: {u.full_name} | <code>{u.id}</code> | PIN: <code>{row['pin']}</code>",
        parse_mode=ParseMode.HTML)
    await update.message.copy(ADMIN_ID)
    await update.message.reply_text("✅ Aapka message admin ko bhej diya gaya.")


# ---------- Mini web server (Render + cron-job.org ping ke liye) ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def run_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Ping).serve_forever()


def main():
    threading.Thread(target=run_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, forward_to_admin))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
