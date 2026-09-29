"""
Roh4n API key bot. Users press /start, get a key; /newkey rotates it; /usage shows hits.

Env:  BOT_TOKEN    token from @BotFather
      API_BASE     your API URL, e.g. https://api.yourdomain.com
      ADMIN_TOKEN  same value as on the API server
Run:  python bot.py
"""
import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_BASE = os.environ["API_BASE"].rstrip("/")  # public URL shown to users
CALL_BASE = os.environ.get("INTERNAL_BASE", API_BASE).rstrip("/")  # where the bot calls the API
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}

WELCOME = (
    "🔸 Roh4n API\n"
    "🚀 Powerful • Fast • Stable\n\n"
    "Use /getkey to create your API key.\n"
    "Use /usage to see your request count.\n\n"
    "Setup: download Youtube.py, replace your old file, add your API key, restart your bot."
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    owner = str(update.effective_user.id)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{CALL_BASE}/admin/keys", params={"owner": owner}, headers=HEADERS)
    if r.status_code != 200:
        await update.message.reply_text("Could not create a key right now. Try again later.")
        return
    key = r.json()["api_key"]
    await update.message.reply_text(
        f"✅ Your API key (any previous key is now disabled):\n\n<code>{key}</code>\n\n"
        f"API URL: <code>{API_BASE}</code>\n"
        "Keep it private. Send /getkey again any time to get a new one.",
        parse_mode="HTML",
    )


async def usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    owner = str(update.effective_user.id)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{CALL_BASE}/admin/usage", params={"owner": owner}, headers=HEADERS)
    if r.status_code == 404:
        await update.message.reply_text("You have no active key. Send /getkey first.")
    elif r.status_code != 200:
        await update.message.reply_text("Could not fetch usage right now.")
    else:
        await update.message.reply_text(f"📊 Requests so far: {r.json()['hits']}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getkey", getkey))
    app.add_handler(CommandHandler("usage", usage))
    app.run_polling()


if __name__ == "__main__":
    main()
