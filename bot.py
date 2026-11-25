import os, time, asyncio, logging, datetime
import json, secrets, tempfile, io
import httpx
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters)
from store import Store

logging.basicConfig(level=logging.INFO)
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ADMIN_USERNAMES = {x.lstrip("@").lower() for x in os.environ.get("ADMIN_USERNAMES", "").replace(" ", "").split(",") if x}
SEED_CHANNELS = [x for x in os.environ.get("SEED_CHANNELS", "").replace(" ", "").split(",") if x]
DB_PATH = os.environ.get("DB_PATH", "/tmp/bot.db")
BACKUP = {"msg": None}
MAX_CHANNELS = 10
PAGE = 8
START = time.time()

def restore_db():
    """Render's disk is wiped on restart: restore the DB from the pinned backup in the first admin's chat."""
    if os.path.exists(DB_PATH) or not ADMIN_IDS:
        return
    try:
        api = "https://api.telegram.org/bot" + TOKEN
        chat = httpx.get(api + "/getChat", params={"chat_id": min(ADMIN_IDS)}, timeout=30).json()["result"]
        pm = chat.get("pinned_message", {})
        doc = pm.get("document")
        if doc and doc.get("file_name") == "backup.db":
            fp = httpx.get(api + "/getFile", params={"file_id": doc["file_id"]}, timeout=30).json()["result"]["file_path"]
            data = httpx.get("https://api.telegram.org/file/bot" + TOKEN + "/" + fp, timeout=60).content
            with open(DB_PATH, "wb") as f:
                f.write(data)
            BACKUP["msg"] = pm.get("message_id")
            logging.info("db restored from pinned backup (%d bytes)", len(data))
    except Exception as e:
        logging.warning("restore failed: %s", e)


restore_db()
store = Store(DB_PATH)
for c in SEED_CHANNELS:
    store.add_channel(c if c.startswith("@") else "@" + c)


# ---------------- helpers ----------------
def is_admin(user) -> bool:
    if user.id in ADMIN_IDS or user.id in store.admins():
        return True
    if user.username and user.username.lower() in ADMIN_USERNAMES:
        key = "bound_" + user.username.lower()
        bound = store.get(key)
        if not bound:  # first contact binds the numeric ID; after that only the ID counts
            store.set(key, str(user.id))
            store.add_admin(user.id)
            store.log("admin_bound", user.id, "@" + user.username)
            return True
        return bound == str(user.id)
    return False


def tick(v):
    return "✅" if v else "❌"


def back(to="main"):
    return [B("• رجوع •", callback_data=f"a:{to}")]


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ---------------- gate (runs before every handler) ----------------
async def check_channels(bot, uid):
    """Returns (missing_refs, errors{ref:msg}). Errors fail open."""
    missing, errors = [], {}
    for _id, ref, enabled in store.channels():
        if not enabled:
            continue
        try:
            m = await bot.get_chat_member(ref, uid)
            if m.status in ("left", "kicked"):
                missing.append(ref)
        except TelegramError as e:
            errors[ref] = str(e)
    return missing, errors


def gate_markup(missing):
    rows = [[B("📢 " + r, url="https://t.me/" + r.lstrip("@"))] for r in missing]
    if store.on("gate_button"):
        rows.append([B("✅ تحقق", callback_data="g:verify")])
    return M(rows)


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return
    admin = is_admin(user)
    inviter = None
    msg = update.message
    if msg and msg.text and msg.text.startswith("/start ref"):
        try:
            inviter = int(msg.text.split("ref", 1)[1])
        except ValueError:
            pass
    new = store.touch_user(user.id, user.full_name, user.username or "", inviter if inviter != user.id else None)
    if new:
        store.log("join", user.id, user.full_name)
        if store.on("notify_join"):
            for a in set(store.admins()) | ADMIN_IDS:
                if a != user.id:
                    try:
                        await ctx.bot.send_message(a, f"👤 مستخدم جديد: {user.full_name} (@{user.username or '-'}) ID {user.id}")
                    except TelegramError:
                        pass
    if admin:
        return
    if store.is_banned(user.id):
        if store.on("ban_notice") and msg:
            await msg.reply_text(store.get("ban_msg"))
        raise ApplicationHandlerStop
    if store.on("maintenance"):
        if msg:
            await msg.reply_text("🛠 البوت تحت الصيانة، حاول لاحقاً.")
        elif update.callback_query:
            await update.callback_query.answer("🛠 البوت تحت الصيانة", show_alert=True)
        raise ApplicationHandlerStop
    if not store.on("gate_on"):
        return
    q = update.callback_query
    missing, errors = await check_channels(ctx.bot, user.id)
    if errors and store.on("gate_notify"):
        seen = ctx.bot_data.setdefault("gate_err_sent", set())
        for ref, e in errors.items():
            if ref not in seen:
                seen.add(ref)
                for a in set(store.admins()) | ADMIN_IDS:
                    try:
                        await ctx.bot.send_message(a, f"⚠️ ما أكدر أتحقق من القناة {ref}. أضف البوت أدمن بالقناة.\n{e}")
                    except TelegramError:
                        pass
    if not missing:
        if q and q.data == "g:verify":
            await q.answer("✅ تم التحقق")
            await q.message.reply_text(store.get("welcome"))
            raise ApplicationHandlerStop
        return
    text = store.get("gate_msg")
    if q:
        await q.answer("اشترك أول" if q.data != "g:verify" else "ما زلت غير مشترك", show_alert=True)
        if q.data != "g:verify":
            await q.message.reply_text(text, reply_markup=gate_markup(missing))
    elif msg:
        await msg.reply_text(text, reply_markup=gate_markup(missing))
    raise ApplicationHandlerStop


# ---------------- user commands ----------------
async def start(update: Update, ctx):
    if ctx.args and not ctx.args[0].startswith("ref") and store.get_link(ctx.args[0]):
        await deliver(update, ctx, ctx.args[0])
        return
    if is_admin(update.effective_user):  # admin sees the panel on /start
        ctx.user_data["me"] = update.effective_user.id
        t, m = screen("main", ctx)
        await update.message.reply_text(t, reply_markup=m)
        return
    await update.message.reply_text(store.get("welcome"))


async def report(update: Update, ctx):
    url, sup = store.get("report_url"), store.get("support")
    await update.message.reply_text(sup + (f"\n{url}" if url else ""))


# ---------------- links / delivery ----------------
def media_of(m):
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    return None, None


def link_url(ctx, code):
    return f"https://t.me/{ctx.bot.username}?start={code}"


async def delete_later(bot, chat_id, ids, at):
    await asyncio.sleep(max(0, at - time.time()))
    for i in ids:
        try:
            await bot.delete_message(chat_id, i)
        except TelegramError:
            pass
    store.del_pending(chat_id, ids)


async def deliver(update: Update, ctx, code):
    items, _v = store.get_link(code)
    chat_id = update.effective_chat.id
    delay = int(store.get("delay"))
    protect = store.on("protect")
    ids = []
    for it in items:
        fn = {"photo": ctx.bot.send_photo, "video": ctx.bot.send_video,
              "animation": ctx.bot.send_animation, "document": ctx.bot.send_document}[it["t"]]
        try:
            m = await fn(chat_id, it["f"], protect_content=protect)
            ids.append(m.message_id)
        except TelegramError as e:
            logging.warning("send failed: %s", e)
    n = await ctx.bot.send_message(chat_id, store.get("note").replace("{s}", str(delay)), protect_content=protect)
    ids.append(n.message_id)
    at = time.time() + delay
    store.add_pending(chat_id, ids, at)
    store.inc_views(code)
    store.log("view", update.effective_user.id, code)
    asyncio.create_task(delete_later(ctx.bot, chat_id, ids, at))


async def link_buttons(update: Update, ctx, parts):
    q = update.callback_query
    act = parts[1]
    if act == "new":
        ctx.user_data["collect"] = []
        await q.message.reply_text("أرسل الصور والفيديوهات اللي تريدها بهذا الرابط (واحد واحد أو مجموعة). بعدها اضغط إنهاء.",
                                   reply_markup=M([[B("✅ إنهاء وإنشاء الرابط", callback_data="n:done")],
                                                   [B("❌ إلغاء", callback_data="n:cancel")]]))
    elif act == "done":
        items = ctx.user_data.pop("collect", None)
        if not items:
            await q.message.reply_text("ما أضفت أي ملف. ابدأ من جديد من 🔗 الروابط.")
            return
        code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
        store.add_link(code, items)
        store.log("link_new", update.effective_user.id, code)
        await q.message.reply_text(f"✅ تم إنشاء الرابط ({len(items)} ملف):\n{link_url(ctx, code)}")
    elif act == "cancel":
        ctx.user_data.pop("collect", None)
        await q.message.reply_text("أُلغيت.")
    elif act == "show":
        await q.message.reply_text(link_url(ctx, parts[2]))
    elif act == "del":
        store.del_link(parts[2])
        t, m = screen("links", ctx)
        await edit(q, t, m)


async def collect_media(update: Update, ctx):
    if not is_admin(update.effective_user):
        return
    col = ctx.user_data.get("collect")
    if col is None:
        return
    t, f = media_of(update.message)
    col.append({"t": t, "f": f})
    await update.message.reply_text(f"➕ أضفت ({len(col)})",
                                    reply_markup=M([[B("✅ إنهاء وإنشاء الرابط", callback_data="n:done")]]))


async def backup_loop(app):
    while True:
        await asyncio.sleep(20)
        if not store.dirty or not ADMIN_IDS:
            continue
        try:
            chat_id = min(ADMIN_IDS)
            tmp = tempfile.mktemp(suffix=".db")
            await asyncio.to_thread(store.snapshot, tmp)
            with open(tmp, "rb") as f:
                m = await app.bot.send_document(chat_id, f, filename="backup.db",
                                                caption="💾 نسخة البيانات (لا تحذفها)", disable_notification=True)
            os.remove(tmp)
            await app.bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
            old, BACKUP["msg"] = BACKUP["msg"], m.message_id
            if old:
                try:
                    await app.bot.delete_message(chat_id, old)
                except TelegramError:
                    pass
        except Exception as e:
            store.dirty = True
            logging.warning("backup failed: %s", e)


async def post_init(app):
    for chat, ids, at in store.pending():
        asyncio.create_task(delete_later(app.bot, chat, ids, at))
    asyncio.create_task(backup_loop(app))


# ---------------- admin panel ----------------
def screen(name, ctx):
    """Returns (text, markup) for an admin screen."""
    s = store
    if name == "main":
        rows = [
            [B("🔗 الروابط والمحتوى", callback_data="a:links")],
            [B("📝 المحتوى", callback_data="a:content"), B("⚙️ الإعدادات", callback_data="a:settings")],
            [B("🔐 الاشتراك", callback_data="a:sub"), B("👥 المستخدمون", callback_data="a:users")],
            [B("📢 التواصل", callback_data="a:comm")],
            [B("🆘 الإبلاغ عن مشكلة", callback_data="a:report"), B("🛠 النظام والدعم", callback_data="a:sys")],
            [B(f"🚫 إشعار الحظر {tick(s.on('ban_notice'))}", callback_data="a:bannotice"),
             B(f"🔔 إشعار الدخول {tick(s.on('notify_join'))}", callback_data="a:joinnotice")],
        ]
        return "🎛 لوحة الأدمن", M(rows)
    if name == "content":
        return f"📝 المحتوى\n\nرسالة الترحيب الحالية:\n{s.get('welcome')}", M([
            [B("✏️ تعديل رسالة الترحيب", callback_data="w:welcome")], back()])
    if name == "settings":
        return "⚙️ الإعدادات", M([
            [B(f"🛠 وضع الصيانة {tick(s.on('maintenance'))}", callback_data="t:maintenance:settings")],
            [B(f"🔐 الاشتراك الإجباري {tick(s.on('gate_on'))}", callback_data="t:gate_on:settings")],
            [B(f"⏱ مدة الحذف: {s.get('delay')} ثانية", callback_data="w:delay")],
            [B(f"🔒 حماية المحتوى (منع التوجيه/الحفظ) {tick(s.on('protect'))}", callback_data="t:protect:settings")],
            [B("✏️ رسالة التنبيه", callback_data="w:note")], back()])
    if name == "users":
        return "👥 المستخدمون\n\nإدارة المستخدمين والأدمنية", M([
            [B("📊 الإحصائيات", callback_data="a:stats"), B("👤 المسؤولون", callback_data="a:admins")],
            [B("🚫 إدارة الحظر", callback_data="a:bans")],
            [B("📋 سجل النشاط", callback_data="a:log")],
            [B("📩 دعوة أشخاص إلى البوت", callback_data="a:invite")], back()])
    if name == "links":
        ls = s.links()
        rows = [[B("➕ إنشاء رابط جديد", callback_data="n:new")]]
        for c, n, v in ls:
            rows.append([B(f"🔗 {c} ({n} ملف، {v} مشاهدة)", callback_data=f"n:show:{c}"), B("🗑", callback_data=f"n:del:{c}")])
        rows.append(back())
        return f"🔗 الروابط ({len(ls)})\n\nكل رابط يرسل ملفاتك ثم يحذفها بعد {s.get('delay')} ثانية.", M(rows)
    if name == "stats":
        total, day, active, banned = s.stats()
        return (f"📊 الإحصائيات\n\nكل المستخدمين: {total}\nجدد آخر 24 ساعة: {day}\nنشطين آخر 24 ساعة: {active}\nالمحظورين: {banned}",
                M([back("users")]))
    if name == "admins":
        ids = sorted(ADMIN_IDS | set(s.admins()))
        txt = "👤 المسؤولون\n\n" + ("\n".join(f"• {i}" for i in ids) or "لا يوجد")
        return txt, M([[B("➕ إضافة مسؤول (ID)", callback_data="w:admin_add"), B("➖ حذف مسؤول", callback_data="w:admin_del")], back("users")])
    if name == "bans":
        rows = s.banned_list()
        txt = "🚫 إدارة الحظر\n\n" + ("\n".join(f"• {i} {n}" for i, n, _u in rows) or "ما كو محظورين")
        return txt, M([[B("🚫 حظر (ID)", callback_data="w:ban"), B("✅ فك حظر (ID)", callback_data="w:unban")], back("users")])
    if name == "log":
        rows = s.recent_log(20)
        txt = "📋 سجل النشاط (آخر 20)\n\n" + ("\n".join(f"{fmt_ts(t)} {k} {u} {x}" for t, k, u, x in rows) or "فارغ")
        return txt, M([back("users")])
    if name == "invite":
        link = f"https://t.me/{ctx.bot.username}?start=ref{ctx.user_data.get('me', 0)}"
        return (f"📩 دعوة أشخاص\n\nرابطك:\n{link}\n\nانضم عن طريقه: {s.invited_count(ctx.user_data.get('me', 0))}",
                M([back("users")]))
    if name == "sub":
        chans = s.channels()
        rows = [[B(f"{tick(e)} 📢 {r}", callback_data=f"c:t:{i}"), B("🗑", callback_data=f"c:d:{i}")] for i, r, e in chans]
        rows += [
            [B(f"➕ إضافة اشتراك جديد ({len(chans)}/{MAX_CHANNELS})", callback_data="w:channel_add")],
            [B("❌ تعطيل الكل", callback_data="c:all:0"), B("✅ تفعيل الكل", callback_data="c:all:1")],
            [B("👁 معاينة البوابة", callback_data="c:preview")],
            [B(f"الإشعار {tick(s.on('gate_notify'))}", callback_data="t:gate_notify:sub")],
            [B(f"زر التحقق {tick(s.on('gate_button'))}", callback_data="t:gate_button:sub")],
            [B("✏️ رسالة البوابة", callback_data="w:gate_msg")],
            [B("❓ شرح القسم", callback_data="a:subhelp"), back()[0]],
        ]
        return f"🔐 الاشتراك الإجباري\n\n📋 الاشتراكات: {len(chans)}/{MAX_CHANNELS}", M(rows)
    if name == "subhelp":
        return ("❓ الاشتراك الإجباري\n\nأي مستخدم جديد لازم يشترك بالقنوات المفعّلة قبل ما يستخدم البوت. "
                "لازم البوت يكون أدمن بكل قناة حتى يتأكد من الاشتراك. القناة تنضاف بيوزرها مثل @name. "
                "إذا ما أكدر أتحقق من قناة، ما أمنع المستخدمين (حتى لا ينقفل البوت) وأرسل لك تنبيه.", M([back("sub")]))
    if name == "comm":
        return "📢 التواصل\n\nإرسال رسالة لكل المستخدمين.", M([[B("📣 رسالة جماعية", callback_data="w:broadcast")], back()])
    if name == "sys":
        up = int(time.time() - START)
        return (f"🛠 النظام والدعم\n\nمدة التشغيل: {up//3600}س {(up%3600)//60}د\n"
                f"💾 البيانات تنحفظ تلقائياً كملف مثبّت (backup.db) بمحادثتك مع البوت. لا تحذفه.\n\nنص الدعم:\n{s.get('support')}",
                M([[B("✏️ تعديل نص الدعم", callback_data="w:support")], back()]))
    if name == "report":
        return f"🆘 الإبلاغ عن مشكلة\n\nرابط حالي: {s.get('report_url') or 'غير محدد'}\nيظهر للمستخدم عند أمر /report", M([
            [B("✏️ تعديل الرابط", callback_data="w:report_url")], back()])
    if name == "bannotice":
        return f"🚫 إشعار الحظر {tick(s.on('ban_notice'))}\n\nالرسالة:\n{s.get('ban_msg')}", M([
            [B("تشغيل/إيقاف", callback_data="t:ban_notice:bannotice"), B("✏️ تعديل", callback_data="w:ban_msg")], back()])
    if name == "joinnotice":
        return f"🔔 إشعار الدخول {tick(s.on('notify_join'))}\n\nيوصلك إشعار عند دخول مستخدم جديد.", M([
            [B("تشغيل/إيقاف", callback_data="t:notify_join:joinnotice")], back()])
    return "؟", M([back()])


async def edit(q, text, markup):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except TelegramError:
        pass  # "message is not modified"


async def admin_cmd(update: Update, ctx):
    if not is_admin(update.effective_user):
        return
    ctx.user_data["me"] = update.effective_user.id
    t, m = screen("main", ctx)
    await update.message.reply_text(t, reply_markup=m)


PROMPTS = {
    "welcome": "أرسل رسالة الترحيب الجديدة:", "gate_msg": "أرسل نص رسالة البوابة:", "support": "أرسل نص الدعم:",
    "report_url": "أرسل الرابط (أو - لإزالته):", "ban_msg": "أرسل رسالة الحظر:",
    "admin_add": "أرسل ID المسؤول الجديد (رقم):", "admin_del": "أرسل ID المسؤول للحذف:",
    "ban": "أرسل ID المستخدم للحظر:", "unban": "أرسل ID المستخدم لفك الحظر:",
    "channel_add": "أرسل يوزر القناة مثل @name (وأضف البوت أدمن فيها):",
    "broadcast": "أرسل نص الرسالة الجماعية:",
    "delay": "أرسل مدة الحذف بالثواني (بين 5 و3600):",
    "note": "أرسل نص رسالة التنبيه. استخدم {s} مكان عدد الثواني:",
}


async def admin_buttons(update: Update, ctx):
    q = update.callback_query
    user = update.effective_user
    if not is_admin(user):
        await q.answer("غير مسموح", show_alert=True)
        return
    ctx.user_data["me"] = user.id
    parts = q.data.split(":")
    k = parts[0]
    await q.answer()
    if k == "a":
        t, m = screen(parts[1], ctx)
        await edit(q, t, m)
    elif k == "t":  # toggle setting then show screen
        store.toggle(parts[1])
        t, m = screen(parts[2], ctx)
        await edit(q, t, m)
    elif k == "w":  # wait for text input
        ctx.user_data["await"] = parts[1]
        await q.message.reply_text(PROMPTS[parts[1]] + "\n(/cancel للإلغاء)")
    elif k == "c":
        if parts[1] == "t":
            store.toggle_channel(int(parts[2]))
        elif parts[1] == "d":
            store.del_channel(int(parts[2]))
        elif parts[1] == "all":
            store.set_all_channels(parts[2] == "1")
        elif parts[1] == "preview":
            await q.message.reply_text(store.get("gate_msg"), reply_markup=gate_markup([r for _i, r, e in store.channels() if e]))
            return
        t, m = screen("sub", ctx)
        await edit(q, t, m)
    elif k == "n":
        await link_buttons(update, ctx, parts)
    elif k == "b":  # confirm broadcast
        if parts[1] == "yes":
            text = ctx.user_data.pop("bc", None)
            if not text:
                return
            await q.message.reply_text("⏳ جاري الإرسال...")
            ok = bad = 0
            for uid in store.all_user_ids():
                try:
                    await ctx.bot.send_message(uid, text)
                    ok += 1
                except TelegramError:
                    bad += 1
                await asyncio.sleep(0.05)
            store.log("broadcast", user.id, f"{ok} ok {bad} failed")
            await q.message.reply_text(f"تم. وصلت: {ok}، فشلت: {bad}")
        else:
            ctx.user_data.pop("bc", None)
            await q.message.reply_text("أُلغيت.")


async def cancel(update: Update, ctx):
    ctx.user_data.pop("await", None)
    ctx.user_data.pop("effect", None)
    await update.message.reply_text("تم الإلغاء.")


async def admin_input(update: Update, ctx) -> bool:
    key = ctx.user_data.get("await")
    if not key or not is_admin(update.effective_user):
        return False
    ctx.user_data.pop("await")
    text = update.message.text.strip()
    s = store
    uid = update.effective_user.id
    if key == "delay":
        if text.isdigit() and 5 <= int(text) <= 3600:
            s.set("delay", text)
            await update.message.reply_text("✔️ تم الحفظ.")
        else:
            await update.message.reply_text("رقم بين 5 و3600.")
        return True
    if key in ("welcome", "gate_msg", "support", "ban_msg", "note"):
        s.set(key, text)
        await update.message.reply_text("✔️ تم الحفظ.")
    elif key == "report_url":
        s.set(key, "" if text == "-" else text)
        await update.message.reply_text("✔️ تم الحفظ.")
    elif key in ("admin_add", "admin_del", "ban", "unban"):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("لازم رقم ID.")
            return True
        n = int(text)
        if key == "admin_add":
            s.add_admin(n)
        elif key == "admin_del":
            if n in ADMIN_IDS:
                await update.message.reply_text("هذا أدمن أساسي من الإعدادات، ما ينحذف من هنا.")
                return True
            s.del_admin(n)
        elif key == "ban":
            if n in ADMIN_IDS or n in s.admins():
                await update.message.reply_text("ما تكدر تحظر أدمن.")
                return True
            s.set_ban(n, True)
        else:
            s.set_ban(n, False)
        s.log(key, uid, str(n))
        await update.message.reply_text("✔️ تم.")
    elif key == "channel_add":
        ref = text if text.startswith("@") else "@" + text
        if len(s.channels()) >= MAX_CHANNELS:
            await update.message.reply_text("وصلت الحد الأعلى.")
            return True
        s.add_channel(ref)
        try:
            me = await ctx.bot.get_chat_member(ref, ctx.bot.id)
            note = "✔️ انضافت، والبوت أدمن بها." if me.status in ("administrator", "creator") else "⚠️ انضافت، بس البوت مو أدمن بالقناة. أضفه أدمن وإلا ما أكدر أتحقق."
        except TelegramError as e:
            note = f"⚠️ انضافت، بس ما كدرت أوصل للقناة ({e}). تأكد من اليوزر وأضف البوت أدمن."
        await update.message.reply_text(note)
    elif key == "broadcast":
        ctx.user_data["bc"] = text
        await update.message.reply_text(f"راح ترسل لـ {len(s.all_user_ids())} مستخدم:\n\n{text}",
                                         reply_markup=M([[B("✅ إرسال", callback_data="b:yes"), B("❌ إلغاء", callback_data="b:no")]]))
    return True


async def text_router(update: Update, ctx):
    await admin_input(update, ctx)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern=r"^[atwcbn]:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL, collect_media))
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 10000)), url_path=TOKEN,
                        webhook_url=f"{url}/{TOKEN}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
