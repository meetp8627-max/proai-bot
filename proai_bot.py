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
from telegram import (Update, BotCommand, ChatPermissions, InlineKeyboardButton,
                      InlineKeyboardMarkup, LinkPreviewOptions, WebAppInfo)
from telegram.constants import ChatAction, ParseMode
import xiaomi
import search
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proai")

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_MODEL = os.environ.get("AI_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "55"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "5500"))
TEMPERATURE = float(os.environ.get("AI_TEMPERATURE", "0.6"))
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

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
def persona() -> str:
    today = datetime.date.today().strftime("%d %B %Y")
    return (
        "Tum 'ProAI' ho, ek AI assistant jo Telegram pe (@PraKrutim_bot) rehta hai. "
        "Tum ek ladka (male) ho. Hamesha masculine Hinglish bolo: 'main kar raha hu', "
        "'bata dunga', 'samajh gaya'. Kabhi 'rahi hu', 'karungi', 'gayi' jaisa feminine grammar mat use karo. "
        f"Aaj ki date hai {today}. "
        "Chote, stylish aur energetic Hinglish me reply do, 3-4 lines me jab tak user detail na maange. "
        "Emojis thode aur sahi jagah use karo. "
        "Apne baare me design, UI ya 'Liquid Glass' ka zikr tab tak mat karo jab tak user na poochhe. "
    )


def build_system_prompt() -> str:
    if tools_ok:
        rules = (
            "Tumhare paas web_search tool hai. Latest ya badalne wali cheezon ke liye (naye phones, prices, "
            "launch dates, news, scores, weather, aaj ke events, kisi ki current position) pehle web_search "
            "chalao, phir sirf search results ke basis pe jawab do. Search query English me, chhoti aur specific rakho. "
            "Result me jawab na mile to saaf bolo, guess ya specs invent mat karo. "
            "Pakke general knowledge ke sawaalon pe search mat chalao. "
        )
    else:
        rules = (
            "Tumhare paas internet ya live data nahi hai aur knowledge purani ho sakti hai. "
            "Latest phones, prices, news jaisi cheezon me guess mat karo, bolo 'mere paas latest info nahi hai'. "
            "Specs ya model names kabhi invent mat karo. "
        )
    return persona() + rules + "Code ya technical sawaal me seedha aur accurate jawab do."


history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

HELP = """<b>ProAI ✨</b>
Private me kuch bhi poocho. Group me mujhe @mention karo ya mere message pe reply karo.

<b>Basic</b>
/app (Mini App) /id /info /ping /reset /admins

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

<b>Smart tools</b>\n/ask /search /news /tldr /translate\n(AI khud bhi zaroorat pe web search karta hai)\n\n<b>Xiaomi Geeks</b> (codename do, jaise /twrp whyred)
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


async def app_cmd(update, ctx):
    if not BASE_URL:
        return await update.message.reply_text("Mini App abhi off hai (server URL set nahi).")
    if update.effective_chat.type != "private":
        return await update.message.reply_text(f"Mini App private chat me khulta hai 👉 https://t.me/{ctx.bot.username}")
    await update.message.reply_text("ProAI Mini App ✨", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("✨ Open ProAI", web_app=WebAppInfo(url=BASE_URL))]]))


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
    msg = upda
