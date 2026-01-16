"""
Telegram OTP bot for x.mnitnetwork.com

- Uses /range <range> to allocate numbers via API and maps them to the Telegram user.
- Receives OTP callbacks from the API at /callback and forwards OTP to mapped user (or admin).
- Safe JSON persistence with file lock.
- Configure via environment variables or edit defaults below (editing inline is NOT recommended for prod).
"""
import os
import json
import logging
import threading
from typing import Optional
from flask import Flask, request, abort
import requests
from filelock import FileLock
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# -------------------------
# CONFIG (you can set as env vars; defaults set to values you provided)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7108794200:AAGWA3aGPDjdYkXJ1VlOSdxBMHtuFpWzAIU")
MAPIKEY = os.getenv("MAPIKEY", "M_WH9Q3U88V")
API_BASE = os.getenv("API_BASE", "https://x.mnitnetwork.com")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional extra secret
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # optional admin to receive unmapped OTPs
PORT = int(os.getenv("PORT", "5000"))

if not TELEGRAM_TOKEN or not MAPIKEY:
    raise RuntimeError("Set TELEGRAM_TOKEN and MAPIKEY environment variables")

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------------
# Persistence
# -------------------------
DATA_FILE = "mappings.json"         # stores: { "<number>": <chat_id>, ... }
LOCK_FILE = DATA_FILE + ".lock"
lock = FileLock(LOCK_FILE, timeout=5)

def load_mappings() -> dict:
    try:
        with lock:
            if not os.path.exists(DATA_FILE):
                return {}
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("Failed to load mappings")
        return {}

def save_mappings(data: dict):
    try:
        with lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception:
        logger.exception("Failed to save mappings")

MAPPINGS = load_mappings()

def normalize_number(num: str) -> str:
    if not num:
        return num
    return num.strip()

# -------------------------
# Telegram bot setup
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

def send_message(chat_id: int, text: str):
    try:
        bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Failed to send Telegram message")

def start(update: Update, context: CallbackContext):
    update.message.reply_text("OTP bot ready. Use /range <range-spec> to allocate numbers. /my to list yours.")

def range_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Usage: /range <range-spec>  e.g. /range 88017XXX")
        return
    range_spec = context.args[0]
    url = f"{API_BASE}/mapi/v1/mdashboard/getnum/number"
    headers = {"content-type": "application/json", "mapikey": MAPIKEY}
    payload = {"range": range_spec, "is_national": None, "remove_plus": None}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        r.raise_for_status()
        resp = r.json()
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        number = data.get("number") or data.get("full_number") or data.get("copy")
        if not number:
            update.message.reply_text(f"Allocation failed or unexpected response:\n{json.dumps(resp)[:1000]}")
            logger.error("Allocation unexpected response: %s", resp)
            return
        number = normalize_number(number)
        # persist mapping
        MAPPINGS[number] = chat_id
        save_mappings(MAPPINGS)
        update.message.reply_text(f"Allocated: {number}")
        logger.info("Allocated %s -> %s", number, chat_id)
    except requests.HTTPError as he:
        logger.exception("HTTP error during allocation")
        update.message.reply_text(f"Allocation HTTP error: {he}")
    except Exception as e:
        logger.exception("Error during allocation")
        update.message.reply_text(f"Allocation error: {e}")

def my_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    items = [n for n, cid in MAPPINGS.items() if cid == chat_id]
    if not items:
        update.message.reply_text("No numbers allocated to you.")
    else:
        update.message.reply_text("Your numbers:\n" + "\n".join(items))

# optional admin command to query API info endpoint
def allocations_command(update: Update, context: CallbackContext):
    chat_id = str(update.effective_chat.id)
    if ADMIN_CHAT_ID and chat_id != str(ADMIN_CHAT_ID):
        update.message.reply_text("Not authorized.")
        return
    # allow args: date page status
    args = context.args
    params = {
        "date": args[0] if len(args) > 0 else "",
        "page": args[1] if len(args) > 1 else "1",
        "search": "",
        "status": args[2] if len(args) > 2 else "success"
    }
    try:
        url = f"{API_BASE}/mapi/v1/mdashboard/getnum/info"
        headers = {"content-type": "application/json", "mapikey": MAPIKEY}
        r = requests.get(url, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        text = r.text
        update.message.reply_text(text[:4000])
    except Exception:
        logger.exception("Failed to fetch allocations")
        update.message.reply_text("Fetch failed; check server logs.")

def run_telegram_polling():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("range", range_command))
    dp.add_handler(CommandHandler("my", my_command))
    dp.add_handler(CommandHandler("allocations", allocations_command))
    updater.start_polling()
    updater.idle()

# -------------------------
# Flask app for webhook callbacks
# -------------------------
app = Flask(__name__)

@app.route("/callback", methods=["POST"])
def callback():
    # verify: either mapikey header or optional WEBHOOK_SECRET
    header_mapikey = request.headers.get("mapikey") or request.headers.get("Mapikey")
    header_secret = request.headers.get("X-Callback-Secret")
    if WEBHOOK_SECRET:
        if header_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch")
            return ("forbidden", 403)
    else:
        if header_mapikey != MAPIKEY:
            logger.warning("Webhook mapikey mismatch")
            return ("forbidden", 403)

    data = request.get_json(silent=True, force=True)
    if not data:
        logger.warning("Callback missing JSON")
        return ("no json", 400)

    # try common keys for number and otp
    to = data.get("to") or data.get("number") or data.get("full_number") or data.get("msisdn")
    otp = data.get("otp") or data.get("code") or data.get("message") or data.get("text")
    if not to or not otp:
        logger.warning("Callback missing fields: %s", data)
        return ("missing fields", 400)

    to = normalize_number(to)
    text = f"OTP for {to}: {otp}"
    chat_id = MAPPINGS.get(to)
    if chat_id:
        send_message(chat_id, text)
        logger.info("Forwarded OTP for %s to %s", to, chat_id)
    else:
        # try matching without '+' or with/without country prefix
        alt = to.lstrip("+0")
        found = None
        for k, v in MAPPINGS.items():
            if k.endswith(alt) or k.replace("+","").endswith(alt):
                found = v
                break
        if found:
            send_message(found, text)
            logger.info("Forwarded OTP (alt match) for %s to %s", to, found)
        elif ADMIN_CHAT_ID:
            send_message(int(ADMIN_CHAT_ID), f"UNMAPPED: {text}\npayload: {json.dumps(data)[:1000]}")
            logger.info("Sent unmapped OTP to admin")
        else:
            logger.info("No mapping for %s and no admin set", to)

    return ("ok", 200)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    # start telegram polling in background thread and run Flask
    t = threading.Thread(target=run_telegram_polling, daemon=True)
    t.start()
    logger.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)
