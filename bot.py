import os, time, asyncio, logging, datetime
import json, secrets, tempfile, io
from urllib.parse import quote
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, MessageEntity
import httpx
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
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
AUD_LABELS = {"new7": "جدد - 7 أيام", "new30": "جدد - 30 يوم", "off7": "غير نشطين +7", "off30": "غير نشطين +30"}

def aud_text(aud):
    segs = [x for x in (aud or "").split(",") if x in AUD_LABELS]
    return "الجميع" if not segs else "، ".join(AUD_LABELS[x] for x in segs)
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
store.freeze_links()
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
    tz = datetime.timezone(datetime.timedelta(hours=3))  # Baghdad
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")


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
    src = None
    if msg and msg.text and msg.text.startswith("/start ") and not msg.text.startswith("/start ref"):
        c0 = msg.text.split(" ", 1)[1].strip().partition("_")[0]
        if c0 and store.get_link(c0):
            src = c0
    new = store.touch_user(user.id, user.full_name, user.username or "", inviter if inviter != user.id else None, src)
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
    if ctx.args and not ctx.args[0].startswith("ref"):
        code, _, inv = ctx.args[0].partition("_")
        if store.get_link(code):
            await open_link(update, ctx, code, int(inv) if inv.isdigit() else None)
            return
    if is_admin(update.effective_user):  # admin sees the panel on /start
        ctx.user_data["me"] = update.effective_user.id
        t, m = screen("main", ctx)
        await update.message.reply_text(t, reply_markup=m)
    await update.message.reply_text(store.get("welcome"))


async def report(update: Update, ctx):
    url, sup = store.get("report_url"), store.get("support")
    await update.message.reply_text(sup + (f"\n{url}" if url else ""))


# ---------------- links / delivery ----------------
def parse_duration(text):
    """'30', '30s', '2m', '90 ثانية', '5 دقيقة' -> seconds, or None."""
    import re
    m = re.fullmatch(r"\s*(\d+)\s*([A-Za-z\u0600-\u06FF]*)\s*", text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit in ("", "s", "sec", "ث", "ثانية", "ثواني", "ثانيه"):
        sec = n
    elif unit in ("m", "min", "د", "دقيقة", "دقائق", "دقيقه", "دقايق"):
        sec = n * 60
    else:
        return None
    return sec if 5 <= sec <= 86400 else None


def fmt_dur(sec):
    sec = int(sec)
    return f"{sec // 60} دقيقة" if sec % 60 == 0 and sec >= 60 else f"{sec} ثانية"

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


def fmt_clock(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s_ = divmod(r, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def blur_bytes(raw, strength, duration=None):
    from PIL import Image, ImageFilter, ImageDraw, ImageFont
    if raw is None:
        im = Image.new("RGB", (640, 360), (40, 40, 40))
    else:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((1280, 1280))
        radius = max(1.0, max(im.size) * 0.002 * strength)
        im = im.filter(ImageFilter.GaussianBlur(radius))
    if duration is not None:
        d = ImageDraw.Draw(im, "RGBA")
        w, h = im.size
        size = max(28, min(w, h) // 6)
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
        txt = fmt_clock(duration)
        box = d.textbbox((0, 0), txt, font=font)
        tw, th_ = box[2] - box[0], box[3] - box[1]
        x, y = (w - tw) // 2, (h - th_) // 2
        pad = size // 3
        d.rounded_rectangle((x - pad - size, y - pad, x + tw + pad, y + th_ + pad * 2), radius=pad, fill=(0, 0, 0, 150))
        tx, ty = x - size - pad // 2, y + th_ // 2 + 2
        r = size // 2
        d.polygon([(tx + r // 2, ty - r), (tx + r // 2, ty + r), (tx + r // 2 + int(r * 1.5), ty)], fill=(255, 255, 255, 230))
        d.text((x - box[0], y - box[1]), txt, font=font, fill=(255, 255, 255, 255))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    out.seek(0)
    return out


async def make_blurs(bot, items, strength=None):
    """Adds item['b'] (file_id of a blurred preview, or None). Uses the admin chat to obtain a file_id."""
    strength = int(strength or store.get("blur") or 3)
    admin = min(ADMIN_IDS)
    for it in items:
        if "b" in it and it.get("bs") == f"{strength}v2":
            continue
        src = it.get("th") or (it["f"] if it["t"] == "photo" else None)
        it["b"] = None
        it["bs"] = f"{strength}v2"
        isvid = it["t"] in ("video", "animation")
        if not src and not isvid:
            continue
        try:
            raw = None
            if src:
                f = await bot.get_file(src)
                raw = bytes(await f.download_as_bytearray())
            dur = it.get("dur") if isvid and it.get("dur") is not None else None
            if raw is None and dur is None:
                continue
            bio = await asyncio.to_thread(blur_bytes, raw, strength, dur)
            m = await bot.send_photo(admin, bio, disable_notification=True)
            it["b"] = m.photo[-1].file_id
            try:
                await bot.delete_message(admin, m.message_id)
            except TelegramError:
                pass
        except Exception as e:
            logging.warning("blur failed: %s", e)
    return items


async def deliver_to(bot, chat_id, uid, code):
    items, _v = store.get_link(code)
    meta = store.link_meta(code)
    delay = int(meta["delay"] or 30)
    note_txt = meta["note"] or ""
    protect = meta["protect"]
    ids = []
    for it in items:
        fn = {"photo": bot.send_photo, "video": bot.send_video,
              "animation": bot.send_animation, "document": bot.send_document}[it["t"]]
        try:
            m = await fn(chat_id, it["f"], protect_content=protect)
            ids.append(m.message_id)
        except TelegramError as e:
            logging.warning("send failed: %s", e)
    n = await bot.send_message(chat_id, note_txt.replace("{s}", fmt_dur(delay)).replace("{t}", fmt_dur(delay)), protect_content=protect)
    ids.append(n.message_id)
    at = time.time() + delay
    store.add_pending(chat_id, ids, at)
    store.inc_views(code)
    store.log("view", uid, code)
    asyncio.create_task(delete_later(bot, chat_id, ids, at))


async def send_locked(bot, chat_id, uid, code, need):
    items, _v = store.get_link(code)
    strength = int(store.link_meta(code)["blur"] or 3)
    if any(it.get("bs") != f"{strength}v2" for it in items):
        items = await make_blurs(bot, items, strength)
        store.set_items(code, items)
    blurred = [it["b"] for it in items if it.get("b")][:10]
    try:
        if len(blurred) == 1:
            await bot.send_photo(chat_id, blurred[0], protect_content=True)
        elif blurred:
            await bot.send_media_group(chat_id, [InputMediaPhoto(f) for f in blurred], protect_content=True)
    except TelegramError as e:
        logging.warning("blur send failed: %s", e)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={code}_{uid}"
    have = store.invite_count(code, uid)
    text = (store.link_meta(code)["lock_msg"] or "").replace("{n}", str(need)).replace("{have}", str(have)).replace("{link}", link)
    share = "https://t.me/share/url?url=" + quote(link, safe="")
    await bot.send_message(chat_id, text, reply_markup=M([
        [B("📤 مشاركة رابطي", url=share)],
        [B("🔄 تحقق من عدد الدعوات", callback_data=f"u:chk:{code}")]]))


async def open_link(update: Update, ctx, code, inviter=None):
    user = update.effective_user
    chat_id = update.effective_chat.id
    need = store.link_meta(code)["need"]
    if not is_admin(user):
        locked_now = need > 0 and store.invite_count(code, user.id) < need
        store.add_visit(code, user.id, inviter if inviter and inviter != user.id else None, locked_now,
                        store.user_src(user.id) == code and store.q("SELECT COUNT(*) FROM link_visits WHERE code=? AND uid=?", (code, user.id), one=True)[0] == 0)
    if inviter and inviter != user.id and need > 0:
        if store.add_invite(code, inviter, user.id):
            have = store.invite_count(code, inviter)
            try:
                if have == need:
                    await ctx.bot.send_message(inviter, f"🎉 اكتمل العدد ({have}/{need}). هذا المحتوى المفتوح:")
                    await deliver_to(ctx.bot, inviter, inviter, code)
                elif have < need:
                    await ctx.bot.send_message(inviter, f"👤 دخل شخص من رابطك ({have}/{need}).")
            except TelegramError as e:
                logging.warning("inviter notify failed: %s", e)
    if need > 0 and not is_admin(user) and store.invite_count(code, user.id) < need:
        await send_locked(ctx.bot, chat_id, user.id, code, need)
        return
    await deliver_to(ctx.bot, chat_id, user.id, code)


async def user_cb(update: Update, ctx):
    q = update.callback_query
    _, act, code = q.data.split(":", 2)
    meta = store.link_meta(code)
    if not meta:
        await q.answer("الرابط غير متاح.", show_alert=True)
        return
    uid = q.from_user.id
    have = store.invite_count(code, uid)
    if meta["need"] > 0 and have < meta["need"] and not is_admin(q.from_user):
        await q.answer(f"دعوت {have} من {meta['need']}. لسه ناقص {meta['need'] - have}.", show_alert=True)
        return
    await q.answer("✅ انفتح")
    await deliver_to(ctx.bot, q.message.chat_id, uid, code)


import re as _re


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def build_caption(text, ents, url):
    """#word# in the text becomes a clickable link to url (the # marks removed). No marks: text is posted exactly as written.
    Other entities (bold, premium emoji...) are kept and shifted."""
    pairs = list(_re.finditer(r"(?<!\w)#([^#]*?\S[^#]*?)#(?!\w)", text))
    # auto-detected types (a "#word#" arrives as a hashtag entity) conflict with a text_link: drop them
    out = [dict(e) for e in (ents or []) if e.get("type") not in ("hashtag", "cashtag", "mention", "url", "email", "phone_number", "bot_command")]
    if not pairs:
        return text, out
    removed = []
    for m in pairs:
        removed += [_u16(text[:m.start()]), _u16(text[:m.end() - 1])]
    new_text = _re.sub(r"(?<!\w)#([^#]*?\S[^#]*?)#(?!\w)", lambda m: m.group(1), text)

    def new_off(o):
        return o - sum(1 for r in removed if r < o)

    res = []
    for e in out:
        s = new_off(e["offset"])
        en = new_off(e["offset"] + e["length"])
        if en > s:
            e["offset"], e["length"] = s, en - s
            res.append(e)
    for m in pairs:
        g = m.group(1)
        lead = len(g) - len(g.lstrip())
        s = new_off(_u16(text[:m.start()]) + 1 + _u16(g[:lead]))
        res.append({"type": "text_link", "offset": s, "length": _u16(g.strip()), "url": url})
    return new_text, res


def _pub_media(bot, pub, cap=None, ents=None):
    out = []
    for i, (t, f) in enumerate(pub["media"]):
        kw = {}
        if i == 0 and cap:
            kw = {"caption": cap, "caption_entities": [MessageEntity.de_json(d, bot) for d in (ents or [])] or None}
        out.append((InputMediaPhoto if t == "photo" else InputMediaVideo)(f, **kw))
    return out


async def pub_buttons(update: Update, ctx, parts):
    q = update.callback_query
    act = parts[1]
    if act == "pchan":
        ctx.user_data["await"] = "pchan"
        await q.message.reply_text("أضف البوت أدمن بقناتك (بصلاحية نشر الرسائل)، وبعدها أرسل هنا @يوزر القناة، أو حوّل رسالة منها.\n(/cancel للإلغاء)")
        return
    if act == "pcapset":
        ctx.user_data["await"] = "pcapset"
        cur = store.get("pub_cap")
        await q.message.reply_text("أرسل وصف النشر الثابت (يُستخدم بكل رابط). النص الي تريده يصير رابط اكتبه بين علامتين #، مثل:\nشاهد المحتوى كامل #اضغط هنا#\nوتظهر \"اضغط هنا\" رابط يفتح البوت بالرابط. بدون # ينشر وصفك مثل ما كتبته بدون رابط.\n"
                                   + (f"\nالحالي:\n{cur}\n" if cur else "") + "(/cancel للإلغاء)")
        return
    if act == "pubcancel":
        ctx.user_data.pop("pub", None)
        ctx.user_data.pop("await", None)
        await q.message.reply_text("أُلغي النشر.")
        return
    if act == "pub":
        code = parts[2]
        if not store.link_meta(code):
            return
        ctx.user_data["pub"] = {"code": code, "media": [], "grp": None}
        if not store.get("post_chan"):
            ctx.user_data["pub"]["need_chan"] = True
            ctx.user_data["await"] = "pchan"
            await q.message.reply_text("أول مرة: أضف البوت أدمن بقناتك (بصلاحية نشر الرسائل)، وبعدها أرسل هنا @يوزر القناة، أو حوّل رسالة منها.\n(/cancel للإلغاء)")
            return
        await _pub_ask_media(q.message)
        return
    pub = ctx.user_data.get("pub")
    if not pub:
        await q.message.reply_text("انتهت الجلسة، ابدأ من جديد من شاشة الرابط.")
        return
    if act == "pubdone":
        if not pub["media"]:
            await q.message.reply_text("أرسل صورة وحدة على الأقل أول.")
            return
        if store.get("pub_cap"):
            try:
                te = json.loads(store.get("pub_ents") or "[]")
            except Exception:
                te = []
            await pub_preview(update, ctx, store.get("pub_cap"), te)
            return
        await _ask_cap(q.message, ctx)
        return
    if act == "pubcap2":
        pub["cap"] = None
        await _ask_cap(q.message, ctx)
        return
    if act in ("pubgo", "pubgoplain"):
        if not pub.get("cap"):
            return
        ref = store.get("post_chan")
        ents_all = pub["ents"]
        has_ce = any(d.get("type") == "custom_emoji" for d in ents_all)
        use = [d for d in ents_all if d.get("type") != "custom_emoji"] if act == "pubgoplain" else ents_all
        try:
            chat = int(ref) if ref.lstrip("-").isdigit() else ref
            await ctx.bot.send_media_group(chat, _pub_media(ctx.bot, pub, pub["cap"], use))
            store.log("channel_post", update.effective_user.id, pub["code"])
            ctx.user_data.pop("pub", None)
            await q.message.reply_text("✅ اننشر بالقناة." + (" (الإيموجي المتحرك نزلت إيموجي عادي)" if act == "pubgoplain" and has_ce else ""))
        except Exception as e:
            if has_ce and act == "pubgo":
                await q.message.reply_text(
                    "تلكرام رفض النشر بالقناة بالإيموجي المتحرك: " + str(e)[:120] + "\nغالباً بوتات القنوات ما تدعم الإيموجي المتحرك، أو حساب مالك البوت بدون Premium. تنشره بإيموجي عادي بدلها؟",
                    reply_markup=M([[B("نشر بإيموجي عادي", callback_data="n:pubgoplain"), B("❌ إلغاء", callback_data="n:pubcancel")]]))
            else:
                await q.message.reply_text("فشل النشر: " + str(e)[:150])
        return


async def _ask_cap(msg, ctx):
    ctx.user_data["await"] = "pcap"
    await msg.reply_text(
        "أرسل الوصف. النص الي تريده يصير رابط اكتبه بين علامتين #، مثل:\nشاهد المحتوى كامل #اضغط هنا#\nوتظهر \"اضغط هنا\" رابط يفتح البوت بالرابط الي أنشأته. "
        "بدون # ينشر وصفك مثل ما كتبته بدون أي رابط.\nأول وصف ترسله ينحفظ ثابت لكل الروابط.\n(/cancel للإلغاء)")


async def _pub_ask_media(msg):
    await msg.reply_text(
        "أرسل الصور (أو فيديو) الي تريدها بالمنشور، 2 أو أكثر، حتى 10. تكدر ترسلها ألبوم أو وحدة وحدة. بعدها اضغط ✅ تم.",
        reply_markup=M([[B("✅ تم", callback_data="n:pubdone"), B("❌ إلغاء", callback_data="n:pubcancel")]]))


async def pub_preview(update, ctx, text, ents):
    pub = ctx.user_data.get("pub")
    m = update.effective_message
    url = f"https://t.me/{ctx.bot.username}?start={pub['code']}"
    cap, ents2 = build_caption(text, ents, url)
    if _u16(cap) > 1024:
        ctx.user_data["await"] = "pcap"
        await m.reply_text(f"الوصف طويل ({_u16(cap)}). الحد 1024 حرف، أرسل وصفاً أقصر لهذه المرة.")
        return
    pub["cap"], pub["ents"] = cap, ents2
    has_ce = any(e.get("type") == "custom_emoji" for e in ents2)
    pub["plain"] = False
    note = ""
    try:
        await ctx.bot.send_media_group(update.effective_chat.id, _pub_media(ctx.bot, pub, cap, ents2))
        if has_ce:
            note = "\n✅ الإيموجي المتحرك قبلها تلكرام بالمعاينة."
    except Exception as e:
        if not has_ce:
            ctx.user_data["await"] = "pcap"
            await m.reply_text("فشلت المعاينة: " + str(e)[:150])
            return
        plain = [d for d in ents2 if d.get("type") != "custom_emoji"]
        try:
            await ctx.bot.send_media_group(update.effective_chat.id, _pub_media(ctx.bot, pub, cap, plain))
        except Exception as e2:
            ctx.user_data["await"] = "pcap"
            await m.reply_text("فشلت المعاينة: " + str(e2)[:150])
            return
        note = "\n⚠️ تلكرام رفض الإيموجي المتحرك (لازم حساب مالك البوت يكون عنده Premium)، والمعاينة فوق بإيموجي عادي."
    warn = note + ("" if ents2 and any(e.get("type") == "text_link" and e.get("url") == url for e in ents2) else "\n⚠️ ما كو نص بين علامتين # فما كو رابط بالمنشور.")
    await m.reply_text(f"هذي المعاينة. تنشر بقناة {store.get('post_chan_name') or ''}؟{warn}",
                       reply_markup=M([[B("✅ نشر", callback_data="n:pubgo"), B("✏️ وصف آخر لهذه المرة", callback_data="n:pubcap2")],
                                       [B("❌ إلغاء", callback_data="n:pubcancel")]]))


async def pub_input(update: Update, ctx, key):
    m = update.message
    pub = ctx.user_data.get("pub")
    if key == "pchan":
        ref = None
        fo = getattr(m, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            ref = fo.chat.id
        elif m.text:
            t = m.text.strip()
            ref = t if t.startswith("@") else ("@" + t if not t.lstrip("-").isdigit() else int(t))
        if ref is None:
            ctx.user_data["await"] = "pchan"
            await m.reply_text("أرسل @يوزر القناة أو حوّل رسالة منها.")
            return True
        try:
            ch = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(ch.id, ctx.bot.id)
            ok = me.status in ("administrator", "creator") and (getattr(me, "can_post_messages", True) is not False)
        except Exception as e:
            ctx.user_data["await"] = "pchan"
            await m.reply_text("ما كدرت أوصل للقناة: " + str(e)[:100] + "\nتأكد البوت أدمن فيها وجرب مرة ثانية.")
            return True
        if not ok:
            ctx.user_data["await"] = "pchan"
            await m.reply_text("البوت مو أدمن بالقناة أو ما عنده صلاحية نشر الرسائل. عدّل صلاحياته وأرسل مرة ثانية.")
            return True
        store.set("post_chan", str(ch.id))
        store.set("post_chan_name", ch.title or str(ch.id))
        await m.reply_text(f"✅ قناة النشر: {ch.title}")
        if pub and pub.pop("need_chan", None):
            await _pub_ask_media(m)
        return True
    if key == "pcapset":
        text = m.text or ""
        if _u16(text) > 1000:
            ctx.user_data["await"] = "pcapset"
            await m.reply_text("الوصف طويل، الحد 1000 حرف. قصّره وأرسله.")
            return True
        store.set("pub_cap", text)
        store.set("pub_ents", json.dumps([e.to_dict() for e in (m.entities or [])]))
        await m.reply_text("✅ انحفظ الوصف الثابت. يُستخدم بكل رابط عند النشر (النص بين علامتين # يصير رابط الرابط تلقائياً).")
        return True
    if key == "pcap" and pub:
        text = m.text or ""
        ents = [e.to_dict() for e in (m.entities or [])]
        if not store.get("pub_cap"):
            store.set("pub_cap", text)
            store.set("pub_ents", json.dumps(ents))
            await m.reply_text("💾 حفظته كوصف ثابت للنشر بكل الروابط (تغيره من ⚙️ الإعدادات > وصف النشر).")
        await pub_preview(update, ctx, text, ents)
        return True
    return False


async def link_buttons(update: Update, ctx, parts):
    if parts[1] in ("pub", "pubdone", "pubgo", "pubcancel", "pchan", "pubcap2", "pcapset", "pubgoplain"):
        await pub_buttons(update, ctx, parts)
        return
    q = update.callback_query
    act = parts[1]
    if act == "new":
        ctx.user_data["collect"] = []
        ctx.user_data.pop("cmsg", None)
        await q.message.reply_text("أرسل الصور والفيديوهات اللي تريدها بهذا الرابط (واحد واحد أو مجموعة). بعدها اضغط إنهاء.",
                                   reply_markup=M([[B("✅ إنهاء وإنشاء الرابط", callback_data="n:done")],
                                                   [B("❌ إلغاء", callback_data="n:cancel")]]))
    elif act == "done":
        items = ctx.user_data.pop("collect", None)
        ctx.user_data.pop("cmsg", None)
        if not items:
            await q.message.reply_text("ما أضفت أي ملف. ابدأ من جديد من 🔗 الروابط.")
            return
        code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
        await make_blurs(ctx.bot, items)
        store.add_link(code, items, "")
        store.log("link_new", update.effective_user.id, code)
        ctx.user_data["await"] = "lname:" + code
        ctx.user_data["newlink"] = code
        await q.message.reply_text(f"✅ تم إنشاء الرابط ({len(items)} ملف):\n{link_url(ctx, code)}\n\nأرسل اسم للرابط (مثلاً: فيديوهات الجمعة) عشان تعرفه بعدين، أو اضغط تخطي ويسميه رقم تلقائي.",
                                   reply_markup=M([[B("⏭ تخطي (ترقيم تلقائي)", callback_data=f"n:skip:{code}")]]))
    elif act == "cancel":
        ctx.user_data.pop("collect", None)
        ctx.user_data.pop("cmsg", None)
        await q.message.reply_text("أُلغيت.")
    elif act == "skip":
        code = parts[2]
        if not store.link_meta(code):
            return
        if ctx.user_data.get("newlink") == code:
            ctx.user_data.pop("newlink", None)
            store.update_link(code, "name", f"رابط {store.link_seq(code) or code}")
            ctx.user_data["await"] = "lneed:" + code
            await q.message.reply_text(f"✔️ سميته «رابط {store.link_seq(code) or code}».\n\nكم شخص لازم يدعو المستخدم حتى يفتح الصور والفيديوهات؟ أرسل رقم (0 = بدون قفل، تنفتح مباشرة):")
        else:
            await q.message.reply_text("هذا الرابط مسمّى من قبل.")
    elif act == "stats":
        t, m = screen("lstats:" + parts[2], ctx)
        await edit(q, t, m)
    elif act == "vis":
        t, m = screen(f"lvis:{parts[2]}:{parts[3]}", ctx)
        await edit(q, t, m)
    elif act == "page":
        ctx.user_data["lpage"] = int(parts[2])
        t, m = screen("links", ctx)
        await edit(q, t, m)
    elif act == "search":
        ctx.user_data["await"] = "lsearch"
        await q.message.reply_text("اكتب جزء من اسم الرابط للبحث:\n(/cancel للإلغاء)")
    elif act == "clearq":
        ctx.user_data.pop("lq", None)
        ctx.user_data["lpage"] = 0
        t, m = screen("links", ctx)
        await edit(q, t, m)
    elif act == "show":
        t, m = screen("link:" + parts[2], ctx)
        await edit(q, t, m)
    elif act == "prot":
        store.update_link(parts[2], "protect", 0 if store.link_meta(parts[2])["protect"] else 1)
        t, m = screen("link:" + parts[2], ctx)
        await edit(q, t, m)
    elif act == "lock":
        ctx.user_data["await"] = "llock:" + parts[2]
        await q.message.reply_text("أرسل رسالة القفل لهذا الرابط فقط. استخدم {n} للعدد المطلوب، {have} لما دعاه الشخص، {link} لرابطه الشخصي:\n(/cancel للإلغاء)")
    elif act == "blur":
        ctx.user_data["await"] = "lblur:" + parts[2]
        await q.message.reply_text("أرسل قوة التغبيش لهذا الرابط من 1 (خفيف) إلى 10 (قوي).\n(/cancel للإلغاء)")
    elif act == "need":
        ctx.user_data["await"] = "lneed:" + parts[2]
        await q.message.reply_text("كم شخص لازم يدعو المستخدم حتى يفتح المحتوى؟ أرسل رقم (0 = بدون قفل):\n(/cancel للإلغاء)")
    elif act in ("name", "time", "msg"):
        ctx.user_data["await"] = {"name": "lname:", "time": "ldelay:", "msg": "lnote:"}[act] + parts[2]
        prompt = {"name": "أرسل الاسم الجديد للرابط:",
                  "time": "أرسل مدة الحذف لهذا الرابط. مثال: 30 أو 45 ثانية أو 2 دقيقة (من 5 ثواني إلى 24 ساعة):",
                  "msg": "أرسل الرسالة اللي تظهر بعد المحتوى لهذا الرابط. استخدم {s} مكان مدة الحذف:"}[act]
        await q.message.reply_text(prompt + "\n(/cancel للإلغاء)")
    elif act == "reset":
        store.reset_link(parts[2])
        t, m = screen("link:" + parts[2], ctx)
        await edit(q, t, m)
    elif act == "del":
        store.del_link(parts[2])
        t, m = screen("links", ctx)
        await edit(q, t, m)


async def collect_media(update: Update, ctx):
    if not is_admin(update.effective_user):
        return
    pub = ctx.user_data.get("pub")
    if pub is not None and not ctx.user_data.get("await") and not pub.get("cap"):
        m = update.message
        if m.photo:
            pub["media"].append(("photo", m.photo[-1].file_id))
        elif m.video:
            pub["media"].append(("video", m.video.file_id))
        else:
            await m.reply_text("للمنشور أرسل صور أو فيديو فقط.")
            return
        del pub["media"][10:]
        if m.media_group_id is None or pub.get("grp") != m.media_group_id:
            pub["grp"] = m.media_group_id
            await m.reply_text(f"✔️ استلمت. العدد الحالي: {len(pub['media'])}. أرسل أكثر أو اضغط ✅ تم.",
                               reply_markup=M([[B("✅ تم", callback_data="n:pubdone"), B("❌ إلغاء", callback_data="n:pubcancel")]]))
        return
    if ctx.user_data.get("await") == "restore":
        ctx.user_data.pop("await", None)
        d = update.message.document
        if not d or d.file_size and d.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("أرسل ملف النسخة (.db) كمستند.")
            return
        tmp = tempfile.mktemp(suffix=".db")
        try:
            tf = await ctx.bot.get_file(d.file_id)
            await tf.download_to_drive(tmp)
        except Exception as e:
            await update.message.reply_text("ما كدرت أنزل الملف: " + str(e)[:100])
            return
        info = await asyncio.to_thread(store.check_backup, tmp)
        if not info:
            os.remove(tmp)
            await update.message.reply_text("هذا الملف مو نسخة صالحة لهذا البوت.")
            return
        ctx.user_data["restore_tmp"] = tmp
        await update.message.reply_text(
            f"ملف صالح: 🔗 {info[0]} رابط | 👥 {info[1]} مستخدم.\nالاسترجاع يبدل البيانات الحالية بهذي النسخة (وأرسل لك نسخة من الحالية قبلها). تكمل؟",
            reply_markup=M([[B("✅ استرجاع", callback_data="a:bkok"), B("❌ إلغاء", callback_data="a:bkno")]]))
        return
    if ctx.user_data.get("await") == "broadcast":
        ctx.user_data.pop("await", None)
        t0, f0 = media_of(update.message)
        await bc_preview(update, ctx, {"k": t0, "f": f0, "cap": update.message.caption or "",
                                       "ents": [e.to_dict() for e in (update.message.caption_entities or [])]})
        return
    col = ctx.user_data.get("collect")
    if col is None:
        if update.message.media_group_id is None or ctx.user_data.get("hint_grp") != update.message.media_group_id:
            ctx.user_data["hint_grp"] = update.message.media_group_id
            await update.message.reply_text("لإضافة هذا المحتوى لرابط: افتح /admin > 🔗 الروابط والمحتوى > إنشاء رابط جديد، وبعدها أرسله أو وجّهه هنا.")
        return
    t, f = media_of(update.message)
    m = update.message
    th = None
    obj = m.video or m.animation or m.document
    if obj is not None and getattr(obj, "thumbnail", None):
        th = obj.thumbnail.file_id
    item = {"t": t, "f": f}
    if m.video:
        item["dur"] = int(m.video.duration or 0)
    elif m.animation:
        item["dur"] = int(m.animation.duration or 0)
    if th:
        item["th"] = th
    col.append(item)
    await update_counter(ctx, update.effective_chat.id, col)


COUNTER_KB = None


async def update_counter(ctx, chat_id, col):
    """One counter message, edited in place as files are added (edits are spaced out to avoid Telegram flood limits)."""
    ud = ctx.user_data
    kb = M([[B("✅ إنهاء وإنشاء الرابط", callback_data="n:done")]])
    if not ud.get("cmsg"):
        sent = await ctx.bot.send_message(chat_id, f"➕ أضفت ({len(col)})", reply_markup=kb)
        ud["cmsg"] = sent.message_id
        ud["cshown"] = len(col)
        ud["clast"] = time.time()
        return
    if ud.get("cpend"):
        return

    async def push():
        wait = 0.8 - (time.time() - ud.get("clast", 0))
        if wait > 0:
            await asyncio.sleep(wait)
        ud["cpend"] = False
        n = len(ud.get("collect") or [])
        if not ud.get("cmsg") or n == ud.get("cshown"):
            return
        try:
            await ctx.bot.edit_message_text(f"➕ أضفت ({n})", chat_id=chat_id, message_id=ud["cmsg"], reply_markup=kb)
            ud["cshown"] = n
        except TelegramError as e:
            logging.warning("counter edit: %s", e)
        ud["clast"] = time.time()

    ud["cpend"] = True
    asyncio.create_task(push())


async def backup_loop(app):
    while True:
        await asyncio.sleep(20)
        if not store.dirty or not ADMIN_IDS:
            continue
        try:
            chat_id = min(ADMIN_IDS)
            tmp = tempfile.mktemp(suffix=".db")
            await asyncio.to_thread(store.snapshot, tmp)
            done = False
            if BACKUP["msg"]:
                try:
                    with open(tmp, "rb") as f:
                        await app.bot.edit_message_media(
                            InputMediaDocument(f, filename="backup.db", caption="💾 نسخة البيانات (لا تحذفها)"),
                            chat_id=chat_id, message_id=BACKUP["msg"])
                    done = True
                except TelegramError as e:
                    if "not modified" in str(e).lower():
                        done = True
                    else:
                        logging.warning("backup edit failed, sending new: %s", e)
            if not done:
                with open(tmp, "rb") as f:
                    m = await app.bot.send_document(chat_id, f, filename="backup.db",
                                                    caption="💾 نسخة البيانات (لا تحذفها)", disable_notification=True)
                await app.bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
                old, BACKUP["msg"] = BACKUP["msg"], m.message_id
                if old:
                    try:
                        await app.bot.delete_message(chat_id, old)
                    except TelegramError:
                        pass
            os.remove(tmp)
        except Exception as e:
            store.dirty = True
            logging.warning("backup failed: %s", e)


async def send_backup(app, why="auto"):
    """Send a NEW, kept backup file to the first admin's chat (not edited, not deleted)."""
    chat_id = min(ADMIN_IDS)
    tmp = tempfile.mktemp(suffix=".db")
    await asyncio.to_thread(store.snapshot, tmp)
    store.dirty = True  # snapshot clears it; the pinned live copy still needs its own refresh
    n_links = store.q("SELECT COUNT(*) FROM links", one=True)[0]
    n_users = store.q("SELECT COUNT(*) FROM users", one=True)[0]
    name = "backup-" + datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%Y-%m-%d-%H%M") + ".db"
    cap = f"💾 نسخة احتياطية ({'تلقائية كل 48 ساعة' if why == 'auto' else 'يدوية'})\n🔗 {n_links} رابط | 👥 {n_users} مستخدم\nاحتفظ بها. للاسترجاع: لوحة الأدمن > 💾 النسخ الاحتياطي > ♻️ استرجاع."
    with open(tmp, "rb") as f:
        await app.bot.send_document(chat_id, f, filename=name, caption=cap, disable_notification=(why == "auto"))
    os.remove(tmp)
    store.set("bk_last", str(int(time.time())))


async def autobackup_loop(app):
    while True:
        await asyncio.sleep(600)
        try:
            if not ADMIN_IDS or not store.on("bk_on"):
                continue
            last = float(store.get("bk_last") or 0)
            if time.time() - last >= 48 * 3600:
                await send_backup(app, "auto")
        except Exception as e:
            logging.warning("autobackup failed: %s", e)
            await asyncio.sleep(1800)


async def post_init(app):
    asyncio.create_task(autobackup_loop(app))
    for chat, ids, at in store.pending():
        asyncio.create_task(delete_later(app.bot, chat, ids, at))
    asyncio.create_task(backup_loop(app))


# ---------------- admin panel ----------------
def screen(name, ctx):
    """Returns (text, markup) for an admin screen."""
    s = store
    if name == "main":
        nl = s.q("SELECT COUNT(*) FROM links", one=True)[0]
        nu = s.q("SELECT COUNT(*) FROM users", one=True)[0]
        rows = [
            [B("🔗 الروابط والمحتوى", callback_data="a:links")],
            [B("📢 الإذاعة", callback_data="a:comm"), B("👥 المستخدمون", callback_data="a:users")],
            [B("🔐 الاشتراك الإجباري", callback_data="a:sub"), B("💾 النسخ الاحتياطي", callback_data="a:backup")],
            [B("⚙️ الإعدادات", callback_data="a:settings"), B("🛠 النظام والدعم", callback_data="a:sys")],
        ]
        return f"🎛 لوحة الأدمن\n\n🔗 {nl} رابط  |  👥 {nu} مستخدم", M(rows)
    if name == "content":
        return f"📝 المحتوى\n\nرسالة الترحيب الحالية:\n{s.get('welcome')}", M([
            [B("✏️ تعديل رسالة الترحيب", callback_data="w:welcome")], back("settings")])
    if name == "settings":
        return "⚙️ الإعدادات\n\nالمدة والرسالة والتغبيش والقفل والحماية هنا هي قيم أولية للروابط الجديدة فقط. تتنسخ لكل رابط عند إنشائه، وتغييرها ما يغيّر الروابط الموجودة. كل رابط يتعدل لحاله من 🔗 الروابط.", M([
            [B(f"🛠 وضع الصيانة {tick(s.on('maintenance'))}", callback_data="t:maintenance:settings")],
            [B(f"🔐 الاشتراك الإجباري {tick(s.on('gate_on'))}", callback_data="t:gate_on:settings")],
            [B(f"⏱ المدة للروابط الجديدة: {fmt_dur(s.get('delay'))}", callback_data="w:delay")],
            [B(f"🔒 حماية المحتوى للروابط الجديدة {tick(s.on('protect'))}", callback_data="t:protect:settings")],
            [B("✏️ رسالة التنبيه (للروابط الجديدة)", callback_data="w:note")],
            [B(f"🌫 التغبيش للروابط الجديدة: {s.get('blur')} (1-10)", callback_data="w:blur")],
            [B("🔐 رسالة القفل (للروابط الجديدة)", callback_data="w:lock_msg")],
            [B(f"📣 قناة النشر: {s.get('post_chan_name') or 'غير محددة'}", callback_data="n:pchan")],
            [B("✏️ وصف النشر الثابت" + (" ✅" if s.get("pub_cap") else ""), callback_data="n:pcapset")],
            [B("📝 رسالة الترحيب", callback_data="a:content")],
            [B(f"🚫 إشعار الحظر {tick(s.on('ban_notice'))}", callback_data="a:bannotice"),
             B(f"🔔 إشعار الدخول {tick(s.on('notify_join'))}", callback_data="a:joinnotice")],
            back()])
    if name == "users":
        return "👥 المستخدمون\n\nإدارة المستخدمين والأدمنية", M([
            [B("📊 الإحصائيات", callback_data="a:stats"), B("👤 المسؤولون", callback_data="a:admins")],
            [B("🚫 إدارة الحظر", callback_data="a:bans")],
            [B("📋 سجل النشاط", callback_data="a:log")],
            [B("📩 دعوة أشخاص إلى البوت", callback_data="a:invite")], back()])
    if name == "links":
        ls = list(reversed(s.links()))  # newest first
        q_ = (ctx.user_data.get("lq") or "").strip().lower()
        if q_:
            ls = [x for x in ls if q_ in (x[1] or x[0]).lower()]
        per = 16
        pages = max(1, -(-len(ls) // per))
        pg = min(max(int(ctx.user_data.get("lpage") or 0), 0), pages - 1)
        ctx.user_data["lpage"] = pg
        rows = [[B("➕ إنشاء رابط جديد", callback_data="n:new"), B("🔎 بحث بالاسم", callback_data="n:search")]]
        if q_:
            rows.append([B(f"❌ مسح البحث: {q_[:20]}", callback_data="n:clearq")])
        cur = []
        for c, nm, n, v in ls[pg * per:(pg + 1) * per]:
            cur.append(B(f"🔗 {(nm or c)[:18]} · {v}👁", callback_data=f"n:show:{c}"))
            if len(cur) == 2:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        if pages > 1:
            nav = []
            if pg > 0:
                nav += [B("⏮", callback_data="n:page:0"), B("◀️", callback_data=f"n:page:{pg - 1}")]
            nav.append(B(f"{pg + 1}/{pages}", callback_data="n:page:" + str(pg)))
            if pg < pages - 1:
                nav += [B("▶️", callback_data=f"n:page:{pg + 1}"), B("⏭", callback_data=f"n:page:{pages - 1}")]
            rows.append(nav)
        rows.append(back())
        head = f"🔗 الروابط ({len(ls)})" + (f" - نتائج البحث عن: {q_}" if q_ else "") + "\nالأحدث أول."
        if not ls:
            head += "\n\nما كو روابط" + (" بهذا الاسم." if q_ else ".")
        return head + "\n\nاضغط على رابط لعرضه وتعديل إعداداته.", M(rows)
    if name.startswith("lstats:"):
        c = name[7:]
        meta = s.link_meta(c)
        if not meta:
            return "الرابط غير موجود.", M([back("links")])
        d = s.link_stats(c, meta["need"])
        lines = [f"📊 إحصائيات: {meta['name'] or c}", "",
                 f"👁 مرات الفتح: {d['opens']}",
                 f"👤 أشخاص مختلفين دخلوا: {d['people']}",
                 f"🔁 رجعوا أكثر من مرة: {d['repeat']}",
                 f"🆕 مستخدمين جدد للبوت دخلوا من هذا الرابط: {d['new_users']}",
                 f"📦 مرات استلام المحتوى الحقيقي: {meta['views']}"]
        if meta["need"] > 0:
            lines += ["", f"🔒 شافوا المغبش (مقفول عليهم): {d['locked']}",
                      f"🔓 أكملوا {meta['need']} دعوات وانفتح لهم: {d['unlocked']}",
                      f"🤝 دخلوا عن طريق رابط دعوة شخصي: {d['via_invite']}",
                      f"👥 مجموع الدعوات المسجلة: {d['invites']} (من {d['inviters']} شخص)"]
        if d["first"]:
            lines += ["", f"أول دخول: {fmt_ts(d['first'])}", f"آخر دخول: {fmt_ts(d['last'])}"]
        if d["days"]:
            lines += ["", "آخر الأيام (فتح / أشخاص):"] + [f"• {dd}: {o} / {p}" for dd, o, p in d["days"]]
        if d["top"]:
            lines += ["", "🏆 أكثر من دعا:"]
            for uid_, nm, un, cnt in d["top"]:
                lines.append(f"• {nm or uid_}{(' @' + un) if un else ''}: {cnt}")
        lines += ["", "(الإحصائيات تُحسب من تاريخ تفعيل هذه الميزة، ودخولك كأدمن ما ينحسب)"]
        return "\n".join(lines), M([[B("👥 من دخل", callback_data=f"n:vis:{c}:0")], [B("‹ الرابط", callback_data=f"n:show:{c}")]])
    if name.startswith("lvis:"):
        _, c, pg = name.split(":")
        meta = s.link_meta(c)
        if not meta:
            return "الرابط غير موجود.", M([back("links")])
        per = 10
        total, rows_ = s.visits_page(c, per, int(pg) * per)
        pages = max(1, -(-total // per))
        lines = [f"👥 من دخل: {meta['name'] or c} ({total} دخول) - صفحة {int(pg) + 1}/{pages}", ""]
        for ts, uid_, nm, un, isnew, inv, invname, locked in rows_:
            tags = ("🆕 جديد" if isnew else "🔁 معروف") + (" · 🔒" if locked else " · 🔓")
            via = f" · عبر {invname or inv}" if inv else " · مباشر"
            lines.append(f"• {fmt_ts(ts)} {nm or uid_}{(' @' + un) if un else ''} [{uid_}] {tags}{via}")
        if not rows_:
            lines.append("ما كو دخول لحد الآن.")
        nav = []
        if int(pg) > 0:
            nav.append(B("◀️", callback_data=f"n:vis:{c}:{int(pg) - 1}"))
        if int(pg) < pages - 1:
            nav.append(B("▶️", callback_data=f"n:vis:{c}:{int(pg) + 1}"))
        kb = ([nav] if nav else []) + [[B("📊 الإحصائيات", callback_data=f"n:stats:{c}")], [B("‹ الرابط", callback_data=f"n:show:{c}")]]
        return "\n".join(lines)[:4000], M(kb)
    if name.startswith("link:"):
        c = name[5:]
        meta = s.link_meta(c)
        if not meta:
            return "الرابط غير موجود.", M([back("links")])
        got = s.get_link(c)
        d = meta["delay"] or 30
        txt = (f"🔗 {meta['name'] or c}\n\nالرابط:\nhttps://t.me/{ctx.bot.username}?start={c}\n\n"
               f"الملفات: {len(got[0])} | المشاهدات: {meta['views']}\n"
               f"(كل الإعدادات تحت خاصة بهذا الرابط فقط)\n"
               f"👥 الدعوات المطلوبة: {meta['need'] or 'بدون قفل'} (دعوات مسجلة: {s.invite_total(c)})\n"
               f"🌫 قوة التغبيش: {meta['blur']}\n"
               f"⏱ مدة الحذف: {fmt_dur(d)}\n"
               f"🔒 منع التوجيه/الحفظ: {'مفعّل' if meta['protect'] else 'متوقف'}\n\n"
               f"💬 الرسالة بعد المحتوى:\n{(meta['note'] or '').replace('{s}', fmt_dur(d))}\n\n"
               f"🔐 رسالة القفل:\n{meta['lock_msg']}")
        return txt, M([
            [B("✏️ الاسم", callback_data=f"n:name:{c}"), B("⏱ المدة", callback_data=f"n:time:{c}")],
            [B("💬 الرسالة بعد المحتوى", callback_data=f"n:msg:{c}"), B("🔐 رسالة القفل", callback_data=f"n:lock:{c}")],
            [B("👥 الدعوات المطلوبة", callback_data=f"n:need:{c}"), B("🌫 التغبيش", callback_data=f"n:blur:{c}")],
            [B(f"🔒 منع التوجيه/الحفظ {tick(meta['protect'])}", callback_data=f"n:prot:{c}")],
            [B("📣 نشر في قناة", callback_data=f"n:pub:{c}")],
            [B("📊 إحصائيات هذا الرابط", callback_data=f"n:stats:{c}"), B("👥 من دخل", callback_data=f"n:vis:{c}:0")],
            [B("♻️ نسخ الإعدادات الافتراضية لهذا الرابط فقط", callback_data=f"n:reset:{c}")],
            [B("🗑 حذف الرابط", callback_data=f"n:del:{c}")], back("links")])
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
        aud = s.get("bc_aud")
        ttl = int(s.get("bc_ttl") or 0)
        btn = s.get("bc_btn")
        txt = (f"📢 إذاعة — 📨 مباشر\n━━━━━━━━━━━━\n\nاختر الجمهور\n\n"
               f"👥 الجمهور الحالي: {aud_text(aud)} ({len(s.audience_ids(aud))} مستخدم)\n"
               f"🔕 إرسال صامت: {'مفعّل' if s.on('bc_silent') else 'متوقف'}\n"
               f"⏳ حذف الإذاعة تلقائياً: {fmt_dur(ttl) if ttl else 'متوقف'}\n"
               f"🔘 زر رابط تحت الرسالة: {btn.split('|', 1)[0].strip() if btn else 'بدون'}")
        rows = [[B("👥 الجميع", callback_data="b:segall:go")],
                [B("🎯 فئات محددة", callback_data="a:bcaud")],
                [B(f"🔕 صامت {tick(s.on('bc_silent'))}", callback_data="t:bc_silent:comm"),
                 B("⏳ حذف تلقائي", callback_data="w:bc_ttl")],
                [B("🔘 زر رابط", callback_data="w:bc_btn")] + ([B("🗑 إزالة الزر", callback_data="b:nobtn")] if btn else []),
                [B("📜 سجل الإذاعات", callback_data="a:bclog")], back()]
        return txt, M(rows)
    if name == "bcaud":
        aud = s.get("bc_aud")
        sel = [x for x in aud.split(",") if x in AUD_LABELS]
        cnt = {k: len(s.audience_ids(k)) for k in AUD_LABELS}
        total = len(s.audience_ids(""))
        txt = ("لمن ترسل؟\n\nاختر الفئة التي تريد إرسال الإذاعة إليها (تقدر تختار أكثر من فئة):\n\n"
               f"• الجميع — كل المستخدمين بدون استثناء ({total})\n"
               f"• جدد 7 أيام — من بدأ البوت خلال آخر أسبوع ({cnt['new7']})\n"
               f"• جدد 30 يوم — من بدأ البوت خلال آخر شهر ({cnt['new30']})\n"
               f"• غير نشطين +7 — لم يتفاعلوا منذ أكثر من أسبوع ({cnt['off7']})\n"
               f"• غير نشطين +30 — لم يتفاعلوا منذ أكثر من شهر ({cnt['off30']})\n\n"
               f"المحدد الآن: {aud_text(aud)} = {len(s.audience_ids(aud))} مستخدم")
        mk = lambda k, label: B(("✅ " if k in sel else "☑️ ") + label, callback_data=f"b:seg:{k}")
        rows = [[B(("✅ " if not sel else "☑️ ") + "إرسال إلى الجميع", callback_data="b:segall")],
                [mk("new30", "جدد — 30 يومًا"), mk("new7", "جدد — ٧ أيام")],
                [mk("off30", "غير نشطين +30"), mk("off7", "غير نشطين +7")],
                [B("🔹 التالي: كتابة الرسالة 🔹", callback_data="b:next")], back("comm")]
        return txt, M(rows)
    if name == "bclog":
        rows_ = s.bc_log()
        lines = ["📜 آخر الإذاعات", ""]
        for ts, tx in rows_:
            lines.append(f"• {fmt_ts(ts)} {tx}")
        if not rows_:
            lines.append("ما كو إذاعات بعد.")
        return "\n".join(lines), M([back("comm")])
    if name == "backup":
        last = float(s.get("bk_last") or 0)
        nxt = last + 48 * 3600
        return ("💾 النسخ الاحتياطي\n\n"
                "البوت يرسل لك ملف نسخة (روابط، محتوى، إعدادات، مستخدمين، إحصائيات) كل 48 ساعة كرسالة جديدة بمحادثتك، "
                "وتبقى عندك. احتفظ بها أو حملها لمكان ثاني (جوجل درايف مثلاً).\n\n"
                f"آخر نسخة: {fmt_ts(last) if last else 'لا يوجد'}\n"
                f"النسخة القادمة: {fmt_ts(nxt) if last and s.on('bk_on') else '-'}\n\n"
                "لو انحذفت الروابط: اضغط استرجاع وأرسل ملف النسخة.",
                M([[B("💾 نسخة الآن", callback_data="a:bknow")],
                   [B(f"⏱ تلقائي كل 48 ساعة {tick(s.on('bk_on'))}", callback_data="t:bk_on:backup")],
                   [B("♻️ استرجاع من ملف", callback_data="a:bkrestore")], back()]))
    if name == "sys":
        up = int(time.time() - START)
        return (f"🛠 النظام والدعم\n\nمدة التشغيل: {up//3600}س {(up%3600)//60}د\n"
                f"💾 البيانات تنحفظ تلقائياً كملف مثبّت (backup.db) بمحادثتك مع البوت. لا تحذفه.\n\nنص الدعم:\n{s.get('support')}",
                M([[B("✏️ تعديل نص الدعم", callback_data="w:support")],
                   [B("🆘 رابط الإبلاغ عن مشكلة", callback_data="a:report")], back()]))
    if name == "report":
        return f"🆘 الإبلاغ عن مشكلة\n\nرابط حالي: {s.get('report_url') or 'غير محدد'}\nيظهر للمستخدم عند أمر /report", M([
            [B("✏️ تعديل الرابط", callback_data="w:report_url")], back("sys")])
    if name == "bannotice":
        return f"🚫 إشعار الحظر {tick(s.on('ban_notice'))}\n\nالرسالة:\n{s.get('ban_msg')}", M([
            [B("تشغيل/إيقاف", callback_data="t:ban_notice:bannotice"), B("✏️ تعديل", callback_data="w:ban_msg")], back("settings")])
    if name == "joinnotice":
        return f"🔔 إشعار الدخول {tick(s.on('notify_join'))}\n\nيوصلك إشعار عند دخول مستخدم جديد.", M([
            [B("تشغيل/إيقاف", callback_data="t:notify_join:joinnotice")], back("settings")])
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
    "broadcast": "أرسل نص الإذاعة، أو صورة/فيديو مع تعليق (أو وجّه رسالتك هنا). راح أعرضها لك قبل الإرسال:",
    "bc_ttl": "أرسل بعد كم تنحذف الإذاعة عند المستخدمين (مثال: 30 دقيقة أو 2 ساعة، لحد 24 ساعة). أرسل 0 لإيقاف الحذف:",
    "bc_btn": "أرسل الزر بهذا الشكل:\nنص الزر | https://رابط\n(أرسل - لإزالته)",
    "blur": "أرسل قوة التغبيش من 1 (خفيف) إلى 10 (قوي). قيمة أولية للروابط الجديدة فقط:",
    "lock_msg": "أرسل رسالة القفل. استخدم {n} للعدد المطلوب، {have} لما دعاه، {link} لرابطه الشخصي:",
    "delay": "أرسل مدة الحذف الافتراضية. مثال: 30 أو 2 دقيقة:",
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
    if q.data == "a:bknow":
        try:
            await send_backup(ctx.application, "manual")
            await q.message.reply_text("تم، أرسلت النسخة فوق.")
        except Exception as e:
            await q.message.reply_text("فشل إرسال النسخة: " + str(e)[:100])
        t, m = screen("backup", ctx)
        await edit(q, t, m)
        return
    if q.data == "a:bkrestore":
        ctx.user_data["await"] = "restore"
        await q.message.reply_text("أرسل ملف النسخة (backup...db) الي وصلك من البوت.\nتنبيه: الاسترجاع يبدل كل البيانات الحالية بالنسخة.\n(/cancel للإلغاء)")
        return
    if q.data == "a:bkno":
        p = ctx.user_data.pop("restore_tmp", None)
        if p and os.path.exists(p):
            os.remove(p)
        await q.message.reply_text("تم الإلغاء، ما تغير شي.")
        return
    if q.data == "a:bkok":
        p = ctx.user_data.pop("restore_tmp", None)
        if not p or not os.path.exists(p):
            await q.message.reply_text("انتهت صلاحية الملف، ابدأ من جديد.")
            return
        try:
            await send_backup(ctx.application, "manual")  # safety copy of the current data first
            await asyncio.to_thread(store.restore_from, p)
            os.remove(p)
            await q.message.reply_text("✅ تم الاسترجاع. (أرسلت نسخة من البيانات السابقة قبلها للاحتياط.)")
        except Exception as e:
            logging.warning("restore failed: %s", e)
            await q.message.reply_text("فشل الاسترجاع: " + str(e)[:100])
        return
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
    elif k == "b":  # broadcast controls
        act = parts[1]
        if act == "seg":
            sel = [x for x in store.get("bc_aud").split(",") if x in AUD_LABELS]
            k_ = parts[2]
            sel = [x for x in sel if x != k_] if k_ in sel else sel + [k_]
            store.set("bc_aud", ",".join(sel))
            t, m = screen("bcaud", ctx)
            await edit(q, t, m)
        elif act == "segall":
            store.set("bc_aud", "")
            t, m = screen("bcaud", ctx)
            await edit(q, t, m)
        elif act == "next":
            ctx.user_data["await"] = "broadcast"
            await q.message.reply_text(PROMPTS["broadcast"] + f"\n\nالجمهور: {aud_text(store.get('bc_aud'))} ({len(store.audience_ids(store.get('bc_aud')))})\n(/cancel للإلغاء)")
        elif act == "nobtn":
            store.set("bc_btn", "")
            t, m = screen("comm", ctx)
            await edit(q, t, m)
        elif act == "stop":
            ctx.application.bot_data["bc_stop"] = True
            await q.answer("جاري الإيقاف")
        elif act == "yes":
            bc = ctx.user_data.pop("bc", None)
            if not bc:
                return
            if ctx.application.bot_data.get("bc_running"):
                await q.message.reply_text("فيه إذاعة شغالة هسه. انتظر تخلص أو اضغط إيقاف.")
                ctx.user_data["bc"] = bc
                return
            ctx.application.bot_data["bc_stop"] = False
            ctx.application.bot_data["bc_running"] = True
            prog = await q.message.reply_text("⏳ جاري الإرسال...", reply_markup=M([[B("🛑 إيقاف", callback_data="b:stop")]]))
            asyncio.create_task(run_broadcast(ctx.application, user.id, bc, prog))
        else:
            ctx.user_data.pop("bc", None)
            await q.message.reply_text("أُلغيت.")


def bc_markup():
    btn = store.get("bc_btn")
    if btn and "|" in btn:
        t, u = [x.strip() for x in btn.split("|", 1)]
        if t and u.startswith(("http://", "https://", "tg://")):
            return M([[B(t, url=u)]])
    return None


def _ents(bot, bc, strip_custom=False):
    out = []
    for d in bc.get("ents") or []:
        if strip_custom and d.get("type") == "custom_emoji":
            continue
        out.append(MessageEntity.de_json(d, bot))
    return out or None


def _has_custom(bc):
    return any(d.get("type") == "custom_emoji" for d in (bc.get("ents") or []))


async def bc_send(bot, uid, bc, silent, markup):
    """Sends one broadcast item. Keeps formatting and animated (Premium) emoji; if Telegram refuses the custom emoji,
    resends with the normal emoji characters and flags bc['_fallback']."""
    for strip in ((False, True) if _has_custom(bc) else (False,)):
        kw = {"disable_notification": silent, "reply_markup": markup}
        try:
            if bc["k"] == "text":
                return await bot.send_message(uid, bc["text"], entities=_ents(bot, bc, strip), **kw)
            fn = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation,
                  "document": bot.send_document}[bc["k"]]
            return await fn(uid, bc["f"], caption=bc.get("cap") or None, caption_entities=_ents(bot, bc, strip), **kw)
        except BadRequest as e:
            if strip or not _has_custom(bc):
                raise
            logging.warning("custom emoji refused: %s", e)
            bc["ents"] = [d for d in bc["ents"] if d.get("type") != "custom_emoji"]  # stop trying for later recipients
            bc["_fallback"] = True
            strip = True
            try:
                return await bc_send(bot, uid, bc, silent, markup)
            except BadRequest:
                raise


async def run_broadcast(app, admin_id, bc, prog):
    bot = app.bot
    aud = store.get("bc_aud")
    ids = store.audience_ids(aud)
    silent = store.on("bc_silent")
    ttl = int(store.get("bc_ttl") or 0)
    markup = bc_markup()
    ok = bad = blocked = 0
    stopped = False
    try:
        for n, uid in enumerate(ids, 1):
            if app.bot_data.get("bc_stop"):
                stopped = True
                break
            for attempt in (0, 1):
                try:
                    m = await bc_send(bot, uid, bc, silent, markup)
                    ok += 1
                    if ttl:
                        at = time.time() + ttl
                        store.add_pending(uid, [m.message_id], at)
                        asyncio.create_task(delete_later(bot, uid, [m.message_id], at))
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except Forbidden:
                    blocked += 1
                    break
                except TelegramError:
                    bad += 1
                    break
            await asyncio.sleep(0.05)
            if n % 100 == 0:
                try:
                    await prog.edit_text(f"⏳ جاري الإرسال... {n}/{len(ids)}\nوصلت: {ok}", reply_markup=M([[B("🛑 إيقاف", callback_data="b:stop")]]))
                except TelegramError:
                    pass
    finally:
        app.bot_data["bc_running"] = False
    prev = (bc.get("text") or bc.get("cap") or bc["k"])[:25].replace("\n", " ")
    store.log("broadcast", admin_id, f"{aud_text(aud)} | وصلت {ok} | حاظرين البوت {blocked} | فشلت {bad} | {prev}")
    try:
        await prog.edit_text(("🛑 توقفت. " if stopped else "✅ تمت الإذاعة. ") + f"وصلت: {ok}\nحاظرين البوت: {blocked}\nفشلت: {bad}" + (f"\nتنحذف بعد {fmt_dur(ttl)}" if ttl else ""))
    except TelegramError:
        pass


async def cancel(update: Update, ctx):
    ctx.user_data.pop("await", None)
    ctx.user_data.pop("effect", None)
    await update.message.reply_text("تم الإلغاء.")


async def admin_input(update: Update, ctx) -> bool:
    key = ctx.user_data.get("await")
    if not key or not is_admin(update.effective_user):
        return False
    ctx.user_data.pop("await")
    if key in ("pcap", "pchan", "pcapset"):
        return await pub_input(update, ctx, key)
    if key == "restore":
        await update.message.reply_text("أرسل ملف النسخة كمستند، مو نص. ابدأ من جديد من 💾 النسخ الاحتياطي.")
        return True
    text = update.message.text.strip()
    s = store
    uid = update.effective_user.id
    if key.startswith(("lname:", "ldelay:", "lnote:", "lneed:", "lblur:", "llock:")):
        kind, code = key.split(":", 1)
        if not s.link_meta(code):
            await update.message.reply_text("الرابط غير موجود.")
            return True
        if kind == "llock":
            s.update_link(code, "lock_msg", text)
        elif kind == "lblur":
            if not text.isdigit() or not 1 <= int(text) <= 10:
                ctx.user_data["await"] = key
                await update.message.reply_text("رقم من 1 إلى 10.")
                return True
            s.update_link(code, "blur", int(text))
        elif kind == "lneed":
            if not text.isdigit() or int(text) > 1000:
                ctx.user_data["await"] = key
                await update.message.reply_text("أرسل رقم فقط (0 بدون قفل).")
                return True
            s.update_link(code, "need", int(text))
        elif kind == "ldelay":
            sec = parse_duration(text)
            if sec is None:
                ctx.user_data["await"] = key
                await update.message.reply_text("ما فهمت المدة. اكتب مثل: 30 أو 45 ثانية أو 2 دقيقة (من 5 ثواني إلى 24 ساعة).")
                return True
            s.update_link(code, "delay", sec)
        else:
            s.update_link(code, "name" if kind == "lname" else "note", text[:100] if kind == "lname" else text)
        if kind == "lname" and ctx.user_data.get("newlink") == code:
            ctx.user_data.pop("newlink")
            ctx.user_data["await"] = "lneed:" + code
            await update.message.reply_text("✔️ انحفظ الاسم.\n\nكم شخص لازم يدعو المستخدم حتى يفتح الصور والفيديوهات؟ أرسل رقم (0 = بدون قفل، تنفتح مباشرة):")
            return True
        t, m = screen("link:" + code, ctx)
        await update.message.reply_text("✔️ تم.\n\n" + t, reply_markup=m)
        return True
    if key == "lsearch":
        ctx.user_data["lq"] = text[:50]
        ctx.user_data["lpage"] = 0
        t, m = screen("links", ctx)
        await update.message.reply_text(t, reply_markup=m)
        return True
    if key == "delay":
        sec = parse_duration(text)
        if sec is not None:
            s.set("delay", str(sec))
            await update.message.reply_text("✔️ تم الحفظ.")
        else:
            await update.message.reply_text("اكتب مثل: 30 أو 2 دقيقة (من 5 ثواني إلى 24 ساعة).")
        return True
    if key == "blur":
        if text.isdigit() and 1 <= int(text) <= 10:
            s.set("blur", text)
            await update.message.reply_text("✔️ تم. تنطبق على الروابط الجديدة فقط.")
        else:
            await update.message.reply_text("رقم من 1 إلى 10.")
        return True
    if key in ("welcome", "gate_msg", "support", "ban_msg", "note", "lock_msg"):
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
        raw = update.message.text
        await bc_preview(update, ctx, {"k": "text", "text": raw,
                                       "ents": [e.to_dict() for e in (update.message.entities or [])]})
    elif key == "bc_ttl":
        if text in ("0", "-"):
            s.set("bc_ttl", "0")
        else:
            sec = parse_duration(text)
            if sec is None:
                ctx.user_data["await"] = "bc_ttl"
                await update.message.reply_text("ما فهمت. مثال: 30 دقيقة أو 2 ساعة، أو 0 للإيقاف.")
                return True
            s.set("bc_ttl", str(sec))
        t, m = screen("comm", ctx)
        await update.message.reply_text("✔️ تم.\n\n" + t, reply_markup=m)
    elif key == "bc_btn":
        if text == "-":
            s.set("bc_btn", "")
        elif "|" in text and text.split("|", 1)[1].strip().startswith(("http://", "https://", "tg://")):
            s.set("bc_btn", text[:300])
        else:
            ctx.user_data["await"] = "bc_btn"
            await update.message.reply_text("الشكل: نص الزر | https://رابط")
            return True
        t, m = screen("comm", ctx)
        await update.message.reply_text("✔️ تم.\n\n" + t, reply_markup=m)
    return True


async def bc_preview(update, ctx, bc):
    ctx.user_data["bc"] = bc
    aud = store.get("bc_aud")
    n = len(store.audience_ids(aud))
    ttl = int(store.get("bc_ttl") or 0)
    await update.message.reply_text("👀 هكذي راح توصل الإذاعة:")
    try:
        await bc_send(update.get_bot(), update.effective_chat.id, bc, False, bc_markup())
    except TelegramError as e:
        await update.message.reply_text(f"ما كدرت أعرض المعاينة: {e}")
        return
    if bc.pop("_fallback", False):
        await update.message.reply_text("⚠️ تيليجرام رفض الإيموجي المتحركة (المميزة). لازم حساب مالك البوت يكون عنده Telegram Premium. إذا أرسلت هيجي، راح توصل الإيموجي عادية بدون حركة.")
    elif _has_custom(bc):
        await update.message.reply_text("✨ الإيموجي المتحركة انقبلت بالمعاينة، وراح توصل متحركة.")
    await update.message.reply_text(
        f"راح ترسل لـ {n} مستخدم ({aud_text(aud)})"
        f"{' بصمت' if store.on('bc_silent') else ''}"
        f"{', وتنحذف بعد ' + fmt_dur(ttl) if ttl else ''}.",
        reply_markup=M([[B("✅ إرسال", callback_data="b:yes"), B("❌ إلغاء", callback_data="b:no")]]))


async def text_router(update: Update, ctx):
    await admin_input(update, ctx)


def _add_health_route():
    """UptimeRobot keep-alive: GET / and HEAD / return 200 (PTB's webhook server only knows the token path)."""
    import tornado.web
    from telegram.ext._utils import webhookhandler as wh

    class Health(tornado.web.RequestHandler):
        def get(self):
            self.write("ok")

        def head(self):
            self.set_status(200)

    orig = wh.WebhookAppClass.__init__

    def patched(self, *a, **k):
        orig(self, *a, **k)
        self.add_handlers(r".*", [(r"/", Health)])

    wh.WebhookAppClass.__init__ = patched


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern=r"^[atwcbn]:"))
    app.add_handler(CallbackQueryHandler(user_cb, pattern=r"^u:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL, collect_media))
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        _add_health_route()
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 10000)), url_path=TOKEN,
                        webhook_url=f"{url}/{TOKEN}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
