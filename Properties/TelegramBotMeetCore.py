import configparser
import os
import sys
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ------------------- Bot Logic -------------------

async def reply_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies to all user text messages.
    """
    await update.message.reply_text("Hello! I'm a Telegram bot.")

def main_bot(token: str):
    """
    Main function to run the bot.
    """
    application = Application.builder().token(token).build()
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_all_messages))
    
    print("Bot is running. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# ------------------- Main Block -------------------

if __name__ == "__main__":
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        ini_path = os.path.join(base_dir, "settings.ini")

        # Reading the token from INI
        config = configparser.ConfigParser()
        config.read(ini_path, encoding="utf-8")
        if "Settings" in config and "TelegramToken" in config["Settings"]:
            token = config["Settings"]["TelegramToken"]
        else:
            print("Token not found in settings.ini!")
            input("Press Enter to exit...")
            exit(1)

        # Starting the bot
        main_bot(token)

    except Exception as e:
        print("An error occurred:", e)
        input("Press Enter to exit...")
