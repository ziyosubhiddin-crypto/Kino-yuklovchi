import asyncio
import os
import subprocess
import shutil
import libtorrent as lt
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from dotenv import load_dotenv

# dotenv yuklash
load_dotenv()

# ─── SOZLAMALAR ───────────────────────────────────────────────
API_TOKEN  = os.getenv("API_TOKEN", "8780158367:AAF1XlP59C4RzArFs9l5mo--eYw4-xdm1X0")
SAVE_PATH  = "./downloads"

# To'liq bot papkasi va skriptlari
TOLIQ_BOT_DIR  = "/Users/ziyodullo/Desktop/To'liq bot"
MOVIES_DIR     = os.path.join(TOLIQ_BOT_DIR, "movies")
SRT_CLEANER    = os.path.join(TOLIQ_BOT_DIR, "srt_cleaner.py")
UPLOADER       = os.path.join(TOLIQ_BOT_DIR, "uploader.py")
PYTHON         = "/Users/ziyodullo/.pyenv/versions/3.11.8/bin/python"
# ──────────────────────────────────────────────────────────────

bot = Bot(token=API_TOKEN)
dp  = Dispatcher()

# Holat 1: torrent yuklanib bo'lgandan keyin kino nomi kutiladi
pending_name: dict[int, dict] = {}

# Holat 2: kino nomi kiritilgandan keyin SRT/VTT kutiladi
pending_srt: dict[int, dict] = {}


def vtt_to_srt(vtt_path: str) -> str:
    """VTT faylni SRT formatiga o'giradi va yangi .srt fayl yo'lini qaytaradi."""
    import re as _re
    srt_path = vtt_path.rsplit(".", 1)[0] + ".srt"
    with open(vtt_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # WEBVTT sarlavhasini va NOTE/STYLE bloklarini olib tashlaymiz
    content = _re.sub(r"^WEBVTT.*?\n", "", content)
    content = _re.sub(r"NOTE[^\n]*\n(?:.*\n)*?\n", "", content)
    content = _re.sub(r"STYLE\n(?:.*\n)*?\n", "", content)

    blocks = [b.strip() for b in _re.split(r"\n{2,}", content) if b.strip()]
    srt_blocks = []
    index = 1
    for block in blocks:
        lines = block.splitlines()
        # Cue ID satrini o'tkazib yuboramiz (raqam yoki matn)
        if lines and not _re.match(r"\d{2}[:\.]\d{2}", lines[0]):
            lines = lines[1:]
        if not lines:
            continue
        # Timestamp qatorini topamiz
        ts_line = lines[0]
        if "-->" not in ts_line:
            continue
        # VTT: 00:00:01.000 --> 00:00:02.000  →  SRT: 00:00:01,000 --> 00:00:02,000
        ts_line = _re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", ts_line)
        # Qisqa format: MM:SS.mmm → 00:MM:SS,mmm
        ts_line = _re.sub(
            r"(?<![\d:])(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})",
            r"00:\g<m>:\g<s>,\g<ms>", ts_line
        )
        # Cue settings (align: center va boshqalar)ni olib tashlaymiz
        ts_line = ts_line.split(" line:")[0].split(" position:")[0].split(" align:")[0].strip()
        text_lines = lines[1:]
        if not text_lines:
            continue
        srt_blocks.append(f"{index}\n{ts_line}\n" + "\n".join(text_lines))
        index += 1

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(srt_blocks) + "\n")
    return srt_path

os.makedirs(SAVE_PATH,  exist_ok=True)
os.makedirs(MOVIES_DIR, exist_ok=True)


def get_video_file(path: str) -> str | None:
    """Papka ichidagi eng katta video faylni topadi."""
    video_extensions = ('.mp4', '.mkv', '.avi', '.mov')
    files = []
    for root, _, filenames in os.walk(path):
        for f in filenames:
            if f.lower().endswith(video_extensions):
                files.append(os.path.join(root, f))
    return max(files, key=os.path.getsize) if files else None


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024


async def download_torrent(source: str, status_msg: types.Message) -> tuple[str | None, str]:
    """Torrentni yuklab oladi. (video_path, torrent_name) qaytaradi."""
    ses = lt.session()
    ses.listen_on(6881, 6891)

    params = lt.add_torrent_params()
    params.save_path = SAVE_PATH

    if source.startswith("magnet:"):
        params = lt.parse_magnet_uri(source)
        params.save_path = SAVE_PATH
    else:
        info = lt.torrent_info(source)
        params.ti = info

    handle = ses.add_torrent(params)

    await status_msg.edit_text("⏳ Metama'lumotlar olinmoqda...")
    while not handle.has_metadata():
        await asyncio.sleep(1)

    torrent_name = handle.status().name
    await status_msg.edit_text(f"🚀 Yuklash boshlandi: **{torrent_name}**")

    last_update = 0
    while not handle.is_seed():
        s = handle.status()
        progress = s.progress * 100
        if progress - last_update > 5 or progress >= 100:
            try:
                await status_msg.edit_text(
                    f"📂 **Fayl:** {torrent_name}\n"
                    f"📊 **Progress:** {progress:.1f}%\n"
                    f"📥 **Tezlik:** {s.download_rate / 1000:.1f} kB/s\n"
                    f"👥 **Peerlar:** {s.num_peers}"
                )
                last_update = progress
            except Exception:
                pass
        await asyncio.sleep(5)

    video_path = get_video_file(os.path.join(SAVE_PATH, torrent_name))
    return video_path, torrent_name


async def run_pipeline(
    chat_id: int,
    video_path: str,
    srt_path: str,
    status_msg: types.Message,
    custom_name: str | None = None,
):
    """srt_cleaner → uploader pipeline ni ishga tushiradi."""
    video_ext  = os.path.splitext(os.path.basename(video_path))[1]  # .mp4 / .mkv
    movie_stem = custom_name if custom_name else os.path.splitext(os.path.basename(video_path))[0]

    # 1. Fayllarni movies/ papkasiga ko'chirish (custom nom bilan)
    dest_video = os.path.join(MOVIES_DIR, f"{movie_stem}{video_ext}")
    dest_srt   = os.path.join(MOVIES_DIR, f"{movie_stem}.srt")

    await status_msg.edit_text("📁 Fayllar ko'chirilmoqda...")
    shutil.move(video_path, dest_video)
    shutil.move(srt_path,   dest_srt)

    # 2. SRT tozalash
    await status_msg.edit_text("🧹 Subtitr tozalanmoqda...")
    result = subprocess.run(
        [PYTHON, SRT_CLEANER, dest_srt, "--in-place"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        await status_msg.edit_text(f"❌ srt_cleaner xatosi:\n{result.stderr[:300]}")
        return

    # 3. Telegramga yuklash
    await status_msg.edit_text(
        f"🚀 Telegramga yuklanmoqda...\n"
        f"📽 Kino: **{movie_stem}**\n"
        f"⏳ Bu jarayon ancha vaqt olishi mumkin..."
    )
    result = subprocess.run(
        [PYTHON, UPLOADER, dest_video, dest_srt],
        capture_output=True, text=True,
        cwd=TOLIQ_BOT_DIR
    )
    if result.returncode != 0:
        await status_msg.edit_text(f"❌ uploader xatosi:\n{result.stderr[:500]}")
        return

    await status_msg.edit_text(
        f"✅ **{movie_stem}** muvaffaqiyatli yuklandi!\n"
        f"🎬 Barcha kliplar bazaga qo'shildi."
    )

    # 4. Diskni tozalash (video o'chiriladi, SRT saqlanadi)
    try:
        os.remove(dest_video)
    except Exception:
        pass


# ─── HANDLERS ─────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "🎬 Kino yuklovchi botga xush kelibsiz!\n\n"
        "**Qanday foydalanish:**\n"
        "1️⃣ Magnet-link yoki .torrent faylini yuboring\n"
        "2️⃣ Yuklab bo'lgandan keyin kino nomini kiriting\n"
        "3️⃣ SRT yoki VTT subtitr faylini yuboring\n"
        "4️⃣ Bot avtomatik tozalab, Telegramga yuklaydi"
    )


@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    chat_id = message.chat.id
    if chat_id in pending_name:
        job = pending_name[chat_id]
        await message.answer(
            f"⏳ **{job['torrent_name']}** yuklab olindi.\n"
            "🎬 Kino nomini kiriting (matn yuboring)."
        )
    elif chat_id in pending_srt:
        job = pending_srt[chat_id]
        await message.answer(
            f"⏳ **{job['custom_name']}** uchun SRT/VTT fayl kutilmoqda.\n"
            "Iltimos, subtitr faylini yuboring."
        )
    else:
        await message.answer("✅ Hozir kutilayotgan jarayon yo'q.\nYangi torrent yuboring.")


@dp.message(F.text.startswith("magnet:") | F.document.func(lambda d: d.file_name.endswith(".torrent")))
async def handle_torrent(message: types.Message):
    chat_id = message.chat.id
    # Agar avvalgi jarayon tugallanmagan bo'lsa
    if chat_id in pending_name:
        await message.answer(
            "⚠️ Kino nomini kiritishingiz kutilmoqda!\n"
            f"📽 Torrent: **{pending_name[chat_id]['torrent_name']}**\n\n"
            "Avval kino nomini yuboring."
        )
        return
    if chat_id in pending_srt:
        await message.answer(
            "⚠️ Siz avval yuklagan kinoning subtitr fayli kutilmoqda!\n"
            f"📽 Kino: **{pending_srt[chat_id]['custom_name']}**\n\n"
            "Avval SRT/VTT faylini yuboring."
        )
        return

    status_msg = await message.answer("🔄 Jarayon boshlanmoqda...")

    try:
        source = ""
        if message.document:
            file   = await bot.get_file(message.document.file_id)
            source = f"temp_{message.document.file_name}"
            await bot.download_file(file.file_path, source)
        else:
            source = message.text

        video_path, torrent_name = await download_torrent(source, status_msg)

        if video_path:
            file_size = os.path.getsize(video_path)
            # Endi kino nomini foydalanuvchidan so'raymiz
            pending_name[chat_id] = {
                "video_path":   video_path,
                "torrent_name": torrent_name,
            }
            await status_msg.edit_text(
                f"✅ **{torrent_name}** yuklab olindi!\n"
                f"📦 Hajmi: {format_size(file_size)}\n\n"
                "🎬 **Kino nomini kiriting** (masalan: `Kong Skull Island 2017`):"
            )
        else:
            await status_msg.edit_text("❌ Video fayl topilmadi.")

    except Exception as e:
        await status_msg.edit_text(f"‼️ Xatolik: {str(e)}")
        # Temp torrent faylini tozalash
        if "source" in locals() and os.path.exists(source) and source.startswith("temp_"):
            os.remove(source)


@dp.message(F.text & ~F.text.startswith("magnet:"))
async def handle_movie_name(message: types.Message):
    """Foydalanuvchi kino nomini kiritganda ishlaydi."""
    chat_id = message.chat.id
    if chat_id not in pending_name:
        return  # bu handler faqat nom kutilayotganda ishlaydi

    job         = pending_name.pop(chat_id)
    custom_name = message.text.strip()

    if not custom_name:
        pending_name[chat_id] = job  # qaytaramiz
        await message.answer("❌ Kino nomi bo'sh bo'lmasin. Qaytadan kiriting:")
        return

    # Nom saqlandi, endi SRT kutiladi
    pending_srt[chat_id] = {
        "video_path":  job["video_path"],
        "torrent_name": job["torrent_name"],
        "custom_name": custom_name,
    }
    await message.answer(
        f"✅ Nom saqlandi: **{custom_name}**\n\n"
        "📄 Endi **SRT yoki VTT subtitr faylini** yuboring."
    )


@dp.message(F.document.func(lambda d: d.file_name.endswith((".srt", ".vtt"))))
async def handle_srt(message: types.Message):
    if message.chat.id not in pending_srt:
        await message.answer(
            "❌ SRT fayl qabul qilish uchun avval torrent yuboring!\n"
            "/start — yordam"
        )
        return

    job        = pending_srt.pop(message.chat.id)
    custom_name = job.get("custom_name")
    status_msg = await message.answer("📥 SRT fayl yuklanmoqda...")

    try:
        # Subtitr faylini vaqtinchalik saqlaymiz
        file      = await bot.get_file(message.document.file_id)
        orig_name = message.document.file_name
        sub_path  = f"temp_{orig_name}"
        await bot.download_file(file.file_path, sub_path)

        # VTT bo'lsa, SRT ga o'giramiz
        if orig_name.lower().endswith(".vtt"):
            await status_msg.edit_text("🔄 VTT → SRT formatiga o'girilmoqda...")
            srt_path = vtt_to_srt(sub_path)
            import os as _os
            _os.remove(sub_path)   # vaqtinchalik .vtt ni o'chiramiz
        else:
            srt_path = sub_path

        ext = os.path.splitext(orig_name)[1].upper()
        await status_msg.edit_text(
            f"✅ {ext} subtitr qabul qilindi!\n"
            f"📽 Kino: **{custom_name}**\n"
            "🔄 Pipeline ishga tushmoqda..."
        )

        # Pipeline ni fon rejimida ishlatamiz (bot qotib qolmasin)
        asyncio.create_task(
            run_pipeline(message.chat.id, job["video_path"], srt_path, status_msg,
                         custom_name=custom_name)
        )

    except Exception as e:
        await status_msg.edit_text(f"‼️ Xatolik: {str(e)}")
        # Faylni qayta pending ga qaytaramiz
        pending_srt[message.chat.id] = job


async def main():
    print("🤖 Kino yuklovchi bot ishga tushdi!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
