import os
from fastapi import FastAPI, Request
import telegram

TOKEN = os.environ.get("TELEGRAM_TOKEN")

bot = telegram.Bot(token=TOKEN)
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if update.message and update.message.text:
        chat_id = update.message.chat.id
        text = update.message.text

        bot.send_message(
            chat_id=chat_id,
            text=f"🤖 البوت يعمل 24/7\n\n📩 رسالتك:\n{text}"
        )

    return {"ok": True}
