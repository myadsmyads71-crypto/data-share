import os
import secrets
import string
from datetime import datetime

from supabase import create_client
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ADMIN_ID = int(os.environ["ADMIN_ID"])

CHANNEL1_ID = os.environ["CHANNEL1_ID"]
CHANNEL2_ID = os.environ["CHANNEL2_ID"]

CHANNEL1_LINK = os.environ["CHANNEL1_LINK"]
CHANNEL2_LINK = os.environ["CHANNEL2_LINK"]

db = create_client(SUPABASE_URL, SUPABASE_KEY)

# Users who have opted to submit content to admin
awaiting_submission = set()


# ================= PIN GENERATOR =================

def generate_pin():
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(6))


async def save_user(user, pin):
    data = {
        "telegram_id": user.id,
        "first_name": user.first_name or "",
        "username": user.username or "",
        "pin": pin,
        "verified": True,
    }

    db.table("telegram_users").insert(data).execute()


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    keyboard = [
        [InlineKeyboardButton("📢 Join Channel 1", url=CHANNEL1_LINK)],
        [InlineKeyboardButton("📢 Join Channel 2", url=CHANNEL2_LINK)],
        [InlineKeyboardButton("✅ Verify Membership", callback_data="verify")],
    ]

    await update.message.reply_text(
        f"Welcome {user.first_name}!\n\n"
        "PIN receive karne ke liye dono channels join karo.\n"
        "Join karne ke baad Verify button press karo.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ================= VERIFY =================

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    try:
        member1 = await context.bot.get_chat_member(
            CHANNEL1_ID, user.id
        )

        member2 = await context.bot.get_chat_member(
            CHANNEL2_ID, user.id
        )

        allowed = ["member", "administrator", "creator"]

        if member1.status not in allowed or member2.status not in allowed:
            await query.message.reply_text(
                "❌ Aap dono channels join nahi kiye.\n"
                "Join karke dobara Verify karo."
            )
            return

        # Check if user already has a PIN
        existing = (
            db.table("telegram_users")
            .select("*")
            .eq("telegram_id", user.id)
            .execute()
        )

        if existing.data:
            pin = existing.data[0]["pin"]

        else:
            pin = generate_pin()

            # Handle rare duplicate PIN collisions
            while True:
                try:
                    await save_user(user, pin)
                    break
                except Exception:
                    pin = generate_pin()

            # Notify admin about new PIN
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

            admin_text = (
                "🆕 New Verified User\n\n"
                f"Name: {user.first_name}\n"
                f"Username: @{user.username if user.username else 'None'}\n"
                f"Telegram ID: {user.id}\n"
                f"PIN: {pin}\n"
                f"Time: {now}"
            )

            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
            )

        # PIN with copy button
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy PIN",
                    copy_text=CopyTextButton(text=pin),
                )
            ]
        ]

        await query.message.reply_text(
            "✅ Membership verified!\n\n"
            f"Your unique PIN:\n`{pin}`\n\n"
            "PIN ko save kar lena.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        print("Verification error:", e)

        await query.message.reply_text(
            "⚠️ Verification error.\n"
            "Bot ko channel admin banaya gaya hai check karo."
        )


# ================= VOLUNTARY SUBMISSION =================

async def submit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ I agree – Submit",
                callback_data="agree_submit",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="cancel_submit",
            )
        ],
    ]

    await update.message.reply_text(
        "Submission notice:\n\n"
        "Agar aap Submit button press karte hain, "
        "toh aapke next message/photo/video/document ko "
        "admin ke paas bheja jayega.\n\n"
        "Submit karne se pehle confirm karo:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def submission_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "agree_submit":
        awaiting_submission.add(user_id)

        await query.message.reply_text(
            "✅ Submission enabled for your next message.\n"
            "Ab apna message, photo, video ya document bhejo.\n"
            "Cancel karne ke liye /cancel bhejo."
        )

    elif query.data == "cancel_submit":
        awaiting_submission.discard(user_id)

        await query.message.reply_text(
            "Submission cancelled."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting_submission.discard(update.effective_user.id)

    await update.message.reply_text(
        "Submission cancelled."
    )


async def receive_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in awaiting_submission:
        return

    awaiting_submission.discard(user.id)

    message = update.effective_message

    # Admin gets user details first
    details = (
        "📩 User voluntary submission\n\n"
        f"Name: {user.first_name}\n"
        f"Username: @{user.username if user.username else 'None'}\n"
        f"Telegram ID: {user.id}\n"
        f"Time: {datetime.now().astimezone()}"
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=details,
    )

    # Copy the submitted content to admin
    await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
    )

    await update.message.reply_text(
        "✅ Aapka submission admin ko bhej diya gaya."
    )


# ================= MAIN =================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(
        CallbackQueryHandler(
            submission_choice,
            pattern="^(agree_submit|cancel_submit)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            receive_submission,
        )
    )

    print("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
