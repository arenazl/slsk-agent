"""
Groove Sync Agent - Local file management agent for Groove Sync.
Runs as a Windows system tray application with an HTTP server on port 9900.
"""

import asyncio
from datetime import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "2.7.2"
PORT = 9900
ALLOWED_ORIGINS = [
    "https://groovesyncdj.netlify.app",
    "https://slsk-ui.netlify.app",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
]
SERVER_URL = "https://slsk-backend-7da97b8a965d.herokuapp.com"
AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".wav", ".aif", ".aiff",
    ".m4a", ".ogg", ".aac", ".wma", ".opus",
}
CONFIG_DIR = Path.home() / ".groovesync"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "agent.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("groovesync")

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read config, using defaults")
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Config saved: %s", cfg)


def get_download_folder() -> str | None:
    return load_config().get("folder")


def set_download_folder(folder: str):
    cfg = load_config()
    cfg["folder"] = folder
    save_config(cfg)
    Path(folder).mkdir(parents=True, exist_ok=True)


IGNORE_DIRS = {"exports", "__cache__"}


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _manifest_path() -> Path | None:
    folder = get_download_folder()
    if not folder:
        return None
    return Path(folder) / "manifest.json"


def load_manifest() -> dict:
    mp = _manifest_path()
    if mp and mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read manifest")
    return {}


def save_manifest(manifest: dict):
    mp = _manifest_path()
    if mp:
        mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_manifest(filename: str, metadata: dict):
    """Add or update a file entry in the manifest."""
    manifest = load_manifest()
    existing = manifest.get(filename, {})
    existing.update({k: v for k, v in metadata.items() if v is not None})
    manifest[filename] = existing
    save_manifest(manifest)


def remove_from_manifest(filename: str):
    manifest = load_manifest()
    manifest.pop(filename, None)
    save_manifest(manifest)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _analyze_and_store(filepath: Path, filename: str):
    """Analyze a track (duration, BPM proxy, intro/outro) and store in manifest.

    Runs synchronously — intended for use in a thread executor.
    """
    import struct
    import math

    ffprobe = shutil.which("ffprobe")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg_bin:
        log.warning("ffprobe/ffmpeg not found, skipping analysis for %s", filename)
        return

    meta = {}

    # --- Duration + sample rate ---
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        meta["duration"] = round(duration, 2)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                meta["sample_rate"] = int(stream.get("sample_rate", 44100))
                break
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", filename, e)
        return

    if meta.get("duration", 0) < 10:
        upsert_manifest(filename, meta)
        return

    # --- Decode to PCM for energy analysis ---
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-i", str(filepath), "-ac", "1", "-ar", "22050",
             "-f", "s16le", "-v", "quiet", "-"],
            capture_output=True, timeout=120,
        )
        raw = result.stdout
        n_samples = len(raw) // 2
        if n_samples < 22050:
            upsert_manifest(filename, meta)
            return

        samples = struct.unpack(f"<{n_samples}h", raw)
        sr = 22050
        duration = meta["duration"]

        # --- RMS energy in 1-second windows ---
        n_frames = n_samples // sr
        rms = []
        for i in range(n_frames):
            start = i * sr
            chunk = samples[start:start + sr]
            mean_sq = sum(s * s for s in chunk) / len(chunk)
            rms.append(math.sqrt(mean_sq))

        if len(rms) >= 10:
            # Smooth with 4s moving average
            smoothed = []
            for i in range(len(rms)):
                lo = max(0, i - 2)
                hi = min(len(rms), i + 3)
                smoothed.append(sum(rms[lo:hi]) / (hi - lo))

            peak = max(smoothed) if smoothed else 1.0
            if peak > 0:
                smoothed = [v / peak for v in smoothed]

            threshold = 0.60

            # Intro end
            intro_end = 0
            consec = 0
            for i in range(len(smoothed)):
                if smoothed[i] >= threshold:
                    consec += 1
                    if consec >= 4:
                        intro_end = max(0, i - 3)
                        break
                else:
                    consec = 0

            # Outro start
            outro_start = duration
            consec = 0
            for i in range(len(smoothed) - 1, -1, -1):
                if smoothed[i] >= threshold:
                    consec += 1
                    if consec >= 4:
                        outro_start = min(duration, i + 4)
                        break
                else:
                    consec = 0

            # Clamp
            intro_end = min(intro_end, duration * 0.25)
            outro_start = max(outro_start, duration * 0.60)

            # Snap to beat grid (4 beats at 128 BPM)
            beat_bar = 1.875
            intro_end = round(intro_end / beat_bar) * beat_bar
            outro_start = round(outro_start / beat_bar) * beat_bar

            meta["intro_end"] = round(max(0, intro_end), 2)
            meta["outro_start"] = round(min(duration, outro_start), 2)

    except Exception as e:
        log.warning("Energy analysis failed for %s: %s", filename, e)

    upsert_manifest(filename, meta)
    log.info("Analyzed and stored metadata for %s: %s", filename, meta)


def _find_file_in_library(filename: str) -> Path | None:
    """Find a file anywhere inside the download folder tree."""
    folder = get_download_folder()
    if not folder:
        return None
    root = Path(folder)
    for p in root.rglob("*"):
        if p.is_file() and p.name == filename:
            return p
    return None


def _file_size_mb(path: Path) -> float:
    return round(path.stat().st_size / (1024 * 1024), 2)


def _detect_format(ext: str) -> str:
    mapping = {
        ".flac": "FLAC", ".mp3": "MP3", ".wav": "WAV",
        ".aif": "AIF", ".aiff": "AIFF", ".m4a": "M4A",
        ".ogg": "OGG", ".aac": "AAC", ".wma": "WMA", ".opus": "OPUS",
    }
    return mapping.get(ext.lower(), ext.upper().lstrip("."))


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin", "")

    # Handle preflight
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex

    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Access-Control-Request-Private-Network"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ---------------------------------------------------------------------------
# HTTP Handlers
# ---------------------------------------------------------------------------


async def handle_status(request: web.Request):
    folder = get_download_folder()
    ffmpeg_available = shutil.which("ffmpeg") is not None
    return web.json_response({
        "status": "ok",
        "folder": folder,
        "version": VERSION,
        "ffmpeg": ffmpeg_available,
    })


async def handle_save_file(request: web.Request):
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No download folder configured"}, status=400)

    reader = await request.multipart()
    file_data = None
    filename = None
    genre = None
    metadata = {}

    while True:
        part = await reader.next()
        if part is None:
            break

        if part.name == "file":
            filename = filename or part.filename
            file_data = await part.read()
        elif part.name == "filename":
            raw = await part.read()
            filename = raw.decode("utf-8")
        elif part.name == "genre":
            raw = await part.read()
            genre = raw.decode("utf-8")
        elif part.name == "metadata":
            raw = await part.read()
            try:
                metadata = json.loads(raw.decode("utf-8"))
            except Exception:
                pass

    if not file_data or not filename:
        return web.json_response({"ok": False, "error": "Missing file or filename"}, status=400)

    dest_dir = Path(folder)
    if genre:
        dest_dir = dest_dir / genre
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / filename
    dest_path.write_bytes(file_data)
    log.info("Saved file: %s", dest_path)

    # Analyze in background (duration, intro/outro, etc.) and store in manifest
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _analyze_and_store, dest_path, filename)

    return web.json_response({"ok": True, "path": str(dest_path)})


async def handle_move_file(request: web.Request):
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No download folder configured"}, status=400)

    body = await request.json()
    filename = body.get("filename")
    genre = body.get("genre", "")

    if not filename:
        return web.json_response({"ok": False, "error": "Missing filename"}, status=400)

    src = _find_file_in_library(filename)
    if not src or not src.exists():
        return web.json_response({"ok": False, "error": "File not found"}, status=404)

    dest_dir = Path(folder)
    if genre:
        dest_dir = dest_dir / genre
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / filename
    if src != dest:
        shutil.move(str(src), str(dest))
        log.info("Moved %s -> %s", src, dest)

        # Clean up empty source directory
        old_parent = src.parent
        if old_parent != Path(folder) and not any(old_parent.iterdir()):
            old_parent.rmdir()

    return web.json_response({"ok": True})


async def handle_library(request: web.Request):
    """Return ONLY file info: filename, size_mb, format, subfolder. No metadata."""
    folder = get_download_folder()
    if not folder:
        return web.json_response([])

    root = Path(folder)
    if not root.exists():
        return web.json_response([])

    library = []

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if p.name == "manifest.json":
            continue

        # Skip files inside ignored directories
        rel = p.relative_to(root)
        top_dir = rel.parts[0] if len(rel.parts) > 1 else ""
        if top_dir.lower() in IGNORE_DIRS:
            continue

        subfolder = str(rel.parent) if str(rel.parent) != "." else ""

        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
        except Exception:
            mtime = ""

        library.append({
            "filename": p.name,
            "size_mb": _file_size_mb(p),
            "format": _detect_format(p.suffix),
            "subfolder": subfolder,
            "mtime": mtime,
        })

    return web.json_response(library)


async def handle_config(request: web.Request):
    body = await request.json()
    new_folder = body.get("folder")
    username = body.get("username")

    if username:
        config = load_config()
        config["username"] = username
        save_config(config)
        log.info("Username linked: %s", username)
        asyncio.ensure_future(_register_agent_ip())

    if "primary" in body:
        config = load_config()
        config["primary"] = bool(body["primary"])
        save_config(config)
        log.info("Primary agent: %s", config["primary"])

    if new_folder:
        set_download_folder(new_folder)
        log.info("Download folder updated to: %s", new_folder)

    if not new_folder and not username and "primary" not in body:
        return web.json_response({"ok": False, "error": "Missing folder or username"}, status=400)

    return web.json_response({"ok": True})


async def handle_rate(request: web.Request):
    """Deprecated: ratings now go to Heroku/Cloudinary. Kept for backwards compat."""
    return web.json_response({"ok": True, "deprecated": True})


async def handle_delete(request: web.Request):
    body = await request.json()
    filename = body.get("filename")
    if not filename:
        return web.json_response({"ok": False, "error": "Missing filename"}, status=400)

    filepath = _find_file_in_library(filename)
    if filepath and filepath.exists():
        parent = filepath.parent
        filepath.unlink()
        log.info("Deleted: %s", filepath)

        # Clean up empty directory
        folder = get_download_folder()
        if folder and parent != Path(folder) and not any(parent.iterdir()):
            parent.rmdir()
    else:
        log.warning("File not found for deletion: %s", filename)

    return web.json_response({"ok": True})


async def handle_delete_dupes(request: web.Request):
    body = await request.json()
    filenames = body.get("filenames", [])
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    download_dir = Path(folder)
    deleted_count = 0
    deleted_files = []

    for fname in filenames:
        filepath = _find_file_in_library(fname)
        if filepath and filepath.exists():
            try:
                parent = filepath.parent
                filepath.unlink()
                deleted_count += 1
                deleted_files.append(fname)
                log.info("Deleted dupe: %s", filepath)
                # Clean up empty directory
                if parent != download_dir and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception as e:
                log.error("Error deleting %s: %s", fname, e)

    return web.json_response({"ok": True, "deleted": deleted_count, "files": deleted_files})


async def handle_organize(request: web.Request):
    body = await request.json()
    moves = body.get("moves", [])
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    download_dir = Path(folder)
    moved_count = 0

    for move in moves:
        fname = move.get("filename")
        genre = move.get("genre")
        if not fname or not genre:
            continue
        filepath = _find_file_in_library(fname)
        if filepath and filepath.exists():
            dest_dir = download_dir / genre
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / filepath.name
            if dest != filepath:
                filepath.rename(dest)
                moved_count += 1
                log.info("Moved %s -> %s", filepath.name, genre)

    return web.json_response({"ok": True, "moved": moved_count})


async def handle_open_folder(request: web.Request):
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    subfolder = request.query.get("folder", "")
    target = Path(folder)
    if subfolder:
        target = target / subfolder
    target.mkdir(parents=True, exist_ok=True)

    try:
        _open_path(str(target))
        log.info("Opened folder: %s", target)
    except Exception as e:
        log.exception("Failed to open folder")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Audio streaming
# ---------------------------------------------------------------------------


def _cors_headers(request):
    origin = request.headers.get("Origin", "")
    h = {
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Range",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length",
    }
    if origin in ALLOWED_ORIGINS:
        h["Access-Control-Allow-Origin"] = origin
    return h


async def handle_audio(request: web.Request):
    """Stream an audio file from the library."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"error": "No folder configured"}, status=400)

    rel = request.match_info.get("path", "")
    if not rel:
        return web.json_response({"error": "Missing file path"}, status=400)

    # Try direct path first, then search by filename
    target = Path(folder) / rel
    if not target.exists() or not target.is_file():
        target = _find_file_in_library(Path(rel).name)
        if not target or not target.exists():
            return web.json_response({"error": "File not found"}, status=404)

    content_types = {
        ".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".aif": "audio/aiff", ".aiff": "audio/aiff", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".opus": "audio/opus",
    }
    ct = content_types.get(target.suffix.lower(), "application/octet-stream")
    file_size = target.stat().st_size
    cors = _cors_headers(request)

    # Support Range requests for seeking
    range_header = request.headers.get("Range", "")
    if range_header:
        start = 0
        end = file_size - 1
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
        length = end - start + 1
        resp = web.StreamResponse(
            status=206,
            headers={
                **cors,
                "Content-Type": ct,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            },
        )
        await resp.prepare(request)
        with open(target, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                await resp.write(chunk)
                remaining -= len(chunk)
    else:
        resp = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": ct,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )
        await resp.prepare(request)
        with open(target, "rb") as f:
            while chunk := f.read(64 * 1024):
                await resp.write(chunk)

    await resp.write_eof()
    return resp


# ---------------------------------------------------------------------------
# Set export
# ---------------------------------------------------------------------------


def _upload_to_cloudinary(data, public_id: str):
    """Upload JSON data to Cloudinary."""
    import tempfile
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
    )
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    cloudinary.uploader.upload(tmp.name, resource_type="raw", public_id=public_id, overwrite=True, invalidate=True)
    os.unlink(tmp.name)


async def handle_export(request: web.Request):
    """Export from library: generates .m3u with absolute paths. Optionally copies tracks + metadata."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    body = await request.json()
    name = body.get("name", "set")
    files = body.get("files", [])
    include_tracks = body.get("include_tracks", False)
    metadata = body.get("metadata", {})

    if not files:
        return web.json_response({"ok": False, "error": "No files"}, status=400)

    root = Path(folder)

    # Build .m3u with absolute paths to original files
    m3u_lines = ["#EXTM3U"]
    copied = 0

    if include_tracks:
        export_dir = root / "exports" / name
        export_dir.mkdir(parents=True, exist_ok=True)

        for i, fname in enumerate(files, 1):
            src = _find_file_in_library(fname)
            if not src or not src.exists():
                continue
            meta = metadata.get(fname, {})
            artist = meta.get("artist", "")
            title = meta.get("title", fname)
            m3u_lines.append(f"#EXTINF:-1,{artist} - {title}" if artist else f"#EXTINF:-1,{title}")
            # Copy file with numbered prefix
            numbered = f"{i:02d} - {fname}"
            dest = export_dir / numbered
            if not dest.exists():
                shutil.copy2(str(src), str(dest))
            m3u_lines.append(str(dest))
            copied += 1

        # Save metadata JSON alongside
        meta_path = export_dir / f"{name}_metadata.json"
        export_meta = []
        for i, fname in enumerate(files, 1):
            meta = metadata.get(fname, {})
            export_meta.append({"order": i, "filename": fname, **meta})
        meta_path.write_text(json.dumps(export_meta, indent=2, ensure_ascii=False), encoding="utf-8")

        m3u_path = export_dir / f"{name}.m3u"
        m3u_content = "\n".join(m3u_lines)
        m3u_path.write_text(m3u_content, encoding="utf-8")
        log.info("Exported '%s' with tracks: %d files to %s", name, copied, export_dir)
        _open_path(str(export_dir))
        return web.json_response({"ok": True, "copied": copied, "folder": str(export_dir), "m3u": str(m3u_path)})

    else:
        # M3U only — just absolute paths to originals
        for i, fname in enumerate(files, 1):
            src = _find_file_in_library(fname)
            if not src or not src.exists():
                continue
            meta = metadata.get(fname, {})
            artist = meta.get("artist", "")
            title = meta.get("title", fname)
            m3u_lines.append(f"#EXTINF:-1,{artist} - {title}" if artist else f"#EXTINF:-1,{title}")
            m3u_lines.append(str(src))
            copied += 1

        m3u_content = "\n".join(m3u_lines)
        # Also save to exports folder
        export_dir = root / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        m3u_path = export_dir / f"{name}.m3u"
        m3u_path.write_text(m3u_content, encoding="utf-8")
        log.info("Exported '%s' m3u only: %d tracks", name, copied)
        return web.json_response({"ok": True, "copied": copied, "folder": str(export_dir), "m3u": str(m3u_path), "m3u_content": m3u_content})


async def handle_export_set(request: web.Request):
    """Export a DJ set: save metadata to Cloudinary, build zip from local files."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    body = await request.json()
    name = body.get("name", "set")
    tracks = body.get("tracks", [])

    if not tracks:
        return web.json_response({"ok": False, "error": "No tracks"}, status=400)

    # 1. Save set metadata to Cloudinary
    username = load_config().get("username", "unknown")
    set_metadata = {"name": name, "username": username, "tracks": tracks, "created": time.strftime("%Y-%m-%d %H:%M")}
    try:
        cloud_key = f"soulseek/sets/{username}/{name}"
        await asyncio.get_event_loop().run_in_executor(None, _upload_to_cloudinary, set_metadata, cloud_key)
        log.info("Set metadata uploaded to Cloudinary: %s", cloud_key)
    except Exception as e:
        log.error("Failed to upload set metadata: %s", e)

    # 2. Copy files to exports/{name}/ and build zip
    import zipfile
    export_dir = Path(folder) / "exports" / name
    export_dir.mkdir(parents=True, exist_ok=True)

    zip_path = Path(folder) / "exports" / f"{name}.zip"
    copied = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for i, track in enumerate(tracks, 1):
            filename = track.get("filename", "")
            src = _find_file_in_library(filename)
            if not src or not src.exists():
                continue

            # Numbered filename for set order
            numbered = f"{i:02d} - {filename}"
            dest = export_dir / numbered
            if not dest.exists():
                shutil.copy2(str(src), str(dest))

            zf.write(str(dest), numbered)
            copied += 1

    log.info("Exported set '%s': %d/%d tracks, zip: %s", name, copied, len(tracks), zip_path)

    # 3. Serve zip as download
    if not zip_path.exists():
        return web.json_response({"ok": False, "error": "Zip creation failed"}, status=500)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{name}.zip"',
            "Content-Length": str(zip_path.stat().st_size),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )
    await resp.prepare(request)

    with open(zip_path, "rb") as f:
        while chunk := f.read(64 * 1024):
            await resp.write(chunk)

    await resp.write_eof()

    # Clean up zip (folder stays)
    zip_path.unlink(missing_ok=True)
    return resp


# ---------------------------------------------------------------------------
# Mix Editor endpoints
# ---------------------------------------------------------------------------


async def handle_track_info(request: web.Request):
    """Return duration, sample_rate, format for a track. Checks manifest first."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"error": "No folder configured"}, status=400)

    rel = request.match_info.get("path", "")
    # Check manifest for pre-analyzed data
    fname = Path(rel).name
    manifest = load_manifest()
    entry = manifest.get(fname, {})
    if entry.get("duration"):
        return web.json_response({
            "duration_seconds": entry["duration"],
            "sample_rate": entry.get("sample_rate", 44100),
            "format": Path(rel).suffix.lstrip(".").upper(),
            "bpm": entry.get("bpm"),
            "intro_end": entry.get("intro_end"),
            "outro_start": entry.get("outro_start"),
        })
    if not rel:
        return web.json_response({"error": "Missing file path"}, status=400)

    # Try subfolder/filename path first, then search by filename
    target = Path(folder) / rel
    if not target.exists() or not target.is_file():
        target = _find_file_in_library(Path(rel).name)
        if not target or not target.exists():
            return web.json_response({"error": "File not found"}, status=404)

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return web.json_response({"error": "ffprobe not found"}, status=500)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    ffprobe, "-v", "quiet", "-print_format", "json",
                    "-show_format", "-show_streams", str(target),
                ],
                capture_output=True, text=True, timeout=30,
            ),
        )
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        # Get sample_rate from first audio stream
        sample_rate = 44100
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                sample_rate = int(stream.get("sample_rate", 44100))
                break
        fmt = target.suffix.lstrip(".").upper()
        return web.json_response({
            "duration_seconds": round(duration, 2),
            "sample_rate": sample_rate,
            "format": fmt,
        })
    except Exception as e:
        log.exception("ffprobe failed for %s", target)
        return web.json_response({"error": str(e)}, status=500)


_analysis_cache = {}  # path -> { intro_end, outro_start, duration }


async def handle_track_analysis(request: web.Request):
    """Analyze a track's energy envelope to detect intro/outro boundaries.

    Returns intro_end (seconds from start where intro ends / beat kicks in)
    and outro_start (seconds from start where outro begins / energy drops).
    Uses ffmpeg to decode audio and numpy for RMS energy analysis.
    """
    folder = get_download_folder()
    if not folder:
        return web.json_response({"error": "No folder configured"}, status=400)

    rel = request.match_info.get("path", "")
    if not rel:
        return web.json_response({"error": "Missing file path"}, status=400)

    # Check manifest first (pre-analyzed on download)
    fname = Path(rel).name
    manifest = load_manifest()
    entry = manifest.get(fname, {})
    if entry.get("intro_end") is not None and entry.get("outro_start") is not None:
        return web.json_response({
            "intro_end": entry["intro_end"],
            "outro_start": entry["outro_start"],
            "duration": entry.get("duration", 0),
        })

    # Check in-memory cache
    if rel in _analysis_cache:
        return web.json_response(_analysis_cache[rel])

    target = Path(folder) / rel
    if not target.exists() or not target.is_file():
        target = _find_file_in_library(Path(rel).name)
        if not target or not target.exists():
            return web.json_response({"error": "File not found"}, status=404)

    ffprobe = shutil.which("ffprobe")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg_bin:
        return web.json_response({"error": "ffmpeg/ffprobe not found"}, status=500)

    def analyze(filepath):
        import struct
        import math

        # Get duration first
        probe = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        if duration < 30:
            return {"intro_end": 0, "outro_start": duration, "duration": duration}

        # Decode to raw PCM mono 22050Hz using ffmpeg
        result = subprocess.run(
            [ffmpeg_bin, "-i", str(filepath), "-ac", "1", "-ar", "22050",
             "-f", "s16le", "-v", "quiet", "-"],
            capture_output=True, timeout=120,
        )
        raw = result.stdout
        n_samples = len(raw) // 2
        if n_samples < 22050:
            return {"intro_end": 0, "outro_start": duration, "duration": duration}

        samples = struct.unpack(f"<{n_samples}h", raw)

        sr = 22050
        # Compute RMS energy in 1-second windows
        n_frames = n_samples // sr
        rms = []
        for i in range(n_frames):
            start = i * sr
            end = start + sr
            chunk = samples[start:end]
            mean_sq = sum(s * s for s in chunk) / len(chunk)
            rms.append(math.sqrt(mean_sq))

        if len(rms) < 10:
            return {"intro_end": 0, "outro_start": duration, "duration": duration}

        # Smooth RMS with a 4-second moving average
        kernel = 4
        smoothed = []
        for i in range(len(rms)):
            lo = max(0, i - kernel // 2)
            hi = min(len(rms), i + kernel // 2 + 1)
            smoothed.append(sum(rms[lo:hi]) / (hi - lo))

        # Normalize
        peak = max(smoothed) if smoothed else 1.0
        if peak > 0:
            smoothed = [v / peak for v in smoothed]

        # Threshold: "full energy" is above 60% of peak
        threshold = 0.60

        # Find intro_end: first moment energy stays above threshold for 4+ seconds
        intro_end = 0
        consecutive = 0
        for i in range(len(smoothed)):
            if smoothed[i] >= threshold:
                consecutive += 1
                if consecutive >= 4:
                    intro_end = max(0, i - 3)
                    break
            else:
                consecutive = 0

        # Find outro_start: last moment energy drops below threshold for 4+ seconds
        outro_start = duration
        consecutive = 0
        for i in range(len(smoothed) - 1, -1, -1):
            if smoothed[i] >= threshold:
                consecutive += 1
                if consecutive >= 4:
                    outro_start = min(duration, i + 4)
                    break
            else:
                consecutive = 0

        # Clamp: intro should be max 25% of track, outro start min 60% of track
        intro_end = min(intro_end, duration * 0.25)
        outro_start = max(outro_start, duration * 0.60)

        # Round to nearest beat-grid (assuming ~128 BPM = 0.46875s per beat, 4 beats = 1.875s)
        beat_bar = 1.875
        intro_end = round(intro_end / beat_bar) * beat_bar
        outro_start = round(outro_start / beat_bar) * beat_bar

        return {
            "intro_end": round(max(0, intro_end), 2),
            "outro_start": round(min(duration, outro_start), 2),
            "duration": round(duration, 2),
        }

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, analyze, target)
        _analysis_cache[rel] = result
        # Also persist to manifest so we never re-analyze
        upsert_manifest(fname, result)
        return web.json_response(result)
    except Exception as e:
        log.exception("track-analysis failed for %s", target)
        return web.json_response({"error": str(e)}, status=500)


async def handle_mix_export(request: web.Request):
    """Render a DJ mix using FFmpeg filter_complex (adelay + afade + amix)."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    body = await request.json()
    name = body.get("name", "mix")
    tracks = body.get("tracks", [])
    out_format = body.get("format", "mp3")
    bitrate = body.get("bitrate", "320k")

    if not tracks:
        return web.json_response({"ok": False, "error": "No tracks"}, status=400)

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return web.json_response({"ok": False, "error": "ffmpeg not found"}, status=500)

    root = Path(folder)
    export_dir = root / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize name for filesystem
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
    out_path = export_dir / f"{safe_name}.{out_format}"

    # Resolve file paths
    input_files = []
    for t in tracks:
        fname = t.get("filename", "")
        subfolder = t.get("subfolder", "")
        # Try subfolder/filename first
        if subfolder:
            candidate = root / subfolder / fname
            if candidate.exists():
                input_files.append((str(candidate), t))
                continue
        found = _find_file_in_library(fname)
        if found and found.exists():
            input_files.append((str(found), t))
        else:
            log.warning("Mix export: file not found: %s", fname)
            return web.json_response({"ok": False, "error": f"File not found: {fname}"}, status=404)

    # Build FFmpeg command with filter_complex
    cmd = [ffmpeg_bin, "-y"]

    # Add inputs
    for filepath, _ in input_files:
        cmd.extend(["-i", filepath])

    # Build filter_complex
    n = len(input_files)
    filter_parts = []
    mix_inputs = []

    master_bpm = data.get("master_bpm", 0)

    for i, (_, t) in enumerate(input_files):
        start_ms = int(t.get("start_time", 0) * 1000)
        duration = t.get("duration", 0)
        fade_in = t.get("fade_in", 0)
        fade_out = t.get("fade_out", 0)
        track_bpm = t.get("bpm", 0)
        label = f"a{i}"

        parts = []
        # Time-stretch to master BPM using atempo
        if master_bpm > 0 and track_bpm > 0 and track_bpm != master_bpm:
            # atempo = original_bpm / target_bpm (speed up or slow down)
            tempo_ratio = track_bpm / master_bpm
            # ffmpeg atempo only accepts 0.5-100.0, chain for extreme values
            if 0.5 <= tempo_ratio <= 100.0:
                parts.append(f"atempo={tempo_ratio:.6f}")
            elif tempo_ratio < 0.5:
                parts.append(f"atempo=0.5,atempo={tempo_ratio / 0.5:.6f}")
        # Delay to position track at start_time
        if start_ms > 0:
            parts.append(f"adelay={start_ms}|{start_ms}")
        # Fade in
        if fade_in > 0:
            parts.append(f"afade=t=in:st={t.get('start_time', 0)}:d={fade_in}")
        # Fade out
        if fade_out > 0 and duration > 0:
            fade_out_start = t.get("start_time", 0) + duration - fade_out
            parts.append(f"afade=t=out:st={fade_out_start}:d={fade_out}")

        if parts:
            filter_chain = ",".join(parts)
            filter_parts.append(f"[{i}:a]{filter_chain}[{label}]")
        else:
            filter_parts.append(f"[{i}:a]acopy[{label}]")
        mix_inputs.append(f"[{label}]")

    # Mix all streams
    mix_input_str = "".join(mix_inputs)
    filter_parts.append(f"{mix_input_str}amix=inputs={n}:duration=longest:dropout_transition=2[out]")

    filter_complex = ";".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex, "-map", "[out]"])

    # Output options
    if out_format == "mp3":
        cmd.extend(["-codec:a", "libmp3lame", "-b:a", bitrate])
    elif out_format == "flac":
        cmd.extend(["-codec:a", "flac"])
    elif out_format == "wav":
        cmd.extend(["-codec:a", "pcm_s16le"])
    else:
        cmd.extend(["-codec:a", "libmp3lame", "-b:a", bitrate])

    cmd.append(str(out_path))

    log.info("Mix export command: %s", " ".join(cmd[:6]) + " ... (filter_complex truncated)")
    log.info("Mix export: %d tracks -> %s", n, out_path)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=600),
        )
        if result.returncode != 0:
            log.error("FFmpeg stderr: %s", result.stderr[-1000:] if result.stderr else "")
            return web.json_response({"ok": False, "error": f"FFmpeg failed: {result.stderr[-500:]}"}, status=500)

        # Get output duration
        out_duration = 0
        try:
            probe_result = subprocess.run(
                [shutil.which("ffprobe") or "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(out_path)],
                capture_output=True, text=True, timeout=10,
            )
            probe_info = json.loads(probe_result.stdout)
            out_duration = float(probe_info.get("format", {}).get("duration", 0))
        except Exception:
            pass

        # Open exports folder
        _open_path(str(export_dir))

        log.info("Mix exported: %s (%.1f seconds)", out_path, out_duration)
        return web.json_response({
            "ok": True,
            "file": str(out_path),
            "duration": round(out_duration, 2),
        })
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "error": "FFmpeg timed out (10 min limit)"}, status=500)
    except Exception as e:
        log.exception("Mix export failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ---------------------------------------------------------------------------
# Refresh charts endpoint (once per day)
# ---------------------------------------------------------------------------

async def handle_refresh_charts(request: web.Request):
    """Trigger Beatport scraping if not done in last 24h."""
    config = load_config()
    last_scraped = config.get("last_scraped", "")
    today = datetime.now().strftime("%Y-%m-%d")
    if last_scraped == today:
        log.info("Charts already scraped today, skipping")
        return web.json_response({"ok": True, "skipped": True, "message": "Already scraped today"})

    log.info("Starting chart scrape via HTTP endpoint")
    try:
        count = await scrape_beatport_charts()
        config["last_scraped"] = today
        save_config(config)
        log.info("Scrape done: %d charts", count)
        return web.json_response({"ok": True, "scraped": count})
    except Exception as e:
        log.error("Scrape failed: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ---------------------------------------------------------------------------
# Self-restart endpoint
# ---------------------------------------------------------------------------

async def handle_restart(request: web.Request):
    """Check for update from GitHub Releases, download if available, and restart."""
    import urllib.request

    update_msg = ""
    try:
        url = "https://api.github.com/repos/arenazl/slsk-agent/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "GrooveSyncAgent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = data.get("tag_name", "").lstrip("v")
        current = VERSION

        if latest > current:
            log.info("Update available: v%s -> v%s", current, latest)

            if sys.platform == "darwin":
                import zipfile
                zip_url = None
                for asset in data.get("assets", []):
                    if asset["name"].endswith("-macOS.zip"):
                        zip_url = asset["browser_download_url"]
                        break
                if zip_url:
                    tmp_zip = Path("/tmp") / "GrooveSyncAgent-macOS.zip"
                    tmp_extract = Path("/tmp") / "GrooveSyncAgent_update"
                    urllib.request.urlretrieve(zip_url, str(tmp_zip))
                    if tmp_extract.exists():
                        shutil.rmtree(tmp_extract)
                    with zipfile.ZipFile(str(tmp_zip), 'r') as zf:
                        zf.extractall(str(tmp_extract))
                    tmp_zip.unlink(missing_ok=True)

                    if getattr(sys, 'frozen', False):
                        current_app = Path(sys.executable).resolve().parent.parent.parent
                        new_app = tmp_extract / "GrooveSyncAgent.app"
                        if new_app.exists() and current_app.name.endswith(".app"):
                            update_sh = Path("/tmp") / "groovesync_update.sh"
                            update_sh.write_text(f"""#!/bin/bash
sleep 2
rm -rf "{current_app}"
cp -R "{new_app}" "{current_app}"
open "{current_app}"
rm -rf "{tmp_extract}"
rm -f "$0"
""", encoding="utf-8")
                            update_sh.chmod(0o755)
                            update_msg = f"Actualizando a v{latest}..."
                            subprocess.Popen(["/bin/bash", str(update_sh)])
                        else:
                            update_msg = f"v{latest} disponible pero no se pudo actualizar .app"
                    else:
                        update_msg = f"v{latest} disponible (solo .app compilado)"
                        shutil.rmtree(tmp_extract, ignore_errors=True)
                else:
                    update_msg = "No se encontró build macOS en el release"
            else:
                # Windows
                exe_url = None
                for asset in data.get("assets", []):
                    if asset["name"].endswith(".exe"):
                        exe_url = asset["browser_download_url"]
                        break
                if exe_url:
                    tmp_path = Path(os.environ.get("TEMP", "/tmp")) / "GrooveSyncAgent_update.exe"
                    log.info("Downloading update from %s", exe_url)
                    urllib.request.urlretrieve(exe_url, str(tmp_path))

                    current_exe = Path(sys.executable)
                    if getattr(sys, 'frozen', False) or sys.executable.endswith('.exe'):
                        bat = Path(os.environ.get("TEMP", "/tmp")) / "groovesync_update.bat"
                        bat.write_text(f"""@echo off
timeout /t 2 /nobreak >nul
copy /Y "{tmp_path}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
""", encoding="utf-8")
                        update_msg = f"Actualizando a v{latest}..."
                        log.info("Launching update+restart bat...")
                        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
                    else:
                        update_msg = f"v{latest} disponible (solo .exe compilado)"
                else:
                    update_msg = "No se encontró .exe en el release"
        else:
            # Already up to date — just restart the process
            update_msg = f"Ya en v{current}, reiniciando..."
            log.info("No update needed, restarting current version...")

            exe = sys.executable
            if getattr(sys, 'frozen', False) or exe.endswith('.exe'):
                bat = Path(os.environ.get("TEMP", "/tmp")) / "groovesync_restart.bat"
                bat.write_text(f"""@echo off
timeout /t 2 /nobreak >nul
start "" "{exe}"
del "%~f0"
""", encoding="utf-8")
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
            elif sys.platform == "darwin":
                if getattr(sys, 'frozen', False):
                    current_app = Path(sys.executable).resolve().parent.parent.parent
                    subprocess.Popen(["open", str(current_app)])
                else:
                    subprocess.Popen([exe, str(Path(__file__).resolve())])
            else:
                subprocess.Popen([exe, str(Path(__file__).resolve())])

    except Exception as e:
        log.exception("Restart/update failed")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    # Send response before exiting
    resp = web.json_response({"ok": True, "message": update_msg, "restarting": True})
    await resp.prepare(request)
    await resp.write_eof()

    await asyncio.sleep(0.5)
    os._exit(0)


# ---------------------------------------------------------------------------
# Catch-all for OPTIONS preflight requests
# ---------------------------------------------------------------------------

async def handle_options(request: web.Request):
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


@web.middleware
async def logging_middleware(request, handler):
    log.info("→ %s %s (from %s)", request.method, request.path, request.headers.get("Origin", "direct"))
    try:
        response = await handler(request)
        log.info("← %s %s → %s", request.method, request.path, response.status)
        return response
    except Exception as e:
        log.error("← %s %s → ERROR: %s", request.method, request.path, e)
        raise


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, logging_middleware], client_max_size=500 * 1024 * 1024)  # 500 MB max upload

    # Register OPTIONS for all routes
    app.router.add_route("OPTIONS", "/{path:.*}", handle_options)

    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/save-file", handle_save_file)
    app.router.add_post("/api/move-file", handle_move_file)
    app.router.add_get("/api/library", handle_library)
    app.router.add_post("/api/config", handle_config)
    app.router.add_post("/api/rate", handle_rate)
    app.router.add_post("/api/delete", handle_delete)
    app.router.add_post("/api/delete-dupes", handle_delete_dupes)
    app.router.add_post("/api/organize", handle_organize)
    app.router.add_get("/api/open-folder", handle_open_folder)
    app.router.add_get("/api/audio/{path:.+}", handle_audio)
    app.router.add_post("/api/export", handle_export)
    app.router.add_post("/api/export-set", handle_export_set)
    app.router.add_get("/api/track-info/{path:.+}", handle_track_info)
    app.router.add_get("/api/track-analysis/{path:.+}", handle_track_analysis)
    app.router.add_post("/api/mix-export", handle_mix_export)
    app.router.add_post("/api/refresh-charts", handle_refresh_charts)
    app.router.add_post("/api/restart", handle_restart)
    return app


def _get_local_ip():
    """Get the local network IP address of this machine."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _get_tailscale_funnel_url():
    """Detect Tailscale Funnel HTTPS URL if active."""
    import subprocess
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        out = subprocess.check_output(["tailscale", "funnel", "status"], text=True, timeout=5, startupinfo=si)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("https://") and ".ts.net" in line:
                # Line like: "https://lookpcnew.tail4ac337.ts.net (Funnel on)"
                url = line.split()[0].rstrip("/")
                return url
    except Exception:
        pass
    return None


async def _register_agent_ip():
    """Register this agent's public URL with the cloud server so mobile/remote clients can connect."""
    config = load_config()
    username = config.get("username")
    if not username:
        log.info("No username configured, skipping agent registration")
        return
    # Prefer Tailscale Funnel (HTTPS, works from internet)
    agent_host = _get_tailscale_funnel_url()
    if not agent_host:
        local_ip = _get_local_ip()
        if not local_ip:
            log.warning("Could not determine agent host")
            return
        agent_host = f"http://{local_ip}:{PORT}"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(f"{SERVER_URL}/api/agent/register", json={
                "username": username,
                "agent_host": agent_host,
            })
        log.info("Registered agent host %s for user %s", agent_host, username)
    except Exception as e:
        log.warning("Failed to register agent: %s", e)


async def start_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server running on http://0.0.0.0:%d", PORT)
    # Register agent IP with cloud server periodically so mobile clients can find us
    asyncio.ensure_future(_register_agent_loop())
    return runner


async def _register_agent_loop():
    """Register agent IP on startup and every 4 minutes (server TTL is 5 min)."""
    while True:
        await _register_agent_ip()
        await asyncio.sleep(240)


# ---------------------------------------------------------------------------
# System Tray
# ---------------------------------------------------------------------------


def _create_tray_icon() -> Image.Image:
    """Load the app logo for the tray icon."""
    size = 64
    for logo_path in [
        Path(getattr(sys, '_MEIPASS', '')) / "logo.png",
        Path(__file__).parent / "logo.png",
        Path.cwd() / "logo.png",
    ]:
        if logo_path.exists():
            img = Image.open(logo_path).convert("RGBA")
            img = img.resize((size, size), Image.LANCZOS)
            return img
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill=(59, 130, 246, 255))
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "G", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), "G", fill=(255, 255, 255, 255), font=font)
    return img


def _pick_folder():
    """Open a folder picker dialog and return selected path."""
    if sys.platform == "darwin":
        try:
            script = (
                'set theFolder to POSIX path of '
                '(choose folder with prompt "Selecciona tu carpeta de descargas")'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().rstrip("/")
            return None
        except Exception:
            return None
    else:
        result = [None]
        def _run():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.focus_force()
            folder = filedialog.askdirectory(title="Selecciona tu carpeta de descargas")
            root.destroy()
            result[0] = folder if folder else None
        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=120)
        return result[0]


def _open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _do_check_update(notify_fn=None):
    """Check for updates. notify_fn(msg) is called to show messages."""
    import urllib.request
    if notify_fn is None:
        notify_fn = lambda msg: None
    try:
        url = "https://api.github.com/repos/arenazl/slsk-agent/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "GrooveSyncAgent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = data.get("tag_name", "").lstrip("v")
        current = VERSION
        if latest <= current:
            log.info("Already up to date: v%s", current)
            notify_fn(f"Ya tenés la última versión (v{current})")
            return

        log.info("New version available: v%s -> v%s", current, latest)

        if sys.platform == "darwin":
            import zipfile
            # Find macOS zip asset in release
            zip_url = None
            for asset in data.get("assets", []):
                if asset["name"].endswith("-macOS.zip"):
                    zip_url = asset["browser_download_url"]
                    break
            if not zip_url:
                log.error("No macOS zip found in release")
                notify_fn("No se encontró build de macOS en el release")
                return

            tmp_zip = Path("/tmp") / "GrooveSyncAgent-macOS.zip"
            tmp_extract = Path("/tmp") / "GrooveSyncAgent_update"
            log.info("Downloading update from %s", zip_url)
            notify_fn(f"Descargando v{latest}...")
            urllib.request.urlretrieve(zip_url, str(tmp_zip))

            # Extract zip
            if tmp_extract.exists():
                shutil.rmtree(tmp_extract)
            with zipfile.ZipFile(str(tmp_zip), 'r') as zf:
                zf.extractall(str(tmp_extract))
            tmp_zip.unlink(missing_ok=True)

            # Find current .app location and replace it
            if getattr(sys, 'frozen', False):
                # Running as compiled .app bundle
                current_app = Path(sys.executable).resolve().parent.parent.parent
                new_app = tmp_extract / "GrooveSyncAgent.app"
                if new_app.exists() and current_app.name.endswith(".app"):
                    # Use a shell script to replace after exit
                    update_sh = Path("/tmp") / "groovesync_update.sh"
                    update_sh.write_text(f"""#!/bin/bash
sleep 2
rm -rf "{current_app}"
cp -R "{new_app}" "{current_app}"
open "{current_app}"
rm -rf "{tmp_extract}"
rm -f "$0"
""", encoding="utf-8")
                    update_sh.chmod(0o755)
                    log.info("Launching update script, restarting...")
                    notify_fn(f"Actualizando a v{latest}...")
                    subprocess.Popen(["/bin/bash", str(update_sh)])
                    os._exit(0)
                else:
                    log.error("Could not determine .app path for update")
                    notify_fn("Error: no se pudo determinar la ubicación de la app")
                    shutil.rmtree(tmp_extract, ignore_errors=True)
            else:
                log.info("Not running as .app bundle, skipping self-update")
                notify_fn("Actualización solo disponible en .app compilado")
                shutil.rmtree(tmp_extract, ignore_errors=True)
        else:
            exe_url = None
            for asset in data.get("assets", []):
                if asset["name"].endswith(".exe"):
                    exe_url = asset["browser_download_url"]
                    break
            if not exe_url:
                log.error("No exe found in release")
                return

            tmp_path = Path(os.environ.get("TEMP", "/tmp")) / "GrooveSyncAgent_update.exe"
            log.info("Downloading update from %s", exe_url)
            urllib.request.urlretrieve(exe_url, str(tmp_path))
            log.info("Downloaded update to %s", tmp_path)

            current_exe = Path(sys.executable)
            if getattr(sys, 'frozen', False) or sys.executable.endswith('.exe'):
                bat = Path(os.environ.get("TEMP", "/tmp")) / "groovesync_update.bat"
                bat.write_text(f"""@echo off
timeout /t 2 /nobreak >nul
copy /Y "{tmp_path}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
""", encoding="utf-8")
                log.info("Launching update script, restarting...")
                notify_fn(f"Actualizando a v{latest}...")
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
                os._exit(0)
            else:
                log.info("Not running as exe, skipping self-update")
                notify_fn("Actualización solo disponible en .exe")
    except Exception as e:
        log.error("Update failed: %s", e)
        notify_fn(f"Error al actualizar: {e}")


# ---------------------------------------------------------------------------
# macOS tray (rumps)
# ---------------------------------------------------------------------------

if sys.platform == "darwin":
    import rumps

    class GrooveSyncMacApp(rumps.App):
        def __init__(self):
            icon_path = None
            for p in [
                Path(getattr(sys, '_MEIPASS', '')) / "menubar_icon.png",
                Path(__file__).parent / "menubar_icon.png",
                Path(getattr(sys, '_MEIPASS', '')) / "logo_transparent.png",
                Path(__file__).parent / "logo_transparent.png",
                Path(getattr(sys, '_MEIPASS', '')) / "logo.png",
                Path(__file__).parent / "logo.png",
            ]:
                if p.exists():
                    icon_path = str(p)
                    break
            super().__init__(
                "Groove Sync",
                icon=icon_path,
                quit_button=None,
            )
            self.menu = [
                rumps.MenuItem("Abrir carpeta", callback=self.on_open_folder),
                rumps.MenuItem("Configurar carpeta", callback=self.on_configure_folder),
                rumps.separator,
                rumps.MenuItem("Renovar Charts", callback=self.on_refresh_charts),
                rumps.MenuItem("Estado", callback=self.on_status),
                rumps.separator,
                rumps.MenuItem("Ver logs", callback=self.on_view_logs),
                rumps.MenuItem("Actualizar", callback=self.on_update),
                rumps.separator,
                rumps.MenuItem("Salir", callback=self.on_quit),
            ]

        def on_open_folder(self, _):
            folder = get_download_folder()
            if folder:
                Path(folder).mkdir(parents=True, exist_ok=True)
                _open_path(folder)
            else:
                self.on_configure_folder(_)

        def on_configure_folder(self, _):
            folder = _pick_folder()
            if folder:
                set_download_folder(folder)
                log.info("Folder configured via tray: %s", folder)

        def on_status(self, _):
            folder = get_download_folder() or "(no configurada)"
            rumps.notification(
                "Groove Sync Agent",
                f"v{VERSION} - Puerto {PORT}",
                f"Carpeta: {folder}",
            )

        def on_refresh_charts(self, _):
            log.info("Manual chart refresh requested")
            rumps.notification("Groove Sync Agent", "", "Renovando charts...")
            def do_scrape():
                loop = asyncio.new_event_loop()
                try:
                    count = loop.run_until_complete(scrape_beatport_charts())
                    log.info("Manual scrape done: %d charts", count)
                    rumps.notification("Groove Sync Agent", "", f"Charts actualizados: {count} géneros")
                except Exception as e:
                    log.error("Manual scrape failed: %s", e)
                finally:
                    loop.close()
            threading.Thread(target=do_scrape, daemon=True).start()

        def on_view_logs(self, _):
            _open_path(str(LOG_FILE))

        def on_update(self, _):
            log.info("Checking for updates...")
            def do():
                _do_check_update(
                    notify_fn=lambda msg: rumps.notification("Groove Sync Agent", "", msg)
                )
            threading.Thread(target=do, daemon=True).start()

        def on_quit(self, _):
            log.info("Agent shutting down via tray menu")
            rumps.quit_application()
            os._exit(0)


# ---------------------------------------------------------------------------
# Windows/Linux tray (pystray)
# ---------------------------------------------------------------------------

def _on_open_folder(icon, item):
    folder = get_download_folder()
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        _open_path(folder)
    else:
        _on_configure_folder(icon, item)


def _on_configure_folder(icon, item):
    folder = _pick_folder()
    if folder:
        set_download_folder(folder)
        log.info("Folder configured via tray: %s", folder)


def _on_status(icon, item):
    folder = get_download_folder() or "(no configurada)"
    try:
        icon.notify(
            f"Carpeta: {folder}\nPuerto: {PORT}\nVersion: {VERSION}",
            "Groove Sync Agent",
        )
    except Exception:
        log.info("Status — Folder: %s, Port: %d, Version: %s", folder, PORT, VERSION)


def _on_refresh_charts(icon, item):
    log.info("Manual chart refresh requested")
    try:
        icon.notify("Renovando charts...", "Groove Sync Agent")
    except Exception:
        pass
    def do_scrape():
        loop = asyncio.new_event_loop()
        try:
            count = loop.run_until_complete(scrape_beatport_charts())
            log.info("Manual scrape done: %d charts", count)
            try:
                icon.notify(f"Charts actualizados: {count} géneros", "Groove Sync Agent")
            except Exception:
                pass
        except Exception as e:
            log.error("Manual scrape failed: %s", e)
        finally:
            loop.close()
    threading.Thread(target=do_scrape, daemon=True).start()


def _on_view_logs(icon, item):
    _open_path(str(LOG_FILE))


def _on_update(icon, item):
    log.info("Checking for updates...")
    try:
        icon.notify("Buscando actualizaciones...", "Groove Sync Agent")
    except Exception:
        pass
    def do():
        def notify_fn(msg):
            try:
                icon.notify(msg, "Groove Sync Agent")
            except Exception:
                pass
        _do_check_update(notify_fn)
    threading.Thread(target=do, daemon=True).start()


def _on_exit(icon, item):
    log.info("Agent shutting down via tray menu")
    icon.stop()
    os._exit(0)


def run_tray(ready_event: threading.Event):
    """Run the pystray icon (Windows/Linux only)."""
    icon = pystray.Icon(
        "groovesync",
        _create_tray_icon(),
        f"Groove Sync v{VERSION} - Online",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir carpeta", _on_open_folder),
            pystray.MenuItem("Configurar carpeta", _on_configure_folder),
            pystray.MenuItem("Renovar Charts", _on_refresh_charts),
            pystray.MenuItem("Estado", _on_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ver logs", _on_view_logs),
            pystray.MenuItem("Actualizar", _on_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", _on_exit),
        ),
    )
    ready_event.set()
    icon.run()


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------


def first_run_setup():
    """Prompt user to pick a download folder on first run."""
    if get_download_folder():
        return

    log.info("First run detected — prompting for download folder")
    folder = _pick_folder()
    if folder:
        set_download_folder(folder)
        log.info("Initial folder set to: %s", folder)
    else:
        # Set a sensible default
        default = str(Path.home() / "Music" / "GrooveSync")
        set_download_folder(default)
        log.info("No folder selected, using default: %s", default)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Beatport Scraping & Cloudinary Upload
# ---------------------------------------------------------------------------

CLOUDINARY_CLOUD_NAME = "di39tigkf"
CLOUDINARY_API_KEY = "986738179528233"
CLOUDINARY_API_SECRET = "k1cxARGZPqw9oxn09scf8N16_oM"
BEATPORT_SCRAPE_INTERVAL = 24 * 3600  # 24 hours
BEATPORT_GENRES = [
    {"name": "Tech House", "id": 11, "slug": "tech-house"},
    {"name": "Melodic House", "id": 90, "slug": "melodic-house-techno"},
    {"name": "Afro House", "id": 89, "slug": "afro-house"},
    {"name": "Deep House", "id": 12, "slug": "deep-house"},
    {"name": "Hip Hop", "id": 105, "slug": "hip-hop"},
    {"name": "Nu Disco", "id": 50, "slug": "nu-disco-disco"},
    {"name": "Downtempo", "id": 63, "slug": "downtempo"},
    {"name": "Electro", "id": 94, "slug": "electro-classic-detroit-modern"},
    {"name": "Indie Dance", "id": 37, "slug": "indie-dance"},
    {"name": "Minimal Tech", "id": 14, "slug": "minimal-deep-tech"},
    {"name": "Progressive House", "id": 15, "slug": "progressive-house"},
    {"name": "Trance", "id": 7, "slug": "trance-main-floor"},
    {"name": "Peak Time Techno", "id": 6, "slug": "techno-peak-time-driving"},
]

async def scrape_beatport_charts():
    """Scrape Beatport Top 100 and all genre charts, upload to Cloudinary."""
    import re
    import tempfile
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    genre_urls = [("main", 0, "", "https://www.beatport.com/top-100")]
    for g in BEATPORT_GENRES:
        genre_urls.append((str(g["id"]), g["id"], g["slug"],
            f"https://www.beatport.com/genre/{g['slug']}/{g['id']}/top-100"))

    # Start headless Chrome
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    try:
        driver = await asyncio.get_event_loop().run_in_executor(
            None, lambda: webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options))
    except Exception as e:
        log.error("Could not start Chrome: %s", e)
        return 0

    def parse_beatport_html(html: str) -> list:
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if not match:
            return []
        next_data = json.loads(match.group(1))
        props = next_data.get("props", {}).get("pageProps", {})
        chart_data = props.get("dehydratedState", {}).get("queries", [])
        tracks = []
        for query in chart_data:
            state = query.get("state", {})
            data = state.get("data", {})
            results = data.get("results", data.get("tracks", []))
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                artists = item.get("artists", [])
                artist_names = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
                title = item.get("name", "")
                mix_name = item.get("mix_name", "")
                if mix_name and mix_name.lower() != "original":
                    title = f"{title} ({mix_name})"
                key_name = ""
                key_obj = item.get("key", {})
                if isinstance(key_obj, dict):
                    key_name = key_obj.get("name", key_obj.get("camelot_short", ""))
                genre_name = ""
                genre_list = item.get("genre", [])
                if isinstance(genre_list, list) and genre_list:
                    genre_name = genre_list[0].get("genre_name", "") if isinstance(genre_list[0], dict) else ""
                elif isinstance(genre_list, dict):
                    genre_name = genre_list.get("name", "")
                tracks.append({
                    "id": item.get("id", 0),
                    "title": title,
                    "artist": artist_names,
                    "genre": genre_name,
                    "bpm": item.get("bpm"),
                    "key": key_name,
                    "label": (item.get("release", {}) or {}).get("label", {}).get("name", "") if isinstance(item.get("release"), dict) else "",
                    "duration_ms": item.get("length_ms", 0) or item.get("length", 0),
                    "sample_url": item.get("sample_url", "") or "",
                    "artwork_url": (item.get("release", {}) or {}).get("image", {}).get("uri", "") if isinstance(item.get("release"), dict) else "",
                    "position": item.get("position", 0) or item.get("number", 0),
                })
            if tracks:
                break
        return tracks

    def upload_to_cloudinary(data, public_id: str):
        try:
            import cloudinary
            import cloudinary.uploader
            cloudinary.config(cloud_name=CLOUDINARY_CLOUD_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET)
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.close()
            cloudinary.uploader.upload(tmp.name, resource_type="raw", public_id=public_id, overwrite=True, invalidate=True)
            os.unlink(tmp.name)
        except Exception as e:
            log.error("Cloudinary upload failed for %s: %s", public_id, e)

    scraped = 0
    try:
        for slug, genre_id, genre_slug, url in genre_urls:
            try:
                log.info("Scraping Beatport: %s", slug)
                await asyncio.get_event_loop().run_in_executor(None, driver.get, url)
                await asyncio.sleep(3)  # Wait for page to load
                html = await asyncio.get_event_loop().run_in_executor(None, lambda: driver.page_source)
                tracks = parse_beatport_html(html)
                if tracks:
                    cache_key = f"soulseek/beatport_chart_{slug}"
                    await asyncio.get_event_loop().run_in_executor(None, upload_to_cloudinary, tracks, cache_key)
                    scraped += 1
                    log.info("Scraped %d tracks for %s, uploaded to Cloudinary", len(tracks), slug)
                else:
                    log.warning("No tracks found for %s", slug)
                await asyncio.sleep(2)  # Delay between requests
            except Exception as e:
                log.error("Error scraping %s: %s", slug, e)
    finally:
        try:
            await asyncio.get_event_loop().run_in_executor(None, driver.quit)
        except Exception:
            pass

    return scraped


def is_primary_agent() -> bool:
    """Check if this agent is configured as primary (auto-scrapes charts)."""
    config = load_config()
    return config.get("primary", False)


async def beatport_scrape_loop():
    """Run Beatport scraping on startup and every 24 hours. Only for primary agent."""
    if not is_primary_agent():
        log.info("Secondary agent — skipping automatic chart scraping")
        return
    while True:
        try:
            count = await scrape_beatport_charts()
            log.info("Beatport scrape complete: %d charts updated", count)
        except Exception as e:
            log.error("Beatport scrape error: %s", e)
        await asyncio.sleep(BEATPORT_SCRAPE_INTERVAL)


def _run_server_in_thread():
    """Run the async HTTP server in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        runner = loop.run_until_complete(start_server())
        loop.create_task(beatport_scrape_loop())
        log.info("Agent ready — listening on port %d", PORT)
        loop.run_forever()
    except Exception as e:
        log.error("Server error: %s", e)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def main():
    log.info("=== Groove Sync Agent v%s starting ===", VERSION)

    # First run setup (folder picker)
    first_run_setup()

    if sys.platform == "darwin":
        # macOS: rumps (AppKit) MUST run on the main thread
        server_thread = threading.Thread(target=_run_server_in_thread, daemon=True)
        server_thread.start()
        log.info("HTTP server started in background thread")

        app = GrooveSyncMacApp()
        app.run()  # blocks main thread
    else:
        # Windows/Linux: tray in thread, server on main thread
        tray_ready = threading.Event()
        tray_thread = threading.Thread(target=run_tray, args=(tray_ready,), daemon=True)
        tray_thread.start()
        tray_ready.wait()
        log.info("System tray icon active")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runner = loop.run_until_complete(start_server())
            loop.create_task(beatport_scrape_loop())
            log.info("Agent ready — listening on port %d", PORT)
            loop.run_forever()
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            log.info("Agent stopped")


if __name__ == "__main__":
    main()
