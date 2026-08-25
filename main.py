"""
Attendance Logger - self-hosted sync server.

Endpoints used by the ESP32 device:
    POST /users               register/upsert an employee
    GET  /users               list employees
    DELETE /users/{id}        remove an employee
    POST /attendance          push a clock-in/out record
    GET  /attendance          query records (?from=unix_ts&to=unix_ts)
    POST /photos/{name}       upload a raw-JPEG photo proof
    GET  /photos/{name}       download a photo proof

Every endpoint requires an `X-API-Key` header matching ATTENDANCE_LOGGER_API_KEY
(set in .env, or the environment). The ESP32 firmware and mycelia-comm's server
proxy both send this key; nothing else should be able to reach this service.

In production this process runs on localhost:8001 behind a reverse proxy that
publishes it at https://app.jnanafarms.com/esp32_attendance_logger/api/. Every
route below is registered BOTH at its bare path (/users, /attendance, ...) and
under the /esp32_attendance_logger/api prefix, so it works whether the reverse
proxy strips that prefix before forwarding or passes the path through as-is —
no assumption about the proxy config is required.

Run:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8001
"""

import os
import secrets
from datetime import datetime
from pathlib import Path

import sqlite3
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

DB_PATH = Path(__file__).parent / "attendance.db"
PHOTO_DIR = Path(__file__).parent / "photos"
PHOTO_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("ATTENDANCE_LOGGER_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "ATTENDANCE_LOGGER_API_KEY is not set. Add it to .env (see attendance_logger_server/.env) "
        "and make sure the ESP32 firmware and mycelia-comm's ATTENDANCE_LOGGER_API_KEY use the same value."
    )

PUBLIC_PREFIX = "/esp32_attendance_logger/api"


def require_api_key(x_api_key: str | None = Header(default=None)):
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "invalid or missing API key")


app = FastAPI(title="Attendance Logger", dependencies=[Depends(require_api_key)])
router = APIRouter()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                fp_id INTEGER NOT NULL DEFAULT 0,
                face_enrolled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                user_id INTEGER,
                name TEXT,
                type TEXT NOT NULL,
                face_score REAL DEFAULT 0,
                face_match INTEGER DEFAULT 0,
                photo TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            );
            """
        )


init_db()


class UserIn(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=40)
    fp_id: int = 0
    face_enrolled: bool = False


class AttendanceIn(BaseModel):
    ts: int
    user_id: int = 0
    name: str = ""
    type: str = Field(pattern="^(in|out)$")
    face_score: float = 0.0
    face_match: bool = False
    photo: str = ""


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@router.post("/users")
def upsert_user(u: UserIn):
    with db() as con:
        con.execute(
            "INSERT INTO users(id, name, fp_id, face_enrolled) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "fp_id=excluded.fp_id, face_enrolled=excluded.face_enrolled",
            (u.id, u.name, u.fp_id, int(u.face_enrolled)),
        )
    return {"ok": True, "id": u.id}


@router.get("/users")
def list_users():
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY id").fetchall()
    return {"users": [dict(r) for r in rows]}


@router.delete("/users/{user_id}")
def delete_user(user_id: int):
    with db() as con:
        cur = con.execute("DELETE FROM users WHERE id=?", (user_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "user not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------
@router.post("/attendance")
def push_attendance(a: AttendanceIn):
    with db() as con:
        con.execute(
            "INSERT INTO attendance(ts, user_id, name, type, face_score, "
            "face_match, photo, synced_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                a.ts,
                a.user_id,
                a.name,
                a.type,
                a.face_score,
                int(a.face_match),
                a.photo,
                datetime.utcnow().isoformat(),
            ),
        )
    return {"ok": True}


@router.get("/attendance")
def query_attendance(frm: int | None = None, to: int | None = None):
    sql = "SELECT * FROM attendance"
    params = []
    if frm is not None:
        sql += " WHERE ts >= ?"
        params.append(frm)
    if to is not None:
        sql += " AND ts <= ?" if frm is not None else " WHERE ts <= ?"
        params.append(to)
    sql += " ORDER BY ts DESC"
    with db() as con:
        rows = con.execute(sql, params).fetchall()
    return {"attendance": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------
@router.post("/photos/{name}")
async def upload_photo(name: str, request: Request):
    body = await request.body()
    if len(body) == 0:
        raise HTTPException(400, "empty body")
    if not name.endswith(".jpg"):
        name += ".jpg"
    (PHOTO_DIR / name).write_bytes(body)
    return {"ok": True, "name": name}


@router.get("/photos/{name}")
def get_photo(name: str):
    p = PHOTO_DIR / name
    if not p.exists():
        raise HTTPException(404, "photo not found")
    return FileResponse(p, media_type="image/jpeg")


# Mounted twice on purpose (see module docstring): bare paths for local/dev use
# and direct testing, plus the public prefix for whichever way the production
# reverse proxy forwards requests.
app.include_router(router)
app.include_router(router, prefix=PUBLIC_PREFIX)
