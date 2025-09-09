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

from telegram import Update

from telegram.ext import MessageHandler, filters, CallbackContext

from db import get_connection

from telegram import (
    Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from db import (
    init_db, init_withdrawals_table, add_user, get_user, get_referred_users,
    get_user_profile, unban_user, is_user_banned, get_user_by_uid, ban_user, activate_user,
    is_user_activated, get_all_users, count_users,
    get_pending_users
)

from db import is_user_banned

from telegram import Bot

RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))

init_db()
init_withdrawals_table()

def add_last_income_date_column():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS last_income_date DATE
            """)
            conn.commit()

add_last_income_date_column()

# --- Update wallet balance in DB ---
def update_wallet_balance(telegram_id: int, new_balance: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET wallet = %s WHERE telegram_id = %s",
                (new_balance, telegram_id)
            )
            conn.commit()

def get_withdrawals_by_user(user_uid):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT amount, status, created_at
                FROM withdrawals
                WHERE user_uid = %s
                ORDER BY created_at DESC
            ''', (user_uid,))
            return cur.fetchall()


ASK_AMOUNT, ASK_MOBILE, ASK_UPI = range(3)
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

# Commands Function
async def ban(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    if not context.args:
        return await update.message.reply_text("❗ Usage: /ban <user_uid>")
    
    user_uid = context.args[0]
    user = get_user_by_uid(user_uid)
    if not user:
        return await update.message.reply_text("❌ User not found!")

    ban_user(user[8])  # user[8] = telegram_id
    await update.message.reply_text(f"🔒 User {user_uid} has been banned.")

async def unban(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    if not context.args:
        return await update.message.reply_text("❗ Usage: /unban <user_uid>")
    
    user_uid = context.args[0]
    user = get_user_by_uid(user_uid)
    if not user:
        return await update.message.reply_text("❌ User not found!")

    unban_user(user[8])
    await update.message.reply_text(f"🔓 User {user_uid} has been unbanned.")

async def userinfo(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    if not context.args:
        return await update.message.reply_text("❗ Usage: /userinfo <user_uid>")

    user_uid = context.args[0]
    user = get_user_by_uid(user_uid)
    if not user:
        return await update.message.reply_text("❌ User not found!")

    banned_status = "Yes" if user[11] else "No"  # banned column
    await update.message.reply_text(
        f"📝 Info for {user_uid}:\n"
        f"Username: {user[2]}\n"
        f"Wallet: {user[5]}\n"
        f"Activated: {'Yes' if user[9] else 'No'}\n"
        f"Banned: {banned_status}"
    )

# States
AWAIT_MESSAGE = 1

# Step 1: /dm command
async def dm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    if len(context.args) < 1:
        return await update.message.reply_text("❗ Usage: /dm <user_uid>")

    user_uid = context.args[0]
    user = get_user_by_uid(user_uid)
    if not user:
        return await update.message.reply_text("❌ User not found!")

    # Save the target user_uid in context
    context.user_data["dm_target_uid"] = user_uid

    await update.message.reply_text(
        "Kindly enter the message you want to send to the user:\n"
        "You can use Markdown formatting (bold, italic, links, etc.)"
    )
    return AWAIT_MESSAGE  # wait for next message

# Step 2: capture the admin’s reply and send DM with Markdown
async def dm_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_uid = context.user_data.get("dm_target_uid")
    if not user_uid:
        return await update.message.reply_text(
            "❌ DM session expired. Please start again with /dm <user_uid>."
        )

    message_text = update.message.text
    user = get_user_by_uid(user_uid)
    if not user:
        context.user_data.pop("dm_target_uid", None)
        return await update.message.reply_text("❌ User not found!")

    try:
        await context.bot.send_message(
            chat_id=user[1], 
            text=message_text,
            parse_mode="Markdown"  # ✅ Enable Markdown formatting
        )
        await update.message.reply_text(f"✉️ Message sent to {user_uid}")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not send message to {user_uid}. Error: {e}"
        )
    finally:
        # Clear the state
        context.user_data.pop("dm_target_uid", None)

    return ConversationHandler.END

# Optional cancel command
async def dm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("dm_target_uid", None)
    await update.message.reply_text("❌ DM cancelled.")
    return ConversationHandler.END
	

# ==========================
# Reports & Tracking
# ==========================
async def last10(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")
    
    users = get_all_users()[-10:]
    await update.message.reply_text("🕒 Last 10 registered users:\n" + "\n".join(map(str, users)))

async def pending(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    pending_users = get_pending_users()
    await update.message.reply_text(f"⏳ Pending users ({len(pending_users)}):\n" +
                                    "\n".join([str(u[1]) for u in pending_users]))

async def active(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    # Fetch active users from DB
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, user_uid FROM users WHERE activation_status = TRUE")
            active_users = cur.fetchall()

    if not active_users:
        await update.message.reply_text("✅ No active users found.")
        return

    text = "\n".join([f"{user[0] or 'User'} ({user[1]})" for user in active_users])
    await update.message.reply_text(f"✅ Active users ({len(active_users)}):\n{text}")


async def inactive(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    # Fetch inactive users from DB
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, user_uid FROM users WHERE activation_status = FALSE")
            inactive_users = cur.fetchall()

    if not inactive_users:
        await update.message.reply_text("❌ No inactive users found.")
        return

    text = "\n".join([f"{user[0] or 'User'} ({user[1]})" for user in inactive_users])
    await update.message.reply_text(f"❌ Inactive users ({len(inactive_users)}):\n{text}")

# ==========================
# Communication
# ==========================
async def notify(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    if not context.args:
        return await update.message.reply_text("❗ Usage: /notify <message>")
    
    message_text = " ".join(context.args)
    failed_ids, sent = [], 0

    for user in get_all_users():
        user_id = user[1]  # telegram_id
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📣 {message_text}")
            sent += 1
        except Exception as e:
            failed_ids.append(user_id)
            print(f"⚠️ Could not send to {user_id}: {e}")

    await update.message.reply_text(
        f"📣 Notification finished.\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {len(failed_ids)}"
    )
    if failed_ids:
        print("🚫 Failed telegram_ids:", failed_ids)


async def remind(update, context):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("🚫 You are not authorized!")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Custom Message", callback_data="remind_custom")],
        [InlineKeyboardButton("📋 Use Template", callback_data="remind_template")]
    ])

    await update.message.reply_text(
        "⏰ Choose how you want to remind inactive users:",
        reply_markup=keyboard
    )

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

                                                
POLICY_LINK = "https://drive.google.com/file/d/158EFh9JwONWSZgACiesNtWuL2teeKgaX/view"

        # Create keyboard with your existing policy link
policy_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "📜 Referral Policy", 
        web_app=WebAppInfo(url=POLICY_LINK)
    )]
])

async def policy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📄 View ZyncPay Policy",
            web_app=WebAppInfo(url=POLICY_LINK)
        )]
    ])
    await update.message.reply_text(
        "Click the button below to view the latest ZyncPay Withdrawal Policy & Bonus Terms:",
        reply_markup=keyboard
    )

app = ApplicationBuilder().token(TOKEN).build()
# Admin WebApp button
admin_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "💬 Open Support Dashboard",
        web_app=WebAppInfo(url="https://dashboard.tawk.to/#/monitoring")
    )]
])

async def support_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    await update.message.reply_text(
        "Welcome Admin! Access your support panel below:",
        reply_markup=admin_keyboard
    )

app = ApplicationBuilder().token(TOKEN).build()

# Global keyboard for support chat
support_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "💬 Chat with Support",
        web_app=WebAppInfo(url="https://atharvthakurzone.github.io/pay-now/")
    )]
])

# Optional test command
async def test_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Testing Support Chat UI. Click the button below:",
        reply_markup=support_keyboard
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

	
#due weekly bonus       
def is_weekly_bonus_due(telegram_id):
    """Check if weekly bonus is due for payment"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT plan_activation_date, last_income_date 
            FROM users 
            WHERE telegram_id = %s
        """, (telegram_id,))
        
        result = cur.fetchone()
        if not result or not result[0]:  # No activation date
            return False
            
        activation_date, last_income_date = result
        today = datetime.date.today()
        
        # Calculate days since activation
        days_since_activation = (today - activation_date).days
        
        # Bonus is due every 28 days (4 weeks)
        # And if we haven't paid it in the current cycle
        if days_since_activation % 28 == 0:
            if not last_income_date or last_income_date < today:
                return True
                
        return False
        
    except Exception as e:
        print(f"Error checking weekly bonus: {e}")
        return False
    finally:
        cur.close()
        conn.close()

#weekly bonus orogress
def get_weekly_bonus_progress(telegram_id):
    user_plan_info = get_user_plan(telegram_id)
    activation_date = user_plan_info.get("activation_date")  # should come from DB column
    
    if not activation_date:
        return "0 / 28"
    
    # Convert string date from DB -> datetime
    if isinstance(activation_date, str):
        activation_date = datetime.strptime(activation_date, "%Y-%m-%d")
    
    today = datetime.now()
    days_passed = (today - activation_date).days
    
    if days_passed < 0:
        days_passed = 0
    
    # cap at 28 days
    progress_days = min(days_passed, 28)
    
    return f"{progress_days} / 28"

#active referred users
def get_active_referred_users(referrer_uid):
    """
    Get activated users who were referred by this UID and their plans
    Returns: List of tuples (telegram_id, username, plan)
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        
        # Get activated users referred by this UID
        cur.execute("""
            SELECT telegram_id, username, plan 
            FROM users 
            WHERE referred_by = %s AND activation_status = TRUE
        """, (referrer_uid,))
        
        active_users = cur.fetchall()
        return active_users
        
    except Exception as e:
        print(f"Error getting active referred users: {e}")
        return []
    finally:
        cur.close()
        conn.close()


#log keeper
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
			

def add_activation_date_column():
    """Add column to track when the user's plan was activated"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS plan_activation_date DATE
            """)
            conn.commit()

# Run this at startup
add_activation_date_column()

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
        "daily_income": 750,
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

        # Check if weekly bonus is due
        weekly_bonus = 0
        if is_weekly_bonus_due(telegram_id):
            weekly_bonus = PLAN_BENEFITS.get(plan, {}).get("weekly_bonus", 0)
            print(f"🎉 Weekly bonus of ₹{weekly_bonus} for {telegram_id}")

        total_income = daily_income + weekly_bonus

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
    [KeyboardButton("⚡ Commands"), KeyboardButton("📊 Stats")],
    [KeyboardButton("🔍 Search User"), KeyboardButton("📤 Broadcast")],
    [KeyboardButton("🏠 Home")]
], resize_keyboard=True)


# /channel command handler (admin only)
#CHANNEL_ID = "@zyncpayupdates"  # your channel username
#
#async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
#    user_id = update.effective_user.id
#    if user_id != ADMIN_CHAT_ID:
#        await update.message.reply_text("❌ You are not authorized to use this command.")
#        return
#
#    if not context.args:
#        await update.message.reply_text("📤 Send the message you want to post to the channel.\nUse as a reply to this command.")
#        return
#
#    text_to_post = " ".join(context.args)
#
#    try:
#        await context.bot.send_chat_action(chat_id=CHANNEL_ID, action=ChatAction.TYPING)
#        await context.bot.send_message(chat_id=CHANNEL_ID, text=text_to_post, parse_mode="HTML")
#        await update.message.reply_text("✅ Message sent to the channel.")
#    except Exception as e:
#        await update.message.reply_text(f"❌ Failed to send message: {e}")


# Withdraw
async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "wallet_withdraw":
        user = get_user(query.from_user.id)
        if not user:
            await query.message.reply_text("❗ You are not registered. Use /start")
            return

        user_uid = user[8]  # user’s UID
        active_referred_users = get_active_referred_users(user_uid)
        active_referrals_count = len(active_referred_users) if active_referred_users else 0

        if active_referrals_count >= 1:
            # ✅ Start withdrawal conversation flow
            return await withdraw_start(update, context)  

        else:
            # ❌ Withdrawal locked
            await query.message.reply_text(
                f"💸 *Withdraw feature is locked!*\n\n"
                f"To unlock this feature, refer the app to at least 1 user. "
                f"Also make sure the new user who joined using your referral code should activate their account with any plan.\n\n"
                f"✅ Active Referred User - {active_referrals_count}/1",
                parse_mode="Markdown"
            )

    elif query.data == "wallet_history":
        user = get_user(query.from_user.id)
        if not user:
            await query.message.reply_text("❗ You are not registered. Use /start")
            return

        user_uid = user[8]

        try:
            from db import get_connection  # your DB connection function

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('''
                        SELECT amount, mobile, upi, status, created_at
                        FROM withdrawals
                        WHERE user_uid = %s
                        ORDER BY created_at DESC
                        LIMIT 10
                    ''', (user_uid,))
                    rows = cur.fetchall()

            if not rows:
                await query.message.reply_text("📄 You have no withdrawal history yet.")
                return

            history_text = "📄 *Your Last Withdrawals:*\n\n"
            for idx, row in enumerate(rows, start=1):
                amount, mobile, upi, status, created_at = row
                amount = f"{amount:,}"  # ✅ fix indentation
                # Add emojis for status
                status_emoji = "✅" if status == "approved" else "❌" if status == "rejected" else "⏳"
                history_text += (
                    f"{idx}️⃣\n"
                    f"💰 Amount: ₹{amount}\n"
                    f"📞 Mobile: {mobile}\n"
                    f"🏦 UPI: {upi}\n"
                    f"📌 Status: {status_emoji} {status.capitalize()}\n"
                    f"🕒 Requested On: {created_at.strftime('%d-%m-%y • %I:%M %p')}\n\n"  # ✅ add space
                )

            await query.message.reply_text(history_text, parse_mode="Markdown")

        except Exception as e:
            print(f"Error fetching withdrawal history: {e}")
            await query.message.reply_text("❌ Failed to fetch withdrawal history. Try again later.")


# --- Withdraw Start ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = get_user(query.from_user.id)

    if not user:
        await query.message.reply_text("❗ You are not registered. Use /start")
        return ConversationHandler.END

    # Get active referrals
    user_uid = user[8]
    active_referred_users = get_active_referred_users(user_uid)
    active_referrals_count = len(active_referred_users) if active_referred_users else 0

    if active_referrals_count < 1:
        await query.message.reply_text(
            "💸 *Withdraw feature is locked!*\n\n"
            "To unlock this feature, refer at least 1 active user.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Store wallet balance for validation
    context.user_data["wallet_balance"] = user[5]

    await query.message.reply_text("💸 Enter the withdrawal amount (minimum ₹250):")
    return ASK_AMOUNT


# --- Ask for Amount ---
async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.")
        return ASK_AMOUNT

    if amount < 250:
        await update.message.reply_text("❌ Minimum withdrawal is ₹250. Enter again:")
        return ASK_AMOUNT

    if amount > context.user_data["wallet_balance"]:
        await update.message.reply_text("❌ You don't have enough balance. Enter again:")
        return ASK_AMOUNT

    # 🔹 Check referrals for withdrawals > ₹1000
    user = get_user(update.effective_user.id)
    user_uid = user[8]
    active_referred_users = get_active_referred_users(user_uid)
    active_referrals_count = len(active_referred_users) if active_referred_users else 0

    if amount > 1000 and active_referrals_count < 2:
        if active_referrals_count == 1:
            await update.message.reply_text(
                "❌ You currently have 1 active referral.\n\n"
                "✅ You can withdraw up to ₹1000 now.\n"
                "🔑 To withdraw more than ₹1000, refer 1 more active user."
            )
        else:
            await update.message.reply_text(
                f"❌ To withdraw more than ₹1000, you must have at least 2 active referrals.\n"
                f"📌 Currently, you have {active_referrals_count} active referral(s)."
            )
        return ASK_AMOUNT

    # Store valid amount
    context.user_data["withdraw_amount"] = amount
    await update.message.reply_text("📞 Enter your mobile number:")
    return ASK_MOBILE


# --- Ask for Mobile ---
async def withdraw_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) < 10:
        await update.message.reply_text("❌ Enter a valid mobile number:")
        return ASK_MOBILE

    context.user_data["withdraw_mobile"] = mobile
    await update.message.reply_text("🏦 Enter your UPI ID:")
    return ASK_UPI


# --- Ask for UPI ---
async def withdraw_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from db import get_connection  # ensure this import exists at top of your file

    upi = update.message.text.strip()
    context.user_data["withdraw_upi"] = upi

    amount = context.user_data["withdraw_amount"]
    mobile = context.user_data["withdraw_mobile"]
    wallet_balance = context.user_data["wallet_balance"]

    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("❗ You are not registered. Use /start")
        return ConversationHandler.END

    user_id = update.effective_user.id
    user_uid = user[8]  # UID from DB
    username = update.effective_user.username or update.effective_user.first_name

    # ✅ Insert withdrawal request into DB
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Insert new withdrawal
                cur.execute('''
                    INSERT INTO withdrawals (user_uid, telegram_id, amount, mobile, upi, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending')
                ''', (user_uid, user_id, amount, mobile, upi))

                # 🔹 Keep only latest 10 withdrawals per user
                cur.execute('''
                    DELETE FROM withdrawals
                    WHERE id NOT IN (
                        SELECT id
                        FROM withdrawals
                        WHERE user_uid = %s
                        ORDER BY created_at DESC
                        LIMIT 10
                    ) AND user_uid = %s
                ''', (user_uid, user_uid))

                conn.commit()
    except Exception as e:
        print(f"❌ ERROR inserting withdrawal into DB: {e}")
        await update.message.reply_text("❌ Error saving your request. Please try again later.")
        return ConversationHandler.END

    # Build admin message
    msg = (
        f"💸 New withdrawal request!\n\n"
        f"👤 User: {username} (ID: {user_id})\n"
        f"📞 Mobile: {mobile}\n"
        f"💰 Amount: ₹{amount}\n"
        f"🏦 UPI: {upi}\n"
        f"📌 Wallet Balance: ₹{wallet_balance}"
    )

    # Inline buttons for admin
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}_{amount}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}_{amount}")
        ]
    ])

    # Send to admin
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=msg,
        reply_markup=keyboard
    )

    # Confirm to user
    await update.message.reply_text(
        "✅ Your withdrawal request has been submitted. Please wait for admin approval."
    )

    return ConversationHandler.END


# --- Admin Approve/Reject ---
ASK_REASON = range(1)

# --- Admin approves or rejects ---
async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]  # approve / reject
    telegram_id = int(data[1])  
    amount = int(data[2])

    try:
        user = get_user(telegram_id)  # ✅ fetch by telegram_id
        if not user:
            await query.edit_message_text(f"❌ User {telegram_id} not found in DB")
            return ConversationHandler.END

        if action == "approve":
            new_balance = user[5] - amount  # wallet balance at index 5
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Deduct from wallet
                    cur.execute(
                        "UPDATE users SET wallet = %s WHERE telegram_id = %s",
                        (new_balance, telegram_id)
                    )
                    # Update withdrawal request status
                    cur.execute(
                        "UPDATE withdrawals SET status = 'approved' "
                        "WHERE telegram_id = %s AND amount = %s AND status = 'pending'",
                        (telegram_id, amount)
                    )
                    conn.commit()

            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"💰 Please be informed, your withdrawal of ₹{amount} has been approved. "
                    f"The amount will be credited to your UPI ID shortly.\n\n🏦 New Balance: ₹{new_balance}"
                )
            )
            await query.edit_message_text(f"✅ Approved withdrawal for {telegram_id}, amount ₹{amount}")
            return ConversationHandler.END

        elif action == "reject":
            # Store reject info temporarily
            context.user_data["reject_info"] = {
                "telegram_id": telegram_id,
                "amount": amount
            }
            await query.edit_message_text(
                f"❌ You chose to reject withdrawal of ₹{amount} for user {telegram_id}.\n\n"
                f"👉 Please type the reason for rejection:"
            )
            return ASK_REASON

    except Exception as e:
        print(f"❌ ERROR in approve/reject block: {e}")
        await query.edit_message_text(f"❌ Error processing request: {e}")
        return ConversationHandler.END


# --- Admin provides rejection reason ---
async def receive_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text
    reject_info = context.user_data.get("reject_info")

    if not reject_info:
        await update.message.reply_text("⚠️ No withdrawal request in progress.")
        return ConversationHandler.END

    telegram_id = reject_info["telegram_id"]
    amount = reject_info["amount"]

    # Inform the user
    await context.bot.send_message(
        chat_id=telegram_id,
        text=f"❌ Your withdrawal of ₹{amount} has been rejected.\n\n📌 Reason: {reason}"
    )

    # Confirm to admin
    await update.message.reply_text(
        f"✅ Rejection notice sent to user {telegram_id}\n📌 Reason: {reason}"
    )

    # Clean up
    context.user_data.pop("reject_info", None)

    return ConversationHandler.END


#Adds media support
#async def forward_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
#    user_id = update.effective_user.id
#    if user_id != ADMIN_CHAT_ID or not update.message:
#        return
#
#    try:
#        await update.message.copy(chat_id=CHANNEL_ID)
#        await update.message.reply_text("✅ Content forwarded to the channel.")
#    except Exception as e:
#        await update.message.reply_text(f"❌ Failed to forward: {e}")


# Start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "🔐 Welcome Admin! Use the buttons below to manage the bot:",
            reply_markup=admin_menu
        )
    else:
        await update.message.reply_text(
            "Welcome to the *ZyncPay*! Please choose an option to continue:",
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
	

from telegram.error import BadRequest

async def handle_name_with_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = update.message.text.strip()
    username = name if name.lower() != "skip" else (user.username or user.first_name)

    referred_by = context.user_data.get("referred_by")
    new_uid = add_user(user.id, username, referred_by)

    referrer = get_user_by_uid(referred_by)

    if referrer:
        referrer_id = referrer[1]  # assuming index 1 is telegram_id

        if referrer_id and str(referrer_id).isdigit():
            referrer_plan = referrer[9] or "Basic"
            ref_bonus = PLAN_BENEFITS.get(referrer_plan, {}).get("referral_bonus", 0)

            try:
                ref_msg = f"🎉 {username} registered with your referral! Kindly ask the referral to activate the account with any plan to claim yur referral bonus."
                await context.bot.send_message(referrer_id, ref_msg, parse_mode="Markdown")
            except BadRequest as e:
                print(f"⚠️ Could not notify referrer {referrer_id}: {e}")
        else:
            print(f"⚠️ Invalid referrer_id: {referrer_id}")
    else:
        print("ℹ️ No referrer found, skipping referral message.")


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
    await update.message.reply_text("✅ You’ve been registered with a referral!\n\n₹100 just dropped into your account for joining via referral! Invite friends now to earn even bigger rewards before they’re gone! 🚀", reply_markup=main_menu)
    return ConversationHandler.END

# Cancel
async def cancel_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Referral process cancelled.", reply_markup=start_menu)
    return ConversationHandler.END

# Wallet 
async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from datetime import datetime, date
        from db import get_connection  # needed for querying withdrawals

        user = get_user(update.effective_user.id)
        if not user:
            await update.message.reply_text("❗ You are not registered. Use /start", reply_markup=start_menu)
            return

        telegram_id = update.effective_user.id
        wallet_balance = user[5]  # wallet balance
        user_uid = user[8]  # user's UID
        
        # Get user's current plan
        user_plan_info = get_user_plan(telegram_id)
        user_plan_name = user_plan_info.get('name', 'Basic')
        
        # Get ACTIVE referred users for earnings calculation
        active_referred_users = get_active_referred_users(user_uid)
        active_referrals_count = len(active_referred_users) if active_referred_users else 0

        # Calculate referral earnings
        referral_earnings = 0
        referral_percent = {"Basic": 0.10, "Plus": 0.12, "Elite": 0.15}.get(user_plan_name, 0.10)
        plan_amounts = {"Basic": 1499, "Plus": 4499, "Elite": 9500}
        
        for referred_user in active_referred_users:
            referred_plan = referred_user[2] or 'Basic'  # plan is at index 2
            referred_plan_amount = plan_amounts.get(referred_plan, 1499)
            referral_earnings += referred_plan_amount * referral_percent

        # Weekly bonus progress based on plan_activation_date
        plan_activation_date = user[14] 
        weekly_bonus_progress = "0 / 28"

        if plan_activation_date:
            try:
                if isinstance(plan_activation_date, date):
                    activation_date = plan_activation_date
                else:
                    activation_date = datetime.strptime(plan_activation_date, "%Y-%m-%d").date()
                days_active = (datetime.now().date() - activation_date).days
                weekly_bonus_progress = f"{min(days_active, 28)} / 28"
            except Exception as e:
                print(f"Error calculating weekly bonus: {e}")
                weekly_bonus_progress = "0 / 28"

        # ✅ Fetch last withdrawal info
        last_withdraw_text = "None"
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT amount, created_at
                        FROM withdrawals
                        WHERE user_uid = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (user_uid,))
                    row = cur.fetchone()
                    if row:
                        amount, created_at = row
                        created_at_fmt = created_at.strftime('%y-%m-%d %I:%M%p')
                        last_withdraw_text = f"₹{amount} ({created_at_fmt})"
        except Exception as e:
            print(f"Error fetching last withdrawal: {e}")

        # Display
        text_msg = (
            f"💰 Wallet Balance: ₹{wallet_balance}\n"
            f"📈 Referral Earnings: ₹{int(referral_earnings)}\n"
            f"🎁 Weekly Bonus Progress: {weekly_bonus_progress}\n"
            f"📝 Last Withdrawal: {last_withdraw_text}"
        )

        # Update the withdrawal button based on active referrals
        if active_referrals_count >= 1:
            withdraw_text = "💸 Withdraw"
        else:
            withdraw_text = "🔒 Withdraw (0/1)"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(withdraw_text, callback_data="wallet_withdraw"),
                InlineKeyboardButton("📄 History", callback_data="wallet_history")
            ]
        ])

        await update.message.reply_text(text_msg, reply_markup=keyboard)
        
    except Exception as e:
        print(f"Error in wallet function: {e}")
        # Fallback to simple version if complex calculation fails
        try:
            user = get_user(update.effective_user.id)
            if user:
                simple_msg = (
                    f"💰 Wallet Balance: ₹{user[5]}\n"
                    f"📈 Referral Earnings: ₹0\n"
                    f"🎁 Weekly Bonus Progress: -0 / 28\n"
                    f"📝 Last Withdrawal: None"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔒 Withdraw (0/1)", callback_data="wallet_withdraw")]
                ])
                await update.message.reply_text(simple_msg, reply_markup=keyboard)
        except:
            await update.message.reply_text(
                "❌ Error accessing wallet information.",
                reply_markup=main_menu
            )


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
        
        # Single inline button for policy
        policy_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📜 Terms & Conditions", 
                web_app=WebAppInfo(url=POLICY_LINK)
            )]
        ])
        
        await update.message.reply_text(
            msg,
            reply_markup=back_menu,  # This already has the Back button
            parse_mode="Markdown"
        )
        
        # Send policy button separately
        await update.message.reply_text(
            "📋 *Terms and conditions apply to referral bonuses*",
            reply_markup=policy_keyboard,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❗ You are not registered. Use /start", reply_markup=start_menu)
		

# Profile
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime, date

    data = get_user_profile(update.effective_user.id)
    print("📌 DEBUG user profile:", data, type(data))

    if not data:
        await update.message.reply_text(
            "❗ You are not registered. Use /start", 
            reply_markup=start_menu
        )
        return

    # Handle Referred By
    referred_by = None
    if isinstance(data, tuple):
        referred_by = data[13] if len(data) > 13 else None
        plan_activation_date = data[14] if len(data) > 14 else None
        withdrawal_limit = data[7] if len(data) > 7 else 0  # Tuple index for withdrawal_limit
    elif isinstance(data, dict):
        referred_by = data.get('referred_by')
        plan_activation_date = data.get('plan_activation_date')
        withdrawal_limit = data.get('withdrawal_limit', 0)
    else:
        plan_activation_date = None
        withdrawal_limit = 0

    ref_by = "N/A"
    if referred_by and isinstance(referred_by, dict):
        ref_by = f"[{referred_by['username']}](tg://user?id={referred_by['telegram_id']}) (UID: {referred_by['uid']})"

    status = "✅ Activated" if (data[12] if isinstance(data, tuple) else data.get('activation_status')) else "❌ Not Activated"

    # 🔥 Membership Level based on withdrawal_limit
    thresholds = [
        (20000, "Legend"),
        (15000, "Grandmaster"),
        (10000, "Master"),
        (7000, "Diamond"),
        (5000, "Platinum"),
        (3000, "Gold"),
        (1000, "Silver"),
        (0, "Bronze"),
    ]
    rank = "Bronze"  # default
    for limit, level in thresholds:
        if withdrawal_limit >= limit:
            rank = level
            break

    msg = (
        f"🆔 User ID: {data[0] if isinstance(data, tuple) else data.get('user_uid')}\n"
        f"👤 Username: {data[1] if isinstance(data, tuple) else data.get('username')}\n"
        f"🔗 Referral Code: {data[0] if isinstance(data, tuple) else data.get('user_uid')}\n"
        f"🔓 Status: {status}\n"
        f"📅 Days Since Registration: {data[11] if isinstance(data, tuple) else data.get('registered_days')}\n"
        f"🏅 Membership Level: {rank}\n"
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
    payment_url = "https://payments.cashfree.com/forms/ZyncPay"

    context.user_data["awaiting_activation"] = True

    if payment_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=payment_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="activation_back")]
        ])
		

        await update.message.reply_text(
			"🚀 Get ready to unlock your earning journey!\n\n"
			"💳 Select your plan on the payment page and complete the payment securely.\n\n"
            "📌 After completing payment:\n"
            "1. Take a screenshot of the successful payment.\n"
            "2. Upload it here for admin verification.\n\n"
            "_Your account will be activated after Admin approval._",
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

    # ✅ Fixed buttons (activate_* instead of approve/reject)
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Basic", callback_data=f"activate_basic_{uid}"),
            InlineKeyboardButton("💎 Plus",  callback_data=f"activate_plus_{uid}"),
        ],		
        [
            InlineKeyboardButton("👑 Elite", callback_data=f"activate_elite_{uid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"activate_reject_{uid}")
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


# --- Admin handles activation approval/rejection ---
async def handle_activation_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")   # e.g. ["activate", "elite", "752"]
    action = parts[1]               # basic / plus / elite / reject
    uid = parts[2]

    plan_name = action.capitalize()   # 👈 "Basic" / "Plus" / "Elite"

    # Get user by UID
    user = get_user_by_uid(uid)
    if not user:
        if query.message.photo:
            await query.edit_message_caption("❌ User not found")
        else:
            await query.edit_message_text("❌ User not found")
        return

    if action == "reject":
        # ❌ Rejected
        await context.bot.send_message(
            chat_id=user[1],  # telegram_id
            text="❌ Your activation request has been rejected."
        )
        if query.message.photo:
            await query.edit_message_caption(f"❌ Rejected activation for UID {uid}")
        else:
            await query.edit_message_text(f"❌ Rejected activation for UID {uid}")
        return

    # ✅ Otherwise, activate the chosen plan
    try:
        # 1. Update the user with plan + activation
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET plan = %s,
                        activation_status = 'TRUE',
                        plan_activation_date = CURRENT_DATE
                    WHERE user_uid = %s
                    """,
                    (plan_name, uid)
                )
                conn.commit()

        # 2. Fetch fresh user data after update
        referred_user = get_user_by_uid(uid)
        referred_by = referred_user[4]  # 👈 assuming index 4 = referred_by UID

        # 3. Handle referral bonus + withdrawal_limit in ONE transaction silently
        if referred_by:
            referrer = get_user_by_uid(referred_by)
            if referrer:
                referrer_id = referrer[1]  # telegram_id

                # ✅ NEW: referral bonus
                ref_bonus = PLAN_BENEFITS.get(plan_name, {}).get("referral_bonus", 0)
                # ✅ NEW: withdrawal increment mapping
                PLAN_WITHDRAWAL_INCREMENT = {"Basic": 1000, "Plus": 3000, "Elite": 6000}
                withdrawal_increment = PLAN_WITHDRAWAL_INCREMENT.get(plan_name, 0)

                # ✅ NEW: single DB transaction for both wallet + withdrawal_limit
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE users
                            SET wallet = wallet + %s,
                                withdrawal_limit = withdrawal_limit + %s
                            WHERE user_uid = %s
                            """,
                            (ref_bonus, withdrawal_increment, referred_by)
                        )
                        conn.commit()
                # ✅ Note: no notification sent for withdrawal increment

        # 4. Notify activated user
        await context.bot.send_message(
            chat_id=user[1],  # telegram_id
            text=f"✅ Your account has been activated with the {plan_name} plan!"
        )

        # 5. Update admin message
        if query.message.photo:
            await query.edit_message_caption(f"✅ Activated {plan_name} plan for UID {uid}")
        else:
            await query.edit_message_text(f"✅ Activated {plan_name} plan for UID {uid}")

    except Exception as e:
        print(f"❌ Error activating user {uid}: {e}")
        if query.message.photo:
            await query.edit_message_caption("❌ Failed to activate user. Please try again later.")
        else:
            await query.edit_message_text("❌ Failed to activate user. Please try again later.")


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
		
        await update.message.reply_text(
            "Need help? Contact support:",
            reply_markup=support_keyboard
        )

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

    elif text == "⚡ Commands" and update.effective_user.id == ADMIN_CHAT_ID:    
        commands_pages = [    
            (    
                "💎💠━━━━━━━━━━━━━━💠💎\n"    
                "        *USER MANAGEMENT*\n"    
                "💎💠━━━━━━━━━━━━━━💠💎\n\n"    
                "• 🔒 Ban: /ban <user_id>\n"    
                "  Usage: Bans the user with the specified ID\n\n"    
                "• 🔓 Unban: /unban <user_id>\n"    
                "  Usage: Unbans the specified user\n\n"    
                "• 📝 Info: /userinfo <user_id>\n"    
                "  Usage: Shows detailed information about a user\n\n"    
                "• ✉️ DM: /dm <user_id> <message>\n"    
                "  Usage: Sends a custom message to the user\n\n"    
                "───────────────"    
            ),    
            (    
                "📊📈━━━━━━━━━━━━━━📈📊\n"    
                "       *REPORTS & TRACKING*\n"    
                "📊📈━━━━━━━━━━━━━━📈📊\n\n"    
                "• 🕒 Last 10: /last10\n"    
                "  Usage: Shows the last 10 registered users\n\n"    
                "• ⏳ Pending: /pending\n"    
                "  Usage: Lists all users pending activation\n\n"    
                "• ✅ Active: /active\n"    
                "  Usage: Shows the count/list of active users\n\n"    
                "• ❌ Inactive: /inactive\n"    
                "  Usage: Shows the count/list of inactive users\n\n"    
                "───────────────"    
            ),    
            (    
                "📢📬━━━━━━━━━━━━━━📬📢\n"    
                "         *COMMUNICATION*\n"    
                "📢📬━━━━━━━━━━━━━━📬📢\n\n"    
                "• 📣 Notify: /notify <message>\n"    
                "  Usage: Sends a notification to all activated users\n\n"    
                "• ⏰ Remind: /remind\n"    
                "  Usage: Sends payment reminders to inactive users\n\n"    
                "───────────────"    
            )    
        ]    

        # store pagination state    
        context.user_data["commands_pages"] = commands_pages    
        context.user_data["commands_page"] = 0

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Next", callback_data="cmd_next")]
        ])

        await update.message.reply_text(commands_pages[0], reply_markup=keyboard, parse_mode="Markdown")


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

# Callback Query
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

    # ✅ Admin approves user (activation)
    elif data.startswith("approve:"):
        try:
            uid = int(data.split(":")[1])
        except ValueError:
            await query.edit_message_text("❌ Invalid UID.")
            return

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
        try:
            uid = int(data.split(":")[1])
        except ValueError:
            await query.edit_message_text("❌ Invalid UID.")
            return

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

    elif data == "cmd_next":
        pages = context.user_data.get("commands_pages", [])
        current = context.user_data.get("commands_page", 0)

        if current + 1 < len(pages):
            context.user_data["commands_page"] = current + 1
            keyboard = []

            if current + 1 < len(pages) - 1:
                keyboard = [[
                    InlineKeyboardButton("⬅️ Back", callback_data="cmd_back"),
                    InlineKeyboardButton("➡️ Next", callback_data="cmd_next")
                ]]
            else:  # last page
                keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="cmd_back")]]

            await query.edit_message_text(pages[current + 1], reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_back":
        pages = context.user_data.get("commands_pages", [])
        current = context.user_data.get("commands_page", 0)

        if current - 1 >= 0:
            context.user_data["commands_page"] = current - 1
            keyboard = []

            if current - 1 == 0:
                keyboard = [[InlineKeyboardButton("➡️ Next", callback_data="cmd_next")]]
            else:
                keyboard = [[
                    InlineKeyboardButton("⬅️ Back", callback_data="cmd_back"),
                    InlineKeyboardButton("➡️ Next", callback_data="cmd_next")
                ]]

            await query.edit_message_text(pages[current - 1], reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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

        telegram_id = query.from_user.id
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

    # ✅ Admin approves plan activation
    elif data.startswith("approve_basic:") or data.startswith("approve_plus:") or data.startswith("approve_elite:"):
        try:
            plan = data.split(":")[0].replace("approve_", "").capitalize()
            uid = int(data.split(":")[1])
        except ValueError:
            await query.edit_message_text("❌ Invalid UID.")
            return

        user = get_user_by_uid(uid)
        if user:
            activate_user(user[1])
            # Update user plan and set activation date
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE users 
                SET plan = %s, plan_activation_date = CURRENT_DATE 
                WHERE telegram_id = %s
            """, (plan, user[1]))
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

    # 💸 Withdraw approve
    elif data.startswith("withdraw_approve_"):
        telegram_id = int(data.split("_")[2])
        user = get_user_by_telegram_id(telegram_id)
        if user:
            # Mark withdrawal as approved in DB if needed
            await context.bot.send_message(chat_id=telegram_id, text="✅ Your withdrawal has been approved!")
            await query.edit_message_text("✅ Withdrawal approved.")
        else:
            await query.edit_message_text("❌ User not found.")

    # 💸 Withdraw reject
    elif data.startswith("withdraw_reject_"):
        telegram_id = int(data.split("_")[2])
        user = get_user_by_telegram_id(telegram_id)
        if user:
            await context.bot.send_message(chat_id=telegram_id, text="❌ Your withdrawal request was rejected.")
            await query.edit_message_text("❌ Withdrawal rejected.")
        else:
            await query.edit_message_text("❌ User not found.")

    # 👀 See other plans
    elif data == "see_other_plans":
        telegram_id = query.from_user.id
        current_plan = get_user_plan(telegram_id)['name']

        plan_details = {
            "Basic": {"emoji": "✅", "amount": 1499, "daily": "₹100/-", "weekly": "₹250/- (Every 4th week)", "referral": "According to the plan of the newly joined user (10% of the plan)"},
            "Plus": {"emoji": "💎", "amount": 4499, "daily": "₹300/-", "weekly": "₹600/- (Every 4th week)", "referral": "According to the plan of the newly joined user (12% of the plan)"},
            "Elite": {"emoji": "👑", "amount": 9500, "daily": "₹750/-", "weekly": "₹1200/- (Every 4th week)", "referral": "According to the plan of the newly joined user (15% of the plan)"}
        }

        keyboard_buttons = []
        for plan_name, details in plan_details.items():
            if plan_name != current_plan:
                keyboard_buttons.append(
                    [InlineKeyboardButton(f"{details['emoji']} {plan_name} - ₹{details['amount']}", callback_data=f"show_plan_{plan_name.lower()}")]
                )

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await query.edit_message_text("Choose a plan to see details:", reply_markup=keyboard)

    # 👀 Show plan details
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
            [InlineKeyboardButton("✅ Select This Plan", url="https://payments.cashfree.com/forms/ZyncPay")]
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


async def remind_callback(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "remind_custom":
        context.user_data["awaiting_custom_remind"] = True

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="remind_cancel")]
        ])

        await query.edit_message_text(
            "📝 Please send the custom reminder message:",
            reply_markup=keyboard
        )

    elif query.data == "remind_template":
        # -----------------------------
        # 1️⃣ Fetch inactive users from DB
        # -----------------------------
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username, telegram_id FROM users WHERE activation_status = FALSE")
                inactive_users = cur.fetchall()  # list of tuples (username, telegram_id)

        payment_url = "https://payments.cashfree.com/forms/ZyncPay"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=payment_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="activation_back")]
        ])

        success_count, fail_count = 0, 0

        # -----------------------------
        # 2️⃣ Loop through inactive users and send messages
        # -----------------------------
        for user in inactive_users:
            try:
                username = user[0] or f"User {user[1]}"
                telegram_id = user[1]

                # -----------------------------
                # 3️⃣ Send first template message
                # -----------------------------
                text1 = f"Dear {username}, you're missing the potential earning and benefits of ZyncPay. Kindly activate your account to start receiving those benefits."
                await context.bot.send_message(chat_id=telegram_id, text=text1)

                # -----------------------------
                # 4️⃣ Send second message with payment button
                # -----------------------------
                text2 = (
                    "🚀 Get ready to unlock your earning journey!\n\n"
                    "💳 Select your plan on the payment page and complete the payment securely.\n\n"
                    "📌 After completing payment:\n"
                    "1. Take a screenshot of the successful payment.\n"
                    "2. Upload it here for admin verification.\n\n"
                    "_Your account will be activated after Admin approval._"
                )
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=text2,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

                success_count += 1
            except Exception as e:
                print(f"❌ Failed to send reminder to {telegram_id}: {e}")
                fail_count += 1

        # -----------------------------
        # 5️⃣ Show admin summary
        # -----------------------------
        await query.edit_message_text(
            f"✅ Sent template reminders to {success_count} users.\n"
            f"❌ Failed for {fail_count} users."
        )

    elif query.data == "remind_cancel":
        context.user_data["awaiting_custom_remind"] = False
        await query.edit_message_text("❌ Custom reminder cancelled.")


async def handle_custom_remind(update, context):
    # Only admin can send the custom reminder
    if update.effective_user.id != ADMIN_CHAT_ID:
        return  

    # Only if admin previously clicked "Custom Message"
    if not context.user_data.get("awaiting_custom_remind"):
        return
    
    custom_message = update.message.text

    # -----------------------------
    # 1️⃣ Fetch inactive users from DB
    # -----------------------------
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, telegram_id FROM users WHERE activation_status = FALSE")
            inactive_users = cur.fetchall()  # list of tuples (username, telegram_id)

    success_count, fail_count = 0, 0

    # -----------------------------
    # 2️⃣ Send the custom message to each inactive user
    # -----------------------------
    for user in inactive_users:
        try:
            telegram_id = user[1]
            await context.bot.send_message(chat_id=telegram_id, text=custom_message)
            success_count += 1
        except Exception as e:
            print(f"❌ Failed to send custom reminder to {telegram_id}: {e}")
            fail_count += 1

    # Reset state
    context.user_data["awaiting_custom_remind"] = False  

    # Confirm to admin
    await update.message.reply_text(
        f"✅ Custom reminder sent to {success_count} users.\n"
        f"❌ Failed for {fail_count} users."
    )


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

#Withdraw Handler
withdraw_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(withdraw_start, pattern="^wallet_withdraw$")],
    states={
        ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
        ASK_MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_mobile)],
        ASK_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi)],
    },
    fallbacks=[],
)

app.add_handler(withdraw_handler)
app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^(approve|reject)_"))

app.add_handler(CallbackQueryHandler(handle_activation_action, pattern="^activate_"))

dm_handler = ConversationHandler(
    entry_points=[CommandHandler("dm", dm_start)],
    states={
        AWAIT_MESSAGE: [MessageHandler(filters.TEXT & (~filters.COMMAND), dm_send)]
    },
    fallbacks=[CommandHandler("cancel", dm_cancel)],
)

app.add_handler(dm_handler)

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

app.add_handler(CommandHandler("testchat", test_support))  #Test Mode
app.add_handler(CommandHandler("supportpanel", support_panel)) #Admin Web app Support chat panel
app.add_handler(CommandHandler("policy", policy_command))
app.add_handler(CommandHandler("ban", ban))
app.add_handler(CommandHandler("unban", unban))
app.add_handler(CommandHandler("userinfo", userinfo))
#app.add_handler(CommandHandler("dm", dm))
app.add_handler(CommandHandler("last10", last10))
app.add_handler(CommandHandler("pending", pending))
app.add_handler(CommandHandler("active", active))
app.add_handler(CommandHandler("inactive", inactive))
app.add_handler(CommandHandler("notify", notify))
app.add_handler(CommandHandler("remind", remind))

# Register all handlers
    # 1. Commands
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("activate", activate))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("id", my_id))
app.add_handler(CommandHandler("distribute_now", distribute_now))
#app.add_handler(CommandHandler("channel", channel_command))

    # 2. Callback handlers
# Only keep wallet_history in wallet_callback
app.add_handler(CallbackQueryHandler(wallet_callback, pattern="^wallet_history$"))
app.add_handler(CallbackQueryHandler(remind_callback, pattern="^remind_"))
app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 3. Conversations
app.add_handler(conv_handler)

    # 4. Messages
app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_remind))
#app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_CHAT_ID), forward_to_channel))
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
