"""Ask Me Anonymously - Telegram bot.

Everyone gets a personal link. Friends open it and send anonymous messages.
Owner can reply anonymously or block a sender. Storage: Postgres (DATABASE_URL)
or in-memory fallback for local tests.
"""
import logging
import os
import secrets
import string
import time
from collections import defaultdict, deque
from urllib.parse import quote

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                      Update)
from telegram.constants import ChatType
from telegram.error import Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, PreCheckoutQueryHandler,
                          filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anonbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_LEN = 1000
RATE = 8            # max messages per sender per owner...
RATE_WINDOW = 3600  # ...per hour


# ---------------- storage ----------------
class MemoryStore:
    def __init__(self):
        self.users, self.codes, self.blocks, self.msgs = {}, {}, set(), {}
        self.next_id = 1

    async def init(self):
        pass

    async def upsert_user(self, uid, name):
        if uid in self.users:
            self.users[uid]["name"] = name
            return self.users[uid]["code"], False
        code = new_code()
        self.users[uid] = {"code": code, "name": name}
        self.codes[code] = uid
        return code, True

    async def owner_by_code(self, code):
        uid = self.codes.get(code)
        return (uid, self.users[uid]["name"]) if uid else None

    async def is_blocked(self, owner, sender):
        return (owner, sender) in self.blocks

    async def block(self, owner, sender):
        self.blocks.add((owner, sender))

    async def unblock_all(self, owner):
        n = len([b for b in self.blocks if b[0] == owner])
        self.blocks = {b for b in self.blocks if b[0] != owner}
        return n

    async def add_msg(self, owner, sender):
        i = self.next_id
        self.next_id += 1
        self.msgs[i] = (owner, sender)
        return i

    async def get_msg(self, mid):
        return self.msgs.get(mid)

    async def stats(self, owner):
        return sum(1 for o, _ in self.msgs.values() if o == owner)

    async def totals(self):
        return len(self.users), len(self.msgs)


class PgStore:
    def __init__(self, dsn):
        self.dsn, self.pool = dsn, None

    async def init(self):
        import asyncpg
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        async with self.pool.acquire() as c:
            await c.execute("""
            CREATE TABLE IF NOT EXISTS users(
              id BIGINT PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT,
              created_at TIMESTAMPTZ DEFAULT now());
            CREATE TABLE IF NOT EXISTS blocks(
              owner BIGINT, sender BIGINT, PRIMARY KEY(owner, sender));
            CREATE TABLE IF NOT EXISTS msgs(
              id BIGSERIAL PRIMARY KEY, owner BIGINT NOT NULL, sender BIGINT NOT NULL,
              created_at TIMESTAMPTZ DEFAULT now());
            CREATE INDEX IF NOT EXISTS msgs_owner ON msgs(owner);
            """)

    async def upsert_user(self, uid, name):
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT code FROM users WHERE id=$1", uid)
            if row:
                await c.execute("UPDATE users SET name=$2 WHERE id=$1", uid, name)
                return row["code"], False
            code = new_code()
            await c.execute("INSERT INTO users(id, code, name) VALUES($1,$2,$3)", uid, code, name)
            return code, True

    async def owner_by_code(self, code):
        row = await self.pool.fetchrow("SELECT id, name FROM users WHERE code=$1", code)
        return (row["id"], row["name"]) if row else None

    async def is_blocked(self, owner, sender):
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM blocks WHERE owner=$1 AND sender=$2", owner, sender))

    async def block(self, owner, sender):
        await self.pool.execute("INSERT INTO blocks VALUES($1,$2) ON CONFLICT DO NOTHING", owner, sender)

    async def unblock_all(self, owner):
        r = await self.pool.execute("DELETE FROM blocks WHERE owner=$1", owner)
        return int(r.split()[-1])

    async def add_msg(self, owner, sender):
        return await self.pool.fetchval(
            "INSERT INTO msgs(owner, sender) VALUES($1,$2) RETURNING id", owner, sender)

    async def get_msg(self, mid):
        row = await self.pool.fetchrow("SELECT owner, sender FROM msgs WHERE id=$1", mid)
        return (row["owner"], row["sender"]) if row else None

    async def stats(self, owner):
        return await self.pool.fetchval("SELECT count(*) FROM msgs WHERE owner=$1", owner)

    async def totals(self):
        u = await self.pool.fetchval("SELECT count(*) FROM users")
        m = await self.pool.fetchval("SELECT count(*) FROM msgs")
        return u, m


def new_code():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


DB_URL = os.environ.get("DATABASE_URL")
store = PgStore(DB_URL) if DB_URL else MemoryStore()
recent = defaultdict(deque)  # (sender, owner) -> timestamps


# ---------------- helpers ----------------
def link_for(ctx, code):
    return f"https://t.me/{ctx.bot.username}?start={code}"


def share_kb(ctx, link):
    text = "Send me an anonymous message 👀 I won't know it's you!"
    share = f"https://t.me/share/url?url={quote(link)}&text={quote(text)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share my link", url=share)],
    ])


async def send_my_link(update, ctx, code, new=False):
    link = link_for(ctx, code)
    head = "Welcome! 🎉\n\n" if new else ""
    await update.effective_message.reply_text(
        f"{head}🔗 Your anonymous link:\n{link}\n\n"
        "Put it in your Instagram story, WhatsApp status or Telegram bio. "
        "Friends tap it and send you messages — you'll never see who sent them.\n\n"
        "You can reply anonymously or block anyone with one tap.\n\n"
        "/mylink — your link  •  /stats — messages received  •  /unblockall",
        reply_markup=share_kb(ctx, link), disable_web_page_preview=True)


# ---------------- handlers ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    code, new = await store.upsert_user(u.id, u.first_name or "someone")
    if new:
        log.info("NEW_USER")
    if ctx.args:
        found = await store.owner_by_code(ctx.args[0])
        if found:
            owner, name = found
            ctx.user_data.pop("reply_to", None)
            ctx.user_data["target"] = owner
            await update.message.reply_text(
                f"✍️ Write your anonymous message to {name}.\n\n"
                "They will NOT see your name or username. Just type it and send.\n"
                "(Be kind — they can block you.)")
            return
        await update.message.reply_text("That link isn't valid anymore. Here's your own instead 👇")
    await send_my_link(update, ctx, code, new)


async def mylink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    code, _ = await store.upsert_user(u.id, u.first_name or "someone")
    await send_my_link(update, ctx, code)


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = await store.stats(update.effective_user.id)
    await update.message.reply_text(f"📬 You've received {n} anonymous message(s). Share your link for more: /mylink")


async def unblockall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = await store.unblock_all(update.effective_user.id)
    await update.message.reply_text(f"✅ Unblocked {n} sender(s).")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    me = update.effective_user.id
    text = msg.text.strip()
    if len(text) > MAX_LEN:
        await msg.reply_text(f"Too long — keep it under {MAX_LEN} characters.")
        return

    # owner replying to an anonymous message
    mid = ctx.user_data.get("reply_to")
    if mid:
        ctx.user_data.pop("reply_to", None)
        row = await store.get_msg(mid)
        if not row or row[0] != me:
            await msg.reply_text("Couldn't find that message.")
            return
        try:
            await ctx.bot.send_message(row[1], f"💬 You got a reply to your anonymous message:\n\n{text}\n\n"
                                               "Want your own anonymous link? /mylink")
            await msg.reply_text("✅ Reply sent. They still don't know who you are, and you don't know who they are.")
        except Forbidden:
            await msg.reply_text("They've stopped the bot, so the reply couldn't be delivered.")
        return

    owner = ctx.user_data.get("target")
    if not owner:
        await msg.reply_text("To send an anonymous message, open someone's link first.\n\nWant your own link? /mylink")
        return
    if await store.is_blocked(owner, me):
        await msg.reply_text("❌ You can't send messages to this person.")
        return
    q = recent[(me, owner)]
    now = time.time()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE:
        await msg.reply_text("Slow down 🙂 Try again in a while.")
        return
    q.append(now)

    mid = await store.add_msg(owner, me)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Reply anonymously", callback_data=f"r:{mid}"),
        InlineKeyboardButton("🚫 Block", callback_data=f"b:{mid}"),
    ]])
    try:
        await ctx.bot.send_message(owner, f"📩 New anonymous message:\n\n{text}", reply_markup=kb)
    except Forbidden:
        await msg.reply_text("This person has stopped the bot, so they can't receive messages right now.")
        return
    log.info("MSG_SENT")
    await msg.reply_text(
        "✅ Sent anonymously! You can send another, or get your OWN link so friends can message you 👇",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get my own link", callback_data="mine")]]))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    me = q.from_user.id
    data = q.data
    if data == "mine":
        await q.answer()
        code, _ = await store.upsert_user(me, q.from_user.first_name or "someone")
        await send_my_link(update, ctx, code)
        return
    kind, _, mid = data.partition(":")
    row = await store.get_msg(int(mid)) if mid.isdigit() else None
    if not row or row[0] != me:
        await q.answer("Message not found", show_alert=True)
        return
    if kind == "r":
        ctx.user_data["reply_to"] = int(mid)
        await q.answer()
        await q.message.reply_text("✍️ Type your reply now. It goes back anonymously.")
    elif kind == "b":
        await store.block(me, row[1])
        await q.answer("Blocked", show_alert=False)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("🚫 Blocked. They can't message you anymore. Undo with /unblockall")


# ---------------- Stars tips ----------------
async def support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"⭐ {n}", callback_data=f"tip{n}") for n in (25, 50, 100)]])
    await update.message.reply_text("This bot is free. If you enjoy it, you can tip with Telegram Stars ❤️", reply_markup=kb)


async def on_tip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    stars = int(q.data[3:])
    await ctx.bot.send_invoice(chat_id=q.message.chat_id, title="Support Ask Me Anonymously",
                               description=f"Voluntary tip of {stars} Stars. Thank you!",
                               payload=f"tip-{stars}", provider_token="", currency="XTR",
                               prices=[LabeledPrice("Tip", stars)])


async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info("TIP %s", update.message.successful_payment.total_amount)
    await update.message.reply_text("Thank you so much for the support! ⭐")


async def paysupport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tips are voluntary. For any payment issue, message here with details and we'll help.")


async def admin_totals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, m = await store.totals()
    await update.message.reply_text(f"Users: {u}\nMessages: {m}")


async def post_init(app):
    await store.init()
    from telegram import BotCommand
    b = app.bot
    try:
        await b.set_my_commands([
            BotCommand("mylink", "Get your anonymous link"),
            BotCommand("stats", "Messages you've received"),
            BotCommand("unblockall", "Unblock everyone you blocked"),
            BotCommand("support", "Tip with Telegram Stars"),
            BotCommand("paysupport", "Help with a payment"),
        ])
        await b.set_my_short_description(
            "Get anonymous messages from friends 👀 Share your link on Instagram & WhatsApp. Free.")
        await b.set_my_description(
            "Ask Me Anonymously 👀\n\nGet your personal link, put it in your Instagram story or "
            "WhatsApp status, and friends send you anonymous messages and confessions.\n\n"
            "• 100% anonymous\n• Reply anonymously\n• Block anyone in one tap\n• Free, no signup\n\nTap Start to get your link!")
        if (await b.get_my_name()).name != "Ask Me Anonymously 👀 Secret Messages":
            await b.set_my_name("Ask Me Anonymously 👀 Secret Messages")
    except Exception as e:  # profile setup is best-effort
        log.warning("profile setup: %s", e)


def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).post_init(post_init).build()
    pv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=pv))
    app.add_handler(CommandHandler("mylink", mylink, filters=pv))
    app.add_handler(CommandHandler("stats", stats, filters=pv))
    app.add_handler(CommandHandler("unblockall", unblockall, filters=pv))
    app.add_handler(CommandHandler("support", support, filters=pv))
    app.add_handler(CommandHandler("paysupport", paysupport, filters=pv))
    app.add_handler(CommandHandler("totals", admin_totals, filters=pv))
    app.add_handler(CallbackQueryHandler(on_tip, pattern="^tip"))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_handler(MessageHandler(pv & filters.TEXT & ~filters.COMMAND, on_text))

    url = os.environ.get("WEBHOOK_URL")
    if url:
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=token[-20:],
                        webhook_url=f"{url.rstrip('/')}/{token[-20:]}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
