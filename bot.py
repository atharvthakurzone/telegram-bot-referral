import datetime
import re
import os
import requests
import asyncio

from telegram.ext import ApplicationBuilder

# Your handler imports here
#from handlers import *  # if applicable
#from config import TOKEN

from db import get_connection

from cashfree import generate_payment_link

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from db import (
    init_db, add_user, get_user, get_referred_users,
    get_user_profile, get_user_by_uid, activate_user,
    is_user_activated, get_all_users, count_users,
    get_pending_users
)

from db import is_user_banned

from telegram import Bot

RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = 1469443288  # @Deep_1200

init_db()

async def clear_webhook():
    bot = Bot(token=TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)


# Set up webhook
PORT = int(os.environ.get("PORT", 8443))  # Render sets the PORT environment variable
#app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN)

def escape_markdown(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

#app = ApplicationBuilder().token(TOKEN).build()

webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
#requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}")

ASK_NAME, ASK_REFERRAL_CODE, ASK_NAME_WITH_REFERRAL, WAITING_FOR_SCREENSHOT = range(4)

def log_action(action: str, actor_id: int, target_id=None, details=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] 👤 {actor_id} → {action}"
    if target_id:
        log_msg += f" Target: {target_id}"
    if details:
        log_msg += f" | {details}"

    # Print to console
    print(log_msg)

    # Optionally write to file
    with open("admin_log.txt", "a") as f:
        f.write(log_msg + "\n")
        
def add_plus_referral_column():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS plus_referral_count INTEGER DEFAULT 0
            """)
            conn.commit()

# Run this ONCE at startup
add_plus_referral_column()

# Reply Keyboards
start_menu = ReplyKeyboardMarkup(
    [[KeyboardButton("📝 Register")], [KeyboardButton("🔗 Register by Referrer")]],
    resize_keyboard=True
)

main_menu = ReplyKeyboardMarkup([
    [KeyboardButton("🏠 Home"), KeyboardButton("👤 Profile"), KeyboardButton("💰 Wallet")],
    [KeyboardButton("🏦 Withdraw"), KeyboardButton("👥 Referrals")]
], resize_keyboard=True)

back_menu = ReplyKeyboardMarkup([
    [KeyboardButton("🔙 Back"), KeyboardButton("🏠 Home")]
], resize_keyboard=True)

admin_menu = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Pending Activations"), KeyboardButton("📊 Stats")],
    [KeyboardButton("🔍 Search User"), KeyboardButton("📤 Broadcast")],
    [KeyboardButton("🏠 Home")]
], resize_keyboard=True)


# Start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "🔐 Welcome Admin! Use the buttons below to manage the bot:",
            reply_markup=admin_menu
        )
    else:
        await update.message.reply_text(
            "Welcome to the Referral Bot! Please choose an option to continue:",
            reply_markup=start_menu
        )
    
    # Temporary Admin ID Checker
    
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = escape_markdown(str(user.id))
    username = f"@{escape_markdown(user.username)}" if user.username else "N/A"

    await update.message.reply_text(
        f"👤 Your Telegram ID is: `{telegram_id}`\n"
        f"📛 Username: {username}",
        parse_mode="MarkdownV2"
    )

# Register
async def handle_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if get_user(user.id):
        await update.message.reply_text("🛑 You are already registered!", reply_markup=main_menu)
        return
    context.user_data['register_mode'] = 'normal'
    await update.message.reply_text("📝 Please enter your name:", reply_markup=ReplyKeyboardRemove())
    return ASK_NAME

async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    username = name if name.lower() != "skip" else (user.username or user.first_name)
    add_user(user.id, username, None)
    await update.message.reply_text("✅ You’ve been registered!", reply_markup=main_menu)
    return ConversationHandler.END

# Referral Register
async def ask_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 Please enter the referral code (User ID of the referrer):", reply_markup=ReplyKeyboardRemove())
    return ASK_REFERRAL_CODE

async def handle_referral_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    user = update.effective_user
    if get_user(user.id):
        await update.message.reply_text("🛑 You are already registered!", reply_markup=main_menu)
        return ConversationHandler.END
    if not get_user_by_uid(code):
        await update.message.reply_text("❌ Invalid referral code. Try again.", reply_markup=start_menu)
        return ConversationHandler.END
    context.user_data['referred_by'] = code
    await update.message.reply_text("📝 Please enter your name:", reply_markup=ReplyKeyboardRemove())
    return ASK_NAME_WITH_REFERRAL

async def handle_name_with_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    username = name if name.lower() != "skip" else (user.username or user.first_name)
    referred_by = context.user_data.get("referred_by")
    new_uid = add_user(user.id, username, referred_by)
    referrer = get_user_by_uid(referred_by)
    if referrer:
        referrer_id = referrer[1]
        ref_msg = (
            f"🎉 Congratulations! "
            f"[{username}](tg://user?id={user.id}) (UID: {new_uid}) "
            f"has joined using your referral code and you've been rewarded with ₹100!"
        )
        await context.bot.send_message(referrer_id, ref_msg, parse_mode="Markdown")
    await update.message.reply_text("✅ You’ve been registered with a referral!", reply_markup=main_menu)
    return ConversationHandler.END

# Cancel
async def cancel_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Referral process cancelled.", reply_markup=start_menu)
    return ConversationHandler.END

# Wallet
async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(
            f"👤 Username: {user[2]}\n💰 Wallet: ₹{user[5]}\n🔗 Your referral code: {user[3]}",
            reply_markup=back_menu
        )
    else:
        await update.message.reply_text("❗ You are not registered. Use /start", reply_markup=start_menu)

# Referrals
async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user:
        code = user[3]
        users = get_referred_users(code)
        link = f"https://t.me/{context.bot.username}?start={code}"
        if users:
            lines = ["👥 Your Referrals:"]
            for username, tid, uid in users:
                display = username or "Unnamed"
                lines.append(f"[{display}](tg://user?id={tid}) (UID: {uid})")
            msg = "\n".join(lines)
        else:
            msg = f"👥 No referrals yet.\n🔗 Share your link:\n{link}"
        await update.message.reply_text(msg, reply_markup=back_menu)
    else:
        await update.message.reply_text("❗ You are not registered. Use /start", reply_markup=start_menu)

# Profile
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_user_profile(update.effective_user.id)
    if not data:
        await update.message.reply_text("❗ You are not registered. Use /start", reply_markup=start_menu)
        return
    ref_by = "N/A"
    if isinstance(data['referred_by'], dict):
        ref_by = f"[{data['referred_by']['username']}](tg://user?id={data['referred_by']['telegram_id']}) (UID: {data['referred_by']['uid']})"
    status = "✅ Activated" if data['activation_status'] else "❌ Not Activated"
    msg = (
        f"🆔 User ID: {data['user_uid']}\n"
        f"👤 Username: {data['username']}\n"
        f"🔗 Referral Code: {data['user_uid']}\n"
        f"🔓 Status: {status}\n"
        f"📅 Days Since Registration: {data['registered_days']}\n"
        f"💸 Earnings Days Completed: {data['earnings_days']} / 20\n"
        f"👤 Referred By: {ref_by}"
    )
    await update.message.reply_text(msg, reply_markup=back_menu, parse_mode="Markdown")

# Activate
async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not get_user(user.id):
        await update.message.reply_text("❗ You are not registered. Use /start")
        return

    if is_user_activated(user.id):
        await update.message.reply_text("✅ Your ID is already activated.")
        return

    username = user.username or user.first_name or "User"
    payment_url = generate_payment_link(user.id, username)

    if not payment_url:
        await update.message.reply_text("❌ Failed to generate payment link. Please try again later.")
        return

    context.user_data["awaiting_activation"] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay ₹999 Now", url=payment_url)],
        [InlineKeyboardButton("❌ Cancel", callback_data="activation_back")]
    ])

    await update.message.reply_text(
        "🙏 Kindly activate your account to start receiving earning benefits."
    )

    await update.message.reply_text(
        "💳 To activate your account, click the button below to pay ₹999 securely via Cashfree and upload the screenshot.",
        reply_markup=keyboard
    )

    await update.message.reply_text(
        "📌 After completing payment:\n\n"
        "1. Take a screenshot of payment success.\n"
        "2. Upload it here for admin to verify.\n\n"
        "_You will be activated after manual verification._",
        parse_mode="Markdown"
    )

    return WAITING_FOR_SCREENSHOT
    print("User is now awaiting activation")   # In activate()
    print("Screenshot received")               # In handle_screenshot()

# Screenshot Handler
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_activation"):
        return

    user = update.effective_user
    if not update.message.photo:
        await update.message.reply_text("❗ Please upload a valid payment screenshot.")
        return

    # Get user info from DB
    user_data = get_user(user.id)
    if not user_data:
        await update.message.reply_text("❗ You are not registered.")
        return

    uid = user_data[8]  # user_uid
    username = user_data[2] or "Unnamed"
    telegram_id = user_data[1]

    # Caption for admin
    caption = (
        f"🆕 Activation Request:\n"
        f"🆔 UID: {uid}\n"
        f"👤 Username: {username}\n"
        f"🧾 Telegram ID: {telegram_id}"
    )

    # Inline Approve / Reject buttons
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{uid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{uid}")
        ]
    ])

    # Send to admin
    photo_file = update.message.photo[-1].file_id
    await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=photo_file, caption=caption, reply_markup=buttons)

    # Notify user
    await update.message.reply_text("📩 Screenshot sent to admin. You'll be notified after verification.")
    context.user_data["awaiting_activation"] = False

    print("User is now awaiting activation") 
    print("📸 Screenshot received by handler")

# Admin Approve
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve <user_id>")
        return
    uid = context.args[0]
    user = get_user_by_uid(uid)
    if not user:
        await update.message.reply_text("❗ User not found.")
        return
    activate_user(user[1])
    await context.bot.send_message(chat_id=user[1], text="✅ Your account has been activated!")
    await update.message.reply_text(f"✅ Activated user UID: {uid}")

# Menu Handler
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Get the text early
    text = update.message.text.strip()

    # Handle edit field=value from admin
    if context.user_data.get("awaiting_profile_edit"):
        context.user_data["awaiting_profile_edit"] = False
        target_id = context.user_data.get("edit_target")

        if "=" not in text:
            await update.message.reply_text("❗ Invalid format. Use `field=value`.")
            return

        field, value = text.split("=", 1)
        field = field.strip()
        value = value.strip()

        valid_fields = ["username", "wallet", "referral_code", "activation_status", "plus_referral_count"]

        if field not in valid_fields:
            await update.message.reply_text(f"❗ Invalid field. You can edit: {', '.join(valid_fields)}")
            return

        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(f"UPDATE users SET {field} = %s WHERE telegram_id = %s", (value, target_id))
            conn.commit()
            cur.close()
            conn.close()
            await update.message.reply_text(
                f"✅ `{field}` updated successfully for user `{target_id}`.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❗ Error: `{e}`", parse_mode="Markdown")
        return

    # (continue with your existing menu logic here)

    # Check for broadcast message first
    if context.user_data.get("awaiting_broadcast"):
        context.user_data["awaiting_broadcast"] = False
        users = get_all_users()
        success = 0

        if update.message.text:
            for uid in users:
                try:
                    await context.bot.send_message(
                chat_id=uid,
                text=f"📢 *Attention!*\nThis is a broadcast message sent to all users:\n\n{update.message.text}",
            parse_mode="Markdown"
            )
                    success += 1
                except:
                    pass

        elif update.message.photo:
            photo = update.message.photo[-1].file_id
            caption = update.message.caption or ""
            for uid in users:
                try:
                    await context.bot.send_photo(
            chat_id=uid,
            photo=photo,
            caption=f"📢 *Attention!*\nThis is a broadcast message sent to all users:\n\n{caption}",
            parse_mode="Markdown"
            )

                    success += 1
                except:
                    pass

        elif update.message.document:
            file = update.message.document.file_id
            caption = update.message.caption or ""
            for uid in users:
                try:
                    await context.bot.send_document(
            chat_id=uid,
            document=file,
            caption=f"📢 *Attention!*\nThis is a broadcast message sent to all users:\n\n{caption}",
            parse_mode="Markdown"
            )

                    success += 1
                except:
                    pass

        else:
            await update.message.reply_text("❗ Unsupported message type for broadcast.")
            return

        await update.message.reply_text(f"📤 Broadcast sent to {success}/{len(users)} users.")
        return

    # Process normal user/admin menu options
    text = update.message.text.strip()

    # User options
    if text in ["🏠 Home", "🔙 Back"]:
        if not is_user_activated(update.effective_user.id):
            return await activate(update, context)
        await update.message.reply_text("🏠 Main Menu:", reply_markup=main_menu)

    elif text == "👤 Profile":
        await profile(update, context)

    elif text == "💰 Wallet":
        await wallet(update, context)

    elif text == "👥 Referrals":
        await referrals(update, context)

    elif text == "🏦 Withdraw":
        await update.message.reply_text("🔒 Withdraw feature coming soon!", reply_markup=back_menu)

    elif text == "📝 Register":
        return await handle_register(update, context)

    elif text == "🔗 Register by Referrer":
        return await ask_referral(update, context)

    # Admin options
    elif text == "📊 Stats" and update.effective_user.id == ADMIN_CHAT_ID:
        total = count_users()
        activated = sum(1 for uid in get_all_users() if is_user_activated(uid))
        await update.message.reply_text(
            f"📊 Bot Stats:\n👥 Total Users: {total}\n✅ Activated: {activated}\n❌ Not Activated: {total - activated}"
        )

    elif text == "🔍 Search User" and update.effective_user.id == ADMIN_CHAT_ID:
        context.user_data["awaiting_user_search"] = True
        await update.message.reply_text("🔎 Please send UID or Telegram ID to search:")

    elif context.user_data.get("awaiting_user_search"):
        await search_user(update, context)

    elif text == "📋 Pending Activations" and update.effective_user.id == ADMIN_CHAT_ID:
        await show_pending_activations(update, context)

    elif text == "📤 Broadcast" and update.effective_user.id == ADMIN_CHAT_ID:
        context.user_data["awaiting_broadcast"] = True
        await update.message.reply_text("📣 Send the message you want to broadcast (Text, Photo, or Document):")

    else:
        await update.message.reply_text("❓ Unknown option. Use /start", reply_markup=start_menu)
        
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def search_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_user_search"] = False
    query = update.message.text.strip()

    user = None
    if query.isdigit():
        user = get_user(int(query)) or get_user_by_uid(query)

    if not user:
        await update.message.reply_text("❗ User not found.")
        return

    profile = get_user_profile(user[1])  # telegram_id

    text = (
        f"👤 *User Profile:*\n"
        f"Username: `{profile['username']}`\n"
        f"UID: `{profile['user_uid']}`\n"
        f"Telegram ID: `{user[1]}`\n"
        f"Wallet: ₹{profile['wallet']}\n"
        f"Referrals: {profile['referral_count']}\n"
        f"Activation: {'✅ Active' if profile['activation_status'] else '❌ Not Active'}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Profile", callback_data=f"edit_{user[1]}")],
        [InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_{user[1]}")]
    ])

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    print(f"🔁 Callback received: {data}")

    # 🔙 Cancel activation from user
    if data == "activation_back":
        context.user_data["awaiting_activation"] = False
        await query.edit_message_text("❌ Activation cancelled.")
        await query.message.reply_text("🏠 Main Menu:", reply_markup=main_menu)

    # ✅ Admin approves user
    elif data.startswith("approve:"):
        uid = data.split(":")[1]
        user = get_user_by_uid(uid)
        if user:
            activate_user(user[1])
            await context.bot.send_message(chat_id=user[1], text="✅ Your account has been activated!")
            await query.edit_message_caption(
                caption=f"✅ Approved!\n\n{query.message.caption}",
                reply_markup=None
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ User not found.")

    # ❌ Admin rejects user
    elif data.startswith("reject:"):
        uid = data.split(":")[1]
        user = get_user_by_uid(uid)
        if user:
            await context.bot.send_message(chat_id=user[1], text="❌ Your activation request was rejected.")
            await query.edit_message_caption(
                caption=f"❌ Rejected!\n\n{query.message.caption}",
                reply_markup=None
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ User not found.")

    # 🚫 Ban User
    elif data.startswith("ban_"):
        telegram_id = int(data.split("_")[1])
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Ban", callback_data=f"confirmban_{telegram_id}"),
                InlineKeyboardButton("❌ No", callback_data="cancelban")
            ]
        ])
        await query.edit_message_text("⚠️ Are you sure you want to ban this user?", reply_markup=keyboard)

    elif data.startswith("confirmban_"):
        telegram_id = int(data.split("_")[1])
        try:
            ban_user(telegram_id)
            await context.bot.send_message(chat_id=telegram_id, text="🚫 You have been banned from using this bot.")
        except:
            pass
        await query.edit_message_text("✅ User has been banned.")

    elif data == "cancelban":
        await query.edit_message_text("❌ Ban cancelled.")

    # ✏️ Edit Profile
    elif data.startswith("edit_"):
        telegram_id = int(data.split("_")[1])
        context.user_data["edit_target"] = telegram_id
        context.user_data["awaiting_profile_edit"] = True
        await query.edit_message_text(
            "✏️ What would you like to update?\nSend in this format:\n`field=value`\n\nExample: `wallet=500`",
            parse_mode="Markdown"
        )
	
async def show_pending_activations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_pending_users()
    if not users:
        await update.message.reply_text("✅ No pending activations.")
        return
    for user in users[:5]:  # Show top 5
        uid = user[8]
        telegram_id = user[1]
        username = user[2] or "Unnamed"
        msg = f"🆔 UID: {uid}\n👤 Username: {username}\n📱 Telegram: {telegram_id}"
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{uid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{uid}")
            ]
        ])
        await update.message.reply_text(msg, reply_markup=buttons)
        
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_broadcast"):
        return
    context.user_data["awaiting_broadcast"] = False

    message = update.message.text
    users = get_all_users()
    success = 0

    for uid in users:
        try:
            await context.bot.send_message(uid, message)
            success += 1
        except:
            pass  # blocked or error

    await update.message.reply_text(f"📤 Broadcast sent to {success}/{len(users)} users.")

# Start Botasync def main():
app = ApplicationBuilder().token(TOKEN).build()

# Register conversation handler
conv_handler = ConversationHandler(
    entry_points=[
        MessageHandler(filters.TEXT & filters.Regex("^📝 Register$"), handle_register),
        MessageHandler(filters.TEXT & filters.Regex("^🔗 Register by Referrer$"), ask_referral)
    ],
    states={
        ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name)],
        ASK_REFERRAL_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_referral_code)],
        ASK_NAME_WITH_REFERRAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_with_referral)],
        WAITING_FOR_SCREENSHOT: [MessageHandler(filters.PHOTO, handle_screenshot)]
    },
    fallbacks=[MessageHandler(filters.Regex("^(🔙 Back|🏠 Home)$"), cancel_referral)],
)

# Register all handlers
    # 1. Commands
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("activate", activate))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("id", my_id))  # ✅ Fixed typo here

    # 2. Callback handlers
app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 3. Conversations
app.add_handler(conv_handler)

    # 4. Messages
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
app.add_handler(MessageHandler(filters.TEXT & filters.ALL, handle_broadcast))
app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))

# Start Botif __name__ == "__main__":
if __name__ == "__main__":
    import asyncio
    from telegram import Bot

    async def setup_webhook():
        bot = Bot(token=TOKEN)
        webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
        await bot.set_webhook(webhook_url)
        print(f"✅ Webhook set to: {webhook_url}")

    # Run webhook setup before starting the bot
    asyncio.run(setup_webhook())

    print("🤖 Bot is running with webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        url_path=TOKEN,
        webhook_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    )
