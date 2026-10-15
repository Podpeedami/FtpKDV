import os
import uuid
import shutil
import threading
from collections import deque
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


def start_transfer_task(file_name: str, phase: str, worker):
    task_id = str(uuid.uuid4())
    transfer_tasks[task_id] = {
        "id": task_id, "status": "queued", "percent": None,
        "speed": 0, "bytes_done": 0, "total_bytes": 0,
        "progress_known": False, "file": file_name,
        "phase": "Подготовка", "error": None, "started_at": time.time(),
    }

    def run():
        task = transfer_tasks[task_id]
        task.update(status="running", phase=phase)
        try:
            worker(task)
            task.update(status="completed", phase="Передача завершена",
                        percent=100, speed=0, progress_known=True)
        except Exception as e:
            task.update(status="error", phase="Ошибка передачи",
                        error=str(e), speed=0)

    threading.Thread(target=run, daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/transfer/download/{session_id}")
def download_remote(session_id: str, req: TransferRequest):
    dest = local_path(req.local_path)
    remote = PurePosixPath(req.remote_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def worker(task):
        ftp = get_ftp(session_id)
        ftp.cwd(str(remote.parent))
        try:
            value = ftp.size(remote.name)
            total = int(value) if value is not None and int(value) > 0 else 0
        except Exception:
            total = 0

        task.update(total_bytes=total, progress_known=total > 0,
                    percent=0 if total > 0 else None)
        samples = deque()
        done = 0

        def on_download(block):
            nonlocal done
            with dest.open("ab") as stream:
                stream.write(block)
            done += len(block)
            now = time.time()
            samples.append((now, len(block)))
            while len(samples) > 1 and now - samples[0][0] > 5:
                samples.popleft()
            elapsed = samples[-1][0] - samples[0][0]
            speed = int(sum(x[1] for x in samples) / max(elapsed, 1.0))
            task.update(bytes_done=done, speed=speed,
                        percent=min(99, int(done * 100 / total)) if total else None)

        try:
            dest.write_bytes(b"")
            ftp.retrbinary(f"RETR {remote.name}", on_download, blocksize=64 * 1024)
            task.update(bytes_done=done, total_bytes=total or done,
                        percent=100, progress_known=True, speed=0)
        except Exception:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    return start_transfer_task(remote.name, "Скачивание с FTP", worker)


@app.post("/api/transfer/upload/{session_id}")
def upload_local(session_id: str, req: TransferRequest):
    src = local_path(req.local_path)
    if not src.is_file():
        raise HTTPException(400, "Выберите локальный файл")
    remote = PurePosixPath(req.remote_path)
    total = src.stat().st_size

    def worker(task):
        ftp = get_ftp(session_id)
        ftp.cwd(str(remote.parent))
        task.update(total_bytes=total, progress_known=total > 0,
                    percent=0 if total > 0 else None)
        samples = deque()
        done = 0

        def on_upload(block):
            nonlocal done
            done += len(block)
            now = time.time()
            samples.append((now, len(block)))
            while len(samples) > 1 and now - samples[0][0] > 5:
                samples.popleft()
            elapsed = samples[-1][0] - samples[0][0]
            speed = int(sum(x[1] for x in samples) / max(elapsed, 1.0))
            task.update(bytes_done=done, speed=speed,
                        percent=min(99, int(done * 100 / total)) if total else None)

        with src.open("rb") as stream:
            ftp.storbinary(f"STOR {remote.name}", stream,
                           blocksize=64 * 1024, callback=on_upload)
        task.update(bytes_done=done, total_bytes=total,
                    percent=100, progress_known=True, speed=0)

    return start_transfer_task(src.name, "Загрузка на FTP", worker)

class FTPCopyRequest(BaseModel):
    source_path: str
    destination_session: str
    destination_path: str


@app.post("/api/transfer/ftp-copy/{source_session}")
def ftp_copy(source_session: str, req: FTPCopyRequest):
    if source_session == req.destination_session:
        raise HTTPException(400, "\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a \u0438 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0434\u043e\u043b\u0436\u043d\u044b \u0431\u044b\u0442\u044c \u0440\u0430\u0437\u043d\u044b\u043c\u0438 FTP-\u0441\u043e\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u044f\u043c\u0438")
    if source_session not in sessions or req.destination_session not in sessions:
        raise HTTPException(401, "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u0435 \u043e\u0431\u0430 FTP-\u0441\u0435\u0440\u0432\u0435\u0440\u0430 \u0437\u0430\u043d\u043e\u0432\u043e")

    task_id = str(uuid.uuid4())
    transfer_tasks[task_id] = {
        "id": task_id, "status": "queued", "percent": 0,
        "speed": 0, "bytes_done": 0, "total_bytes": 0,
        "progress_known": False,
        "file": PurePosixPath(req.source_path).name,
        "phase": "\u041f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430", "error": None, "started_at": time.time(),
    }

    def worker():
        task = transfer_tasks[task_id]
        temp_path = None
        try:
            source = get_ftp(source_session)
            dest = get_ftp(req.destination_session)
            src_path = PurePosixPath(req.source_path)
            dst_path = PurePosixPath(req.destination_path)
            source.cwd(str(src_path.parent))
            try:
                size = source.size(src_path.name)
                total = int(size) if size is not None and int(size) > 0 else 0
            except Exception:
                total = 0
            size_known = total > 0
            task.update(status="running", phase="\u0421\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0441 \u0438\u0441\u0445\u043e\u0434\u043d\u043e\u0433\u043e FTP",
                        total_bytes=total * 2 if size_known else 0, progress_known=size_known)
            import tempfile
            fd, temp_path = tempfile.mkstemp(prefix="ftpkdv-transfer-")
            os.close(fd)
            downloaded = 0
            samples = deque()

            def update_speed(amount):
                now = time.time()
                samples.append((now, amount))
                while len(samples) > 1 and now - samples[0][0] > 5:
                    samples.popleft()
                elapsed = samples[-1][0] - samples[0][0]
                count = sum(item[1] for item in samples)
                return int(count / max(elapsed, 1.0))

            with open(temp_path, "wb") as stream:
                def on_download(chunk):
                    nonlocal downloaded
                    stream.write(chunk)
                    downloaded += len(chunk)
                    task.update(bytes_done=downloaded, speed=update_speed(len(chunk)),
                                percent=min(49, int(downloaded * 50 / total)) if size_known else None)
                source.retrbinary(f"RETR {src_path.name}", on_download, blocksize=64 * 1024)

            total = downloaded
            task.update(phase="\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u043d\u0430 \u0446\u0435\u043b\u0435\u0432\u043e\u0439 FTP",
                        bytes_done=downloaded, total_bytes=total * 2,
                        percent=50, progress_known=True, speed=0)
            samples.clear()
            uploaded = 0
            dest.cwd(str(dst_path.parent))
            with open(temp_path, "rb") as stream:
                def on_upload(block):
                    nonlocal uploaded
                    uploaded += len(block)
                    task.update(bytes_done=total + uploaded, speed=update_speed(len(block)),
                                percent=50 + int(uploaded * 50 / total) if total else 50)
                dest.storbinary(f"STOR {dst_path.name}", stream, blocksize=64 * 1024, callback=on_upload)
            task.update(status="completed", phase="\u041a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043e",
                        percent=100, speed=0, bytes_done=total * 2, total_bytes=total * 2,
                        progress_known=True)
        except Exception as e:
            task.update(status="error", phase="\u041e\u0448\u0438\u0431\u043a\u0430 \u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f", error=str(e), speed=0)
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

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
