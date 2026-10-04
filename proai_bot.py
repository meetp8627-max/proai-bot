# ProAI Telegram Bot — fresh start
# pip install "python-telegram-bot[webhooks]==21.6" openai
#
# Environment variables (keys code me kabhi hardcode mat karo):
#   BOT_TOKEN    -> BotFather se
#   AI_API_KEY   -> NVIDIA API key (build.nvidia.com)
# Optional:
#   AI_BASE_URL  -> default NVIDIA endpoint
#   AI_MODEL     -> default nemotron-3.5-lightning-30b-a3b
#   MAX_HISTORY  -> kitne messages yaad rakhne hain (default 40)
#   MAX_TOKENS   -> reply ki max length (default 2048)
#   WEBHOOK_URL  -> Render jaise hosts ke liye (e.g. https://app.onrender.com)

import os
import logging
from collections import defaultdict, deque

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_MODEL = os.environ.get("AI_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))

ai = AsyncOpenAI(
    api_key=os.environ["AI_API_KEY"],
    base_url=os.environ.get("AI_BASE_URL", "https://integrate.api.nvidia.com/v1"),
)

SYSTEM_PROMPT = (
    "Tum ProAI ho — ek smart, stylish aur friendly assistant. "
    "User ki language me reply do (Hinglish ho to Hinglish). "
    "Jawab short aur clear rakho."
)

# Har chat ki last MAX_HISTORY messages yaad rahengi
history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! Main ProAI hu ✨ Kuch bhi poocho.\n/reset se chat clear karo."
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    history[update.effective_chat.id].clear()
    await update.message.reply_text("Chat reset ho gayi ✅")


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    history[chat_id].append({"role": "user", "content": update.message.text})
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        res = await ai.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      *history[chat_id]],
            max_tokens=MAX_TOKENS,
        )
        reply = res.choices[0].message.content
        history[chat_id].append({"role": "assistant", "content": reply})
    except Exception as e:
        logging.exception("AI error")
        reply = f"⚠️ AI error: {e}"

    await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    webhook = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if webhook:  # Render free: Telegram ka request service ko jaga deta hai
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook}/{BOT_TOKEN}",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
