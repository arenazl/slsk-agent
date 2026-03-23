"""
Groove Sync Agent - Local file management agent for Groove Sync.
Runs as a Windows system tray application with an HTTP server on port 9900.
"""

import asyncio
import json
import logging
import os
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

VERSION = "1.0.0"
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

    # Update manifest
    entry = {
        "title": metadata.get("title", ""),
        "artist": metadata.get("artist", ""),
        "genre": genre or metadata.get("genre", ""),
        "key": metadata.get("key", ""),
        "bpm": metadata.get("bpm"),
        "rating": metadata.get("rating"),
        "size_mb": _file_size_mb(dest_path),
        "format": _detect_format(Path(filename).suffix),
    }
    if genre and metadata.get("genre") and genre != metadata.get("genre"):
        entry["manual_genre"] = True

    upsert_manifest(filename, entry)

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

    # Update manifest
    upsert_manifest(filename, {"genre": genre, "manual_genre": True})

    return web.json_response({"ok": True})


async def handle_library(request: web.Request):
    folder = get_download_folder()
    if not folder:
        return web.json_response([])

    root = Path(folder)
    if not root.exists():
        return web.json_response([])

    manifest = load_manifest()
    library = []

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if p.name == "manifest.json":
            continue

        # Determine genre from subfolder
        rel = p.relative_to(root)
        genre = str(rel.parent) if str(rel.parent) != "." else ""

        meta = manifest.get(p.name, {})
        library.append({
            "filename": p.name,
            "title": meta.get("title", ""),
            "artist": meta.get("artist", ""),
            "genre": meta.get("genre", genre),
            "key": meta.get("key", ""),
            "bpm": meta.get("bpm"),
            "rating": meta.get("rating"),
            "size_mb": _file_size_mb(p),
            "format": _detect_format(p.suffix),
            "manual_genre": meta.get("manual_genre", False),
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

    if new_folder:
        set_download_folder(new_folder)
        log.info("Download folder updated to: %s", new_folder)

    if not new_folder and not username:
        return web.json_response({"ok": False, "error": "Missing folder or username"}, status=400)

    return web.json_response({"ok": True})


async def handle_rate(request: web.Request):
    body = await request.json()
    filename = body.get("filename")
    rating = body.get("rating")

    if not filename:
        return web.json_response({"ok": False, "error": "Missing filename"}, status=400)
    if rating is None:
        return web.json_response({"ok": False, "error": "Missing rating"}, status=400)

    upsert_manifest(filename, {"rating": rating})
    log.info("Rated %s: %s", filename, rating)
    return web.json_response({"ok": True})


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

    remove_from_manifest(filename)
    return web.json_response({"ok": True})


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
        os.startfile(str(target))
        log.info("Opened folder: %s", target)
    except Exception as e:
        log.exception("Failed to open folder")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    return web.json_response({"ok": True})


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
    app.router.add_get("/api/open-folder", handle_open_folder)
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
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Selecciona tu carpeta de descargas")
    root.destroy()
    return folder if folder else None


def _on_open_folder(icon, item):
    folder = get_download_folder()
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        os.startfile(folder)
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
