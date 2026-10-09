"""
Telegram Video Panel — панель для пакетного скачивания видео из Telegram-групп.

Работает в двух режимах:
  • локально (по умолчанию): python app.py  → http://127.0.0.1:8765
  • на сервере: TVP_SERVER=1 и TVP_PASSWORD=... (см. install.sh) — вход по паролю,
    видео складываются в TVP_DOWNLOAD_DIR, их можно скачать или удалить прямо из панели.

Переменные окружения:
  TVP_HOST, TVP_PORT        адрес и порт (по умолчанию 127.0.0.1:8765)
  TVP_SERVER=1              серверный режим
  TVP_PASSWORD              пароль панели (обязателен в серверном режиме)
  TVP_DATA_DIR              где хранить config.json и сессию Telegram (по умолчанию рядом с app.py)
  TVP_DOWNLOAD_DIR          корневая папка для видео
"""

# Версия панели (семантическая: исправления → 1.0.1, новые возможности → 1.1.0, несовместимые изменения → 2.0.0).
# Меняется при каждом изменении кода; что изменилось — в CHANGELOG.md.
VERSION = "1.7.2"

import asyncio
import io
import sqlite3
import tempfile
import hashlib
import html as htmllib
import hmac
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import subprocess
import time
import webbrowser
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Route
from telethon import TelegramClient, errors
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import InputMessagesFilterVideo

BASE = Path(__file__).resolve().parent
SERVER = os.environ.get("TVP_SERVER") == "1"
HOST = os.environ.get("TVP_HOST", "127.0.0.1")
PORT = int(os.environ.get("TVP_PORT", "8765"))
PASSWORD = os.environ.get("TVP_PASSWORD", "")
DATA_DIR = Path(os.environ.get("TVP_DATA_DIR", str(BASE))).expanduser()
DOWNLOAD_ROOT = Path(os.environ.get(
    "TVP_DOWNLOAD_DIR",
    str(DATA_DIR / "videos") if SERVER else str(Path.home() / "Downloads" / "telegram_videos"),
)).expanduser()

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SECRET_FILE = DATA_DIR / ".secret"
SESSION = str(DATA_DIR / "tg_session")
VIDEO = InputMessagesFilterVideo()
COOKIE = "tvp_auth"
INDEX_FILE = ".tvp_index.json"  # в каждой папке: "chat_id:msg_id" -> имя файла (для пропуска уже скачанных)
EXT_CHOICES = (".mp4", ".mov", ".m4v", ".mkv")  # выбор расширения: файл только переименовывается, не перекодируется
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mpg", ".mpeg", ".wmv", ".flv", ".ts"}

state = {"client": None, "phone": None, "code_hash": None, "ya_device": None, "ya_device_task": None}
entities = {}


def human(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


# ---------- авторизация панели ----------

def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
        try:
            SECRET_FILE.chmod(0o600)
        except Exception:
            pass
    return SECRET_FILE.read_text().strip().encode()


def auth_token() -> str:
    # меняется при смене пароля — старые входы становятся недействительны
    return hmac.new(_secret(), ("tvp:" + PASSWORD).encode(), hashlib.sha256).hexdigest()


def auth_enabled() -> bool:
    return bool(PASSWORD)


def is_authed(request: Request) -> bool:
    if not auth_enabled():
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), auth_token())


# ---------- конфиг и Telegram-клиент ----------

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


async def get_client():
    if state["client"] is None:
        cfg = load_config()
        if not cfg.get("api_id") or not cfg.get("api_hash"):
            return None
        state["client"] = TelegramClient(SESSION, int(cfg["api_id"]), cfg["api_hash"])
    c = state["client"]
    if not c.is_connected():
        await c.connect()
    return c


async def need_client():
    c = await get_client()
    if c is None:
        raise HTTPException(400, "Сначала укажите api_id и api_hash")
    return c


async def need_auth():
    c = await need_client()
    if not await c.is_user_authorized():
        raise HTTPException(401, "Нужно войти в Telegram")
    return c


async def drop_client():
    c, state["client"] = state["client"], None
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass


# ---------- папки ----------

def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")
    return s[:80] or "telegram"


def resolve_folder(user_value: str, chat_name: str, tz=timezone.utc) -> Path:
    """Локально — любой путь. На сервере — только подпапка внутри DOWNLOAD_ROOT;
    если не задана — «ГГГГ-ММ-ДД Название группы» (дата скачивания по часовому поясу браузера)."""
    v = (user_value or "").strip()
    if not SERVER:
        return Path(os.path.expandvars(os.path.expanduser(v or str(DOWNLOAD_ROOT))))
    sub = safe_name(v) if v else safe_name(f"{datetime.now(tz):%Y-%m-%d} {chat_name}")
    return DOWNLOAD_ROOT / sub


def inside_root(rel: str) -> Path:
    p = (DOWNLOAD_ROOT / rel).resolve()
    root = DOWNLOAD_ROOT.resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "Недопустимый путь")
    return p


# ---------- имена файлов ----------

def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    return (stem[:150] + ext[:10]) if stem else ""


def original_name(msg, tz, ext_override=None) -> str:
    """Имя как при ручном сохранении в Telegram Desktop: исходное имя файла,
    а если его нет — video_ГГГГ-ММ-ДД_ЧЧ-ММ-СС.<расширение>."""
    f = msg.file
    ext = (f.ext if f and f.ext else None) or ".mp4"
    name = safe_filename(f.name) if f and f.name else ""
    if not name:
        name = f"video_{msg.date.astimezone(tz):%Y-%m-%d_%H-%M-%S}{ext}"
    elif not os.path.splitext(name)[1]:
        name += ext
    if ext_override:
        name = os.path.splitext(name)[0] + ext_override
    return name


def unique_path(folder: Path, name: str, taken: set) -> Path:
    """name.mp4 → name (1).mp4 → name (2).mp4 … если имя уже занято."""
    stem, ext = os.path.splitext(name)
    p, n = folder / name, 1
    while p.exists() or p.name in taken:
        p, n = folder / f"{stem} ({n}){ext}", n + 1
    return p


def load_index(folder: Path) -> dict:
    try:
        return json.loads((folder / INDEX_FILE).read_text("utf-8"))
    except Exception:
        return {}


def save_index(folder: Path, idx: dict):
    tmp = folder / (INDEX_FILE + ".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
    tmp.replace(folder / INDEX_FILE)


META_FILE = ".tvp_meta.json"  # период скачанных видео в папке: {"from", "to", "tz"} — для {period} в сообщении


def update_period(folder: Path, d_from, d_to, tz):
    """Расширяет сохранённый период папки до объединения со скачанным сейчас."""
    try:
        m = json.loads((folder / META_FILE).read_text("utf-8"))
    except Exception:
        m = {}
    if m.get("from"):
        d_from = min(d_from, datetime.fromisoformat(m["from"]))
    if m.get("to"):
        d_to = max(d_to, datetime.fromisoformat(m["to"]))
    offset = tz.utcoffset(None).total_seconds() // 60
    (folder / META_FILE).write_text(json.dumps(
        {"from": d_from.isoformat(), "to": d_to.isoformat(), "tz": offset}), "utf-8")


def period_text(folder: Path) -> str:
    try:
        m = json.loads((folder / META_FILE).read_text("utf-8"))
        tz = timezone(timedelta(minutes=m.get("tz", 0)))
        d_from = datetime.fromisoformat(m["from"]).astimezone(tz)
        d_to = (datetime.fromisoformat(m["to"]) - timedelta(seconds=1)).astimezone(tz)
        return f"с {d_from:%d.%m.%Y} по {d_to:%d.%m.%Y}"
    except Exception:
        return "не указан"


def parse_date(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Неверная дата: {v}")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def last_id_before(c, entity, when):
    """id последнего сообщения строго раньше when (None — таких нет). get_messages(offset_date=…) у альбомов,
    отправленных в одну секунду, может вернуть не самое последнее — досматриваем сообщения сразу за ним."""
    before = await c.get_messages(entity, limit=1, offset_date=when)
    if not before:
        return None
    bid = before[0].id
    async for m in c.iter_messages(entity, min_id=bid, reverse=True, limit=200):
        if m.date >= when:
            break
        bid = max(bid, m.id)
    return bid


async def videos_since(c, entity, when, total) -> int:
    """Сколько видео отправлено начиная с момента when. min_date/max_date в поиске Telegram
    игнорирует, поэтому берём последнее сообщение до when и смотрим offset_id_offset."""
    if when is None:
        return total
    if when >= datetime.now(timezone.utc):
        return 0
    bid = await last_id_before(c, entity, when)
    if bid is None:
        return total
    r = await c(SearchRequest(peer=entity, q="", filter=VIDEO, min_date=None, max_date=None,
                              offset_id=bid + 1, add_offset=0, limit=1, max_id=0, min_id=0, hash=0))
    if not r.messages:
        return total
    off = getattr(r, "offset_id_offset", None)
    if off is not None:
        return off
    # Telegram не присылает offset_id_offset, когда он равен 0 (так бывает в обычных группах) —
    # точно пересчитываем видео новее границы по id (обычно это один пустой запрос)
    newer = 0
    async for m in c.iter_messages(entity, filter=VIDEO, min_id=bid):
        newer += m.id > bid
    return newer


async def count_videos(c, entity, date_from=None, date_to=None) -> int:
    total = (await c.get_messages(entity, limit=0, filter=VIDEO)).total
    if not date_from and not date_to:
        return total
    return max(0, await videos_since(c, entity, date_from, total)
               - await videos_since(c, entity, date_to, total))


# ---------- скачивание ----------

def scale_filter(n):
    # ограничиваем короткую сторону до n (подходит и для вертикальных видео), не увеличиваем
    return (f"scale='if(gt(iw,ih),-2,min({n},iw))':"
            f"'if(gt(iw,ih),min({n},ih),-2)'")


async def convert(src: Path, dst: Path, quality: str):
    tmp_out = dst.with_name(dst.stem + ".conv.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-vf", scale_filter(int(quality)),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp_out)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        tmp_out.unlink(missing_ok=True)
        raise
    if proc.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        raise RuntimeError(err.decode(errors="ignore").strip()[-300:] or "ffmpeg error")
    tmp_out.replace(dst)
    src.unlink(missing_ok=True)


# ---------- Яндекс Диск и отправка ссылки ----------

YA_API = "https://cloud-api.yandex.net/v1/disk"
YA_OAUTH = "https://oauth.yandex.ru"
DEFAULT_SETTINGS = {
    "ya_root": "Telegram Video Panel",   # папка на Диске, внутри — подпапки как на сервере
    "auto_upload": False,                # загружать на Диск по мере скачивания (только если Диск подключён)
    "archive": False,                    # после проверки загрузки удалять видео с сервера (папка — «в архиве»)
    "info_file": True,                   # создавать info.txt (текст сообщения со ссылкой) в папке сервера и Диска
    "clean_age": False, "clean_age_days": 30,      # автоочистка: удалять видео старше N дней (только если есть на Диске)
    "clean_space": False, "clean_space_gb": 5,     # …или самые старые, пока свободного места меньше X ГБ
    "warn_space_gb": 3,                            # предупреждать, когда свободного места меньше Y ГБ
    "update_check": True,                          # раз в 6 часов проверять новую версию на GitHub
    "dl_jobs": 2,                        # заданий на скачивание одновременно
    "tg_parallel": 3,                    # видео из Telegram одновременно (всего)
    "ya_parallel": 3,                    # файлов на/с Яндекс Диска одновременно (всего)
    "send_link": True,                   # отправлять ссылку после загрузки
    "target": "source",                  # source — группа, откуда видео; saved — «Избранное»; chat — target_chat
    "target_chat": None, "target_name": "",
    "template": "📁 {folder}\nПериод: {period}\nВидео: {count} ({size})\n{link}",
}


def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update(load_config().get("settings") or {})
    return out


def ya_auto_enabled() -> bool:
    return bool(get_settings().get("auto_upload")) and bool(ya_conf().get("token"))


def ya_conf() -> dict:
    return load_config().get("yandex") or {}


def set_ya_conf(**kw):
    cfg = load_config()
    y = cfg.get("yandex") or {}
    y.update(kw)
    cfg["yandex"] = {k: v for k, v in y.items() if v is not None}
    save_config(cfg)


def ya_error(r: httpx.Response) -> str:
    try:
        d = r.json()
        return d.get("description") or d.get("message") or d.get("error_description") or d.get("error") or r.text
    except Exception:
        return f"HTTP {r.status_code}"


def ya_client(token=None, **kw) -> httpx.AsyncClient:
    token = token or ya_conf().get("token")
    if not token:
        raise HTTPException(400, "Яндекс Диск не подключён")
    return httpx.AsyncClient(headers={"Authorization": f"OAuth {token}"},
                             timeout=httpx.Timeout(60, read=600, write=600), **kw)


async def ya_info(token=None) -> dict:
    async with ya_client(token) as h:
        r = await h.get(YA_API + "/")
    if r.status_code == 401:
        raise HTTPException(401, "Токен Яндекс Диска недействителен — подключите Диск заново")
    if r.status_code != 200:
        raise HTTPException(400, f"Яндекс Диск: {ya_error(r)}")
    d = r.json()
    u = d.get("user") or {}
    return {"user": u.get("display_name") or u.get("login") or "", "total": d.get("total_space", 0),
            "used": d.get("used_space", 0), "max_file": d.get("max_file_size")}


class YaFatal(RuntimeError):
    """Ошибка Диска, которую повтор не исправит (нет места, файл слишком большой, неверный запрос)."""


def ya_check(r: httpx.Response, ok_codes, what: str):
    if r.status_code in ok_codes:
        return
    msg = f"{what}: {ya_error(r)} (HTTP {r.status_code})"
    # временные: таймаут, «ресурс заблокирован», «слишком много запросов», ошибки сервера, разовый 401
    if r.status_code in (401, 408, 423, 429) or r.status_code >= 500 and r.status_code != 507:
        raise RuntimeError(msg)
    raise YaFatal(msg)


YA_RETRY_DELAYS = (3, 10, 30, 60, 120)  # паузы между попытками, всего попыток — на одну больше


async def ya_retry(j, what: str, fn, act=None):
    """Выполняет действие с Диском, при временной ошибке (сеть, таймаут, 5xx, 423, 429, 401) повторяет его.
    j — задание (лог и состояние повтора), act — строка активного файла (там видно «повтор через…»)."""
    for i in range(len(YA_RETRY_DELAYS) + 1):
        try:
            return await fn()
        except (asyncio.CancelledError, YaFatal):
            raise
        except Exception as e:
            if i == len(YA_RETRY_DELAYS):
                raise
            d = YA_RETRY_DELAYS[i]
            n = f"попытка {i + 2} из {len(YA_RETRY_DELAYS) + 1}"
            jlog(j, f"↻ {what}: {str(e) or type(e).__name__} — повтор через {d} с ({n})")
            if act is not None:
                act["phase"] = f"↻ повтор через {d} с ({n})"
            else:
                j["ya"]["retry"] = f"{what}: повтор через {d} с ({n})"
            try:
                await asyncio.sleep(d)
            finally:
                if act is None:
                    j["ya"]["retry"] = None


async def ya_mkdirs(h: httpx.AsyncClient, path: str):
    cur = "disk:"
    for part in [x for x in path[len("disk:/"):].split("/") if x]:
        cur += "/" + part
        r = await h.put(YA_API + "/resources", params={"path": cur})
        ya_check(r, (201, 409), f"не удалось создать папку {cur[len('disk:'):]}")


async def ya_move(h: httpx.AsyncClient, src: str, dst: str):
    r = await h.post(YA_API + "/resources/move", params={"from": src, "path": dst, "overwrite": "true"})
    ya_check(r, (200, 201, 202), "не удалось переименовать после загрузки")
    await ya_wait_operation(h, r, "переименовать файл после загрузки")


async def ya_publish(h: httpx.AsyncClient, remote: str) -> str:
    r = await h.put(YA_API + "/resources/publish", params={"path": remote})
    ya_check(r, (200, 201), "не удалось открыть доступ по ссылке")
    r = await h.get(YA_API + "/resources", params={"path": remote, "fields": "public_url"})
    ya_check(r, (200,), "не удалось получить ссылку")
    link = r.json().get("public_url")
    if not link:
        raise RuntimeError("Яндекс Диск не вернул публичную ссылку")
    return link


async def ya_wait_operation(h: httpx.AsyncClient, r: httpx.Response, what: str):
    """Ждёт асинхронную операцию Диска (ответ 202)."""
    if r.status_code != 202:
        return
    href = r.json().get("href")
    for _ in range(300):
        st = (await h.get(href)).json().get("status")
        if st == "success":
            return
        if st == "failed":
            raise RuntimeError(f"Яндекс не смог {what}")
        await asyncio.sleep(1)
    raise RuntimeError(f"Яндекс слишком долго не может {what}")


async def ya_list(h: httpx.AsyncClient, path: str) -> dict:
    out, offset = {}, 0
    while True:
        r = await h.get(YA_API + "/resources", params={
            "path": path, "limit": 1000, "offset": offset, "fields": "_embedded.items.name,_embedded.items.size,_embedded.total"})
        if r.status_code == 404:
            return out
        ya_check(r, (200,), "не удалось получить список файлов")
        emb = r.json().get("_embedded") or {}
        items = emb.get("items") or []
        out.update({i["name"]: i.get("size") for i in items})
        offset += len(items)
        if not items or offset >= emb.get("total", 0):
            return out


def folder_source_chat(folder: Path):
    """id группы, из которой скачана папка — по ключам .tvp_index.json («chat_id:msg_id»)."""
    ids = [k.split(":")[0] for k in load_index(folder)]
    if not ids:
        return None
    try:
        return int(max(set(ids), key=ids.count))
    except ValueError:
        return None


def render_template(tpl: str, **v) -> str:
    for k, val in v.items():
        tpl = tpl.replace("{" + k + "}", str(val))
    return tpl


INFO_FILE = "info.txt"  # в папке на сервере и на Диске: тот же текст, что в сообщении со ссылкой


def link_text(folder: Path, link: str, count: int, size: int) -> str:
    return render_template(get_settings()["template"] or DEFAULT_SETTINGS["template"],
                           folder=folder.name, period=period_text(folder), count=count, size=human(size), link=link)


async def write_info_file(j, folder: Path, remote: str, link: str, count: int, size: int):
    """info.txt в папке сервера и на Диске (UTF-8 с BOM и CRLF — открывается и старым Блокнотом)."""
    data = link_text(folder, link, count, size).replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig")
    (folder / INFO_FILE).write_bytes(data)

    async def put():
        r = await h.get(YA_API + "/resources/upload", params={"path": f"{remote}/{INFO_FILE}", "overwrite": "true"})
        ya_check(r, (200,), "не удалось получить адрес для загрузки")
        async with httpx.AsyncClient(timeout=60) as up:
            ya_check(await up.put(r.json()["href"], content=data), (200, 201, 202), "Диск не принял файл")

    async with ya_client() as h:
        await ya_retry(j, INFO_FILE, put)


async def send_link(folder: Path, chat_id, link: str, count: int, size: int, mode=None, target_chat=None):
    """Отправляет ссылку по шаблону. mode: saved / chat (target_chat) / source; по умолчанию — из настроек."""
    st = get_settings()
    c = await need_auth()
    if mode is None:
        mode, target_chat = st["target"], st.get("target_chat")
    if mode == "saved":
        target, label = "me", "«Избранное»"
    elif mode == "chat" and target_chat:
        target = await get_entity(c, int(target_chat))
        label = f"«{getattr(target, 'title', None) or getattr(target, 'first_name', None) or target_chat}»"
    else:
        chat_id = chat_id or folder_source_chat(folder)
        if not chat_id:
            raise RuntimeError("не знаю, из какой группы эта папка — выберите чат")
        target = await get_entity(c, int(chat_id))
        label = f"«{getattr(target, 'title', None) or chat_id}»"
    await c.send_message(target, link_text(folder, link, count, size), link_preview=False)
    return label


def local_videos(folder: Path):
    return sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT
                   and not f.name.endswith(".conv.mp4")), key=lambda f: f.name.lower())


# ---------- задания: очередь, параллельность, архив ----------
#
# Каждое действие — задание (job): download (Telegram → сервер, при автозагрузке ещё и на Диск),
# upload (папка сервера → Диск), restore (Диск → сервер для папки «в архиве»).
# Планировщик jobs_loop() запускает задания из очереди: download — не больше settings.dl_jobs одновременно,
# в одну папку — только одно задание за раз. Внутри заданий файлы идут параллельно, общие лимиты —
# tg_slots (видео из Telegram) и ya_slots (файлы на/с Диска). Список заданий сохраняется в jobs.json,
# после перезапуска службы незавершённые задания продолжаются (уже сделанное пропускается).

JOBS_FILE = DATA_DIR / "jobs.json"
ARCHIVE_FILE = ".tvp_archived.json"  # видео папки удалены с сервера после проверки, лежат на Яндекс Диске
KEEP_FINISHED = 50


class Limiter:
    """Ограничитель параллельности; предел берётся из настроек при каждой проверке, поэтому меняется на лету."""

    def __init__(self, key):
        self.key, self.active, self.waiters = key, 0, []

    def limit(self) -> int:
        return max(1, int(get_settings()[self.key]))

    async def acquire(self):
        while self.active >= self.limit():
            fut = asyncio.get_running_loop().create_future()
            self.waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self.waiters:
                    self.waiters.remove(fut)
        self.active += 1

    def release(self, *_):
        self.active -= 1
        self.poke()

    def poke(self):
        for f in self.waiters:
            if not f.done():
                f.set_result(None)


tg_slots, ya_slots = Limiter("tg_parallel"), Limiter("ya_parallel")
jobs: dict = {}
job_tasks: dict = {}
jobs_state = {"dirty": False, "wake": None, "shutdown": False}


def mark():
    jobs_state["dirty"] = True


def wake_scheduler():
    mark()
    if jobs_state["wake"] is not None:
        jobs_state["wake"].set()


def jlog(j, msg):
    j["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
    del j["log"][:-300]
    mark()


def fresh_dl():
    return {"total": 0, "index": 0, "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0, "active": {}}


def fresh_ya():
    return {"total": 0, "uploaded": 0, "skipped": 0, "errors": 0, "bytes": 0, "active": {}, "waiting": False,
            "retry": None, "link": None, "sent": None, "remote": "", "archived": False}


def new_job(kind, folder: Path, **params):
    j = {"id": secrets.token_hex(4), "kind": kind, "status": "queued", "folder": folder.name, "path": str(folder),
         "params": params, "created": time.time(), "started": None, "finished": None, "error": None,
         "dl": None, "ya": None, "log": []}
    jobs[j["id"]] = j
    wake_scheduler()
    return j


def save_jobs():
    jobs_state["dirty"] = False
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(jobs.values()), ensure_ascii=False), "utf-8")
    tmp.replace(JOBS_FILE)


def load_jobs():
    try:
        data = json.loads(JOBS_FILE.read_text("utf-8"))
    except Exception:
        return
    for j in data:
        if j.get("status") in ("running", "queued"):
            if j["status"] == "running":
                jlog(j, "Служба панели перезапускалась — задание продолжится, уже сделанное пропустится")
            j["status"] = "queued"
        for part in ("dl", "ya"):
            if j.get(part):
                j[part].update(active={}, **({"waiting": False, "retry": None} if part == "ya" else {}))
        jobs[j["id"]] = j


def prune_jobs():
    done = sorted((j for j in jobs.values() if j["status"] not in ("queued", "running")),
                  key=lambda j: j.get("finished") or 0)
    for j in done[:-KEEP_FINISHED]:
        jobs.pop(j["id"], None)


def folder_jobs(name, statuses=("queued", "running")):
    return [j for j in jobs.values() if j["folder"] == name and j["status"] in statuses]


async def jobs_loop():
    """Планировщик заданий + периодическое сохранение jobs.json."""
    wake = jobs_state["wake"]
    while True:
        try:
            await asyncio.wait_for(wake.wait(), 2)
        except asyncio.TimeoutError:
            pass
        wake.clear()
        try:
            st = get_settings()
            running = [j for j in jobs.values() if j["status"] == "running"]
            busy = {j["folder"] for j in running}
            n_dl = sum(1 for j in running if j["kind"] == "download")
            for j in sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: j["created"]):
                if j["folder"] in busy:
                    continue  # в эту папку уже работает задание — ждём
                if j["kind"] == "download":
                    if n_dl >= max(1, int(st["dl_jobs"])):
                        continue
                    n_dl += 1
                busy.add(j["folder"])
                start_job(j)
            if jobs_state["dirty"]:
                prune_jobs()
                save_jobs()
        except Exception as e:
            print("Планировщик заданий:", e)


def start_job(j):
    j.update(status="running", started=time.time(), finished=None, error=None)
    mark()
    job_tasks[j["id"]] = asyncio.create_task(run_job(j))


async def run_job(j):
    try:
        if j["kind"] == "download":
            await run_download(j)
        elif j["kind"] == "upload":
            await ya_stage(j, Path(j["path"]))
        elif j["kind"] == "restore":
            await run_restore(j)
        j["status"] = "done"
    except asyncio.CancelledError:
        if jobs_state["shutdown"]:
            return  # служба останавливается — после запуска задание продолжится
        j["status"] = "stopped"
        jlog(j, "Остановлено")
    except HTTPException as e:
        j.update(status="error", error=str(e.detail))
        jlog(j, f"Ошибка: {e.detail}")
    except Exception as e:
        j.update(status="error", error=str(e) or type(e).__name__)
        jlog(j, f"Ошибка: {j['error']}")
    finally:
        job_tasks.pop(j["id"], None)
        if not jobs_state["shutdown"]:
            j["finished"] = time.time()
            for part in ("dl", "ya"):
                if j.get(part):
                    j[part]["active"] = {}
            if j.get("ya"):
                j["ya"].update(waiting=False, retry=None)
            asyncio.create_task(cleanup_after_job())
        wake_scheduler()


async def cleanup_after_job():
    try:
        await cleanup_run("после задания")
    except Exception as e:
        clog(f"⚠ ошибка очистки: {e}")


async def cancel_all(tasks):
    for t in list(tasks):
        t.cancel()
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


# --- скачивание из Telegram ---

async def run_download(j):
    p = j["params"]
    folder = Path(j["path"])
    folder.mkdir(parents=True, exist_ok=True)
    tz = timezone(timedelta(minutes=p.get("tz", 0)))
    date_from, date_to = parse_date(p.get("date_from")), parse_date(p.get("date_to"))
    chat_id = p["chat_id"]
    c = await need_auth()
    entity = await get_entity(c, chat_id)
    dl = j["dl"] = fresh_dl()
    dl["total"] = await count_videos(c, entity, date_from, date_to)
    period = (f"с {date_from.astimezone(tz):%d.%m.%Y}" if date_from else "с начала") + \
             f" по {date_to.astimezone(tz):%d.%m.%Y %H:%M}"
    jlog(j, f"Группа «{p.get('chat_name')}», период {period}: найдено видео: {dl['total']}")
    jlog(j, f"Папка: {folder}")
    idx = load_index(folder)
    in_archive = set((load_archive(folder).get("files") or {}))
    quality, ext = p.get("quality", "original"), p.get("ext", "original")
    convert_to = ".mp4" if quality != "original" and shutil.which("ffmpeg") else None
    ext_override = convert_to or (ext if ext in EXT_CHOICES else None)
    oldest_first = p.get("oldest_first", True)

    ya_q, ctx, ya_task = None, {"ok": False}, None
    if p.get("ya") and ya_conf().get("token"):
        ya_q = asyncio.Queue()
        ya_task = asyncio.create_task(ya_stage(j, folder, ya_q, ctx, chat_id))
        jlog(j, "Яндекс Диск: каждое видео загружается сразу после скачивания")

    taken, tasks, first = set(), set(), None
    try:
        try:
            if oldest_first:
                bid = await last_id_before(c, entity, date_from) if date_from else None
                it = c.iter_messages(entity, filter=VIDEO, reverse=True, min_id=bid or 0)
            else:
                it = c.iter_messages(entity, filter=VIDEO, offset_date=date_to)
            async for msg in it:
                if date_from and msg.date < date_from:
                    if oldest_first:
                        continue
                    break
                if msg.date >= date_to:
                    if oldest_first:
                        break
                    continue
                dl["index"] += 1
                first = msg.date if first is None else min(first, msg.date)
                key = f"{chat_id}:{msg.id}"
                have = idx.get(key)
                if have and ((folder / have).exists() or have in in_archive):
                    dl["skipped"] += 1
                    continue
                path = unique_path(folder, original_name(msg, tz, ext_override), taken)
                taken.add(path.name)
                await tg_slots.acquire()
                t = asyncio.create_task(dl_one(c, j, msg, path, key, folder, idx, quality, ext, tz,
                                               convert_to, ya_q, taken))
                t.add_done_callback(tg_slots.release)
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
            jlog(j, f"Скачивание завершено. Скачано: {dl['downloaded']}, уже были: {dl['skipped']}, "
                    f"ошибок: {dl['errors']}")
            ctx["ok"] = dl["errors"] == 0
        finally:
            await cancel_all(tasks)
            if (date_from or first) and date_to:
                try:
                    update_period(folder, date_from or first, min(date_to, datetime.now(timezone.utc)), tz)
                except Exception as e:
                    jlog(j, f"⚠ не удалось сохранить период: {e}")
            if ya_q is not None:
                ya_q.put_nowait(None)  # загрузчик догрузит очередь, проверит и опубликует папку
        if ya_task is not None:
            await ya_task
    except asyncio.CancelledError:
        if ya_task is not None:
            ya_task.cancel()
            await asyncio.gather(ya_task, return_exceptions=True)
        raise


async def dl_one(c, j, msg, path, key, folder, idx, quality, ext, tz, convert_to, ya_q, taken):
    dl = j["dl"]
    label = path.name
    size = msg.file.size if msg.file and msg.file.size else 0
    act = dl["active"][label] = {"done": 0, "total": size, "speed": 0, "phase": "download"}
    tmp = folder / f".tvp_{msg.id}.part"
    try:
        for attempt in range(1, 5):
            try:
                t0 = time.monotonic()
                act.update(done=0, speed=0, phase="download")

                def progress(done, total, t0=t0):
                    act["done"], act["total"] = done, total or act["total"]
                    dt = time.monotonic() - t0
                    act["speed"] = done / dt if dt > 0 else 0

                got = await c.download_media(msg, file=str(tmp), progress_callback=progress)
                if not got:
                    raise RuntimeError("пустой ответ от Telegram")
                tmp = Path(got)
                break
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as e:
                if attempt == 4:
                    raise
                act["phase"] = f"Telegram просит подождать {e.seconds} с"
                jlog(j, f"⏸ {label}: Telegram просит подождать {e.seconds} с — скачаю после паузы")
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                if attempt == 4:
                    raise
                d = (5, 15, 45)[attempt - 1]
                act["phase"] = f"↻ повтор через {d} с (попытка {attempt + 1} из 4)"
                jlog(j, f"↻ {label}: {str(e) or type(e).__name__} — повтор через {d} с (попытка {attempt + 1} из 4)")
                await asyncio.sleep(d)
        if convert_to:
            act["phase"] = "convert"
            try:
                await convert(tmp, path, quality)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                jlog(j, f"⚠ {label}: не удалось сжать ({e}), сохраняю оригинал")
                taken.discard(path.name)
                path = unique_path(folder, original_name(msg, tz, ext if ext in EXT_CHOICES else None), taken)
                taken.add(path.name)
                tmp.replace(path)
        else:
            tmp.replace(path)
        idx[key] = path.name
        save_index(folder, idx)
        dl["downloaded"] += 1
        dl["bytes"] += path.stat().st_size
        jlog(j, f"✓ {path.name}  ({human(path.stat().st_size)})")
        if ya_q is not None:
            ya_q.put_nowait(path)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        dl["errors"] += 1
        taken.discard(path.name)
        jlog(j, f"✗ {label}: {e}")
    finally:
        dl["active"].pop(label, None)
        if Path(tmp) != path:
            Path(tmp).unlink(missing_ok=True)
        mark()


# --- Яндекс Диск: загрузка папки, проверка, ссылка, архив ---

def load_archive(folder: Path) -> dict:
    try:
        return json.loads((folder / ARCHIVE_FILE).read_text("utf-8"))
    except Exception:
        return {}


def save_archive(folder: Path, a: dict):
    tmp = folder / (ARCHIVE_FILE + ".tmp")
    tmp.write_text(json.dumps(a, ensure_ascii=False), "utf-8")
    tmp.replace(folder / ARCHIVE_FILE)


def archive_folder(folder: Path, remote: str, link: str, local_files):
    """Видео проверены на Диске — удаляем их с сервера, а папку (индекс, период, список) оставляем."""
    a = load_archive(folder)
    files = a.get("files") or {}
    files.update({f.name: f.stat().st_size for f in local_files})
    save_archive(folder, {"remote": remote, "link": link, "at": time.time(), "files": files})
    for f in local_files:
        f.unlink(missing_ok=True)


async def ya_stage(j, folder: Path, feed: asyncio.Queue = None, ctx: dict = None, chat_id=None):
    """Зеркалит папку сервера в disk:/<ya_root>/<папка>, публикует её, проверяет, отправляет ссылку и
    (если включено) переводит папку в архив. feed — очередь от скачивания (None в ней — скачивание закончилось)."""
    y = j["ya"] = fresh_ya()
    st = get_settings()
    remote = f"{ya_root_path()}/{folder.name}"
    y["remote"] = remote
    q = feed or asyncio.Queue()
    for f in local_videos(folder):
        q.put_nowait(f)
    if feed is None:
        q.put_nowait(None)
    jlog(j, f"Яндекс Диск: {remote[len('disk:'):]}")
    seen, tasks = set(), set()
    async with ya_client() as h:
        try:
            await ya_retry(j, "создание папки на Диске", lambda: ya_mkdirs(h, remote))
            existing = await ya_retry(j, "список файлов на Диске", lambda: ya_list(h, remote))
            while True:
                y["total"] = len(seen) + sum(1 for x in q._queue if x is not None and x.name not in seen)
                y["waiting"] = feed is not None and q.empty()
                f = await q.get()
                y["waiting"] = False
                if f is None:
                    break
                if f.name in seen or not f.exists():
                    continue
                seen.add(f.name)
                y["total"] = max(y["total"], len(seen))
                if existing.get(f.name) == f.stat().st_size:
                    y["skipped"] += 1
                    continue
                await ya_slots.acquire()
                t = asyncio.create_task(ya_one(j, h, remote, f))
                t.add_done_callback(ya_slots.release)
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
        finally:
            await cancel_all(tasks)
        link = await ya_retry(j, "публикация ссылки", lambda: ya_publish(h, remote))
        y["link"] = link
        on_disk = await ya_retry(j, "проверка файлов на Диске", lambda: ya_list(h, remote))
    vids = {n: sz for n, sz in on_disk.items() if os.path.splitext(n)[1].lower() in VIDEO_EXT}
    jlog(j, f"Яндекс Диск: загружено {y['uploaded']}, уже были {y['skipped']}, ошибок {y['errors']}")
    jlog(j, f"Ссылка: {link}")
    if st.get("info_file", True):
        try:
            await write_info_file(j, folder, remote, link, len(vids), sum(v or 0 for v in vids.values()))
            jlog(j, f"📝 {INFO_FILE} — в папке на сервере и на Диске")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            jlog(j, f"⚠ {INFO_FILE} не создан: {e}")
    dl_ok = ctx is None or ctx.get("ok")
    local = local_videos(folder)
    missing = [f.name for f in local if vids.get(f.name) != f.stat().st_size]
    problem = ("были ошибки загрузки" if y["errors"] else
               f"на Диске нет или отличаются {len(missing)} файл(ов): {', '.join(missing[:5])}" if missing else
               "скачивание из Telegram остановлено или прошло с ошибками" if not dl_ok else None)
    if not st.get("send_link"):
        jlog(j, "Отправка ссылки в чат выключена в настройках")
    elif problem:
        jlog(j, f"Ссылку в чат не отправляю: {problem}. Запустите ещё раз — сделанное пропустится")
    else:
        try:
            y["sent"] = await send_link(folder, chat_id, link, len(vids), sum(v or 0 for v in vids.values()))
            jlog(j, f"Ссылка отправлена в {y['sent']}")
        except Exception as e:
            jlog(j, f"⚠ ссылку отправить не удалось: {e}")
    if st.get("archive") and local:
        if problem:
            jlog(j, f"Видео с сервера не удаляю: {problem}")
        else:
            archive_folder(folder, remote, link, local)
            y["archived"] = True
            jlog(j, f"Проверено: все {len(local)} видео есть на Диске — удалены с сервера, "
                    f"папка осталась в списке (её можно восстановить с Диска)")


async def ya_one(j, h, remote, f):
    y = j["ya"]
    size = f.stat().st_size
    act = y["active"][f.name] = {"done": 0, "total": size, "speed": 0, "phase": "upload"}

    async def attempt():
        # Яндекс режет скорость загрузки через API до ~120 КБ/с для видео (и даже .bin), а файлы
        # с «непонятным» расширением принимает в ~10 раз быстрее: грузим как «имя.mp4_» и переименовываем
        act.update(done=0, speed=0, phase="upload")
        tmp_remote = f"{remote}/{f.name}_"
        r = await h.get(YA_API + "/resources/upload", params={"path": tmp_remote, "overwrite": "true"})
        ya_check(r, (200,), "не удалось получить адрес для загрузки")
        href = r.json()["href"]
        t0 = time.monotonic()

        async def body_iter():
            with open(f, "rb") as src:
                while chunk := await asyncio.to_thread(src.read, 1 << 20):
                    act["done"] += len(chunk)
                    dt = time.monotonic() - t0
                    act["speed"] = act["done"] / dt if dt > 0 else 0
                    yield chunk
            act["phase"] = "finish"  # всё отправлено — Яндекс дочитывает и сохраняет файл

        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=900, write=900)) as up:
            r = await up.put(href, content=body_iter(), headers={"Content-Length": str(size)})
        ya_check(r, (200, 201, 202), "Диск не принял файл")
        await ya_move(h, tmp_remote, f"{remote}/{f.name}")

    try:
        await ya_retry(j, f"загрузка {f.name}", attempt, act)
        y["uploaded"] += 1
        y["bytes"] += size
        jlog(j, f"☁ {f.name}  ({human(size)})")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        y["errors"] += 1
        jlog(j, f"✗ ☁ {f.name}: {e}")
    finally:
        y["active"].pop(f.name, None)
        mark()


# --- восстановление папки из архива (Диск → сервер) ---

async def run_restore(j):
    folder = Path(j["path"])
    a = load_archive(folder)
    if not a:
        raise RuntimeError("Папка не в архиве — восстанавливать нечего")
    remote = a.get("remote") or f"{ya_root_path()}/{folder.name}"
    want = a.get("files") or {}
    y = j["ya"] = fresh_ya()
    y.update(remote=remote, link=a.get("link"))
    jlog(j, f"Восстановление с Яндекс Диска: {remote[len('disk:'):]} → {folder}")
    tasks = set()
    async with ya_client() as h:
        on_disk = await ya_retry(j, "список файлов на Диске", lambda: ya_list(h, remote))
        if not on_disk:
            raise RuntimeError("Папки на Яндекс Диске нет или она пуста — восстановить нельзя")
        lost = [n for n in want if n not in on_disk]
        if lost:
            jlog(j, f"⚠ На Диске нет {len(lost)} файл(ов): {', '.join(lost[:5])}")
        names = [n for n in want if n in on_disk]
        y["total"] = len(names)
        try:
            for n in names:
                dst = folder / n
                if dst.exists() and dst.stat().st_size == on_disk[n]:
                    y["skipped"] += 1
                    continue
                await ya_slots.acquire()
                t = asyncio.create_task(ya_get(j, h, remote, n, on_disk[n], folder))
                t.add_done_callback(ya_slots.release)
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
        finally:
            await cancel_all(tasks)
    left = {n: sz for n, sz in want.items() if not (folder / n).exists()}
    if left:
        a["files"] = left
        save_archive(folder, a)
        jlog(j, f"Восстановлено {y['uploaded']}, уже были {y['skipped']}; на сервер не вернулось {len(left)} — "
                f"папка остаётся в архиве")
    else:
        (folder / ARCHIVE_FILE).unlink(missing_ok=True)
        jlog(j, f"Восстановлено {y['uploaded']}, уже были {y['skipped']} — все видео снова на сервере")


async def ya_get(j, h, remote, name, size, folder):
    y = j["ya"]
    act = y["active"][name] = {"done": 0, "total": size or 0, "speed": 0, "phase": "download"}
    tmp = folder / f".tvp_restore_{secrets.token_hex(4)}.part"

    async def attempt():
        act.update(done=0, speed=0, phase="download")
        r = await h.get(YA_API + "/resources/download", params={"path": f"{remote}/{name}"})
        ya_check(r, (200,), "не удалось получить адрес для скачивания")
        href = r.json()["href"]
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=900), follow_redirects=True) as cl:
            async with cl.stream("GET", href) as resp:
                ya_check(resp, (200,), "Диск не отдал файл")
                with open(tmp, "wb") as out:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        await asyncio.to_thread(out.write, chunk)
                        act["done"] += len(chunk)
                        dt = time.monotonic() - t0
                        act["speed"] = act["done"] / dt if dt > 0 else 0
        if size and tmp.stat().st_size != size:
            raise RuntimeError(f"размер не совпал ({tmp.stat().st_size} вместо {size})")
        tmp.replace(folder / name)

    try:
        await ya_retry(j, f"скачивание {name} с Диска", attempt, act)
        y["uploaded"] += 1
        y["bytes"] += size or 0
        jlog(j, f"⤓ {name}  ({human(size)})")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        y["errors"] += 1
        jlog(j, f"✗ ⤓ {name}: {e}")
    finally:
        y["active"].pop(name, None)
        tmp.unlink(missing_ok=True)
        mark()



# ---------- автоочистка сервера ----------
#
# Удаляет с сервера видео, которые точно есть на Яндекс Диске (имя + размер), через механизм архива:
# папка остаётся в списке «в архиве», видео восстанавливаются с Диска. Правила (в настройках):
# старше N дней и/или самые старые, пока свободного места меньше X ГБ. Папки с заданиями не трогаем.

# ---------- проверка обновлений ----------
#
# Раз в 6 часов анонимно спрашиваем GitHub о релизах; если есть версия новее VERSION — панель показывает
# «Доступна X.Y.Z», что нового и команду обновления. Сама панель себя не обновляет (у неё нет прав root).

UPDATE_REPO = os.environ.get("TVP_UPDATE_REPO", "AlexKanivets/tg-video-panel")
update = {"checked": None, "latest": None, "newer": False, "releases": [], "error": None}


def ver_tuple(v: str):
    nums = re.findall(r"\d+", str(v or ""))[:3]
    return tuple(int(x) for x in nums) + (0,) * (3 - len(nums))


async def update_check() -> dict:
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": f"tg-video-panel/{VERSION}"}) as h:
            r = await h.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases", params={"per_page": 30})
        if r.status_code != 200:
            raise RuntimeError(f"GitHub ответил {r.status_code}")
        rel = [x for x in r.json() if not x.get("draft") and not x.get("prerelease")]
        rel.sort(key=lambda x: ver_tuple(x.get("tag_name")), reverse=True)
        newer = [x for x in rel if ver_tuple(x.get("tag_name")) > ver_tuple(VERSION)]
        update.update(checked=time.time(), error=None,
                      latest=rel[0]["tag_name"].lstrip("v") if rel else None, newer=bool(newer),
                      releases=[{"version": x["tag_name"].lstrip("v"), "name": x.get("name") or x["tag_name"],
                                 "date": (x.get("published_at") or "")[:10], "notes": x.get("body") or "",
                                 "url": x.get("html_url")} for x in newer])
    except Exception as e:
        update.update(checked=time.time(), error=str(e) or type(e).__name__)
    return update


async def update_loop():
    await asyncio.sleep(20)
    while True:
        if get_settings().get("update_check", True):
            await update_check()
        await asyncio.sleep(6 * 3600)


def update_public() -> dict:
    mode = "docker" if os.environ.get("TVP_DOCKER") == "1" else "server" if SERVER else "local"
    return dict(update, current=VERSION, mode=mode, repo=UPDATE_REPO,
                enabled=bool(get_settings().get("update_check", True)))



GIB = 1 << 30
cleanup = {"running": False, "last": None, "reason": "", "deleted": 0, "freed": 0, "log": []}
cleanup_lock = asyncio.Lock()


def clog(msg):
    cleanup["log"].append(f"{datetime.now():%d.%m %H:%M}  {msg}")
    del cleanup["log"][:-100]


def disk_free() -> int:
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(DOWNLOAD_ROOT).free


async def cleanup_run(reason="по расписанию") -> dict:
    st = get_settings()
    if not (st.get("clean_age") or st.get("clean_space")):
        return {"skipped": "автоочистка выключена"}
    if not ya_conf().get("token"):
        return {"skipped": "Яндекс Диск не подключён — без него очистка ничего не удаляет"}
    if cleanup_lock.locked():
        return {"skipped": "очистка уже идёт"}
    async with cleanup_lock:
        cleanup.update(running=True, reason=reason)
        deleted = freed = 0
        try:
            now = time.time()
            files = []  # (mtime, size, folder, path) — кандидаты из свободных папок
            for d in sorted(DOWNLOAD_ROOT.iterdir()) if DOWNLOAD_ROOT.is_dir() else []:
                if d.is_dir() and not folder_jobs(d.name):
                    for f in local_videos(d):
                        stt = f.stat()
                        files.append((stt.st_mtime, stt.st_size, d, f))
            files.sort(key=lambda x: x[0])
            pick = set()
            if st.get("clean_age"):
                limit = now - max(1, int(st["clean_age_days"])) * 86400
                pick.update(f for m, _, _, f in files if m < limit)
            if st.get("clean_space"):
                need = max(1, int(st["clean_space_gb"])) * GIB - disk_free()
                for m, size, _, f in files:
                    if need <= 0:
                        break
                    if f not in pick:
                        pick.add(f)
                    need -= size
            if not pick:
                return {"deleted": 0, "freed": 0}
            by_folder = {}
            for _, size, d, f in files:
                if f in pick:
                    by_folder.setdefault(d, []).append(f)
            root = ya_root_path()
            async with ya_client() as h:
                for d, flist in by_folder.items():
                    if folder_jobs(d.name):
                        continue  # пока собирали список, с папкой начало работать задание
                    a = load_archive(d)
                    remote = a.get("remote") or f"{root}/{d.name}"
                    try:
                        on_disk = await ya_list(h, remote)
                        r = await h.get(YA_API + "/resources", params={"path": remote, "fields": "public_url"})
                        link = r.json().get("public_url") if r.status_code == 200 else a.get("link")
                    except Exception as e:
                        clog(f"⚠ «{d.name}»: не удалось проверить Диск ({e}) — пропускаю")
                        continue
                    ok_files = [f for f in flist if f.exists() and on_disk.get(f.name) == f.stat().st_size]
                    missing = len(flist) - len(ok_files)
                    if missing:
                        clog(f"«{d.name}»: {missing} видео нет на Диске (или отличаются) — не удаляю их")
                    if not ok_files:
                        continue
                    size = sum(f.stat().st_size for f in ok_files)
                    archive_folder(d, remote, link, ok_files)
                    deleted += len(ok_files)
                    freed += size
                    clog(f"🧹 «{d.name}»: удалено с сервера {len(ok_files)} видео, освобождено {human(size)} "
                         f"(есть на Диске, можно восстановить)")
            return {"deleted": deleted, "freed": freed}
        finally:
            cleanup.update(running=False, last=time.time(), deleted=deleted, freed=freed)
            if deleted:
                clog(f"Итого ({reason}): {deleted} видео, {human(freed)}; свободно {human(disk_free())}")


async def cleanup_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await cleanup_run()
        except Exception as e:
            clog(f"⚠ ошибка очистки: {e}")
        await asyncio.sleep(1800)


def space_info() -> dict:
    st = get_settings()
    free = disk_free()
    warn = max(0, float(st.get("warn_space_gb") or 0)) * GIB
    return {"free": free, "warn": bool(warn) and free < warn, "warn_gb": st.get("warn_space_gb")}


# ---------- HTTP-обработчики ----------

async def body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def ok(data=None):
    return JSONResponse(data if data is not None else {"ok": True})


async def page(request: Request):
    if not is_authed(request):
        return HTMLResponse(LOGIN_PAGE)
    return HTMLResponse(PAGE)


async def login(request: Request):
    form = await request.form()
    pwd = str(form.get("password", ""))
    if auth_enabled() and hmac.compare_digest(pwd.encode(), PASSWORD.encode()):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, auth_token(), max_age=60 * 60 * 24 * 30, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https"
                        or request.headers.get("x-forwarded-proto") == "https")
        return resp
    await asyncio.sleep(1.5)  # притормаживаем перебор пароля
    return HTMLResponse(LOGIN_PAGE.replace("<!--ERR-->", "Неверный пароль"), status_code=401)


async def panel_logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


async def status(request: Request):
    cfg = load_config()
    out = {"version": VERSION, "configured": bool(cfg.get("api_id")), "authorized": False, "me": None,
           "ffmpeg": bool(shutil.which("ffmpeg")), "server": SERVER,
           "panel_auth": auth_enabled(),
           "default_folder": "" if SERVER else str(DOWNLOAD_ROOT),
           "root": str(DOWNLOAD_ROOT), "error": None}
    try:
        c = await get_client()
        if c and await c.is_user_authorized():
            me = await c.get_me()
            out["authorized"] = True
            out["me"] = " ".join(x for x in (me.first_name, me.last_name) if x) or me.username
    except Exception as e:
        out["error"] = f"Нет связи с Telegram: {e}"
    return ok(out)


async def set_config(request: Request):
    b = await body(request)
    try:
        api_id = int(b.get("api_id"))
    except Exception:
        raise HTTPException(400, "api_id должен быть числом")
    api_hash = str(b.get("api_hash", "")).strip()
    if not api_hash:
        raise HTTPException(400, "Укажите api_hash")
    cfg = load_config()
    cfg.update(api_id=api_id, api_hash=api_hash)
    save_config(cfg)
    await drop_client()
    await get_client()
    return ok()


async def send_code(request: Request):
    phone = str((await body(request)).get("phone", "")).strip()
    c = await need_client()
    try:
        r = await c.send_code_request(phone)
    except Exception as e:
        raise HTTPException(400, str(e))
    state["phone"], state["code_hash"] = phone, r.phone_code_hash
    return ok()


async def sign_in(request: Request):
    b = await body(request)
    c = await need_client()
    try:
        if b.get("password"):
            await c.sign_in(password=b["password"])
        else:
            await c.sign_in(phone=state["phone"], code=str(b.get("code", "")).strip(),
                            phone_code_hash=state["code_hash"])
    except errors.SessionPasswordNeededError:
        return ok({"need_password": True})
    except Exception as e:
        raise HTTPException(400, str(e))
    return ok()


async def tg_logout(request: Request):
    if any(j["kind"] == "download" and j["status"] in ("queued", "running") for j in jobs.values()):
        raise HTTPException(409, "Сначала остановите задания на скачивание")
    c = await need_client()
    try:
        await c.log_out()
    finally:
        await drop_client()
    return ok()


async def dialogs(request: Request):
    c = await need_auth()
    out = []
    async for d in c.iter_dialogs():
        if d.is_group or d.is_channel:
            entities[d.id] = d.entity
            out.append({"id": d.id, "name": d.name or "(без названия)",
                        "type": "группа" if d.is_group else "канал"})
    return ok(out)


async def get_entity(c, chat_id: int):
    try:
        return entities.get(chat_id) or await c.get_entity(chat_id)
    except Exception as e:
        raise HTTPException(400, f"Чат не найден: {e}")


def chat_id_of(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверный id чата")


async def count(request: Request):
    c = await need_auth()
    entity = await get_entity(c, chat_id_of(request.path_params["chat_id"]))
    q = request.query_params
    try:
        total = await count_videos(c, entity, parse_date(q.get("from")),
                                   parse_date(q.get("to")) or datetime.now(timezone.utc))
    except Exception as e:
        raise HTTPException(400, str(e))
    return ok({"total": total})


async def start(request: Request):
    """Создаёт задание на скачивание; оно встаёт в очередь и запускается планировщиком."""
    b = await body(request)
    quality = str(b.get("quality", "original"))
    if quality not in ("original", "1080", "720", "480"):
        raise HTTPException(400, "Неизвестное качество")
    ext = str(b.get("ext", "original"))
    if ext != "original" and ext not in EXT_CHOICES:
        raise HTTPException(400, "Неизвестное расширение")
    if quality != "original":
        ext = ".mp4"  # после сжатия ffmpeg всегда получается mp4
    date_from, date_to = parse_date(b.get("date_from")), parse_date(b.get("date_to"))
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(400, "Дата «с» должна быть раньше даты «по»")
    try:
        tz_min = -max(-840, min(840, int(b.get("tz_offset", 0))))
    except (TypeError, ValueError):
        tz_min = 0
    tz = timezone(timedelta(minutes=tz_min))
    chat_id = chat_id_of(b.get("chat_id"))
    c = await need_auth()
    entity = await get_entity(c, chat_id)
    name = getattr(entity, "title", None) or str(chat_id)
    folder = resolve_folder(str(b.get("folder", "")), name, tz)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"Не удалось создать папку: {e}")
    j = new_job("download", folder, chat_id=chat_id, chat_name=name, quality=quality, ext=ext,
                oldest_first=bool(b.get("oldest_first", True)), tz=tz_min,
                date_from=date_from.isoformat() if date_from else None,
                date_to=(date_to or datetime.now(timezone.utc)).isoformat(), ya=ya_auto_enabled())
    return ok({"id": j["id"], "folder": j["folder"]})


def job_public(j, with_log=False):
    out = {k: v for k, v in j.items() if k != "log"}
    out["log_len"] = len(j["log"])
    if with_log:
        out["log"] = j["log"]
    return out


async def jobs_list(request: Request):
    want = set((request.query_params.get("logs") or "").split(","))
    order = {"running": 0, "queued": 1}
    lst = sorted(jobs.values(), key=lambda j: (order.get(j["status"], 2),
                                               j["created"] if j["status"] == "queued" else -(j.get("finished") or j["created"])))
    st = get_settings()
    return ok({"jobs": [job_public(j, j["id"] in want) for j in lst], "space": space_info(),
               "version": VERSION,
               "limits": {"dl_jobs": st["dl_jobs"], "tg_parallel": st["tg_parallel"], "ya_parallel": st["ya_parallel"]}})


async def legacy_poll(request: Request):
    # вкладки, открытые до версии 1.2 (до «Заданий»), опрашивают /api/job и /api/ya/job; на 403 их код
    # перезагружает страницу — так они сами переходят на новую версию
    return JSONResponse({"detail": "Панель обновилась — обновите страницу"}, status_code=403)


# ---------- резервная копия (зашифрованная) ----------
#
# Файл .tvpbak: b"TVPBAK1" + соль(16) + nonce(12) + AES-256-GCM(zip). Ключ — scrypt(пароль, соль, N=2^15, r=8, p=1).
# Внутри zip: config.json (ключи Telegram API, Яндекс Диск, настройки), tg_session.session (вход в Telegram),
# .secret и manifest.json. Видео и пароль панели не входят.

BACKUP_MAGIC = b"TVPBAK1"


def _backup_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def _session_bytes() -> bytes:
    """Согласованная копия sqlite-сессии Telethon (даже пока клиент подключён)."""
    src = Path(SESSION + ".session")
    if not src.exists():
        return b""
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as td:
        dst = Path(td) / "s.session"
        with sqlite3.connect(str(src)) as a, sqlite3.connect(str(dst)) as b:
            a.backup(b)
        return dst.read_bytes()


def make_backup(password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"app": "tg-video-panel", "version": VERSION,
                                                "created": datetime.now(timezone.utc).isoformat()}))
        if CONFIG_FILE.exists():
            z.writestr("config.json", CONFIG_FILE.read_bytes())
        sess = _session_bytes()
        if sess:
            z.writestr("tg_session.session", sess)
        if SECRET_FILE.exists():
            z.writestr(".secret", SECRET_FILE.read_bytes())
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return BACKUP_MAGIC + salt + nonce + AESGCM(_backup_key(password, salt)).encrypt(nonce, buf.getvalue(), BACKUP_MAGIC)


def open_backup(data: bytes, password: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not data.startswith(BACKUP_MAGIC) or len(data) < len(BACKUP_MAGIC) + 28 + 16:
        raise HTTPException(400, "Это не файл резервной копии панели")
    salt = data[7:23]
    nonce = data[23:35]
    try:
        raw = AESGCM(_backup_key(password, salt)).decrypt(nonce, data[35:], BACKUP_MAGIC)
    except InvalidTag:
        raise HTTPException(400, "Неверный пароль резервной копии (или файл повреждён)")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        out = {n: z.read(n) for n in z.namelist() if n in ("manifest.json", "config.json", "tg_session.session", ".secret")}
    if "manifest.json" not in out:
        raise HTTPException(400, "В файле нет данных панели")
    return out


# ---------- страница просмотра по секретной ссылке ----------
#
# /s/<token> открывается без пароля панели и показывает ТОЛЬКО одну папку: плеер, список видео, скачивание
# по одному и ZIP. token — 192 бита случайных данных. Ссылки хранятся в data/shares.json {token: {folder,
# created, expires, views}}; после срока ссылка не работает, любую можно отозвать.

SHARES_FILE = DATA_DIR / "shares.json"
SHARE_DAYS = (0, 1, 7, 30)  # 0 — бессрочно


def load_shares() -> dict:
    try:
        d = json.loads(SHARES_FILE.read_text("utf-8"))
    except Exception:
        return {}
    now = time.time()
    return {t: x for t, x in d.items() if not x.get("expires") or x["expires"] > now}


def save_shares(d: dict):
    tmp = SHARES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(SHARES_FILE)


def share_url(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + "/s/" + token


async def shares_list(request: Request):
    folder = request.query_params.get("folder")
    out = [dict(x, token=t, url=share_url(request, t)) for t, x in load_shares().items()
           if not folder or x["folder"] == folder]
    out.sort(key=lambda x: x["created"], reverse=True)
    return ok({"shares": out})


async def shares_create(request: Request):
    b = await body(request)
    p = server_folder(b.get("folder"))
    try:
        days = int(b.get("days", 7))
    except (TypeError, ValueError):
        days = -1
    if days not in SHARE_DAYS:
        raise HTTPException(400, "Срок — 1, 7, 30 дней или бессрочно")
    d = load_shares()
    token = secrets.token_urlsafe(24)
    d[token] = {"folder": p.name, "created": time.time(), "expires": time.time() + days * 86400 if days else None,
                "views": 0}
    save_shares(d)
    return ok(dict(d[token], token=token, url=share_url(request, token)))


async def shares_revoke(request: Request):
    token = str((await body(request)).get("token") or "")
    d = load_shares()
    if d.pop(token, None) is None:
        raise HTTPException(404, "Ссылка не найдена")
    save_shares(d)
    return ok()


def shares_rename(old: str, new: str):
    d = load_shares()
    changed = False
    for x in d.values():
        if x["folder"] == old:
            x["folder"] = new
            changed = True
    if changed:
        save_shares(d)


def shares_drop_folder(name: str):
    d = load_shares()
    left = {t: x for t, x in d.items() if x["folder"] != name}
    if len(left) != len(d):
        save_shares(left)


SHARE_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"}


def share_folder(token: str):
    x = load_shares().get(token)
    if not x:
        return None, None
    p = DOWNLOAD_ROOT / x["folder"]
    return (x, p) if p.is_dir() else (None, None)


def share_gone():
    return HTMLResponse(SHARE_PAGE.replace("<!--T-->", "Ссылка недействительна").replace(
        "<!--B-->", '<section class="card"><h2>Ссылка недействительна</h2><p class="hint">Срок действия истёк, '
                    'ссылку отозвали или папку удалили. Попросите прислать новую ссылку.</p></section>'),
        status_code=404, headers=SHARE_HEADERS)


async def share_page(request: Request):
    token = request.path_params["token"]
    x, p = share_folder(token)
    if not x:
        return share_gone()
    d = load_shares()
    d[token]["views"] = d[token].get("views", 0) + 1
    save_shares(d)
    e = htmllib.escape
    vids = sorted(local_videos(p), key=lambda f: f.name.lower())
    total = sum(f.stat().st_size for f in vids)
    until = f"до {datetime.fromtimestamp(x['expires']):%d.%m.%Y %H:%M}" if x.get("expires") else "бессрочно"
    base = f"/s/{token}"
    meta = [f"{len(vids)} видео · {human(total)}"]
    per = period_text(p)
    if per != "не указан":
        meta.append(f"период {per}")
    meta.append(f"ссылка действует {until}")
    if vids:
        first = f"{base}/f/{quote(vids[0].name)}"
        items = "".join(
            f'<div class="pitem{" on" if i == 0 else ""}" data-src="{base}/f/{quote(f.name)}" data-name="{e(f.name)}">'
            f'<span class="n">▶ {e(f.name)}</span><span class="muted">{human(f.stat().st_size)}</span>'
            f'<a class="btn sm" href="{base}/f/{quote(f.name)}?dl=1" title="Скачать" aria-label="Скачать {e(f.name)}">⤓</a></div>'
            for i, f in enumerate(vids))
        content = (f'<section class="card"><video id="pl" controls playsinline preload="metadata" src="{first}"></video>'
                   f'<div class="now" id="now">{e(vids[0].name)}</div>'
                   f'<a class="btn" href="{base}/zip">Скачать всё одним архивом (ZIP, {human(total)})</a></section>'
                   f'<section class="card"><div class="plist" id="plist">{items}</div></section>')
    else:
        a = load_archive(p)
        link = a.get("link") if a else None
        content = ('<section class="card"><h2>Видео перенесены на Яндекс Диск</h2>'
                   + (f'<p><a class="btn primary" href="{e(link)}" rel="noopener">Открыть на Яндекс Диске</a></p>' if link
                      else '<p class="hint">Сейчас видео на сервере нет.</p>') + '</section>')
    page = SHARE_PAGE.replace("<!--T-->", e(p.name)).replace("<!--B-->",
        f'<section class="card"><h2>📁 {e(p.name)}</h2><p class="hint" style="margin:0">{" · ".join(e(m) for m in meta)}</p></section>{content}')
    return HTMLResponse(page, headers=SHARE_HEADERS)


async def share_file(request: Request):
    x, p = share_folder(request.path_params["token"])
    name = request.path_params["name"]
    if not x:
        raise HTTPException(404, "Ссылка недействительна")
    f = next((v for v in local_videos(p) if v.name == name), None)  # только видео этой папки
    if f is None:
        raise HTTPException(404, "Файл не найден")
    dl = request.query_params.get("dl") == "1"
    return FileResponse(f, media_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                        filename=f.name if dl else None, content_disposition_type="attachment" if dl else "inline",
                        headers=SHARE_HEADERS)


async def share_zip(request: Request):
    x, p = share_folder(request.path_params["token"])
    if not x:
        raise HTTPException(404, "Ссылка недействительна")
    files = sorted(local_videos(p), key=lambda f: f.name.lower())
    if not files:
        raise HTTPException(404, "В папке нет видео")
    name = p.name + ".zip"
    fallback = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "videos.zip"
    return StreamingResponse(zip_stream(files), media_type="application/zip", headers=dict(SHARE_HEADERS, **{
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"}))


async def backup_download(request: Request):
    pwd = str((await body(request)).get("password") or "")
    if len(pwd) < 8:
        raise HTTPException(400, "Пароль копии — не короче 8 символов")
    data = await asyncio.to_thread(make_backup, pwd)
    name = f"tg-video-panel-backup-{datetime.now():%Y-%m-%d}.tvpbak"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def backup_restore(request: Request):
    if any(j["status"] in ("queued", "running") for j in jobs.values()):
        raise HTTPException(409, "Сначала дождитесь окончания заданий или остановите их")
    form = await request.form()
    up, pwd = form.get("file"), str(form.get("password") or "")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "Выберите файл резервной копии")
    data = await up.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "Файл слишком большой для резервной копии панели")
    parts = await asyncio.to_thread(open_backup, data, pwd)
    if "config.json" in parts:
        try:
            json.loads(parts["config.json"])
        except Exception:
            raise HTTPException(400, "Повреждённые настройки в резервной копии")
    await drop_client()  # отпускаем текущую сессию Telegram перед заменой
    if "config.json" in parts:
        CONFIG_FILE.write_bytes(parts["config.json"])
        CONFIG_FILE.chmod(0o600)
    if "tg_session.session" in parts:
        Path(SESSION + ".session-journal").unlink(missing_ok=True)
        Path(SESSION + ".session").write_bytes(parts["tg_session.session"])
        Path(SESSION + ".session").chmod(0o600)
    if ".secret" in parts:
        SECRET_FILE.write_bytes(parts[".secret"])
        SECRET_FILE.chmod(0o600)
    m = json.loads(parts["manifest.json"])
    st = {"authorized": False, "me": None}
    try:
        c = await get_client()
        if c and await c.is_user_authorized():
            me = await c.get_me()
            st.update(authorized=True, me=" ".join(x for x in (me.first_name, me.last_name) if x) or me.username)
    except Exception as e:
        st["error"] = f"Telegram: {e}"
    resp = ok(dict(st, version=m.get("version"), created=m.get("created"),
                   yandex=bool((json.loads(parts.get("config.json") or b"{}").get("yandex") or {}).get("token"))))
    if ".secret" in parts:  # подпись входа сменилась — сразу выдаём новую cookie, чтобы не выкинуло из панели
        resp.set_cookie(COOKIE, auth_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


async def update_get(request: Request):
    return ok(update_public())


async def update_now(request: Request):
    await update_check()
    return ok(update_public())


async def cleanup_get(request: Request):
    return ok(dict(cleanup, space=space_info()))


async def cleanup_now(request: Request):
    res = await cleanup_run("вручную")
    return ok(dict(res, space=space_info(), log=cleanup["log"][-20:]))


async def jobs_stop(request: Request):
    j = jobs.get(str((await body(request)).get("id")))
    if not j:
        raise HTTPException(404, "Задание не найдено")
    if j["status"] == "queued":
        j.update(status="stopped", finished=time.time())
        jlog(j, "Убрано из очереди")
        wake_scheduler()
    elif j["status"] == "running" and job_tasks.get(j["id"]):
        job_tasks[j["id"]].cancel()
    return ok()


async def jobs_retry(request: Request):
    j = jobs.get(str((await body(request)).get("id")))
    if not j:
        raise HTTPException(404, "Задание не найдено")
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, "Задание ещё выполняется")
    n = new_job(j["kind"], Path(j["path"]), **j["params"])
    return ok({"id": n["id"]})


async def jobs_clear(request: Request):
    for jid in [k for k, j in jobs.items() if j["status"] not in ("queued", "running")]:
        jobs.pop(jid, None)
    wake_scheduler()
    return ok()


async def open_folder(request: Request):
    if SERVER:
        raise HTTPException(400, "На сервере недоступно")
    p = resolve_folder(str((await body(request)).get("folder", "")), "")
    p.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        os.startfile(p)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return ok()


async def files(request: Request):
    root = DOWNLOAD_ROOT
    root.mkdir(parents=True, exist_ok=True)
    groups = []
    loose = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            items = [{"name": f.name, "path": f"{entry.name}/{f.name}", "size": f.stat().st_size,
                      "mtime": f.stat().st_mtime}
                     for f in sorted(entry.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
                     if f.is_file() and f.suffix.lower() in VIDEO_EXT]
            a = load_archive(entry)
            arch = None
            if a:
                af = a.get("files") or {}
                arch = {"count": len(af), "size": sum(v or 0 for v in af.values()), "link": a.get("link"),
                        "at": a.get("at")}
            groups.append({"name": entry.name, "files": items, "size": sum(i["size"] for i in items),
                           "archived": arch, "busy": bool(folder_jobs(entry.name)),
                           "source_chat": folder_source_chat(entry)})
        elif entry.is_file() and entry.suffix.lower() in VIDEO_EXT:
            loose.append({"name": entry.name, "path": entry.name, "size": entry.stat().st_size,
                          "mtime": entry.stat().st_mtime})
    if loose:
        groups.insert(0, {"name": "(корень)", "files": loose, "size": sum(i["size"] for i in loose)})
    du = shutil.disk_usage(root)
    return ok({"root": str(root), "groups": groups,
               "disk": {"total": du.total, "used": du.used, "free": du.free}})


class _ZipSink:
    """Приёмник для zipfile без seek/tell — zipfile тогда пишет потоково (data descriptor)."""
    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def zip_stream(files):
    # без сжатия: видео и так сжато, а архив отдаётся сразу, без временного файла на диске
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for f in files:
            info = zipfile.ZipInfo(f.name, date_time=time.localtime(f.stat().st_mtime)[:6])
            with open(f, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    if len(sink.buf) >= 1 << 20:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


async def folder_zip(request: Request):
    p = inside_root(request.path_params["path"])
    if not p.is_dir() or p == DOWNLOAD_ROOT.resolve():
        raise HTTPException(404, "Папка не найдена")
    files = sorted((f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT),
                   key=lambda f: f.name.lower())
    if not files:
        raise HTTPException(404, "В папке нет видео")
    name = p.name + ".zip"
    fallback = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "videos.zip"
    return StreamingResponse(zip_stream(files), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"})


async def file_download(request: Request):
    p = inside_root(request.path_params["path"])
    if not p.is_file():
        raise HTTPException(404, "Файл не найден")
    inline = request.query_params.get("inline") == "1"
    return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                        filename=None if inline else p.name,
                        content_disposition_type="inline" if inline else "attachment")


async def file_delete(request: Request):
    rel = str((await body(request)).get("path", ""))
    if not rel:
        raise HTTPException(400, "Не указан файл")
    p = inside_root(rel)
    if p.is_file():
        if p.parent != DOWNLOAD_ROOT.resolve() and folder_jobs(p.parent.name, ("running",)):
            raise HTTPException(409, "С этой папкой сейчас работает задание — дождитесь окончания")
        p.unlink()
    elif p.is_dir() and p != DOWNLOAD_ROOT.resolve():
        if folder_jobs(p.name):
            raise HTTPException(409, "С этой папкой есть задание в очереди или в работе — остановите его")
        shutil.rmtree(p)
        shares_drop_folder(p.name)
    else:
        raise HTTPException(404, "Не найдено")
    return ok()


async def settings_get(request: Request):
    y = ya_conf()
    dev = state.get("ya_device")
    out = {"settings": get_settings(), "ya": {"connected": bool(y.get("token")), "client_id": y.get("client_id", ""),
                                              "has_secret": bool(y.get("client_secret")),
                                              "device": device_public(dev) if dev and dev["status"] == "pending" else None}}
    if y.get("token"):
        try:
            out["ya"].update(await ya_info())
        except HTTPException as e:
            out["ya"]["error"] = e.detail
        except Exception as e:
            out["ya"]["error"] = f"Нет связи с Яндекс Диском: {e}"
    return ok(out)


async def settings_set(request: Request):
    b = await body(request)
    st = get_settings()
    if "ya_root" in b:
        root = "/".join(safe_name(x) for x in str(b["ya_root"]).split("/") if x.strip())
        st["ya_root"] = root or DEFAULT_SETTINGS["ya_root"]
    if "send_link" in b:
        st["send_link"] = bool(b["send_link"])
    for k, hi in (("dl_jobs", 5), ("tg_parallel", 8), ("ya_parallel", 8)):
        if k in b:
            try:
                st[k] = max(1, min(hi, int(b[k])))
            except (TypeError, ValueError):
                raise HTTPException(400, "Параллельность — целое число")
    if "archive" in b:
        st["archive"] = bool(b["archive"])
    if "update_check" in b:
        st["update_check"] = bool(b["update_check"])
    for k in ("clean_age", "clean_space"):
        if k in b:
            st[k] = bool(b[k])
    for k, lo, hi in (("clean_age_days", 1, 3650), ("clean_space_gb", 1, 100000), ("warn_space_gb", 0, 100000)):
        if k in b:
            try:
                st[k] = max(lo, min(hi, int(b[k])))
            except (TypeError, ValueError):
                raise HTTPException(400, "Параметры очистки — целые числа")
    if "info_file" in b:
        st["info_file"] = bool(b["info_file"])
    if "auto_upload" in b:
        if b["auto_upload"] and not ya_conf().get("token"):
            raise HTTPException(400, "Автозагрузку можно включить только после подключения Яндекс Диска")
        st["auto_upload"] = bool(b["auto_upload"])
    if "target" in b:
        if b["target"] not in ("source", "saved", "chat"):
            raise HTTPException(400, "Неизвестный получатель")
        st["target"] = b["target"]
    if "target_chat" in b:
        st["target_chat"] = chat_id_of(b["target_chat"]) if b["target_chat"] not in (None, "") else None
        st["target_name"] = str(b.get("target_name", ""))[:200]
    if st["target"] == "chat" and not st.get("target_chat"):
        raise HTTPException(400, "Выберите чат для отправки ссылки")
    if "template" in b:
        tpl = str(b["template"]).strip()
        if tpl and "{link}" not in tpl:
            raise HTTPException(400, "В тексте сообщения должен быть {link}")
        st["template"] = tpl or DEFAULT_SETTINGS["template"]
    cfg = load_config()
    cfg["settings"] = st
    save_config(cfg)
    tg_slots.poke()
    ya_slots.poke()
    wake_scheduler()
    return ok({"settings": st})


async def ya_device_worker(dev: dict):
    """Ждёт подтверждения кода на ya.ru/device на стороне сервера — не зависит от того,
    открыта ли вкладка с панелью (Safari на iPhone приостанавливает фоновые вкладки)."""
    try:
        while time.time() < dev["until"] and state.get("ya_device") is dev:
            await asyncio.sleep(dev["interval"])
            y = ya_conf()
            try:
                async with httpx.AsyncClient(timeout=30) as h:
                    r = await h.post(YA_OAUTH + "/token", data={
                        "grant_type": "device_code", "code": dev["code"],
                        "client_id": y.get("client_id"), "client_secret": y.get("client_secret")})
                d = r.json()
            except Exception:
                continue  # сеть моргнула — пробуем ещё раз
            if r.status_code == 200 and d.get("access_token"):
                set_ya_conf(token=d["access_token"], refresh_token=d.get("refresh_token"))
                dev["status"] = "connected"
                return
            if d.get("error") == "slow_down":
                dev["interval"] += 5
            elif d.get("error") != "authorization_pending":
                dev.update(status="error", error=f"Яндекс: {ya_error(r)}")
                return
        if dev["status"] == "pending":
            dev["status"] = "expired"
    except asyncio.CancelledError:
        pass


def device_public(dev):
    if not dev:
        return None
    return {"status": dev["status"], "error": dev.get("error"), "user_code": dev["user_code"],
            "url": dev["url"], "expires_in": max(0, int(dev["until"] - time.time()))}


async def ya_device_start(request: Request):
    b = await body(request)
    cid = str(b.get("client_id") or ya_conf().get("client_id") or "").strip()
    secret = str(b.get("client_secret") or ya_conf().get("client_secret") or "").strip()
    if not cid or not secret:
        raise HTTPException(400, "Укажите ClientID и Client secret приложения Яндекса")
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.post(YA_OAUTH + "/device/code", data={
            "client_id": cid, "device_id": hashlib.sha256(_secret()).hexdigest()[:32],
            "device_name": "Telegram Video Panel"})
    if r.status_code != 200:
        raise HTTPException(400, f"Яндекс: {ya_error(r)}")
    d = r.json()
    set_ya_conf(client_id=cid, client_secret=secret)
    if state.get("ya_device_task"):
        state["ya_device_task"].cancel()
    dev = {"code": d["device_code"], "user_code": d["user_code"],
           "url": d.get("verification_url", "https://ya.ru/device"), "status": "pending",
           "interval": max(3, int(d.get("interval", 5))), "until": time.time() + int(d.get("expires_in", 300))}
    state["ya_device"] = dev
    state["ya_device_task"] = asyncio.create_task(ya_device_worker(dev))
    return ok(device_public(dev))


async def ya_device_poll(request: Request):
    """Статус подтверждения: pending / connected / expired / error (сам Яндекс опрашивает сервер)."""
    dev = state.get("ya_device")
    if not dev:
        return ok({"status": "connected" if ya_conf().get("token") else "none"})
    out = device_public(dev)
    if dev["status"] in ("connected", "expired", "error"):
        state["ya_device"] = None
    return ok(out)


async def ya_set_token(request: Request):
    token = str((await body(request)).get("token", "")).strip()
    if not token:
        raise HTTPException(400, "Вставьте токен")
    info = await ya_info(token)
    set_ya_conf(token=token)
    return ok(info)


async def ya_logout(request: Request):
    if any(j["status"] in ("queued", "running") and (j["kind"] != "download" or j["params"].get("ya"))
           for j in jobs.values()):
        raise HTTPException(409, "Сначала остановите задания, которые работают с Диском")
    set_ya_conf(token=None, refresh_token=None)
    return ok()


def server_folder(name) -> Path:
    p = inside_root(str(name or ""))
    if not name or not p.is_dir() or p == DOWNLOAD_ROOT.resolve():
        raise HTTPException(404, "Папка не найдена")
    return p


async def ya_start(request: Request):
    if not ya_conf().get("token"):
        raise HTTPException(400, "Сначала подключите Яндекс Диск в настройках")
    p = server_folder((await body(request)).get("folder"))
    if any(j["kind"] == "upload" for j in folder_jobs(p.name)):
        raise HTTPException(409, "Эта папка уже в очереди на загрузку")
    if not local_videos(p):
        raise HTTPException(400, "В папке на сервере нет видео")
    j = new_job("upload", p)
    return ok({"id": j["id"]})


async def restore_start(request: Request):
    if not ya_conf().get("token"):
        raise HTTPException(400, "Сначала подключите Яндекс Диск в настройках")
    p = server_folder((await body(request)).get("folder"))
    if not load_archive(p):
        raise HTTPException(400, "Эта папка не в архиве")
    if any(j["kind"] == "restore" for j in folder_jobs(p.name)):
        raise HTTPException(409, "Восстановление этой папки уже в очереди")
    j = new_job("restore", p)
    return ok({"id": j["id"]})


def ya_root_path() -> str:
    root = (get_settings().get("ya_root") or "").strip("/ ") or DEFAULT_SETTINGS["ya_root"]
    return f"disk:/{root}"


def ya_folder_name(v) -> str:
    name = str(v or "").strip()
    if not name or name != safe_name(name) or name in (".", ".."):
        raise HTTPException(400, "Неверное имя папки")
    return name


async def ya_folders(request: Request):
    root = ya_root_path()
    async with ya_client() as h:
        r = await h.get(YA_API + "/resources", params={
            "path": root, "limit": 1000,
            "fields": "_embedded.items.name,_embedded.items.type,_embedded.items.public_url,_embedded.items.modified"})
        if r.status_code == 404:
            return ok({"root": root[len("disk:"):], "folders": []})
        if r.status_code == 401:
            raise HTTPException(401, "Токен Яндекс Диска недействителен — подключите Диск заново")
        if r.status_code != 200:
            raise HTTPException(400, f"Яндекс Диск: {ya_error(r)}")
        dirs = [i for i in (r.json().get("_embedded") or {}).get("items", []) if i.get("type") == "dir"]
        sem = asyncio.Semaphore(6)

        async def files_of(d):
            async with sem:
                try:
                    return await ya_list(h, f"{root}/{d['name']}")
                except Exception:
                    return {}
        contents = await asyncio.gather(*(files_of(d) for d in dirs))
    out = []
    for d, items in zip(dirs, contents):
        vids = {n: sz for n, sz in items.items() if os.path.splitext(n)[1].lower() in VIDEO_EXT}
        local = DOWNLOAD_ROOT / d["name"]
        out.append({"name": d["name"], "link": d.get("public_url"), "modified": d.get("modified"),
                    "count": len(vids), "size": sum(v or 0 for v in vids.values()),
                    "local": local.is_dir(), "source_chat": folder_source_chat(local) if local.is_dir() else None,
                    "archived": local.is_dir() and bool(load_archive(local)),
                    "uploading": bool(folder_jobs(d["name"], ("running",)))})
    out.sort(key=lambda x: x["modified"] or "", reverse=True)
    return ok({"root": root[len("disk:"):], "folders": out})


async def ya_publish_folder(request: Request):
    name = ya_folder_name((await body(request)).get("folder"))
    async with ya_client() as h:
        try:
            return ok({"link": await ya_publish(h, f"{ya_root_path()}/{name}")})
        except RuntimeError as e:
            raise HTTPException(400, str(e))


async def ya_send(request: Request):
    """Отправить ссылку на папку Диска в выбранный чат: mode = saved | source | chat (+chat_id)."""
    b = await body(request)
    name = ya_folder_name(b.get("folder"))
    mode = b.get("mode")
    if mode not in ("saved", "source", "chat"):
        raise HTTPException(400, "Не выбран чат")
    target_chat = chat_id_of(b.get("chat_id")) if mode == "chat" else None
    remote = f"{ya_root_path()}/{name}"
    async with ya_client() as h:
        try:
            link = await ya_publish(h, remote)
            items = await ya_list(h, remote)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    vids = [v or 0 for n, v in items.items() if os.path.splitext(n)[1].lower() in VIDEO_EXT]
    try:
        label = await send_link(DOWNLOAD_ROOT / name, None, link, len(vids), sum(vids), mode, target_chat)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return ok({"sent": label, "link": link})


async def ya_delete_folder(request: Request):
    """Удаляет папку с Яндекс Диска в Корзину Диска (не навсегда). Видео на сервере не трогает."""
    name = ya_folder_name((await body(request)).get("folder"))
    if folder_jobs(name, ("running",)):
        raise HTTPException(409, "С этой папкой сейчас работает задание — дождитесь окончания или остановите его")
    async with ya_client() as h:
        r = await h.delete(YA_API + "/resources", params={"path": f"{ya_root_path()}/{name}"})
        if r.status_code == 404:
            raise HTTPException(404, "Такой папки на Диске уже нет")
        if r.status_code not in (202, 204):
            raise HTTPException(400, f"Яндекс Диск: {ya_error(r)}")
        try:
            await ya_wait_operation(h, r, "удалить папку")
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    return ok()


async def rename_folder(request: Request):
    """Переименовать папку на сервере и/или на Яндекс Диске. Сначала проверяем обе стороны, потом меняем."""
    b = await body(request)
    old = ya_folder_name(b.get("folder"))
    new = str(b.get("new") or "").strip()
    if not new or new != safe_name(new) or new in (".", ".."):
        raise HTTPException(400, 'Недопустимое имя. Нельзя: пустое, длиннее 80 символов, точка в начале или конце, символы / \\ : * ? " < > |')
    if new == old:
        raise HTTPException(400, "Новое имя совпадает со старым")
    do_server, do_disk = bool(b.get("server")), bool(b.get("disk")) and bool(ya_conf().get("token"))
    if not do_server and not do_disk:
        raise HTTPException(400, "Выберите, где переименовать")
    src, dst = DOWNLOAD_ROOT / old, DOWNLOAD_ROOT / new
    if folder_jobs(old) or folder_jobs(new):
        raise HTTPException(409, "С этой папкой есть задание в очереди или в работе — дождитесь окончания")
    server_has = do_server and src.is_dir()
    if server_has and dst.exists():
        raise HTTPException(409, f"На сервере уже есть папка «{new}»")
    root = ya_root_path() if do_disk else None
    async with (ya_client() if do_disk else httpx.AsyncClient()) as h:
        disk_has = False
        if do_disk:
            r = await h.get(YA_API + "/resources", params={"path": f"{root}/{old}", "fields": "name"})
            disk_has = r.status_code == 200
            if disk_has and (await h.get(YA_API + "/resources", params={"path": f"{root}/{new}", "fields": "name"})).status_code == 200:
                raise HTTPException(409, f"На Яндекс Диске уже есть папка «{new}»")
        if not server_has and not disk_has:
            raise HTTPException(404, "Папка не найдена ни на сервере, ни на Диске")
        result = {"server": "нет папки" if do_server else "не выбрано", "disk": "нет папки" if do_disk else "не выбрано"}
        if server_has:
            src.rename(dst)
            result["server"] = "переименована"
            shares_rename(old, new)
            for j in jobs.values():  # в истории заданий — новое имя, чтобы «Запустить снова» шло в ту же папку
                if j["folder"] == old and j["path"] == str(src):
                    j.update(folder=new, path=str(dst))
            mark()
        if disk_has:
            r = await h.post(YA_API + "/resources/move", params={"from": f"{root}/{old}", "path": f"{root}/{new}",
                                                               "overwrite": "false"})
            try:
                if r.status_code not in (201, 202):
                    raise RuntimeError(ya_error(r))
                await ya_wait_operation(h, r, "переименовать папку")
                result["disk"] = "переименована"
                arch_dir = dst if server_has else src
                a = load_archive(arch_dir) if arch_dir.is_dir() else {}
                if a and a.get("remote") == f"{root}/{old}":  # архив папки сервера указывает на новое место
                    a["remote"] = f"{root}/{new}"
                    save_archive(arch_dir, a)
            except RuntimeError as e:
                msg = f"Яндекс Диск: {e}"
                if server_has:
                    msg = f"На сервере переименована, а на Диске нет — {msg}"
                raise HTTPException(400, msg)
    return ok(result)


# ---------- приложение ----------

PUBLIC = {"/", "/login", "/logout", "/favicon.ico", "/favicon.svg"}

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect width="64" height="64" rx="14" fill="#2a8bf2"/>'
           '<path d="M25 19v26l21-13z" fill="#fff"/></svg>')
FAVICON_LINK = ('<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
                '<meta name="theme-color" content="#2a8bf2">')


async def favicon(request: Request):
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in PUBLIC and not scope["path"].startswith("/s/"):
            if not is_authed(Request(scope)):
                await JSONResponse({"detail": "Нужно войти в панель"}, status_code=403)(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


async def any_error(request: Request, exc: Exception):
    return JSONResponse({"detail": f"Внутренняя ошибка: {exc}"}, status_code=500)


@asynccontextmanager
async def lifespan(_app):
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        await get_client()
    except Exception as e:
        print("Не удалось подключиться к Telegram при старте:", e)
    jobs_state["wake"] = asyncio.Event()
    load_jobs()
    loop_task = asyncio.create_task(jobs_loop())
    clean_task = asyncio.create_task(cleanup_loop())
    upd_task = asyncio.create_task(update_loop())
    wake_scheduler()
    yield
    jobs_state["shutdown"] = True  # незавершённые задания остаются «в работе» и продолжатся после запуска
    await cancel_all(list(job_tasks.values()))
    loop_task.cancel()
    clean_task.cancel()
    upd_task.cancel()
    try:
        save_jobs()
    except Exception as e:
        print("Не удалось сохранить задания:", e)
    await drop_client()


routes = [
    Route("/", page),
    Route("/favicon.svg", favicon),
    Route("/favicon.ico", favicon),
    Route("/login", login, methods=["POST"]),
    Route("/logout", panel_logout, methods=["POST", "GET"]),
    Route("/api/status", status),
    Route("/api/config", set_config, methods=["POST"]),
    Route("/api/send_code", send_code, methods=["POST"]),
    Route("/api/sign_in", sign_in, methods=["POST"]),
    Route("/api/logout", tg_logout, methods=["POST"]),
    Route("/api/dialogs", dialogs),
    Route("/api/count/{chat_id}", count),
    Route("/api/start", start, methods=["POST"]),
    Route("/api/jobs", jobs_list),
    Route("/api/job", legacy_poll),
    Route("/api/ya/job", legacy_poll),
    Route("/api/jobs/stop", jobs_stop, methods=["POST"]),
    Route("/api/jobs/retry", jobs_retry, methods=["POST"]),
    Route("/api/jobs/clear", jobs_clear, methods=["POST"]),
    Route("/api/cleanup", cleanup_get),
    Route("/api/cleanup/run", cleanup_now, methods=["POST"]),
    Route("/api/update", update_get),
    Route("/api/update/check", update_now, methods=["POST"]),
    Route("/api/backup", backup_download, methods=["POST"]),
    Route("/api/backup/restore", backup_restore, methods=["POST"]),
    Route("/api/shares", shares_list),
    Route("/api/shares", shares_create, methods=["POST"]),
    Route("/api/shares/revoke", shares_revoke, methods=["POST"]),
    Route("/s/{token}", share_page),
    Route("/s/{token}/f/{name:path}", share_file),
    Route("/s/{token}/zip", share_zip),
    Route("/api/restore", restore_start, methods=["POST"]),
    Route("/api/open_folder", open_folder, methods=["POST"]),
    Route("/api/files", files),
    Route("/api/files/delete", file_delete, methods=["POST"]),
    Route("/files/{path:path}", file_download),
    Route("/zip/{path:path}", folder_zip),
    Route("/api/settings", settings_get),
    Route("/api/settings", settings_set, methods=["POST"]),
    Route("/api/ya/device_start", ya_device_start, methods=["POST"]),
    Route("/api/ya/device_poll", ya_device_poll, methods=["POST"]),
    Route("/api/ya/token", ya_set_token, methods=["POST"]),
    Route("/api/ya/logout", ya_logout, methods=["POST"]),
    Route("/api/ya/upload", ya_start, methods=["POST"]),
    Route("/api/ya/folders", ya_folders),
    Route("/api/ya/publish", ya_publish_folder, methods=["POST"]),
    Route("/api/ya/send", ya_send, methods=["POST"]),
    Route("/api/ya/delete", ya_delete_folder, methods=["POST"]),
    Route("/api/rename", rename_folder, methods=["POST"]),
]

app = Starlette(routes=routes, lifespan=lifespan,
                exception_handlers={HTTPException: http_error, Exception: any_error})
app.add_middleware(AuthMiddleware)


# ---------- страницы ----------

STYLE = r"""
:root{
  --bg:#f4f5f7; --card:#fff; --text:#16181d; --muted:#6b7280; --line:#e4e6eb;
  --accent:#2a8bf2; --accent-text:#fff; --err:#dc2626;
  --input:#fff; --sel:#e8f2fe; --track:#e9ecf1; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root{--bg:#0f1115; --card:#181b21; --text:#e8eaef; --muted:#9097a3; --line:#2a2e37;
    --input:#11141a; --sel:#1b2a3d; --track:#232831;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:650}
header .logo{width:30px;height:30px;border-radius:8px;background:var(--accent);display:grid;place-items:center;color:#fff}
header .upd{font-size:12px;font-weight:600;color:#fff;background:#16a34a;border:0;border-radius:99px;padding:3px 10px;cursor:pointer}
header .upd:hover{background:#15803d}
.relnotes{max-height:45vh;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:4px 14px;margin:10px 0;font-size:14px}
.relnotes h4{margin:12px 0 4px;font-size:14px}
.relnotes ul{margin:4px 0;padding-left:20px}
.relnotes li{margin:2px 0}
.cmd{font-family:var(--mono);font-size:12.5px;background:var(--input);border:1px solid var(--line);border-radius:8px;padding:10px 12px;white-space:pre-wrap;word-break:break-all}
header .ver{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:1px 8px}
header .me{margin-left:auto;color:var(--muted);font-size:14px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
main{max-width:1100px;margin:0 auto;padding:20px 16px;display:grid;gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;min-width:0}
.card h2{margin:0 0 4px;font-size:16px}
.spacewarn{background:#fef3c7;color:#78350f;border:1px solid #f59e0b;border-radius:10px;padding:10px 14px;font-size:14px}
.spacewarn a{color:inherit;font-weight:600}
@media (prefers-color-scheme: dark){.spacewarn{background:#3b2a07;color:#fde68a;border-color:#b45309}}
.disk.low #diskText{color:var(--err);font-weight:600}
.disk.low .bar>div{background:var(--err)}
.loader{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:56px 0;color:var(--muted);font-size:14px}
.loader.sm{flex-direction:row;padding:16px 12px;gap:10px;font-size:13px}
.spinner{width:30px;height:30px;border:3px solid var(--track);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;flex:0 0 auto}
.loader.sm .spinner{width:18px;height:18px;border-width:2px}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.spinner{animation-duration:2.5s}}
details.sblock>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;user-select:none}
details.sblock>summary::-webkit-details-marker{display:none}
details.sblock>summary h2{margin:0}
details.sblock>summary::after{content:"";margin-left:auto;width:9px;height:9px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg);transition:transform .15s;flex:0 0 auto;margin-right:4px}
details.sblock[open]>summary::after{transform:rotate(-135deg)}
details.sblock[open]>summary{margin-bottom:12px}
details.sblock:not([open]){padding-top:16px;padding-bottom:16px}
details.sblock>summary:hover h2{color:var(--accent)}
.card h3{font-size:14px;margin:20px 0 6px;padding-top:14px;border-top:1px solid var(--line)}
.card p.hint{margin:0 0 14px;color:var(--muted);font-size:14px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:18px}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 5px}
a{color:var(--accent)}
.btn{color:var(--text)}
.check{display:flex;gap:8px;align-items:center;color:var(--text);font-size:14px;margin:14px 0 0;cursor:pointer}
.check input{width:auto;margin:0}
textarea{width:100%;min-height:90px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--input);color:var(--text);font:13px/1.45 var(--mono);resize:vertical}
.settings summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:10px}
.settings summary::-webkit-details-marker{display:none}
.settings summary h2{margin:0}
.settings h3{font-size:14px;margin:18px 0 4px}
.code{font:600 26px/1 var(--mono);letter-spacing:4px;padding:12px 16px;border:1px dashed var(--accent);border-radius:10px;display:inline-block;margin:8px 0}
.ok{color:#16a34a}
ol.steps{margin:6px 0 0;padding-left:20px;font-size:14px;color:var(--muted)}
ol.steps li{margin:3px 0}
ol.steps code{font-family:var(--mono);font-size:12.5px;color:var(--text)}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--input);color:var(--text);font:inherit}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
.row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
.row>*{flex:1;min-width:140px}
.row>.fit{flex:0 0 auto;min-width:0}
button,.btn{padding:10px 16px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--text);font:inherit;font-weight:550;cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
button.danger{background:var(--err);border-color:var(--err);color:#fff}
button:disabled{opacity:.5;cursor:not-allowed}
button.link{border:0;background:none;color:var(--muted);padding:4px 6px;text-decoration:underline}
button.sm,.btn.sm{padding:5px 10px;font-size:13px;border-radius:7px}
.hidden{display:none!important}
.err{color:var(--err);font-size:14px;margin-top:10px;min-height:1em}
.list{border:1px solid var(--line);border-radius:10px;max-height:360px;overflow:auto;margin-top:10px}
.item{display:flex;justify-content:space-between;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.item:last-child{border-bottom:0}
.item:hover{background:var(--sel)}
.item.sel{background:var(--sel);box-shadow:inset 3px 0 var(--accent)}
.item .t{font-size:12px;color:var(--muted);white-space:nowrap}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;background:var(--track);color:var(--muted)}
.chosen{margin-top:12px;padding:12px;border-radius:10px;background:var(--sel);font-size:14px}
.bar{height:10px;background:var(--track);border-radius:99px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--accent);transition:width .4s}
.bar.small{height:6px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0}
@media (max-width:560px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
.cur{font-family:var(--mono);font-size:13px;margin:12px 0 6px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;word-break:break-all}
.log{font-family:var(--mono);font-size:12.5px;background:var(--input);border:1px solid var(--line);border-radius:10px;padding:10px 12px;height:220px;overflow:auto;white-space:pre-wrap;margin-top:12px}
.muted{color:var(--muted)}
.fgroup{border:1px solid var(--line);border-radius:10px;margin-top:10px;overflow:hidden}
.fgroup summary{display:flex;gap:10px;align-items:center;padding:10px 12px;cursor:pointer;background:var(--input);list-style:none}
.fgroup summary::-webkit-details-marker{display:none}
.fgroup summary b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow{display:flex;gap:8px;align-items:center;padding:8px 12px;border-top:1px solid var(--line);font-size:14px;flex-wrap:wrap}
.ibtn{width:34px;height:34px;padding:0;display:inline-grid;place-items:center;border-radius:8px;flex:0 0 auto;color:var(--text)}
.ibtn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ibtn:hover{border-color:var(--accent);color:var(--accent)}
.ibtn.del:hover{border-color:var(--err);color:var(--err)}
.ibtn.sm{width:30px;height:30px;padding:0;font-size:0}
.ibtn.sm svg{width:16px;height:16px}
.tabs{display:flex;gap:2px;padding:0 16px;background:var(--card);border-bottom:1px solid var(--line);overflow-x:auto}
.tab{border:0;border-bottom:2px solid transparent;border-radius:0;background:none;padding:12px 14px;color:var(--muted);white-space:nowrap}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}
.tab .pill{margin-left:6px}
.tabpane{display:grid;gap:18px}
details.logwrap>summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:12px;width:max-content}
.linkrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 0;font-size:14px}
.linkrow a.l{word-break:break-all}
.yrow{display:flex;gap:10px;align-items:center;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-top:8px;flex-wrap:wrap}
.yrow .nm{flex:1;min-width:200px}
.yrow .nm b{display:block;word-break:break-all}
.yrow .nm .meta{font-size:12px;color:var(--muted)}
.yrow .nm a{font-size:13px;word-break:break-all}
.job{border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:10px}
.jhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.jhead .jt{flex:1;min-width:200px;font-weight:600;word-break:break-word}
.jhead .muted{font-size:13px}
.jsec{font-size:13px;margin:10px 0 5px;color:var(--muted)}
.jact{font-family:var(--mono);font-size:12.5px;display:flex;justify-content:space-between;gap:10px;margin:6px 0 3px;flex-wrap:wrap;word-break:break-all}
.pill.st-running{background:var(--sel);color:var(--accent)}
.pill.st-done{color:#16a34a}
.pill.st-error{color:var(--err)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);display:grid;place-items:center;z-index:50;padding:16px}
.modal-box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:min(480px,100%);max-height:88vh;display:flex;flex-direction:column}
.modal-box .list{max-height:52vh}
#confirmModal{z-index:65}  /* подтверждение всегда поверх других окон */
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 16px;border-radius:10px;font-size:14px;z-index:70;max-width:calc(100% - 32px)}
.combo{position:relative}
.combo input{padding-right:40px}
.combo .arrow{position:absolute;right:5px;top:50%;transform:translateY(-50%);width:30px;height:30px;padding:0;border:0;background:none;color:var(--muted);display:grid;place-items:center;border-radius:7px}
.combo .arrow:hover{background:var(--sel);color:var(--text)}
.combo .arrow svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.combo-list{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:20;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.22);max-height:290px;overflow:auto;padding:4px}
.combo-opt{padding:9px 10px;border-radius:7px;cursor:pointer;display:flex;justify-content:space-between;align-items:baseline;gap:12px;font-size:14px}
.combo-opt.active{background:var(--sel)}
.combo-opt .t{color:var(--muted);font-size:12px;white-space:nowrap}
.combo-opt.empty{color:var(--muted)}
.combo-sep{height:1px;background:var(--line);margin:4px 6px}
.icons{display:flex;gap:6px;align-items:center;margin-left:auto}
.frow .n{flex:1;min-width:180px;font-family:var(--mono);font-size:13px;word-break:break-all}
.disk{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:14px}
.disk .bar{flex:1;min-width:160px}
"""

LOGIN_PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Telegram Video Panel</title>""" + FAVICON_LINK + r"""<style>""" + STYLE + r"""
.box{max-width:380px;margin:12vh auto;padding:0 16px}
</style></head><body>
<div class="box"><form class="card" method="post" action="/login">
  <h2>Telegram Video Panel</h2>
  <p class="hint">Введите пароль панели.</p>
  <input type="password" name="password" autofocus autocomplete="current-password" required>
  <div class="err"><!--ERR--></div>
  <button class="primary" style="width:100%;margin-top:6px">Войти</button>
</form></div></body></html>"""

SHARE_PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="referrer" content="no-referrer">
<title><!--T--></title>""" + FAVICON_LINK + r"""<style>""" + STYLE + r"""
#pl{width:100%;max-height:70vh;background:#000;border-radius:10px;display:block}
.now{font-family:var(--mono);font-size:13px;margin:10px 0 12px;word-break:break-all;color:var(--muted)}
.plist{display:grid;gap:4px}
.pitem{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:8px;cursor:pointer}
.pitem:hover,.pitem.on{background:var(--sel)}
.pitem .n{flex:1;min-width:0;word-break:break-all;font-size:14px}
.pitem .muted{font-size:12px;white-space:nowrap}
main{max-width:900px}
</style></head><body>
<header><div class="logo">▶</div><h1 style="font-size:16px"><!--T--></h1></header>
<main><!--B--></main>
<script>
(function(){
  var pl = document.getElementById('pl'), list = document.getElementById('plist'), now = document.getElementById('now');
  if(!pl || !list) return;
  function play(it){
    [].forEach.call(list.querySelectorAll('.pitem'), function(x){ x.classList.toggle('on', x === it); });
    pl.src = it.getAttribute('data-src'); now.textContent = it.getAttribute('data-name'); pl.play();
    window.scrollTo({top: 0, behavior: 'smooth'});
  }
  list.addEventListener('click', function(e){
    if(e.target.closest('a')) return;
    var it = e.target.closest('.pitem'); if(it) play(it);
  });
  pl.addEventListener('ended', function(){
    var cur = list.querySelector('.pitem.on'), nx = cur && cur.nextElementSibling; if(nx) play(nx);
  });
})();
</script></body></html>"""

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Video Panel</title>
""" + FAVICON_LINK + r"""
<style>""" + STYLE + r"""</style>
</head>
<body>
<header>
  <div class="logo">▶</div>
  <h1>Telegram Video Panel</h1><span class="ver" title="Версия панели">v""" + VERSION + r"""</span><button class="upd hidden" id="updBadge" type="button"></button>
  <div class="me">
    <span id="meName"></span>
    <button class="link hidden" id="logoutBtn">Выйти из Telegram</button>
    <a class="btn sm hidden" id="panelLogout" href="/logout">Выйти из панели</a>
  </div>
</header>
<nav class="tabs hidden" id="tabs" role="tablist">
  <button class="tab active" data-tab="tg" role="tab">🎬 Видео</button>
  <button class="tab" data-tab="set" role="tab">⚙ Настройки<span class="pill" id="yaTabPill" title="Яндекс Диск"></span></button>
</nav>
<main>
  <div class="err" id="globalErr" style="margin:0"></div>
  <div class="loader hidden" id="pageLoader" role="status"><div class="spinner"></div><span>Загрузка...</span></div>
  <div class="spacewarn hidden" id="reloadBar" role="status"></div>
  <div class="spacewarn hidden" id="spaceWarn" role="alert"></div>
<div class="tabpane" id="tabTg">
  <section class="card hidden" id="jobsCard">
    <div class="row" style="align-items:center">
      <div><h2>Задания</h2><p class="hint" style="margin:0" id="jobsHint"></p></div>
      <div class="fit"><button class="sm" id="jobsClear">Очистить завершённые</button></div>
    </div>
    <div id="jobsList"></div>
  </section>

  <section class="card hidden" id="keysCard">
    <h2>Ключи Telegram API</h2>
    <p class="hint">Получите их на my.telegram.org → API development tools (один раз).</p>
    <div class="row">
      <div><label>api_id</label><input id="apiId" inputmode="numeric" placeholder="1234567"></div>
      <div><label>api_hash</label><input id="apiHash" placeholder="0123456789abcdef…"></div>
    </div>
    <div style="margin-top:14px"><button class="primary" id="keysBtn">Сохранить</button></div>
    <div class="err" id="keysErr"></div>
    <details style="margin-top:12px"><summary class="muted" style="font-size:13px;cursor:pointer">Есть резервная копия с другого сервера? Восстановить</summary>
      <p class="hint" style="margin:8px 0 0">Вернёт ключи Telegram, вход в Telegram, подключение Яндекс Диска и все настройки.</p>
      <div class="row" style="margin-top:8px">
        <div><label>Файл резервной копии (.tvpbak)</label><input type="file" id="rs1File" accept=".tvpbak"></div>
        <div><label>Пароль копии</label><input type="password" id="rs1Pwd" autocomplete="off"></div>
        <div class="fit"><button id="rs1Btn" type="button">Восстановить</button></div>
      </div>
      <div class="err" id="rs1Err"></div>
    </details>
  </section>

  <section class="card hidden" id="loginCard">
    <h2>Вход в Telegram</h2>
    <p class="hint">Код придёт в приложение Telegram. Сессия сохранится, входить нужно один раз.</p>
    <div class="row">
      <div><label>Номер телефона</label><input id="phone" placeholder="+7…" autocomplete="tel"></div>
      <div class="fit"><button class="primary" id="phoneBtn">Получить код</button></div>
    </div>
    <div class="row hidden" id="codeRow">
      <div><label>Код из Telegram</label><input id="code" inputmode="numeric" autocomplete="one-time-code"></div>
      <div class="fit"><button class="primary" id="codeBtn">Войти</button></div>
    </div>
    <div class="row hidden" id="pwdRow">
      <div><label>Облачный пароль (двухэтапная проверка)</label><input id="pwd" type="password"></div>
      <div class="fit"><button class="primary" id="pwdBtn">Войти</button></div>
    </div>
    <div class="err" id="loginErr"></div>
    <button class="link" id="changeKeys">Изменить api_id / api_hash</button>
  </section>

  <div class="grid hidden" id="mainGrid">
    <section class="card">
      <h2>Группа</h2>
      <p class="hint">Выберите группу или канал, из которого скачивать видео.</p>
      <div class="row">
        <div><input id="search" placeholder="Поиск по названию…"></div>
        <div class="fit"><button class="ibtn" id="reloadBtn" data-icon="refresh" title="Обновить список групп" aria-label="Обновить список групп"></button></div>
      </div>
      <div class="list" id="dlgList"><div class="loader sm"><div class="spinner"></div><span>Загрузка...</span></div></div>
      <div class="chosen hidden" id="chosen"></div>
    </section>

    <section class="card">
      <h2>Скачивание</h2>
      <p class="hint">Имена файлов — как при сохранении из Telegram; одинаковые получают номер: «имя (1)». Уже скачанные пропускаются.</p>
      <label id="folderLabel">Папка для сохранения</label>
      <div class="row">
        <div class="combo" id="folderCombo">
          <input id="folder" role="combobox" aria-expanded="false" aria-controls="folderList" aria-autocomplete="list" autocomplete="off">
          <button type="button" class="arrow hidden" id="folderArrow" tabindex="-1" aria-label="Показать существующие папки"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>
          <div class="combo-list hidden" id="folderList" role="listbox"></div>
        </div>
        <div class="fit"><button id="openBtn">Открыть</button></div>
      </div>
      <div class="muted hidden" id="folderHint" style="font-size:13px;margin-top:6px"></div>
      <div class="row">
        <div><label>Период: с даты</label><input type="date" id="dateFrom"></div>
        <div><label>по дату (пусто — по сегодня)</label><input type="date" id="dateTo"></div>
      </div>
      <div class="row">
        <div>
          <label>Качество</label>
          <select id="quality">
            <option value="original">Оригинал (без изменений)</option>
            <option value="1080">До 1080p</option>
            <option value="720">До 720p</option>
            <option value="480">До 480p</option>
          </select>
        </div>
        <div>
          <label>Расширение файла</label>
          <select id="ext">
            <option value="original">Как в Telegram</option>
            <option value=".mp4">.mp4</option>
            <option value=".mov">.mov</option>
            <option value=".m4v">.m4v</option>
            <option value=".mkv">.mkv</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div>
          <label>Порядок</label>
          <select id="order">
            <option value="old">Сначала старые</option>
            <option value="new">Сначала новые</option>
          </select>
        </div>
      </div>
      <div class="muted" id="extHint" style="font-size:13px;margin-top:6px"></div>
      <div class="muted hidden" id="noFfmpeg" style="font-size:13px;margin-top:6px">Для смены качества нужен ffmpeg. Он не найден, поэтому доступен только оригинал.</div>
      <div class="muted" id="yaAutoStatus" style="font-size:13px;margin-top:12px"></div>
      <div class="row" style="margin-top:16px">
        <button class="primary" id="startBtn" disabled>Начать скачивание</button>
      </div>
      <div class="muted" style="font-size:13px;margin-top:6px">Если уже идут задания — новое встанет в очередь. Прогресс — в блоке «Задания» вверху.</div>
      <div class="err" id="startErr"></div>
    </section>
  </div>


  <section class="card hidden" id="filesCard">
    <div class="row" style="align-items:center">
      <div><h2>Скачанные видео</h2><p class="hint" style="margin:0" id="rootPath"></p></div>
      <div class="fit"><button class="ibtn" id="filesReload" data-icon="refresh" title="Обновить список видео" aria-label="Обновить список видео"></button></div>
    </div>
    <div class="disk" style="margin-top:14px"><span id="diskText"></span><div class="bar small"><div id="diskBar"></div></div></div>
    <div id="filesList"></div>
  </section>

  <section class="card hidden" id="yaFoldersCard">
    <div class="row" style="align-items:center">
      <div><h2>Папки на Яндекс Диске</h2><p class="hint" style="margin:0" id="yaRootHint"></p></div>
      <div class="fit"><button class="ibtn" id="yaFoldersReload" data-icon="refresh" title="Обновить список папок" aria-label="Обновить список папок"></button></div>
    </div>
    <div id="yaFolders"><div class="loader sm"><div class="spinner"></div><span>Загрузка...</span></div></div>
  </section>
</div>

<div class="tabpane hidden" id="tabSet">
  <details class="card sblock" id="parCard">
    <summary><h2>Параллельность</h2></summary>
    <p class="hint">Больше — быстрее, но Telegram может попросить подождать, а сервер тратит больше канала и места.</p>
    <div class="row">
      <div><label>Заданий на скачивание одновременно</label><input type="number" id="stJobs" min="1" max="5"></div>
      <div><label>Видео из Telegram одновременно (всего)</label><input type="number" id="stTg" min="1" max="8"></div>
      <div><label>Файлов на Яндекс Диск одновременно (всего)</label><input type="number" id="stYa" min="1" max="8"></div>
    </div>
  </details>
  <details class="card sblock" id="backupCard">
    <summary><h2>Резервная копия</h2></summary>
    <p class="hint">Один зашифрованный файл: ключи Telegram API, вход в Telegram, подключение Яндекс Диска и все настройки панели.
      Пригодится для переезда на другой сервер. Видео и пароль панели в копию не входят.</p>
    <div class="row">
      <div><label>Пароль для копии (не короче 8 символов)</label><input type="password" id="bkPwd" autocomplete="new-password"></div>
      <div><label>Пароль ещё раз</label><input type="password" id="bkPwd2" autocomplete="new-password"></div>
      <div class="fit"><button class="primary" id="bkBtn" type="button">Скачать копию</button></div>
    </div>
    <div class="muted" style="font-size:13px;margin-top:6px">⚠ Запомните пароль: без него копию не открыть никак. Храните файл надёжно — в нём вход в ваш Telegram.</div>
    <div class="err" id="bkErr"></div>
    <h3>Восстановить из копии</h3>
    <p class="hint" style="margin:0">Заменит текущие ключи, вход в Telegram, подключение Диска и настройки на те, что в копии.</p>
    <div class="row" style="margin-top:8px">
        <div><label>Файл резервной копии (.tvpbak)</label><input type="file" id="rs2File" accept=".tvpbak"></div>
        <div><label>Пароль копии</label><input type="password" id="rs2Pwd" autocomplete="off"></div>
        <div class="fit"><button id="rs2Btn" type="button">Восстановить</button></div>
      </div>
      <div class="err" id="rs2Err"></div>
  </details>
  <details class="card sblock" id="updCard">
    <summary><h2>Обновления</h2></summary>
    <label class="check"><input type="checkbox" id="stUpdate"> Проверять новые версии на GitHub (раз в 6 часов)</label>
    <div class="muted" style="font-size:13px;margin-top:4px">Панель только сообщает о новой версии и показывает команду обновления — сама себя она не обновляет.</div>
    <div style="margin-top:10px"><button class="sm" id="updCheckNow" type="button">Проверить сейчас</button> <span class="muted" id="updState" style="font-size:13px"></span></div>
  </details>
  <details class="card sblock" id="cleanCard">
    <summary><h2>Очистка сервера</h2></summary>
    <p class="hint">Удаляются только видео, которые точно есть на Яндекс Диске (сверка имени и размера). Папка остаётся
      в «Скачанных видео» с пометкой «в архиве» — видео можно вернуть кнопкой «Восстановить с Диска».
      Проверка — каждые 30 минут и после каждого задания.</p>
    <div class="row" style="align-items:center">
      <label class="check fit" style="margin:0"><input type="checkbox" id="stCleanAge"> Удалять видео старше</label>
      <div class="fit"><input type="number" id="stCleanDays" min="1" max="3650" style="width:90px"></div>
      <div class="fit muted">дней</div>
    </div>
    <div class="row" style="align-items:center;margin-top:8px">
      <label class="check fit" style="margin:0"><input type="checkbox" id="stCleanSpace"> Удалять самые старые, когда свободно меньше</label>
      <div class="fit"><input type="number" id="stCleanGb" min="1" style="width:90px"></div>
      <div class="fit muted">ГБ</div>
    </div>
    <div class="row" style="align-items:center;margin-top:8px">
      <div class="fit" style="font-size:14px">Предупреждать, когда свободно меньше</div>
      <div class="fit"><input type="number" id="stWarnGb" min="0" style="width:90px"></div>
      <div class="fit muted">ГБ (0 — не предупреждать)</div>
    </div>
    <div class="muted" id="cleanHint" style="font-size:13px;margin-top:8px"></div>
    <div style="margin-top:12px"><button class="sm" id="cleanNow" type="button">Очистить сейчас</button> <span class="muted" id="cleanRes" style="font-size:13px"></span></div>
    <details class="logwrap"><summary>Журнал очистки</summary><div class="log" id="cleanLog" style="height:140px"></div></details>
  </details>
  <details class="card sblock" id="yaSettingsCard">
    <summary><h2>Яндекс Диск</h2><span class="pill" id="yaPill"></span></summary>
    <h3>Подключение</h3>
      <div id="yaConnected" class="hidden">
        <p style="margin:6px 0" id="yaInfo"></p>
        <button class="sm" id="yaLogout">Отключить Диск</button>
      </div>
      <div id="yaConnect" class="hidden">
        <p class="hint" style="margin:4px 0 6px">Один раз создайте своё приложение Яндекса — это бесплатно и занимает пару минут:</p>
        <ol class="steps">
          <li>Откройте <a href="https://oauth.yandex.ru/client/new" target="_blank" rel="noopener">oauth.yandex.ru/client/new</a> под своим аккаунтом Яндекса.</li>
          <li>Название — любое, например <code>Telegram Video Panel</code>. Платформа — <b>«Веб-сервисы»</b>, Redirect URI — <code>https://oauth.yandex.ru/verification_code</code>.</li>
          <li>В доступах найдите <b>Яндекс Диск REST API</b> и отметьте: «Доступ к информации о Диске», «Чтение всего Диска», «Запись в любом месте на Диске».</li>
          <li>Создайте приложение и скопируйте сюда <b>ClientID</b> и <b>Client secret</b>.</li>
        </ol>
        <div class="row">
          <div><label>ClientID</label><input id="yaCid" autocomplete="off"></div>
          <div><label>Client secret</label><input id="yaSecret" type="password" autocomplete="off" placeholder="сохранён, можно не вводить"></div>
          <div class="fit"><button class="primary" id="yaConnectBtn">Подключить</button></div>
        </div>
        <div id="yaCodeBox" class="hidden" style="margin-top:12px">
          <div>Откройте <a id="yaUrl" target="_blank" rel="noopener" href="https://ya.ru/device">ya.ru/device</a> (на телефоне или компьютере), войдите в Яндекс и введите код:</div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <div class="code" id="yaCode"></div>
            <button class="sm" id="yaCopy" type="button">Скопировать</button>
          </div>
          <div class="muted" id="yaWait" style="font-size:13px">Жду подтверждения…</div>
        </div>
        <details style="margin-top:12px"><summary class="muted" style="font-size:13px;cursor:pointer">У меня уже есть OAuth-токен</summary>
          <div class="row"><div><input id="yaToken" type="password" placeholder="y0_…"></div><div class="fit"><button id="yaTokenBtn">Сохранить токен</button></div></div>
        </details>
      </div>
      <div class="err" id="yaErr"></div>
    <h3>Загрузка, архив и ссылка</h3>
      <label>Папка на Яндекс Диске (внутри будут подпапки как на сервере)</label>
      <input id="stRoot" placeholder="Telegram Video Panel">
      <label class="check"><input type="checkbox" id="stAuto"> Автоматически загружать видео на Яндекс Диск при скачивании (каждое — сразу после скачивания)</label>
      <div class="muted" id="stAutoHint" style="font-size:13px;margin-top:4px"></div>
      <label class="check"><input type="checkbox" id="stArchive"> После загрузки и проверки, что все видео папки есть на Диске, удалять их с сервера</label>
      <div class="muted" style="font-size:13px;margin-top:4px">Папка останется в «Скачанных видео» с пометкой «в архиве» — её можно восстановить с Диска. Освобождает место на сервере.</div>
      <label class="check"><input type="checkbox" id="stInfo"> Создавать файл info.txt</label>
      <div class="muted" style="font-size:13px;margin-top:4px">После загрузки в папке на сервере и на Диске появится info.txt с тем же текстом, что и сообщение со ссылкой (шаблон ниже).</div>
      <label class="check"><input type="checkbox" id="stSend"> Отправлять ссылку на папку в Telegram после загрузки</label>
      <div class="row">
        <div><label>Куда отправлять</label>
          <select id="stTarget">
            <option value="source">В группу, из которой скачаны видео</option>
            <option value="saved">В «Избранное»</option>
            <option value="chat">В выбранный чат…</option>
          </select></div>
        <div id="stChatWrap" class="hidden"><label>Чат</label><select id="stChat"></select></div>
      </div>
      <label>Текст сообщения и info.txt — можно использовать {folder}, {period}, {count}, {size}, {link}</label>
      <textarea id="stTpl"></textarea>
  </details>
  <div><button class="primary" id="stSave">Сохранить настройки</button> <span class="ok" id="stSaved"></span></div>
  <div class="err" id="stErr" style="margin:0"></div>
</div>
</main>

<div class="modal hidden" id="sendModal" role="dialog" aria-modal="true" aria-labelledby="sendTitle">
  <div class="modal-box">
    <h2 id="sendTitle" style="margin:0 0 4px;font-size:16px">Отправить ссылку</h2>
    <p class="hint" id="sendWhat" style="margin:0 0 10px"></p>
    <input id="sendSearch" placeholder="Поиск чата…" autocomplete="off">
    <div class="list" id="sendList"></div>
    <div class="err" id="sendErr"></div>
    <div style="margin-top:12px;text-align:right"><button id="sendCancel">Отмена</button></div>
  </div>
</div>
<div class="modal hidden" id="renameModal" role="dialog" aria-modal="true" aria-labelledby="renameTitle">
  <form class="modal-box" id="renameForm">
    <h2 id="renameTitle" style="margin:0 0 4px;font-size:16px">Переименовать папку</h2>
    <p class="hint" id="renameWhat" style="margin:0 0 10px"></p>
    <input id="renameInput" autocomplete="off" required>
    <label class="check" id="renameServerWrap"><input type="checkbox" id="renameServer" checked> На сервере</label>
    <label class="check" id="renameDiskWrap"><input type="checkbox" id="renameDisk" checked> На Яндекс Диске</label>
    <div class="muted" style="font-size:13px;margin-top:6px">Если папки на одной из сторон нет — там ничего не изменится.</div>
    <div class="err" id="renameErr"></div>
    <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
      <button type="button" id="renameCancel">Отмена</button>
      <button class="primary" id="renameOk">Переименовать</button>
    </div>
  </form>
</div>
<div class="modal hidden" id="confirmModal" role="alertdialog" aria-modal="true" aria-labelledby="confirmText">
  <div class="modal-box">
    <p id="confirmText" style="margin:0 0 16px;font-size:15px;line-height:1.5"></p>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button type="button" id="confirmNo">Отмена</button>
      <button type="button" class="danger" id="confirmYes">Да</button>
    </div>
  </div>
</div>
<div class="modal hidden" id="updModal" role="dialog" aria-modal="true" aria-labelledby="updTitle">
  <div class="modal-box">
    <h2 id="updTitle" style="margin:0 0 4px;font-size:16px"></h2>
    <p class="hint" id="updSub" style="margin:0"></p>
    <div class="relnotes" id="updNotes"></div>
    <div style="font-size:14px;margin:4px 0 6px">Как обновить — на сервере выполните:</div>
    <div class="cmd" id="updCmd"></div>
    <div class="muted" id="updNote" style="font-size:13px;margin-top:6px"></div>
    <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
      <a class="btn" id="updLink" target="_blank" rel="noopener">На GitHub</a>
      <button type="button" id="updCopy">Скопировать команду</button>
      <button type="button" class="primary" id="updClose">Закрыть</button>
    </div>
  </div>
</div>
<div class="modal hidden" id="shareModal" role="dialog" aria-modal="true" aria-labelledby="shareTitle">
  <div class="modal-box">
    <h2 id="shareTitle" style="margin:0 0 4px;font-size:16px">Ссылка для просмотра</h2>
    <p class="hint" style="margin:0 0 10px">Страница с плеером и скачиванием видео этой папки — открывается без пароля панели.
      Видна только эта папка. Ссылку можно отозвать в любой момент.</p>
    <div class="row" style="align-items:end">
      <div><label>Срок действия</label>
        <select id="shareDays"><option value="1">1 день</option><option value="7" selected>7 дней</option><option value="30">30 дней</option><option value="0">Бессрочно</option></select></div>
      <div class="fit"><button class="primary" id="shareCreate" type="button">Создать ссылку</button></div>
    </div>
    <div class="err" id="shareErr"></div>
    <h3 style="font-size:14px;margin:16px 0 6px">Активные ссылки</h3>
    <div id="shareList" class="muted" style="font-size:14px"></div>
    <div style="margin-top:14px;text-align:right"><button type="button" id="shareClose">Закрыть</button></div>
  </div>
</div>
<div class="toast hidden" id="toast" role="status"></div>

<script>
function tvpShowError(msg){
  var el = document.getElementById('globalErr');
  if(el) el.textContent = 'Ошибка в браузере: ' + msg + ' — попробуйте обновить браузер или открыть панель в Chrome / Safari последней версии.';
}
window.onerror = function(msg, src, line){ tvpShowError(msg + (line ? ' (строка ' + line + ')' : '')); };
window.onunhandledrejection = function(e){ tvpShowError(e && e.reason && e.reason.message ? e.reason.message : String(e && e.reason)); };
</script>
<script>
const $ = s => document.querySelector(s);
const PAGE_VERSION = '""" + VERSION + r"""';
const ICON = {
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  zip: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M12 11v6"/><path d="m9.5 14.5 2.5 2.5 2.5-2.5"/>',
  cloud: '<path d="M7 18a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10a4 4 0 0 1 0 8"/><path d="M12 12v9"/><path d="m8.5 15.5 3.5-3.5 3.5 3.5"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  play: '<path d="M7 4v16l13-8z"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  restore: '<path d="M7 18a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10a4 4 0 0 1 0 8"/><path d="M12 12v9"/><path d="m8.5 17.5 3.5 3.5 3.5-3.5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4"/><path d="m15.4 6.5-6.8 4"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1 1"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1-1"/>',
};
// своё окно подтверждения: системный confirm() во встроенных браузерах (Telegram, ярлык на iPhone)
// молча возвращает «Отмена», и удаление «не работает»
function askConfirm(text, yesLabel){
  return new Promise(resolve => {
    const m = document.getElementById('confirmModal'), yes = document.getElementById('confirmYes'), no = document.getElementById('confirmNo');
    document.getElementById('confirmText').textContent = text;
    yes.textContent = yesLabel || 'Да';
    const done = v => { m.classList.add('hidden'); yes.onclick = no.onclick = m.onclick = document.onkeydown = null; resolve(v); };
    yes.onclick = () => done(true); no.onclick = () => done(false);
    m.onclick = e => { if(e.target === m) done(false); };
    document.onkeydown = e => { if(e.key === 'Escape') done(false); };
    m.classList.remove('hidden'); no.focus();
  });
}
const svg = n => '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICON[n] + '</svg>';
function ibtn(icon, title, attrs, tag){
  const t = esc(title), cls = 'ibtn sm' + (icon === 'trash' ? ' del' : '');
  return tag === 'a' ? `<a class="btn ${cls}" title="${t}" aria-label="${t}" ${attrs}>${svg(icon)}</a>`
                     : `<button class="${cls}" title="${t}" aria-label="${t}" ${attrs}>${svg(icon)}</button>`;
}
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = svg(el.dataset.icon));
let dialogs = [], selected = null, S = {};

async function api(path, body, timeoutMs){
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  opt.credentials = 'same-origin';
  let timer = null;
  if(timeoutMs){ const ac = new AbortController(); opt.signal = ac.signal; timer = setTimeout(() => ac.abort(), timeoutMs); }
  let r;
  // сервер может на полминуты перезапускаться (обновление) — повторяем запрос, а не сдаёмся сразу
  for(let attempt = 0; ; attempt++){
    try{ r = await fetch(path, opt); break; }
    catch(e){
      if(e.name === 'AbortError'){ clearTimeout(timer); throw new Error('нет ответа за ' + Math.round(timeoutMs/1000) + ' с'); }
      if(attempt >= 3){ clearTimeout(timer); throw new Error('сервер не отвечает — возможно, он перезапускается; попробуйте через минуту (' + e.message + ')'); }
      if(body !== undefined && typeof toast === 'function') toast('Сервер недоступен (возможно, перезапускается) — повторяю…');
      await new Promise(res => setTimeout(res, 2000 * (attempt + 1)));
    }
  }
  clearTimeout(timer);
  if(r.status === 403){ location.reload(); throw new Error('Нужно войти'); }
  const d = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : (r.statusText || 'Ошибка'));
  return d;
}
const show = (el, on=true) => el.classList.toggle('hidden', !on);
function human(n){ n = +n||0; const u=['Б','КБ','МБ','ГБ','ТБ']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++} return (i?n.toFixed(1):n.toFixed(0))+' '+u[i]; }
function dur(s){ s=Math.max(0,Math.round(s)); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60; return (h?h+' ч ':'')+(h||m?m+' мин ':'')+x+' с'; }
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function dayStart(v, plus=0){ if(!v) return null; const d = new Date(v+'T00:00:00'); d.setDate(d.getDate()+plus); return d.toISOString(); }
function period(){ return {date_from: dayStart($('#dateFrom').value), date_to: dayStart($('#dateTo').value, 1)}; }
function store(k,v){ try{ localStorage.setItem(k,v) }catch(e){} }
function recall(k){ try{ return localStorage.getItem(k) }catch(e){ return null } }
const fileUrl = p => '/files/' + p.split('/').map(encodeURIComponent).join('/');

async function init(){
  $('#globalErr').textContent = '';
  show($('#pageLoader'));
  try{ S = await api('/api/status', undefined, 20000); }
  catch(e){ show($('#pageLoader'), false); $('#globalErr').textContent = 'Не удалось получить ответ панели: ' + e.message +
    '. Если на этом компьютере стоит антивирус или блокировщик рекламы — попробуйте отключить проверку для этого адреса или открыть панель в режиме инкогнито.'; return; }
  show($('#pageLoader'), false);
  if(S.error) $('#globalErr').textContent = S.error;
  show($('#panelLogout'), S.panel_auth);
  if(S.server){
    $('#folderLabel').textContent = 'Подпапка';
    $('#folder').placeholder = 'Не задано — или выберите / впишите папку';
    show($('#openBtn'), false);
  }
  const savedFolder = recall('folder');
  $('#folder').value = savedFolder !== null ? savedFolder : S.default_folder;
  $('#quality').value = recall('quality') || 'original';
  $('#ext').value = recall('ext') || 'original';
  $('#order').value = recall('order') || 'old';
  if(!S.ffmpeg){ $('#quality').value='original'; $('#quality').disabled = true; show($('#noFfmpeg')); }
  syncExt();

  show($('#keysCard'), !S.configured);
  show($('#loginCard'), S.configured && !S.authorized);
  show($('#mainGrid'), S.authorized);
  show($('#logoutBtn'), S.authorized);
  show($('#filesCard'), S.server);
  if(S.server) loadFiles();
  show($('#tabs'), S.authorized);
  setTab(S.authorized ? (recall('tab') || 'tg') : 'tg');
  if(S.authorized){
    $('#meName').textContent = S.me || '';
    loadDialogs();
    loadSettings();
    pollJobs();
    loadUpdate();
  }
}

$('#keysBtn').onclick = async () => {
  $('#keysErr').textContent='';
  try{ await api('/api/config', {api_id: $('#apiId').value, api_hash: $('#apiHash').value}); init(); }
  catch(e){ $('#keysErr').textContent = e.message; }
};
$('#changeKeys').onclick = () => { show($('#loginCard'), false); show($('#keysCard')); };

$('#phoneBtn').onclick = async () => {
  $('#loginErr').textContent=''; $('#phoneBtn').disabled = true;
  try{ await api('/api/send_code', {phone: $('#phone').value}); show($('#codeRow')); $('#code').focus(); }
  catch(e){ $('#loginErr').textContent = e.message; }
  $('#phoneBtn').disabled = false;
};
$('#codeBtn').onclick = async () => {
  $('#loginErr').textContent='';
  try{
    const r = await api('/api/sign_in', {code: $('#code').value});
    if(r.need_password){ show($('#pwdRow')); $('#pwd').focus(); } else init();
  }catch(e){ $('#loginErr').textContent = e.message; }
};
$('#pwdBtn').onclick = async () => {
  $('#loginErr').textContent='';
  try{ await api('/api/sign_in', {password: $('#pwd').value}); init(); }
  catch(e){ $('#loginErr').textContent = e.message; }
};
$('#logoutBtn').onclick = async () => {
  if(!await askConfirm('Выйти из Telegram в этой панели? Потом нужно будет снова войти по коду.', 'Выйти')) return;
  try{ await api('/api/logout', {}); location.reload(); }catch(e){ toast(e.message); }
};

async function loadDialogs(){
  $('#dlgList').innerHTML = '<div class="loader sm"><div class="spinner"></div><span>Загрузка...</span></div>';
  try{ dialogs = await api('/api/dialogs'); renderDialogs(); if(typeof fillChats === 'function' && ST.target) fillChats(); }
  catch(e){ $('#dlgList').innerHTML = '<div class="item" style="color:var(--err)">'+esc(e.message)+'</div>'; }
}
function renderDialogs(){
  const q = $('#search').value.trim().toLowerCase();
  const list = dialogs.filter(d => d.name.toLowerCase().includes(q));
  $('#dlgList').innerHTML = list.length ? list.map(d =>
    `<div class="item ${selected&&selected.id===d.id?'sel':''}" data-id="${d.id}"><span>${esc(d.name)}</span><span class="t">${d.type}</span></div>`
  ).join('') : '<div class="item muted">Ничего не найдено</div>';
}
$('#search').oninput = renderDialogs;
$('#reloadBtn').onclick = loadDialogs;
$('#dlgList').onclick = e => {
  const it = e.target.closest('.item[data-id]'); if(!it) return;
  selected = dialogs.find(d => String(d.id) === it.dataset.id);
  renderDialogs();
  recount();
};
let countSeq = 0;
async function recount(){
  if(!selected) return;
  const seq = ++countSeq, p = period();
  show($('#chosen'));
  $('#chosen').innerHTML = `<b>${esc(selected.name)}</b> <span class="pill">считаю видео…</span>`;
  $('#startBtn').disabled = true;
  if(p.date_from && p.date_to && p.date_from >= p.date_to){
    $('#chosen').innerHTML = `<b>${esc(selected.name)}</b> <span style="color:var(--err)">Дата «с» должна быть раньше даты «по»</span>`;
    return;
  }
  const qs = new URLSearchParams(); if(p.date_from) qs.set('from', p.date_from); if(p.date_to) qs.set('to', p.date_to);
  try{
    const r = await api('/api/count/'+selected.id+'?'+qs);
    if(seq !== countSeq) return;
    const label = p.date_from || p.date_to ? 'видео за период' : 'видео';
    $('#chosen').innerHTML = `<b>${esc(selected.name)}</b> <span class="pill">${label}: ${r.total}</span>`;
    $('#startBtn').disabled = r.total === 0;
  }catch(err){
    if(seq !== countSeq) return;
    $('#chosen').innerHTML = `<b>${esc(selected.name)}</b> <span style="color:var(--err)">${esc(err.message)}</span>`;
  }
}
$('#dateFrom').onchange = recount;
$('#dateTo').onchange = recount;

$('#folder').onchange = () => store('folder', $('#folder').value);
// поле «Подпапка» с выпадающим списком: «Не задано» + существующие папки сервера, можно вписать своё имя
let serverFolders = [], comboItems = [], comboActive = -1;
function renderFolderPick(){
  const cur = $('#folder').value.trim(), has = serverFolders.some(g => g.name === cur);
  show($('#folderArrow'), S.server);
  show($('#folderHint'), S.server);
  $('#folderHint').textContent = !cur ? 'Не задано — папка будет названа сегодняшней датой и названием группы.'
    : has ? 'Папка уже есть на сервере — видео, которые в ней уже лежат, будут пропущены.' : 'Будет создана новая папка.';
}
function comboOpen(filter){
  if(!S.server || $('#folder').disabled) return;
  const q = (filter || '').trim().toLowerCase();
  const list = serverFolders.filter(g => !q || g.name.toLowerCase().includes(q));
  comboItems = [{name:'', label:'Не задано', note:'дата и название группы'}].concat(
    list.map(g => ({name:g.name, label:g.name, note:g.files.length + ' видео'})));
  const cur = $('#folder').value.trim();
  comboActive = Math.max(0, comboItems.findIndex(i => i.name === cur));
  $('#folderList').innerHTML = comboItems.map((it, i) =>
    `<div class="combo-opt ${i===comboActive?'active':''} ${it.name?'':'empty'}" role="option" id="fopt${i}" data-i="${i}" aria-selected="${i===comboActive}"><span>${it.name ? '📁 ' + esc(it.label) : esc(it.label)}</span><span class="t">${esc(it.note)}</span></div>` +
    (i === 0 ? (list.length ? '<div class="combo-sep"></div>' : `<div class="combo-opt empty" style="cursor:default">${serverFolders.length ? 'Нет совпадений — будет новая папка' : 'Папок на сервере пока нет'}</div>`) : '')).join('');
  show($('#folderList'));
  $('#folder').setAttribute('aria-expanded', 'true');
}
function comboClose(){ show($('#folderList'), false); $('#folder').setAttribute('aria-expanded', 'false'); }
function comboPick(i){
  const it = comboItems[i]; if(!it) return;
  $('#folder').value = it.name; store('folder', it.name); comboClose(); renderFolderPick();
}
function comboMove(d){
  if($('#folderList').classList.contains('hidden')){ comboOpen(); return; }
  comboActive = (comboActive + d + comboItems.length) % comboItems.length;
  document.querySelectorAll('#folderList .combo-opt[data-i]').forEach(el => el.classList.toggle('active', +el.dataset.i === comboActive));
  const el = $('#fopt' + comboActive); if(el) el.scrollIntoView({block:'nearest'});
}
$('#folderArrow').onmousedown = e => { e.preventDefault(); $('#folderList').classList.contains('hidden') ? (comboOpen(), $('#folder').focus()) : comboClose(); };
$('#folderList').onmousedown = e => { e.preventDefault(); const o = e.target.closest('.combo-opt[data-i]'); if(o) comboPick(+o.dataset.i); };
$('#folder').oninput = () => { renderFolderPick(); comboOpen($('#folder').value); };
$('#folder').onfocus = () => { if(S.server && !$('#folder').value) comboOpen(); };
$('#folder').onblur = () => setTimeout(comboClose, 120);
$('#folder').onkeydown = e => {
  if(e.key === 'ArrowDown'){ e.preventDefault(); comboMove(1); }
  else if(e.key === 'ArrowUp'){ e.preventDefault(); comboMove(-1); }
  else if(e.key === 'Enter' && !$('#folderList').classList.contains('hidden')){ e.preventDefault(); comboPick(comboActive); }
  else if(e.key === 'Escape') comboClose();
};
function syncExt(){
  const compress = $('#quality').value !== 'original';
  if(compress) $('#ext').value = '.mp4';
  else $('#ext').value = recall('ext') || 'original';
  $('#ext').disabled = compress;
  $('#extHint').textContent = compress
    ? 'При сжатии файл всегда сохраняется в .mp4.'
    : ($('#ext').value === 'original' ? '' : 'Видео не перекодируется — меняется только расширение в имени, качество оригинальное.');
}
$('#quality').onchange = () => { store('quality', $('#quality').value); syncExt(); };
$('#ext').onchange = () => { store('ext', $('#ext').value); syncExt(); };
$('#order').onchange = () => store('order', $('#order').value);
$('#openBtn').onclick = () => api('/api/open_folder', {folder: $('#folder').value}).catch(e => toast(e.message));

$('#startBtn').onclick = async () => {
  if(!selected) return;
  $('#startErr').textContent='';
  try{
    await api('/api/start', {chat_id: selected.id, folder: $('#folder').value, quality: $('#quality').value,
      ext: $('#ext').value, oldest_first: $('#order').value==='old', tz_offset: new Date().getTimezoneOffset(),
      date_from: period().date_from, date_to: period().date_to});
    toast('Задание добавлено' + (JOBS.some(j => j.status === 'running') ? ' в очередь' : ''));
    pollJobs();
  }catch(e){ $('#startErr').textContent = e.message; }
};

// файлы на сервере
const openGroups = new Set();
async function loadFiles(){
  let d;
  try{ d = await api('/api/files'); }catch(e){ $('#filesList').innerHTML = '<div class="err">'+esc(e.message)+'</div>'; return; }
  $('#rootPath').textContent = 'Папка на сервере: ' + d.root;
  const pct = d.disk.total ? d.disk.used / d.disk.total * 100 : 0;
  $('#diskText').textContent = `Свободно ${human(d.disk.free)} из ${human(d.disk.total)}`;
  $('.disk').classList.toggle('low', !!(SPACE && SPACE.warn));
  $('#diskBar').style.width = pct + '%';
  serverFolders = d.groups.filter(g => g.name !== '(корень)');
  renderFolderPick();
  if(!d.groups.length){ $('#filesList').innerHTML = '<p class="muted" style="margin:14px 0 0">Пока пусто.</p>'; return; }
  $('#filesList').innerHTML = d.groups.map(g => `
    <details class="fgroup" data-g="${esc(g.name)}" ${openGroups.has(g.name)?'open':''}>
      <summary><b>📁 ${esc(g.name)}</b>${g.files.length || !g.archived ? `<span class="pill">${g.files.length} · ${human(g.size)}</span>` : ''}
        ${g.archived ? `<span class="pill" title="Видео удалены с сервера после проверки, лежат на Яндекс Диске">☁ в архиве · ${g.archived.count} · ${human(g.archived.size)}</span>` : ''}
        ${g.busy ? '<span class="pill st-running">в работе</span>' : ''}
        <span class="icons">
        ${g.name !== '(корень)' && g.files.length ? ibtn('zip', 'Скачать папку ZIP-архивом', `href="/zip/${encodeURIComponent(g.name)}"`, 'a') : ''}
        ${g.archived && YA.connected ? ibtn('restore', 'Восстановить видео с Яндекс Диска на сервер', `data-restore="${esc(g.name)}"`) : ''}
        ${g.archived && g.archived.link ? ibtn('copy', 'Скопировать ссылку на Диск', `data-copy="${esc(g.archived.link)}"`) : ''}
        ${g.archived && YA.connected ? ibtn('send', 'Отправить ссылку в чат', `data-send="${esc(g.name)}"`) : ''}
        ${g.name !== '(корень)' && g.files.length && YA.connected ? ibtn('cloud', 'Загрузить на Яндекс Диск' + autoSendNote(), `data-ya="${esc(g.name)}"`) : ''}
        ${g.name !== '(корень)' ? ibtn('share', 'Ссылка для просмотра без пароля', `data-share="${esc(g.name)}"`) : ''}
        ${g.name !== '(корень)' ? ibtn('edit', 'Переименовать папку', `data-rename="${esc(g.name)}"`) : ''}
        ${g.name !== '(корень)' ? ibtn('trash', g.archived ? 'Убрать папку из списка' : 'Удалить папку', `data-del="${esc(g.name)}" data-what="${g.archived
          ? `папку «${esc(g.name)}» из списка (видео на Яндекс Диске останутся, но восстановить их кнопкой будет нельзя)` + (g.files.length ? ' вместе с видео на сервере' : '')
          : `папку «${esc(g.name)}» со всеми видео`}"`) : ''}
        </span></summary>
      ${g.files.map(f => `<div class="frow"><span class="n">${esc(f.name)}</span><span class="muted">${human(f.size)}</span>
        <span class="icons">
        ${ibtn('play', 'Смотреть', `href="${fileUrl(f.path)}?inline=1" target="_blank" rel="noopener"`, 'a')}
        ${ibtn('download', 'Скачать', `href="${fileUrl(f.path)}"`, 'a')}
        ${ibtn('trash', 'Удалить', `data-del="${esc(f.path)}" data-what="${esc(f.name)}"`)}
        </span></div>`).join('') || (g.archived ? '' : '<div class="frow muted">Нет видео</div>')}
      ${!g.files.length && g.archived ? '<div class="frow muted">Видео на Яндекс Диске. Чтобы вернуть их на сервер — значок «Восстановить».</div>' : ''}
    </details>`).join('');
}
$('#filesReload').onclick = loadFiles;
$('#filesList').addEventListener('toggle', e => {
  const g = e.target.dataset && e.target.dataset.g; if(!g) return;
  e.target.open ? openGroups.add(g) : openGroups.delete(g);
}, true);
$('#filesList').onclick = async e => {
  const y = e.target.closest('button[data-ya]');
  if(y){
    e.preventDefault();
    try{ await api('/api/ya/upload', {folder: y.dataset.ya}); toast('Задание на загрузку добавлено'); pollJobs(); }catch(err){ toast(err.message); }
    return;
  }
  const b = e.target.closest('button[data-del]'); if(!b) return;
  e.preventDefault();
  if(!await askConfirm('Удалить ' + b.dataset.what + ' с сервера? Это нельзя отменить.', 'Удалить')) return;
  try{ await api('/api/files/delete', {path: b.dataset.del}); loadFiles(); }catch(err){ toast(err.message); }
};

// ---------- Яндекс Диск и настройки ----------
let YA = {connected:false}, ST = {}, devTimer = null;

async function loadSettings(){
  let d;
  try{ d = await api('/api/settings'); }catch(e){ $('#yaErr').textContent = e.message; return; }
  YA = d.ya; ST = d.settings;
  show($('#yaConnected'), YA.connected); show($('#yaConnect'), !YA.connected);
  $('#yaPill').textContent = YA.connected ? (YA.error ? 'ошибка' : 'подключён') : 'не подключён';
  $('#yaTabPill').textContent = YA.connected ? (YA.error ? '☁ !' : '☁ ✓') : '☁ —';
  show($('#yaFoldersCard'), YA.connected);
  if(YA.connected) loadYaFolders();
  $('#yaInfo').innerHTML = YA.error ? '<span style="color:var(--err)">'+esc(YA.error)+'</span>'
    : `<span class="ok">✓ Подключено</span>${YA.user ? ': <b>'+esc(YA.user)+'</b>' : ''} · свободно ${human(YA.total - YA.used)} из ${human(YA.total)}`;
  $('#yaCid').value = YA.client_id || '';
  if(!YA.connected && YA.device && !devPending) showDevice(YA.device);
  $('#yaSecret').placeholder = YA.has_secret ? 'сохранён, можно не вводить' : '';
  $('#stRoot').value = ST.ya_root || '';
  $('#stSend').checked = !!ST.send_link;
  $('#stTarget').value = ST.target || 'source';
  $('#stTpl').value = ST.template || '';
  fillChats();
  $('#stJobs').value = ST.dl_jobs; $('#stTg').value = ST.tg_parallel; $('#stYa').value = ST.ya_parallel;
  $('#stArchive').checked = !!ST.archive; $('#stArchive').disabled = !YA.connected;
  $('#stInfo').checked = ST.info_file !== false;
  $('#stCleanAge').checked = !!ST.clean_age; $('#stCleanDays').value = ST.clean_age_days;
  $('#stCleanSpace').checked = !!ST.clean_space; $('#stCleanGb').value = ST.clean_space_gb; $('#stWarnGb').value = ST.warn_space_gb;
  $('#cleanHint').textContent = YA.connected ? '' : '⚠ Яндекс Диск не подключён — пока он не подключён, очистка ничего не удаляет.';
  loadCleanup();
  $('#stUpdate').checked = ST.update_check !== false;
  $('#stAuto').checked = YA.connected && !!ST.auto_upload;
  $('#stAuto').disabled = !YA.connected;
  $('#stAutoHint').textContent = YA.connected ? '' : 'Доступно после подключения Яндекс Диска.';
  renderAutoStatus();
  loadFiles();
}
function renderAutoStatus(){
  const on = YA.connected && !!ST.auto_upload;
  $('#yaAutoStatus').innerHTML = '☁ Автозагрузка на Яндекс Диск: ' + (on ? '<b class="ok">включена</b>' : '<b>выключена</b>') +
    (YA.connected ? '' : ' (Диск не подключён)') + ' · <a href="#" id="yaAutoLink">настройки</a>';
}
$('#yaAutoStatus').onclick = e => {
  if(e.target.id !== 'yaAutoLink') return;
  e.preventDefault(); setTab('set'); $('#yaSettingsCard').open = true; $('#yaSettingsCard').scrollIntoView({behavior:'smooth'});
};
function fillChats(){
  const cur = ST.target_chat ? String(ST.target_chat) : '';
  const opts = dialogs.map(d => `<option value="${d.id}" ${String(d.id)===cur?'selected':''}>${esc(d.name)}</option>`);
  if(cur && !dialogs.some(d => String(d.id) === cur)) opts.unshift(`<option value="${esc(cur)}" selected>${esc(ST.target_name || cur)}</option>`);
  $('#stChat').innerHTML = '<option value="">— выберите чат —</option>' + opts.join('');
  show($('#stChatWrap'), $('#stTarget').value === 'chat');
}
$('#stTarget').onchange = fillChats;

$('#stSave').onclick = async () => {
  $('#stErr').textContent = ''; $('#stSaved').textContent = '';
  const sel = $('#stChat'), chatName = sel.selectedIndex > 0 ? sel.options[sel.selectedIndex].text : '';
  try{
    const r = await api('/api/settings', {ya_root: $('#stRoot').value, send_link: $('#stSend').checked,
      auto_upload: $('#stAuto').checked && YA.connected, archive: $('#stArchive').checked, info_file: $('#stInfo').checked,
      update_check: $('#stUpdate').checked,
      clean_age: $('#stCleanAge').checked, clean_age_days: $('#stCleanDays').value,
      clean_space: $('#stCleanSpace').checked, clean_space_gb: $('#stCleanGb').value, warn_space_gb: $('#stWarnGb').value,
      dl_jobs: $('#stJobs').value, tg_parallel: $('#stTg').value, ya_parallel: $('#stYa').value,
      target: $('#stTarget').value, target_chat: $('#stTarget').value === 'chat' ? sel.value : null,
      target_name: chatName, template: $('#stTpl').value});
    ST = r.settings; $('#stRoot').value = ST.ya_root; $('#stTpl').value = ST.template; renderAutoStatus();
    $('#stJobs').value = ST.dl_jobs; $('#stTg').value = ST.tg_parallel; $('#stYa').value = ST.ya_parallel;
    $('#stSaved').textContent = '✓ Сохранено'; setTimeout(() => $('#stSaved').textContent = '', 3000);
  }catch(e){ $('#stErr').textContent = e.message; }
};

let devPending = false;
async function copyText(text){
  // navigator.clipboard есть только на https/localhost — панель открыта по http, поэтому есть запасной путь
  if(navigator.clipboard && window.isSecureContext){
    try{ await navigator.clipboard.writeText(text); return true; }catch(e){}
  }
  const t = document.createElement('textarea');
  t.value = text; t.setAttribute('readonly', ''); t.style.cssText = 'position:fixed;top:0;left:0;opacity:0;font-size:16px';
  document.body.appendChild(t); t.focus(); t.select(); t.setSelectionRange(0, text.length);
  let okCopy = false;
  try{ okCopy = document.execCommand('copy'); }catch(e){}
  t.remove();
  return okCopy;
}
$('#yaCopy').onclick = async () => {
  const b = $('#yaCopy'), done = await copyText($('#yaCode').textContent.trim());
  b.textContent = done ? '✓ Скопировано' : 'Не удалось — выделите код вручную';
  setTimeout(() => b.textContent = 'Скопировать', 2500);
};
function showDevice(d){
  $('#yaCode').textContent = d.user_code; $('#yaUrl').href = d.url; $('#yaUrl').textContent = d.url.replace(/^https?:\/\//, '');
  $('#yaWait').textContent = 'Жду подтверждения… Можно спокойно переключиться на Яндекс — панель подключится сама.';
  show($('#yaCodeBox')); devPending = true; clearTimeout(devTimer); devTimer = setTimeout(devTick, 3000);
}
async function devTick(){
  clearTimeout(devTimer);
  if(!devPending) return;
  let r;
  try{ r = await api('/api/ya/device_poll', {}, 15000); }
  catch(e){ $('#yaWait').textContent = 'Связь с панелью прервалась — продолжаю ждать подтверждения…'; devTimer = setTimeout(devTick, 3000); return; }
  if(r.status === 'pending'){ $('#yaWait').textContent = 'Жду подтверждения… Можно спокойно переключиться на Яндекс — панель подключится сама.'; devTimer = setTimeout(devTick, 3000); return; }
  devPending = false; show($('#yaCodeBox'), false);
  if(r.status === 'connected') loadSettings();
  else if(r.status === 'expired') $('#yaErr').textContent = 'Код устарел — нажмите «Подключить» ещё раз.';
  else if(r.status === 'error') $('#yaErr').textContent = r.error || 'Яндекс отклонил подключение';
  else loadSettings();
}
document.addEventListener('visibilitychange', () => { if(!document.hidden && devPending) devTick(); });
$('#yaConnectBtn').onclick = async () => {
  $('#yaErr').textContent = '';
  try{ showDevice(await api('/api/ya/device_start', {client_id: $('#yaCid').value, client_secret: $('#yaSecret').value})); }
  catch(e){ $('#yaErr').textContent = e.message; }
};
$('#yaTokenBtn').onclick = async () => {
  $('#yaErr').textContent = '';
  try{ await api('/api/ya/token', {token: $('#yaToken').value}); $('#yaToken').value = ''; loadSettings(); }
  catch(e){ $('#yaErr').textContent = e.message; }
};
$('#yaLogout').onclick = async () => {
  if(!await askConfirm('Отключить Яндекс Диск от панели? Файлы на Диске останутся.', 'Отключить')) return;
  try{ await api('/api/ya/logout', {}); loadSettings(); }catch(e){ toast(e.message); }
};
// куда автоотправка из настроек отправит ссылку — для подсказок
function autoTargetLabel(){
  if(ST.target === 'saved') return '«Избранное»';
  if(ST.target === 'chat') return '«' + (ST.target_name || 'выбранный чат') + '»';
  return 'группу, откуда скачаны видео';
}
function autoSendNote(){ return ST.send_link ? ' (после загрузки ссылка автоматически уйдёт в ' + autoTargetLabel() + ' — это настраивается в «Настройках»)' : ''; }

// ---------- задания ----------
let JOBS = [], jobsTimer = null, openLogs = new Set(), prevActive = 0;
const KIND = {download: '📥', upload: '☁', restore: '⤓'};
const STATUS = {queued: 'в очереди', running: 'идёт', done: 'готово', error: 'ошибка', stopped: 'остановлено'};
function jobTitle(j){
  if(j.kind === 'download') return `${j.params.chat_name} → ${j.folder}`;
  if(j.kind === 'upload') return `На Яндекс Диск: ${j.folder}`;
  return `Восстановление с Диска: ${j.folder}`;
}
function phaseText(a){
  if(a.phase === 'convert') return 'сжатие ffmpeg…';
  if(a.phase === 'finish') return 'Яндекс обрабатывает файл…';
  if(a.phase === 'download' || a.phase === 'upload'){
    const eta = a.speed > 0 && a.total ? ' · осталось ' + dur((a.total - a.done) / a.speed) : '';
    return `${human(a.done)} / ${human(a.total)} · ${human(a.speed)}/с${eta}`;
  }
  return a.phase;
}
function activeHtml(act){
  return Object.entries(act || {}).map(([n, a]) => `<div class="jact"><span>${esc(n)}</span><span class="muted">${esc(phaseText(a))}</span></div>
    <div class="bar small"><div style="width:${a.total ? Math.min(100, a.done / a.total * 100) : 0}%"></div></div>`).join('');
}
// задание рисуется частями: шапка с кнопками и ссылка перерисовываются только когда меняются,
// иначе ежесекундное обновление съедает щелчки по кнопкам
function jobParts(j){
  const run = j.status === 'running', q = j.status === 'queued';
  const errs = (j.dl ? j.dl.errors : 0) + (j.ya ? j.ya.errors : 0);
  const head = `<span class="jt">${KIND[j.kind] || ''} ${esc(jobTitle(j))}</span>
    <span class="pill st-${j.status}">${STATUS[j.status] || j.status}${j.status === 'done' && errs ? ' · ошибок: ' + errs : ''}</span>
    <span class="muted jtime"></span>
    <span class="icons">${run || q ? ibtn('stop', q ? 'Убрать из очереди' : 'Остановить', `data-jstop="${j.id}" data-queued="${q ? 1 : 0}"`)
                              : ibtn('refresh', 'Запустить снова', `data-jretry="${j.id}"`)}</span>`;
  let body = '';
  if(q) body += `<div class="jsec">Ждёт своей очереди${JOBS.some(x => x.status === 'running' && x.folder === j.folder) ? ' — с этой папкой уже работает другое задание' : ''}</div>`;
  if(j.dl){
    const d = j.dl, done = d.downloaded + d.skipped + d.errors;
    body += `<div class="jsec">Telegram: ${done} из ${d.total} · скачано ${d.downloaded} · уже были ${d.skipped}${d.errors ? ' · <span style="color:var(--err)">ошибок ' + d.errors + '</span>' : ''} · ${human(d.bytes)}</div>
      <div class="bar"><div style="width:${d.total ? Math.min(100, done / d.total * 100) : 0}%"></div></div>${run ? activeHtml(d.active) : ''}`;
  }
  if(j.ya){
    const y = j.ya, done = y.uploaded + y.skipped + y.errors, word = j.kind === 'restore' ? 'восстановлено' : 'загружено';
    body += `<div class="jsec">☁ Яндекс Диск: ${done} из ${y.total} · ${word} ${y.uploaded} · уже были ${y.skipped}${y.errors ? ' · <span style="color:var(--err)">ошибок ' + y.errors + '</span>' : ''} · ${human(y.bytes)}${run && y.waiting ? ' · жду видео из Telegram…' : ''}${y.retry ? ' · ↻ ' + esc(y.retry) : ''}</div>
      <div class="bar"><div style="width:${y.total ? Math.min(100, done / y.total * 100) : 0}%"></div></div>${run ? activeHtml(y.active) : ''}`;
  }
  if(j.error) body += `<div class="err">${esc(j.error)}</div>`;
  const y = j.ya;
  const link = y && y.link ? `<div class="linkrow">Ссылка: <a class="l" href="${esc(y.link)}" target="_blank" rel="noopener">${esc(y.link)}</a>
      ${ibtn('copy', 'Скопировать ссылку', `data-copy="${esc(y.link)}"`)} ${ibtn('send', 'Отправить ссылку в чат…', `data-send="${esc(j.folder)}"`)}
      ${y.sent ? `<span class="ok" style="font-size:13px">отправлена в ${esc(y.sent)}</span>` : ''}
      ${y.archived ? '<span class="muted" style="font-size:13px">· видео удалены с сервера (папка в архиве)</span>' : ''}</div>` : '';
  return {head, body, link};
}
function setHtml(el, h){ if(el._h !== h){ el._h = h; el.innerHTML = h; } }
function renderJobs(){
  const list = $('#jobsList'), ids = new Set(JOBS.map(j => j.id));
  [...list.children].forEach(el => { if(!ids.has(el.dataset.job)) el.remove(); });
  JOBS.forEach((j, i) => {
    let el = document.getElementById('job-' + j.id);
    if(!el){
      el = document.createElement('div'); el.className = 'job'; el.id = 'job-' + j.id; el.dataset.job = j.id;
      el.innerHTML = `<div class="jhead"></div><div class="jbody"></div><div class="jlink"></div>
        <details class="logwrap hidden" data-log="${j.id}"><summary></summary><div class="log" id="jlog-${j.id}" style="height:180px">Загрузка...</div></details>`;
    }
    if(list.children[i] !== el) list.insertBefore(el, list.children[i] || null);
    const p = jobParts(j);
    setHtml(el.querySelector('.jhead'), p.head);
    setHtml(el.querySelector('.jbody'), p.body);
    setHtml(el.querySelector('.jlink'), p.link);
    const t = el.querySelector('.jtime');
    if(t) t.textContent = j.started ? dur((j.finished || Date.now()/1000) - j.started) : '';
    const det = el.querySelector('details');
    show(det, j.log_len > 0);
    det.querySelector('summary').textContent = `Лог (${j.log_len})`;
    if(j.log){
      const b = det.querySelector('.log'), txt = j.log.join('\n');
      if(b.textContent !== txt){
        const atBottom = b.textContent === 'Загрузка...' || b.scrollHeight - b.scrollTop - b.clientHeight < 30;
        b.textContent = txt;
        if(atBottom) b.scrollTop = b.scrollHeight;
      }
    }
  });
}
async function pollJobs(){
  clearTimeout(jobsTimer);
  let d;
  try{ d = await api('/api/jobs?logs=' + [...openLogs].join(',')); }catch(e){ jobsTimer = setTimeout(pollJobs, 4000); return; }
  JOBS = d.jobs;
  renderSpace(d.space);
  if(d.version && d.version !== PAGE_VERSION && $('#reloadBar').classList.contains('hidden')){
    $('#reloadBar').innerHTML = `Панель обновилась до версии ${esc(d.version)} (на этой странице — ${PAGE_VERSION}). <a href="#" onclick="location.reload();return false">Обновить страницу</a>`;
    show($('#reloadBar'));
  }
  const active = JOBS.filter(j => j.status === 'running' || j.status === 'queued').length;
  if(active < prevActive){ if(S.server) loadFiles(); if(YA.connected){ loadSettings(); } }
  prevActive = active;
  show($('#jobsCard'), JOBS.length > 0);
  const running = JOBS.filter(j => j.status === 'running').length, queued = JOBS.filter(j => j.status === 'queued').length;
  $('#jobsHint').textContent = running || queued ? `идёт: ${running} · в очереди: ${queued} · одновременно до ${d.limits.dl_jobs} заданий, ${d.limits.tg_parallel} видео из Telegram, ${d.limits.ya_parallel} файлов на Диск` : 'Все задания завершены';
  show($('#jobsClear'), JOBS.some(j => j.status !== 'running' && j.status !== 'queued'));
  renderJobs();
  jobsTimer = setTimeout(pollJobs, active ? 1000 : 5000);
}
$('#jobsList').addEventListener('toggle', e => {
  const id = e.target.dataset && e.target.dataset.log; if(!id) return;
  if(e.target.open){ if(!openLogs.has(id)){ openLogs.add(id); pollJobs(); } } else openLogs.delete(id);
}, true);
$('#jobsClear').onclick = async () => { try{ await api('/api/jobs/clear', {}); pollJobs(); }catch(e){ toast(e.message); } };

// ---------- ссылки для просмотра ----------
let shareFolder = null;
function openShare(folder){
  shareFolder = folder; $('#shareTitle').textContent = 'Ссылка для просмотра: «' + folder + '»';
  $('#shareErr').textContent = ''; show($('#shareModal')); loadShares();
}
async function loadShares(){
  let d; try{ d = await api('/api/shares?folder=' + encodeURIComponent(shareFolder)); }catch(e){ $('#shareList').textContent = e.message; return; }
  $('#shareList').innerHTML = d.shares.length ? d.shares.map(x => `<div class="linkrow" style="margin:0 0 10px">
      <a class="l" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a>
      ${ibtn('copy', 'Скопировать ссылку', `data-copy="${esc(x.url)}"`)} ${ibtn('trash', 'Отозвать ссылку', `data-revoke="${esc(x.token)}"`)}
      <span class="muted" style="font-size:12px;width:100%">${x.expires ? 'действует до ' + new Date(x.expires * 1000).toLocaleString() : 'бессрочно'} · просмотров: ${x.views || 0}</span></div>`).join('')
    : 'Пока нет — создайте ссылку выше.';
}
$('#shareCreate').onclick = async () => {
  $('#shareErr').textContent = '';
  try{
    const x = await api('/api/shares', {folder: shareFolder, days: +$('#shareDays').value});
    toast(await copyText(x.url) ? 'Ссылка создана и скопирована' : 'Ссылка создана');
    loadShares();
  }catch(e){ $('#shareErr').textContent = e.message; }
};
$('#shareClose').onclick = () => show($('#shareModal'), false);
$('#shareModal').onclick = e => { if(e.target.id === 'shareModal') show($('#shareModal'), false); };

// ---------- резервная копия ----------
$('#bkBtn').onclick = async () => {
  $('#bkErr').textContent = '';
  const p1 = $('#bkPwd').value, p2 = $('#bkPwd2').value;
  if(p1.length < 8){ $('#bkErr').textContent = 'Пароль копии — не короче 8 символов'; return; }
  if(p1 !== p2){ $('#bkErr').textContent = 'Пароли не совпадают'; return; }
  const b = $('#bkBtn'); b.disabled = true; b.textContent = 'Шифрую…';
  try{
    const r = await fetch('/api/backup', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: p1})});
    if(!r.ok){ const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Ошибка ' + r.status); }
    const blob = await r.blob(), name = (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name ? name[1] : 'tg-video-panel-backup.tvpbak';
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    $('#bkPwd').value = $('#bkPwd2').value = '';
    toast('Резервная копия скачана');
  }catch(e){ $('#bkErr').textContent = e.message; }
  b.disabled = false; b.textContent = 'Скачать копию';
};
async function doRestore(p){
  $('#' + p + 'Err').textContent = '';
  const f = $('#' + p + 'File').files[0], pwd = $('#' + p + 'Pwd').value;
  if(!f){ $('#' + p + 'Err').textContent = 'Выберите файл .tvpbak'; return; }
  if(!pwd){ $('#' + p + 'Err').textContent = 'Введите пароль копии'; return; }
  if(!await askConfirm('Восстановить из копии? Текущие ключи Telegram, вход в Telegram, подключение Диска и настройки будут заменены.', 'Восстановить')) return;
  const b = $('#' + p + 'Btn'); b.disabled = true; b.textContent = 'Восстанавливаю…';
  try{
    const fd = new FormData(); fd.append('file', f); fd.append('password', pwd);
    const r = await fetch('/api/backup/restore', {method: 'POST', credentials: 'same-origin', body: fd});
    const d = await r.json().catch(() => ({}));
    if(!r.ok) throw new Error(d.detail || 'Ошибка ' + r.status);
    toast('Восстановлено' + (d.authorized ? ': Telegram — ' + (d.me || 'подключён') : '') + (d.yandex ? ', Яндекс Диск подключён' : ''));
    setTimeout(() => location.reload(), 1500);
  }catch(e){ $('#' + p + 'Err').textContent = e.message; }
  b.disabled = false; b.textContent = 'Восстановить';
}
$('#rs1Btn').onclick = () => doRestore('rs1');
$('#rs2Btn').onclick = () => doRestore('rs2');

// ---------- обновления ----------
let UPD = null;
function updCommand(mode){
  if(mode === 'docker') return 'cd /opt/tg-video-panel && sudo git pull && sudo docker compose up -d --build';
  if(mode === 'server') return 'cd /opt/tg-video-panel && sudo git pull && sudo bash install.sh --ip';
  return 'git pull   # в папке панели, затем перезапустите её';
}
function notesHtml(md){  // простой показ описания релиза: заголовки ###, списки «- », абзацы
  let html = '', inList = false;
  const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
  md.split('\n').forEach(line => {
    const l = line.trimEnd();
    if(/^\s*[-*] /.test(l)){ if(!inList){ html += '<ul>'; inList = true; } html += '<li>' + inline(l.replace(/^\s*[-*] /, '')) + '</li>'; return; }
    if(/^\s{2,}\S/.test(l) && inList){ html = html.replace(/<\/li>$/, ' ' + inline(l.trim()) + '</li>'); return; }
    if(inList){ html += '</ul>'; inList = false; }
    if(/^#{1,6} /.test(l)) html += '<h4>' + inline(l.replace(/^#{1,6} /, '')) + '</h4>';
    else if(l.trim()) html += '<p style="margin:6px 0">' + inline(l) + '</p>';
  });
  return html + (inList ? '</ul>' : '');
}
function renderUpdate(u){
  UPD = u;
  const b = $('#updBadge');
  show(b, !!(u && u.newer && u.enabled));
  if(u && u.newer) b.textContent = 'Доступна ' + u.latest;
  $('#updState').textContent = !u ? '' : u.error ? 'Не удалось проверить: ' + u.error
    : !u.checked ? 'Ещё не проверялось' : u.newer ? `Доступна версия ${u.latest} (у вас ${u.current})`
    : `У вас последняя версия ${u.current} · проверено ${new Date(u.checked * 1000).toLocaleString()}`;
}
async function loadUpdate(){ try{ renderUpdate(await api('/api/update')); }catch(e){} setTimeout(loadUpdate, 30 * 60 * 1000); }
$('#updBadge').onclick = () => {
  const u = UPD; if(!u) return;
  $('#updTitle').textContent = 'Доступна версия ' + u.latest;
  $('#updSub').textContent = 'У вас ' + u.current + '. Что нового:';
  $('#updNotes').innerHTML = u.releases.map(r => `<h4>${esc(r.name)}${r.date ? ' · ' + esc(r.date) : ''}</h4>${notesHtml(r.notes)}`).join('');
  $('#updCmd').textContent = updCommand(u.mode);
  $('#updNote').textContent = u.mode === 'docker' ? 'Панель перезапустится примерно на минуту; идущие задания продолжатся сами.'
    : u.mode === 'server' ? 'Если ставили с доменом или без --ip — укажите тот же режим, что при установке. Идущие задания продолжатся сами.' : '';
  $('#updLink').href = (u.releases[0] && u.releases[0].url) || ('https://github.com/' + u.repo + '/releases');
  show($('#updModal'));
};
$('#updClose').onclick = () => show($('#updModal'), false);
$('#updModal').onclick = e => { if(e.target.id === 'updModal') show($('#updModal'), false); };
$('#updCopy').onclick = async () => toast(await copyText($('#updCmd').textContent) ? 'Команда скопирована' : 'Не удалось скопировать — выделите вручную');
$('#updCheckNow').onclick = async () => {
  $('#updState').textContent = 'Проверяю…';
  try{ renderUpdate(await api('/api/update/check', {})); }catch(e){ $('#updState').textContent = e.message; }
};

// ---------- место на сервере и очистка ----------
let SPACE = null;
function renderSpace(sp){
  SPACE = sp;
  const w = $('#spaceWarn');
  show(w, !!(sp && sp.warn));
  if(sp && sp.warn) setHtml(w, `⚠ На сервере осталось <b>${human(sp.free)}</b> свободного места (порог предупреждения — ${sp.warn_gb} ГБ).
    Новые видео могут не поместиться. Включите автоочистку или удалите ненужное — <a href="#" data-goset="1">настройки очистки</a>.`);
  const dk = document.querySelector('.disk'); if(dk) dk.classList.toggle('low', !!(sp && sp.warn));
}
$('#spaceWarn').onclick = e => { if(e.target.dataset.goset){ e.preventDefault(); setTab('set'); $('#cleanCard').open = true; $('#cleanCard').scrollIntoView({behavior:'smooth'}); } };
// блоки настроек сворачиваются; какие раскрыты — помним
(function(){
  let open = [];
  try{ open = JSON.parse(recall('openBlocks') || '[]'); }catch(e){}
  document.querySelectorAll('details.sblock').forEach(d => { d.open = open.includes(d.id); });
  $('#tabSet').addEventListener('toggle', e => {
    if(!e.target.classList || !e.target.classList.contains('sblock')) return;
    store('openBlocks', JSON.stringify([...document.querySelectorAll('details.sblock')].filter(d => d.open).map(d => d.id)));
  }, true);
})();
async function loadCleanup(){
  try{
    const c = await api('/api/cleanup');
    $('#cleanLog').textContent = c.log.join('\n') || 'Пока ничего не удалялось.';
    $('#cleanRes').textContent = c.last ? `Последняя проверка: ${new Date(c.last * 1000).toLocaleString()}` + (c.deleted ? ` · удалено ${c.deleted} видео, ${human(c.freed)}` : '') : '';
    renderSpace(c.space);
  }catch(e){}
}
$('#cleanNow').onclick = async () => {
  const b = $('#cleanNow'); b.disabled = true; $('#cleanRes').textContent = 'Проверяю…';
  try{
    const r = await api('/api/cleanup/run', {});
    $('#cleanRes').textContent = r.skipped ? r.skipped : r.deleted ? `Удалено ${r.deleted} видео, освобождено ${human(r.freed)}` : 'Удалять нечего — правила не сработали';
    $('#cleanLog').textContent = (r.log || []).join('\n') || 'Пока ничего не удалялось.';
    renderSpace(r.space); if(S.server) loadFiles();
  }catch(e){ $('#cleanRes').textContent = e.message; }
  b.disabled = false;
};

// ---------- вкладки ----------
let currentTab = 'tg';
function setTab(t){
  currentTab = t === 'set' ? 'set' : 'tg';
  show($('#tabTg'), currentTab === 'tg'); show($('#tabSet'), currentTab === 'set');
  document.querySelectorAll('.tab').forEach(b => { b.classList.toggle('active', b.dataset.tab === currentTab); b.setAttribute('aria-selected', b.dataset.tab === currentTab); });
  store('tab', currentTab);
  if(currentTab === 'tg' && YA.connected) loadYaFolders();
}
$('#tabs').onclick = e => { const b = e.target.closest('.tab'); if(b) setTab(b.dataset.tab); };

function toast(msg){
  const t = $('#toast'); t.textContent = msg; show(t);
  clearTimeout(toast.timer); toast.timer = setTimeout(() => show(t, false), 3500);
}

// ---------- папки на Яндекс Диске ----------
let yaFolderList = [];
async function loadYaFolders(){
  let d;
  try{ d = await api('/api/ya/folders'); }
  catch(e){ $('#yaFolders').innerHTML = '<div class="err">' + esc(e.message) + '</div>'; return; }
  yaFolderList = d.folders;
  $('#yaRootHint').textContent = 'Папка на Диске: ' + d.root;
  $('#yaFolders').innerHTML = d.folders.length ? d.folders.map(f => `
    <div class="yrow">
      <div class="nm"><b>📁 ${esc(f.name)}</b>
        <div class="meta">${f.count} видео · ${human(f.size)}${f.archived ? ' · на сервере — в архиве' : f.local ? '' : ' · на сервере удалена'}${f.uploading ? ' · <span class="ok">в работе…</span>' : ''}</div>
        ${f.link ? `<a href="${esc(f.link)}" target="_blank" rel="noopener">${esc(f.link)}</a>` : '<span class="meta">ссылка ещё не создана</span>'}</div>
      <span class="icons">
        ${f.link ? ibtn('copy', 'Скопировать ссылку', `data-copy="${esc(f.link)}"`) : ibtn('link', 'Создать ссылку', `data-publish="${esc(f.name)}"`)}
        ${ibtn('send', 'Отправить ссылку в чат', `data-send="${esc(f.name)}"`)}
        ${ibtn('edit', 'Переименовать папку', `data-rename="${esc(f.name)}"`)}
        ${ibtn('trash', 'Удалить папку с Яндекс Диска', `data-yadel="${esc(f.name)}"`)}
      </span>
    </div>`).join('') : '<p class="muted" style="margin:10px 0 0">На Диске пока нет папок.</p>';
}
$('#yaFoldersReload').onclick = loadYaFolders;

document.addEventListener('click', async e => {
  const c = e.target.closest('[data-copy]');
  if(c){ e.preventDefault(); toast(await copyText(c.dataset.copy) ? 'Ссылка скопирована' : 'Не удалось скопировать — выделите ссылку вручную'); return; }
  const rs = e.target.closest('[data-restore]');
  if(rs){ e.preventDefault(); try{ await api('/api/restore', {folder: rs.dataset.restore}); toast('Задание на восстановление добавлено'); pollJobs(); }catch(err){ toast(err.message); } return; }
  const js = e.target.closest('[data-jstop]');
  if(js){ e.preventDefault(); const q = js.dataset.queued === '1';
    if(!q && !await askConfirm('Остановить задание? Уже скачанное и загруженное сохранится, потом можно запустить снова.', 'Остановить')) return;
    try{ await api('/api/jobs/stop', {id: js.dataset.jstop}); pollJobs(); }catch(err){ toast(err.message); } return; }
  const jr = e.target.closest('[data-jretry]');
  if(jr){ e.preventDefault(); try{ await api('/api/jobs/retry', {id: jr.dataset.jretry}); toast('Задание добавлено снова'); pollJobs(); }catch(err){ toast(err.message); } return; }
  const sh = e.target.closest('[data-share]');
  if(sh){ e.preventDefault(); openShare(sh.dataset.share); return; }
  const sr = e.target.closest('[data-revoke]');
  if(sr){ e.preventDefault();
    if(!await askConfirm('Отозвать ссылку? Она сразу перестанет открываться.', 'Отозвать')) return;
    try{ await api('/api/shares/revoke', {token: sr.dataset.revoke}); toast('Ссылка отозвана'); loadShares(); }catch(err){ toast(err.message); } return; }
  const rn = e.target.closest('[data-rename]');
  if(rn){ e.preventDefault(); openRename(rn.dataset.rename); return; }
  const s = e.target.closest('[data-send]');
  if(s){ e.preventDefault(); openSend(s.dataset.send); return; }
  const p = e.target.closest('[data-publish]');
  if(p){
    e.preventDefault(); p.disabled = true;
    try{ await api('/api/ya/publish', {folder: p.dataset.publish}); loadYaFolders(); }catch(err){ toast(err.message); p.disabled = false; }
    return;
  }
  const d = e.target.closest('[data-yadel]');
  if(d){
    e.preventDefault();
    const fa = yaFolderList.find(x => x.name === d.dataset.yadel);
    if(!await askConfirm(`Удалить папку «${d.dataset.yadel}» с Яндекс Диска? Она попадёт в Корзину Яндекс Диска, видео на сервере останутся.` +
      (fa && fa.archived ? ' Внимание: на сервере эта папка в архиве — видео есть только на Диске, после удаления восстановить их кнопкой будет нельзя.' : ''), 'Удалить')) return;
    d.disabled = true;
    try{ await api('/api/ya/delete', {folder: d.dataset.yadel}); toast('Папка перемещена в Корзину Яндекс Диска'); loadYaFolders(); }
    catch(err){ toast(err.message); d.disabled = false; }
  }
});

// ---------- отправка ссылки в выбранный чат ----------
let sendFolder = null;
function openSend(folder){
  sendFolder = folder;
  $('#sendWhat').textContent = 'Папка «' + folder + '». Выберите, куда отправить ссылку. Текст — по шаблону из настроек.';
  $('#sendSearch').value = ''; $('#sendErr').textContent = '';
  renderSendList(); show($('#sendModal')); $('#sendSearch').focus();
}
function renderSendList(){
  const q = $('#sendSearch').value.trim().toLowerCase();
  const f = yaFolderList.find(x => x.name === sendFolder);
  const sc = f && f.source_chat ? f.source_chat : (serverFolders.find(g => g.name === sendFolder) || {}).source_chat;
  const src = sc ? dialogs.find(d => d.id === sc) : null;
  const items = [{mode:'saved', name:'⭐ Избранное', t:'только вам'}];
  if(src) items.push({mode:'source', name:'↩ ' + src.name, t:'группа, откуда видео'});
  dialogs.forEach(d => items.push({mode:'chat', id:d.id, name:d.name, t:d.type}));
  const isCfg = i => ST.target === 'saved' ? i.mode === 'saved' : ST.target === 'source' ? i.mode === 'source'
    : ST.target === 'chat' && i.mode === 'chat' && String(i.id) === String(ST.target_chat);
  items.forEach(i => { if(isCfg(i)){ i.t += ' · из настроек'; i.cfg = true; } });
  items.sort((a, b) => (b.cfg ? 1 : 0) - (a.cfg ? 1 : 0));
  const list = items.filter(i => !q || i.name.toLowerCase().includes(q));
  $('#sendList').innerHTML = list.length ? list.map(i =>
    `<div class="item" data-mode="${i.mode}" data-id="${i.id || ''}"><span>${esc(i.name)}</span><span class="t">${esc(i.t)}</span></div>`).join('')
    : '<div class="item muted">Ничего не найдено</div>';
}
$('#sendSearch').oninput = renderSendList;
$('#sendCancel').onclick = () => show($('#sendModal'), false);
$('#sendModal').onclick = e => { if(e.target.id === 'sendModal') show($('#sendModal'), false); };
document.addEventListener('keydown', e => { if(e.key === 'Escape' && !$('#sendModal').classList.contains('hidden')) show($('#sendModal'), false); });
$('#sendList').onclick = async e => {
  const it = e.target.closest('.item[data-mode]'); if(!it) return;
  $('#sendErr').textContent = ''; it.style.opacity = .5;
  try{
    const r = await api('/api/ya/send', {folder: sendFolder, mode: it.dataset.mode, chat_id: it.dataset.id || null});
    show($('#sendModal'), false); toast('Ссылка отправлена в ' + r.sent);
  }catch(err){ $('#sendErr').textContent = err.message; it.style.opacity = 1; }
};

// ---------- переименование папки ----------
let renameFrom = null;
function openRename(name){
  renameFrom = name;
  $('#renameWhat').textContent = 'Сейчас: «' + name + '»';
  $('#renameInput').value = name; $('#renameErr').textContent = '';
  $('#renameServer').checked = true; $('#renameDisk').checked = YA.connected;
  show($('#renameServerWrap'), S.server); show($('#renameDiskWrap'), YA.connected);
  show($('#renameModal')); $('#renameInput').focus(); $('#renameInput').select();
}
$('#renameCancel').onclick = () => show($('#renameModal'), false);
$('#renameModal').onclick = e => { if(e.target.id === 'renameModal') show($('#renameModal'), false); };
document.addEventListener('keydown', e => { if(e.key === 'Escape' && !$('#renameModal').classList.contains('hidden')) show($('#renameModal'), false); });
$('#renameForm').onsubmit = async e => {
  e.preventDefault(); $('#renameErr').textContent = ''; $('#renameOk').disabled = true;
  const to = $('#renameInput').value.trim();
  try{
    const r = await api('/api/rename', {folder: renameFrom, new: to, server: S.server && $('#renameServer').checked,
                                        disk: YA.connected && $('#renameDisk').checked});
    show($('#renameModal'), false);
    if($('#folder').value.trim() === renameFrom){ $('#folder').value = to; store('folder', to); }
    if(openGroups.has(renameFrom)){ openGroups.delete(renameFrom); openGroups.add(to); }
    const parts = [];
    if(r.server === 'переименована') parts.push('на сервере');
    if(r.disk === 'переименована') parts.push('на Яндекс Диске');
    toast('Папка переименована ' + parts.join(' и '));
    if(S.server) loadFiles();
    if(YA.connected) loadYaFolders();
  }catch(err){ $('#renameErr').textContent = err.message; }
  $('#renameOk').disabled = false;
};

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if SERVER and not PASSWORD:
        raise SystemExit("В серверном режиме задайте пароль панели: TVP_PASSWORD=...")
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}"
    print(f"Telegram Video Panel v{VERSION}: {url}  (Ctrl+C — остановить)")
    if not SERVER:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info",
                proxy_headers=True, forwarded_allow_ips=os.environ.get("TVP_FORWARDED_ALLOW_IPS", "127.0.0.1"))
