# bot.py
# المتطلبات: python -m pip install -r requirements.txt
# بيئة Render: TOKEN, ADMIN_USERNAME(بدون @), WEBHOOK_URL, (اختياري WEBHOOK_SECRET, TZ=Asia/Baghdad)

import os, sqlite3, logging, html
from typing import Optional, Tuple, List
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
import uvicorn

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from telegram.error import Forbidden, BadRequest

# ========= إعدادات أساسية =========
TOKEN = os.environ.get("TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("ENV TOKEN مفقود")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()  # بدون @
if not ADMIN_USERNAME:
    raise SystemExit("ENV ADMIN_USERNAME مفقود (بدون @)")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
if not WEBHOOK_URL.startswith("https://"):
    raise SystemExit("ENV WEBHOOK_URL غير مضبوط أو ليس https")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", f"hook{TOKEN.split(':')[0]}")
TZ = os.environ.get("TZ", "Asia/Baghdad")
os.environ["TZ"] = TZ

DEPTS = ["solar", "maintenance", "cameras", "networks"]
DEPT_LABEL = {
    "solar": "🔆 الطاقة الشمسية",
    "maintenance": "🧰 الصيانة",
    "cameras": "📷 الكاميرات",
    "networks": "🌐 الشبكات",
}

# ========= اللوج =========
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("company_bot")

# ========= قاعدة البيانات =========
DB_PATH = os.environ.get("DB_PATH", "tasks.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users(
  chat_id INTEGER PRIMARY KEY,
  full_name TEXT, username TEXT,
  dept TEXT, title TEXT, phone TEXT,
  role TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT
)""")
conn.commit()

def _col_exists(table, col):
    return any(r[1]==col for r in cur.execute(f"PRAGMA table_info({table})"))

def migrate():
    needed = [
        ("dept", "TEXT"),
        ("assignee_chat_id", "INTEGER"),
        ("due_ts", "INTEGER"),
        ("due_text", "TEXT"),
        ("status", "TEXT DEFAULT 'assigned'"),
        ("created_at", "TEXT"),
        ("created_by", "INTEGER"),
        ("ack_ts", "INTEGER"),
        ("ack_by", "INTEGER"),
        ("archived_ts", "INTEGER"),
        ("deleted_ts", "INTEGER"),
        ("reason_text", "TEXT"),
        ("reason_ts", "INTEGER"),
        ("done_ts", "INTEGER"),
    ]
    for col, typ in needed:
        if not _col_exists("tasks", col):
            cur.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
    conn.commit()
migrate()

# ========= أدوات عامّة =========
def ensure_user(u):
    cur.execute("INSERT OR IGNORE INTO users(chat_id,full_name,username) VALUES(?,?,?)",
                (u.id, u.full_name, (u.username or "")))
    role = "admin" if (u.username or "").lower()==ADMIN_USERNAME.lower() else "member"
    cur.execute("UPDATE users SET role=COALESCE(role,?), full_name=?, username=? WHERE chat_id=?",
                (role, u.full_name, (u.username or ""), u.id))
    conn.commit()

def is_registered(uid:int)->bool:
    r = cur.execute("SELECT dept,title FROM users WHERE chat_id=?", (uid,)).fetchone()
    return bool(r and r[0] and r[1])

def is_admin(user)->bool:
    return bool(user and (user.username or "").lower()==ADMIN_USERNAME.lower())

def get_admin_chat_id()->Optional[int]:
    r = cur.execute("SELECT chat_id FROM users WHERE LOWER(username)=LOWER(?)", (ADMIN_USERNAME,)).fetchone()
    return r[0] if r else None

def human(ts:int)->str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M")

def parse_due(s:str)->Optional[int]:
    if not s: return None
    s = s.strip()
    now = datetime.now(timezone.utc)
    try:
        if s.startswith("+") and s[-1].lower() in ("h","d"):
            n = int(s[1:-1])
            dt = now + (timedelta(hours=n) if s[-1].lower()=="h" else timedelta(days=n))
            return int(dt.timestamp())
        s2 = s.replace("/","-")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def esc(s: str) -> str:
    return html.escape(s or "")

# ========= لوحات الأزرار =========
def kb_status(task_id:int)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 تم الاستلام", callback_data=f"ack:{task_id}")],
        [InlineKeyboardButton("🚀 قيد التنفيذ", callback_data=f"st:in_progress:{task_id}"),
         InlineKeyboardButton("🏁 إنهاء المهمة ✅", callback_data=f"st:done:{task_id}")],
        [InlineKeyboardButton("❗️ تعذّر الإكمال", callback_data=f"reason:{task_id}")]
    ])

def admin_menu_kb()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 إضافة مهمة", callback_data="admin:add"),
         InlineKeyboardButton("📋 كل المهام", callback_data="admin:all")],
        [InlineKeyboardButton("⏳ غير المنجزة", callback_data="admin:incomplete"),
         InlineKeyboardButton("✅ المنجزة", callback_data="admin:completed")],
        [InlineKeyboardButton("🔔 تذكير غير المنجزة", callback_data="admin:remind_pending"),
         InlineKeyboardButton("👥 الموظفون", callback_data="admin:users")],
        [InlineKeyboardButton("🛠 إدارة مهمة برقم", callback_data="admin:manage")]
    ])

def dept_buttons()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(DEPT_LABEL["solar"], callback_data="reg:dept:solar")],
        [InlineKeyboardButton(DEPT_LABEL["maintenance"], callback_data="reg:dept:maintenance")],
        [InlineKeyboardButton(DEPT_LABEL["cameras"], callback_data="reg:dept:cameras")],
        [InlineKeyboardButton(DEPT_LABEL["networks"], callback_data="reg:dept:networks")],
    ])

def kb_add_dest()->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 لموظّف (@username/@me)", callback_data="add:dest:user")],
        [InlineKeyboardButton("🏷 لقسم", callback_data="add:dest:dept")],
    ])

def kb_depts(prefix:str)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(DEPT_LABEL[d], callback_data=f"{prefix}:{d}") ] for d in DEPTS]
    )

# ========= إرسال مهمة =========
async def send_task_msg(ctx, chat_id:int, task_id:int, title:str, due_ts:Optional[int], due_text:Optional[str])->Tuple[bool,str|None]:
    when = (due_text.strip() if (due_text and due_text.strip()) else (human(due_ts) if due_ts else "-"))
    txt = (
        f"🎯 مهمة جديدة #{task_id}\n"
        f"• العنوان: {title}\n"
        f"• الموعد: {when}\n\n"
        "رجاءً اضغط: «📥 تم الاستلام» ثم «🚀 قيد التنفيذ» أو «🏁 إنهاء المهمة ✅»."
    )
    try:
        await ctx.bot.send_message(chat_id, txt, reply_markup=kb_status(task_id))
        return True, None
    except Forbidden as e:
        log.warning(f"notify {chat_id} forbidden: {e}"); return False, "forbidden"
    except BadRequest as e:
        log.warning(f"notify {chat_id} badrequest: {e}"); return False, "badrequest"
    except Exception as e:
        log.warning(f"notify {chat_id} other: {e}"); return False, "other"

# ========= أوامر سريعة =========
async def ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def whoami(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)
    row = cur.execute("SELECT dept,title,role FROM users WHERE chat_id=?", (u.id,)).fetchone()
    dept,title,role = (row or (None,None,None))
    await update.message.reply_text(
        f"👤 @{u.username or '-'}\n"
        f"• الاسم: {u.full_name}\n"
        f"• الصلاحية: {role or 'member'}\n"
        f"• القسم: {DEPT_LABEL.get(dept, dept) if dept else '—'}\n"
        f"• المسمّى: {title or '—'}"
    )

async def show_menu(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return await update.message.reply_text("هذه اللوحة للمدير فقط 🙅‍♂️.")
    await update.message.reply_text("لوحة الإدارة:", reply_markup=admin_menu_kb())

# ========= التسجيل =========
async def start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)
    if is_registered(u.id):
        if is_admin(u):
            return await update.message.reply_text("لوحة الإدارة:", reply_markup=admin_menu_kb())
        return await update.message.reply_text("🎉 جاهز!\n• /mytasks — مهامك 👀")
    await update.message.reply_text(
        f"أهلًا {u.full_name}! 😄 خلّينا نكمّل تسجيلك:\n"
        "1) اختر القسم\n2) اكتب المسمّى الوظيفي\n3) (اختياري) رقم الهاتف"
    )
    await update.message.reply_text("اختر قسمك 👇", reply_markup=dept_buttons())

async def on_reg_buttons(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _,_,dept = q.data.split(":")
    cur.execute("UPDATE users SET dept=? WHERE chat_id=?", (dept, q.from_user.id)); conn.commit()
    await q.message.reply_text(f"✅ اخترت: {DEPT_LABEL[dept]}\nاكتب الآن مسمّاك الوظيفي.")
    ctx.user_data["awaiting_title"]=True

async def on_title_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    # لا تعترض تدفق الإضافة أو السبب
    if ctx.user_data.get("add_state") or ctx.user_data.get("awaiting_reason_for"):
        return
    if not ctx.user_data.get("awaiting_title"):
        return
    title=(update.message.text or "").strip()
    if len(title)<2:
        return await update.message.reply_text("اكتب مسمى واضح يا بطل 💪.")
    cur.execute("UPDATE users SET title=? WHERE chat_id=?", (title, update.effective_user.id)); conn.commit()
    ctx.user_data["awaiting_title"]=False
    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 مشاركة رقم الهاتف", request_contact=True)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("تمام ✅ لو تحب، شارك رقمك (اختياري) بالزر أو /skip للتخطي.", reply_markup=kb)
    ctx.user_data["awaiting_phone"]=True

async def on_contact(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_phone"): return
    ph = update.message.contact.phone_number
    cur.execute("UPDATE users SET phone=? WHERE chat_id=?", (ph, update.effective_user.id)); conn.commit()
    ctx.user_data["awaiting_phone"]=False
    await finish_registration(update, ctx)

async def skip_phone(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_phone"):
        ctx.user_data["awaiting_phone"]=False
        await finish_registration(update, ctx)

async def finish_registration(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎫 تم التفعيل! هذه مهامك الآن 👇", reply_markup=ReplyKeyboardRemove())
    await mytasks(update, ctx)

# ========= مهامي =========
async def mytasks(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = cur.execute("""SELECT id,title,status,due_ts,due_text FROM tasks
                          WHERE assignee_chat_id=? AND archived_ts IS NULL AND deleted_ts IS NULL
                            AND status!='done'
                          ORDER BY id ASC""",(uid,)).fetchall()
    if not rows:
        return await update.message.reply_text("لا توجد مهام غير مكتملة 🎉.")
    lines=[]
    for i,t,st,ts,txt in rows:
        when = (txt.strip() if (txt and txt.strip()) else (human(ts) if ts else "-"))
        lines.append(f"#{i} • {t} • {when} • حالة: {st}")
    await update.message.reply_text("🔸 مهامك غير المكتملة:\n" + "\n".join(lines))

# ========= إضافة مهمة =========
async def add_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return await update.message.reply_text("هذا الأمر للمدير فقط 🙅‍♂️.")
    ctx.user_data.clear(); ctx.user_data["add_state"]="title"
    await update.message.reply_text("🎯 عنوان المهمة؟ اكتبها ✍️")

async def add_flow_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("add_state")
    if not st: return
    txt=(update.message.text or "").strip()
    if st=="title":
        if len(txt)<2:
            return await update.message.reply_text("اكتب عنوانًا واضحًا لو سمحت.")
        ctx.user_data["title"]=txt; ctx.user_data["add_state"]="dest"
        return await update.message.reply_text("اختَر وجهة الإسناد:", reply_markup=kb_add_dest())
    if st=="user_wait":
        if txt in ("لي","إلي","الي","إلي","اليّ","اليّ."): txt="@me"
        ctx.user_data["assignee"]=txt; ctx.user_data["add_state"]="due"
        return await update.message.reply_text("🗓 الموعد (مثل +2d أو 2025-10-10 14:00):")
    if st=="due":
        ctx.user_data["due_str"]=txt
        await update.message.reply_text("⏳ جاري إنشاء المهام وإرسال الإشعارات…")
        return await add_finalize(update, ctx)

async def add_flow_buttons(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user):
        return await q.message.reply_text("هذا الأمر للمدير فقط 🙅‍♂️.")
    data = q.data.split(":")
    if data[:2]==["add","dest"]:
        if data[2]=="user":
            ctx.user_data["add_state"]="user_wait"
            return await q.message.reply_text("اكتب @username أو @me أو جزء من اسم الموظف:")
        if data[2]=="dept":
            ctx.user_data["add_state"]="dept"
            return await q.message.reply_text("اختر القسم:", reply_markup=kb_depts("add:dept"))
    if data[:2]==["add","dept"]:
        ctx.user_data["dept"]=data[2]; ctx.user_data["add_state"]="due"
        return await q.message.reply_text("🗓 الموعد (اكتبه نصًا بحرّية):")

async def add_finalize(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    try:
        title   = ctx.user_data.get("title")
        dest_user = ctx.user_data.get("assignee")
        dept    = ctx.user_data.get("dept")
        due_str = ctx.user_data.get("due_str")
        due_ts  = parse_due(due_str)
        if not title:
            return await update.message.reply_text("⚠️ لم أستلم عنوان المهمة. اكتب /add وابدأ من جديد.")
        created_ok: List[int] = []
        failed_list: List[Tuple[int,str]] = []

        if dest_user:  # إلى موظف
            if dest_user.lower()=="@me":
                chat_id = update.effective_user.id
            elif dest_user.startswith("@"):
                r = cur.execute("SELECT chat_id FROM users WHERE LOWER(username)=LOWER(?)", (dest_user[1:].lower(),)).fetchone()
                chat_id = r[0] if r else None
            else:
                r = cur.execute("SELECT chat_id FROM users WHERE full_name LIKE ?", (f"%{dest_user}%",)).fetchone()
                chat_id = r[0] if r else None
            if not chat_id: return await update.message.reply_text("❗️ لم أجد الموظف.")
            cur.execute("""INSERT INTO tasks(title, dept, assignee_chat_id, due_ts, due_text, created_at, created_by)
                           VALUES(?,?,?,?,?,?,?)""",
                        (title, None, chat_id, due_ts, (due_str or ""), datetime.utcnow().isoformat(), update.effective_user.id))
            conn.commit()
            tid = cur.lastrowid
            ok, why = await send_task_msg(ctx, chat_id, tid, title, due_ts, due_str)
            if ok: created_ok.append(tid)
            else: failed_list.append((chat_id, why or "unknown"))
        else:            # إلى قسم
            if not dept: return await update.message.reply_text("❗️ اختر القسم أولًا.")
            members = cur.execute("SELECT chat_id FROM users WHERE dept=?", (dept,)).fetchall()
            if not members: return await update.message.reply_text("القسم بدون موظفين مسجّلين. اطلب منهم إرسال /start.")
            for (chat_id,) in members:
                cur.execute("""INSERT INTO tasks(title, dept, assignee_chat_id, due_ts, due_text, created_at, created_by)
                               VALUES(?,?,?,?,?,?,?)""",
                            (title, dept, chat_id, due_ts, (due_str or ""), datetime.utcnow().isoformat(), update.effective_user.id))
                conn.commit()
                tid = cur.lastrowid
                ok, why = await send_task_msg(ctx, chat_id, tid, title, due_ts, due_str)
                if ok: created_ok.append(tid)
                else: failed_list.append((chat_id, why or "unknown"))

        if failed_list:
            def uname(cid:int)->str:
                r = cur.execute("SELECT username,full_name FROM users WHERE chat_id=?", (cid,)).fetchone()
                if not r: return str(cid)
                u,f = r; return f"@{u}" if u else (f or str(cid))
            names = ", ".join(f"{uname(cid)}({why})" for cid,why in failed_list)
            await update.message.reply_text(f"✅ تم إنشاء {len(created_ok)} مهمة.\n⚠️ تعذّر إشعار: {names}")
        else:
            await update.message.reply_text(f"✅ تم إنشاء {len(created_ok)} مهمة وإشعار الجميع.")
        ctx.user_data.clear()
    except Exception as e:
        log.exception("add_finalize failed")
        await update.message.reply_text(f"❌ خطأ أثناء الإنشاء: {e}")

# ========= أزرار المهمة =========
async def on_ack(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try:
        _, sid = q.data.split(":"); sid=int(sid)
        cur.execute("UPDATE tasks SET ack_ts=?, ack_by=? WHERE id=?",
                    (int(datetime.now(timezone.utc).timestamp()), q.from_user.id, sid))
        conn.commit()
        await q.message.reply_text("📥 تم تسجيل استلامك للمهمة. ألف عافية 👌")
        admin = get_admin_chat_id()
        if admin:
            who = f"@{q.from_user.username}" if q.from_user.username else q.from_user.full_name
            await ctx.bot.send_message(admin, f"🔔 تأكيد: {who} استلم المهمة #{sid} ✅")
    except Exception:
        log.exception("ack failed")

async def on_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try:
        _, status, sid = q.data.split(":"); sid=int(sid)
        if status=="done":
            cur.execute("UPDATE tasks SET status=?, done_ts=? WHERE id=?",
                        ("done", int(datetime.now(timezone.utc).timestamp()), sid))
        else:
            cur.execute("UPDATE tasks SET status=? WHERE id=?", (status, sid))
        conn.commit()
        if status == "done":
            try: await q.message.reply_text(f"🏁 عاشت إيدك يا بطل! تم إنهاء المهمة #{sid} ✅")
            except: pass
            admin=get_admin_chat_id()
            if admin:
                who = f"@{q.from_user.username}" if q.from_user.username else q.from_user.full_name
                try: await ctx.bot.send_message(admin, f"🎉 تم إكمال المهمة بنجاح من {who} — #{sid}")
                except: pass
        else:
            nice = "🚀 بدأت الشغل، موفق!" if status=="in_progress" else "👌 تم التحديث."
            try: await q.message.reply_text(f"{nice} (#{sid})")
            except: pass
            admin=get_admin_chat_id()
            if admin:
                who = f"@{q.from_user.username}" if q.from_user.username else q.from_user.full_name
                try: await ctx.bot.send_message(admin, f"🔔 تحديث حالة #{sid} من {who} → {status}")
                except: pass
        try:
            await q.edit_message_reply_markup(reply_markup=kb_status(sid))
        except BadRequest:
            try:
                await ctx.bot.send_message(q.message.chat.id, f"لوحة التحكم للمهمة #{sid}:", reply_markup=kb_status(sid))
            except: pass
    except Exception:
        log.exception("status failed")
        await q.message.reply_text("⚠️ صار خطأ بسيط أثناء التحديث، جرّب ثانية لو سمحت 🙏")

async def on_reason_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    try:
        _, sid = q.data.split(":"); sid=int(sid)
        ctx.user_data["awaiting_reason_for"]=sid
        await q.message.reply_text("اكتب الآن سبب عدم الاكتمال…")
    except Exception:
        log.exception("reason btn failed")
        await q.message.reply_text("⚠️ صار خطأ، حاول مرة أخرى.")

async def on_reason_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.pop("awaiting_reason_for", None)
    if not sid: return
    reason=(update.message.text or "").strip()
    if len(reason)<3:
        ctx.user_data["awaiting_reason_for"]=sid
        return await update.message.reply_text("اكتب سببًا واضحًا لو تكرّمت.")
    now=int(datetime.now(timezone.utc).timestamp())
    try:
        cur.execute("UPDATE tasks SET reason_text=?, reason_ts=?, status=? WHERE id=?",
                    (reason, now, "late", sid))
        conn.commit()
        await update.message.reply_text("تم تسجيل السبب وإعلام الإدارة. ✅")
        admin=get_admin_chat_id()
        if admin:
            who = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name
            await ctx.bot.send_message(admin, f"📣 سبب عدم إكمال #{sid} من {who}:\n{reason}")
    except Exception:
        log.exception("reason save failed")
        await update.message.reply_text("⚠️ لم أستطع حفظ السبب، حاول ثانية.")

# ========= لوحة المدير (admin:*) =========
async def on_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user):
        return await q.message.reply_text("هذا الأمر للمدير فقط 🙅‍♂️.")

    _, action = q.data.split(":", 1)

    if action == "add":
        ctx.user_data.clear()
        ctx.user_data["add_state"] = "title"
        return await q.message.reply_text("🎯 عنوان المهمة؟ اكتبها ✍️")

    elif action == "all":
        rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,t.assignee_chat_id,t.dept,u.username
                              FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                              WHERE t.archived_ts IS NULL AND t.deleted_ts IS NULL
                              ORDER BY t.id DESC LIMIT 200""").fetchall()
        if not rows:
            return await q.message.reply_text("لا توجد مهام بعد.")
        out=[]
        for i,t,st,dtxt,aid,dept,uname in rows:
            who = f"@{uname}" if uname else (f"dept:{dept}" if dept else "-")
            when = dtxt.strip() if dtxt else "-"
            out.append(f"#{i} • {t} • {who} • {st} • {when}")
        return await q.message.reply_text("📋 كل المهام (أحدث 200)\n" + "\n".join(out))

    elif action == "incomplete":
        rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,t.assignee_chat_id,t.dept,u.username
                              FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                              WHERE t.status!='done' AND t.deleted_ts IS NULL AND t.archived_ts IS NULL
                              ORDER BY t.id ASC LIMIT 200""").fetchall()
        if not rows:
            return await q.message.reply_text("لا توجد مهام غير مكتملة ✅")
        out=[]
        for i,t,st,dtxt,aid,dept,uname in rows:
            who = f"@{uname}" if uname else (f"dept:{dept}" if dept else "-")
            when = dtxt.strip() if dtxt else "-"
            out.append(f"#{i} • {t} • {who} • {st} • {when}")
        return await q.message.reply_text("⏳ غير المنجزة:\n" + "\n".join(out))

    elif action == "completed":
        rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,t.assignee_chat_id,t.dept,u.username
                              FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                              WHERE t.status='done' AND t.deleted_ts IS NULL
                              ORDER BY t.id DESC LIMIT 200""").fetchall()
        if not rows:
            return await q.message.reply_text("لا توجد مهام مكتملة بعد.")
        out=[]
        for i,t,st,dtxt,aid,dept,uname in rows:
            who = f"@{uname}" if uname else (f"dept:{dept}" if dept else "-")
            when = dtxt.strip() if dtxt else "-"
            out.append(f"#{i} • {t} • {who} • {st} • {when}")
        return await q.message.reply_text("✅ المنجزة:\n" + "\n".join(out))

    elif action == "remind_pending":
        rows = cur.execute("""SELECT DISTINCT assignee_chat_id FROM tasks
                              WHERE status!='done' AND deleted_ts IS NULL AND archived_ts IS NULL
                              AND assignee_chat_id IS NOT NULL""").fetchall()
        cnt = 0
        for (cid,) in rows:
            try:
                await ctx.bot.send_message(cid, "🔔 تذكير: لديك مهام غير مكتملة. استخدم /mytasks للاطلاع.")
                cnt += 1
            except Exception:
                pass
        return await q.message.reply_text(f"تم إرسال التذكير إلى {cnt} موظف.")

    elif action == "users":
        rows = cur.execute("""SELECT full_name, username, dept, title FROM users ORDER BY full_name""").fetchall()
        if not rows:
            return await q.message.reply_text("لا يوجد مستخدمون مسجّلون بعد.")
        out=[]
        for f,u,d,t in rows:
            tag = f"@{u}" if u else "-"
            out.append(f"{f} ({tag}) • {DEPT_LABEL.get(d,d)} • {t or '-'}")
        return await q.message.reply_text("👥 الموظفون:\n" + "\n".join(out))

    elif action == "manage":
        return await q.message.reply_text("أرسل رقم المهمة التي تريد إدارتها (ميزة موسّعة لاحقًا).")

    else:
        return await q.message.reply_text("أمر غير معروف من لوحة المدير.")

# ==== أمر مدير نصي ====
async def alltasks(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,t.assignee_chat_id,t.dept,u.username
                          FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                          WHERE t.archived_ts IS NULL AND t.deleted_ts IS NULL
                          ORDER BY t.id DESC LIMIT 200""").fetchall()
    if not rows:
        return await update.message.reply_text("لا توجد مهام بعد.")
    out=[]
    for i,t,st,dtxt,aid,dept,uname in rows:
        who = f"@{uname}" if uname else (f"dept:{dept}" if dept else "-")
        when = dtxt.strip() if dtxt else "-"
        out.append(f"#{i} • {t} • {who} • {st} • {when}")
    await update.message.reply_text("📋 كل المهام (أحدث 200)\n" + "\n".join(out))

# ==== تقرير يومي (اختياري) ====
async def _daily_wrapper(ctx):
    admin = get_admin_chat_id()
    if not admin: return
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    done_cnt = cur.execute("SELECT COUNT(*) FROM tasks WHERE done_ts IS NOT NULL").fetchone()[0]
    pend_cnt = cur.execute("""SELECT COUNT(*) FROM tasks 
                              WHERE status!='done' AND deleted_ts IS NULL AND archived_ts IS NULL""").fetchone()[0]
    await ctx.bot.send_message(admin, f"🗓 تقرير يومي {today}\n✅ المكتملة: {done_cnt}\n⏳ غير المكتملة: {pend_cnt}")

# ========= بناء التطبيق =========
def build_application() -> Application:
    app = Application.builder().token(TOKEN).updater(None).build()  # Updater=None للويبهوك

    # أوامر عامة
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("skip", skip_phone))

    # تسجيل
    app.add_handler(CallbackQueryHandler(on_reg_buttons, pattern=r"^reg:dept:"))
    app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_title_text), group=0)

    # لوحة المدير + إضافة
    app.add_handler(CallbackQueryHandler(on_admin_menu, pattern=r"^admin:"))
    app.add_handler(CommandHandler("add", add_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_flow_text), group=1)
    app.add_handler(CallbackQueryHandler(add_flow_buttons, pattern=r"^add:"))

    # الموظف
    app.add_handler(CommandHandler("mytasks", mytasks))
    app.add_handler(CallbackQueryHandler(on_ack, pattern=r"^ack:\d+$"))
    app.add_handler(CallbackQueryHandler(on_status, pattern=r"^st:"))
    app.add_handler(CallbackQueryHandler(on_reason_button, pattern=r"^reason:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reason_text), group=2)

    # مدير نصي بسيط
    app.add_handler(CommandHandler("alltasks", alltasks))

    # JobQueue
    if app.job_queue:
        app.job_queue.run_daily(
            lambda ctx: ctx.application.create_task(_daily_wrapper(ctx)),
            time=dtime(hour=23, minute=30, tzinfo=ZoneInfo(TZ)),
        )
    else:
        log.warning('JobQueue غير متوفر؛ ثبّت الإضافة: python-telegram-bot[job-queue]==22.5')
    return app

# ========= FastAPI + Webhook =========
application = build_application()
api = FastAPI()

@api.on_event("startup")
async def _on_startup():
    await application.initialize()
    url = f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_SECRET}"
    await application.bot.set_webhook(url)
    log.info(f"Webhook set to: {url}")
    await application.start()

@api.on_event("shutdown")
async def _on_shutdown():
    await application.stop()
    await application.shutdown()

@api.post("/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return {"ok": False}
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"ok": True}

@api.get("/")
def root():
    return {"ok": True, "service": "company_bot", "mode": "webhook"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(api, host="0.0.0.0", port=port)

