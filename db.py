import psycopg
import os
import time

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE,
                    username TEXT,
                    referral_code TEXT,
                    referred_by TEXT,
                    wallet INTEGER DEFAULT 0,
                    registered_on BIGINT,
                    withdrawal_limit INTEGER DEFAULT 0,
                    user_uid TEXT UNIQUE,
                    activation_status BOOLEAN DEFAULT FALSE,
                    banned BOOLEAN DEFAULT FALSE,
                    plus_referral_count INTEGER DEFAULT 0,
                    plan VARCHAR(10) DEFAULT NULL,
                    last_income_date DATE,
                    plan_activation_date DATE
                )
            ''')
            conn.commit()


def init_withdrawals_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_uid TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    mobile TEXT NOT NULL,
                    upi TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    telegram_id BIGINT
                )
            ''')
            conn.commit()


def generate_uid():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM users')
            count = cur.fetchone()[0] + 1
            return str(1200 + count)


def add_user(telegram_id, username, referred_by):
    new_uid = generate_uid()
    registered_on = int(time.time())
    wallet = 100 if referred_by else 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO users 
                (telegram_id, username, referral_code, referred_by, registered_on, user_uid, wallet)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO NOTHING
            ''', (telegram_id, username, new_uid, referred_by, registered_on, new_uid, wallet))

            if referred_by:
                cur.execute('UPDATE users SET wallet = wallet + 100 WHERE referral_code = %s', (referred_by,))

            conn.commit()
    return new_uid


def get_user(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM users WHERE telegram_id = %s', (telegram_id,))
            return cur.fetchone()


def get_user_by_uid(user_uid):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_uid = %s", (user_uid,))
            return cur.fetchone()


def get_all_users():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM users")
            return [row[0] for row in cur.fetchall()]


def count_users():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]


def get_referred_users(referral_code):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT username, telegram_id, user_uid FROM users WHERE referred_by = %s', (referral_code,))
            return cur.fetchall()


def get_user_profile(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM users WHERE telegram_id = %s', (telegram_id,))
            user = cur.fetchone()

            if not user:
                return None

            real_count = len(get_referred_users(user[4]))  # referred_by
            plus_count = user[11] if len(user) > 11 and user[11] is not None else 0
            referral_count = real_count + plus_count

            registered_on = user[6] or int(time.time())
            now = int(time.time())
            if registered_on > now:
                registered_on = now

            days = int((now - registered_on) / 86400)

            referred_by_link = "N/A"
            if user[4]:
                cur.execute("SELECT username, user_uid, telegram_id FROM users WHERE referral_code = %s", (str(user[4]),))
                result = cur.fetchone()
                if result:
                    referred_by_link = {
                        "username": result[0] or "User",
                        "uid": result[1],
                        "telegram_id": result[2]
                    }

            return {
                "username": user[2],
                "referral_code": user[3],
                "wallet": user[5],
                "referral_count": referral_count,
                "registered_days": days,
                "withdrawal_limit": user[7],
                "user_uid": user[8],
                "referred_by": referred_by_link,
                "activation_status": user[9]
            }


def activate_user(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET activation_status = TRUE WHERE telegram_id = %s", (telegram_id,))
            conn.commit()


def is_user_activated(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT activation_status FROM users WHERE telegram_id = %s", (telegram_id,))
            result = cur.fetchone()
            return result[0] if result else False


def get_pending_users():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE activation_status = FALSE")
            return cur.fetchall()


def ban_user(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET banned = TRUE WHERE telegram_id = %s", (telegram_id,))
            conn.commit()


def unban_user(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET banned = FALSE WHERE telegram_id = %s", (telegram_id,))
            conn.commit()


def is_user_banned(telegram_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT banned FROM users WHERE telegram_id = %s", (telegram_id,))
            result = cur.fetchone()
            return result[0] if result else False
