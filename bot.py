import datetime
import re
import os
import requests
import asyncio
import sys
import time


sys.stdout.reconfigure(line_buffering=True)

from telegram.ext import ApplicationBuilder

from telegram.constants import ChatAction

from telegram.ext import MessageHandler, filters, CallbackContext

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

def add_last_income_date_column():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_income_date DATE
            """)
            conn.commit()

add_last_income_date_column()

ASK_MOBILE = range(1000, 1001)
manual_payment_requests = {}  # Stores user payment details for admin use

async def clear_webhook():
    bot = Bot(token=TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)


# Set up webhook
PORT = int(os.environ.get("PORT", 8443))  # Render sets the PORT environment variable
#app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN)

def escape_markdown(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
#requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}")

ASK_NAME, ASK_REFERRAL_CODE, ASK_NAME_WITH_REFERRAL, WAITING_FOR_SCREENSHOT = range(4)

#Daily Income Scheduler
async def schedule_daily_income():
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()

        print(f"⏳ Waiting {int(wait_seconds)} seconds until next daily income...")
        await asyncio.sleep(wait_seconds)

        try:
            distribute_daily_income_once()
            print("✅ Daily income distributed.")
        except Exception as e:
            print(f"❌ Error distributing daily income: {e}")

# TEST VERSION: runs every 60 seconds
#async def schedule_daily_income():
 #   while True:
  #      print("⏳ Test: Distributing income in 60 seconds...")
   #     await asyncio.sleep(60)  # Run every 1 minute
#
 #       try:
  #          distribute_daily_income_once()
   #         print("✅ Test: Daily income distributed.")
    #    except Exception as e:
     #       print(f"❌ Test: Error distributing daily income: {e}")
		

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

from db import get_all_users, get_user, get_connection

PLAN_BENEFITS = {
    "Basic": {
        "daily_income": 100,
        "weekly_bonus": 250,
        "referral_bonus": 150
    },
    "Plus": {
        "daily_income": 300,
        "weekly_bonus": 600,
        "referral_bonus": 450
    },
    "Elite": {
        "daily_income": 700,
        "weekly_bonus": 1200,
        "referral_bonus": 950
    }
}

def distribute_daily_income_once():
    users = get_all_users()  
    today = datetime.date.today()
	
    # 🔍 Debug: Check what get_all_users() returns
    print("🔍 Sample users from get_all_users():")
    for u in users[:5]:
        print(u)
	    
    for telegram_id in users:
        print(f"➡️ Checking user: {telegram_id}")

        if not is_user_activated(telegram_id):
            print(f"⛔ Not activated: {telegram_id}")
            continue

        user = get_user(telegram_id)
        if not user:
            print(f"❌ User not found: {telegram_id}")
            continue

        plan = user[12] or "Basic"
        wallet = user[5]
        last_income_date = user[13]

        # ⏭️ Skip if already paid today
        if last_income_date == today:
            print(f"⏭️ Already paid today: {telegram_id}")
            continue
	    
        daily_income = PLAN_BENEFITS.get(plan, {}).get("daily_income", 0)
	
        print(f"📊 User: {telegram_id}, Plan: {plan}, Wallet: ₹{wallet}, Income: ₹{daily_income}")

        if daily_income > 0:
            new_wallet = wallet + daily_income
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET wallet = %s, last_income_date = %s WHERE telegram_id = %s",
                        (new_wallet, today, telegram_id)
                    )
                    conn.commit()
            print(f"💸 {telegram_id}: +₹{daily_income} (Plan: {plan})")

    print("✅ Daily income distributed to all users.")


def get_user_plan(telegram_id):
    """
    Fetch the user's plan details from the database.
    Returns a dict: {"name": plan_name, "amount": plan_amount}
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        return {"name": "None", "amount": 0}

    plan_name = user[12]  # index 12 stores plan
    # Set plan amount based on name
    plan_map = {
        "Basic": 1499,
        "Plus": 4499,
        "Elite": 9500
    }
    plan_amount = plan_map.get(plan_name, 0)

    return {"name": plan_name, "amount": plan_amount}

#Instant Payout Distributor
async def distribute_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized to perform this action.")
        return

    try:
        distribute_daily_income_once()
        await update.message.reply_text("✅ Daily income distribution triggered manually.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error distributing income: {e}")

# Reply Keyboards
start_menu = ReplyKeyboardMarkup(
    [[KeyboardButton("📝 Register")], [KeyboardButton("🔗 Register by Referrer")]],
    resize_keyboard=True
)

main_menu = ReplyKeyboardMarkup([
    [KeyboardButton("🏠 Home"), KeyboardButton("👤 Profile"), KeyboardButton("💰 Wallet")],
    [KeyboardButton("📄 Plans"), KeyboardButton("👥 Referrals")]
], resize_keyboard=True)

back_menu = ReplyKeyboardMarkup([
    [KeyboardButton("🔙 Back"), KeyboardButton("🏠 Home")]
], resize_keyboard=True)

admin_menu = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Pending Activations"), KeyboardButton("📊 Stats")],
    [KeyboardButton("🔍 Search User"), KeyboardButton("📤 Broadcast")],
    [KeyboardButton("🏠 Home")]
], resize_keyboard=True)


# /channel command handler (admin only)
CHANNEL_ID = "@zyncpayupdates"  # your channel username

async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text("📤 Send the message you want to post to the channel.\nUse as a reply to this command.")
        return

    text_to_post = " ".join(context.args)

    try:
        await context.bot.send_chat_action(chat_id=CHANNEL_ID, action=ChatAction.TYPING)
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text_to_post, parse_mode="HTML")
        await update.message.reply_text("✅ Message sent to the channel.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message: {e}")
	    

#Adds media support
async def forward_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID or not update.message:
        return

    try:
        await update.message.copy(chat_id=CHANNEL_ID)
        await update.message.reply_text("✅ Content forwarded to the channel.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to forward: {e}")


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
        referrer_plan = referrer[9] or "Basic"
        ref_bonus = PLAN_BENEFITS.get(referrer_plan, {}).get("referral_bonus", 0)

# Add bonus to wallet
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET wallet = wallet + %s WHERE telegram_id = %s", (ref_bonus, referrer[1]))
                conn.commit()

        ref_msg = (
            f"🎉 Congratulations! "
            f"[{username}](tg://user?id={user.id}) (UID: {new_uid}) "
            f"has joined using your referral code.\n"
            f"You’ve been rewarded with ₹{ref_bonus} as a *{referrer_plan}* user!"
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
        text_msg = (
            f"👤 Username: {user[2]}\n"
            f"💰 Wallet: ₹{user[5]}\n"
            f"🔗 Your referral code: {user[3]}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💸 Withdraw", callback_data="wallet_withdraw"),
                InlineKeyboardButton("📄 My Withdrawals", callback_data="wallet_history")
            ]
        ])

        await update.message.reply_text(text_msg, reply_markup=keyboard)
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

    context.user_data["awaiting_activation"] = True

    await update.message.reply_text(
        "Kindly activate your account to start receiving earning benefits."
    )

    if payment_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay ₹999 Now", url=payment_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="activation_back")]
        ])

        await update.message.reply_text(
            "💳 To activate your account, click the button below to pay ₹999 securely and upload the screenshot.",
            reply_markup=keyboard
        )

        await update.message.reply_text(
            "📌 After completing payment:\n\n"
            "1. Take a screenshot of payment success.\n"
            "2. Upload it here for admin to verify.\n\n"
            "_Your account will be activated after manual verification._",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        return WAITING_FOR_SCREENSHOT

    # Payment link failed – fallback flow
    plan_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Basic ₹1499", callback_data="plan_basic")],
        [InlineKeyboardButton("Plus ₹4499", callback_data="plan_plus")],
        [InlineKeyboardButton("Elite ₹9500", callback_data="plan_elite")],
        [InlineKeyboardButton("❌ Cancel", callback_data="activation_back")]
    ])

    await update.message.reply_text("Please select the plan below to activate your account", reply_markup=plan_keyboard)
    return ConversationHandler.END  # actual handling will continue via callback
	
# Screenshot Handler
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📸 handle_screenshot triggered")

    user = update.effective_user

    if not update.message.photo:
        print("❗ No photo found in message")
        await update.message.reply_text("❗ Please upload a valid payment screenshot.")
        return

    # Get user info from DB
    user_data = get_user(user.id)
    if not user_data:
        print(f"❗ User {user.id} not found in DB")
        await update.message.reply_text("❗ You are not registered.")
        return

    if is_user_activated(user.id):
        print(f"⛔ User {user.id} is already activated — ignoring screenshot")
        await update.message.reply_text("✅ Your account is already activated. No need to upload a screenshot.")
        return

    if not context.user_data.get("awaiting_activation"):
        print("⚠️ Not awaiting activation, but continuing...")

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

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Basic", callback_data=f"approve_basic:{uid}"),
	    InlineKeyboardButton("💎 Plus", callback_data=f"approve_plus:{uid}"),
	],		
        [
            InlineKeyboardButton("👑 Elite", callback_data=f"approve_elite:{uid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{uid}")
        ]
    ])

    # Send to admin
    photo_file = update.message.photo[-1].file_id
    print(f"📤 Sending photo to admin {ADMIN_CHAT_ID} (file_id: {photo_file})")

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo_file,
            caption=caption,
            reply_markup=buttons
        )
        print("✅ Photo sent to admin")
        await update.message.reply_text("📩 Screenshot sent to admin. You'll be notified after verification.")
    except Exception as e:
        print(f"❌ Error sending photo to admin: {e}")
        await update.message.reply_text("❌ Failed to send screenshot to admin. Please try again later.")
        return

    context.user_data["awaiting_activation"] = False
    await update.message.reply_text("✅ Screenshot received and is under review.")
    print("📸 Screenshot handler completed")

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
	    
        # 🔗 Admin is expected to send a payment link to a user
    if context.user_data.get("awaiting_payment_link_for"):
        target_id = context.user_data["awaiting_payment_link_for"]
        del context.user_data["awaiting_payment_link_for"]

        selected_plan = manual_payment_requests.get(target_id, {})
        plan_name = selected_plan.get("name", "Unknown")
        plan_amount = selected_plan.get("amount", 0)
        mobile = selected_plan.get("mobile", "N/A")

        message_text = update.message.text.strip()

        if not message_text.startswith("https"):
            await update.message.reply_text("❗ Please send a valid payment *link* (starting with https).", parse_mode="Markdown")
            return

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"💳 Please use the link below to make the payment for your selected plan "
                    f"(*{plan_name} – ₹{plan_amount}*).\n\n"
                    f"🔗 {message_text}\n\n"
		    f"This link has also been shared to your mobile number: `{mobile}`"
                ),
                parse_mode="Markdown"
            )
            await update.message.reply_text("✅ Payment link forwarded to user.")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send link: `{e}`", parse_mode="Markdown")
        return

    # Get the text early
    text = update.message.text.strip()

        # Step: User submitting mobile number for manual payment
    if context.user_data.get("awaiting_mobile_number"):
        context.user_data["awaiting_mobile_number"] = False
        mobile = text.strip()

        if not mobile.isdigit() or len(mobile) != 10:
            await update.message.reply_text("❗ Please enter a valid 10-digit mobile number.")
            return

        user = get_user(update.effective_user.id)
        if not user:
            await update.message.reply_text("❗ You are not registered.")
            return

        if update.effective_user.id in manual_payment_requests:
            manual_payment_requests[update.effective_user.id]["mobile"] = mobile

        selected_plan = context.user_data.get("selected_plan", {})
        plan_name = selected_plan.get("name", "Unknown")
        plan_amount = selected_plan.get("amount", 0)

        uid = user[8]
        username = user[2] or "Unnamed"
        telegram_id = user[1]

        # Prepare admin message
        caption = (
            f"🧾 *Manual Activation Request*\n\n"
            f"🆔 UID: `{uid}`\n"
            f"👤 Username: {username}\n"
            f"📱 Telegram ID: `{telegram_id}`\n"
            f"💳 Plan: *{plan_name}* (₹{plan_amount})\n"
            f"📞 Mobile: `{mobile}`"
        )

        button = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send Payment Link", callback_data=f"sendlink_{telegram_id}")]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=caption,
            parse_mode="Markdown",
            reply_markup=button
        )

        await update.message.reply_text(
            "✅ Thank you for sharing the mobile number. Payment link will be shared with you in the next 15 minutes."
        )
        return

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
		
            if field == "activation_status" and value.lower() in ["false", "0", "no"]:
		# Deactivate user and clear plan
                cur.execute("UPDATE users SET activation_status = FALSE, plan = NULL WHERE telegram_id = %s", (target_id,))
            else:
                # Normal field update
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

    elif text == "📄 Plans":
        if is_user_activated(update.effective_user.id):
            # Active user flow
            user_plan = get_user_plan(update.effective_user.id)  # fetch user's plan
    
            # Plan-specific details
            plan_details = {
                "Basic": {
                    "emoji": "✅",
                    "amount": 1499,
                    "daily": "₹100/-",
                    "weekly": "₹250/- (Every 4th week)",
                    "referral": "According to the plan of the newly joined user (10% of the plan)"
                },
                "Plus": {
                    "emoji": "💎",
                    "amount": 4499,
                    "daily": "₹300/-",
                    "weekly": "₹600/- (Every 4th week)",
                    "referral": "According to the plan of the newly joined user (12% of the plan)"
                },
                "Elite": {
                    "emoji": "👑",
                    "amount": 9500,
                    "daily": "₹750/-",
                    "weekly": "₹1200/- (Every 4th week)",
                    "referral": "According to the plan of the newly joined user (15% of the plan)"
                }
            }
    
            plan_name = user_plan.get('name') or user_plan.get('plan_name') or "Unknown"
            details = plan_details.get(plan_name, {})
            emoji = details.get("emoji", "")
            amount = details.get("amount", 0)
            daily_income = details.get("daily", "According to plan")
            weekly_bonus = details.get("weekly", "According to plan")
            referral_bonus = details.get("referral", "According to plan")
    
            text_msg = (
                f"My Plan:\n"
                f"{emoji} {plan_name} - ₹{amount}\n\n"
                f"Duration - Not Defined.\n"
                f"Daily Income - {daily_income}\n"
                f"Weekly Bonus - {weekly_bonus}\n"
                f"Referral Bonus - {referral_bonus}"
            )
    
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("See Other Plans", callback_data="see_other_plans")]]
            )
            await update.message.reply_text(text_msg, reply_markup=keyboard)
    
        else:
            # Inactive user flow
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Basic", callback_data="show_plan_basic")],
                [InlineKeyboardButton("💎 Plus", callback_data="show_plan_plus")],
                [InlineKeyboardButton("👑 Elite", callback_data="show_plan_elite")]
            ])
            await update.message.reply_text("Choose a plan to see details:", reply_markup=keyboard)


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


# Callback query
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

    # 👇 Plan selected by user after payment link failure
    elif data.startswith("plan_"):
        plan_map = {
            "plan_basic": ("Basic", 1499),
            "plan_plus": ("Plus", 4499),
            "plan_elite": ("Elite", 9500)
        }
        plan_key = data
        plan_name, plan_amount = plan_map.get(plan_key, ("Unknown", 0))

        if plan_name == "Unknown":
            await query.message.reply_text("⚠️ Invalid plan selected. Please try again.")
            return

        telegram_id = query.from_user.id  # ✅ fixed placement

        manual_payment_requests[telegram_id] = {
            "name": plan_name,
            "amount": plan_amount
        }
        context.user_data["awaiting_mobile_number"] = True

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"📱 Please enter your mobile number to receive the payment link for the *{plan_name}* plan (₹{plan_amount}).",
            parse_mode="Markdown"
        )

    # 👇 Admin clicked "Send payment link"
    elif data.startswith("sendlink_"):
        target_id = int(data.split("_")[1])
        context.user_data["awaiting_payment_link_for"] = target_id
        await query.message.reply_text("✉️ Please send the payment link to forward to the user.")

    elif data.startswith("approve_basic:") or data.startswith("approve_plus:") or data.startswith("approve_elite:"):
        plan = data.split(":")[0].replace("approve_", "").capitalize()
        uid = data.split(":")[1]
        user = get_user_by_uid(uid)
        if user:
            activate_user(user[1])  # Activate the user normally
            # Update user plan in DB
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("UPDATE users SET plan = %s WHERE telegram_id = %s", (plan, user[1]))
            conn.commit()
            cur.close()
            conn.close()

	
            await context.bot.send_message(chat_id=user[1], text=f"✅ Your account has been activated with the *{plan}* plan!")
            await query.edit_message_caption(
                caption=f"✅ Approved with {plan} Plan!\n\n{query.message.caption}",
                reply_markup=None
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ User not found.")


    elif data == "see_other_plans":
        telegram_id = query.from_user.id
        current_plan = get_user_plan(telegram_id)['name']

        plan_details = {
            "Basic": {"emoji": "✅", "amount": 1499, "daily": "₹100/-", "weekly": "₹250/- (Every 4th week)", "referral": "According to the plan of the newly joined user (10% of the plan)"},
            "Plus": {"emoji": "💎", "amount": 4499, "daily": "₹300/-", "weekly": "₹600/- (Every 4th week)", "referral": "According to the plan of the newly joined user (12% of the plan)"},
            "Elite": {"emoji": "👑", "amount": 9500, "daily": "₹750/-", "weekly": "₹1200/- (Every 4th week)", "referral": "According to the plan of the newly joined user (15% of the plan)"}
        }

        # Show all plans other than current
        keyboard_buttons = []
        for plan_name, details in plan_details.items():
            if plan_name != current_plan:
                keyboard_buttons.append(
                    [InlineKeyboardButton(f"{details['emoji']} {plan_name} - ₹{details['amount']}", callback_data=f"show_plan_{plan_name.lower()}")]
                )

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await query.edit_message_text("Choose a plan to see details:", reply_markup=keyboard)


    elif data.startswith("show_plan_"):
        plan_name = data.replace("show_plan_", "").capitalize()
        telegram_id = query.from_user.id

        plan_details = {
            "Basic": {"emoji": "✅", "amount": 1499, "daily": "₹100/-", "weekly": "₹250/- (Every 4th week)", "referral": "According to the plan of the newly joined user (10% of the plan)"},
            "Plus": {"emoji": "💎", "amount": 4499, "daily": "₹300/-", "weekly": "₹600/- (Every 4th week)", "referral": "According to the plan of the newly joined user (12% of the plan)"},
            "Elite": {"emoji": "👑", "amount": 9500, "daily": "₹750/-", "weekly": "₹1200/- (Every 4th week)", "referral": "According to the plan of the newly joined user (15% of the plan)"}
        }

        details = plan_details.get(plan_name)
        if not details:
            await query.answer("⚠️ Plan not found.", show_alert=True)
            return

        text_msg = (
            f"{details['emoji']} *{plan_name} Plan*\n\n"
            f"💰 Price: ₹{details['amount']}\n"
            f"📅 Daily Income: {details['daily']}\n"
            f"📅 Weekly Bonus: {details['weekly']}\n"
            f"👥 Referral Bonus: {details['referral']}"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Plans", callback_data="see_other_plans")],
            [InlineKeyboardButton("✅ Select This Plan", callback_data=f"plan_{plan_name.lower()}")]
        ])

        await query.edit_message_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")


#Pending account activation	
async def show_pending_activations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_pending_users()
    if not users:
        await update.message.reply_text("✅ No pending activations.")
        return
    for user in users[:5]:
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


#Hnadle Broadcast Messages
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📢 handle_broadcast triggered")
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

# Start Botasync def main
async def setup_webhook(app):
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    await app.bot.set_webhook(webhook_url)
    print(f"✅ Webhook set to: {webhook_url}")

# ✅ Start the daily income scheduler
    asyncio.create_task(schedule_daily_income())

app = ApplicationBuilder().token(TOKEN).post_init(setup_webhook).build()


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
app.add_handler(CommandHandler("id", my_id))
app.add_handler(CommandHandler("distribute_now", distribute_now))
app.add_handler(CommandHandler("channel", channel_command))

    # 2. Callback handlers
app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 3. Conversations
app.add_handler(conv_handler)

    # 4. Messages
app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_CHAT_ID), forward_to_channel))
#app.add_handler(MessageHandler(filters.TEXT & filters.ALL, handle_broadcast))

async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"📩 Raw update: {update}", flush=True)
	
app.add_handler(MessageHandler(filters.ALL, log_all_updates))


# Start bot with webhook

if __name__ == "__main__":
    print("🤖 Bot is running with webhook...", flush=True)
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        url_path=TOKEN,
        webhook_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    )


