"""
Roh4n API server compatible with the Youtube.py client:

    GET /stream/{video_id}?key=API_KEY&type=audio&quality=128   -> mp3 file
    GET /stream/{video_id}?key=API_KEY&type=video&quality=480   -> mp4 file

Requires ffmpeg installed on the server.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Env:  ADMIN_TOKEN   secret used to create/revoke keys (required)
      DATABASE_URL  Postgres URL so keys survive restarts on hosts with temporary disks
      COOKIES_FILE  optional cookies.txt for yt-dlp (helps on server IPs)
      RATE_LIMIT    requests per minute per key (default 60)
      MAX_JOBS      parallel downloads (default 4)
"""
import asyncio
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "60"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "4"))
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
AUDIO_QUALITIES = {64, 96, 128, 192, 256, 320}
VIDEO_QUALITIES = {144, 240, 360, 480, 720, 1080}

DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Postgres URL (Neon/Supabase). Empty = local SQLite


class DB:
    """Tiny wrapper so the same SQL works on SQLite (local) and Postgres (hosted)."""

    def __init__(self):
        self.pg = bool(DATABASE_URL)
        self.lock = threading.Lock()
        self._connect()
        self.execute(
            """CREATE TABLE IF NOT EXISTS keys (
                hash TEXT PRIMARY KEY, owner TEXT, created DOUBLE PRECISION,
                revoked INTEGER DEFAULT 0, hits INTEGER DEFAULT 0)"""
        )

    def _connect(self):
        if self.pg:
            import psycopg

            self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
        else:
            self.conn = sqlite3.connect("keys.db", check_same_thread=False, isolation_level=None)

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self.lock:
            for attempt in (0, 1):
                try:
                    return self.conn.execute(sql, params)
                except Exception:
                    if attempt or not self.pg:
                        raise
                    self._connect()  # hosted DBs drop idle connections; reconnect once

    def commit(self):  # autocommit is on; kept so existing calls still work
        pass


db = DB()

app = FastAPI(title="Roh4n API")
_hits: dict[str, deque] = defaultdict(deque)
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_jobs = asyncio.Semaphore(MAX_JOBS)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def require_key(
    key: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
) -> str:
    k = key or x_api_key
    if not k:
        raise HTTPException(401, "Missing API key")
    h = _hash(k)
    row = db.execute("SELECT revoked FROM keys WHERE hash=?", (h,)).fetchone()
    if not row or row[0]:
        raise HTTPException(403, "Invalid or revoked API key")

    now = time.time()
    q = _hits[h]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded")
    q.append(now)

    db.execute("UPDATE keys SET hits = hits + 1 WHERE hash=?", (h,))
    db.commit()
    return h


def require_admin(x_admin_token: str | None = Header(default=None)):
    if not ADMIN_TOKEN or not secrets.compare_digest(x_admin_token or "", ADMIN_TOKEN):
        raise HTTPException(403, "Admin only")


def _base_opts() -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    return o


def _download(video_id: str, kind: str, quality: int, out_path: Path) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    stem = str(out_path.with_suffix(""))
    opts = _base_opts()
    if kind == "audio":
        opts.update(
            format="bestaudio/best",
            outtmpl=stem + ".%(ext)s",
            postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)}
            ],
        )
    else:
        opts.update(
            format=(
                f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            ),
            outtmpl=stem + ".%(ext)s",
            merge_output_format="mp4",
        )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stream/{video_id}")
async def stream(
    video_id: str,
    type: str = Query("audio", pattern="^(audio|video)$"),
    quality: int = Query(128),
    _: str = Depends(require_key),
):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video id")
    allowed = AUDIO_QUALITIES if type == "audio" else VIDEO_QUALITIES
    if quality not in allowed:
        quality = 128 if type == "audio" else 480

    ext = "mp3" if type == "audio" else "mp4"
    out_path = CACHE_DIR / f"{video_id}_{type}_{quality}.{ext}"

    async with _locks[out_path.name]:  # avoid duplicate downloads of the same file
        if not (out_path.exists() and out_path.stat().st_size > 10000):
            async with _jobs:
                try:
                    await asyncio.to_thread(_download, video_id, type, quality, out_path)
                except Exception as e:
                    raise HTTPException(502, f"Download failed: {e}")
        if not out_path.exists():
            raise HTTPException(502, "Download produced no file")

    return FileResponse(out_path, media_type="audio/mpeg" if type == "audio" else "video/mp4")


# ---------- admin endpoints ----------
@app.post("/admin/keys", dependencies=[Depends(require_admin)])
def create_key(owner: str):
    key = "roh4n_" + secrets.token_urlsafe(24)
    # one active key per owner: creating a new key rotates (revokes) the old one
    db.execute("UPDATE keys SET revoked=1 WHERE owner=?", (owner,))
    db.execute("INSERT INTO keys (hash, owner, created) VALUES (?,?,?)", (_hash(key), owner, time.time()))
    db.commit()
    return {"owner": owner, "api_key": key}  # shown once; only the hash is stored


@app.delete("/admin/keys", dependencies=[Depends(require_admin)])
def revoke_key(api_key: str):
    db.execute("UPDATE keys SET revoked=1 WHERE hash=?", (_hash(api_key),))
    db.commit()
    return {"revoked": True}


@app.get("/admin/usage", dependencies=[Depends(require_admin)])
def usage(owner: str):
    row = db.execute(
        "SELECT hits, created FROM keys WHERE owner=? AND revoked=0 ORDER BY created DESC LIMIT 1",
        (owner,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "No active key")
    return {"owner": owner, "hits": row[0], "created": row[1]}
