import os
from fastapi import FastAPI, Request
import telegram
import requests

# =========================
# إعدادات عامة
# =========================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

bot = telegram.Bot(token=TOKEN)
app = FastAPI()

# =========================
# النصوص الرسمية (ثابتة)
# =========================

WELCOME_TEXT = """
🤖 Warith AI Assistant

مساعد ذكي للطلاب والتقنيين
إجابات فورية • شرح مبسّط • دعم 24/7

👤 المطوّر:
Warith Al-Awadi

✉️ فقط اكتب سؤالك وسأجيبك مباشرة
"""

ABOUT_TEXT = """
ℹ️ حول البوت

• مساعد ذكي يعتمد على الذكاء الاصطناعي
• مخصص للطلاب والتقنيين
• يشرح، يبسّط، ويجيب على الأسئلة
• يعمل على مدار الساعة 24/7

👤 المطوّر:
Warith Al-Awadi
"""

HELP_TEXT = """
🆘 المساعدة

الأوامر المتاحة:
/start  - بدء الاستخدام
/help   - المساعدة
/about  - معلومات عن البوت

💡 يمكنك أيضًا كتابة أي سؤال مباشرة بدون أوامر.
"""

SYSTEM_PROMPT = """
أنت مساعد ذكي للطلاب والتقنيين.
اشرح بإسلوب واضح وبسيط.
استخدم اللغة العربية بشكل افتراضي.
إذا كان السؤال تقنيًا، أعطِ مثالًا.
إذا لم تعرف الجواب، كن صريحًا.
"""

# =========================
# الصفحة الرئيسية (فحص السيرفر)
# =========================

@app.get("/")
async def root():
    return {"ok": True, "service": "company_bot", "mode": "webhook"}

# =========================
# Webhook Telegram
# =========================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = telegram.Update.de_json(data, bot)

    if not update.message or not update.message.text:
        return {"ok": True}

    chat_id = update.message.chat.id
    text = update.message.text.strip()

    # =========================
    # أوامر أساسية
    # =========================

    if text == "/start":
        bot.send_message(chat_id=chat_id, text=WELCOME_TEXT)
        return {"ok": True}

    if text == "/about":
        bot.send_message(chat_id=chat_id, text=ABOUT_TEXT)
        return {"ok": True}

    if text == "/help":
        bot.send_message(chat_id=chat_id, text=HELP_TEXT)
        return {"ok": True}

    # =========================
    # الرد الذكي (AI)
    # =========================

    if not OPENAI_KEY:
        bot.send_message(
            chat_id=chat_id,
            text="⚠️ الذكاء الاصطناعي غير مفعّل حاليًا."
        )
        return {"ok": True}

    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ],
            "temperature": 0.6
        }

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=40
        )

        result = response.json()
        answer = result["choices"][0]["message"]["content"]

        final_answer = f"{answer}\n\n—\n🤖 Warith AI Assistant"

        bot.send_message(chat_id=chat_id, text=final_answer)

    except Exception as e:
        bot.send_message(
            chat_id=chat_id,
            text="❌ حدث خطأ مؤقت، حاول مرة أخرى."
        )

    return {"ok": True}
