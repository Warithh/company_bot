# bot.py
# المتطلبات:  python -m pip install "python-telegram-bot[job-queue]==22.5" tzdata
import os, sqlite3, logging, atexit, math, html, asyncio
from typing import Optional, Tuple, List
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from telegram.error import Forbidden, BadRequest

# ========= قفل منع تعدد النسخ =========
LOCK_PATH = os.path.join(os.path.dirname(__file__), "bot.lock")
if os.path.exists(LOCK_PATH):
    raise SystemExit("🔒 البوت يعمل مسبقًا (bot.lock موجود). احذف الملف إن لم يكن قيد التشغيل.")
open(LOCK_PATH, "w", encoding="utf-8").write(str(os.getpid()))
def _cleanup():
    try:
        if os.path.exists(LOCK_PATH): os.remove(LOCK_PATH)
    except: pass
atexit.register(_cleanup)

# ========= إعدادات =========
TOKEN = os.environ.get("TOKEN", "8300674692:AAHs3T5PQ1glMV4zkb6JZyL-X2Fi53VoCBE")   # يُفضّل وضعه بمتغيّر بيئة
ADMIN_USERNAME = "lof99" 
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0")          # "1" Webhook، "0" Polling
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")               # مثال: https://your-app.onrender.com

DEPTS = ["solar", "maintenance", "cameras", "networks"]
DEPT_LABEL = {
    "solar": "🔆 الطاقة الشمسية",
    "maintenance": "🧰 الصيانة",
    "cameras": "📷 الكاميرات",
    "networks": "🌐 الشبكات",
}

# ========= لوج =========
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("company_bot")
fh = logging.FileHandler("bot.log", encoding="utf-8"); fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(fh)

# ========= قاعدة البيانات =========
conn = sqlite3.connect("tasks.db", check_same_thread=False)
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
    """يضيف الأعمدة الناقصة بدون مسح البيانات."""
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
        ("done_ts", "INTEGER"),     # ← الإصلاح هنا
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
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def parse_due(s:str)->Optional[int]:
    """يدعم +2h / +1d أو تاريخ ISO."""
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

# ========= تنسيق HTML =========
def esc(s: str) -> str:
    return html.escape(s or "")

def fmt_task_block(tid:int, title:str, status:str, due_text:Optional[str], uname:Optional[str]=None, dept:Optional[str]=None) -> str:
    who = (f"@{uname}" if uname else (DEPT_LABEL.get(dept, dept) if dept else "—"))
    when = (due_text.strip() if due_text and due_text.strip() else "—")
    status_ar = {"assigned":"مُسندة","in_progress":"قيد التنفيذ","late":"متأخرة","done":"مكتملة"}.get(status, status)
    return (
        f"<b>#{tid}</b> — {esc(title)}\n"
        f"👤: {esc(who)}\n"
        f"⏰: {esc(when)}\n"
        f"📌: {esc(status_ar)}\n"
        f"<i>————————————</i>"
    )

async def send_html(bot, chat_id:int, text:str, reply_markup=None):
    return await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")

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
         InlineKeyboardButton("📦 الأرشيف", callback_data="admin:archives")],
        [InlineKeyboardButton("👥 الموظفون", callback_data="admin:users"),
         InlineKeyboardButton("🧪 تشخيص سريع", callback_data="admin:diag")],
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

def kb_depts(prefix:str, chat_id:Optional[int]=None)->InlineKeyboardMarkup:
    rows = []
    for d in DEPTS:
        data = f"{prefix}:{d}" if chat_id is None else f"{prefix}:{chat_id}:{d}"
        rows.append([InlineKeyboardButton(DEPT_LABEL[d], callback_data=data)])
    return InlineKeyboardMarkup(rows)

# ========= رسائل المدير =========
async def send_alltasks_msg(ctx, chat_id:int):
    rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,t.assignee_chat_id,t.dept,u.username
                          FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                          WHERE t.archived_ts IS NULL AND t.deleted_ts IS NULL
                          ORDER BY t.id DESC LIMIT 200""").fetchall()
    if not rows:
        return await send_html(ctx.bot, chat_id, "لا توجد مهام بعد.")
    blocks=[fmt_task_block(i,t,st,dtxt,uname,dept) for i,t,st,dtxt,aid,dept,uname in rows]
    await send_html(ctx.bot, chat_id, "📋 <b>كل المهام (أحدث 200)</b>\n" + "\n".join(blocks))

async def send_incomplete_msg(ctx, chat_id:int):
    rows = cur.execute("""SELECT t.id,t.title,t.status,u.username,t.due_text,t.dept
                          FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                          WHERE t.status!='done' AND t.archived_ts IS NULL AND t.deleted_ts IS NULL
                          ORDER BY t.id DESC LIMIT 200""").fetchall()
    if not rows:
        return await send_html(ctx.bot, chat_id, "لا توجد مهام غير منجزة 🎉.")
    blocks=[fmt_task_block(i,t,st,dtxt,uname,dept) for i,t,st,uname,dtxt,dept in rows]
    await send_html(ctx.bot, chat_id, f"⏳ <b>غير المنجزة ({len(rows)})</b>\n" + "\n".join(blocks))

async def send_completed_msg(ctx, chat_id:int):
    rows = cur.execute("""SELECT t.id,t.title,t.status,u.username,t.due_text,t.dept
                          FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                          WHERE t.status='done' AND t.archived_ts IS NULL AND t.deleted_ts IS NULL
                          ORDER BY t.id DESC LIMIT 200""").fetchall()
    if not rows:
        return await send_html(ctx.bot, chat_id, "لا توجد مهام مكتملة بعد.")
    blocks=[fmt_task_block(i,t,st,dtxt,uname,dept) for i,t,st,uname,dtxt,dept in rows]
    await send_html(ctx.bot, chat_id, f"✅ <b>المكتملة ({len(rows)})</b>\n" + "\n".join(blocks))

async def send_archives_msg(ctx, chat_id:int, show_all:bool, user_id:int):
    if show_all:
        rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,u.username,t.dept
                              FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                              WHERE t.archived_ts IS NOT NULL AND t.deleted_ts IS NULL
                              ORDER BY t.archived_ts DESC, t.id DESC LIMIT 200""").fetchall()
    else:
        rows = cur.execute("""SELECT t.id,t.title,t.status,t.due_text,u.username,t.dept
                              FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                              WHERE t.assignee_chat_id=? AND t.archived_ts IS NOT NULL AND t.deleted_ts IS NULL
                              ORDER BY t.archived_ts DESC, t.id DESC LIMIT 200""", (user_id,)).fetchall()
    if not rows:
        return await send_html(ctx.bot, chat_id, "لا يوجد عناصر مؤرشفة.")
    blocks=[fmt_task_block(i,t,st,dtxt,uname,dept) for i,t,st,dtxt,uname,dept in rows]
    title = "📦 <b>الأرشيف (الكل)</b>" if show_all else "📦 <b>أرشيفي</b>"
    await send_html(ctx.bot, chat_id, title + "\n" + "\n".join(blocks))

async def send_diag_msg(ctx, chat_id:int):
    total_users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    per_dept=[f"{DEPT_LABEL[d]}: {cur.execute('SELECT COUNT(*) FROM users WHERE dept=?',(d,)).fetchone()[0]}" for d in DEPTS]
    total_tasks = cur.execute("SELECT COUNT(*) FROM tasks WHERE deleted_ts IS NULL").fetchone()[0]
    pending = cur.execute("""SELECT COUNT(*) FROM tasks
                             WHERE status IN ('assigned','in_progress','late')
                               AND archived_ts IS NULL AND deleted_ts IS NULL""").fetchone()[0]
    await send_html(
        ctx.bot, chat_id,
        "🧪 <b>تشخيص سريع</b>:\n"
        f"• المستخدمون: {total_users}\n"
        f"• الأقسام: {', '.join(per_dept)}\n"
        f"• المهام: {total_tasks} | غير منجزة: {pending}"
    )

# ========= رسائل المهمة للموظف =========
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

# ========= تسجيل/بدء =========
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
    if ctx.user_data.get("add_state"): return
    if not ctx.user_data.get("awaiting_title"): return
    title=(update.message.text or "").strip()
    if len(title)<2: return await update.message.reply_text("اكتب مسمى واضح يا بطل 💪.")
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

# ========= مهامي (للموظف) =========
async def mytasks(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = cur.execute("""SELECT id,title,status,due_ts,due_text FROM tasks
                          WHERE assignee_chat_id=? AND archived_ts IS NULL AND deleted_ts IS NULL
                            AND status!='done'
                          ORDER BY id ASC""",(uid,)).fetchall()
    if not rows:
        return await send_html(ctx.bot, update.effective_chat.id, "لا توجد مهام غير مكتملة 🎉.")
    blocks=[]
    for i,t,st,ts,txt in rows:
        when = (txt.strip() if (txt and txt.strip()) else (human(ts) if ts else "—"))
        blocks.append(fmt_task_block(i, t, st, when))
    await send_html(ctx.bot, update.effective_chat.id, "🔸 <b>مهامك غير المكتملة</b>:\n" + "\n".join(blocks))

# ========= إضافة مهمة (مدير) =========
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
        ctx.user_data["title"]=txt; ctx.user_data["add_state"]="dest"
        return await update.message.reply_text("اختَر وجهة الإسناد:", reply_markup=kb_add_dest())
    if st=="user_wait":
        if txt in ("لي","إلي","الي"): txt="@me"
        ctx.user_data["assignee"]=txt; ctx.user_data["add_state"]="due"
        return await update.message.reply_text("🗓 الموعد (اكتبه نصًا بحرّية):")
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
                r = cur.execute("SELECT chat_id FROM users WHERE username LIKE ?", (dest_user[1:],)).fetchone()
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

# ========= أزرار الموظف =========
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
    q = update.callback_query
    await q.answer()
    try:
        _, status, sid = q.data.split(":"); sid=int(sid)
        if status=="done":
            cur.execute("UPDATE tasks SET status=?, done_ts=? WHERE id=?",
                        ("done", int(datetime.now(timezone.utc).timestamp()), sid))
        else:
            cur.execute("UPDATE tasks SET status=? WHERE id=?", (status, sid))
        conn.commit()

        if status == "done":
            try:
                await q.message.reply_text(f"🏁 عاشت إيدك يا بطل! تم إنهاء المهمة #{sid} ✅")
            except Exception:
                pass
            admin=get_admin_chat_id()
            if admin:
                who = f"@{q.from_user.username}" if q.from_user.username else q.from_user.full_name
                try:
                    await ctx.bot.send_message(admin, f"🎉 تم إكمال المهمة بنجاح يا سيدنا 👑\nمن {who} — #{sid}")
                except Exception:
                    pass
        else:
            nice = "🚀 بدأت الشغل، موفق!" if status=="in_progress" else "👌 تم التحديث."
            try:
                await q.message.reply_text(f"{nice} (#{sid})")
            except Exception:
                pass
            admin=get_admin_chat_id()
            if admin:
                who = f"@{q.from_user.username}" if q.from_user.username else q.from_user.full_name
                try:
                    await ctx.bot.send_message(admin, f"🔔 تحديث حالة #{sid} من {who} → {status}")
                except Exception:
                    pass
        try:
            await q.edit_message_reply_markup(reply_markup=kb_status(sid))
        except BadRequest:
            try:
                await ctx.bot.send_message(q.message.chat.id, f"لوحة التحكم للمهمة #{sid}:", reply_markup=kb_status(sid))
            except Exception:
                pass
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

# ========= أوامر إدارة نصية =========
async def alltasks(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    await send_alltasks_msg(ctx, update.effective_chat.id)

async def remind(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    parts=(update.message.text or "").split()
    if len(parts)<2: return await update.message.reply_text("استعمال: /remind رقم_المهمة")
    try: tid=int(parts[1])
    except: return await update.message.reply_text("رقم مهمة غير صحيح.")
    r = cur.execute("""SELECT title,assignee_chat_id,due_ts,due_text FROM tasks
                       WHERE id=? AND archived_ts IS NULL AND deleted_ts IS NULL""", (tid,)).fetchone()
    if not r: return await update.message.reply_text("لم أجد هذه المهمة.")
    title, aid, ts, txt = r
    ok, why = await send_task_msg(ctx, aid, tid, title, ts, txt)
    await update.message.reply_text("🔔 تم إعادة الإشعار." if ok else f"⚠️ تعذّر الإشعار ({why}).")

async def send_remind_pending_msg(ctx, chat_id:int):
    rows = cur.execute("""SELECT id,title,assignee_chat_id,due_ts,due_text FROM tasks
                          WHERE status IN ('assigned','in_progress','late')
                            AND archived_ts IS NULL AND deleted_ts IS NULL
                          ORDER BY id DESC LIMIT 300""").fetchall()
    if not rows: return await ctx.bot.send_message(chat_id, "لا توجد مهام بحاجة لتذكير 👌")
    sent=0; fail=0
    for i,t,aid,ts,txt in rows:
        ok,_ = await send_task_msg(ctx, aid, i, t, ts, txt)
        sent += 1 if ok else 0
        fail += 0 if ok else 1
    msg = f"🔔 تمت إعادة إشعار {sent} مهمة."
    if fail: msg += f"\n⚠️ فشل تذكير {fail} (المستخدم لم يبدأ البوت أو قام بحظره)."
    await ctx.bot.send_message(chat_id, msg)

async def remind_pending(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    await send_remind_pending_msg(ctx, update.effective_chat.id)

def _is_owner_or_admin(user, task_id:int)->bool:
    if is_admin(user): return True
    r=cur.execute("SELECT assignee_chat_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(r and r[0]==user.id)

async def archive_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    if len(parts)<2: return await update.message.reply_text("استعمال: /archive رقم_المهمة")
    try: tid=int(parts[1])
    except: return await update.message.reply_text("رقم مهمة غير صحيح.")
    if not _is_owner_or_admin(update.effective_user, tid):
        return await update.message.reply_text("ليس لديك صلاحية على هذه المهمة.")
    now=int(datetime.now(timezone.utc).timestamp())
    cur.execute("UPDATE tasks SET archived_ts=? WHERE id=? AND deleted_ts IS NULL", (now, tid))
    conn.commit()
    await update.message.reply_text("📦 تم الأرشفة." if cur.rowcount else "لم يتم الأرشفة.")

async def unarchive_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    if len(parts)<2: return await update.message.reply_text("استعمال: /unarchive رقم_المهمة")
    try: tid=int(parts[1])
    except: return await update.message.reply_text("رقم مهمة غير صحيح.")
    if not _is_owner_or_admin(update.effective_user, tid):
        return await update.message.reply_text("ليس لديك صلاحية على هذه المهمة.")
    cur.execute("UPDATE tasks SET archived_ts=NULL WHERE id=?", (tid,))
    conn.commit()
    await update.message.reply_text("📦 أُزيلت الأرشفة." if cur.rowcount else "لم تتغير الحالة.")

async def del_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    if len(parts)<2: return await update.message.reply_text("استعمال: /del رقم_المهمة")
    try: tid=int(parts[1])
    except: return await update.message.reply_text("رقم مهمة غير صحيح.")
    if not _is_owner_or_admin(update.effective_user, tid):
        return await update.message.reply_text("ليس لديك صلاحية على هذه المهمة.")
    now=int(datetime.now(timezone.utc).timestamp())
    cur.execute("SELECT deleted_ts FROM tasks WHERE id=?", (tid,))
    row = cur.fetchone()
    if not row:
        return await update.message.reply_text("المهمة غير موجودة.")
    if row[0]:
        return await update.message.reply_text("المهمة محذوفة بالفعل.")
    cur.execute("UPDATE tasks SET deleted_ts=? WHERE id=?", (now, tid))
    conn.commit()
    await update.message.reply_text("🗑️ حذف ناعم تم.")

async def restore_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    if len(parts)<2: return await update.message.reply_text("استعمال: /restore رقم_المهمة")
    try: tid=int(parts[1])
    except: return await update.message.reply_text("رقم مهمة غير صحيح.")
    if not _is_owner_or_admin(update.effective_user, tid):
        return await update.message.reply_text("ليس لديك صلاحية على هذه المهمة.")
    cur.execute("UPDATE tasks SET deleted_ts=NULL WHERE id=?", (tid,))
    conn.commit()
    await update.message.reply_text("♻️ تم الاسترجاع." if cur.rowcount else "المهمة ليست محذوفة.")

async def archives_cmd(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    show_all = len(parts)>1 and parts[1].lower()=="all"
    if show_all and not is_admin(update.effective_user):
        return await update.message.reply_text("فقط المدير يستطيع عرض أرشيف الكل.")
    await send_archives_msg(ctx, update.effective_chat.id, show_all, update.effective_user.id)

# ========= إدارة الموظفين =========
USERS_PAGE_SIZE = 10

def users_menu_kb(page:int)->InlineKeyboardMarkup:
    rows = cur.execute("""SELECT chat_id, full_name, username, dept, title
                          FROM users ORDER BY COALESCE(dept,''), COALESCE(full_name,'')""").fetchall()
    total = len(rows)
    pages = max(1, math.ceil(total / USERS_PAGE_SIZE))
    page = max(0, min(page, pages-1))
    start = page * USERS_PAGE_SIZE
    items = rows[start:start+USERS_PAGE_SIZE]

    kb=[]
    for (cid, name, uname, dept, title) in items:
        label = f"{name or 'بدون اسم'} • @{uname or '-'} • {DEPT_LABEL.get(dept or '', dept or '—')}"
        kb.append([InlineKeyboardButton(label[:60], callback_data=f"users:open:{cid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("« السابق", callback_data=f"users:page:{page-1}"))
    if page<pages-1: nav.append(InlineKeyboardButton("التالي »", callback_data=f"users:page:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("⤴️ رجوع للوحة", callback_data="admin:back")])
    return InlineKeyboardMarkup(kb)

def user_manage_kb(target_cid:int)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 تغيير القسم", callback_data=f"users:setdept:{target_cid}")],
        [InlineKeyboardButton("📝 تغيير المسمّى", callback_data=f"users:settitle:{target_cid}")],
        [InlineKeyboardButton("⭐️ تغيير الصلاحية", callback_data=f"users:setrole:{target_cid}")],
        [InlineKeyboardButton("🧾 مهامه", callback_data=f"users:tasks:{target_cid}:0")],
        [InlineKeyboardButton("🔁 إعادة إسناد كل مهامه", callback_data=f"users:reassignall:{target_cid}:0")],
        [InlineKeyboardButton("🗑 حذف الموظف", callback_data=f"users:del:{target_cid}")],
        [InlineKeyboardButton("⤴️ رجوع للقائمة", callback_data="admin:users")]
    ])

async def list_users(update_or_ctx, ctx:ContextTypes.DEFAULT_TYPE, page:int=0):
    chat_id = update_or_ctx.effective_chat.id if hasattr(update_or_ctx, "effective_chat") else update_or_ctx.callback_query.message.chat.id
    await ctx.bot.send_message(chat_id, "👥 الموظفون — اختر موظفًا للإدارة:", reply_markup=users_menu_kb(page))

async def users_press(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user): return await q.message.reply_text("للمدير فقط.")
    data = q.data.split(":")
    if data[1]=="page":
        page=int(data[2]); return await q.message.edit_text("👥 الموظفون — اختر موظفًا للإدارة:", reply_markup=users_menu_kb(page))
    if data[1]=="open":
        cid=int(data[2])
        u = cur.execute("SELECT full_name, username, dept, title, role FROM users WHERE chat_id=?", (cid,)).fetchone()
        if not u: return await q.message.reply_text("الموظف غير موجود.")
        name, uname, dept, title, role = u
        text = (f"👤 {name or '-'} (@{uname or '-'})\n"
                f"• القسم: {DEPT_LABEL.get(dept or '', dept or '—')}\n"
                f"• المسمّى: {title or '—'}\n"
                f"• الصلاحية: {role or 'member'}")
        return await q.message.reply_text(text, reply_markup=user_manage_kb(cid))
    if data[1]=="setdept":
        cid=int(data[2])
        return await q.message.reply_text("اختر القسم الجديد:", reply_markup=kb_depts("users:setdeptchoose", cid))
    if data[1]=="setdeptchoose":
        cid=int(data[2]); dept=data[3]
        cur.execute("UPDATE users SET dept=? WHERE chat_id=?", (dept, cid)); conn.commit()
        return await q.message.reply_text("✅ تم تغيير القسم.")
    if data[1]=="settitle":
        cid=int(data[2]); ctx.user_data["await_title_for_user"]=cid
        return await q.message.reply_text("اكتب المسمّى الوظيفي الجديد:")
    if data[1]=="setrole":
        cid=int(data[2])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("عضو (member)", callback_data=f"users:setrolechoose:{cid}:member")],
            [InlineKeyboardButton("مدير (admin)", callback_data=f"users:setrolechoose:{cid}:admin")]
        ])
        return await q.message.reply_text("اختر الصلاحية:", reply_markup=kb)
    if data[1]=="setrolechoose":
        cid=int(data[2]); role=data[3]
        cur.execute("UPDATE users SET role=? WHERE chat_id=?", (role, cid)); conn.commit()
        return await q.message.reply_text("✅ تم تغيير الصلاحية.")
    if data[1]=="del":
        cid=int(data[2])
        cur.execute("DELETE FROM users WHERE chat_id=?", (cid,)); conn.commit()
        return await q.message.reply_text("🗑 تم حذف الموظف من السجل.")
    if data[1]=="tasks":
        target=int(data[2]); page=int(data[3])
        await show_user_tasks(q, ctx, target, page)
    if data[1]=="deltask":
        tid=int(data[2]); now=int(datetime.now(timezone.utc).timestamp())
        cur.execute("UPDATE tasks SET deleted_ts=? WHERE id=?", (now, tid)); conn.commit()
        return await q.message.reply_text(f"🗑 حُذفت المهمة #{tid} (حذف ناعم).")
    if data[1]=="reassign":
        tid=int(data[2]); page=int(data[3])
        await show_users_pick_target(q, ctx, tid, page)
    if data[1]=="pickto":
        tid=int(data[2]); to_cid=int(data[3])
        cur.execute("UPDATE tasks SET assignee_chat_id=? WHERE id=?", (to_cid, tid)); conn.commit()
        return await q.message.reply_text(f"🔁 أُعيد إسناد #{tid} بنجاح.")
    if data[1]=="reassignall":
        from_cid=int(data[2]); page=int(data[3])
        await show_users_pick_target(q, ctx, None, page, from_cid=from_cid)
    if data[1]=="pickallto":
        from_cid=int(data[2]); to_cid=int(data[3])
        cur.execute("UPDATE tasks SET assignee_chat_id=? WHERE assignee_chat_id=? AND deleted_ts IS NULL", (to_cid, from_cid)); conn.commit()
        return await q.message.reply_text(f"🔁 أُعيد إسناد كل مهام الموظف بنجاح.")
    if data[1]=="back":
        return await q.message.reply_text("لوحة الإدارة:", reply_markup=admin_menu_kb())

async def show_user_tasks(q, ctx, target_cid:int, page:int):
    rows = cur.execute("""SELECT id,title,status,due_text
                          FROM tasks WHERE assignee_chat_id=? AND deleted_ts IS NULL
                          ORDER BY id DESC""", (target_cid,)).fetchall()
    total=len(rows)
    if not total: return await q.message.reply_text("لا توجد مهام لهذا الموظف.")
    pages = max(1, math.ceil(total/USERS_PAGE_SIZE))
    page=max(0, min(page, pages-1))
    start=page*USERS_PAGE_SIZE
    items=rows[start:start+USERS_PAGE_SIZE]
    lines=[f"🧾 مهام الموظف (صفحة {page+1}/{pages})"]
    kb=[]
    for (tid,title,st,dtxt) in items:
        when = dtxt.strip() if dtxt else "-"
        lines.append(f"#{tid} • {title} • {st} • {when}")
        kb.append([InlineKeyboardButton(f"🗑 حذف #{tid}", callback_data=f"users:deltask:{tid}"),
                   InlineKeyboardButton(f"🔁 إعادة إسناد #{tid}", callback_data=f"users:reassign:{tid}:0")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("« السابق", callback_data=f"users:tasks:{target_cid}:{page-1}"))
    if page<pages-1: nav.append(InlineKeyboardButton("التالي »", callback_data=f"users:tasks:{target_cid}:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("⤴️ رجوع لإدارة الموظف", callback_data=f"users:open:{target_cid}")])
    await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def show_users_pick_target(q, ctx, tid:Optional[int], page:int, from_cid:Optional[int]=None):
    rows = cur.execute("""SELECT chat_id, full_name, username FROM users ORDER BY COALESCE(full_name,'')""").fetchall()
    if from_cid:
        rows = [r for r in rows if r[0] != from_cid]
    total=len(rows); pages=max(1, math.ceil(total/USERS_PAGE_SIZE))
    page=max(0, min(page, pages-1))
    start=page*USERS_PAGE_SIZE
    items=rows[start:start+USERS_PAGE_SIZE]
    kb=[]
    for (cid,name,uname) in items:
        label = f"{name or 'بدون اسم'} (@{uname or '-'})"
        if tid is not None:
            cb = f"users:pickto:{tid}:{cid}"
        else:
            cb = f"users:pickallto:{from_cid}:{cid}"
        kb.append([InlineKeyboardButton(label[:60], callback_data=cb)])
    nav=[]
    if page>0:
        cb = (f"users:reassign:{tid}:{page-1}" if tid is not None else f"users:reassignall:{from_cid}:{page-1}")
        nav.append(InlineKeyboardButton("« السابق", callback_data=cb))
    if page<pages-1:
        cb = (f"users:reassign:{tid}:{page+1}" if tid is not None else f"users:reassignall:{from_cid}:{page+1}")
        nav.append(InlineKeyboardButton("التالي »", callback_data=cb))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("⤴️ رجوع", callback_data="admin:users")])
    await q.message.reply_text("اختر الموظف الهدف لإعادة الإسناد:", reply_markup=InlineKeyboardMarkup(kb))

async def on_admin_title_input(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    target = ctx.user_data.pop("await_title_for_user", None)
    if not target: return
    if not is_admin(update.effective_user): return
    new_title = (update.message.text or "").strip()
    if len(new_title)<2:
        ctx.user_data["await_title_for_user"]=target
        return await update.message.reply_text("اكتب مسمّى أطول لو تكرّمت.")
    cur.execute("UPDATE users SET title=? WHERE chat_id=?", (new_title, target)); conn.commit()
    await update.message.reply_text("✅ تم تحديث المسمّى.")

# ========= إدارة مهمة برقم =========
async def perform_task_admin_action(ctx, chat_id: int, user, action: str, tid: int):
    if not _is_owner_or_admin(user, tid):
        return await ctx.bot.send_message(chat_id, "ليس لديك صلاحية على هذه المهمة.")
    now = int(datetime.now(timezone.utc).timestamp())
    if action == "remind":
        r = cur.execute("""SELECT title,assignee_chat_id,due_ts,due_text
                           FROM tasks WHERE id=? AND archived_ts IS NULL AND deleted_ts IS NULL""", (tid,)).fetchone()
        if not r:
            return await ctx.bot.send_message(chat_id, "لم أجد هذه المهمة (قد تكون محذوفة/مؤرشفة).")
        title, aid, ts, txt = r
        ok, why = await send_task_msg(ctx, aid, tid, title, ts, txt)
        return await ctx.bot.send_message(chat_id, "🔔 تم إعادة الإشعار." if ok else f"⚠️ تعذّر الإشعار ({why}).")
    elif action == "archive":
        cur.execute("UPDATE tasks SET archived_ts=? WHERE id=? AND deleted_ts IS NULL", (now, tid)); conn.commit()
        return await ctx.bot.send_message(chat_id, "📦 تم الأرشفة." if cur.rowcount else "لم يتم الأرشفة (قد تكون محذوفة).")
    elif action == "unarchive":
        cur.execute("UPDATE tasks SET archived_ts=NULL WHERE id=?", (tid,)); conn.commit()
        return await ctx.bot.send_message(chat_id, "📦 أُزيلت الأرشفة." if cur.rowcount else "لم تتغير الحالة.")
    elif action == "del":
        cur.execute("SELECT deleted_ts FROM tasks WHERE id=?", (tid,)); row = cur.fetchone()
        if not row: return await ctx.bot.send_message(chat_id, "المهمة غير موجودة.")
        if row[0]: return await ctx.bot.send_message(chat_id, "المهمة محذوفة بالفعل.")
        cur.execute("UPDATE tasks SET deleted_ts=? WHERE id=?", (now, tid)); conn.commit()
        return await ctx.bot.send_message(chat_id, "🗑️ حذف ناعم تم.")
    elif action == "restore":
        cur.execute("UPDATE tasks SET deleted_ts=NULL WHERE id=?", (tid,)); conn.commit()
        return await ctx.bot.send_message(chat_id, "♻️ تم الاسترجاع." if cur.rowcount else "المهمة ليست محذوفة.")
    else:
        return await ctx.bot.send_message(chat_id, "إجراء غير معروف.")

async def on_manage_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return
    if not ctx.user_data.pop("awaiting_manage_id", False): return
    try:
        tid = int((update.message.text or "").strip())
    except:
        ctx.user_data["awaiting_manage_id"]=True
        return await update.message.reply_text("أدخل رقم صحيح.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 تذكير", callback_data=f"manage:remind:{tid}"),
         InlineKeyboardButton("📦 أرشفة", callback_data=f"manage:archive:{tid}")],
        [InlineKeyboardButton("📦 إلغاء الأرشفة", callback_data=f"manage:unarchive:{tid}")],
        [InlineKeyboardButton("🗑 حذف ناعم", callback_data=f"manage:del:{tid}"),
         InlineKeyboardButton("♻️ استرجاع", callback_data=f"manage:restore:{tid}")]
    ])
    await update.message.reply_text(f"إدارة المهمة #{tid}:", reply_markup=kb)

async def on_manage_press(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user): return
    try:
        _, action, sid = q.data.split(":")
        tid = int(sid)
    except Exception:
        return await q.message.reply_text("بيانات غير صالحة.")
    return await perform_task_admin_action(ctx, q.message.chat.id, q.from_user, action, tid)

# ========= لوحة المدير =========
async def admin_menu(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return await update.message.reply_text("هذه اللوحة للمدير فقط 🙅‍♂️.")
    await update.message.reply_text("لوحة الإدارة:", reply_markup=admin_menu_kb())

async def on_admin_menu_press(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user):
        return await q.message.reply_text("هذه اللوحة للمدير فقط 🙅‍♂️.")
    chat_id = q.message.chat.id
    _, action = q.data.split(":", 1)

    if action == "add":
        ctx.user_data.clear(); ctx.user_data["add_state"]="title"
        return await q.message.reply_text("🎯 عنوان المهمة؟ اكتبها ✍️")
    if action == "all":
        return await send_alltasks_msg(ctx, chat_id)
    if action == "incomplete":
        return await send_incomplete_msg(ctx, chat_id)
    if action == "completed":
        return await send_completed_msg(ctx, chat_id)
    if action == "remind_pending":
        return await send_remind_pending_msg(ctx, chat_id)
    if action == "archives":
        return await q.message.reply_text(
            "اختر:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("أرشيفي", callback_data="admin:archives:self"),
                 InlineKeyboardButton("كل الأرشيف", callback_data="admin:archives:all")]
            ])
        )
    if action.startswith("archives:"):
        show_all = action.endswith(":all")
        return await send_archives_msg(ctx, chat_id, show_all, q.from_user.id)
    if action == "users":
        return await list_users(update, ctx, page=0)
    if action == "diag":
        return await send_diag_msg(ctx, chat_id)
    if action == "manage":
        ctx.user_data["awaiting_manage_id"]=True
        return await q.message.reply_text("أرسل رقم المهمة لإدارتها (تذكير/أرشفة/حذف/استرجاع).")
    if action == "back":
        return await q.message.reply_text("لوحة الإدارة:", reply_markup=admin_menu_kb())

# ========= تقرير يومي =========
async def send_daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    admin = get_admin_chat_id()
    if not admin: return
    today = datetime.now(ZoneInfo("Asia/Baghdad")).date()

    done_rows = cur.execute("""SELECT t.id, t.title, u.username
                               FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                               WHERE t.done_ts IS NOT NULL
                                 AND DATE(t.done_ts, 'unixepoch') = ?
                                 AND t.deleted_ts IS NULL""", (today.isoformat(),)).fetchall()

    pending_rows = cur.execute("""SELECT t.id, t.title, u.username, t.status
                                  FROM tasks t LEFT JOIN users u ON u.chat_id=t.assignee_chat_id
                                  WHERE t.deleted_ts IS NULL AND t.archived_ts IS NULL
                                    AND t.status != 'done'
                                    AND DATE(strftime('%s', t.created_at), 'unixepoch') = ?
                               """,(today.isoformat(),)).fetchall()

    def by_user(rows, with_status=False):
        d = {}
        for r in rows:
            if with_status:
                i, t, u, st = r
            else:
                i, t, u = r; st=None
            name = f"@{u}" if u else "بدون اسم"
            d.setdefault(name, []).append((i, t, st))
        return d

    d_done = by_user(done_rows)
    d_pending = by_user(pending_rows, with_status=True)

    lines = [f"🗓 تقرير يومي — {today} (بتوقيت بغداد)"]
    lines.append("\n✅ المكتمل:")
    if not d_done:
        lines.append("• لا شيء")
    else:
        for user, items in d_done.items():
            lines.append(f"• {user}:")
            for i, t, _ in items:
                lines.append(f"   - #{i} {t}")

    lines.append("\n⏳ غير المكتمل (المُنشأ اليوم):")
    if not d_pending:
        lines.append("• لا شيء")
    else:
        for user, items in d_pending.items():
            lines.append(f"• {user}:")
            for i, t, st in items:
                lines.append(f"   - #{i} {t} • حالة: {st}")

    await ctx.bot.send_message(admin, "\n".join(lines))

# ========= هاندلر أخطاء عام =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)

# ========= main =========
def main():
    app = Application.builder().token(TOKEN).build()

    # أخطاء
    app.add_error_handler(on_error)

    # تسجيل/بدء
    app.add_handler(CommandHandler("start", start), group=0)
    app.add_handler(CommandHandler("skip", skip_phone), group=0)
    app.add_handler(CallbackQueryHandler(on_reg_buttons, pattern=r"^reg:dept:"), group=0)
    app.add_handler(MessageHandler(filters.CONTACT, on_contact), group=0)

    # لوحة المدير
    app.add_handler(CommandHandler("menu", admin_menu), group=0)
    app.add_handler(CallbackQueryHandler(on_admin_menu_press, pattern=r"^admin:"), group=0)

    # إدارة الموظفين
    app.add_handler(CallbackQueryHandler(users_press, pattern=r"^users:"), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_title_input), group=0)

    # الإضافة (مدير)
    app.add_handler(CommandHandler("add", add_start), group=1)
    app.add_handler(CallbackQueryHandler(add_flow_buttons, pattern=r"^add:"), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_flow_text), group=1)

    # الموظف: عرض/تفاعل
    app.add_handler(CommandHandler("mytasks", mytasks), group=2)
    app.add_handler(CallbackQueryHandler(on_ack, pattern=r"^ack:\d+$"), group=2)
    app.add_handler(CallbackQueryHandler(on_status, pattern=r"^st:"), group=2)
    app.add_handler(CallbackQueryHandler(on_reason_button, pattern=r"^reason:\d+$"), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_reason_text), group=2)

    # إدارة مهام بالأوامر
    app.add_handler(CommandHandler("alltasks", alltasks), group=3)
    app.add_handler(CommandHandler("remind_pending", remind_pending), group=3)
    app.add_handler(CommandHandler("remind", remind), group=3)
    app.add_handler(CommandHandler("archive", archive_cmd), group=3)
    app.add_handler(CommandHandler("unarchive", unarchive_cmd), group=3)
    app.add_handler(CommandHandler("del", del_cmd), group=3)
    app.add_handler(CommandHandler("restore", restore_cmd), group=3)
    app.add_handler(CommandHandler("archives", archives_cmd), group=3)

    # إدارة مهمة برقم (أزرار)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_manage_text), group=4)
    app.add_handler(CallbackQueryHandler(on_manage_press, pattern=r"^manage:"), group=4)

    # تقرير يومي 23:30 بغداد
    jq = app.job_queue
    if jq:
        jq.run_daily(send_daily_summary, time=dtime(hour=23, minute=30, tzinfo=ZoneInfo("Asia/Baghdad")))
    else:
        log.warning('JobQueue غير متوفر؛ ثبّت الإضافة: python -m pip install "python-telegram-bot[job-queue]==22.5"')

    # تشغيل (Polling أو Webhook)
    if USE_WEBHOOK == "1":
        if not WEBHOOK_URL:
            raise SystemExit("يرجى ضبط WEBHOOK_URL برابط تطبيقك (https://your-app.onrender.com).")
        async def _run():
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.bot.set_webhook(url=WEBHOOK_URL)
            port = int(os.environ.get("PORT", "10000"))
            await app.run_webhook(listen="0.0.0.0", port=port, url_path="", webhook_url=WEBHOOK_URL)
        asyncio.run(_run())
    else:
        app.run_polling(drop_pending_updates=True, allowed_updates=None)

if __name__ == "__main__":
    main()
