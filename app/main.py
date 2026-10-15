import os
import uuid
import shutil
import threading
import time
from pathlib import Path, PurePosixPath
from ftplib import FTP, error_perm
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="FtpKDV")
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"
# Credentials are held in process memory only; restarting the container clears sessions.
sessions: dict[str, dict] = {}
transfer_tasks: dict[str, dict] = {}


class ConnectRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = Field(default="anonymous", max_length=255)
    password: str = Field(default="", max_length=1024)
    passive: bool = True


class PathRequest(BaseModel):
    path: str = "/"


class ItemRequest(BaseModel):
    path: str
    name: str = ""


class RenameRequest(BaseModel):
    path: str
    new_name: str


class TransferRequest(BaseModel):
    local_path: str
    remote_path: str


def local_path(user_path: str) -> Path:
    candidate = (WORKSPACE / user_path.lstrip("/")).resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise HTTPException(400, "Недопустимый локальный путь")
    return candidate


def get_ftp(session_id: str) -> FTP:
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(401, "FTP не подключён")
    ftp = s["ftp"]
    try:
        ftp.voidcmd("NOOP")
    except Exception:
        try:
            ftp.connect(s["host"], s["port"], timeout=12)
            ftp.login(s["username"], s["password"])
            ftp.set_pasv(s["passive"])
            s["ftp"] = ftp
        except Exception as e:
            sessions.pop(session_id, None)
            raise HTTPException(502, f"Соединение потеряно: {e}")
    return s["ftp"]


def ftp_list(ftp: FTP, path: str):
    try:
        ftp.cwd(path or "/")
        rows = []
        try:
            for name, facts in ftp.mlsd():
                if name in (".", ".."):
                    continue
                typ = facts.get("type", "file")
                if typ == "cdir":
                    continue
                rows.append({
                    "name": name,
                    "type": "dir" if typ == "dir" else "file",
                    "size": int(facts.get("size", 0) or 0),
                    "modified": facts.get("modify", ""),
                })
            return sorted(rows, key=lambda x: (x["type"] != "dir", x["name"].lower()))
        except Exception:
            names = ftp.nlst()
            rows = []
            for raw in names:
                name = PurePosixPath(raw.rstrip("/")).name or raw
                if name in (".", ".."):
                    continue
                typ = "file"
                try:
                    here = ftp.pwd()
                    ftp.cwd(name)
                    ftp.cwd(here)
                    typ = "dir"
                except Exception:
                    pass
                rows.append({"name": name, "type": typ, "size": 0, "modified": ""})
            return sorted(rows, key=lambda x: (x["type"] != "dir", x["name"].lower()))
    except Exception as e:
        raise HTTPException(502, f"Не удалось прочитать каталог: {e}")


@app.get("/api/status")
def status():
    return {"ok": True, "ftp_connected": False}


@app.get("/api/local/list")
def local_list(path: str = "/"):
    p = local_path(path)
    if not p.exists():
        raise HTTPException(404, "Каталог не найден")
    if not p.is_dir():
        raise HTTPException(400, "Это не каталог")
    items = []
    for x in p.iterdir():
        try:
            st = x.stat()
            items.append({"name": x.name, "type": "dir" if x.is_dir() else "file",
                          "size": st.st_size if x.is_file() else 0,
                          "modified": str(int(st.st_mtime))})
        except OSError:
            continue
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {"path": "/" + str(p.relative_to(WORKSPACE)).replace("\\", "/").strip("./"),
            "items": items}


@app.post("/api/ftp/connect")
@app.post("/api/ftp/connect/{panel}")
def connect(req: ConnectRequest, panel: str = "right"):
    ftp = FTP()
    try:
        ftp.connect(req.host, req.port, timeout=12)
        ftp.login(req.username, req.password)
        ftp.set_pasv(req.passive)
        pwd = ftp.pwd()
        sid = str(uuid.uuid4())
        sessions[sid] = {"ftp": ftp, "host": req.host, "port": req.port,
                         "username": req.username, "password": req.password,
                         "passive": req.passive}
        return {"session_id": sid, "path": pwd, "items": ftp_list(ftp, pwd), "panel": panel}
    except Exception as e:
        try: ftp.close()
        except Exception: pass
        raise HTTPException(502, f"Не удалось подключиться: {e}")


@app.post("/api/ftp/{session_id}/disconnect")
def disconnect(session_id: str):
    s = sessions.pop(session_id, None)
    if s:
        try: s["ftp"].quit()
        except Exception: pass
    return {"ok": True}


@app.get("/api/ftp/{session_id}/list")
def remote_list(session_id: str, path: str = "/"):
    ftp = get_ftp(session_id)
    try:
        ftp.cwd(path)
        actual = ftp.pwd()
        return {"path": actual, "items": ftp_list(ftp, actual)}
    except Exception as e:
        raise HTTPException(502, f"Не удалось открыть каталог: {e}")


@app.post("/api/local/mkdir")
def local_mkdir(req: ItemRequest):
    parent = local_path(req.path)
    name = req.name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "Некорректное имя папки")
    (parent / name).mkdir()
    return {"ok": True}


@app.post("/api/ftp/{session_id}/mkdir")
def remote_mkdir(session_id: str, req: ItemRequest):
    ftp = get_ftp(session_id)
    try:
        ftp.cwd(req.path)
        ftp.mkd(req.name)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Не удалось создать папку: {e}")


@app.post("/api/local/rename")
def local_rename(req: RenameRequest):
    src = local_path(req.path)
    name = req.new_name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "Некорректное имя")
    dst = src.with_name(name)
    if dst.parent != src.parent:
        raise HTTPException(400, "Недопустимый путь")
    src.rename(dst)
    return {"ok": True}


@app.post("/api/ftp/{session_id}/rename")
def remote_rename(session_id: str, req: RenameRequest):
    ftp = get_ftp(session_id)
    try:
        ftp.cwd(str(PurePosixPath(req.path).parent))
        ftp.rename(PurePosixPath(req.path).name, req.new_name)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Не удалось переименовать: {e}")


@app.post("/api/local/delete")
def local_delete(req: PathRequest):
    p = local_path(req.path)
    if p == WORKSPACE:
        raise HTTPException(400, "Нельзя удалить корень рабочей папки")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    else:
        raise HTTPException(404, "Файл не найден")
    return {"ok": True}


@app.post("/api/ftp/{session_id}/delete")
def remote_delete(session_id: str, req: PathRequest):
    ftp = get_ftp(session_id)
    try:
        p = PurePosixPath(req.path)
        ftp.cwd(str(p.parent))
        try:
            ftp.delete(p.name)
        except error_perm:
            ftp.rmd(p.name)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Не удалось удалить: {e}")


@app.post("/api/transfer/download/{session_id}")
def download_remote(session_id: str, req: TransferRequest):
    ftp = get_ftp(session_id)
    dest = local_path(req.local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as f:
            ftp.cwd(str(PurePosixPath(req.remote_path).parent))
            ftp.retrbinary(f"RETR {PurePosixPath(req.remote_path).name}", f.write)
        return {"ok": True, "path": str(dest.relative_to(WORKSPACE))}
    except Exception as e:
        try: dest.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(502, f"Скачивание не удалось: {e}")


@app.post("/api/transfer/upload/{session_id}")
def upload_local(session_id: str, req: TransferRequest):
    ftp = get_ftp(session_id)
    src = local_path(req.local_path)
    if not src.is_file():
        raise HTTPException(400, "Выберите локальный файл")
    try:
        ftp.cwd(str(PurePosixPath(req.remote_path).parent))
        with src.open("rb") as f:
            ftp.storbinary(f"STOR {PurePosixPath(req.remote_path).name}", f)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Загрузка не удалась: {e}")



class FTPCopyRequest(BaseModel):
    source_path: str
    destination_session: str
    destination_path: str


@app.post("/api/transfer/ftp-copy/{source_session}")
def ftp_copy(source_session: str, req: FTPCopyRequest):
    # Run the transfer in a worker thread so the browser can poll progress.
    if source_session == req.destination_session:
        raise HTTPException(400, "Источник и назначение должны быть разными FTP-соединениями")
    if source_session not in sessions or req.destination_session not in sessions:
        raise HTTPException(401, "Подключите оба FTP-сервера заново")

    task_id = str(uuid.uuid4())
    transfer_tasks[task_id] = {
        "id": task_id, "status": "queued", "percent": 0,
        "speed": 0, "bytes_done": 0, "total_bytes": 0,
        "file": PurePosixPath(req.source_path).name,
        "phase": "Подготовка", "error": None, "started_at": time.time(),
    }

    def worker():
        task = transfer_tasks[task_id]
        source = dest = None
        temp_path = None
        try:
            source = get_ftp(source_session)
            dest = get_ftp(req.destination_session)
            src_path = PurePosixPath(req.source_path)
            dst_path = PurePosixPath(req.destination_path)
            source.cwd(str(src_path.parent))
            try:
                total = int(source.size(src_path.name) or 0)
            except Exception:
                total = 0
            task.update(status="running", phase="Скачивание с исходного FTP", total_bytes=total)
            import tempfile
            fd, temp_path = tempfile.mkstemp(prefix="ftpkdv-transfer-")
            os.close(fd)
            downloaded = 0
            phase_started = time.time()
            with open(temp_path, "wb") as stream:
                def on_download(chunk):
                    nonlocal downloaded
                    stream.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.time() - phase_started, 0.001)
                    task.update(bytes_done=downloaded, speed=int(downloaded / elapsed),
                                percent=int(downloaded * 50 / total) if total else 0)
                source.retrbinary(f"RETR {src_path.name}", on_download, blocksize=64 * 1024)

            dest.cwd(str(dst_path.parent))
            task.update(phase="Загрузка на целевой FTP", bytes_done=0, speed=0)
            uploaded = 0
            phase_started = time.time()
            with open(temp_path, "rb") as stream:
                def on_upload(block):
                    nonlocal uploaded
                    uploaded += len(block)
                    elapsed = max(time.time() - phase_started, 0.001)
                    task.update(bytes_done=uploaded, speed=int(uploaded / elapsed),
                                percent=50 + int(uploaded * 50 / total) if total else 50)
                dest.storbinary(f"STOR {dst_path.name}", stream, blocksize=64 * 1024, callback=on_upload)
            task.update(status="completed", phase="Копирование завершено", percent=100,
                        speed=0, bytes_done=total, total_bytes=total)
        except Exception as e:
            task.update(status="error", phase="Ошибка копирования", error=str(e), speed=0)
        finally:
            if temp_path:
                try: os.unlink(temp_path)
                except OSError: pass

    threading.Thread(target=worker, daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/transfer/status/{task_id}")
def transfer_status(task_id: str):
    task = transfer_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Задача копирования не найдена")
    return dict(task)


@app.get("/api/local/file")
def local_file(path: str):
    p = local_path(path)
    if not p.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(p, filename=p.name)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
