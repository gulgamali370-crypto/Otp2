#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py

Telegram OTP bot integrated with x.mnitnetwork.com
- /start : welcome message
- /range <prefix> : appends XXX (if missing), calls API to allocate a number, persists mapping (number -> telegram chat_id),
                   informs user "please wait" and edits message with allocated number when available.
- /my : list user's allocated numbers
- /allocs [date page status] : admin-only (ADMIN_CHAT_ID) calls getnum/info endpoint and returns raw response
- /callback : HTTP webhook endpoint for API to POST incoming SMS/OTP payloads; forwards OTP to mapped Telegram user
             (verifies header 'mapikey' or optional WEBHOOK_SECRET header)
Configuration via environment variables:
  TELEGRAM_TOKEN, MAPIKEY, API_BASE (default https://x.mnitnetwork.com), WEBHOOK_SECRET, ADMIN_CHAT_ID, PORT

Save only this file and requirements.txt in the deployment.
"""

import os
import re
import json
import time
import logging
import threading
from typing import Dict, Optional, Any
from flask import Flask, request
import requests
from filelock import FileLock
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# -------------------------
# Configuration
# -------------------------
# Defaults below use values you previously provided. Override via environment variables in production.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7108794200:AAGWA3aGPDjdYkXJ1VlOSdxBMHtuFpWzAIU")
MAPIKEY = os.getenv("MAPIKEY", "M_WH9Q3U88V")
API_BASE = os.getenv("API_BASE", "https://x.mnitnetwork.com")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")      # optional: X-Callback-Secret header value
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")       # optional admin Telegram chat id to receive unmapped OTPs
PORT = int(os.getenv("PORT", "5000"))

if not TELEGRAM_TOKEN or not MAPIKEY:
    raise RuntimeError("TELEGRAM_TOKEN and MAPIKEY must be set (or left as defaults).")

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mnit-otp-bot")

# -------------------------
# Persistence: mappings.json
# -------------------------
DATA_FILE = "mappings.json"      # stores normalized_number -> chat_id
LOCK_FILE = DATA_FILE + ".lock"
lock = FileLock(LOCK_FILE, timeout=5)

def load_mappings() -> Dict[str, int]:
    try:
        with lock:
            if not os.path.exists(DATA_FILE):
                return {}
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                return {str(k): int(v) for k, v in raw.items()}
    except Exception:
        logger.exception("Failed to load mappings")
        return {}

def save_mappings(m: Dict[str, int]) -> None:
    try:
        with lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(m, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to save mappings")

MAPPINGS: Dict[str, int] = load_mappings()

# -------------------------
# Utilities: normalize & OTP extraction
# -------------------------
def normalize_number(num: str) -> str:
    """Return digits-only representation (no leading +)."""
    if num is None:
        return ""
    s = str(num)
    return re.sub(r"\D+", "", s)

def find_chat_for_number(num_raw: str) -> Optional[int]:
    """Find mapped chat id for a received number: exact or suffix match."""
    n = normalize_number(num_raw)
    if not n:
        return None
    # exact match
    if n in MAPPINGS:
        return MAPPINGS[n]
    # try suffix matches: try last 12..6 digits
    for length in range(12, 5, -1):
        if len(n) >= length:
            suf = n[-length:]
            for stored, chat in MAPPINGS.items():
                if stored.endswith(suf):
                    return chat
    # try simple endswith (stored might include country code)
    for stored, chat in MAPPINGS.items():
        if stored.endswith(n) or stored.replace("+", "").endswith(n):
            return chat
    return None

def extract_otp(text: str) -> Optional[str]:
    """Extract OTP code from text: prefer 4-8 digit sequences, then shorter digit groups."""
    if not text:
        return None
    # common pattern: 4-8 digits
    m = re.search(r"\b(\d{4,8})\b", text)
    if m:
        return m.group(1)
    # look for patterns like FB-46541, # 77959, etc.
    m2 = re.search(r"[#:-]\s*([0-9]{3,8})\b", text)
    if m2:
        return m2.group(1)
    # fallback: any digits
    m3 = re.search(r"(\d+)", text)
    if m3:
        return m3.group(1)
    return None

# -------------------------
# Telegram bot: commands and helpers
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

def safe_send(chat_id: int, text: str) -> None:
    try:
        bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Failed to send message to chat_id=%s", chat_id)

def cmd_start(update: Update, context: CallbackContext) -> None:
    txt = (
        "Welcome to OTP Bot.\n\n"
        "Commands:\n"
        "/range <prefix>  - allocate number(s) for given prefix (XXX appended automatically)\n"
        "/my              - list your allocated numbers\n"
        "/allocs [date page status] - admin: query allocations info endpoint\n\n"
        "Example: /range 88017 -> will call API with 88017XXX"
    )
    update.message.reply_text(txt)

def cmd_my(update: Update, context: CallbackContext) -> None:
    chat = update.effective_chat.id
    items = [k for k, v in MAPPINGS.items() if v == chat]
    if not items:
        update.message.reply_text("You have no allocated numbers.")
    else:
        update.message.reply_text("Your numbers:\n" + "\n".join(items))

def api_allocate(range_with_xxx: str) -> Dict[str, Any]:
    """Call allocate endpoint using documentation format."""
    url = f"{API_BASE.rstrip('/')}/mapi/v1/mdashboard/getnum/number"
    headers = {"content-type": "application/json", "mapikey": MAPIKEY}
    payload = {"range": range_with_xxx, "is_national": None, "remove_plus": None}
    logger.info("Calling allocate API url=%s payload=%s", url, payload)
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def cmd_range(update: Update, context: CallbackContext) -> None:
    """Handle /range command: append XXX if missing, call API, persist mapping, inform user."""
    chat = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Usage: /range <prefix>. Example: /range 88017")
        return
    prefix = context.args[0].strip()
    if not prefix:
        update.message.reply_text("Invalid prefix.")
        return
    # Ensure XXX appended
    if not prefix.endswith("XXX"):
        req_range = prefix + "XXX"
    else:
        req_range = prefix
    # Send initial wait message
    m = update.message.reply_text(f"Requesting numbers for {req_range}... please wait.")
    try:
        resp_json = api_allocate(req_range)
        # parse response as per provided example: resp["data"]["number"] or ["full_number"] or ["copy"]
        data = resp_json.get("data") if isinstance(resp_json, dict) else None
        if not data:
            m.edit_text("Allocation response missing data. See admin logs.")
            logger.error("Allocation response no data: %s", resp_json)
            return
        number_raw = data.get("number") or data.get("full_number") or data.get("copy")
        if not number_raw:
            m.edit_text("Allocation succeeded but number not found in response.")
            logger.error("No number field in data: %s", data)
            return
        norm = normalize_number(number_raw)
        # store mapping
        MAPPINGS[norm] = chat
        save_mappings(MAPPINGS)
        # build reply
        lines = [f"✅ Number allocated: {number_raw}"]
        if data.get("country"):
            lines.append(f"Country: {data.get('country')}")
        if data.get("operator"):
            lines.append(f"Operator: {data.get('operator')}")
        if data.get("status"):
            lines.append(f"Status: {data.get('status')}")
        lines.append(f"Range: {req_range}")
        if resp_json.get("message"):
            lines.append(f"Info: {resp_json.get('message')}")
        m.edit_text("\n".join(lines))
        logger.info("Mapped %s -> chat %s", norm, chat)
    except requests.HTTPError as he:
        logger.exception("HTTP error on allocate")
        try:
            m.edit_text(f"Allocation failed (HTTP): {he}\n{he.response.text if he.response is not None else ''}")
        except Exception:
            pass
    except Exception as e:
        logger.exception("Error during allocation")
        try:
            m.edit_text(f"Allocation error: {e}")
        except Exception:
            pass

def cmd_allocs(update: Update, context: CallbackContext) -> None:
    """Admin command to call info endpoint: GET /mapi/v1/mdashboard/getnum/info?date=...&page=...&status=..."""
    chat = str(update.effective_chat.id)
    if ADMIN_CHAT_ID and chat != str(ADMIN_CHAT_ID):
        update.message.reply_text("Not authorized.")
        return
    args = context.args
    date = args[0] if len(args) > 0 else ""
    page = args[1] if len(args) > 1 else "1"
    status = args[2] if len(args) > 2 else "success"
    url = f"{API_BASE.rstrip('/')}/mapi/v1/mdashboard/getnum/info"
    headers = {"content-type": "application/json", "mapikey": MAPIKEY}
    params = {"date": date, "page": page, "search": "", "status": status}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        # send up to 4000 chars due to Telegram limit
        update.message.reply_text(r.text[:4000])
    except Exception:
        logger.exception("Failed to fetch allocations info")
        update.message.reply_text("Failed to fetch allocations info. Check server logs.")

# -------------------------
# Telegram polling thread
# -------------------------
def run_telegram():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("range", cmd_range))
    dp.add_handler(CommandHandler("my", cmd_my))
    dp.add_handler(CommandHandler("allocs", cmd_allocs))
    updater.start_polling()
    logger.info("Telegram polling started")
    updater.idle()

# -------------------------
# Flask HTTP webhook for incoming SMS/OTP
# -------------------------
app = Flask(__name__)

@app.route("/callback", methods=["POST"])
def callback():
    # Verify incoming request using mapikey header or optional WEBHOOK_SECRET
    header_mapikey = request.headers.get("mapikey") or request.headers.get("Mapikey")
    header_secret = request.headers.get("X-Callback-Secret") or request.headers.get("x-callback-secret")
    if WEBHOOK_SECRET:
        if header_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch: %s", header_secret)
            return ("forbidden", 403)
    else:
        if header_mapikey != MAPIKEY:
            logger.warning("Webhook mapikey mismatch: %s", header_mapikey)
            return ("forbidden", 403)

    try:
        payload = request.get_json(force=True, silent=True)
    except Exception:
        payload = None

    if not payload or not isinstance(payload, dict):
        logger.warning("Invalid callback payload: %s", request.get_data(as_text=True))
        return ("bad request", 400)

    # Determine the number field; examples from docs: copy, full_number, number, to, msisdn
    num_field_candidates = ["to", "number", "full_number", "msisdn", "copy"]
    num_raw = None
    for k in num_field_candidates:
        if k in payload and payload.get(k):
            num_raw = payload.get(k)
            break

    # Determine message field and otp field
    msg_candidates = ["message", "text", "body"]
    msg_text = ""
    for k in msg_candidates:
        if k in payload and payload.get(k):
            msg_text = str(payload.get(k))
            break
    # direct otp field often present
    otp_field = payload.get("otp") or payload.get("code")
    otp_val = None
    if otp_field is not None:
        otp_val = str(otp_field)
    else:
        # try to extract from message text
        otp_val = extract_otp(msg_text)

    if not num_raw:
        # no number -> notify admin if configured
        logger.warning("Callback missing number field: %s", payload)
        if ADMIN_CHAT_ID:
            safe_send(int(ADMIN_CHAT_ID), "Callback missing number field. Payload:\n" + json.dumps(payload)[:1500])
        return ("missing number", 400)

    mapped_chat = find_chat_for_number(num_raw)
    human_readable_num = num_raw if isinstance(num_raw, str) else str(num_raw)
    if otp_val:
        forward_text = f"✅ OTP for {human_readable_num}: {otp_val}"
    else:
        forward_text = f"⚠️ OTP callback for {human_readable_num} but OTP not extracted. Message:\n{msg_text}"

    if mapped_chat:
        safe_send(mapped_chat, forward_text)
        logger.info("Forwarded OTP for %s to chat %s", normalize_number(num_raw), mapped_chat)
        return ("ok", 200)
    else:
        logger.info("No mapping for %s; payload: %s", normalize_number(num_raw), payload)
        if ADMIN_CHAT_ID:
            safe_send(int(ADMIN_CHAT_ID), "UNMAPPED OTP:\n" + forward_text + "\nPayload:\n" + json.dumps(payload)[:1500])
        return ("unmapped", 200)

# -------------------------
# Entrypoint: start Telegram thread and Flask app
# -------------------------
if __name__ == "__main__":
    t = threading.Thread(target=run_telegram, daemon=True)
    t.start()
    logger.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)
