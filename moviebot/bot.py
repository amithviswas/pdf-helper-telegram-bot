"""Movie & Series Finder - Telegram bot (legal where-to-watch finder for India).

Type a title -> rating, story, trailer and which OTT app streams it in India.
/new  -> new on OTT in India (by language)
/trending -> trending this week
/watchlist -> your saved titles
Data: TMDB API (watch providers by JustWatch). Env: BOT_TOKEN, TMDB_TOKEN,
DATABASE_URL (optional, for watchlists), WEBHOOK_URL (optional).
"""
import datetime as dt
import html
import logging
import os

import httpx
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      LabeledPrice, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, PreCheckoutQueryHandler,
                          filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moviebot")
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
REGION = "IN"
OTT_IDS = "8|119|122|2336|232|237|220|309|11"  # Netflix, Prime, Hotstar, JioHotstar, Zee5, SonyLIV, JioCinema, Sun NXT, MUBI
LANGS = {"all": "All", "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "ml": "Malayalam",
         "kn": "Kannada", "en": "English", "ko": "Korean"}

http: httpx.AsyncClient = None
ia_http = httpx.AsyncClient(timeout=15)
GENRES = {}


async def tmdb(path, **params):
    r = await http.get(API + path, params=params)
    r.raise_for_status()
    return r.json()


# ---------------- watchlist storage ----------------
class MemStore:
    def __init__(self):
        self.d = {}

    async def init(self):
        pass

    async def add(self, uid, kind, tid, title):
        self.d.setdefault(uid, {})[(kind, tid)] = title

    async def remove(self, uid, kind, tid):
        self.d.get(uid, {}).pop((kind, tid), None)

    async def list(self, uid):
        return [(k, t, ti) for (k, t), ti in self.d.get(uid, {}).items()]

    async def seen_user(self, uid):
        pass

    async def totals(self):
        return len(self.d), sum(len(v) for v in self.d.values())


class PgStore:
    def __init__(self, dsn):
        self.dsn = dsn

    async def init(self):
        import asyncpg
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        u = urlsplit(self.dsn)
        q = [(k, v) for k, v in parse_qsl(u.query) if k != "channel_binding"]
        self.pool = await asyncpg.create_pool(urlunsplit(u._replace(query=urlencode(q))),
                                              min_size=1, max_size=4)
        await self.pool.execute("""
        CREATE TABLE IF NOT EXISTS mv_watchlist(
          uid BIGINT, kind TEXT, tid INT, title TEXT, added TIMESTAMPTZ DEFAULT now(),
          PRIMARY KEY(uid, kind, tid));
        CREATE TABLE IF NOT EXISTS mv_users(uid BIGINT PRIMARY KEY, first_seen TIMESTAMPTZ DEFAULT now(),
          last_seen TIMESTAMPTZ DEFAULT now());
        """)

    async def add(self, uid, kind, tid, title):
        await self.pool.execute("INSERT INTO mv_watchlist(uid,kind,tid,title) VALUES($1,$2,$3,$4) "
                                "ON CONFLICT DO NOTHING", uid, kind, tid, title)

    async def remove(self, uid, kind, tid):
        await self.pool.execute("DELETE FROM mv_watchlist WHERE uid=$1 AND kind=$2 AND tid=$3", uid, kind, tid)

    async def list(self, uid):
        rows = await self.pool.fetch("SELECT kind, tid, title FROM mv_watchlist WHERE uid=$1 ORDER BY added DESC LIMIT 30", uid)
        return [(r["kind"], r["tid"], r["title"]) for r in rows]

    async def seen_user(self, uid):
        await self.pool.execute("INSERT INTO mv_users(uid) VALUES($1) ON CONFLICT(uid) DO UPDATE SET last_seen=now()", uid)

    async def totals(self):
        u = await self.pool.fetchval("SELECT count(*) FROM mv_users")
        w = await self.pool.fetchval("SELECT count(*) FROM mv_users WHERE last_seen > now() - interval '7 days'")
        return u, w


store = PgStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else MemStore()


# ---------------- formatting ----------------
def year_of(item):
    d = item.get("release_date") or item.get("first_air_date") or ""
    return d[:4]


def title_of(item):
    return item.get("title") or item.get("name") or "Untitled"


def short_line(item, kind=None):
    kind = kind or item.get("media_type")
    icon = "📺" if kind == "tv" else "🎬"
    y = year_of(item)
    r = item.get("vote_average") or 0
    star = f" ⭐{r:.1f}" if r else ""
    return f"{icon} {title_of(item)}{f' ({y})' if y else ''}{star}"


def results_kb(items, default_kind=None):
    rows = []
    for it in items[:8]:
        kind = it.get("media_type") or default_kind
        if kind not in ("movie", "tv"):
            continue
        rows.append([InlineKeyboardButton(short_line(it, kind)[:60], callback_data=f"d:{kind}:{it['id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


def providers_text(wp):
    ind = (wp or {}).get("results", {}).get(REGION, {})
    lines, seen = [], set()
    for key, label in (("flatrate", "▶️ Streaming on"), ("free", "🆓 Free on"), ("ads", "🆓 Free with ads on"),
                       ("rent", "💳 Rent on"), ("buy", "🛒 Buy on")):
        if key in ("rent", "buy") and lines:
            break  # streamable: skip rent/buy noise
        names = []
        for pr in ind.get(key, []):
            n = pr["provider_name"].replace(" with Ads", "").replace(" Amazon Channel", "")
            if n not in seen:
                seen.add(n)
                names.append(n)
        if names:
            lines.append(f"{label}: <b>{html.escape(', '.join(names[:5]))}</b>")
    return "\n".join(lines) if lines else "❌ Not on any Indian OTT app right now (maybe only in theatres)."


async def details(kind, tid):
    d = await tmdb(f"/{kind}/{tid}", append_to_response="videos,watch/providers,credits", language="en-IN")
    t = title_of(d)
    y = year_of(d)
    rating = d.get("vote_average") or 0
    genres = ", ".join(g["name"] for g in d.get("genres", [])[:3])
    if kind == "movie":
        rt = d.get("runtime") or 0
        extra = f"{rt // 60}h {rt % 60}m" if rt else ""
    else:
        extra = f"{d.get('number_of_seasons', '?')} season(s)"
    lang = (d.get("original_language") or "").upper()
    cast = ", ".join(c["name"] for c in d.get("credits", {}).get("cast", [])[:4])
    overview = d.get("overview") or "No story available yet."
    if len(overview) > 450:
        overview = overview[:450].rsplit(" ", 1)[0] + "…"
    meta = " • ".join(x for x in [y, genres, extra, lang] if x)
    text = (f"{'📺' if kind == 'tv' else '🎬'} <b>{html.escape(t)}</b>\n"
            f"{html.escape(meta)}\n"
            + (f"⭐ {rating:.1f}/10 ({d.get('vote_count', 0)} votes)\n" if rating else "")
            + (f"👥 {html.escape(cast)}\n" if cast else "")
            + f"\n{html.escape(overview)}\n\n"
            f"{providers_text(d.get('watch/providers'))}\n\n"
            "<i>Streaming data: JustWatch • TMDB</i>")
    trailer = None
    for v in d.get("videos", {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
            trailer = f"https://www.youtube.com/watch?v={v['key']}"
            if v.get("type") == "Trailer":
                break
    btns = []
    if trailer:
        btns.append([InlineKeyboardButton("🎞 Watch trailer", url=trailer)])
    btns.append([InlineKeyboardButton("➕ Add to watchlist", callback_data=f"w:{kind}:{tid}"),
                 InlineKeyboardButton("🔁 Similar", callback_data=f"s:{kind}:{tid}")])
    poster = IMG + d["poster_path"] if d.get("poster_path") else None
    return t, text, InlineKeyboardMarkup(btns), poster


async def send_details(chat_msg, kind, tid):
    t, text, kb, poster = await details(kind, tid)
    if poster and len(text) <= 1024:
        await chat_msg.reply_photo(poster, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        if poster:
            await chat_msg.reply_photo(poster)
        await chat_msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                  disable_web_page_preview=True)


# ---------------- free legal full movies (Internet Archive) ----------------
IA_SAFE = "(year:[1800 TO 1930] OR licenseurl:*publicdomain* OR licenseurl:*creativecommons*)"


async def ia_search(q=None, rows=6):
    query = f"collection:(feature_films) AND mediatype:(movies) AND {IA_SAFE}"
    if q:
        q = "".join(c for c in q if c.isalnum() or c == " ").strip()
        if not q:
            return []
        query = f"title:({q}) AND " + query
    params = {"q": query, "fl[]": ["identifier", "title", "year"], "sort[]": "downloads desc",
              "rows": rows, "output": "json"}
    try:
        r = await ia_http.get("https://archive.org/advancedsearch.php", params=params)
        return r.json().get("response", {}).get("docs", [])
    except Exception as e:
        log.warning("ia: %s", e)
        return []


def ia_kb(docs):
    rows = []
    for d in docs:
        t = d.get("title") or d["identifier"]
        if isinstance(t, list):
            t = t[0]
        y = d.get("year")
        rows.append([InlineKeyboardButton(f"⬇️ {t[:45]}{f' ({y})' if y else ''}",
                                          url=f"https://archive.org/details/{d['identifier']}")])
    return InlineKeyboardMarkup(rows)


async def free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args) if ctx.args else None
    docs = await ia_search(q, rows=10)
    if not docs:
        await update.message.reply_text("No free legal copy found for that. Try /free with no name to see popular classics.")
        return
    await update.message.reply_text(
        "🆓 Free full movies — legal to watch & download (public domain / Creative Commons, via Internet Archive).\n"
        "Tap one, then pick MPEG4 under Download Options:", reply_markup=ia_kb(docs))


# ---------------- handlers ----------------
WELCOME = ("🍿 <b>Movie & Series Finder</b>\n\n"
           "Just type any movie or web series name, like <i>Pushpa 2</i> or <i>Panchayat</i>.\n"
           "I'll show the rating, story, trailer and <b>which app it's streaming on in India</b> "
           "(Netflix, Prime, Hotstar, Zee5, SonyLIV…).\n\n"
           "/new — new on OTT this month (Telugu, Hindi, Tamil…)\n"
           "/trending — what everyone's watching this week\n"
           "/watchlist — your saved titles\n"
           "/free — free full movies you can legally download\n"
           "/support — tip with Stars")


def share_kb(ctx):
    from urllib.parse import quote
    link = f"https://t.me/{ctx.bot.username}"
    url = f"https://t.me/share/url?url={quote(link)}&text={quote('Find where any movie or web series is streaming in India 🍿')}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share with friends", url=url)]])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=share_kb(ctx))


async def search_tmdb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()[:80]
    data = await tmdb("/search/multi", query=q, language="en-IN", region=REGION, include_adult="false")
    items = [i for i in data.get("results", []) if i.get("media_type") in ("movie", "tv")]
    if not items:
        await update.message.reply_text("😕 Nothing found. Check the spelling, or try the English title.")
        return
    items.sort(key=lambda i: (title_of(i).lower() != q.lower(), -(i.get("popularity") or 0)))
    if len(items) == 1 or title_of(items[0]).lower() == q.lower() and (
            len(items) < 2 or title_of(items[1]).lower() != q.lower()):
        await send_details(update.message, items[0]["media_type"], items[0]["id"])
        if len(items) > 1:
            await update.message.reply_text("Not this one? Pick another:", reply_markup=results_kb(items[1:]))
        return
    await update.message.reply_text(f"🔎 Results for “{q}”:", reply_markup=results_kb(items))


async def on_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await search_tmdb(update, ctx)
    docs = await ia_search(update.message.text.strip()[:80], rows=3)
    if docs:
        await update.message.reply_text("🆓 Free full movie available (legal, public domain):", reply_markup=ia_kb(docs))


def lang_kb(prefix):
    keys = list(LANGS)
    rows = [keys[i:i + 4] for i in range(0, len(keys), 4)]
    return InlineKeyboardMarkup([[InlineKeyboardButton(LANGS[k], callback_data=f"{prefix}:{k}") for k in r] for r in rows])


async def new_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆕 New on OTT in India — pick a language:", reply_markup=lang_kb("n"))


async def new_list(lang, kind):
    today = dt.date.today()
    since = (today - dt.timedelta(days=60)).isoformat()
    params = dict(watch_region=REGION, with_watch_providers=OTT_IDS, with_watch_monetization_types="flatrate",
                  sort_by="popularity.desc", language="en-IN")
    if kind == "movie":
        params.update({"primary_release_date.gte": since, "primary_release_date.lte": today.isoformat()})
    else:
        params.update({"first_air_date.gte": since, "first_air_date.lte": today.isoformat()})
    if lang != "all":
        params["with_original_language"] = lang
    return (await tmdb(f"/discover/{kind}", **params)).get("results", [])


async def trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = await tmdb("/trending/all/week", language="en-IN")
    await update.message.reply_text("🔥 Trending this week — tap one:", reply_markup=results_kb(data.get("results", [])))


async def watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = await store.list(update.effective_user.id)
    if not items:
        await update.message.reply_text("Your watchlist is empty. Search a title and tap ➕ Add to watchlist.")
        return
    rows = [[InlineKeyboardButton(("📺 " if k == "tv" else "🎬 ") + t[:50], callback_data=f"d:{k}:{i}"),
             InlineKeyboardButton("✖", callback_data=f"x:{k}:{i}")] for k, i, t in items]
    await update.message.reply_text("📌 Your watchlist (tap to check where it's streaming now):",
                                    reply_markup=InlineKeyboardMarkup(rows))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    act = parts[0]
    try:
        if act == "d":
            await q.answer()
            await send_details(q.message, parts[1], int(parts[2]))
        elif act == "n":
            await q.answer()
            lang = parts[1]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Movies", callback_data=f"nm:{lang}:movie"),
                                        InlineKeyboardButton("📺 Web series", callback_data=f"nm:{lang}:tv")]])
            await q.message.reply_text(f"{LANGS[lang]} — movies or web series?", reply_markup=kb)
        elif act == "nm":
            await q.answer("Loading…")
            lang, kind = parts[1], parts[2]
            items = await new_list(lang, kind)
            if not items:
                await q.message.reply_text("Nothing new streaming in the last 2 months for that pick. Try another language.")
                return
            await q.message.reply_text(f"🆕 New {LANGS[lang]} {'movies' if kind == 'movie' else 'web series'} on OTT (last 60 days):",
                                       reply_markup=results_kb(items, kind))
        elif act == "s":
            await q.answer()
            kind, tid = parts[1], int(parts[2])
            data = await tmdb(f"/{kind}/{tid}/recommendations", language="en-IN")
            items = data.get("results", [])
            if not items:
                await q.message.reply_text("No similar titles found.")
                return
            await q.message.reply_text("🔁 You might also like:", reply_markup=results_kb(items, kind))
        elif act == "w":
            kind, tid = parts[1], int(parts[2])
            d = await tmdb(f"/{kind}/{tid}")
            await store.add(q.from_user.id, kind, tid, title_of(d))
            await q.answer("Added to your watchlist ✅ (/watchlist)")
        elif act == "x":
            await store.remove(q.from_user.id, parts[1], int(parts[2]))
            await q.answer("Removed")
        else:
            await q.answer()
    except httpx.HTTPError as e:
        log.warning("tmdb error %s", e)
        await q.answer("Couldn't load that right now, try again.", show_alert=True)


async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u:
        try:
            await store.seen_user(u.id)
        except Exception as e:
            log.warning("track: %s", e)


async def totals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, w = await store.totals()
    await update.message.reply_text(f"Users: {u}\nActive last 7 days: {w}")


# ---------------- Stars tips ----------------
async def support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"⭐ {n}", callback_data=f"tip{n}") for n in (25, 50, 100)]])
    await update.message.reply_text("This bot is free. If it helped you find something good to watch, tip with Telegram Stars ❤️", reply_markup=kb)


async def on_tip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    stars = int(q.data[3:])
    await ctx.bot.send_invoice(chat_id=q.message.chat_id, title="Support Movie & Series Finder",
                               description=f"Voluntary tip of {stars} Stars. Thank you!",
                               payload=f"tip-{stars}", provider_token="", currency="XTR",
                               prices=[LabeledPrice("Tip", stars)])


async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info("TIP %s", update.message.successful_payment.total_amount)
    await update.message.reply_text("Thank you so much! ⭐")


async def paysupport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tips are voluntary. For any payment issue, message here with details.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("error: %s", ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong, please try again.")
        except Exception:
            pass


async def post_init(app):
    global http
    http = httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {os.environ['TMDB_TOKEN']}",
                                                  "accept": "application/json"})
    await store.init()
    b = app.bot
    try:
        await b.set_my_commands([
            BotCommand("new", "New on OTT (Telugu, Hindi, Tamil…)"),
            BotCommand("trending", "Trending this week"),
            BotCommand("watchlist", "Your saved titles"),
            BotCommand("free", "Free full movies (legal downloads)"),
            BotCommand("support", "Tip with Telegram Stars"),
            BotCommand("paysupport", "Help with a payment"),
        ])
        await b.set_my_short_description("Find where any movie or web series is streaming in India 🍿 Ratings, trailers, new OTT releases. Free.")
        await b.set_my_description(
            "🍿 Movie & Series Finder\n\nType any movie or web series name and get:\n"
            "• Which app it's on in India (Netflix, Prime, Hotstar, Zee5, SonyLIV…)\n"
            "• Rating, story, cast & trailer\n• Similar picks\n\n"
            "Plus: new OTT releases in Telugu, Hindi, Tamil & more, trending this week, and your own watchlist.\n\nFree • no signup")
        name = "Movie & Series Finder 🍿 Where to Watch"
        if (await b.get_my_name()).name != name:
            await b.set_my_name(name)
    except Exception as e:
        log.warning("profile setup: %s", e)


def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).post_init(post_init).concurrent_updates(True).build()
    from telegram.ext import TypeHandler
    app.add_handler(TypeHandler(Update, track), group=-1)
    pv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "help"], start, filters=pv))
    app.add_handler(CommandHandler("new", new_cmd, filters=pv))
    app.add_handler(CommandHandler("trending", trending, filters=pv))
    app.add_handler(CommandHandler("watchlist", watchlist, filters=pv))
    app.add_handler(CommandHandler("free", free_cmd, filters=pv))
    app.add_handler(CommandHandler("support", support, filters=pv))
    app.add_handler(CommandHandler("paysupport", paysupport, filters=pv))
    app.add_handler(CommandHandler("totals", totals, filters=pv))
    app.add_handler(CallbackQueryHandler(on_tip, pattern="^tip"))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_handler(MessageHandler(pv & filters.TEXT & ~filters.COMMAND, on_search))
    app.add_error_handler(on_error)

    url = os.environ.get("WEBHOOK_URL")
    if url:
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=token[-20:],
                        webhook_url=f"{url.rstrip('/')}/{token[-20:]}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
