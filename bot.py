from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("media-cleaner")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "1000"))
MAX_DURATION_MINUTES = int(os.getenv("MAX_DURATION_MINUTES", "90"))
MAX_OUTPUT_MB = int(os.getenv("MAX_OUTPUT_MB", "48"))
DEMUCS_MODEL = os.getenv("DEMUCS_MODEL", "htdemucs").strip()
DEMUCS_SHIFTS = max(1, int(os.getenv("DEMUCS_SHIFTS", "2")))
WORKERS = max(1, int(os.getenv("WORKERS", "1")))
ALLOWED_USER_IDS = {
    int(part.strip()) for part in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if part.strip().isdigit()
}
JOB_SEMAPHORE = asyncio.Semaphore(WORKERS)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class UserFacingError(Exception):
    """An expected failure that can be explained to the requester."""


def run_checked(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UserFacingError("انتهت مهلة المعالجة. جرّب ملفًا أقصر أو أعد المحاولة لاحقًا.") from exc
    except FileNotFoundError as exc:
        raise UserFacingError(f"الأداة المطلوبة غير موجودة: {args[0]}. راجع إعداد Docker/README.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-1200:]
        log.warning("Command failed (%s): %s", args[0], detail)
        raise UserFacingError("تعذرت معالجة هذا الملف. قد يكون الترميز غير مدعوم أو الملف تالفًا.") from exc


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UserFacingError("أرسل رابطًا عامًا يبدأ بـ http:// أو https://.")
    if parsed.username or parsed.password:
        raise UserFacingError("الروابط التي تحتوي على اسم مستخدم أو كلمة مرور غير مدعومة.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise UserFacingError("هذا الرابط غير مسموح به لأسباب أمنية.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        if not addresses or any(not ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses):
            raise UserFacingError("الرابط يشير إلى عنوان شبكة خاص أو غير عام، لذلك تم رفضه.")
    except socket.gaierror as exc:
        raise UserFacingError("تعذر الوصول إلى اسم النطاق. تحقق من الرابط وحاول مجددًا.") from exc
    except ValueError as exc:
        raise UserFacingError("اسم النطاق أو المنفذ في الرابط غير صالح.") from exc
    return url


def _progress_guard(max_bytes: int):
    def hook(status: dict) -> None:
        if status.get("status") == "downloading":
            downloaded = status.get("downloaded_bytes", 0) or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            if downloaded > max_bytes or total > max_bytes:
                raise UserFacingError("حجم التنزيل أكبر من الحد المسموح به لهذا البوت.")
    return hook


def download_media(url: str, workdir: Path) -> tuple[Path, str]:
    url = validate_public_url(url)
    max_bytes = MAX_DOWNLOAD_MB * 1024 * 1024
    opts = {
        "format": "bestvideo*+bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "max_filesize": max_bytes,
        "merge_output_format": "mkv",
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_guard(max_bytes)],
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except UserFacingError:
        raise
    except Exception as exc:
        log.exception("yt-dlp failed")
        raise UserFacingError(
            "فشل تنزيل الرابط. تأكد أنه متاح علنًا وأن التنزيل مسموح به من المنصة؛ لا يدعم البوت تجاوز DRM أو تسجيل الدخول."
        ) from exc

    if not info:
        raise UserFacingError("لم تُرجع المنصة بيانات صالحة عن الملف.")
    if info.get("_type") in {"playlist", "multi_video"}:
        raise UserFacingError("أرسل رابط فيديو أو صوت واحدًا، وليس قائمة تشغيل.")
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_MINUTES * 60:
        raise UserFacingError(f"مدة الملف تتجاوز الحد الحالي ({MAX_DURATION_MINUTES} دقيقة).")

    candidates = [p for p in workdir.glob("source.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not candidates:
        raise UserFacingError("اكتمل طلب التنزيل لكن لم يُعثر على ملف وسائط صالح.")
    source = max(candidates, key=lambda p: p.stat().st_size)
    if source.stat().st_size > max_bytes:
        raise UserFacingError(f"حجم الملف يتجاوز الحد الحالي ({MAX_DOWNLOAD_MB} ميجابايت).")
    title = str(info.get("title") or "media")[:160]
    return source, title


def probe_media(source: Path) -> tuple[bool, float | None]:
    result = run_checked([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)
    ], timeout=60)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UserFacingError("تعذر قراءة معلومات الملف.") from exc
    streams = data.get("streams", [])
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    if not has_audio:
        raise UserFacingError("لم أجد مسارًا صوتيًا داخل الملف.")
    raw_duration = data.get("format", {}).get("duration")
    duration = float(raw_duration) if raw_duration else None
    if duration and duration > MAX_DURATION_MINUTES * 60:
        raise UserFacingError(f"مدة الملف تتجاوز الحد الحالي ({MAX_DURATION_MINUTES} دقيقة).")
    return has_video, duration


def remove_background_music(source: Path, workdir: Path) -> tuple[Path, bool]:
    has_video, _ = probe_media(source)
    input_wav = workdir / "audio_input.wav"
    run_checked([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(input_wav),
    ], timeout=1800)

    stems_dir = workdir / "stems"
    cmd = [
        "python", "-m", "demucs", "--two-stems=vocals", "-n", DEMUCS_MODEL,
        "--shifts", str(DEMUCS_SHIFTS), "-o", str(stems_dir), str(input_wav),
    ]
    # Demucs can take a while on CPU; model weights are cached by the container/user.
    try:
        run_checked(cmd, timeout=max(3600, int((MAX_DURATION_MINUTES or 90) * 120)))
    except UserFacingError as exc:
        raise UserFacingError(
            "فشل فصل الصوت. تأكد من توفر الذاكرة والمساحة، ثم جرّب ملفًا أقصر. أول تشغيل قد يستغرق وقتًا لتنزيل نموذج Demucs."
        ) from exc

    vocals = stems_dir / DEMUCS_MODEL / input_wav.stem / "vocals.wav"
    if not vocals.exists():
        raise UserFacingError("اكتمل الفصل دون العثور على مسار الكلام الناتج.")

    output = workdir / ("speech_only.mp4" if has_video else "speech_only.mp3")
    if not has_video:
        run_checked([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(vocals),
            "-vn", "-c:a", "libmp3lame", "-b:a", "320k", str(output),
        ], timeout=600)
    else:
        # Preserve the downloaded video stream without re-encoding. Fall back to Matroska
        # if the original video codec cannot be muxed into MP4.
        mp4_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-i", str(vocals),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
        try:
            run_checked(mp4_args, timeout=900)
        except UserFacingError:
            output = workdir / "speech_only.mkv"
            run_checked([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-i", str(vocals),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
                "-shortest", str(output),
            ], timeout=900)
    return output, has_video


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "أهلًا! أرسل رابطًا عامًا لفيديو أو صوت مسموح لك بتنزيله. سأحاول إزالة الموسيقى الخلفية وإبقاء الكلام.\n\n"
        "ملاحظة: الفصل بالذكاء الاصطناعي قد يترك بعض الموسيقى أو يؤثر قليلًا على الكلام، ولا يتجاوز DRM أو تسجيل الدخول.\n"
        "استخدم /help للمساعدة."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"أرسل رابطًا واحدًا في كل مرة. الحد الحالي: {MAX_DOWNLOAD_MB} ميجابايت تنزيل، "
        f"{MAX_DURATION_MINUTES} دقيقة، و{MAX_OUTPUT_MB} ميجابايت للملف الناتج.\n\n"
        "يقبل البوت الروابط التي تدعمها yt-dlp عندما تكون متاحة علنًا ومسموحًا بتنزيلها. لا يدعم قوائم التشغيل أو تجاوز DRM/تسجيل الدخول."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        await message.reply_text("هذا البوت خاص حاليًا وغير مفعّل لحسابك.")
        return
    match = URL_RE.search(message.text or "")
    if not match:
        await message.reply_text("لم أجد رابطًا. أرسل رابط فيديو أو صوت يبدأ بـ http:// أو https://")
        return
    url = match.group(0).rstrip(".,،؛!?)】]")
    await message.reply_text("وصل الرابط. سأضعه في قائمة المعالجة؛ قد يستغرق الفصل بعض الوقت.")
    context.application.create_task(
        process_request(message.chat_id, user.id, url, context.application.bot),
        update=update,
        name=f"media-job-{user.id}",
    )


async def process_request(chat_id: int, user_id: int, url: str, bot) -> None:
    async with JOB_SEMAPHORE:
        workdir = Path(tempfile.mkdtemp(prefix=f"tgmedia-{user_id}-"))
        status_message = None
        try:
            status_message = await bot.send_message(chat_id, "بدأت المعالجة الآن. التنزيل الأول لنموذج الفصل قد يجعل التشغيل الأول أبطأ.")
            source, title = await asyncio.to_thread(download_media, url, workdir)
            output, has_video = await asyncio.to_thread(remove_background_music, source, workdir)
            if output.stat().st_size > MAX_OUTPUT_MB * 1024 * 1024:
                raise UserFacingError(
                    f"الملف الناتج حجمه أكبر من حد Telegram المضبوط ({MAX_OUTPUT_MB} ميجابايت). "
                    "ارفع MAX_OUTPUT_MB فقط إذا كان خادم Bot API الذي تستخدمه يدعم ذلك."
                )
            caption = "تمت المعالجة: الكلام/الصوت الأمامي مع تقليل الموسيقى الخلفية.\n" + title[:700]
            with output.open("rb") as file_obj:
                await bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(file_obj, filename=output.name),
                    caption=caption[:1000],
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
            if status_message:
                await status_message.delete()
        except UserFacingError as exc:
            log.info("Job rejected/failed for user %s: %s", user_id, exc)
            try:
                if status_message:
                    await status_message.edit_text(str(exc))
                else:
                    await bot.send_message(chat_id, str(exc))
            except Exception:
                log.exception("Could not send failure notice")
        except Exception:
            log.exception("Unexpected processing failure")
            try:
                await bot.send_message(chat_id, "حدث خطأ غير متوقع أثناء المعالجة. راجع سجل الحاوية للتفاصيل.")
            except Exception:
                pass
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def post_init(application: Application) -> None:
    log.info("Bot started; workers=%s model=%s", WORKERS, DEMUCS_MODEL)


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set the token.")
    for command in ("ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            raise SystemExit(f"{command} is required but was not found on PATH")
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
