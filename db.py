import sqlite3
import time

DB_NAME = "referral_bot.db"

# Initialize DB
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Create users table if not exists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            referral_code TEXT,
            referred_by TEXT,
            wallet INTEGER DEFAULT 0,
            registered_on INTEGER,
            earnings_days INTEGER DEFAULT 0,
            user_uid TEXT UNIQUE,
            activation_status INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0
        )
    ''')

    conn.commit()
    conn.close()


# Generate UID
def generate_uid():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM users')
    count = cur.fetchone()[0] + 1
    conn.close()
    return str(749 + count)

# Add User
def add_user(telegram_id, username, referred_by):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    new_uid = generate_uid()
    registered_on = int(time.time())
    wallet = 100 if referred_by else 0

    cur.execute('''
        INSERT OR IGNORE INTO users 
        (telegram_id, username, referral_code, referred_by, registered_on, user_uid, wallet)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (telegram_id, username, new_uid, referred_by, registered_on, new_uid, wallet))

    if referred_by:
        cur.execute('UPDATE users SET wallet = wallet + 100 WHERE referral_code = ?', (referred_by,))

    conn.commit()
    conn.close()
    return new_uid

# Get user by Telegram ID
def get_user(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
    user = cur.fetchone()
    conn.close()
    return user

# Get user by UID
def get_user_by_uid(user_uid):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_uid = ?", (user_uid,))
    user = cur.fetchone()
    conn.close()
    return user

# Get all users (Telegram IDs only)
def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM users")
    users = [row[0] for row in cur.fetchall()]
    conn.close()
    return users

# Count total users
def count_users():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    conn.close()
    return total

# Get referred users
def get_referred_users(referral_code):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT username, telegram_id, user_uid FROM users WHERE referred_by = ?', (referral_code,))
    users = cur.fetchall()
    conn.close()
    return users

# Get profile info with extra fields
def get_user_profile(telegram_id):
    import time
    import datetime
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
    user = cur.fetchone()

    if not user:
        conn.close()
        return None

    real_count = len(get_referred_users(user[3]))
    plus_count = user[11] if len(user) > 11 and user[11] is not None else 0
    referral_count = real_count + plus_count

    # ✅ Auto-correct future registration date
    registered_on = user[6] or int(time.time())
    now = int(time.time())
    if registered_on > now:
        print(f"⚠️ Future timestamp detected for user {telegram_id}: {registered_on} > {now}")
        registered_on = now

    days = int((now - registered_on) / 86400)

    referred_by_link = "N/A"
    if user[4]:
        cur.execute("SELECT username, user_uid, telegram_id FROM users WHERE referral_code = ?", (str(user[4]),))
        result = cur.fetchone()
        if result:
            referred_by_link = {
                "username": result[0] or "User",
                "uid": result[1],
                "telegram_id": result[2]
            }

    conn.close()
    return {
        "username": user[2],
        "referral_code": user[3],
        "wallet": user[5],
        "referral_count": referral_count,
        "registered_days": days,
        "earnings_days": user[7],
        "user_uid": user[8],
        "referred_by": referred_by_link,
        "activation_status": user[9]
    }

# Activation functions
def activate_user(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET activation_status = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def is_user_activated(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT activation_status FROM users WHERE telegram_id = ?", (telegram_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] == 1 if result else False

# Get all users with pending activation
def get_pending_users():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE activation_status = 0")
    result = cur.fetchall()
    conn.close()
    return result

def ban_user(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET banned = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def unban_user(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET banned = 0 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def is_user_banned(telegram_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT banned FROM users WHERE telegram_id = ?", (telegram_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] == 1 if result else False
