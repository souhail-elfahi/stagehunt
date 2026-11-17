"""One-off: read the bot's getUpdates and store your chat id in .env (never prints secrets)."""

import sys

import requests
from dotenv import dotenv_values, set_key

ENV = ".env"
token = dotenv_values(ENV).get("TELEGRAM_TOKEN")
if not token:
    sys.exit("TELEGRAM_TOKEN is empty in .env")

try:
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    data = resp.json()
except Exception as exc:  # the exception text can contain the token: don't show it
    sys.exit(f"getUpdates failed ({type(exc).__name__})")
if not data.get("ok"):
    sys.exit(f"getUpdates refused: {data.get('description', 'unknown error')}")

chats = [u["message"]["chat"] for u in data["result"] if "message" in u]
private = [c for c in chats if c.get("type") == "private"]
if not private:
    sys.exit("No private message found: send any message to the bot, then re-run.")
chat = private[-1]
set_key(ENV, "TELEGRAM_CHAT_ID", str(chat["id"]), quote_mode="never")
print(f"chat id saved (from {chat.get('first_name', '?')})")
