import sqlite3, threading, time, json

DEFAULTS = {
    "bk_on": "1",
    "welcome": "أهلاً! استخدم رابط دعوة صالح لاستلام المحتوى.",
    "delay": "30",
    "protect": "0",
    "note": "⏳ ستنحذف هذه الرسائل بعد {s}.\nأعد توجيهها أو احفظها قبل الحذف.",
    "blur": "3",
    "lock_msg": "🔒 هذا المحتوى مقفول.\nلفتح الصور والفيديوهات لازم تدعو {n} أشخاص عبر رابطك الخاص:\n{link}\n\nدعوت لحد الآن: {have} من {n}",
    "deleted_note": "🗑 تم حذف المحتوى.",
    "maintenance": "0",
    "bc_aud": "",
    "bc_silent": "0",
    "bc_ttl": "0",
    "bc_btn": "",
    "notify_join": "1",
    "ban_notice": "1",
    "ban_msg": "🚫 تم حظرك من استخدام البوت.",
    "gate_on": "1",
    "gate_notify": "1",
    "gate_button": "1",
    "gate_msg": "🔐 للاستمرار لازم تشترك بالقنوات التالية أول، وبعدها اضغط تحقق.",
    "support": "للتواصل مع الدعم راسل الأدمن.",
    "report_url": "",
}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.dirty = False
        self._schema()

    def _schema(self):
        self.q("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT,
                  joined REAL, last_seen REAL, banned INTEGER DEFAULT 0, inviter INTEGER)""")
        self.q("CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY)")
        self.q("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
        self.q("CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, enabled INTEGER DEFAULT 1)")
        self.q("CREATE TABLE IF NOT EXISTS links(code TEXT PRIMARY KEY, items TEXT, created REAL, views INTEGER DEFAULT 0)")
        for col, typ in (("name", "TEXT"), ("delay", "INTEGER"), ("note", "TEXT"), ("need", "INTEGER"), ("blur", "INTEGER"), ("lock_msg", "TEXT"), ("protect", "INTEGER"), ("seq", "INTEGER")):
            try:
                self.q(f"ALTER TABLE links ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        try:
            self.q("ALTER TABLE users ADD COLUMN src TEXT")
        except sqlite3.OperationalError:
            pass
        self.q("CREATE TABLE IF NOT EXISTS link_visits(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, uid INTEGER, ts REAL, inviter INTEGER, locked INTEGER, is_new INTEGER)")
        if not self.get("linkseq"):
            self.set("linkseq", str(self.q("SELECT COUNT(*) FROM links", one=True)[0]))
        self.q("CREATE TABLE IF NOT EXISTS invites(code TEXT, inviter INTEGER, invited INTEGER, ts REAL, PRIMARY KEY(code, invited))")
        self.q("CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, ids TEXT, at REAL)")
        self.q("CREATE TABLE IF NOT EXISTS log(ts REAL, kind TEXT, uid INTEGER, text TEXT)")

    @staticmethod
    def check_backup(path):
        """(links, users) counts if path is a valid backup of this bot, else None."""
        try:
            c = sqlite3.connect(path)
            try:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return None
                names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"links", "users", "settings"} <= names:
                    return None
                return (c.execute("SELECT COUNT(*) FROM links").fetchone()[0],
                        c.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            finally:
                c.close()
        except Exception:
            return None

    def restore_from(self, path):
        with self.lock:
            src = sqlite3.connect(path)
            src.backup(self.db)
            src.close()
        self._schema()
        self.dirty = True

    def q(self, sql, args=(), one=False, many=False):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            if not sql.lstrip().upper().startswith("SELECT"):
                self.dirty = True
            if one:
                return cur.fetchone()
            if many:
                return cur.fetchall()
            return None

    # settings
    def get(self, k):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,), one=True)
        return r[0] if r else DEFAULTS.get(k, "")

    def set(self, k, v):
        self.q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def on(self, k):
        return self.get(k) == "1"

    def toggle(self, k):
        self.set(k, "0" if self.on(k) else "1")

    # users
    def touch_user(self, uid, name, username, inviter=None, src=None):
        """Returns True if the user is new."""
        r = self.q("SELECT id FROM users WHERE id=?", (uid,), one=True)
        now = time.time()
        if r:
            self.q("UPDATE users SET name=?, username=?, last_seen=? WHERE id=?", (name, username, now, uid))
            return False
        self.q("INSERT INTO users(id,name,username,joined,last_seen,inviter,src) VALUES(?,?,?,?,?,?,?)",
               (uid, name, username, now, now, inviter, src))
        return True

    def all_user_ids(self, include_banned=False):
        sql = "SELECT id FROM users" + ("" if include_banned else " WHERE banned=0")
        return [r[0] for r in self.q(sql, many=True)]

    SEG_SQL = {"new7": "joined>{t7}", "new30": "joined>{t30}", "off7": "last_seen<{t7}", "off30": "last_seen<{t30}"}

    def audience_ids(self, aud=""):
        """aud: comma list of segments (union). Empty = everyone not banned."""
        now = time.time()
        segs = [x for x in (aud or "").split(",") if x in self.SEG_SQL]
        cond = ""
        if segs:
            cond = " AND (" + " OR ".join(self.SEG_SQL[x].format(t7=now - 7 * 86400, t30=now - 30 * 86400) for x in segs) + ")"
        return [r[0] for r in self.q("SELECT id FROM users WHERE banned=0" + cond, many=True)]

    def bc_log(self, n=10):
        return self.q("SELECT ts,text FROM log WHERE kind='broadcast' ORDER BY ts DESC LIMIT ?", (n,), many=True)

    def is_banned(self, uid):
        r = self.q("SELECT banned FROM users WHERE id=?", (uid,), one=True)
        return bool(r and r[0])

    def set_ban(self, uid, flag):
        r = self.q("SELECT id FROM users WHERE id=?", (uid,), one=True)
        if not r:
            self.q("INSERT INTO users(id,name,username,joined,last_seen) VALUES(?,?,?,?,?)",
                   (uid, "", "", time.time(), time.time()))
        self.q("UPDATE users SET banned=? WHERE id=?", (1 if flag else 0, uid))

    def banned_list(self):
        return self.q("SELECT id,name,username FROM users WHERE banned=1", many=True)

    def stats(self):
        total = self.q("SELECT COUNT(*) FROM users", one=True)[0]
        day = self.q("SELECT COUNT(*) FROM users WHERE joined>?", (time.time() - 86400,), one=True)[0]
        active = self.q("SELECT COUNT(*) FROM users WHERE last_seen>?", (time.time() - 86400,), one=True)[0]
        banned = self.q("SELECT COUNT(*) FROM users WHERE banned=1", one=True)[0]
        return total, day, active, banned

    def invited_count(self, uid):
        return self.q("SELECT COUNT(*) FROM users WHERE inviter=?", (uid,), one=True)[0]

    # admins
    def admins(self):
        return [r[0] for r in self.q("SELECT id FROM admins", many=True)]

    def add_admin(self, uid):
        self.q("INSERT OR IGNORE INTO admins(id) VALUES(?)", (uid,))

    def del_admin(self, uid):
        self.q("DELETE FROM admins WHERE id=?", (uid,))

    # channels
    def channels(self):
        return self.q("SELECT id,ref,enabled FROM channels ORDER BY id", many=True)

    def add_channel(self, ref):
        self.q("INSERT OR IGNORE INTO channels(ref,enabled) VALUES(?,1)", (ref,))

    def toggle_channel(self, cid):
        self.q("UPDATE channels SET enabled=1-enabled WHERE id=?", (cid,))

    def del_channel(self, cid):
        self.q("DELETE FROM channels WHERE id=?", (cid,))

    def set_all_channels(self, flag):
        self.q("UPDATE channels SET enabled=?", (1 if flag else 0,))

    # log
    def log(self, kind, uid, text=""):
        self.q("INSERT INTO log(ts,kind,uid,text) VALUES(?,?,?,?)", (time.time(), kind, uid, text[:200]))

    def recent_log(self, n=20):
        return self.q("SELECT ts,kind,uid,text FROM log ORDER BY ts DESC LIMIT ?", (n,), many=True)

    # links
    def add_link(self, code, items, name=""):
        """New link copies the current defaults, so later changes to one link or to the defaults never touch another link."""
        seq = int(self.get("linkseq") or 0) + 1
        self.set("linkseq", str(seq))
        self.q("INSERT INTO links(code,items,created,name,delay,note,blur,lock_msg,protect,seq) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (code, json.dumps(items), time.time(), name, int(self.get("delay") or 30), self.get("note"),
                int(self.get("blur") or 3), self.get("lock_msg"), 1 if self.on("protect") else 0, seq))
        return seq

    def reset_link(self, code):
        self.q("UPDATE links SET delay=?,note=?,blur=?,lock_msg=?,protect=? WHERE code=?",
               (int(self.get("delay") or 30), self.get("note"), int(self.get("blur") or 3), self.get("lock_msg"),
                1 if self.on("protect") else 0, code))

    def freeze_links(self):
        """Old links that relied on the shared defaults get their own copy of the values now in effect."""
        self.q("UPDATE links SET delay=? WHERE delay IS NULL", (int(self.get("delay") or 30),))
        self.q("UPDATE links SET note=? WHERE note IS NULL", (self.get("note"),))
        self.q("UPDATE links SET blur=? WHERE blur IS NULL", (int(self.get("blur") or 3),))
        self.q("UPDATE links SET lock_msg=? WHERE lock_msg IS NULL", (self.get("lock_msg"),))
        self.q("UPDATE links SET protect=? WHERE protect IS NULL", (1 if self.on("protect") else 0,))

    def link_meta(self, code):
        r = self.q("SELECT name,delay,note,views,need,blur,lock_msg,protect FROM links WHERE code=?", (code,), one=True)
        return {"name": r[0] or "", "delay": r[1], "note": r[2], "views": r[3], "need": r[4] or 0, "blur": r[5], "lock_msg": r[6], "protect": bool(r[7])} if r else None

    def update_link(self, code, field, value):
        assert field in ("name", "delay", "note", "need", "blur", "lock_msg", "protect")
        self.q(f"UPDATE links SET {field}=? WHERE code=?", (value, code))

    def get_link(self, code):
        r = self.q("SELECT items,views FROM links WHERE code=?", (code,), one=True)
        return (json.loads(r[0]), r[1]) if r else None

    def links(self):
        return [(c, n or "", len(json.loads(i)), v) for c, n, i, v in self.q("SELECT code,name,items,views FROM links ORDER BY created", many=True)]

    def del_link(self, code):
        self.q("DELETE FROM links WHERE code=?", (code,))

    def inc_views(self, code):
        self.q("UPDATE links SET views=views+1 WHERE code=?", (code,))

    # pending deletions
    def add_pending(self, chat, ids, at):
        self.q("INSERT INTO pending(chat,ids,at) VALUES(?,?,?)", (chat, json.dumps(ids), at))

    def del_pending(self, chat, ids):
        self.q("DELETE FROM pending WHERE chat=? AND ids=?", (chat, json.dumps(ids)))

    def pending(self):
        return [(c, json.loads(i), a) for c, i, a in self.q("SELECT chat,ids,at FROM pending", many=True)]

    # backup
    def snapshot(self, path):
        with self.lock:
            dst = sqlite3.connect(path)
            self.db.backup(dst)
            dst.close()
            self.dirty = False

    def set_items(self, code, items):
        self.q("UPDATE links SET items=? WHERE code=?", (json.dumps(items), code))

    def add_invite(self, code, inviter, invited):
        """True if this invited user is newly counted for this link."""
        r = self.q("SELECT 1 FROM invites WHERE code=? AND invited=?", (code, invited), one=True)
        if r:
            return False
        self.q("INSERT INTO invites(code,inviter,invited,ts) VALUES(?,?,?,?)", (code, inviter, invited, time.time()))
        return True

    def invite_count(self, code, inviter):
        return self.q("SELECT COUNT(*) FROM invites WHERE code=? AND inviter=?", (code, inviter), one=True)[0]

    def invite_total(self, code):
        return self.q("SELECT COUNT(*) FROM invites WHERE code=?", (code,), one=True)[0]

    # ---- per-link statistics ----
    def link_seq(self, code):
        r = self.q("SELECT seq FROM links WHERE code=?", (code,), one=True)
        return r[0] if r else None

    def add_visit(self, code, uid, inviter, locked, is_new):
        self.q("INSERT INTO link_visits(code,uid,ts,inviter,locked,is_new) VALUES(?,?,?,?,?,?)",
               (code, uid, time.time(), inviter, 1 if locked else 0, 1 if is_new else 0))

    def user_src(self, uid):
        r = self.q("SELECT src FROM users WHERE id=?", (uid,), one=True)
        return r[0] if r else None

    def link_stats(self, code, need):
        one = lambda sql, a=(): self.q(sql, a, one=True)[0]
        d = {}
        d["opens"] = one("SELECT COUNT(*) FROM link_visits WHERE code=?", (code,))
        d["people"] = one("SELECT COUNT(DISTINCT uid) FROM link_visits WHERE code=?", (code,))
        d["repeat"] = one("SELECT COUNT(*) FROM (SELECT uid FROM link_visits WHERE code=? GROUP BY uid HAVING COUNT(*)>1)", (code,))
        d["new_users"] = one("SELECT COUNT(*) FROM users WHERE src=?", (code,))
        d["locked"] = one("SELECT COUNT(DISTINCT uid) FROM link_visits WHERE code=? AND locked=1", (code,))
        d["via_invite"] = one("SELECT COUNT(DISTINCT uid) FROM link_visits WHERE code=? AND inviter IS NOT NULL", (code,))
        d["invites"] = one("SELECT COUNT(*) FROM invites WHERE code=?", (code,))
        d["inviters"] = one("SELECT COUNT(DISTINCT inviter) FROM invites WHERE code=?", (code,))
        if need > 0:
            d["unlocked"] = one("SELECT COUNT(*) FROM (SELECT inviter FROM invites WHERE code=? GROUP BY inviter HAVING COUNT(*)>=?)", (code, need))
        else:
            d["unlocked"] = d["people"]
        r = self.q("SELECT MIN(ts),MAX(ts) FROM link_visits WHERE code=?", (code,), one=True)
        d["first"], d["last"] = r
        d["days"] = self.q("SELECT strftime('%m-%d', ts+10800, 'unixepoch') d, COUNT(*), COUNT(DISTINCT uid) FROM link_visits WHERE code=? GROUP BY d ORDER BY d DESC LIMIT 7", (code,), many=True)
        d["top"] = self.q("""SELECT i.inviter, u.name, u.username, COUNT(*) c FROM invites i LEFT JOIN users u ON u.id=i.inviter
                             WHERE i.code=? GROUP BY i.inviter ORDER BY c DESC LIMIT 5""", (code,), many=True)
        return d

    def visits_page(self, code, limit, offset):
        total = self.q("SELECT COUNT(*) FROM link_visits WHERE code=?", (code,), one=True)[0]
        rows = self.q("""SELECT v.ts, v.uid, u.name, u.username, v.is_new, v.inviter, iu.name, v.locked
                         FROM link_visits v LEFT JOIN users u ON u.id=v.uid LEFT JOIN users iu ON iu.id=v.inviter
                         WHERE v.code=? ORDER BY v.ts DESC LIMIT ? OFFSET ?""", (code, limit, offset), many=True)
        return total, rows
