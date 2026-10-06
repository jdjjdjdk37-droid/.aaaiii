import html
import logging
import mimetypes
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import telebot
import yt_dlp
from bs4 import BeautifulSoup
from telebot import types

try:
    import imageio_ffmpeg
    FFMPEG_BINARY = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_BINARY = shutil.which("ffmpeg") or "ffmpeg"

# ضع توكن البوت هنا بين علامتي الاقتباس، ثم شغّل الملف مباشرة.
# مثال: BOT_TOKEN = "1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
BOT_TOKEN = "8695860206:AAHL-FDu68ci0Fw_us3qC7vXJohc_RqP-38"
# اكتب اسم أو يوزر البوت هنا ليظهر في اسم ملف الصوت.
BOT_USERNAME = "@shoo_sbot"
DEVELOPER_USERNAME = "@to_ls"

# صفر = بلا حد حجم داخلي. تبقى حدود Telegram وموارد الخادم قائمة.
MAX_FILE_SIZE_MB = 0
MAX_FILE_SIZE = 0
DOWNLOAD_TIMEOUT = 180

# توكن جلسة Showgram، وليس توكن بوت تليجرام.
# خذه من ترويسة authorization في جلسة Showgram المصرّح بها.
# اتركه فارغًا إذا كنت لا تريد دعم Showgram.
SHOWGRAM_AUTH_TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI2YWM0ZmEzYTBhODFmM2JkNDgwYTZjY2UiLCJzaWQiOiI2YWM0ZmEzY2ZkMDNkODA5ZDdlZDYyYzUiLCJpYXQiOjE3OTEyOTQwMTIsImV4cCI6MTc5Mzg4NjAxMn0.Yspy71evgAF23yhuxFfrXia-WFLksglkB8sQkxoBnpU'
SHOWGRAM_API_BASE = "https://api.showgram.app"

if not BOT_TOKEN or BOT_TOKEN == "ضع_توكن_البوت_هنا":
    raise RuntimeError("افتح bot.py وضع توكن البوت في المتغير BOT_TOKEN أولًا")

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("media-bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
USER_QUEUES: dict[int, queue.Queue] = {}
USER_WORKERS: dict[int, threading.Thread] = {}
USER_QUEUES_LOCK = threading.Lock()
MEDIA_ACTIONS: dict[str, tuple[int, str, float]] = {}
MEDIA_ACTIONS_LOCK = threading.Lock()


def clean_url(raw: str) -> str:
    return raw.strip().rstrip(".,!?،؛)]}>")


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return clean_url(match.group(0)) if match else None


def safe_name(value: str, fallback: str = "media") -> str:
    value = re.sub(r"[\\/:*?\"<>|\n\r]+", "_", value).strip(" .")
    return (value[:90] or fallback)


def showgram_hint(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "showgram.app" in host or "demoda.app" in host:
        return (
            "هذا رابط Showgram. لتنزيله ضع توكن جلسة Showgram في المتغير "
            "SHOWGRAM_AUTH_TOKEN داخل الكود (وليس توكن بوت تليجرام)."
        )
    return None


def is_showgram_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("showgram.app") or host.endswith("demoda.app")


def get_showgram_media(url: str) -> tuple[str, str]:
    """يجلب رابط الوسائط الأصلي من API الرسمي دون تنزيله على الخادم."""
    if not SHOWGRAM_AUTH_TOKEN.strip():
        raise RuntimeError(showgram_hint(url))
    match = re.search(r"/r/([A-Za-z0-9_-]+)", urlparse(url).path)
    if not match:
        raise RuntimeError("تعذر قراءة معرّف ريل Showgram من الرابط")
    reel_id = match.group(1)
    headers = {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "application/json, text/plain, */*",
        "x-showgram-build": "61",
        "x-showgram-version": "1.0.18",
        "authorization": "Bearer " + SHOWGRAM_AUTH_TOKEN.strip().removeprefix("Bearer ").strip(),
    }
    response = requests.get(
        f"{SHOWGRAM_API_BASE}/api/reels/{reel_id}",
        headers=headers,
        timeout=DOWNLOAD_TIMEOUT,
    )
    if response.status_code in (401, 403):
        raise RuntimeError("توكن Showgram غير صالح أو منتهي؛ أدخل توكن جلسة حديثًا")
    response.raise_for_status()
    data = response.json()
    reel = data.get("reel") or data
    # نفضّل الملف الأصلي، ثم نبحث عن الصوت/الصورة/الملف في بنية المنشور.
    media_url = (
        reel.get("videoUrl")
        or reel.get("videoUrlSd")
        or reel.get("videoUrlLow")
        or reel.get("audioUrl")
        or reel.get("mediaUrl")
        or reel.get("fileUrl")
        or reel.get("imageUrl")
        or reel.get("photoUrl")
    )
    if not media_url and isinstance(reel.get("images"), list):
        for image in reel["images"]:
            if isinstance(image, str) and image.startswith("http"):
                media_url = image
                break
            if isinstance(image, dict):
                media_url = image.get("url") or image.get("src")
                if media_url:
                    break
    if not media_url:
        raise RuntimeError("لم يُرجع Showgram رابط الفيديو الأصلي لهذا الريل")
    return media_url, reel.get("caption") or reel.get("title") or "Showgram Reel"


def download_showgram(url: str, folder: str) -> tuple[Path, str]:
    """Fallback: تنزيل رابط Showgram الأصلي محليًا إذا رفض Telegram الرابط المباشر."""
    media_url, title = get_showgram_media(url)
    media_response = requests.get(
        media_url,
        headers={"User-Agent": "Showgram/1.0.18 (Linux;Android)"},
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
    )
    media_response.raise_for_status()
    path, _ = stream_media(media_url, media_response, folder)
    return path, title


def download_with_ytdlp(url: str, folder: str) -> tuple[Path, str]:
    output = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    opts = {
        "outtmpl": output,
        "format": "best[ext=mp4]/best[ext=webm]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 2,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Telegram Media Bot)"},
    }
    if MAX_FILE_SIZE:
        opts["max_filesize"] = MAX_FILE_SIZE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or "media"
    files = [p for p in Path(folder).iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("لم يتم إنشاء ملف بعد التنزيل")
    path = max(files, key=lambda p: p.stat().st_mtime)
    return path, title


def extract_direct_media(url: str, folder: str) -> tuple[Path, str] | None:
    """Fallback للصفحات التي تضع رابط الفيديو في og:video أو source."""
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Telegram Media Bot)"},
        timeout=DOWNLOAD_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith(("video/", "image/")):
        media_url = response.url
        return stream_media(media_url, response, folder)

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        value = tag.get("content")
        if value and key in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
            candidates.append(value)
    for tag in soup.find_all(["video", "source"]):
        value = tag.get("src") or tag.get("href")
        if value:
            candidates.append(value)

    for candidate in candidates:
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith("/"):
            candidate = requests.compat.urljoin(response.url, candidate)
        try:
            media_response = requests.get(
                candidate,
                headers={"User-Agent": "Mozilla/5.0 (Telegram Media Bot)"},
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            )
            media_response.raise_for_status()
            if media_response.headers.get("content-type", "").lower().startswith(("video/", "image/")):
                return stream_media(candidate, media_response, folder)
            media_response.close()
        except requests.RequestException:
            continue
    return None


def stream_media(url: str, response: requests.Response, folder: str) -> tuple[Path, str]:
    content_type = response.headers.get("content-type", "").split(";")[0].lower()
    length = int(response.headers.get("content-length", "0") or 0)
    if MAX_FILE_SIZE and length > MAX_FILE_SIZE:
        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} MB)")
    ext = mimetypes.guess_extension(content_type) or Path(urlparse(url).path).suffix or ".mp4"
    if ext == ".jpe":
        ext = ".jpg"
    path = Path(folder) / f"download{ext}"
    total = 0
    with path.open("wb") as out:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            total += len(chunk)
            if MAX_FILE_SIZE and total > MAX_FILE_SIZE:
                path.unlink(missing_ok=True)
                raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} MB)")
            out.write(chunk)
    return path, "media"


def fetch_media(url: str, folder: str) -> tuple[Path, str]:
    if is_showgram_url(url):
        return download_showgram(url, folder)
    try:
        return download_with_ytdlp(url, folder)
    except Exception as ytdlp_error:
        logger.info("yt-dlp failed for %s: %s; trying direct media fallback", url, ytdlp_error)
        direct = extract_direct_media(url, folder)
        if direct:
            return direct
        hint = showgram_hint(url)
        if hint:
            raise RuntimeError(hint) from ytdlp_error
        raise RuntimeError(
            "تعذر استخراج ملف وسائط من الرابط. قد يكون الرابط خاصًا، أو يتطلب تسجيل دخول، "
            "أو محميًا بـ DRM، أو غير مدعوم."
        ) from ytdlp_error


def remember_media_action(user_id: int, source_url: str) -> str:
    token = secrets.token_urlsafe(8)
    with MEDIA_ACTIONS_LOCK:
        MEDIA_ACTIONS[token] = (user_id, source_url, time.time() + 3600)
    return token


def media_keyboard(chat_id: int, source_url: str):
    keyboard = types.InlineKeyboardMarkup()
    action = remember_media_action(chat_id, source_url)
    keyboard.row(
        types.InlineKeyboardButton("🎵 MP3", callback_data=f"audio:{action}"),
        types.InlineKeyboardButton("🎙 رسالة صوتية", callback_data=f"voice:{action}"),
    )
    return keyboard


def send_result(
    chat_id: int,
    path: Path,
    title: str,
    source_url: str | None = None,
    send_as_voice: bool = False,
    caption: str | None = None,
):
    size = path.stat().st_size
    if MAX_FILE_SIZE and size > MAX_FILE_SIZE:
        raise ValueError(f"حجم الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} MB)")
    ext = path.suffix.lower()
    keyboard = None
    if ext in VIDEO_EXTS and source_url:
        keyboard = media_keyboard(chat_id, source_url)
    with path.open("rb") as media:
        if ext in VIDEO_EXTS:
            return bot.send_video(
                chat_id, media, supports_streaming=True, reply_markup=keyboard,
                caption=caption, timeout=DOWNLOAD_TIMEOUT,
            )
        elif ext in IMAGE_EXTS:
            return bot.send_photo(chat_id, media, caption=caption, timeout=DOWNLOAD_TIMEOUT)
        elif ext in AUDIO_EXTS:
            if send_as_voice:
                return bot.send_voice(chat_id, media, caption=caption, timeout=DOWNLOAD_TIMEOUT)
            return bot.send_audio(chat_id, media, caption=caption, timeout=DOWNLOAD_TIMEOUT)
        else:
            return bot.send_document(chat_id, media, caption=caption, timeout=DOWNLOAD_TIMEOUT)


def send_direct_result(chat_id: int, media_url: str, source_url: str, caption: str | None = None):
    """يجعل Telegram يسحب الملف من CDN مباشرة بدل مرور الملف على الخادم."""
    suffix = Path(urlparse(media_url).path).suffix.lower()
    if suffix in IMAGE_EXTS:
        return bot.send_photo(chat_id, media_url, caption=caption, timeout=DOWNLOAD_TIMEOUT)
    if suffix in AUDIO_EXTS:
        return bot.send_audio(chat_id, media_url, caption=caption, timeout=DOWNLOAD_TIMEOUT)
    return bot.send_video(
        chat_id,
        media_url,
        supports_streaming=True,
        reply_markup=media_keyboard(chat_id, source_url),
        caption=caption,
        timeout=DOWNLOAD_TIMEOUT,
    )


def format_size(size: int | None) -> str:
    if not size or size <= 0:
        return "غير متاح"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "غير متاح"


def file_name_from_url(media_url: str, fallback: str = "Showgram_Reel") -> str:
    raw_name = Path(unquote(urlparse(media_url).path)).name
    if not raw_name or raw_name in ("/", "."):
        raw_name = fallback
    return safe_name(raw_name)


def build_file_caption(
    title: str,
    extension: str,
    size: int | None = None,
    file_name: str | None = None,
    description: str | None = None,
) -> str:
    description_line = (
        f"\nالوصف: {html.escape(description[:1000])}"
        if description and description.strip()
        else ""
    )
    return (
        "<b>ℹ️ معلومات الملف</b>\n"
        f"<blockquote>النوع: {html.escape(extension.upper().lstrip('.') or 'FILE')}\n"
        f"الحجم: {format_size(size)}{description_line}</blockquote>\n"
        f"👨‍💻 المطور: {html.escape(DEVELOPER_USERNAME)}"
    )


def animate_status(chat_id: int, message_id: int, stop_event: threading.Event) -> None:
    """يعرض حركة نصية خفيفة أثناء التنزيل دون إغراق Telegram بالطلبات."""
    frames = [
        "<b>⏳ جاري العمل</b>\n<blockquote>جاري تحليل الرابط ⋅</blockquote>",
        "<b>⏳ جاري العمل</b>\n<blockquote>جاري الاتصال بالمصدر ⋅⋅</blockquote>",
        "<b>⏳ جاري العمل</b>\n<blockquote>جاري تنزيل الوسائط ⋅⋅⋅</blockquote>",
        "<b>⏳ جاري العمل</b>\n<blockquote>جاري تجهيز الملف ⋅⋅⋅⋅</blockquote>",
    ]
    index = 0
    while not stop_event.wait(1.3):
        try:
            bot.edit_message_text(frames[index % len(frames)], chat_id, message_id)
            index += 1
        except Exception:
            # لا نوقف عملية التنزيل إذا تعذر تحديث رسالة الحالة.
            pass


def process_message(message: types.Message, url: str) -> None:
    status = bot.reply_to(
        message,
        "<b>⏳ جاري تجهيز طلبك...</b>\n"
        "<blockquote>سيتم إرسال الملف هنا بعد اكتمال التحميل.</blockquote>",
    )
    stop_animation = threading.Event()
    animation = threading.Thread(
        target=animate_status,
        args=(message.chat.id, status.id, stop_animation),
        daemon=True,
    )
    animation.start()
    folder = tempfile.mkdtemp(prefix="telegram-media-")
    try:
        if is_showgram_url(url):
            media_url, title = get_showgram_media(url)
            try:
                stop_animation.set()
                bot.edit_message_text(
                    "<b>⚡ رابط مباشر جاهز</b>\n<blockquote>📤 جاري الإرسال السريع...</blockquote>",
                    message.chat.id,
                    status.id,
                )
                direct_caption = build_file_caption(
                    title,
                    Path(urlparse(media_url).path).suffix,
                    file_name=file_name_from_url(media_url),
                    description=title,
                )
                send_direct_result(message.chat.id, media_url, url, caption=direct_caption)
                bot.delete_message(message.chat.id, status.id)
                return
            except Exception as direct_error:
                logger.info("Direct Telegram delivery failed; using local fallback: %s", direct_error)
                stop_animation.clear()
        path, title = fetch_media(url, folder)
        stop_animation.set()
        bot.edit_message_text(
            "<b>✅ اكتمل التحميل</b>\n<blockquote>📤 جاري إرسال الملف...</blockquote>",
            message.chat.id,
            status.id,
        )
        local_caption = build_file_caption(
            title,
            path.suffix,
            size=path.stat().st_size,
            file_name=path.name,
            description=title if is_showgram_url(url) else None,
        )
        send_result(message.chat.id, path, title, source_url=url, caption=local_caption)
        bot.delete_message(message.chat.id, status.id)
    except ValueError as exc:
        stop_animation.set()
        bot.edit_message_text(f"⚠️ {html.escape(str(exc))}", message.chat.id, status.id)
    except Exception as exc:
        stop_animation.set()
        logger.exception("Processing failed")
        bot.edit_message_text(
            f"<b>❌ تعذر إكمال الطلب</b>\n<blockquote>{html.escape(str(exc))}</blockquote>",
            message.chat.id,
            status.id,
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def extract_audio(url: str, folder: str, as_voice: bool = False) -> Path:
    source_path, _ = fetch_media(url, folder)
    brand = safe_name(BOT_USERNAME.lstrip("@"), "ShowgramBot")
    audio_path = Path(folder) / (f"{brand}_voice.ogg" if as_voice else f"{brand}_audio.mp3")
    if not Path(FFMPEG_BINARY).exists() and not shutil.which(FFMPEG_BINARY):
        raise RuntimeError(
            "أداة FFmpeg غير متوفرة. شغّل pip install -r requirements.txt ثم أعد المحاولة."
        )
    result = subprocess.run(
        [
            FFMPEG_BINARY, "-y", "-i", str(source_path), "-vn",
            *(["-codec:a", "libopus", "-b:a", "48k", "-vbr", "on", "-application", "voip"]
              if as_voice else ["-codec:a", "libmp3lame", "-q:a", "2"]),
            str(audio_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=DOWNLOAD_TIMEOUT,
    )
    if result.returncode != 0 or not audio_path.exists():
        raise RuntimeError("لم يتم العثور على مسار صوت داخل هذا الملف")
    return audio_path


def process_audio(message: types.Message, url: str, as_voice: bool = False) -> None:
    label = "رسالة صوتية" if as_voice else "ملف MP3"
    status = bot.reply_to(
        message,
        f"<b>🎵 جاري تجهيز {label}...</b>\n"
        f"<blockquote>سيتم إرسال {label} بعد اكتمال المعالجة.</blockquote>",
    )
    folder = tempfile.mkdtemp(prefix="telegram-audio-")
    try:
        path = extract_audio(url, folder, as_voice=as_voice)
        bot.edit_message_text("<b>✅ تم استخراج الصوت</b>\n<blockquote>📤 جاري الإرسال...</blockquote>", message.chat.id, status.id)
        audio_caption = build_file_caption(
            "Audio", path.suffix, path.stat().st_size, file_name=path.name
        )
        send_result(message.chat.id, path, "Audio", send_as_voice=as_voice, caption=audio_caption)
        bot.delete_message(message.chat.id, status.id)
    except Exception as exc:
        logger.exception("Audio extraction failed")
        bot.edit_message_text(
            f"<b>❌ تعذر استخراج الصوت</b>\n<blockquote>{html.escape(str(exc))}</blockquote>",
            message.chat.id,
            status.id,
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def user_queue_worker(user_id: int, user_queue: queue.Queue) -> None:
    while True:
        task_type, message, url = user_queue.get()
        try:
            if task_type in ("audio", "voice"):
                process_audio(message, url, as_voice=(task_type == "voice"))
            else:
                process_message(message, url)
        except Exception:
            logger.exception("Queue task failed")
        finally:
            user_queue.task_done()
            with USER_QUEUES_LOCK:
                if user_queue.empty():
                    USER_QUEUES.pop(user_id, None)
                    USER_WORKERS.pop(user_id, None)
                    return


def enqueue_task(user_id: int, task_type: str, message: types.Message, url: str) -> None:
    with USER_QUEUES_LOCK:
        user_queue = USER_QUEUES.setdefault(user_id, queue.Queue())
        user_queue.put((task_type, message, url))
        worker = USER_WORKERS.get(user_id)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=user_queue_worker,
                args=(user_id, user_queue),
                daemon=True,
                name=f"download-user-{user_id}",
            )
            USER_WORKERS[user_id] = worker
            worker.start()


def cleanup_temp_files() -> None:
    temp_root = Path(tempfile.gettempdir())
    prefixes = ("telegram-media-", "telegram-audio-")
    while True:
        now = time.time()
        for prefix in prefixes:
            for path in temp_root.glob(prefix + "*"):
                try:
                    if path.is_dir() and now - path.stat().st_mtime > 3600:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass
        time.sleep(1800)


@bot.callback_query_handler(func=lambda call: call.data.startswith(("audio:", "voice:")))
def audio_button(call: types.CallbackQuery) -> None:
    mode, token = call.data.split(":", 1)
    with MEDIA_ACTIONS_LOCK:
        action = MEDIA_ACTIONS.get(token)
        if action and action[2] < time.time():
            MEDIA_ACTIONS.pop(token, None)
            action = None
    if not action:
        bot.answer_callback_query(call.id, "انتهت صلاحية هذا الزر، أرسل الرابط من جديد.", show_alert=True)
        return
    owner_id, url, _ = action
    if call.from_user.id != owner_id:
        bot.answer_callback_query(call.id, "هذا الزر ليس خاصًا بك.", show_alert=True)
        return
    enqueue_task(call.from_user.id, mode, call.message, url)
    bot.answer_callback_query(call.id, "🎵 سيتم تجهيز الصوت بعد قليل")


@bot.message_handler(commands=["start", "help"])
def welcome(message: types.Message) -> None:
    bot.reply_to(
        message,
        "<b>مرحبًا بك في بوت تنزيل ريلز Showgram</b>\n\n"
        "<blockquote>أرسل رابط Showgram، وسيحاول البوت تنزيل النسخة الأصلية بدون العلامة المائية التي يضيفها التطبيق في مسار التنزيل.</blockquote>\n\n"
        "<b>طريقة الاستخدام:</b>\n"
        "1) أرسل الرابط.\n"
        "2) انتظر اكتمال التحميل.\n"
        "3) اختر <b>🎵 MP3</b> أو <b>🎙 رسالة صوتية</b> أسفل الفيديو.\n\n"
        "<b>يدعم:</b> الفيديو، الصور، الصوت، والملفات.\n"
        "<i>لا يدعم الحسابات الخاصة أو DRM أو الروابط التي تتطلب صلاحيات غير متوفرة.</i>",
    )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_text(message: types.Message) -> None:
    url = extract_url(message.text)
    if not url:
        bot.reply_to(message, "أرسل رابطًا يبدأ بـ http:// أو https://")
        return
    enqueue_task(message.from_user.id, "video", message, url)


if __name__ == "__main__":
    threading.Thread(target=cleanup_temp_files, daemon=True, name="temp-cleaner").start()
    logger.info("Bot is running")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
