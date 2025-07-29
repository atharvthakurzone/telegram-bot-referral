import requests
import uuid
import datetime
from config import CASHFREE_APP_ID, CASHFREE_SECRET_KEY, CASHFREE_ENV

BASE_URL = "https://api.cashfree.com/pg" if CASHFREE_ENV == "PROD" else "https://sandbox.cashfree.com/pg"

def generate_payment_link(user_id: int, username: str):
    url = f"{BASE_URL}/links"

    headers = {
        "Content-Type": "application/json",
        "x-api-version": "2022-01-01",
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
    }

    order_id = f"ORDER_{user_id}_{uuid.uuid4().hex[:6]}"
    payload = {
        "customer_details": {
            "customer_id": str(user_id),
            "customer_name": username or "Unknown",
            "customer_email": f"user{user_id}@mail.com",
            "customer_phone": "9999999999"
        },
        "link_notify": {
            "send_sms": False,
            "send_email": False
        },
        "link_meta": {
            "return_url": "https://cashfree.com",
            "notify_url": "https://yourdomain.com/cashfree-webhook"
        },
        "link_amount": 999.0,
        "link_currency": "INR",
        "link_purpose": "Activation",
        "link_id": order_id,
        "link_expiry_time": (datetime.datetime.utcnow() + datetime.timedelta(minutes=15)).isoformat() + "Z"
    }

    res = requests.post(url, headers=headers, json=payload)
    data = res.json()

    if res.status_code == 200 and data.get("link_url"):
        return data["link_url"]
    else:
        print("❌ Error creating payment link:", data)
        return None
