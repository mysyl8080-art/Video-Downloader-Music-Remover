import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN", "")
MAX_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1800"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/video-bot"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("video-bot")


def allowed_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def download_video(url: str, folder: Path) -> Path:
    options = {
        "outtmpl": str(folder / "%(title).80s-%(id)s.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        candidate = Path(ydl.prepare_filename(info))
        if candidate.exists():
            return candidate
        mp4_candidate = candidate.with_suffix(".mp4")
        if mp4_candidate.exists():
            return mp4_candidate
        matches = list(folder.glob("*"))
        videos = [p for p in matches if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if videos:
            return videos[0]
        raise RuntimeError("Download completed but the output video was not found.")


def run_command(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Command failed")[-2500:])


def remove_music(video: Path, folder: Path) -> Path:
    audio = folder / "source_audio.wav"
    run_command([
        "ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "2",
        "-ar", "44100", str(audio),
    ])

    separated = folder / "separated"
    run_command([
        "python", "-m", "demucs", "--two-stems=vocals",
        "-n", os.getenv("DEMUCS_MODEL", "htdemucs"),
        "-o", str(separated), str(audio),
    ])

    vocal_candidates = list(separated.glob("**/vocals.wav"))
    if not vocal_candidates:
        raise RuntimeError("Demucs did not produce a vocals.wav file.")
    vocal_track = vocal_candidates[0]
    output = folder / f"{video.stem}-no-music.mp4"
    run_command([
        "ffmpeg", "-y", "-i", str(video), "-i", str(vocal_track),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(output),
    ])
    return output


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً بك!\n\n"
        "📥 ابعت رابط فيديو عام لتنزيله.\n"
        "🎙️ لإزالة الموسيقى: ابعت الفيديو كملف أو فيديو ثم استخدم /remove.\n\n"
        "الأوامر:\n/download رابط الفيديو\n/remove"
    )


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = " ".join(context.args).strip()
    if not allowed_url(url):
        await update.message.reply_text("ابعت رابط صحيح يبدأ بـ http أو https.")
        return
    status = await update.message.reply_text("⏳ جاري تنزيل الفيديو...")
    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as temp:
        folder = Path(temp)
        try:
            video = await asyncio.to_thread(download_video, url, folder)
            size_mb = video.stat().st_size / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                await status.edit_text(f"حجم الفيديو {size_mb:.0f} MB ويتجاوز الحد المحدد.")
                return
            await status.edit_text("✅ تم التنزيل. جاري الإرسال...")
            with video.open("rb") as stream:
                await update.message.reply_video(video=stream, supports_streaming=True)
            await status.delete()
        except Exception as exc:
            log.exception("Download failed")
            await status.edit_text(f"تعذر تنزيل الفيديو: {str(exc)[:700]}")


async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    media = message.video or message.document
    if not media:
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.reply_text("من فضلك ابعت ملف فيديو.")
        return
    context.user_data["pending_video"] = media.file_id
    await message.reply_text("تم استلام الفيديو. استخدم /remove لإزالة الموسيقى والإبقاء على الصوت البشري.")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    file_id = context.user_data.get("pending_video")
    if not file_id:
        await update.message.reply_text("ابعت الفيديو أولاً كفيديو أو ملف، وبعدها استخدم /remove.")
        return
    status = await update.message.reply_text("🎙️ جاري فصل الصوت البشري عن الموسيقى. قد تستغرق العملية بعض الوقت...")
    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as temp:
        folder = Path(temp)
        try:
            tg_file = await context.bot.get_file(file_id)
            source = folder / "input_video"
            await tg_file.download_to_drive(custom_path=str(source))
            output = await asyncio.to_thread(remove_music, source, folder)
            size_mb = output.stat().st_size / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                await status.edit_text(f"الفيديو الناتج حجمه {size_mb:.0f} MB ويتجاوز حد الإرسال.")
                return
            await status.edit_text("✅ انتهت المعالجة. جاري إرسال الفيديو...")
            with output.open("rb") as stream:
                await update.message.reply_video(video=stream, supports_streaming=True)
            context.user_data.pop("pending_video", None)
            await status.delete()
        except Exception as exc:
            log.exception("Music removal failed")
            await status.edit_text(f"تعذرت معالجة الفيديو: {str(exc)[:700]}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if allowed_url(text):
        context.args = text.split()
        await download_command(update, context)
    else:
        await update.message.reply_text("ابعت رابط الفيديو أو استخدم /start لمعرفة طريقة الاستخدام.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("download", download_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    log.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
