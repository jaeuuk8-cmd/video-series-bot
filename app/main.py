import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from .db import Database

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_TELEGRAM_ID"])
BOT_API_URL = os.getenv("BOT_API_URL", "http://telegram-bot-api:8081").rstrip("/")
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
IDLE_SECONDS = int(os.getenv("SERIES_IDLE_SECONDS", "5"))
db = Database(DATA_DIR / "library.db")
app = FastAPI()


@dataclass
class PendingBatch:
    files: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None


pending: dict[int, PendingBatch] = defaultdict(PendingBatch)
waiting_title: dict[int, list[dict]] = {}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


class Telegram:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=30.0))

    async def call(self, method: str, **payload):
        response = await self.client.post(f"{BOT_API_URL}/bot{BOT_TOKEN}/{method}", json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram API error"))
        return body["result"]

    async def send_text(self, chat_id: int, text: str, **extra):
        return await self.call("sendMessage", chat_id=chat_id, text=text, **extra)

    async def send_document(self, chat_id: int, path: Path, caption: str):
        """Upload the renamed file directly instead of relying on a shared path."""
        with path.open("rb") as document:
            response = await self.client.post(
                f"{BOT_API_URL}/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"document": (path.name, document, "application/octet-stream")},
            )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram API error"))
        return body["result"]


tg = Telegram()


def allowed(message: dict) -> bool:
    return message.get("from", {}).get("id") == OWNER_ID


def media_from(message: dict) -> dict | None:
    # 파일(문서) 전송을 우선 권장하지만 동영상 메시지도 허용합니다.
    if message.get("photo"):
        photo = message["photo"][-1]
        return {"file_id": photo["file_id"], "original_filename": "photo.jpg", "kind": "image"}

    media = message.get("document") or message.get("video")
    if not media:
        return None
    mime = media.get("mime_type", "")
    filename = media.get("file_name") or "video.mp4"
    extension = Path(filename).suffix.lower()
    if not (mime.startswith(("video/", "image/")) or extension in SUPPORTED_EXTENSIONS):
        return None
    return {
        "file_id": media["file_id"],
        "original_filename": filename,
        "kind": "image" if mime.startswith("image/") or extension in IMAGE_EXTENSIONS else "video",
    }


async def collect_later(user_id: int, chat_id: int):
    await asyncio.sleep(IDLE_SECONDS)
    batch = pending.pop(user_id, None)
    if not batch or not batch.files:
        return
    waiting_title[user_id] = batch.files
    await tg.send_text(chat_id, f"영상 {len(batch.files)}개를 한 시리즈로 준비했습니다. 시리즈 제목을 보내주세요.\n/cancel 로 취소할 수 있습니다.")


async def enqueue_video(message: dict, media: dict):
    user_id, chat_id = message["from"]["id"], message["chat"]["id"]
    batch = pending[user_id]
    batch.files.append(media)
    if batch.task:
        batch.task.cancel()
    batch.task = asyncio.create_task(collect_later(user_id, chat_id))


def extract_thumbnail(source: Path, destination: Path, kind: str) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y"]
    if kind == "video":
        command.extend(["-ss", "00:00:01"])
    command.extend(["-i", str(source), "-frames:v", "1", "-vf", "scale='min(640,iw)':-2", str(destination)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and destination.exists()


def sequential_filename(position: int, original_filename: str, kind: str) -> str:
    """Keep the real container extension; never relabel images or AVI files."""
    extension = Path(original_filename).suffix.lower()
    fallback = ".jpg" if kind == "image" else ".mp4"
    return f"{position}{extension if extension in SUPPORTED_EXTENSIONS else fallback}"


async def process_series(chat_id: int, title: str, files: list[dict]):
    series_id = db.create_series(title)
    work_dir = DATA_DIR / "jobs" / f"series-{series_id}-{int(time.time())}"
    work_dir.mkdir(parents=True, exist_ok=True)
    await tg.send_text(chat_id, f"‘{title}’ 등록을 시작합니다. 대용량 파일은 시간이 걸릴 수 있습니다.")
    try:
        for position, item in enumerate(files, start=1):
            stored_name = sequential_filename(position, item["original_filename"], item["kind"])
            info = await tg.call("getFile", file_id=item["file_id"])
            # Local Bot API mode에서는 이 값이 컨테이너 공용 볼륨의 절대 경로입니다.
            source = Path(info["file_path"])
            if not source.exists():
                raise RuntimeError("Telegram 처리 파일을 찾지 못했습니다.")
            # Telegram은 로컬 경로의 basename을 실제 파일명으로 사용하므로,
            # 재업로드 전에 원하는 이름의 하드링크(불가하면 복사본)를 만듭니다.
            renamed = work_dir / stored_name
            try:
                os.link(source, renamed)
            except OSError:
                shutil.copy2(source, renamed)
            thumb = DATA_DIR / "thumbnails" / f"series-{series_id}-{position}.jpg"
            extract_thumbnail(renamed, thumb, item["kind"])
            # local Bot API가 같은 볼륨을 읽어 파일명 그대로 Telegram에 올립니다.
            sent = await tg.send_document(chat_id, renamed, stored_name)
            attachment = sent.get("document") or sent.get("video")
            if not attachment and sent.get("photo"):
                attachment = sent["photo"][-1]
            if not attachment:
                raise RuntimeError("Telegram did not return the uploaded file information.")
            db.add_video(series_id, position, stored_name, item["original_filename"], attachment["file_id"], str(thumb) if thumb.exists() else None)
            await tg.send_text(chat_id, f"{stored_name} 등록 완료 ({position}/{len(files)})")
    except Exception as exc:
        await tg.send_text(chat_id, f"등록 중 오류가 발생했습니다: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    await tg.send_text(
        chat_id,
        f"등록이 끝났습니다. /library 에서 시리즈 목록을 열 수 있습니다.\n"
        f"대표 썸네일 변경: /cover {series_id} 영상번호  (예: /cover {series_id} 3)",
    )


def webapp_button():
    return {"inline_keyboard": [[{"text": "🎬 시리즈 목록 열기", "web_app": {"url": f"https://{PUBLIC_HOST}"}}]]}


async def handle_message(message: dict):
    if not allowed(message):
        return
    user_id, chat_id = message["from"]["id"], message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if text == "/start":
        await tg.send_text(chat_id, "MP4·MOV·AVI 파일을 연달아 보내세요. 잠시 뒤 시리즈 제목을 묻습니다.\n파일은 1.mp4, 2.mp4…처럼 순서대로 다시 등록됩니다.", reply_markup=webapp_button())
        return
    if text == "/library":
        await tg.send_text(chat_id, "시리즈 목록입니다.", reply_markup=webapp_button())
        return
    if text.startswith("/cover"):
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await tg.send_text(chat_id, "사용법: /cover 시리즈번호 영상번호\n예: /cover 1 3")
            return
        if db.set_cover(int(parts[1]), int(parts[2])):
            await tg.send_text(chat_id, "대표 썸네일을 변경했습니다. /library에서 확인하세요.")
        else:
            await tg.send_text(chat_id, "해당 시리즈 또는 영상 번호를 찾지 못했습니다.")
        return
    if text == "/cancel":
        pending.pop(user_id, None)
        waiting_title.pop(user_id, None)
        await tg.send_text(chat_id, "대기 중인 시리즈 등록을 취소했습니다.")
        return
    media = media_from(message)
    if media:
        await enqueue_video(message, media)
        return
    if text and user_id in waiting_title:
        files = waiting_title.pop(user_id)
        asyncio.create_task(process_series(chat_id, text[:100], files))
        return


async def polling():
    offset = 0
    while True:
        try:
            updates = await tg.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    await handle_message(update["message"])
        except Exception:
            await asyncio.sleep(3)


def validate_init_data(init_data: str | None):
    if not init_data:
        raise HTTPException(401, "Telegram Mini App에서 열어주세요.")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(received_hash, expected):
        raise HTTPException(401, "유효하지 않은 Telegram 로그인입니다.")
    user = json.loads(values.get("user", "{}"))
    if user.get("id") != OWNER_ID:
        raise HTTPException(403, "이 목록은 소유자 전용입니다.")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/series")
async def series(x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    return [{"id": row["id"], "title": row["title"], "count": row["video_count"], "cover": f"/api/thumb/{row['cover_video_id']}" if row["cover_video_id"] else None} for row in db.list_series()]


@app.get("/api/series/{series_id}")
async def videos(series_id: int, x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    return [{"id": row["id"], "filename": row["stored_filename"], "position": row["position"], "thumb": f"/api/thumb/{row['id']}" if row["thumbnail_path"] else None} for row in db.list_videos(series_id)]


@app.post("/api/video/{video_id}/send")
async def send_video(video_id: int, x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    row = db.video_for_send(video_id)
    if not row:
        raise HTTPException(404, "영상을 찾지 못했습니다.")
    await tg.call("sendDocument", chat_id=OWNER_ID, document=row["telegram_file_id"], caption=row["stored_filename"])
    return {"ok": True}


@app.get("/api/thumb/{video_id}")
async def thumbnail(video_id: int, x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    row = db.thumbnail_for_video(video_id)
    if not row or not row["thumbnail_path"] or not Path(row["thumbnail_path"]).exists():
        raise HTTPException(404, "썸네일이 없습니다.")
    return FileResponse(row["thumbnail_path"], media_type="image/jpeg")


HTML = """<!doctype html><html lang='ko'><meta name='viewport' content='width=device-width,initial-scale=1'>
<script src='https://telegram.org/js/telegram-web-app.js'></script><style>body{font-family:system-ui;margin:16px;background:#f5f5f5;color:#111}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.card{background:#fff;border-radius:12px;overflow:hidden;padding:10px}.cover{width:100%;aspect-ratio:16/9;object-fit:cover;background:#ddd}button{border:0;background:none;padding:0;text-align:left;font:inherit;width:100%}.muted{color:#666;font-size:13px}</style>
<h2 id='title'>내 시리즈</h2><div id='list' class='grid'></div><script>const init=Telegram.WebApp.initData, H={'X-Telegram-Init-Data':init}, list=document.querySelector('#list');Telegram.WebApp.ready();async function img(url){if(!url)return '';let r=await fetch(url,{headers:H}),b=await r.blob();return URL.createObjectURL(b)}async function load(){let rows=await (await fetch('/api/series',{headers:H})).json();list.innerHTML='';for(let x of rows){let u=await img(x.cover);let b=document.createElement('button');b.className='card';b.innerHTML=(u?`<img class='cover' src='${u}'>`:'<div class="cover"></div>')+`<b>${x.title}</b><div class='muted'>${x.count}편</div>`;b.onclick=()=>open(x);list.append(b)}}async function open(x){document.querySelector('#title').textContent=x.title;let rows=await (await fetch('/api/series/'+x.id,{headers:H})).json();list.innerHTML='';for(let v of rows){let u=await img(v.thumb);let b=document.createElement('button');b.className='card';b.innerHTML=(u?`<img class='cover' src='${u}'>`:'<div class="cover"></div>')+`<b>${v.filename}</b><div class='muted'>누르면 텔레그램으로 전송</div>`;b.onclick=async()=>{await fetch('/api/video/'+v.id+'/send',{method:'POST',headers:H});Telegram.WebApp.showAlert(v.filename+'을(를) 채팅으로 보냈습니다.')};list.append(b)}}load().catch(()=>list.textContent='텔레그램에서 이 화면을 다시 열어주세요.');</script></html>"""


@app.on_event("startup")
async def start_polling():
    asyncio.create_task(polling())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
