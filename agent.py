"""
Groove Sync Agent - Local file management agent for Groove Sync.
Runs as a Windows system tray application with an HTTP server on port 9900.
"""

import asyncio
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

VERSION = "1.5.0"
PORT = 9900
ALLOWED_ORIGINS = [
    "https://groovesyncdj.netlify.app",
    "https://slsk-ui.netlify.app",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
]
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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ---------------------------------------------------------------------------
# HTTP Handlers
# ---------------------------------------------------------------------------


async def handle_status(request: web.Request):
    folder = get_download_folder()
    return web.json_response({
        "status": "ok",
        "folder": folder,
        "version": VERSION,
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

        library.append({
            "filename": p.name,
            "size_mb": _file_size_mb(p),
            "format": _detect_format(p.suffix),
            "subfolder": subfolder,
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
# Catch-all for OPTIONS preflight requests
# ---------------------------------------------------------------------------

async def handle_options(request: web.Request):
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware], client_max_size=500 * 1024 * 1024)  # 500 MB max upload

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
    app.router.add_post("/api/export-set", handle_export_set)
    return app


async def start_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    log.info("HTTP server running on http://127.0.0.1:%d", PORT)
    return runner


# ---------------------------------------------------------------------------
# System Tray
# ---------------------------------------------------------------------------


def _create_tray_icon() -> Image.Image:
    """Load the app logo for the tray icon."""
    size = 64
    # Try to load logo.png from same directory as the script/exe
    for logo_path in [
        Path(getattr(sys, '_MEIPASS', '')) / "logo.png",
        Path(__file__).parent / "logo.png",
        Path.cwd() / "logo.png",
    ]:
        if logo_path.exists():
            img = Image.open(logo_path).convert("RGBA")
            img = img.resize((size, size), Image.LANCZOS)
            return img
    # Fallback: simple icon
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
    """Open a tkinter folder picker dialog and return selected path."""
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
    """Show a simple status notification."""
    folder = get_download_folder() or "(no configurada)"
    try:
        icon.notify(
            f"Carpeta: {folder}\nPuerto: {PORT}\nVersion: {VERSION}",
            "Groove Sync Agent",
        )
    except Exception:
        log.info("Status — Folder: %s, Port: %d, Version: %s", folder, PORT, VERSION)


def _on_refresh_charts(icon, item):
    """Trigger immediate Beatport chart refresh."""
    log.info("Manual chart refresh requested")
    try:
        icon.notify("Renovando charts...", "Groove Sync Agent")
    except Exception:
        pass
    # Run scraping in a thread to not block the tray
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


def _on_exit(icon, item):
    log.info("Agent shutting down via tray menu")
    icon.stop()
    os._exit(0)


def run_tray(ready_event: threading.Event):
    """Run the system tray icon (blocking, runs on its own thread)."""
    icon = pystray.Icon(
        "groovesync",
        _create_tray_icon(),
        "Groove Sync Agent",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir carpeta", _on_open_folder),
            pystray.MenuItem("Configurar carpeta", _on_configure_folder),
            pystray.MenuItem("Renovar Charts", _on_refresh_charts),
            pystray.MenuItem("Estado", _on_status),
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


def main():
    log.info("=== Groove Sync Agent v%s starting ===", VERSION)

    # First run setup (folder picker)
    first_run_setup()

    # Start tray icon in a separate thread
    tray_ready = threading.Event()
    tray_thread = threading.Thread(target=run_tray, args=(tray_ready,), daemon=True)
    tray_thread.start()
    tray_ready.wait()
    log.info("System tray icon active")

    # Run the async HTTP server on the main thread's event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        runner = loop.run_until_complete(start_server())
        # Start Beatport scraper in background
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
