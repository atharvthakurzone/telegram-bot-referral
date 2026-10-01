import os
import asyncio


# ============================================================
# BOT DEPLOYMENT CONFIGURATION
# ============================================================

# Supported modes:
#   polling  → Heroku Worker / VPS / background worker
#   webhook  → Render Web Service / webhook hosting
#
# Default is polling.
BOT_MODE = os.getenv("BOT_MODE", "polling").lower().strip()

PORT = int(os.getenv("PORT", "8443"))
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")

# Import this only for the daily scheduler.
from bot import schedule_daily_income


async def post_init(application):
    """
    Runs once after the Telegram application initializes.
    """

    # Start the daily income scheduler regardless of platform.
    asyncio.create_task(schedule_daily_income())

    # --------------------------------------------------------
    # WEBHOOK MODE
    # --------------------------------------------------------

    if BOT_MODE == "webhook":

        if not RENDER_HOST:
            raise RuntimeError(
                "BOT_MODE=webhook but RENDER_EXTERNAL_HOSTNAME is missing."
            )

        webhook_url = f"https://{RENDER_HOST}/{application.bot.token}"

        await application.bot.set_webhook(
            webhook_url,
            drop_pending_updates=True
        )

        print(f"✅ Webhook set to: {webhook_url}", flush=True)

    # --------------------------------------------------------
    # POLLING MODE
    # --------------------------------------------------------

    else:

        # Remove any webhook left behind by a previous deployment.
        await application.bot.delete_webhook(
            drop_pending_updates=True
        )

        print("✅ Telegram webhook cleared.", flush=True)
        print("✅ Bot will run using polling.", flush=True)


def start(application):
    """
    Start the bot according to BOT_MODE.
    """

    if BOT_MODE == "webhook":

        print("🤖 Starting bot in WEBHOOK mode...", flush=True)

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=application.bot.token,
            webhook_url=f"https://{RENDER_HOST}/{application.bot.token}",
        )

    else:

        print("🤖 Starting bot in POLLING mode...", flush=True)

        application.run_polling(
            drop_pending_updates=True
        )
