import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from .db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("video-series-bot")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_TELEGRAM_ID"])
BOT_API_URL = os.getenv("BOT_API_URL", "http://telegram-bot-api:8081").rstrip("/")
PUBLIC_HOST = os.environ["PUBLIC_HOST"]
IDLE_SECONDS = int(os.getenv("SERIES_IDLE_SECONDS", "5"))
db = Database(DATA_DIR / "library.db")
app = FastAPI()


collect_tasks: dict[int, asyncio.Task] = {}
running_jobs: dict[int, asyncio.Task] = {}
# A target selected with /add (or the library's Add media button).  Only the
# owner can use this bot, so one target per owner is sufficient.
adding_to_series: dict[int, int] = {}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS


class Telegram:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=30.0))

    async def call(self, method: str, **payload):
        last_error = None
        for attempt in range(4):
            try:
                response = await self.client.post(
                    f"{BOT_API_URL}/bot{BOT_TOKEN}/{method}", json=payload
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.is_success and body.get("ok"):
                    return body["result"]
                description = body.get("description", f"HTTP {response.status_code}")
                last_error = RuntimeError(f"Telegram {method}: {description}")
                retry_after = body.get("parameters", {}).get("retry_after")
                if response.status_code != 429 and response.status_code < 500:
                    raise last_error
                delay = int(retry_after) if retry_after else 2 ** attempt
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                delay = 2 ** attempt
            if attempt < 3:
                LOGGER.warning("Telegram %s failed; retrying in %ss: %s", method, delay, last_error)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Telegram {method} failed after retries: {last_error}")

    async def send_text(self, chat_id: int, text: str, **extra):
        return await self.call("sendMessage", chat_id=chat_id, text=text, **extra)

    async def send_document(self, chat_id: int, path: Path, caption: str):
        """Upload the renamed file directly instead of relying on a shared path."""
        last_error = None
        for attempt in range(4):
            try:
                with path.open("rb") as document:
                    response = await self.client.post(
                        f"{BOT_API_URL}/bot{BOT_TOKEN}/sendDocument",
                        data={"chat_id": str(chat_id), "caption": caption},
                        files={"document": (path.name, document, "application/octet-stream")},
                    )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.is_success and body.get("ok"):
                    return body["result"]
                description = body.get("description", f"HTTP {response.status_code}")
                last_error = RuntimeError(f"Telegram sendDocument: {description}")
                retry_after = body.get("parameters", {}).get("retry_after")
                if response.status_code != 429 and response.status_code < 500:
                    raise last_error
                delay = int(retry_after) if retry_after else 2 ** attempt
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                delay = 2 ** attempt
            if attempt < 3:
                LOGGER.warning("Upload failed; retrying in %ss: %s", delay, last_error)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Telegram upload failed after retries: {last_error}")


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


async def collect_later(user_id: int, chat_id: int, job_id: int):
    await asyncio.sleep(IDLE_SECONDS)
    job = db.get_job(job_id)
    files = db.job_files(job_id)
    if not job or job["status"] != "collecting" or not files:
        return
    adding_to_series.pop(user_id, None)
    if job["mode"] == "append":
        db.set_job_status(job_id, "queued")
        schedule_job(job_id)
        return
    db.set_job_status(job_id, "waiting_title")
    await tg.send_text(chat_id, f"파일 {len(files)}개를 한 시리즈로 준비했습니다. 시리즈 제목을 보내주세요.\n/cancel 로 취소할 수 있습니다.")


async def enqueue_video(message: dict, media: dict):
    user_id, chat_id = message["from"]["id"], message["chat"]["id"]
    target_series_id = adding_to_series.get(user_id)
    mode = "append" if target_series_id is not None else "new"
    job_id = db.get_or_create_collecting_job(user_id, chat_id, mode, target_series_id)
    added = db.add_job_file(
        job_id, media["file_id"], media["original_filename"], media["kind"]
    )
    if not added:
        await tg.send_text(chat_id, "같은 파일은 이 묶음에 한 번만 추가됩니다.")
    task = collect_tasks.get(user_id)
    if task:
        task.cancel()
    collect_tasks[user_id] = asyncio.create_task(collect_later(user_id, chat_id, job_id))


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


async def update_progress(chat_id: int, message_id: int | None, text: str):
    try:
        if message_id:
            await tg.call(
                "editMessageText", chat_id=chat_id, message_id=message_id, text=text
            )
        else:
            await tg.send_text(chat_id, text)
    except Exception:
        LOGGER.warning("Could not update progress message", exc_info=True)


def schedule_job(job_id: int):
    current = running_jobs.get(job_id)
    if current and not current.done():
        return
    task = asyncio.create_task(process_job(job_id))
    running_jobs[job_id] = task
    task.add_done_callback(lambda _task: running_jobs.pop(job_id, None))


async def process_job(job_id: int):
    job = db.get_job(job_id)
    if not job or job["status"] not in ("queued", "processing"):
        return
    chat_id = int(job["chat_id"])
    if job["mode"] == "append":
        series_id = job["target_series_id"]
        if not series_id or not db.series_exists(int(series_id)):
            db.set_job_status(job_id, "failed", "Series not found")
            await tg.send_text(chat_id, "추가할 시리즈를 찾지 못했습니다. /library에서 다시 선택하세요.")
            return
        series_id = int(series_id)
        start_text = f"시리즈 #{series_id}에 파일을 추가합니다."
    else:
        if not job["title"]:
            db.set_job_status(job_id, "waiting_title")
            return
        series_id = int(job["series_id"]) if job["series_id"] else db.create_series(job["title"])
        if not job["series_id"]:
            db.set_job_series(job_id, series_id)
        start_text = f"‘{job['title']}’ 등록을 시작합니다."

    db.set_job_status(job_id, "processing")
    files = db.job_files(job_id)
    total = len(files)
    progress = await tg.send_text(
        chat_id, f"{start_text}\n0/{total} 처리됨 · /cancel 로 다음 파일부터 중지"
    )
    progress_id = progress.get("message_id")
    work_dir = DATA_DIR / "jobs" / f"job-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    completed = sum(1 for item in files if item["status"] == "completed")
    failures = 0
    next_position = max(
        [db.next_position(series_id)]
        + [int(item["position"]) + 1 for item in files if item["position"] is not None]
    )
    try:
        for item in files:
            if item["status"] == "completed":
                continue
            current_job = db.get_job(job_id)
            if current_job and current_job["cancel_requested"]:
                db.set_job_status(job_id, "cancelled")
                await update_progress(
                    chat_id, progress_id, f"작업을 중지했습니다. {completed}/{total}개 처리됨"
                )
                return
            file_id = int(item["id"])
            position = int(item["position"]) if item["position"] is not None else next_position
            stored_name = item["stored_filename"] or sequential_filename(
                position, item["original_filename"], item["kind"]
            )
            if item["position"] is None:
                db.assign_job_file(file_id, position, stored_name)
                next_position += 1
            db.mark_file_processing(file_id)
            try:
                info = await tg.call("getFile", file_id=item["source_file_id"])
                source = Path(info["file_path"])
                if not source.exists():
                    raise RuntimeError("Telegram 처리 파일을 찾지 못했습니다.")
                renamed = work_dir / stored_name
                if not renamed.exists():
                    try:
                        os.link(source, renamed)
                    except OSError:
                        shutil.copy2(source, renamed)
                thumb = DATA_DIR / "thumbnails" / f"series-{series_id}-{position}.jpg"
                if not thumb.exists():
                    extract_thumbnail(renamed, thumb, item["kind"])

                uploaded_file_id = item["uploaded_file_id"]
                uploaded_message_id = item["uploaded_message_id"]
                if not uploaded_file_id:
                    sent = await tg.send_document(chat_id, renamed, stored_name)
                    attachment = sent.get("document") or sent.get("video")
                    if not attachment and sent.get("photo"):
                        attachment = sent["photo"][-1]
                    if not attachment:
                        raise RuntimeError("Telegram did not return the uploaded file information.")
                    uploaded_file_id = attachment["file_id"]
                    uploaded_message_id = sent["message_id"]
                    db.mark_file_uploaded(file_id, uploaded_file_id, uploaded_message_id)

                video_id = db.add_video(
                    series_id,
                    position,
                    stored_name,
                    item["original_filename"],
                    uploaded_file_id,
                    str(thumb) if thumb.exists() else None,
                    job_file_id=file_id,
                )
                db.mark_file_completed(file_id, video_id)
                completed += 1
                if uploaded_message_id:
                    try:
                        await tg.call(
                            "deleteMessage", chat_id=chat_id, message_id=uploaded_message_id
                        )
                    except Exception:
                        LOGGER.warning("Could not delete temporary upload message", exc_info=True)
                await update_progress(
                    chat_id, progress_id, f"처리 중: {completed}/{total} 완료 · 실패 {failures}"
                )
            except Exception as exc:
                failures += 1
                db.mark_file_failed(file_id, str(exc))
                LOGGER.exception("File processing failed: job_id=%s file_id=%s", job_id, file_id)
                await update_progress(
                    chat_id, progress_id, f"처리 중: {completed}/{total} 완료 · 실패 {failures}"
                )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failures:
        db.set_job_status(job_id, "partial_failed", f"{failures} file(s) failed")
        await update_progress(
            chat_id,
            progress_id,
            f"작업 완료: {completed}/{total}개 저장, {failures}개 실패\n/retry 로 실패한 파일만 다시 처리할 수 있습니다.",
        )
    else:
        db.set_job_status(job_id, "completed")
        await update_progress(
            chat_id,
            progress_id,
            f"등록이 끝났습니다. {completed}/{total}개 저장됨\n/library 에서 확인하세요.",
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
    if text.startswith("/add"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit() or not db.series_exists(int(parts[1])):
            await tg.send_text(chat_id, "사용법: /add 시리즈번호\n예: /add 3")
            return
        adding_to_series[user_id] = int(parts[1])
        await tg.send_text(chat_id, "추가할 영상 또는 사진을 연달아 보내세요. 5초간 새 파일이 없으면 자동으로 추가합니다. /cancel 로 취소할 수 있습니다.")
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
        task = collect_tasks.pop(user_id, None)
        if task:
            task.cancel()
        adding_to_series.pop(user_id, None)
        job_id = db.request_cancel(user_id)
        if job_id:
            await tg.send_text(chat_id, "작업 취소를 요청했습니다. 처리 중이면 현재 파일 뒤에 중지됩니다.")
        else:
            await tg.send_text(chat_id, "취소할 작업이 없습니다.")
        return
    if text.startswith("/retry"):
        parts = text.split()
        job = None
        if len(parts) == 2 and parts[1].isdigit():
            candidate = db.get_job(int(parts[1]))
            if candidate and candidate["user_id"] == user_id:
                job = candidate
        elif len(parts) == 1:
            job = db.latest_failed_job(user_id)
        if not job or not db.prepare_retry(int(job["id"])):
            await tg.send_text(chat_id, "다시 처리할 실패 작업이 없습니다.")
            return
        schedule_job(int(job["id"]))
        await tg.send_text(chat_id, f"작업 #{job['id']}의 실패 파일만 다시 처리합니다.")
        return
    media = media_from(message)
    if media:
        await enqueue_video(message, media)
        return
    active = db.active_job(user_id)
    if text and active and active["status"] == "waiting_title":
        db.set_job_title(int(active["id"]), text[:100])
        schedule_job(int(active["id"]))
        return


async def polling():
    offset = 0
    while True:
        try:
            updates = await tg.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for update in updates:
                update_id = int(update["update_id"])
                if db.is_update_processed(update_id):
                    offset = update_id + 1
                    continue
                if "message" in update:
                    await handle_message(update["message"])
                db.mark_update_processed(update_id)
                offset = update_id + 1
        except Exception:
            LOGGER.exception("Telegram polling failed")
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


@app.patch("/api/series/{series_id}")
async def rename_series(series_id: int, payload: dict = Body(...), x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "A series title is required.")
    if not db.rename_series(series_id, title[:100]):
        raise HTTPException(404, "Series not found.")
    return {"ok": True}


@app.delete("/api/series/{series_id}")
async def delete_series(series_id: int, x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    paths = db.delete_series(series_id)
    for value in paths:
        Path(value).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/series/{series_id}/add")
async def select_series_for_append(series_id: int, x_telegram_init_data: str | None = Header(default=None)):
    validate_init_data(x_telegram_init_data)
    if not db.series_exists(series_id):
        raise HTTPException(404, "Series not found.")
    adding_to_series[OWNER_ID] = series_id
    await tg.send_text(
        OWNER_ID,
        "추가할 영상 또는 사진을 연달아 보내세요. 5초간 새 파일이 없으면 선택한 시리즈에 자동 추가합니다. /cancel 로 취소할 수 있습니다.",
    )
    return {"ok": True}


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


HTML = """<!doctype html><html lang='en'><meta name='viewport' content='width=device-width,initial-scale=1'>
<script src='https://telegram.org/js/telegram-web-app.js'></script>
<style>body{font-family:system-ui;margin:16px;background:#f5f5f5;color:#111}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.card{background:#fff;border-radius:12px;overflow:hidden;padding:10px}.cover{width:100%;aspect-ratio:16/9;object-fit:cover;background:#ddd}button{border:0;background:none;padding:0;text-align:left;font:inherit;width:100%}.muted{color:#666;font-size:13px}.controls{display:flex;gap:8px;margin:0 0 14px}.controls button{width:auto;background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 10px}.controls .danger{color:#b42318}</style>
<h2 id='title'>My series</h2><div id='controls' class='controls'></div><div id='list' class='grid'></div>
<script>
const init=Telegram.WebApp.initData,H={'X-Telegram-Init-Data':init},title=document.querySelector('#title'),controls=document.querySelector('#controls'),list=document.querySelector('#list');Telegram.WebApp.ready();
async function api(url,options={}){let r=await fetch(url,{...options,headers:{...H,...(options.headers||{})}});if(!r.ok)throw new Error(await r.text());return r.json()}
async function img(url){if(!url)return '';let r=await fetch(url,{headers:H});if(!r.ok)return '';return URL.createObjectURL(await r.blob())}
function card(name,cover,sub,onClick){let b=document.createElement('button');b.className='card';b.innerHTML=(cover?`<img class='cover' src='${cover}'>`:'<div class='cover'></div>')+`<b></b><div class='muted'></div>`;b.querySelector('b').textContent=name;b.querySelector('.muted').textContent=sub;b.onclick=onClick;return b}
async function load(){title.textContent='My series';controls.innerHTML='';let rows=await api('/api/series');list.innerHTML='';for(let x of rows){list.append(card(x.title,await img(x.cover),x.count+' items',()=>open(x)))}}
async function open(x){title.textContent=x.title;controls.innerHTML='';let back=document.createElement('button');back.textContent='Back';back.onclick=load;let rename=document.createElement('button');rename.textContent='Rename';rename.onclick=async()=>{let value=prompt('New series title',x.title);if(!value||!value.trim())return;await api('/api/series/'+x.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:value.trim()})});await load()};let remove=document.createElement('button');remove.className='danger';remove.textContent='Delete';remove.onclick=async()=>{if(!confirm('Delete this series and its thumbnails?'))return;await api('/api/series/'+x.id,{method:'DELETE'});await load()};controls.append(back,rename,remove);let rows=await api('/api/series/'+x.id);list.innerHTML='';for(let v of rows){list.append(card(v.filename,await img(v.thumb),'Tap to send to Telegram',async()=>{await api('/api/video/'+v.id+'/send',{method:'POST'});Telegram.WebApp.showAlert(v.filename+' was sent to this chat.')}))}}
load().catch(()=>list.textContent='Open this page from Telegram.');
</script></html>"""


HTML = """<!doctype html><html lang='en'><meta name='viewport' content='width=device-width,initial-scale=1'>
<script src='https://telegram.org/js/telegram-web-app.js'></script>
<style>body{font-family:system-ui;margin:16px;background:#f5f5f5;color:#111}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.card{background:#fff;border:0;border-radius:12px;overflow:hidden;padding:10px;text-align:left;font:inherit}.cover{width:100%;aspect-ratio:16/9;object-fit:cover;background:#ddd}.muted{color:#666;font-size:13px}.controls{display:flex;gap:8px;margin:0 0 14px}.controls button{background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 10px}.controls .danger{color:#b42318}</style>
<h2 id='title'>My series</h2><div id='controls' class='controls'></div><div id='list' class='grid'></div>
<script>
(function(){
var title=document.getElementById('title'),controls=document.getElementById('controls'),list=document.getElementById('list');
var webApp=window.Telegram&&window.Telegram.WebApp;
if(!webApp){list.textContent='Open this page from Telegram.';return;}
var headers={'X-Telegram-Init-Data':webApp.initData};webApp.ready();
async function api(url,options){options=options||{};options.headers=Object.assign({},headers,options.headers||{});var response=await fetch(url,options);if(!response.ok)throw new Error(await response.text());return response.json();}
async function imageUrl(url){if(!url)return '';var response=await fetch(url,{headers:headers});if(!response.ok)return '';return URL.createObjectURL(await response.blob());}
function makeCard(name,cover,subtitle,handler){var card=document.createElement('button');card.className='card';if(cover){var image=document.createElement('img');image.className='cover';image.src=cover;card.appendChild(image);}else{var blank=document.createElement('div');blank.className='cover';card.appendChild(blank);}var nameEl=document.createElement('b');nameEl.textContent=name;card.appendChild(nameEl);var sub=document.createElement('div');sub.className='muted';sub.textContent=subtitle;card.appendChild(sub);card.onclick=handler;return card;}
async function load(){title.textContent='My series';controls.innerHTML='';var rows=await api('/api/series');list.innerHTML='';for(var i=0;i<rows.length;i++){var series=rows[i];var cover=await imageUrl(series.cover);list.appendChild(makeCard(series.title,cover,series.count+' items',(function(value){return function(){openSeries(value);};})(series)));}}
function showError(error){webApp.showAlert('작업에 실패했습니다: '+String(error.message||error));}
async function openSeries(series){title.textContent=series.title;controls.innerHTML='';var back=document.createElement('button');back.textContent='Back';back.onclick=load;var add=document.createElement('button');add.textContent='Add media';add.onclick=async function(){try{await api('/api/series/'+series.id+'/add',{method:'POST'});webApp.showAlert('이제 봇 채팅에서 영상 또는 사진을 보내세요. 5초 뒤 자동으로 추가됩니다.');}catch(error){showError(error);}};var rename=document.createElement('button');rename.textContent='Rename';rename.onclick=async function(){var value=prompt('New series title',series.title);if(!value||!value.trim())return;try{await api('/api/series/'+series.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:value.trim()})});await load();}catch(error){showError(error);}};var remove=document.createElement('button');remove.className='danger';remove.textContent='Delete';remove.onclick=async function(){if(!confirm('Delete this series and its thumbnails?'))return;try{await api('/api/series/'+series.id,{method:'DELETE'});await load();}catch(error){showError(error);}};controls.append(back,add,rename,remove);var rows=await api('/api/series/'+series.id);list.innerHTML='';for(var i=0;i<rows.length;i++){var video=rows[i];var thumb=await imageUrl(video.thumb);list.appendChild(makeCard(video.filename,thumb,'Tap to send to Telegram',(function(value){return async function(){try{await api('/api/video/'+value.id+'/send',{method:'POST'});webApp.showAlert(value.filename+' was sent to this chat.');}catch(error){showError(error);}};})(video)));}}
load().catch(function(error){list.textContent='라이브러리를 불러오지 못했습니다.';showError(error);});
})();
</script></html>"""


async def resume_jobs():
    await asyncio.sleep(1)
    for job in db.recover_jobs():
        try:
            if job["status"] == "waiting_title":
                count = len(db.job_files(int(job["id"])))
                await tg.send_text(
                    int(job["chat_id"]),
                    f"서버가 다시 시작되었습니다. 보관된 파일 {count}개의 시리즈 제목을 보내주세요.\n/cancel 로 취소할 수 있습니다.",
                )
            elif job["status"] == "queued":
                schedule_job(int(job["id"]))
        except Exception:
            LOGGER.exception("Could not resume job %s", job["id"])


@app.on_event("startup")
async def start_polling():
    asyncio.create_task(resume_jobs())
    asyncio.create_task(polling())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
