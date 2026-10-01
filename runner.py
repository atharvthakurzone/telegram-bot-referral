import bot
import deployment


# Replace bot.py's Render-specific startup callback
# with our platform-independent deployment callback.
bot.app.post_init = deployment.post_init


# Start the application according to the selected platform.
deployment.start(bot.app)
