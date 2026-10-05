# ProAI Telegram Bot — AI chat + Rose-style group management
# Env: BOT_TOKEN, AI_API_KEY (required) | DATABASE_URL (recommended, permanent storage)
# Optional: AI_BASE_URL, AI_MODEL, MAX_HISTORY, MAX_TOKENS, WEBHOOK_URL

import os
import re
import json
import html
import logging
import datetime
from functools import wraps
from collections import defaultdict, deque

from openai import AsyncOpenAI
from telegram import Update, BotCommand, ChatPermissions
from telegram.constants import ChatAction, ParseMode
import xiaomi
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proai")

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_MODEL = os.environ.get("AI_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "55"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "5000"))

ai = AsyncOpenAI(
    api_key=os.environ["AI_API_KEY"],
    base_url=os.environ.get("AI_BASE_URL", "https://integrate.api.nvidia.com/v1"),
)

# ───────────────────────── Storage (Postgres or SQLite) ─────────────────────────
DB_URL = os.environ.get("DATABASE_URL")
if DB_URL:
    import psycopg
    PH = "%s"

    def _conn():
        return psycopg.connect(DB_URL)
else:
    import sqlite3
    PH = "?"

    def _conn():
        return sqlite3.connect("proai.db")
    log.warning("DATABASE_URL nahi mila: SQLite use ho raha hai (Render free pe restart me data ud jayega)")


def _run(sql, params=(), fetch=False):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql.replace("?", PH), params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


_run("CREATE TABLE IF NOT EXISTS kv (chat BIGINT, k TEXT, v TEXT, PRIMARY KEY (chat, k))")


def db_get(chat, key, default=None):
    rows = _run("SELECT v FROM kv WHERE chat=? AND k=?", (chat, key), fetch=True)
    return json.loads(rows[0][0]) if rows else default


def db_set(chat, key, value):
    _run("INSERT INTO kv (chat, k, v) VALUES (?,?,?) "
         "ON CONFLICT (chat, k) DO UPDATE SET v=excluded.v",
         (chat, key, json.dumps(value)))


def db_del(chat, key):
    _run("DELETE FROM kv WHERE chat=? AND k=?", (chat, key))


def db_keys(chat, prefix):
    rows = _run("SELECT k FROM kv WHERE chat=? AND k LIKE ?", (chat, prefix + "%"), fetch=True)
    return sorted(r[0][len(prefix):] for r in rows)


# ───────────────────────── AI personality ─────────────────────────
def build_system_prompt() -> str:
    today = datetime.date.today().strftime("%d %B %Y")
    return (
        "Tum 'ProAI' ho, ek smart aur stylish AI assistant jo Telegram pe "
        "(@PraKrutim_bot) rehta hai. xiaomi ya kisi bhi device me madad kar sakta hai.: "
        "clean, premium aur smooth. "
        f"Aaj ki date hai {today}. "
        "Hamesha premium, stylish aur energetic Hinglish me reply do. "
        "Replies 3-4 lines me rakho jab tak user detail na maange. "
        "Emojis thode aur sahi jagah use karo. "
        "Code ya technical sawaal me seedha aur accurate jawab do."
    )


history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

HELP = """<b>ProAI ✨</b>
DM me kuch bhi poocho. Group me mujhe @mention karo ya mere message pe reply karo.

<b>Basic</b>
/id /info /ping /reset /admins

<b>Admin</b> (reply ya user ID)
/ban /unban /kick /kickme /mute /unmute
/tban /tmute (time: 30m, 2h, 1d)
/warn /unwarn /resetwarns /warns /setwarnlimit
/pin /unpin /del /purge

<b>Welcome &amp; Rules</b>
/setwelcome /welcome on|off /resetwelcome
/setrules /rules /clearrules
Welcome vars: {first} {mention} {chatname}

<b>Notes &amp; Filters</b>
/save /get /notes /clear (ya #notename)
/filter /filters /stop

<b>Xiaomi Geeks</b> (codename do, jaise /twrp whyred)
/recovery /fastboot /latest /archive
/firmware /vendor /eu /twrp /pb /of
/specs /models /whatis /codename
/set_codename (default device) /unlockbl /tools /guides"""


# ───────────────────────── Helpers ─────────────────────────
def group_admin(perm=None):
    """Sirf group admins (optional specific permission) ke liye."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat, msg = update.effective_chat, update.effective_message
            if chat.type == "private":
                return await msg.reply_text("Ye command sirf groups me chalta hai 👥")
            m = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
            ok = m.status == "creator" or (
                m.status == "administrator" and (perm is None or getattr(m, perm, False)))
            if not ok:
                return await msg.reply_text("Ye sirf admins ke liye hai 🚫")
            try:
                return await fn(update, ctx)
            except Exception as e:
                log.exception("command error")
                return await msg.reply_text(f"⚠️ Fail: {html.escape(str(e))}\n(Bot ko admin banaya hai?)")
        return wrapper
    return deco


async def get_target(update, ctx):
    msg = update.effective_message
    args = list(ctx.args or [])
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.mention_html(), args
    if args and args[0].lstrip("-").isdigit():
        uid = int(args.pop(0))
        return uid, f"<code>{uid}</code>", args
    await msg.reply_text("Kisi ke message pe reply karo ya user ID do 🙏")
    return None, None, args


async def protected(ctx, chat_id, uid) -> bool:
    if uid == ctx.bot.id:
        return True
    try:
        m = await ctx.bot.get_chat_member(chat_id, uid)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


def parse_duration(text):
    m = re.fullmatch(r"(\d+)([smhdw])", (text or "").lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return datetime.timedelta(seconds=n * secs)


def until(delta):
    return datetime.datetime.now(datetime.timezone.utc) + delta


def text_after_command(msg):
    parts = (msg.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)
MUTED = ChatPermissions(can_send_messages=False)


# ───────────────────────── Basic commands ─────────────────────────
async def start(update, ctx):
    await update.message.reply_text(
        "Hey! Main ProAI hu ✨ Kuch bhi poocho.\nSaare commands: /help")


async def help_cmd(update, ctx):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def reset(update, ctx):
    history[update.effective_chat.id].clear()
    await update.message.reply_text("Chat reset ho gayi ✅")


async def ping(update, ctx):
    await update.message.reply_text("Pong! 🏓 Zinda hu bhai ⚡")


async def id_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    lines = [f"👤 Your ID: <code>{update.effective_user.id}</code>",
             f"💬 Chat ID: <code>{chat.id}</code>"]
    if msg.reply_to_message and msg.reply_to_message.from_user:
        lines.append(f"↩️ Replied user ID: <code>{msg.reply_to_message.from_user.id}</code>")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def info(update, ctx):
    msg = update.effective_message
    u = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    uname = f"@{u.username}" if u.username else "none"
    await msg.reply_text(
        f"<b>User info</b>\nID: <code>{u.id}</code>\nName: {html.escape(u.full_name)}\n"
        f"Username: {html.escape(uname)}\nBot: {u.is_bot}", parse_mode=ParseMode.HTML)


async def admins(update, ctx):
    chat = update.effective_chat
    if chat.type == "private":
        return await update.message.reply_text("Ye group command hai 👥")
    lst = await ctx.bot.get_chat_administrators(chat.id)
    names = [("👑 " if a.status == "creator" else "🛡 ") + html.escape(a.user.full_name)
             for a in lst if not a.user.is_bot]
    await update.message.reply_text("<b>Admins</b>\n" + "\n".join(names), parse_mode=ParseMode.HTML)


# ───────────────────────── Moderation ─────────────────────────
@group_admin("can_restrict_members")
async def ban(update, ctx):
    uid, name, rest = await get_target(update, ctx)
    if uid is None:
        return
    chat = update.effective_chat
    if await protected(ctx, chat.id, uid):
        return await update.message.reply_text("Admin ya bot ko ban nahi kar sakta 😅")
    delta = parse_duration(rest[0]) if rest else None
    await ctx.bot.ban_chat_member(chat.id, uid, until_date=until(delta) if delta else None)
    await update.message.reply_text(
        f"🔨 {name} ban ho gaya" + (f" ({rest[0]} ke liye)" if delta else "") + "!",
        parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def unban(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    await ctx.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
    await update.message.reply_text(f"✅ {name} unban ho gaya", parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def kick(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    chat = update.effective_chat
    if await protected(ctx, chat.id, uid):
        return await update.message.reply_text("Admin ya bot ko kick nahi kar sakta 😅")
    await ctx.bot.ban_chat_member(chat.id, uid)
    await ctx.bot.unban_chat_member(chat.id, uid)
    await update.message.reply_text(f"👢 {name} ko kick kar diya", parse_mode=ParseMode.HTML)


async def kickme(update, ctx):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        return
    if await protected(ctx, chat.id, user.id):
        return await update.message.reply_text("Tum admin ho, tumhe kick nahi kar sakta 😄")
    await ctx.bot.ban_chat_member(chat.id, user.id)
    await ctx.bot.unban_chat_member(chat.id, user.id)


@group_admin("can_restrict_members")
async def mute(update, ctx):
    uid, name, rest = await get_target(update, ctx)
    if uid is None:
        return
    chat = update.effective_chat
    if await protected(ctx, chat.id, uid):
        return await update.message.reply_text("Admin ya bot ko mute nahi kar sakta 😅")
    delta = parse_duration(rest[0]) if rest else None
    await ctx.bot.restrict_chat_member(chat.id, uid, MUTED,
                                       until_date=until(delta) if delta else None)
    await update.message.reply_text(
        f"🔇 {name} mute ho gaya" + (f" ({rest[0]} ke liye)" if delta else "") + "!",
        parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def unmute(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    await ctx.bot.restrict_chat_member(update.effective_chat.id, uid, FULL)
    await update.message.reply_text(f"🔊 {name} unmute ho gaya", parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def warn(update, ctx):
    uid, name, rest = await get_target(update, ctx)
    if uid is None:
        return
    chat = update.effective_chat
    if await protected(ctx, chat.id, uid):
        return await update.message.reply_text("Admin ya bot ko warn nahi kar sakta 😅")
    limit = db_get(chat.id, "warnlimit", 3)
    count = db_get(chat.id, f"warn:{uid}", 0) + 1
    reason = html.escape(" ".join(rest)) if rest else "no reason"
    if count >= limit:
        await ctx.bot.ban_chat_member(chat.id, uid)
        db_del(chat.id, f"warn:{uid}")
        return await update.message.reply_text(
            f"🚫 {name} ko {limit}/{limit} warns mile, ban kar diya!\nReason: {reason}",
            parse_mode=ParseMode.HTML)
    db_set(chat.id, f"warn:{uid}", count)
    await update.message.reply_text(
        f"⚠️ {name} warned ({count}/{limit})\nReason: {reason}", parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def unwarn(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    chat = update.effective_chat
    count = max(db_get(chat.id, f"warn:{uid}", 0) - 1, 0)
    db_set(chat.id, f"warn:{uid}", count)
    await update.message.reply_text(f"✅ {name} ka ek warn hata diya ({count} bache)",
                                    parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def resetwarns(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    db_del(update.effective_chat.id, f"warn:{uid}")
    await update.message.reply_text(f"♻️ {name} ke saare warns reset", parse_mode=ParseMode.HTML)


async def warns(update, ctx):
    chat, msg = update.effective_chat, update.effective_message
    if chat.type == "private":
        return
    u = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    limit = db_get(chat.id, "warnlimit", 3)
    await msg.reply_text(f"{u.mention_html()} ke warns: {db_get(chat.id, f'warn:{u.id}', 0)}/{limit}",
                         parse_mode=ParseMode.HTML)


@group_admin("can_restrict_members")
async def setwarnlimit(update, ctx):
    if not ctx.args or not ctx.args[0].isdigit() or int(ctx.args[0]) < 1:
        return await update.message.reply_text("Use: /setwarnlimit 3")
    db_set(update.effective_chat.id, "warnlimit", int(ctx.args[0]))
    await update.message.reply_text(f"✅ Warn limit ab {ctx.args[0]} hai")


@group_admin("can_pin_messages")
async def pin(update, ctx):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Jo pin karna hai uspe reply karo 📌")
    loud = "loud" in [a.lower() for a in (ctx.args or [])]
    await ctx.bot.pin_chat_message(update.effective_chat.id, r.message_id,
                                   disable_notification=not loud)


@group_admin("can_pin_messages")
async def unpin(update, ctx):
    r = update.message.reply_to_message
    await ctx.bot.unpin_chat_message(update.effective_chat.id, r.message_id if r else None)
    await update.message.reply_text("📍 Unpinned")


@group_admin("can_delete_messages")
async def delete(update, ctx):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Jo delete karna hai uspe reply karo")
    await r.delete()
    await update.message.delete()


@group_admin("can_delete_messages")
async def purge(update, ctx):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Jahan se delete shuru karna hai us message pe reply karo")
    ids = range(r.message_id, min(update.message.message_id, r.message_id + 100) + 1)
    n = 0
    for mid in ids:
        try:
            await ctx.bot.delete_message(update.effective_chat.id, mid)
            n += 1
        except Exception:
            pass
    await ctx.bot.send_message(update.effective_chat.id, f"🧹 {n} messages saaf!")


# ───────────────────────── Welcome & Rules ─────────────────────────
DEFAULT_WELCOME = "Welcome {mention} to {chatname}! 🎉"


async def on_new_members(update, ctx):
    chat = update.effective_chat
    if not db_get(chat.id, "welcome_on", True):
        return
    tmpl = html.escape(db_get(chat.id, "welcome_text", DEFAULT_WELCOME))
    for u in update.message.new_chat_members:
        if u.is_bot:
            continue
        out = (tmpl.replace("{first}", html.escape(u.first_name))
                   .replace("{mention}", u.mention_html())
                   .replace("{chatname}", html.escape(chat.title or "")))
        await ctx.bot.send_message(chat.id, out, parse_mode=ParseMode.HTML)


@group_admin("can_change_info")
async def setwelcome(update, ctx):
    t = text_after_command(update.message)
    if not t:
        return await update.message.reply_text("Use: /setwelcome Welcome {mention} to {chatname}!")
    db_set(update.effective_chat.id, "welcome_text", t)
    await update.message.reply_text("✅ Welcome message set ho gaya")


@group_admin("can_change_info")
async def welcome(update, ctx):
    chat = update.effective_chat
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        db_set(chat.id, "welcome_on", ctx.args[0].lower() == "on")
        return await update.message.reply_text(f"✅ Welcome {ctx.args[0].lower()}")
    state = "ON" if db_get(chat.id, "welcome_on", True) else "OFF"
    await update.message.reply_text(
        f"Welcome: {state}\nText: {db_get(chat.id, 'welcome_text', DEFAULT_WELCOME)}\n\nUse: /welcome on|off")


@group_admin("can_change_info")
async def resetwelcome(update, ctx):
    db_del(update.effective_chat.id, "welcome_text")
    await update.message.reply_text("♻️ Welcome message default pe reset")


@group_admin("can_change_info")
async def setrules(update, ctx):
    t = text_after_command(update.message)
    if not t:
        return await update.message.reply_text("Use: /setrules <rules text>")
    db_set(update.effective_chat.id, "rules", t)
    await update.message.reply_text("✅ Rules set ho gaye")


async def rules(update, ctx):
    r = db_get(update.effective_chat.id, "rules")
    await update.message.reply_text(f"📜 Rules:\n\n{r}" if r else "Is group ke rules abhi set nahi hain.")


@group_admin("can_change_info")
async def clearrules(update, ctx):
    db_del(update.effective_chat.id, "rules")
    await update.message.reply_text("🗑 Rules clear")


# ───────────────────────── Notes & Filters ─────────────────────────
@group_admin("can_change_info")
async def save(update, ctx):
    msg = update.message
    args = ctx.args or []
    if not args:
        return await msg.reply_text("Use: /save name text  (ya kisi message pe reply karke /save name)")
    name = args[0].lower().lstrip("#")
    content = (msg.reply_to_message.text if msg.reply_to_message and msg.reply_to_message.text
               else " ".join(args[1:]))
    if not content:
        return await msg.reply_text("Note ka text bhi do 📝")
    db_set(update.effective_chat.id, f"note:{name}", content)
    await msg.reply_text(f"✅ Note <code>#{html.escape(name)}</code> save ho gaya", parse_mode=ParseMode.HTML)


async def get_note(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Use: /get name")
    n = db_get(update.effective_chat.id, f"note:{ctx.args[0].lower().lstrip('#')}")
    await update.message.reply_text(n if n else "Aisa koi note nahi mila 🤷")


async def notes(update, ctx):
    ks = db_keys(update.effective_chat.id, "note:")
    await update.message.reply_text(
        "📝 Notes:\n" + "\n".join(f"#{k}" for k in ks) if ks else "Abhi koi note save nahi hai.")


@group_admin("can_change_info")
async def clear_note(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Use: /clear name")
    db_del(update.effective_chat.id, f"note:{ctx.args[0].lower().lstrip('#')}")
    await update.message.reply_text("🗑 Note delete")


@group_admin("can_change_info")
async def add_filter(update, ctx):
    msg = update.message
    args = ctx.args or []
    if len(args) < 2:
        return await msg.reply_text("Use: /filter keyword reply text")
    db_set(update.effective_chat.id, f"filter:{args[0].lower()}", " ".join(args[1:]))
    await msg.reply_text(f"✅ Filter <code>{html.escape(args[0].lower())}</code> set",
                         parse_mode=ParseMode.HTML)


async def list_filters(update, ctx):
    ks = db_keys(update.effective_chat.id, "filter:")
    await update.message.reply_text(
        "🔎 Filters:\n" + "\n".join(f"• {k}" for k in ks) if ks else "Abhi koi filter nahi hai.")


@group_admin("can_change_info")
async def stop_filter(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Use: /stop keyword")
    db_del(update.effective_chat.id, f"filter:{ctx.args[0].lower()}")
    await update.message.reply_text("🛑 Filter hata diya")


async def group_triggers(update, ctx):
    """Group me #note aur filter keywords."""
    chat, text = update.effective_chat, update.effective_message.text or ""
    if chat.type == "private":
        return
    for tag in re.findall(r"#(\w+)", text):
        n = db_get(chat.id, f"note:{tag.lower()}")
        if n:
            return await update.effective_message.reply_text(n)
    low = text.lower()
    for kw in db_keys(chat.id, "filter:"):
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return await update.effective_message.reply_text(db_get(chat.id, f"filter:{kw}"))


# ───────────────────────── AI chat ─────────────────────────
async def chat(update, ctx):
    msg, chat_obj = update.message, update.effective_chat
    text = msg.text
    if chat_obj.type != "private":
        mention = f"@{ctx.bot.username}".lower()
        replied_to_bot = (msg.reply_to_message and msg.reply_to_message.from_user
                          and msg.reply_to_message.from_user.id == ctx.bot.id)
        if mention not in text.lower() and not replied_to_bot:
            return
        text = re.sub(re.escape(mention), "", text, flags=re.I).strip() or "hi"
        text = f"{update.effective_user.first_name}: {text}"

    cid = chat_obj.id
    history[cid].append({"role": "user", "content": text})
    await ctx.bot.send_chat_action(cid, ChatAction.TYPING)
    try:
        res = await ai.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": build_system_prompt()}, *history[cid]],
            max_tokens=MAX_TOKENS,
        )
        reply = res.choices[0].message.content
        history[cid].append({"role": "assistant", "content": reply})
    except Exception as e:
        log.exception("AI error")
        reply = f"⚠️ AI error: {e}"
    await msg.reply_text(reply)


# ───────────────────────── Setup ─────────────────────────
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("help", "Saare commands"), BotCommand("id", "User/chat ID"),
        BotCommand("rules", "Group rules"), BotCommand("notes", "Saved notes"),
        BotCommand("filters", "Active filters"), BotCommand("warns", "Warns check"),
        BotCommand("admins", "Admin list"), BotCommand("ping", "Bot alive?"),
        BotCommand("recovery", "MIUI/HyperOS recovery ROM"), BotCommand("twrp", "TWRP download"),
        BotCommand("codename", "Device name se codename"), BotCommand("specs", "Device specs"),
        BotCommand("reset", "AI chat reset"),
    ])


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    cmds = {
        "start": start, "help": help_cmd, "reset": reset, "ping": ping, "id": id_cmd,
        "info": info, "admins": admins, "adminlist": admins,
        "ban": ban, "tban": ban, "unban": unban, "kick": kick, "kickme": kickme,
        "mute": mute, "tmute": mute, "unmute": unmute,
        "warn": warn, "unwarn": unwarn, "resetwarns": resetwarns, "warns": warns,
        "setwarnlimit": setwarnlimit, "pin": pin, "unpin": unpin, "del": delete, "purge": purge,
        "setwelcome": setwelcome, "welcome": welcome, "resetwelcome": resetwelcome,
        "setrules": setrules, "rules": rules, "clearrules": clearrules,
        "save": save, "get": get_note, "notes": notes, "clear": clear_note,
        "filter": add_filter, "filters": list_filters, "stop": stop_filter,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    xiaomi.register(app, db_get, db_set)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    text = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(text, group_triggers), group=1)
    app.add_handler(MessageHandler(text, chat), group=2)

    webhook = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if webhook:
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", "10000")),
                        url_path=BOT_TOKEN, webhook_url=f"{webhook}/{BOT_TOKEN}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
