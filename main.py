import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import firebase_admin
from firebase_admin import credentials, db, storage
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo
import uvicorn

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("toonflix")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_IDS = {int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
INITIAL_ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
if not OWNER_IDS:
    raise RuntimeError("OWNER_IDS is required. Set at least one trusted Telegram numeric user ID.")
DATABASE_URL = os.environ["FIREBASE_DATABASE_URL"]
BUCKET_NAME = os.environ["FIREBASE_STORAGE_BUCKET"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1800"))
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/toonflix_uploads"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

def init_firebase():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is missing")
    try:
        service_account = json.loads(raw)
    except json.JSONDecodeError:
        # Supports base64-encoded service account JSON as well.
        import base64
        service_account = json.loads(base64.b64decode(raw).decode("utf-8"))
    cred = credentials.Certificate(service_account)
    firebase_admin.initialize_app(cred, {
        "databaseURL": DATABASE_URL,
        "storageBucket": BUCKET_NAME,
    })

init_firebase()

# Owner IDs come from the trusted deployment environment and cannot be removed
# through bot commands. Additional admins are stored persistently in Realtime DB.
admins_ref = db.reference("toonflixAdmins")
if admins_ref.get() is None:
    admins_ref.set({str(uid): True for uid in INITIAL_ADMIN_IDS})
bucket = storage.bucket()
client = TelegramClient("toonflix_bot_session", API_ID, API_HASH)
app = FastAPI(title="ToonFlix File-to-Link API", version="1.0.0")

def safe_filename(name: str) -> str:
    name = Path(name or "video.mp4").name
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", name).strip(" .")
    return name[:180] or "video.mp4"

def get_video_info(message):
    name = "video.mp4"
    duration = None
    width = height = None
    mime = "application/octet-stream"
    if message.file:
        name = message.file.name or name
        mime = message.file.mime_type or mime
    if message.document:
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                name = attr.file_name
            elif isinstance(attr, DocumentAttributeVideo):
                duration = int(attr.duration)
                width, height = attr.w, attr.h
    if message.video and message.video.attributes:
        for attr in message.video.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                duration = int(attr.duration)
                width, height = attr.w, attr.h
    return safe_filename(name), duration, width, height, mime

def get_admin_ids():
    stored = admins_ref.get() or {}
    return OWNER_IDS | {int(uid) for uid, enabled in stored.items() if enabled}

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

async def handle_admin_command(event, text_value: str):
    sender = await event.get_sender()
    parts = text_value.strip().split()
    command = parts[0].split("@", 1)[0].lower()

    if command == "/myid":
        await event.reply(f"Your Telegram user ID: `{sender.id}`")
        return True

    if command == "/admins":
        if not is_owner(sender.id):
            await event.reply("⛔ Sirf Owner admin list dekh sakta hai.")
            return True
        ids = sorted(get_admin_ids())
        listing = "\\n".join(f"• `{uid}`" + (" (Owner)" if uid in OWNER_IDS else "") for uid in ids)
        await event.reply("👥 **Bot Admins**\\n\\n" + (listing or "Koi admin nahi hai."))
        return True

    if command in ("/addadmin", "/removeadmin"):
        if not is_owner(sender.id):
            await event.reply("⛔ Sirf Owner admins add/remove kar sakta hai.")
            return True
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply(f"Usage: `{command} USER_ID`")
            return True
        target_id = int(parts[1])
        if command == "/addadmin":
            admins_ref.child(str(target_id)).set(True)
            await event.reply(f"✅ Admin add kar diya gaya. User ID: `{target_id}`")
        else:
            if target_id in OWNER_IDS:
                await event.reply("⛔ Owner ko admin list se remove nahi kiya ja sakta.")
                return True
            admins_ref.child(str(target_id)).delete()
            await event.reply(f"✅ Admin remove kar diya gaya. User ID: `{target_id}`")
        return True

    return False

async def process_video(event):
    sender = await event.get_sender()
    if sender.id not in get_admin_ids():
        await event.reply("⛔ Sorry, sirf bot ke authorized admins video upload kar sakte hain.")
        return
    msg = event.message
    if not msg.media or not msg.file:
        return
    size = int(msg.file.size or 0)
    if size <= 0:
        await event.reply("File size read nahi ho saka. Video ko document ke roop mein dobara bhejein.")
        return
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        await event.reply(f"File {MAX_UPLOAD_MB} MB limit se badi hai.")
        return

    file_id = uuid.uuid4().hex
    filename, duration, width, height, mime = get_video_info(msg)
    local_path = TMP_DIR / f"{file_id}_{filename}"
    status_msg = await event.reply(
        "⏳ Upload shuru ho gaya hai...\n"
        f"File: {filename}\nSize: {size / (1024*1024):.1f} MB"
    )
    try:
        await client.download_media(msg, file=str(local_path))
        if not local_path.exists():
            raise RuntimeError("Telegram download failed")
        object_path = f"videos/{file_id}/{filename}"
        blob = bucket.blob(object_path)
        token = str(uuid.uuid4())
        blob.metadata = {"firebaseStorageDownloadTokens": token}
        blob.content_type = "video/mp4" if filename.lower().endswith(".mp4") else mime
        blob.upload_from_filename(str(local_path), content_type=blob.content_type)
        blob.patch()

        stream_url = f"{PUBLIC_BASE_URL}/stream/{file_id}" if PUBLIC_BASE_URL else (
            f"https://firebasestorage.googleapis.com/v0/b/{BUCKET_NAME}/o/{quote(object_path, safe='')}?alt=media&token={token}"
        )
        record = {
            "fileId": file_id,
            "fileName": filename,
            "storagePath": object_path,
            "sizeBytes": size,
            "mimeType": blob.content_type,
            "durationSeconds": duration,
            "width": width,
            "height": height,
            "streamUrl": stream_url,
            "telegramChatId": msg.chat_id,
            "telegramMessageId": msg.id,
            "createdAt": {".sv": "timestamp"},
            "status": "ready",
        }
        db.reference(f"toonflixVideos/{file_id}").set(record)
        await status_msg.edit(
            "✅ **Upload complete!**\n\n"
            f"**File:** `{filename}`\n"
            f"**Size:** {size / (1024*1024):.1f} MB\n"
            f"**Quality:** {f'{height}p' if height else 'Unknown'}\n"
            f"**Duration:** {f'{duration}s' if duration else 'Unknown'}\n\n"
            f"**MP4 Streaming URL:**\n`{stream_url}`"
        )
    except Exception as exc:
        log.exception("Upload failed")
        await status_msg.edit(f"❌ Upload failed: `{str(exc)[:350]}`")
    finally:
        try:
            if local_path.exists():
                local_path.unlink()
        except OSError:
            log.warning("Could not remove temp file %s", local_path)

@client.on(events.NewMessage(incoming=True))
async def incoming_handler(event):
    if event.raw_text and event.raw_text.strip().lower() in ("/start", "/help"):
        await event.reply(
            "🎬 **ToonFlix File-to-Link Bot**\n\n"
            "Mujhe MP4 video ya video document bhejein. Main Firebase Storage par upload karke streaming URL dunga.\n"
            "Owner-only bot."
        )
        return
    if event.message.media and event.message.file:
        await process_video(event)

@app.get("/")
async def root():
    return {"service": "ToonFlix File-to-Link Bot", "status": "online"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/firebase-config")
async def firebase_web_config():
    # Firebase Web config is public client configuration, not a service-account secret.
    return {
        "apiKey": os.getenv("FIREBASE_API_KEY", ""),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN", ""),
        "databaseURL": DATABASE_URL,
        "projectId": os.getenv("FIREBASE_PROJECT_ID", ""),
        "storageBucket": BUCKET_NAME,
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID", ""),
        "appId": os.getenv("FIREBASE_APP_ID", ""),
        "measurementId": os.getenv("FIREBASE_MEASUREMENT_ID", ""),
    }

@app.get("/stream/{file_id}")
async def stream(file_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(status_code=400, detail="Invalid file ID")
    record = db.reference(f"toonflixVideos/{file_id}").get()
    if not record or record.get("status") != "ready":
        raise HTTPException(status_code=404, detail="Video not found")
    path = record.get("storagePath")
    token = None
    blob = bucket.blob(path)
    blob.reload()
    token = (blob.metadata or {}).get("firebaseStorageDownloadTokens")
    if not token:
        raise HTTPException(status_code=404, detail="Streaming token missing")
    url = f"https://firebasestorage.googleapis.com/v0/b/{BUCKET_NAME}/o/{quote(path, safe='')}?alt=media&token={token}"
    return RedirectResponse(url=url, status_code=307)

async def main():
    await client.start(bot_token=BOT_TOKEN)
    log.info("Telegram bot connected")
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
