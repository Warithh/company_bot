import os
from flask import Flask, request
import telegram

TOKEN = os.environ.get("TELEGRAM_TOKEN")

bot = telegram.Bot(token=TOKEN)
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update = telegram.Update.de_json(request.get_json(force=True), bot)

    if update.message and update.message.text:
        chat_id = update.message.chat.id
        text = update.message.text

        bot.send_message(
            chat_id=chat_id,
            text=f"📩 وصلني سؤالك:\n{text}\n\n🤖 البوت يعمل 24/7"
        )

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
