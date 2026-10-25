"""PDF Helper Bot for Telegram.

Send photos -> get one PDF.  Send PDFs -> merge them or shrink them.
Free to run: no paid APIs. Needs BOT_TOKEN env var. Ghostscript (gs) for compression.
"""
import os
import shutil
import subprocess
import tempfile
import logging
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pdfbot")

MAX_FILES = 30
MAX_MB = 20  # Telegram bots can download files up to 20 MB

WELCOME = (
    "Hi! I'm your free PDF helper.\n\n"
    "📷 Send me photos (notes, documents, certificates) and I'll turn them into one PDF.\n"
    "📎 Send me PDF files and I can merge them into one, or shrink a big PDF so it fits upload limits.\n\n"
    "Just send your files, then tap a button. /clear starts over."
)


def session(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    s = ctx.user_data
    if "dir" not in s or not Path(s["dir"]).exists():
        s["dir"] = tempfile.mkdtemp(prefix="pdfbot_")
        s["images"], s["pdfs"] = [], []
    return s


def reset(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data.get("dir")
    if d:
        shutil.rmtree(d, ignore_errors=True)
    ctx.user_data.clear()


def buttons(s: dict) -> InlineKeyboardMarkup:
    rows = []
    if s["images"]:
        rows.append([InlineKeyboardButton(f"📄 Make PDF ({len(s['images'])} photos)", callback_data="make")])
    if len(s["pdfs"]) >= 2:
        rows.append([InlineKeyboardButton(f"🔗 Merge {len(s['pdfs'])} PDFs", callback_data="merge")])
    if len(s["pdfs"]) == 1:
        rows.append([InlineKeyboardButton("🗜 Shrink this PDF", callback_data="compress")])
    rows.append([InlineKeyboardButton("🗑 Clear", callback_data="clear")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    await update.message.reply_text(WELCOME)


async def clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    await update.message.reply_text("Cleared. Send new files whenever you're ready.")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = session(ctx)
    if len(s["images"]) >= MAX_FILES:
        await update.message.reply_text(f"Max {MAX_FILES} photos at a time. Tap Make PDF first.")
        return
    msg = update.message
    if msg.photo:
        f = await msg.photo[-1].get_file()
    else:
        f = await msg.document.get_file()
    path = Path(s["dir"]) / f"img_{len(s['images']):03d}"
    await f.download_to_drive(path)
    s["images"].append(str(path))
    await msg.reply_text(f"Got photo {len(s['images'])}. Send more or tap Make PDF.",
                         reply_markup=buttons(s))


async def on_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = session(ctx)
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_MB * 1024 * 1024:
        await update.message.reply_text(f"That file is over {MAX_MB} MB, which Telegram won't let bots download.")
        return
    if len(s["pdfs"]) >= MAX_FILES:
        await update.message.reply_text(f"Max {MAX_FILES} PDFs at a time.")
        return
    f = await doc.get_file()
    path = Path(s["dir"]) / f"doc_{len(s['pdfs']):03d}.pdf"
    await f.download_to_drive(path)
    s["pdfs"].append(str(path))
    await update.message.reply_text(f"Got PDF {len(s['pdfs'])}: {doc.file_name}", reply_markup=buttons(s))


def images_to_pdf(paths, out):
    pages = []
    for p in paths:
        im = Image.open(p)
        im = im.convert("RGB")
        im.thumbnail((2000, 2000))
        pages.append(im)
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=150)


def merge_pdfs(paths, out):
    w = PdfWriter()
    for p in paths:
        w.append(p)
    with open(out, "wb") as fh:
        w.write(fh)


def compress_pdf(src, out):
    subprocess.run(["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4", "-dPDFSETTINGS=/ebook",
                    "-dNOPAUSE", "-dQUIET", "-dBATCH", f"-sOutputFile={out}", src],
                   check=True, timeout=120)


def mb(path):
    return os.path.getsize(path) / 1024 / 1024


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = session(ctx)
    action = q.data
    if action == "clear":
        reset(ctx)
        await q.message.reply_text("Cleared.")
        return
    out = Path(s["dir"]) / "result.pdf"
    try:
        if action == "make" and s["images"]:
            await q.message.reply_text("Making your PDF…")
            images_to_pdf(s["images"], out)
            await q.message.reply_document(open(out, "rb"), filename="photos.pdf",
                                           caption="Here's your PDF ✅")
        elif action == "merge" and len(s["pdfs"]) >= 2:
            await q.message.reply_text("Merging…")
            merge_pdfs(s["pdfs"], out)
            await q.message.reply_document(open(out, "rb"), filename="merged.pdf",
                                           caption="Merged ✅")
        elif action == "compress" and s["pdfs"]:
            await q.message.reply_text("Shrinking…")
            src = s["pdfs"][-1]
            compress_pdf(src, out)
            before, after = mb(src), mb(out)
            if after >= before:
                await q.message.reply_text("This PDF is already as small as I can make it.")
                return
            await q.message.reply_document(open(out, "rb"), filename="small.pdf",
                                           caption=f"Shrunk from {before:.1f} MB to {after:.1f} MB ✅")
        else:
            await q.message.reply_text("Send files first.")
            return
    except Exception as e:  # noqa
        log.exception("failed")
        await q.message.reply_text("Sorry, something went wrong with that file. Try another one.")
        return
    reset(ctx)
    await q.message.reply_text("Done! Send more files anytime. Share me with friends who need this 🙌")


def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, on_pdf))
    app.add_handler(CallbackQueryHandler(on_button))

    url = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
    if url:
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=token[-20:],
                        webhook_url=f"{url.rstrip('/')}/{token[-20:]}")
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
