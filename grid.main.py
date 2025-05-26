import os
import asyncio
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.enums import ContentType, ParseMode
from aiogram.methods import GetUpdates

from aiogram.types import Update, Message, FSInputFile
from aiogram.client.default import DefaultBotProperties
from grid_db import MySQLManager
from pathlib import Path
from datetime import datetime
from aiohttp import ClientSession
from moviepy import VideoFileClip
from PIL import Image

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")



bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
db = MySQLManager({
    "host": os.getenv("MYSQL_DB_HOST"),
    "port": int(os.getenv("MYSQL_DB_PORT", 3306)),
    "user": os.getenv("MYSQL_DB_USER"),
    "password": os.getenv("MYSQL_DB_PASSWORD"),
    "db": os.getenv("MYSQL_DB_NAME"),
    "autocommit": True
})

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
shutdown_event = asyncio.Event()
BOT_NAME = None
API_ID = None

async def download_from_file_id(file_id: str, save_path: str):
    file = await bot.get_file(file_id)
    file_path = file.file_path
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    async with ClientSession() as session:
        async with session.get(download_url) as resp:
            if resp.status == 200:
                with open(save_path, "wb") as f:
                    while True:
                        chunk = await resp.content.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            else:
                raise Exception(f"❌ Download failed: {resp.status}")
    print(f"✔️ Download completed", flush=True)

async def make_keyframe_grid(video_path: str, preview_basename: str, rows: int = 3, cols: int = 3) -> str:
    clip = VideoFileClip(video_path)
    n = rows * cols
    times = [(i + 1) * clip.duration / (n + 1) for i in range(n)]
    imgs = [Image.fromarray(clip.get_frame(t)) for t in times]

    w, h = imgs[0].size
    grid_img = Image.new('RGB', (w * cols, h * rows))
    for idx, img in enumerate(imgs):
        x = (idx % cols) * w
        y = (idx // cols) * h
        grid_img.paste(img, (x, y))

    output_path = f"{preview_basename}.jpg"
    grid_img.save(output_path)
    print(f"✔️ Generated keyframe grid: {output_path}", flush=True)
    return output_path

async def bypass(file_id: str, from_bot: str, to_bot: str):
    print(f"👉 Bypassing: {file_id} from {from_bot} to {to_bot}", flush=True)
    pass

async def handle_video(message: Message):
    print("handle_video", flush=True)
    video = message.video
    file_unique_id = video.file_unique_id
    file_id = video.file_id
    await db.init()

    print("check video", flush=True)
    await db.execute("""
        INSERT INTO video (file_unique_id, file_size, duration, width, height, mime_type, create_time, update_time)
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON DUPLICATE KEY UPDATE 
            file_size=VALUES(file_size),
            duration=VALUES(duration),
            width=VALUES(width),
            height=VALUES(height),
            mime_type=VALUES(mime_type),
            update_time=NOW()
    """, (file_unique_id, video.file_size, video.duration, video.width, video.height, video.mime_type))

    print("check file_extension", flush=True)
    await db.execute("""
        INSERT IGNORE INTO file_extension (file_type, file_unique_id, file_id, bot, create_time)
        VALUES ('video', %s, %s, %s, NOW())
    """, (file_unique_id, file_id, BOT_NAME))

    print("check bid_thumbnail", flush=True)
    thumb_row = await db.fetchone("""
        SELECT thumb_file_unique_id FROM bid_thumbnail WHERE file_unique_id=%s
    """, (file_unique_id,))
    if thumb_row and thumb_row[0]:
        thumb_file_unique_id = thumb_row[0]
        print("check bid_thumbnail file_extension")
        rows = await db.fetchall("""
            SELECT file_id, bot FROM file_extension WHERE file_unique_id=%s
        """, (thumb_file_unique_id,))
        if rows:
            for file_id_result, bot_name in rows:
                if bot_name == BOT_NAME:
                    await message.answer_photo(file_id_result, caption="✅ 縮圖已存在")
                    return
                else:
                    await bypass(file_id_result, bot_name, BOT_NAME)
                    return
        else:
            print("No existing thumbnail found, will create a new one")
            pass
            #await db.execute("DELETE FROM bid_thumbnail WHERE thumb_file_unique_id=%s", (thumb_file_unique_id,))
    else:
        print("check grid_jobs", flush=True)
        # 在 handle_video 或者你插入 grid_jobs 的地方，把 message.chat.id、message.message_id 也传进去
        await db.execute("""
            INSERT INTO grid_jobs (
                file_id,
                file_unique_id,
                file_type,
                bot_name,
                job_state,
                scheduled_at,
                retry_count,
                source_chat_id,
                source_message_id
            )
            VALUES (%s, %s, 'video', %s, 'pending', NOW(), 0, %s, %s)
            ON DUPLICATE KEY UPDATE
                job_state      = 'pending',
                scheduled_at   = NOW(),
                retry_count    = retry_count + 1,
                source_chat_id = VALUES(source_chat_id),
                source_message_id = VALUES(source_message_id)
        """, (
            file_id,
            file_unique_id,
            BOT_NAME,
            message.chat.id,
            message.message_id
        ))

    await message.answer("🌀 已加入關鍵幀任務排程")

async def get_last_update_id() -> int:
    await db.init()
    row = await db.fetchone("SELECT message_id FROM scrap_progress WHERE api_id=%s AND chat_id=0", (API_ID,))
    return int(row[0]) if row else 0

async def update_scrap_progress(new_update_id: int):
    await db.execute("""
        INSERT INTO scrap_progress (chat_id, api_id, message_id, update_datetime)
        VALUES (0, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE 
            message_id=VALUES(message_id),
            update_datetime=NOW()
    """, (API_ID, new_update_id))

async def limited_polling():
    last_update_id = await get_last_update_id()
    print(f"📥 Polling from offset={last_update_id + 1}")

    while not shutdown_event.is_set():
        updates: list[Update] = await bot(GetUpdates(
            offset=last_update_id + 1,
            limit=100,
            timeout=5
        ))

        if not updates:
            await asyncio.sleep(1)
            continue

        max_update_id = last_update_id
        for update in updates:
            print(f"📬 Received update: {update.update_id}")
            max_update_id = max(max_update_id, update.update_id)
            if update.message and update.message.video:
                try:
                    await handle_video(update.message)
                except Exception as e:
                    print(f"[Error] handle_video: {e}")

        if max_update_id != last_update_id:
            await update_scrap_progress(max_update_id)
            last_update_id = max_update_id

        await asyncio.sleep(1)

    print("🛑 Polling stopped")

async def process_one_grid_job():
    job = await db.fetchone("""
        SELECT id, file_id, file_unique_id, source_chat_id, source_message_id
        FROM grid_jobs
        WHERE job_state='pending'
        ORDER BY scheduled_at ASC
        LIMIT 1
    """)

    if not job:
        print("📭 No pending job found")
        await asyncio.sleep(30)
        shutdown_event.set()
        return

    job_id, file_id, file_unique_id, chat_id, message_id = job
    print(f"🔧 Processing job ID={job_id}",flush=True)

    try:
        video_path = f"temp/{file_unique_id}.mp4"
        preview_basename = f"temp/preview_{file_unique_id}"
        os.makedirs("temp", exist_ok=True)

        await download_from_file_id(file_id, video_path)
        preview_path = await make_keyframe_grid(video_path, preview_basename)

        # 使用 FSInputFile 上传文件
        input_file = FSInputFile(preview_path)
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=input_file,
            reply_to_message_id=message_id
        )
        photo_file_id = sent.photo[-1].file_id
        photo_unique_id = sent.photo[-1].file_unique_id

        # 更新任务状态

        await db.execute("""
            UPDATE grid_jobs
            SET job_state='done',
                finished_at=NOW(),
                grid_file_id=%s
            WHERE id=%s
        """, (photo_file_id, job_id))

        await db.execute("""
            INSERT INTO photo (
                file_unique_id, file_size, width, height, file_name,
                caption, root_unique_id, create_time, files_drive,
                hash, same_fuid
            )
            VALUES (%s, %s, %s, %s, NULL, NULL, NULL, NOW(), NULL, NULL, NULL)
            ON DUPLICATE KEY UPDATE
                file_size=VALUES(file_size),
                width=VALUES(width),
                height=VALUES(height),
                create_time=NOW()
        """, (
            photo_unique_id,
            sent.photo[-1].file_size,
            sent.photo[-1].width,
            sent.photo[-1].height
        ))

        await db.execute("""
            INSERT INTO file_extension (file_type, file_unique_id, file_id, bot, create_time)
            VALUES ('photo', %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                file_id=VALUES(file_id),
                bot=VALUES(bot),
                create_time=NOW()
        """, (photo_unique_id, photo_file_id, BOT_NAME))

        await db.execute(
            """
            INSERT INTO bid_thumbnail (
                file_unique_id,
                thumb_file_unique_id,
                bot_name,
                file_id,
                confirm_status,
                uploader_id,
                status,
                t_update
            )
            VALUES (%s, %s, %s, %s, 0, 0, 1, 1)
            ON DUPLICATE KEY UPDATE
                file_id          = VALUES(file_id),
                confirm_status   = VALUES(confirm_status),
                uploader_id      = VALUES(uploader_id),
                status           = VALUES(status),
                t_update         = 1
            """,
            (
                file_unique_id,
                photo_unique_id,
                BOT_NAME,
                photo_file_id,       # 这里加上 photo_file_id
            )
        )


        print(f"✅ Job ID={job_id} completed")
    except Exception as e:
        print(f"❌ Job ID={job_id} failed: {e}")
    finally:
        shutdown_event.set()

async def shutdown():
    # 1) 关闭 aiogram 内部的 HTTP session
    await bot.session.close()
    # 2) 关闭你的 MySQL 连接池
    await db.close()

async def main():
    global BOT_NAME, API_ID
    me = await bot.get_me()
    BOT_NAME = me.username
    API_ID = me.id
    print(f"🤖 Logged in as @{BOT_NAME} (API_ID={API_ID})")

    # 并行启动，两者谁先结束，就取消另一个
    task1 = asyncio.create_task(process_one_grid_job())
    task2 = asyncio.create_task(limited_polling())

    try:
        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED
        )
        # 取消还在跑的任务
        for t in pending:
            t.cancel()
    finally:
        # 不管如何，都优雅地关掉 session 和连接池
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
