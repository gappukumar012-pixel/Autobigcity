"""
BigCity Firebase Monitor Bot (Strict Time + Redeem Link + Backup)
"""
import asyncio
import json
import os
import re
import sys
import time
import logging
import httpx
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID_ENV = os.environ.get("ADMIN_ID", "").strip()
SAVE_URL = os.environ.get("SAVE_URL", "").strip().rstrip("/")

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "bot_state.json"
ADMINS_FILE = BASE_DIR / "admins.json"
PANELS_FILE = BASE_DIR / "panels.json"
MAINTENANCE_FILE = BASE_DIR / "maintenance.json"

BOT_START_TIME = time.time() 
IS_INITIALIZED = False
IS_MAINTENANCE = False
MAX_CONCURRENT = 30
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
OTP_WATCH_SECONDS = 300
API_TIMEOUT = 8
MONITOR_INTERVAL = 5

active_watches = set()
panel_spam_tracker = {} 
process_lock = asyncio.Lock() 

# ─── MESSAGE PATTERNS ────────────────────────────────────────────────────────
REWARD_PATTERN = re.compile(r"Reward\s*Code\s*(?:is|:)\s*([A-Z0-9]{6,20})", re.IGNORECASE)
OTP_PATTERN    = re.compile(r"OTP\s*to\s*register\s*(?:is|:)\s*(\d{7})", re.IGNORECASE)
BIGCITY_HINT   = re.compile(r"big\s*city", re.IGNORECASE)

# ─── CLOUD & LOCAL DATA MANAGEMENT ───────────────────────────────────────────

def load_json(path, default):
    filename = path.name
    if SAVE_URL:
        try:
            r = requests.get(f"{SAVE_URL}/bot_backup/{filename}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data is not None: return data
        except Exception: pass

    try:
        if path.exists():
            with open(path) as f: return json.load(f)
    except Exception: pass
    return default

def save_json(path, data):
    filename = path.name
    if SAVE_URL:
        try:
            requests.put(f"{SAVE_URL}/bot_backup/{filename}", json=data, timeout=10)
        except Exception: pass

    try:
        with open(path, "w") as f: json.dump(data, f, indent=2)
    except Exception: pass

def load_panels(): return load_json(PANELS_FILE, {})
def save_panels(p): save_json(PANELS_FILE, p)
def load_state(): return load_json(STATE_FILE, {})
def save_state(s): save_json(STATE_FILE, s)

# ─── MAINTENANCE MODE ────────────────────────────────────────────────────────
def load_maintenance():
    global IS_MAINTENANCE
    data = load_json(MAINTENANCE_FILE, {"status": False})
    IS_MAINTENANCE = data.get("status", False)

def save_maintenance(status):
    global IS_MAINTENANCE
    IS_MAINTENANCE = status
    save_json(MAINTENANCE_FILE, {"status": status})

# ─── ADMIN MANAGEMENT ────────────────────────────────────────────────────────

def get_admins():
    admins = load_json(ADMINS_FILE, [])
    if ADMIN_ID_ENV.isdigit() and int(ADMIN_ID_ENV) not in admins:
        admins.append(int(ADMIN_ID_ENV))
    return admins

def save_admins(admins):
    save_json(ADMINS_FILE, list(set(admins)))

def is_admin(cid: int):
    admins = get_admins()
    if not admins:
        save_admins([cid])
        return True
    return cid in admins

# ─── FIREBASE HELPERS ────────────────────────────────────────────────────────

def parse_firebase_link(link: str):
    try:
        p = urlparse(link.strip())
        host = p.netloc.lower()
        if "firebaseio.com" not in host and "firebasedatabase.app" not in host:
            return None
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception: return None

async def api_get(client, url, timeout=API_TIMEOUT):
    async with semaphore:
        try:
            r = await client.get(url, timeout=timeout)
            if r.status_code == 200: return r.json(), None
            return None, f"HTTP {r.status_code}"
        except Exception as e: return None, str(e)[:80]

def is_device_id(k: str) -> bool:
    if not isinstance(k, str): return False
    if k.lower() in {"messages","clients","devices","users","all_devices",
                      "nodes","settings","sms","logs","status","info","admin","config"}:
        return False
    return 6 <= len(k) <= 60

async def discover_structure(client, base):
    root, _ = await api_get(client, f"{base}/.json?shallow=true", 10)
    if not isinstance(root, dict): return None, None
    keys = list(root.keys())
    for c in ("clients", "devices", "users", "all_devices", "nodes"):
        if c in keys:
            sub, _ = await api_get(client, f"{base}/{c}.json?shallow=true", 10)
            if isinstance(sub, dict) and any(is_device_id(k) for k in sub.keys()):
                msg = c
                for m in ("messages", "sms", "logs"):
                    if m in keys: msg = m; break
                return c, msg
    if any(is_device_id(k) for k in keys):
        msg = ""
        for c in ("messages", "sms", "logs"):
            if c in keys: msg = c; break
        return "", msg
    return None, None

def device_is_online(d: dict) -> bool:
    if not isinstance(d, dict): return False
    for f in ("online", "isOnline", "active", "connected", "status", "state"):
        if f in d:
            v = str(d[f]).lower()
            if v in ("true", "online", "1", "yes", "active", "connected"): return True
            if v in ("false", "offline", "0", "no", "inactive"): return False
    return False

def clean_number(val: str) -> str:
    if not val: return ""
    digits = re.sub(r'\D', '', str(val))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10: return digits
    return ""

def _extract_number(d: dict) -> str:
    if not isinstance(d, dict): return ""
    for f in ("Phone Number", "phoneNumber", "phone", "number", "mobile", "Phone #1", "Phone #2"):
        if f in d and d[f]:
            cleaned = clean_number(d[f])
            if cleaned: return cleaned
    return ""

def get_msg_timestamp(md: dict) -> float:
    dt_str = md.get("dateTime", md.get("time", md.get("timestamp", "")))
    if not dt_str: return 0.0
    if isinstance(dt_str, (int, float)):
        return (dt_str / 1000.0) if dt_str > 1e12 else float(dt_str)
    
    dt_str = str(dt_str).strip()
    match = re.search(r'(\d{1,2}-\d{1,2}-\d{4})\s*\|\s*(\d{1,2}:\d{2}\s*[apAP][mM])', dt_str)
    if match:
        try:
            date_part = match.group(1)
            time_part = match.group(2).lower()
            dt = datetime.strptime(f"{date_part} {time_part}", "%d-%m-%Y %I:%M %p")
            return dt.timestamp()
        except Exception: pass
    return 0.0

async def fetch_online_devices(client, base, dev_node):
    path = f"/{dev_node}" if dev_node else ""
    data, _ = await api_get(client, f"{base}{path}.json", 15)
    if not isinstance(data, dict): return []
    out = []
    for did, dd in data.items():
        if not is_device_id(did) or not isinstance(dd, dict) or not device_is_online(dd): continue
        num = _extract_number(dd) 
        out.append((did, num))
    return out

async def fetch_last_messages(client, base, msg_node, dev_id, limit=15):
    path = f"/{msg_node}" if msg_node else ""
    url = f'{base}{path}/{dev_id}.json?orderBy="%24key"&limitToLast={limit}'
    data, _ = await api_get(client, url, 10)
    return data if isinstance(data, dict) else {}

def classify_bigcity(text: str):
    if not text or not BIGCITY_HINT.search(text): return None, None
    m = REWARD_PATTERN.search(text)
    if m: return "reward", m.group(1)
    m = OTP_PATTERN.search(text)
    if m: return "otp", m.group(1)
    return None, None

def extract_sender_time(md: dict):
    dt = md.get("dateTime", md.get("time", md.get("timestamp","")))
    if isinstance(dt, (int, float)):
        try: dt = datetime.fromtimestamp(dt/1000 if dt>1e12 else dt).strftime("%d-%m-%Y | %I:%M %p")
        except Exception: dt = str(dt)
    return str(dt)

def extract_text(md: dict):
    for f in ("message","body","text","msg","SMS","content"):
        if f in md and md[f]: return str(md[f])
    return ""

# ─── PREMIUM FORMATTERS (With Text Link) ─────────────────────────────────────

def fmt_reward(panel, did, num, dt, code):
    # Button for OTP Catcher only
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 OTP Catcher", callback_data=f"otp|{panel}~{did}~{int(time.time())}")]])
    
    # Yahan clickable link use kiya hai (<a href="...">)
    txt = (
        "🎁 <b>𝐁𝐈𝐆𝐂𝐈𝐓𝐘 𝐑𝐄𝐖𝐀𝐑𝐃</b> 🎁\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>𝐏𝐚𝐧𝐞𝐥:</b> {panel}\n"
        f"📲 <b>𝐃𝐞𝐯𝐢𝐜𝐞:</b> <code>{did}</code>\n"
        f"🔢 <b>𝐍𝐮𝐦𝐛𝐞𝐫:</b> <code>{num or 'N/A'}</code>\n"
        f"⏰ <b>𝐓𝐢𝐦𝐞:</b> {dt}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎟 <b>𝐂𝐨𝐝𝐞:</b> <code>{code}</code>\n\n"
        "👇 <b>Redeem Here:</b> <a href='https://www.golddropwins.com/'>golddropwins.com</a>"
    )
    return txt, btn

def fmt_otp(panel, did, num, dt, otp):
    return (
        "🔐 <b>𝐁𝐈𝐆𝐂𝐈𝐓𝐘 𝐎𝐓𝐏</b> 🔐\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>𝐏𝐚𝐧𝐞𝐥:</b> {panel}\n"
        f"📲 <b>𝐃𝐞𝐯𝐢𝐜𝐞:</b> <code>{did}</code>\n"
        f"🔢 <b>𝐍𝐮𝐦𝐛𝐞𝐫:</b> <code>{num or 'N/A'}</code>\n"
        f"⏰ <b>𝐓𝐢𝐦𝐞:</b> {dt}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔑 <b>𝐎𝐓𝐏:</b> <code>{otp}</code>"
    )

# ─── MONITORING ──────────────────────────────────────────────────────────────

async def process_device(client, pk, pc, did, num, state, admins, app):
    base = pc["base"]
    msg_node = pc.get("message_node","")
    panel = pc.get("name","Panel")
    sent = 0
    try:
        msgs = await fetch_last_messages(client, base, msg_node, did, limit=15)
        if not msgs: return 0
        
        for mk, md in msgs.items():
            if not isinstance(md, dict): continue
            mid = str(md.get("id", mk))
            key = f"{pk}:{did}:{mid}"
            
            if state.get(key): continue
            
            # 🛑 Strict Time Check
            msg_time = get_msg_timestamp(md)
            if msg_time > 0 and msg_time < BOT_START_TIME:
                state[key] = True
                continue
            
            if not IS_INITIALIZED:
                state[key] = True
                continue
            
            text = extract_text(md)
            if not text:
                state[key] = True; continue
            kind, payload = classify_bigcity(text)
            if not kind:
                state[key] = True; continue
                
            # Anti-Spam (Bomber)
            now = time.time()
            if pk not in panel_spam_tracker:
                panel_spam_tracker[pk] = []
            panel_spam_tracker[pk] = [t for t in panel_spam_tracker[pk] if now - t < 10]
            
            if len(panel_spam_tracker[pk]) >= 15:
                logger.warning(f"BOMBER DETECTED ON PANEL {pk}. REMOVING PANEL.")
                panels = load_panels()
                if pk in panels:
                    del panels[pk]
                    save_panels(panels)
                for uid in admins:
                    try:
                        await app.bot.send_message(chat_id=uid, text=f"🚨 <b>PANEL AUTO-REMOVED!</b>\n\nPanel: <b>{panel}</b>\nReason: <b>Heavy Spamming</b>", parse_mode="HTML")
                    except Exception: pass
                return sent

            panel_spam_tracker[pk].append(now)
            dt = extract_sender_time(md)
            
            if kind == "reward":
                txt, btn = fmt_reward(panel, did, num, dt, payload)
                for uid in admins:
                    try:
                        await app.bot.send_message(chat_id=uid, text=txt, parse_mode="HTML", reply_markup=btn, disable_web_page_preview=True)
                    except Exception: pass
                sent += 1
            state[key] = True
    except Exception: pass
    return sent

async def monitor_panels(context: ContextTypes.DEFAULT_TYPE):
    global IS_INITIALIZED
    if IS_MAINTENANCE: return

    app = context.application
    try:
        admins = get_admins()
        if not admins: return

        async with process_lock: 
            panels = load_panels()
            state = load_state()
            
            total = 0
            async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
                for pk, pc in list(panels.items()):
                    base = pc.get("base")
                    if not base: continue
                    init_key = f"init:{pk}"
                    
                    try:
                        if pc.get("device_node") is None or pc.get("message_node") is None:
                            dn, mn = await asyncio.wait_for(discover_structure(client, base), timeout=20)
                            if dn is None: continue
                            pc["device_node"] = dn; pc["message_node"] = mn
                            save_panels(panels)
                        
                        online = await asyncio.wait_for(fetch_online_devices(client, base, pc.get("device_node","")), timeout=25)
                        if not online: continue
                        
                        if not state.get(init_key):
                            for did, _ in online:
                                try: 
                                    msgs = await asyncio.wait_for(fetch_last_messages(client, base, pc.get("message_node",""), did, limit=15), timeout=15)
                                    for mk, md in msgs.items():
                                        if isinstance(md, dict): 
                                            state[f"{pk}:{did}:{str(md.get('id', mk))}"] = True
                                except Exception: pass
                            state[init_key] = True
                            save_state(state)
                            continue
                        
                        tasks = [process_device(client, pk, pc, did, num, state, admins, app) for did, num in online]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for r in results:
                            if isinstance(r, int): total += r
                    except Exception: pass
                    
            if not IS_INITIALIZED:
                IS_INITIALIZED = True
                save_state(state)
                logger.info("STRICT INITIAL SCAN COMPLETE. BOT IS NOW ACTIVE.")
            elif total > 0:
                save_state(state)
                
    except Exception as e: logger.error(f"monitor outer: {e}")

# ─── BACKUP & RESTORE ────────────────────────────────────────────────────────

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    panels = load_panels()
    admins = get_admins()
    state = load_state()
    
    backup_data = {"panels": panels, "admins": admins, "state": state}
    backup_str = json.dumps(backup_data, indent=2)
    
    with open("backup.txt", "w") as f:
        f.write(backup_str)
        
    await update.message.reply_text("📦 <b>Backup Ready!</b>\nYeh file save kar lo, aage deploy ke baad restore karne ke liye.", parse_mode="HTML")
    await update.message.reply_document(document=open("backup.txt", "rb"), filename="bot_backup.txt")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    await update.message.reply_text("📂 <b>Restore:</b> Kripya apni backup.txt file bhejein (attach as document).", parse_mode="HTML")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    doc = update.message.document
    if not doc.file_name.endswith(".txt") and not doc.file_name.endswith(".json"):
        return await update.message.reply_text("❌ Sirf .txt ya .json file allowed hai.")
    
    file = await doc.get_file()
    await file.download_to_drive("restore_temp.txt")
    
    try:
        with open("restore_temp.txt", "r") as f:
            data = json.load(f)
            
        if "panels" in data: save_panels(data["panels"])
        if "admins" in data: save_admins(data["admins"])
        if "state" in data: save_state(data["state"])
            
        await update.message.reply_text("✅ <b>Restore Successful!</b>\nSaara data wapas aa gaya.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Restore failed: {str(e)}")

# ─── MAINTENANCE & OTP CATCHER ───────────────────────────────────────────────

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    status = "🔴 PAUSED (Maintenance)" if IS_MAINTENANCE else "🟢 ACTIVE"
    out = f"🛠 <b>Maintenance Mode</b>\n━━━━━━━━━━━━━━━━━━\nCurrent Status: {status}\n\nPause karne par bot scan rok dega."
    btn_pause = InlineKeyboardButton("⏸ Pause", callback_data="maint_pause")
    btn_resume = InlineKeyboardButton("▶️ Resume", callback_data="maint_resume")
    kb = InlineKeyboardMarkup([[btn_pause, btn_resume]])
    await update.message.reply_text(out, parse_mode="HTML", reply_markup=kb)

async def maintenance_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return await q.answer("❌ Admin Only!", show_alert=True)
    
    if q.data == "maint_pause":
        save_maintenance(True)
        await q.answer("Bot Paused (Maintenance Mode ON)", show_alert=True)
        await q.edit_message_text("🛠 <b>Maintenance Mode</b>\n\nStatus: 🔴 <b>PAUSED</b>\n\nBot ab scan nahi karega.", parse_mode="HTML")
    elif q.data == "maint_resume":
        save_maintenance(False)
        await q.answer("Bot Resumed (Maintenance Mode OFF)", show_alert=True)
        await q.edit_message_text("🛠 <b>Maintenance Mode</b>\n\nStatus: 🟢 <b>ACTIVE</b>\n\nBot wapas scan kar raha hai.", parse_mode="HTML")

async def otp_catcher_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        if not is_admin(q.from_user.id): return await q.answer("❌ Admin Only!", show_alert=True)
        await q.answer("OTP Catcher starting...")
        if not q.data.startswith("otp|"): return
        parts = q.data.split("|",1)[1].split("~")
        if len(parts) != 3: return await q.edit_message_reply_markup(reply_markup=None)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        context.application.create_task(otp_watch(context.application, q.message.chat_id, parts[0], parts[1]))
    except Exception: pass

async def stop_otp_watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        if not is_admin(q.from_user.id): return await q.answer("❌ Admin Only!", show_alert=True)
        if not q.data.startswith("stop_otp|"): return
        did = q.data.split("|",1)[1]
        if did in active_watches: active_watches.remove(did)
        await q.answer("🛑 OTP Watch Stopped!", show_alert=True)
        try: 
            await q.edit_message_reply_markup(reply_markup=None)
            await q.edit_message_text(f"🛑 <b>OTP Watch manually stopped</b> for <code>{did}</code>.", parse_mode="HTML")
        except Exception: pass
    except Exception: pass

async def otp_watch(app, chat_id, pk, did):
    try:
        active_watches.add(did)
        pc = load_panels().get(pk)
        if not pc: return await app.bot.send_message(chat_id, "❌ Panel missing.")
        base = pc["base"]; msg_node = pc.get("message_node","")
        panel = pc.get("name","Panel")
        end_at = time.time() + OTP_WATCH_SECONDS
        seen = set()
        stop_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop OTP Watch", callback_data=f"stop_otp|{did}")]])
        
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            for mk, md in (await fetch_last_messages(client, base, msg_node, did, limit=15)).items():
                if isinstance(md, dict): seen.add(str(md.get("id", mk)))
            
            dev_num = ""
            try:
                path = f"/{pc.get('device_node','')}" if pc.get("device_node") else ""
                dd, _ = await api_get(client, f"{base}{path}/{did}.json", 6)
                if isinstance(dd, dict): dev_num = _extract_number(dd)
            except Exception: pass
                
            watch_msg = await app.bot.send_message(chat_id, f"🎯 <b>OTP Catcher active</b> — <code>{did}</code>\nNumber: <code>{dev_num or 'N/A'}</code>\nMax 5 min, sirf naya OTP.\n<i>(Manually band karne ke liye niche button dabayein)</i>", parse_mode="HTML", reply_markup=stop_btn)
            last_ping = time.time()
            while time.time() < end_at:
                if did not in active_watches:
                    try: await watch_msg.edit_reply_markup(reply_markup=None)
                    except Exception: pass
                    return
                try:
                    for mk, md in (await asyncio.wait_for(fetch_last_messages(client, base, msg_node, did, limit=15), timeout=12)).items():
                        if not isinstance(md, dict): continue
                        mid = str(md.get("id", mk))
                        if mid in seen: continue
                        seen.add(mid)
                        text = extract_text(md)
                        kind, payload = classify_bigcity(text)
                        if kind != "otp": continue
                        dt = extract_sender_time(md)
                        await app.bot.send_message(chat_id, fmt_otp(panel, did, dev_num, dt, payload), parse_mode="HTML", disable_web_page_preview=True)
                        try: await watch_msg.edit_reply_markup(reply_markup=None)
                        except Exception: pass
                        await app.bot.send_message(chat_id, f"✅ <b>OTP aaya — watch band</b> (<code>{did}</code>).", parse_mode="HTML")
                        active_watches.discard(did)
                        return
                except Exception: pass
                if time.time() - last_ping > 60:
                    last_ping = time.time()
                    if did not in active_watches: return
                    try: await app.bot.send_message(chat_id, f"⏳ OTP watch running... <code>{did}</code>", parse_mode="HTML")
                    except Exception: pass
                await asyncio.sleep(8)
            active_watches.discard(did)
            try: await watch_msg.edit_reply_markup(reply_markup=None)
            except Exception: pass
            await app.bot.send_message(chat_id, f"⌛ <b>OTP watch timeout</b> (<code>{did}</code>).", parse_mode="HTML")
    except Exception as e: 
        active_watches.discard(did)
        logger.error(f"otp_watch outer: {e}")

# ─── COMMANDS ────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if not is_admin(cid): return await update.message.reply_text("❌ Aap Admin nahi hain.")
    kb = ReplyKeyboardMarkup([
        ["📊 Status","📋 My Panels"],
        ["➕ Add Panel","❌ Remove Panel"],
        ["👑 Admin Panel","🛠 Maintenance"],
        ["📦 Backup", "📂 Restore"]
    ], resize_keyboard=True)
    await update.message.reply_text(f"🤖 <b>BigCity Firebase Monitor</b>\n\n• Strict Time Check\n• Short Msg + Redeem Link\n• 🛠 Maintenance Mode\n• 📦 Backup/Restore\n\n👤 <code>{cid}</code>", parse_mode="HTML", reply_markup=kb)

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    admins = get_admins()
    out = "👑 <b>Admin Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join([f"• <code>{a}</code>" for a in admins])
    out += "\n\nNaya Admin add karne ke liye <code>/addadmin ID</code> bhejo.\nRemove karne ke liye <code>/deladmin ID</code> bhejo."
    await update.message.reply_text(out, parse_mode="HTML")

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        new_id = int(context.args[0])
        admins = get_admins()
        if new_id in admins: return await update.message.reply_text("Yeh pehle se Admin hai.")
        admins.append(new_id); save_admins(admins)
        await update.message.reply_text(f"✅ <code>{new_id}</code> ko Admin bana diya gaya.", parse_mode="HTML")
    except Exception: await update.message.reply_text("❌ Format: <code>/addadmin 123456789</code>", parse_mode="HTML")

async def del_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        del_id = int(context.args[0])
        if str(del_id) == ADMIN_ID_ENV: return await update.message.reply_text("❌ Super Admin ko remove nahi kar sakte.")
        admins = get_admins()
        if del_id not in admins: return await update.message.reply_text("Yeh Admin nahi hai.")
        admins.remove(del_id); save_admins(admins)
        await update.message.reply_text(f"✅ <code>{del_id}</code> ko hata diya gaya.", parse_mode="HTML")
    except Exception: await update.message.reply_text("❌ Format: <code>/deladmin 123456789</code>", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        panels = load_panels(); out = "📊 <b>Status</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"; total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            for pk, pc in panels.items():
                name = pc.get("name","?"); base = pc.get("base")
                if not base: out += f"<b>{name}</b> — 🔴 no url\n"; continue
                dn = pc.get("device_node")
                if dn is None:
                    try:
                        dn, mn = await asyncio.wait_for(discover_structure(client, base), timeout=15)
                        if dn is not None: pc["device_node"]=dn; pc["message_node"]=mn; save_panels(panels)
                    except Exception: pass
                if dn is None: out += f"<b>{name}</b> — 🔴 structure\n"; continue
                try:
                    online = await asyncio.wait_for(fetch_online_devices(client, base, dn), timeout=20)
                    total += len(online); out += f"<b>{name}</b> — 🟢 {len(online)} online\n"
                except Exception: out += f"<b>{name}</b> — 🔴 timeout\n"
        out += f"\n📦 <b>Online:</b> {total}"
        await update.message.reply_text(out, parse_mode="HTML")
    except Exception: pass

async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    panels = load_panels()
    if not panels: return await update.message.reply_text("❌ Koi panel nahi.")
    await update.message.reply_text("📋 <b>Panels</b>\n\n" + "\n\n".join([f"<b>{i}. {pc.get('name')}</b>\n<code>{pc.get('base','')[:50]}</code>" for i,(pk,pc) in enumerate(panels.items(),1)]), parse_mode="HTML")

async def add_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    context.user_data["awaiting_url"] = True
    await update.message.reply_text("➕ <b>Firebase URL(s) bhejo</b> — ek line ek URL.\nExample:\n<code>https://abc-default-rtdb.firebaseio.com</code>", parse_mode="HTML")

async def remove_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    panels = load_panels()
    if not panels: return await update.message.reply_text("❌ Koi panel nahi.")
    out = "❌ <b>Remove Panel</b>\n\n"; pl = []
    for i,(pk,pc) in enumerate(panels.items(),1): out += f"{i}. {pc.get('name')}\n"; pl.append(pk)
    out += "\n<i>Number(s) bhejo. Multiple remove karne ke liye space ya comma ( , ) lagao.</i>\n<i>Example: 1, 2</i>\n<i>All remove karne ke liye /removeall bhejo.</i>"
    context.user_data["awaiting_remove"] = True; context.user_data["panels_list"] = pl
    await update.message.reply_text(out, parse_mode="HTML")

async def remove_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    save_panels({})
    await update.message.reply_text("✅ <b>Saare panels remove ho gaye.</b>", parse_mode="HTML")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        text = (update.message.text or "").strip()
        if context.user_data.get("awaiting_url"):
            context.user_data["awaiting_url"] = False
            panels = load_panels(); added = 0; failed = []
            async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
                for link in [l.strip() for l in text.splitlines() if l.strip()]:
                    base = parse_firebase_link(link)
                    if not base: failed.append(f"{link[:40]} (invalid)"); continue
                    try: dn, mn = await asyncio.wait_for(discover_structure(client, base), timeout=15)
                    except Exception: dn, mn = None, None
                    if dn is None: failed.append(f"{link[:40]} (structure)"); continue
                    panels[f"p_{int(time.time())}_{added}"] = {"name": f"Panel {len(panels)+1}", "base": base, "device_node": dn, "message_node": mn, "panel_url": link, "added_date": datetime.now().strftime("%Y-%m-%d")}
                    added += 1
            save_panels(panels); msg = f"✅ {added} panel add."
            if failed: msg += "\n\n❌ Failed:\n" + "\n".join(failed[:10])
            await update.message.reply_text(msg); return
        if context.user_data.get("awaiting_remove"):
            context.user_data["awaiting_remove"] = False
            pl = context.user_data.get("panels_list", []); panels = load_panels(); removed_count = 0
            for n in re.split(r'[,\s]+', text):
                if n.isdigit():
                    idx = int(n) - 1
                    if 0 <= idx < len(pl) and panels.pop(pl[idx], None): removed_count += 1
            save_panels(panels)
            if removed_count > 0: await update.message.reply_text(f"✅ <b>{removed_count} panel(s) removed.</b>", parse_mode="HTML")
            else: await update.message.reply_text("❌ Koi valid number nahi mila.")
            return
    except Exception: pass

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN: logger.error("BOT_TOKEN missing!"); sys.exit(1)
    
    load_maintenance()

    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("deladmin", del_admin_command))
    app.add_handler(CommandHandler("removeall", remove_all_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))
    
    app.add_handler(MessageHandler(filters.Regex(r"^(📦 Backup|Backup)$"), backup_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📂 Restore|Restore)$"), restore_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.add_handler(MessageHandler(filters.Regex(r"^(📊 Status|Status)$"), status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📋 My Panels|My Panels)$"), my_panels_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(➕ Add Panel|Add Panel)$"), add_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(❌ Remove Panel|Remove Panel)$"), remove_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(👑 Admin Panel|Admin Panel)$"), admin_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(🛠 Maintenance|Maintenance)$"), maintenance_command))
    
    app.add_handler(CallbackQueryHandler(maintenance_cb, pattern=r"^maint_"))
    app.add_handler(CallbackQueryHandler(otp_catcher_cb, pattern=r"^otp\|"))
    app.add_handler(CallbackQueryHandler(stop_otp_watch_cb, pattern=r"^stop_otp\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    app.job_queue.run_repeating(
        monitor_panels, interval=MONITOR_INTERVAL, first=1,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 10}
    )
    
    logger.info("Bot starting...")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        while True: await asyncio.sleep(1)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
    except Exception as e: logger.fatal(f"Fatal: {e}")