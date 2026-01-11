import os
import openai
from fastapi import FastAPI, Request
import requests

openai.api_key = os.getenv("")
BOT_TOKEN = os.getenv("")

app = FastAPI()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

@app.post("/")
async def telegram_webhook(req: Request):
    data = await req.json()

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    user_text = data["message"].get("text", "")

    if not user_text:
        return {"ok": True}

    # OpenAI response
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "أنت مساعد ذكي للطلاب، تشرح ببساطة ووضوح."},
            {"role": "user", "content": user_text}
        ]
    )

    reply = response.choices[0].message.content

    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": reply}
    )

    return {"ok": True}
