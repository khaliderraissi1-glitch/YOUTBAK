import os
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
import yt_dlp
from .config import get_settings
from .models import VideoRequest
from .service import extract_video, get_cached, _pick_format, _ydl_opts

settings = get_settings()
limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])
app = FastAPI(title=settings.app_name, version="1.0.0", description="YouTube metadata and media download API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins or ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return {"name": settings.app_name, "status": "ok", "docs": "/docs", "endpoint": "POST /api/info"}

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "youtube-backend"}

@app.post("/api/info")
@limiter.limit(settings.rate_limit)
def info(body: VideoRequest, request: Request):
    try: return extract_video(str(body.url), str(request.base_url))
    except Exception as exc: raise HTTPException(status_code=422, detail=f"Failed to process YouTube URL: {exc}") from exc

@app.get("/api/info")
@limiter.limit(settings.rate_limit)
def info_get(url: str, request: Request):
    try: return extract_video(url, str(request.base_url))
    except Exception as exc: raise HTTPException(status_code=422, detail=f"Failed to process YouTube URL: {exc}") from exc

@app.get("/api/download/{token}/{kind}")
@limiter.limit("10/minute")
def download(token: str, kind: str, request: Request):
    if kind not in {"video_hd", "video_sd", "audio_mp3"}: raise HTTPException(400, "Unsupported download type")
    cached = get_cached(token)
    if not cached: raise HTTPException(410, "This download link expired. Request the video information again.")
    info = cached["info"]
    format_id, _, _ = _pick_format(info, kind)
    if not format_id: raise HTTPException(404, "Requested format is not available for this video")
    if kind == "audio_mp3":
        tempdir = tempfile.mkdtemp(prefix="youtube-")
        out = os.path.join(tempdir, "audio.%(ext)s")
        opts = _ydl_opts() | {"format": format_id, "outtmpl": out, "noplaylist": True, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([cached["canonical_url"]])
            mp3 = next(Path(tempdir).glob("audio.mp3"), None)
            if not mp3: raise RuntimeError("MP3 conversion failed; ensure ffmpeg is installed")
            return FileResponse(mp3, media_type="audio/mpeg", filename="youtube-audio.mp3")
        except Exception as exc: raise HTTPException(502, str(exc)) from exc
    opts = _ydl_opts() | {"format": format_id}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl: data = ydl.extract_info(cached["canonical_url"], download=False)
        selected = next((f for f in data.get("formats", []) if f.get("format_id") == format_id), None)
        if not selected or not selected.get("url"): raise RuntimeError("Media URL unavailable")
        return StreamingResponse(_proxy(selected["url"]), media_type="video/mp4", headers={"Content-Disposition": 'attachment; filename="youtube-video.mp4"'})
    except Exception as exc: raise HTTPException(502, f"Unable to fetch media: {exc}") from exc

async def _proxy(url: str):
    import httpx
    async with httpx.AsyncClient(follow_redirects=True, timeout=settings.request_timeout_seconds) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(1024 * 1024): yield chunk
