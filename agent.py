"""
Groove Sync Agent - Local file management agent for Groove Sync.
Runs as a Windows system tray application with an HTTP server on port 9900.
"""

import asyncio
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import OrderedDict
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web

# SoulSeek client (optional — agente funciona sin él, pero sin esto los
# downloads delegados no funcionan. `pip install aioslsk` para activarlo).
try:
    from aioslsk.client import SoulSeekClient
    from aioslsk.settings import Settings, CredentialsSettings, SharesSettings
    from aioslsk.events import SearchResultEvent
    from aioslsk.protocol.primitives import AttributeKey
    AIOSLSK_AVAILABLE = True
except ImportError:
    AIOSLSK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "2.12.24"


def _ver_tuple(v):
    """Versión como tupla de enteros para comparar bien ('2.12.10' > '2.12.8').
    Comparar como string fallaba en el cruce de 1->2 dígitos (.9 vs .10)."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))
PORT = 9900
HOST = "127.0.0.1"  # local-only; prevents LAN exposure (was 0.0.0.0 pre-2.10.0)
ALLOWED_ORIGINS = [
    "https://djfreeapp.ar",
    "https://www.djfreeapp.ar",
    "https://groovesyncdj.netlify.app",  # legacy, kept for compat
    "https://slsk-ui.netlify.app",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
]
# Allow any *.djfreeapp.ar / *.netlify.app subdomain too
ALLOWED_ORIGIN_SUFFIXES = (".djfreeapp.ar", ".netlify.app")
# Backend hub. Default: Cloud Run (São Paulo). Override con AGENT_SERVER_URL
# migración Cloud Run→GCP: nunca más quemar la URL sola en el binario).
def _resolve_server_url():
    # 1. Override via Env Var
    env_override = os.environ.get("AGENT_SERVER_URL")
    if env_override:
        return env_override

    # 2. Override via config.json (in the same dir as the executable)
    exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    local_config = exe_dir / "config.json"
    if local_config.exists():
        try:
            with open(local_config, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("server_url"):
                    return data["server_url"]
        except Exception:
            pass

    # 3. Default Production Cloud Run (us-east4 — región oficial desde 2026-07-28)
    return "https://djfreeapp-api-730989854717.us-east4.run.app"

SERVER_URL = _resolve_server_url()
AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".wav", ".aif", ".aiff",
    ".m4a", ".ogg", ".aac", ".wma", ".opus",
}
CONFIG_DIR = Path.home() / ".djfreeapp"
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
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("djfreeapp")

# ---------------------------------------------------------------------------
# Subprocess helpers (hide cmd windows on Windows)
# ---------------------------------------------------------------------------


def _hidden_startupinfo():
    """Return a STARTUPINFO that hides the console window on Windows."""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# Monkey-patch subprocess.run and Popen to always hide windows on Windows
_original_run = subprocess.run
_original_popen_init = subprocess.Popen.__init__


def _patched_run(*args, **kwargs):
    if sys.platform == "win32" and "startupinfo" not in kwargs:
        kwargs["startupinfo"] = _hidden_startupinfo()
    if sys.platform == "win32" and "creationflags" not in kwargs:
        kwargs["creationflags"] = _NO_WINDOW
    return _original_run(*args, **kwargs)


def _patched_popen_init(self, *args, **kwargs):
    if sys.platform == "win32" and "startupinfo" not in kwargs:
        kwargs["startupinfo"] = _hidden_startupinfo()
    if sys.platform == "win32" and "creationflags" not in kwargs:
        kwargs["creationflags"] = _NO_WINDOW
    _original_popen_init(self, *args, **kwargs)


subprocess.run = _patched_run
subprocess.Popen.__init__ = _patched_popen_init


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


def _safe_join(folder: str, rel: str) -> Path | None:
    """Join `folder` and `rel` and verify the resolved path is inside `folder`.

    Returns None if `rel` tries to escape via `..`, absolute paths, or symlinks.
    Used to harden file-serving endpoints against path-traversal attacks.
    """
    try:
        root = Path(folder).resolve()
        target = (root / rel).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
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
        except Exception as ex:
            # Sin esto, un error crudo (no-HTTPException) se escapa SIN headers CORS y el
            # browser lo muestra como "No Access-Control-Allow-Origin", tapando el error real.
            log.error("[handler error] %s %s: %s", request.method, request.path, ex)
            resp = web.json_response({"error": str(ex)}, status=500)

    if origin in ALLOWED_ORIGINS or any(origin.endswith(suf) for suf in ALLOWED_ORIGIN_SUFFIXES):
        resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Access-Control-Request-Private-Network"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ---------------------------------------------------------------------------
# SoulSeek client (persistent connection, reused across downloads)
# ---------------------------------------------------------------------------

_slsk_client = None
_slsk_lock = asyncio.Lock()
_slsk_credentials = None  # (username, password) currently logged in
_slsk_login_at = 0.0      # epoch del último login (para detectar sesiones viejas)
_SLSK_SESSION_TTL = 300   # 5 min — re-loginar si la session es más vieja

async def get_slsk_client(username: str, password: str, force_relogin: bool = False):
    """Return a live SoulSeek client, reusing the existing login when possible.
    Connection is kept alive across calls — login is expensive and peer-reputation
    improves with long-lived connections (Nicotine+ pattern).

    `force_relogin=True` cierra el client actual y reconecta — usado cuando una
    search devuelve 0 results y sospechamos que la session expiró silenciosamente
    (aioslsk loguea WARNING "no valid session was set" pero no levanta exception).
    """
    global _slsk_client, _slsk_credentials, _slsk_login_at
    if not AIOSLSK_AVAILABLE:
        raise RuntimeError("aioslsk not installed. Run: pip install aioslsk")
    async with _slsk_lock:
        session_age = time.time() - _slsk_login_at
        session_alive = (
            _slsk_client is not None
            and _slsk_credentials == (username, password)
            and session_age < _SLSK_SESSION_TTL
            and not force_relogin
        )
        if session_alive:
            return _slsk_client
        # Creds changed, session vieja, o force → reconectar
        if _slsk_client is not None:
            try:
                await _slsk_client.stop()
            except Exception:
                pass
            _slsk_client = None
        folder = get_download_folder() or str(Path.home() / "Downloads")
        settings = Settings(
            credentials=CredentialsSettings(username=username, password=password),
            shares=SharesSettings(download=folder),
            network={
                # Range alto poco usado. Antes era 64321/64421 pero alguno se
                # bloqueaba en este python (probable Windows Firewall pidiendo
                # confirmacion para python.exe). 53xxx es menos colisionado.
                "listening": {"port": 53321, "port_range": 100},
                "listening_obfuscated": {"port": 53421, "port_range": 100},
            },
        )
        client = SoulSeekClient(settings)
        async def dummy_connect_ports():
            pass
        client.network.connect_listening_ports = dummy_connect_ports
        await client.start()
        await client.login()
        _slsk_client = client
        _slsk_credentials = (username, password)
        _slsk_login_at = time.time()
        log.info("SoulSeek client connected as %s (age reset, ttl=%ds)", username, _SLSK_SESSION_TTL)
        return _slsk_client


async def _report_progress(callback_url: str, payload: dict):
    """POST progress to Cloud Run so it broadcasts to UI via WS."""
    if not callback_url:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await s.post(callback_url, json=payload,
                         timeout=aiohttp.ClientTimeout(total=5))
    except Exception as e:
        log.debug("progress callback failed: %s", e)


def _write_tags(filepath, genre=None, artist=None, title=None, bpm=None, key=None):
    """Escribe tags en el archivo (mutagen). Solo escribe los campos provistos.
    Soporta FLAC/MP3/AIFF/WAV/M4A/OGG. Best-effort (no rompe si falla)."""
    try:
        from mutagen.flac import FLAC
        from mutagen.easyid3 import EasyID3
        from mutagen.aiff import AIFF
        from mutagen.wave import WAVE
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.id3 import TIT2, TPE1, TCON, TBPM, TKEY, ID3, error as ID3Error
    except Exception as e:
        log.warning("mutagen no disponible: %s", e)
        return False
    ext = Path(filepath).suffix.lower()
    bpm_s = None
    try:
        if bpm:
            bpm_s = str(int(round(float(bpm))))
    except Exception:
        bpm_s = None
    try:
        if ext in (".flac", ".ogg"):
            a = FLAC(str(filepath)) if ext == ".flac" else OggVorbis(str(filepath))
            if artist: a["artist"] = artist
            if title: a["title"] = title
            if genre: a["genre"] = genre
            if bpm_s: a["bpm"] = bpm_s
            if key: a["initialkey"] = key; a["key"] = key
            a.save()
        elif ext == ".mp3":
            try:
                a = EasyID3(str(filepath))
            except ID3Error:
                a = EasyID3(); a.save(str(filepath)); a = EasyID3(str(filepath))
            if artist: a["artist"] = artist
            if title: a["title"] = title
            if genre: a["genre"] = genre
            if bpm_s: a["bpm"] = bpm_s
            a.save()
            if key:
                t = ID3(str(filepath)); t.setall("TKEY", [TKEY(encoding=3, text=key)]); t.save()
        elif ext in (".aiff", ".aif", ".wav"):
            # Algunos .aif son en realidad WAV (root chunk RIFF) o viceversa; probamos
            # el contenedor que corresponde por extensión y caemos al otro si falla.
            a = None
            for C in ([WAVE, AIFF] if ext == ".wav" else [AIFF, WAVE]):
                try:
                    a = C(str(filepath)); break
                except Exception:
                    a = None
            if a is None:
                return False
            if a.tags is None:
                a.add_tags()
            if artist: a.tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
            if title: a.tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            if genre: a.tags.setall("TCON", [TCON(encoding=3, text=genre)])
            if bpm_s: a.tags.setall("TBPM", [TBPM(encoding=3, text=bpm_s)])
            if key: a.tags.setall("TKEY", [TKEY(encoding=3, text=key)])
            a.save()
        elif ext in (".m4a", ".aac"):
            a = MP4(str(filepath))
            if artist: a["\xa9ART"] = artist
            if title: a["\xa9nam"] = title
            if genre: a["\xa9gen"] = genre
            if bpm_s: a["tmpo"] = [int(bpm_s)]
            a.save()
        return True
    except Exception as e:
        log.warning("write tags fail %s: %s", filepath, e)
        return False


async def _curate_downloaded(folder, filename):
    """Tras bajar un tema: pide al server la metadata curada (Beatport + IA),
    escribe los tags y mueve el archivo a su carpeta de genero. Best-effort —
    si algo falla, el archivo queda bajado igual (sin curar)."""
    try:
        src = Path(folder) / filename
        if not src.exists():
            found = _find_file_in_library(filename)
            if not found:
                return
            src = found
        meta = None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{SERVER_URL}/api/curate-track",
                                  json={"filename": filename},
                                  timeout=aiohttp.ClientTimeout(total=45)) as r:
                    if r.status == 200:
                        meta = await r.json()
        except Exception as e:
            log.debug("curate request fail: %s", e)
        if not meta or meta.get("error"):
            return
        genre = (meta.get("genre") or "").strip()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write_tags, str(src), genre,
                                   meta.get("artist"), meta.get("title"),
                                   meta.get("bpm"), meta.get("key"))
        if genre:
            dest_dir = Path(folder) / genre
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            try:
                same = src.resolve() == dest.resolve()
            except Exception:
                same = (str(src) == str(dest))
            if not same and not dest.exists():
                shutil.move(str(src), str(dest))
                old = src.parent
                if old != Path(folder) and old.exists() and not any(old.iterdir()):
                    try: old.rmdir()
                    except Exception: pass
        log.info("Curated %s -> genre=%s bpm=%s key=%s (src=%s)",
                 filename, genre, meta.get("bpm"), meta.get("key"), meta.get("source"))
        # Duracion (ffprobe) -> manifest en CADA descarga, asi no hay que re-correr
        # el batch. El archivo quedo en su carpeta de genero (o en src si sin genero).
        try:
            final = (Path(folder) / genre / src.name) if genre else src
            if not final.exists():
                final = _find_file_in_library(filename) or src
            ffprobe = shutil.which("ffprobe")
            if ffprobe and final and final.exists():
                out = await asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(
                    [ffprobe, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                    capture_output=True, text=True, timeout=20).stdout.strip())
                if out:
                    import aiohttp as _ah
                    async with _ah.ClientSession() as s2:
                        await s2.post(f"{SERVER_URL}/api/set-duration",
                                      json={"filename": filename, "duration": round(float(out), 1), "username": _tunnel_user or ""},
                                      timeout=_ah.ClientTimeout(total=20))
        except Exception as e:
            log.debug("set-duration fail: %s", e)
    except Exception as e:
        log.warning("curate_downloaded fail: %s", e)


async def _run_slsk_download(username, password, sources, filename, callback_url):
    """Background task: try each source with fail-fast; report progress."""
    async def report(status, **kw):
        # via="agent" — la UI se entera que el archivo ya está en disco local
        # y no intenta hacer fetch a Cloud Run /audio (que daría 404).
        await _report_progress(callback_url, {"filename": filename, "status": status, "via": "agent", **kw})

    folder = get_download_folder()
    if not folder:
        await report("error", message="No download folder configured")
        return

    try:
        client = await get_slsk_client(username, password)
    except Exception as e:
        await report("error", message=f"connect: {str(e)[:120]}")
        return

    tried_users = set()
    for src_idx, src in enumerate(sources):
        peer = src.get("username") or src.get("peer")
        remote_path = src.get("remote_path")
        if not peer or not remote_path or peer in tried_users:
            continue
        tried_users.add(peer)
        queue = src.get("queue", 0) or 0
        has_slots = bool(src.get("free_slots"))
        is_last = (src_idx >= len(sources) - 1)

        await report("queued", source=peer, queue=queue,
                     source_idx=src_idx + 1, source_total=len(sources))

        try:
            transfer = await client.transfers.download(peer, remote_path)
        except Exception as e:
            log.debug("download init failed for %s: %s", peer, e)
            await report("error_source", source=peer, message=str(e)[:100])
            continue

        last_bytes = 0
        stall_count = 0
        # Give peers 90 seconds to accept handshakes and open upload slots
        if is_last:
            MAX_STALL = 300
        elif queue > 500:
            MAX_STALL = 180
        else:
            MAX_STALL = 90
        transfer_started = False
        last_status_sec = 0

        for elapsed in range(7200):  # max 2h safety
            await asyncio.sleep(1)
            try:
                if transfer.is_finalized():
                    break
            except Exception:
                pass
            current = getattr(transfer, "bytes_transfered", 0) or 0
            if current > last_bytes:
                if not transfer_started:
                    transfer_started = True
                    MAX_STALL = max(MAX_STALL, 120)  # be patient once serving
                last_bytes = current
                stall_count = 0
                if transfer.filesize:
                    pct = int(current / transfer.filesize * 100)
                    speed_kb = int((getattr(transfer, "speed", 0) or 0) / 1024)
                    await report("downloading", pct=pct, speed=speed_kb, source=peer)
            else:
                stall_count += 1
                if stall_count - last_status_sec >= 15:
                    last_status_sec = stall_count
                    await report("queued", source=peer, queue=queue,
                                 wait_secs=stall_count, timeout_secs=MAX_STALL,
                                 source_idx=src_idx + 1, source_total=len(sources))
                if stall_count >= MAX_STALL:
                    break

        try:
            finalized_ok = transfer.is_finalized() and transfer.is_transfered()
        except Exception:
            finalized_ok = False
        if finalized_ok:
            # Real on-disk filename — aioslsk uses the remote path's basename,
            # which can differ from `filename` (the original search request name).
            # The UI keys searchDlStatus by `filename` but needs `local_name` to
            # check the file actually landed in local storage.
            local_name = remote_path.rsplit("\\", 1)[-1] if "\\" in remote_path else remote_path.rsplit("/", 1)[-1]
            await report("completed", source=peer, local_name=local_name)
            # Curación automática: tags (Beatport+IA via server) + ubicar en
            # carpeta de género. En background para no demorar el "completed".
            try:
                folder = get_download_folder()
                if folder:
                    asyncio.create_task(_curate_downloaded(folder, local_name))
            except Exception:
                pass
            return

    await report("error", message="no sources succeeded")


_AUDIO_EXTS = {"mp3", "flac", "wav", "aiff", "aif", "m4a", "ogg", "opus"}


def _build_search_ladder(query: str) -> list:
    """Genera queries progresivamente más simples — replica la lógica del
    server. Iteramos en orden hasta encontrar resultados con cola baja."""
    STOPWORDS = {'feat', 'featuring', 'ft', 'with', 'and', 'the', 'a', 'an',
                 'mix', 'extended', 'original', 'radio', 'edit', 'club',
                 'remix', 'version', 'vocal', 'instrumental', 'dub', 'vip',
                 'rework', 'bootleg'}
    queries = []
    base = re.sub(r'\([^)]*\)', ' ', query)
    base = re.sub(r'\[[^\]]*\]', ' ', base)
    for suf in ['Extended Mix', 'Original Mix', 'Radio Edit', 'Club Mix',
                'Remix', 'Extended', 'Original']:
        base = re.sub(rf'\b{re.escape(suf)}\b', ' ', base, flags=re.IGNORECASE)
    base = re.sub(r'\s+', ' ', base).strip()

    artist_part, title_part = None, None
    for sep in [' - ', ' – ', ' — ']:
        if sep in base:
            parts = base.split(sep, 1)
            artist_part = parts[0].strip()
            title_part = parts[1].strip() if len(parts) > 1 else ''
            break

    def _strip(s): return re.sub(r'[^\w\s]', ' ', s).strip()

    queries.append(_strip(base))
    if artist_part and title_part:
        artists = re.split(r'\s*(?:,|&|\sand\s|\sft\.?\s|\sfeat\.?\s|\swith\s)\s*',
                           artist_part, flags=re.IGNORECASE)
        artists = [a.strip() for a in artists if a.strip()]
        if len(artists) > 1:
            queries.append(_strip(f"{artists[-1]} {title_part}"))
            queries.append(_strip(f"{artists[0]} {title_part}"))
        queries.append(_strip(title_part))
        title_words = [w for w in _strip(title_part).lower().split()
                       if len(w) >= 3 and w not in STOPWORDS]
        if len(title_words) >= 2:
            queries.append(' '.join(title_words[:3]))
    else:
        words = _strip(base).split()
        if len(words) > 2:
            distinctive = [w for w in words if len(w) >= 3 and w.lower() not in STOPWORDS]
            if len(distinctive) >= 2:
                queries.append(' '.join(distinctive[:3]))

    # Dedup preserving order
    seen, out = set(), []
    for q in queries:
        q = re.sub(r'\s+', ' ', q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


async def _run_slsk_search(username: str, password: str, query: str, search_wait: int = 20):
    """Search SoulSeek for `query` and return a list of audio candidates.
    Replica simplificada de _search_soulseek_impl del server, pero corre
    en la red local del usuario → ve peers normales que Cloud Run no alcanza."""
    client = await get_slsk_client(username, password)

    _stopw = {"mix", "the", "and", "ext", "original", "feat", "featuring",
              "remix", "extended", "edit", "club", "radio", "vocal"}

    import unicodedata
    def _strip_accents(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

    # El artist de Beatport suele ser fake ("Jesus Fernandez" para una cover
    # de Danny Tenaglia). Por eso recalculamos artist_kw/title_kw POR ITERACIÓN:
    # query 1 (full) usa artist filter; query 2+ (title only) lo ignora.
    def _kw_for(q: str):
        q_clean = _strip_accents(q)
        if " - " in q_clean:
            _parts = q_clean.split(" - ", 1)
            ap = _parts[0]
            tp = _parts[1] if len(_parts) > 1 else ""
        else:
            ap = ""
            tp = q_clean
        ak = [w for w in re.sub(r'[^\w\s]', ' ', ap.lower()).split()
              if len(w) >= 3 and w not in _stopw]
        tk = [w for w in re.sub(r'[^\w\s]', ' ', tp.lower()).split()
              if len(w) >= 3 and w not in _stopw]
        return ak, tk

    # Ladder de queries: si la primera (más específica) no trae resultados con
    # cola baja, probamos progresivamente más simples — igual que el server.
    search_queries = _build_search_ladder(query)
    log.info("[agent search] query=%r ladder=%s", query, search_queries)

    # El artist_kw lo derivamos UNA VEZ del query ORIGINAL (que tiene "Artist - Title"),
    # porque las queries del ladder se simplifican y pierden el separador. Sin esto,
    # un track "Jesus Fernandez - Music Is The Answer" devuelve también las versiones
    # de Danny Tenaglia (matchean por title) — el user quiere SOLO el artist pedido.
    orig_artist_kw, _ = _kw_for(query)

    candidates = []

    # _artist_kw queda fijo (siempre filtra por el artist original).
    # _title_kw se recalcula por iteración (sigue al ladder).
    _artist_kw, _title_kw = list(orig_artist_kw), []

    async def on_result(event):
        for file in event.result.shared_items:
            fname_full = file.filename
            fname = fname_full.rsplit("\\", 1)[-1] if "\\" in fname_full else fname_full.rsplit("/", 1)[-1]
            ext_raw = (file.extension or fname.rsplit(".", 1)[-1] if "." in fname else "").lower()
            if ext_raw not in _AUDIO_EXTS:
                continue

            # Relevance check — MÁS LAXO que el server. Queremos traer MUCHOS
            # candidates (~20-50) porque la mayoría tiene NAT y "indirect
            # connection timed out". Más candidates = más chance de pegar uno
            # que conecte. Filter actual: artist match obligatorio + al menos
            # 1 keyword de title (no exigimos 50% match).
            full_norm = re.sub(r'[^\w\s]', ' ', (fname + ' ' + fname_full).lower())
            if _artist_kw:
                if not any(w in full_norm for w in _artist_kw):
                    continue
            if _title_kw:
                if not any(w in full_norm for w in _title_kw):
                    continue

            attrs = file.get_attribute_map()
            bitrate = attrs.get(AttributeKey.BITRATE, 0) or 0
            duration = attrs.get(AttributeKey.DURATION, 0) or 0
            size_mb = (file.filesize or 0) / (1024 * 1024)

            candidates.append({
                "username": event.result.username,
                "filename": fname,
                "remote_path": fname_full,
                "ext": ext_raw.upper(),
                "size_mb": round(size_mb, 1),
                "bitrate": bitrate,
                "duration": duration,
                "free_slots": event.result.has_free_slots,
                "speed": event.result.avg_speed or 0,
                "queue": event.result.queue_size or 0,
            })

    # Mandamos TODAS las queries del ladder en paralelo y dejamos al filter de
    # _artist_kw (siempre constante) decidir qué entra. Lanzar en paralelo es
    # mucho más eficiente que esperar 20s por query secuencial y junta más
    # peers (algunos solo responden a queries más cortas).
    _artist_kw = list(orig_artist_kw)
    # El _title_kw lo cubrimos con las palabras de TODOS los queries del ladder
    # (unión) — más laxo, pero el artist filter ya restringe lo suficiente.
    _title_kw = []
    for sq in search_queries:
        _, tk = _kw_for(sq)
        for w in tk:
            if w not in _title_kw:
                _title_kw.append(w)

    client.events.register(SearchResultEvent, on_result)
    search_reqs = []
    try:
        for sq in search_queries:
            try:
                req = await client.searches.search(sq)
                search_reqs.append(req)
            except Exception as e:
                log.warning("[agent search] failed to start %r: %s", sq, e)
        await asyncio.sleep(search_wait)
    finally:
        client.events.unregister(SearchResultEvent, on_result)
        for req in search_reqs:
            try:
                client.searches.remove_request(req)
            except Exception:
                pass
    log.info("[agent search] parallel ladder done: %d queries akw=%s tkw=%s -> %d raw candidates",
             len(search_queries), _artist_kw, _title_kw, len(candidates))

    # Dedupe sources of the same logical file (same ext + duration ±5s + size ±10%)
    grouped = []
    for c in candidates:
        merged = False
        for g in grouped:
            same_ext = c["ext"] == g["ext"]
            same_dur = abs((c["duration"] or 0) - (g["duration"] or 0)) <= 5
            sa, sb = c["size_mb"], g["size_mb"]
            same_size = sa and sb and abs(sa - sb) / max(sa, sb) <= 0.10
            if same_ext and same_dur and same_size:
                g.setdefault("sources", [g.copy()])
                g["sources"].append(c)
                if (c.get("queue", 9999) < g.get("queue", 9999)) or (c.get("free_slots") and not g.get("free_slots")):
                    g["username"] = c["username"]
                    g["queue"] = c["queue"]
                    g["free_slots"] = c["free_slots"]
                    g["speed"] = c["speed"]
                merged = True
                break
        if not merged:
            grouped.append(c)
    for g in grouped:
        g["source_count"] = len(g.get("sources", [g]))

    # Sort: free slots first, then queue ASC, then quality DESC (FLAC > MP3)
    def _qual(r):
        e = r.get("ext", "").lower()
        if e in ("flac", "wav"): return 1000
        if e in ("aiff", "aif"): return 900
        if e == "mp3": return 300 + min(r.get("bitrate", 0) or 0, 320)
        return 100
    grouped.sort(key=lambda r: (
        0 if r.get("free_slots") else 1,
        r.get("queue", 9999),
        -_qual(r),
    ))
    return grouped


async def handle_slsk_search(request: web.Request):
    """Search SoulSeek via the local agent (sees peers Cloud Run NAT can't reach).
    Body: {username, password, query, wait?:20}. Returns {ok, results:[...]}.
    Synchronous — UI awaits the full result list."""
    if not AIOSLSK_AVAILABLE:
        return web.json_response(
            {"ok": False, "error": "aioslsk not installed on agent"},
            status=501,
        )
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    cfg = load_config()
    username = data.get("username") or cfg.get("username")
    password = data.get("password") or cfg.get("password")
    query = (data.get("query") or "").strip()
    wait_s = int(data.get("wait") or 20)

    if not username or not password or not query:
        return web.json_response(
            {"ok": False, "error": "Missing username/password/query"},
            status=400,
        )

    try:
        try:
            results = await _run_slsk_search(username, password, query, search_wait=wait_s)
        except Exception as e:
            err = str(e).lower()
            # aioslsk a veces queda con un _slsk_client cacheado en estado
            # roto (listening port no abre la primera vez post-boot, o el
            # cliente se desconecto sin invalidar el cache). Force-relogin
            # reinstancia el cliente y casi siempre lo desbloquea.
            if "listening port" in err or "no valid session" in err or "connection" in err:
                log.warning("[slsk-search] exc=%s — force relogin and retry", str(e)[:120])
                await get_slsk_client(username, password, force_relogin=True)
                results = await _run_slsk_search(username, password, query, search_wait=wait_s)
            else:
                raise
        # Si vino 0 resultados, la session pudo haber expirado silenciosamente
        # (aioslsk: "WARNING: not returning search results: no valid session").
        # Retry UNA vez forzando re-login del SoulSeek client.
        if not results:
            log.warning("[slsk-search] 0 results — force relogin and retry")
            await get_slsk_client(username, password, force_relogin=True)
            results = await _run_slsk_search(username, password, query, search_wait=wait_s)
    except Exception as e:
        log.exception("slsk-search error")
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)

    return web.json_response({"ok": True, "results": results, "count": len(results)})


async def handle_slsk_download(request: web.Request):
    """Delegated download entry point. Body: {username, password, filename,
    sources:[{username,remote_path,queue,free_slots,speed}], callback_url}.
    Returns immediately; progress streams to callback_url on Cloud Run."""
    if not AIOSLSK_AVAILABLE:
        return web.json_response(
            {"ok": False, "error": "aioslsk not installed on agent"},
            status=501,
        )
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    cfg = load_config()
    username = data.get("username") or cfg.get("username")
    password = data.get("password") or cfg.get("password")
    sources = data.get("sources") or []
    filename = data.get("filename")
    callback_url = data.get("callback_url") or ""

    if not username or not password or not filename or not sources:
        return web.json_response(
            {"ok": False, "error": "Missing username/password/filename/sources"},
            status=400,
        )

    asyncio.create_task(_run_slsk_download(username, password, sources, filename, callback_url))
    return web.json_response({"ok": True, "status": "started", "source_count": len(sources)})


async def handle_slsk_reconnect(request: web.Request):
    """Fuerza el re-login del cliente de Soulseek para renovar la sesión.
    Body opcional: {username, password}. Retorna {ok: True, message: "Sesión renovada"}.
    """
    if not AIOSLSK_AVAILABLE:
        return web.json_response({"ok": False, "error": "aioslsk not installed"}, status=501)
    try:
        data = await request.json()
    except Exception:
        data = {}
    cfg = load_config()
    username = data.get("username") or cfg.get("username") or "arenazl"
    password = data.get("password") or cfg.get("password") or "look"

    try:
        await get_slsk_client(username, password, force_relogin=True)
        _show_msg("Soulseek", f"Sesión de Soulseek renovada para {username}")
        log.info("[slsk-reconnect] Sesión de Soulseek renovada con éxito para %s", username)
        return web.json_response({"ok": True, "message": f"Sesión de Soulseek renovada para {username}"})
    except Exception as e:
        log.error("[slsk-reconnect] Error al renovar sesión: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


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
        "slsk": AIOSLSK_AVAILABLE,
        "tunnel": {
            "running": _tunnel_task is not None and not _tunnel_task.done(),
            "user": _tunnel_user,
            "connected": _tunnel_connected,
        },
    })


# ─── WS reverse tunnel (cliente) ─────────────────────────
# Mantiene una conexion WS saliente al server (Cloud Run) por la cual el
# server le manda HTTP requests que el agente ejecuta localmente y
# devuelve por el mismo canal. Sin Tailscale, sin puerto abierto.
_tunnel_task = None              # asyncio.Task | None
_tunnel_user: str | None = None
_tunnel_started_at: float | None = None
_tunnel_connected: bool = False


async def _tunnel_dispatch(data: dict) -> dict:
    """Reemite un http_request hacia 127.0.0.1:PORT y devuelve la respuesta.
    Usar el propio HTTP loopback nos da el routing y middlewares gratis."""
    import aiohttp
    import base64 as _b64
    method = data.get("method", "GET")
    path = data.get("path", "/")
    query = data.get("query") or {}
    in_headers = data.get("headers") or {}
    body_b64 = data.get("body_b64")
    body = _b64.b64decode(body_b64) if body_b64 else None
    url = f"http://127.0.0.1:{PORT}{path}"
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.request(method, url, params=query,
                                 headers=in_headers, data=body) as resp:
                resp_body = await resp.read()
                out_headers = {k: v for k, v in resp.headers.items()
                               if k.lower() not in ("content-length", "transfer-encoding", "connection")}
                return {
                    "type": "http_response",
                    "request_id": data.get("request_id"),
                    "status": resp.status,
                    "headers": out_headers,
                    "body_b64": _b64.b64encode(resp_body).decode("ascii"),
                }
    except Exception as e:
        log.warning("[TUNNEL] dispatch %s %s failed: %s", method, path, e)
        return {
            "type": "http_response",
            "request_id": data.get("request_id"),
            "status": 502,
            "headers": {"Content-Type": "application/json"},
            "body_b64": _b64.b64encode(json.dumps({"error": str(e)}).encode()).decode("ascii"),
        }


async def _tunnel_loop(username: str):
    """Conecta WS a Cloud Run y queda escuchando. Reconnect con backoff."""
    global _tunnel_connected
    import aiohttp
    ws_url = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/agent-tunnel?u={username}"
    backoff = 1.0
    while True:
        try:
            log.info("[TUNNEL] connecting to %s", ws_url)
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(
                    ws_url, heartbeat=30.0,
                    max_msg_size=64 * 1024 * 1024,
                ) as ws:
                    _tunnel_connected = True
                    backoff = 1.0
                    log.info("[TUNNEL] connected as %s", username)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue
                            if data.get("type") == "http_request":
                                # En background: no bloquea el read loop si el
                                # dispatch tarda (downloads grandes, slsk search).
                                async def _runone(d=data, w=ws):
                                    resp = await _tunnel_dispatch(d)
                                    try:
                                        await w.send_json(resp)
                                    except Exception as se:
                                        log.warning("[TUNNEL] send response failed: %s", se)
                                asyncio.create_task(_runone())
                            elif data.get("type") == "pong":
                                pass
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except asyncio.CancelledError:
            log.info("[TUNNEL] cancelled")
            raise
        except Exception as e:
            log.warning("[TUNNEL] connection error: %s", e)
        _tunnel_connected = False
        log.info("[TUNNEL] disconnected — retry in %.1fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


async def handle_tunnel_start(request: web.Request):
    """POST {user}. Arranca (o reabre) el tunnel WS al server con ese username.
    Idempotente: si ya corre con el mismo user, devuelve already_running."""
    global _tunnel_task, _tunnel_user, _tunnel_started_at
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = (body.get("user") or request.query.get("user") or "").strip()
    if not user:
        return web.json_response({"ok": False, "error": "missing user"}, status=400)
    if _tunnel_task and not _tunnel_task.done() and _tunnel_user == user:
        return web.json_response({"ok": True, "user": user, "status": "already_running"})
    if _tunnel_task and not _tunnel_task.done():
        _tunnel_task.cancel()
        try:
            await _tunnel_task
        except Exception:
            pass
    _tunnel_user = user
    _tunnel_started_at = time.time()
    _tunnel_task = asyncio.create_task(_tunnel_loop(user))
    return web.json_response({"ok": True, "user": user, "status": "started"})


async def handle_tunnel_stop(request: web.Request):
    global _tunnel_task, _tunnel_user, _tunnel_connected
    if _tunnel_task and not _tunnel_task.done():
        _tunnel_task.cancel()
        try:
            await _tunnel_task
        except Exception:
            pass
    prev_user = _tunnel_user
    _tunnel_task = None
    _tunnel_user = None
    _tunnel_connected = False
    return web.json_response({"ok": True, "stopped_user": prev_user})


async def handle_tunnel_status(request: web.Request):
    return web.json_response({
        "running": _tunnel_task is not None and not _tunnel_task.done(),
        "user": _tunnel_user,
        "connected": _tunnel_connected,
        "started_at": _tunnel_started_at,
        "server_url": SERVER_URL,
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

    # Drag manual = recategorizar: grabar el tag de genero = carpeta destino,
    # asi Rekordbox lo lee. (genre vacio = volver a la raiz, no tocamos el tag.)
    if genre:
        try:
            _write_tags(str(dest), genre=genre)
        except Exception as e:
            log.debug("move-file genre tag fail: %s", e)

    # Actualizar el manifest (género) y sincronizarlo a la NUBE, así la app refleja el
    # cambio sin re-escanear y la carpeta física + el manifest quedan consistentes.
    synced = False
    try:
        upsert_manifest(filename, {"genre": genre})
        manifest = load_manifest()
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{SERVER_URL}/api/sync-manifest",
                              json={"manifest": manifest, "username": _tunnel_user or ""},
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                await r.read()
                synced = r.status == 200
    except Exception as e:
        log.warning("move-file manifest sync fail: %s", e)

    return web.json_response({"ok": True, "synced": synced})


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
    """Deprecated: ratings now go to Cloud Run/Cloudinary. Kept for backwards compat."""
    return web.json_response({"ok": True, "deprecated": True})


async def handle_delete(request: web.Request):
    body = await request.json()
    filename = body.get("filename")
    title = body.get("title", "")

    filepath = None
    if filename:
        filepath = _find_file_in_library(filename)

    if not filepath and title:
        folder = get_download_folder()
        if folder:
            root = Path(folder)
            t_norm = re.sub(r'[^a-z0-9]', '', title.lower())
            for p in root.rglob("*"):
                if p.is_file():
                    p_norm = re.sub(r'[^a-z0-9]', '', p.name.lower())
                    if t_norm and t_norm in p_norm:
                        filepath = p
                        break

    if filepath and filepath.exists():
        parent = filepath.parent
        filepath.unlink()
        log.info("Deleted: %s", filepath)

        # Clean up empty directory
        folder = get_download_folder()
        if folder and parent != Path(folder) and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except Exception:
                pass
        return web.json_response({"ok": True, "deleted": str(filepath)})
    else:
        log.warning("File not found for deletion: filename=%s title=%s", filename, title)

    return web.json_response({"ok": True, "deleted": False})


def _trash_file(path):
    """Manda el archivo a la Papelera de Windows (reversible). Fallback a borrado
    definitivo solo si send2trash no esta disponible."""
    try:
        from send2trash import send2trash as _s2t
        _s2t(str(path))
    except Exception:
        Path(path).unlink()


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
                _trash_file(filepath)
                deleted_count += 1
                deleted_files.append(fname)
                log.info("Trashed dupe: %s", filepath)
                # Clean up empty directory
                if parent != download_dir and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception as e:
                log.error("Error deleting %s: %s", fname, e)

    return web.json_response({"ok": True, "deleted": deleted_count, "files": deleted_files})


async def handle_write_tags(request: web.Request):
    """Reescribe los tags fisicos (mutagen) de archivos existentes — para que la
    curacion de metadata llegue al archivo y Rekordbox la lea.
    Body {files: [{filename, artist?, title?, genre?, key?, bpm?}]}."""
    body = await request.json()
    items = body.get("files", []) or []
    written = 0
    for it in items:
        fn = it.get("filename")
        if not fn:
            continue
        fp = _find_file_in_library(fn)
        if not fp or not fp.exists():
            continue
        try:
            _write_tags(str(fp), genre=it.get("genre"), artist=it.get("artist"),
                        title=it.get("title"), bpm=it.get("bpm"), key=it.get("key"))
            written += 1
        except Exception as e:
            log.error("write-tags %s: %s", fn, e)
    return web.json_response({"ok": True, "written": written})


def _meta_is_dirty(info):
    a = (info.get("artist") or "").strip()
    t = (info.get("title") or "").strip()
    return (not a) or (not t) or ("_" in t) or len(t) > 70 or bool(re.match(r"^\s*\d{1,3}[\.\-_ ]", t))


async def handle_fix_metadata(request: web.Request):
    """Arregla los metatags de la biblioteca y los baja al ARCHIVO físico (para que
    Rekordbox los lea). Pasos:
      1) baja el manifest curado (Cloudinary, via server),
      2) para cada archivo con artista vacío o título sucio: lo cura con
         /api/curate-track (Beatport+IA) SIN pisar el género ya asignado,
      3) escribe los tags DENTRO del archivo con mutagen (artista/título/género/key/bpm),
      4) sube el manifest si cambió.
    Body {username?}. Devuelve {fixed_meta, tags_written, total, errors}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)
    username = (body.get("username") or load_config().get("username") or "").strip()

    import aiohttp
    # 1) manifest curado desde el server
    manifest = {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{SERVER_URL}/api/metadata", params={"user": username},
                             timeout=aiohttp.ClientTimeout(total=40)) as r:
                if r.status == 200:
                    manifest = await r.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"manifest: {e}"}, status=502)
    if not isinstance(manifest, dict):
        manifest = {}

    root = Path(folder)
    files = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS or p.name == "manifest.json":
            continue
        rel = p.relative_to(root)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        if top.lower() in IGNORE_DIRS:
            continue
        files.append(p)

    fixed_meta = 0
    tags_written = 0
    errors = 0
    changed = False
    loop = asyncio.get_event_loop()

    async with aiohttp.ClientSession() as s:
        for p in files:
            info = dict(manifest.get(p.name, {}) or {})
            # 2) curar si está incompleto (sin pisar género)
            if _meta_is_dirty(info):
                meta = None
                try:
                    async with s.post(f"{SERVER_URL}/api/curate-track",
                                      json={"artist": info.get("artist", ""), "title": info.get("title", ""), "filename": p.name},
                                      timeout=aiohttp.ClientTimeout(total=45)) as r:
                        if r.status == 200:
                            meta = await r.json()
                except Exception:
                    meta = None
                if meta and not meta.get("error"):
                    cur_t = info.get("title") or ""
                    title_dirty = (not cur_t.strip()) or ("_" in cur_t) or len(cur_t) > 70
                    if not (info.get("artist") or "").strip() and meta.get("artist"):
                        info["artist"] = meta["artist"]; changed = True
                    if title_dirty and meta.get("title"):
                        info["title"] = meta["title"]; changed = True
                    if not (info.get("key") or "").strip() and meta.get("key"):
                        info["key"] = meta["key"]; changed = True
                    if not info.get("bpm") and meta.get("bpm"):
                        info["bpm"] = meta["bpm"]; changed = True
                    if not (info.get("genre") or "").strip() and meta.get("genre"):
                        info["genre"] = meta["genre"]; changed = True
                    manifest[p.name] = info
                    fixed_meta += 1
            # 3) escribir los tags DENTRO del archivo (con los valores del manifest)
            if info.get("artist") or info.get("title") or info.get("genre") or info.get("key"):
                ok = await loop.run_in_executor(
                    None, _write_tags, str(p), info.get("genre"), info.get("artist"),
                    info.get("title"), info.get("bpm"), info.get("key"))
                if ok:
                    tags_written += 1
                else:
                    errors += 1

    # 4) subir manifest si cambió
    if changed:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{SERVER_URL}/api/sync-manifest",
                                  json={"manifest": manifest, "username": username},
                                  timeout=aiohttp.ClientTimeout(total=60)) as r:
                    await r.read()
        except Exception as e:
            log.warning("fix-metadata sync-manifest fail: %s", e)

    log.info("fix-metadata: %s curados, %s tags escritos, %s errores (de %s)",
             fixed_meta, tags_written, errors, len(files))
    return web.json_response({"ok": True, "fixed_meta": fixed_meta, "tags_written": tags_written,
                              "total": len(files), "errors": errors})


_ILLEGAL_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_genre(name: str) -> str:
    if not name:
        return ""
    cleaned = _ILLEGAL_WIN_CHARS.sub("", name).strip().rstrip(". ")
    return cleaned[:80]


async def handle_organize(request: web.Request):
    body = await request.json()
    moves = body.get("moves", [])
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    download_dir = Path(folder)
    moved_count = 0
    skipped = 0

    for move in moves:
        fname = move.get("filename")
        genre = _sanitize_genre(move.get("genre", ""))
        if not fname or not genre:
            skipped += 1
            continue
        try:
            filepath = _find_file_in_library(fname)
            if not (filepath and filepath.exists()):
                skipped += 1
                continue
            dest_dir = download_dir / genre
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / filepath.name
            if dest != filepath:
                filepath.rename(dest)
                moved_count += 1
                log.info("Moved %s -> %s", filepath.name, genre)
        except Exception as e:
            log.warning("organize: failed %s -> %s: %s", fname, genre, e)
            skipped += 1

    return web.json_response({"ok": True, "moved": moved_count, "skipped": skipped})


async def handle_open_folder(request: web.Request):
    folder = get_download_folder()
    if not folder:
        return web.json_response({"ok": False, "error": "No folder configured"}, status=400)

    subfolder = request.query.get("folder", "")
    file_name = request.query.get("file", "")
    target = Path(folder)
    if subfolder:
        target = target / subfolder
    target.mkdir(parents=True, exist_ok=True)

    # Si vino `file`, el caller queria resaltar ese archivo especifico.
    # Si NO existe, devolvemos 404 con mensaje claro en vez de fallback a
    # "abrir la carpeta" — sino el user ve la carpeta abrirse y cree que
    # algo anda mal porque no esta el tema seleccionado.
    file_path = (target / file_name) if file_name else None
    try:
        if file_name:
            if file_path and file_path.exists():
                _reveal_path(str(file_path))
                log.info("Revealed file: %s", file_path)
            else:
                log.info("File not found, returning 404: %s", file_path)
                return web.json_response(
                    {"ok": False, "error": f"Archivo no encontrado: {file_name}"},
                    status=404,
                )
        else:
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
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Range, Access-Control-Request-Private-Network",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length",
        # Private Network Access: Chrome bloquea pedidos a localhost desde paginas HTTPS
        # salvo que la respuesta lo permita explicitamente (audio del agente).
        "Access-Control-Allow-Private-Network": "true",
    }
    if origin in ALLOWED_ORIGINS or any(origin.endswith(suf) for suf in ALLOWED_ORIGIN_SUFFIXES):
        h["Access-Control-Allow-Origin"] = origin
    return h


# Reproduccion rapida: los WAV/AIFF/FLAC grandes (sin comprimir o pesados) el
# navegador los buferea/decodifica casi enteros antes de arrancar -> 5-10s de
# espera aunque el archivo este local. Para escuchar en la app los
# transcodificamos al vuelo a MP3 192k (stream): arranca al toque (~130ms) y
# pesa ~12x menos. El archivo original NUNCA se toca (queda lossless en disco
# para Rekordbox/mezclar). Solo afecta la previsualizacion en la app.
_TRANSCODE_BITRATE = 192                          # kbps (CBR)
_TRANSCODE_BPS = _TRANSCODE_BITRATE * 1000 // 8   # bytes/seg (24000) para mapear byte<->tiempo


def _probe_duration(path) -> float:
    """Duracion en segundos via ffprobe (0.0 si falla)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return float(out or 0)
    except Exception:
        return 0.0


async def _stream_transcoded(request: web.Request, target: Path, cors: dict):
    """Transcodifica al vuelo a MP3 192k para arranque instantaneo.
    Seek aproximado via Range: con CBR, byte_offset / _TRANSCODE_BPS = segundos,
    asi mapeamos el Range del navegador a un -ss de ffmpeg. Devuelve None si no
    hay ffmpeg (el caller cae al servido directo)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    duration = _probe_duration(target)
    total = int(duration * _TRANSCODE_BPS) if duration else 0

    start = 0
    range_header = request.headers.get("Range", "")
    m = re.match(r"bytes=(\d+)-", range_header)
    if m:
        start = int(m.group(1))
    start_time = start / _TRANSCODE_BPS if start else 0.0

    cmd = [ffmpeg, "-nostdin", "-v", "error"]
    if start_time > 0.05:
        cmd += ["-ss", f"{start_time:.3f}"]
    cmd += ["-i", str(target), "-vn", "-map", "0:a:0",
            "-c:a", "libmp3lame", "-b:a", f"{_TRANSCODE_BITRATE}k", "-f", "mp3", "-"]

    headers = {**cors, "Content-Type": "audio/mpeg", "Accept-Ranges": "bytes"}
    if total:
        if range_header:
            end = total - 1
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(total - start)
            status = 206
        else:
            headers["Content-Length"] = str(total)
            status = 200
    else:
        status = 206 if range_header else 200

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)

    cap = (total - start) if total else None   # no exceder el Content-Length prometido
    sent = 0
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            if cap is not None:
                room = cap - sent
                if room <= 0:
                    break
                if len(chunk) > room:
                    chunk = chunk[:room]
            await resp.write(chunk)
            sent += len(chunk)
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await resp.write_eof()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    return resp


async def handle_audio(request: web.Request):
    """Stream an audio file from the library."""
    folder = get_download_folder()
    if not folder:
        return web.json_response({"error": "No folder configured"}, status=400)

    rel = request.match_info.get("path", "")
    if not rel:
        return web.json_response({"error": "Missing file path"}, status=400)

    # Try direct path first, then search by filename
    target = _safe_join(folder, rel)
    if not target or not target.exists() or not target.is_file():
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

    # Reproduccion rapida en la app: transcodificar formatos pesados a MP3 al
    # vuelo (los .mp3 ya son livianos -> se sirven directo). El original intacto.
    if request.query.get("fast") == "1" and target.suffix.lower() != ".mp3":
        transcoded = await _stream_transcoded(request, target, cors)
        if transcoded is not None:
            return transcoded
        # sin ffmpeg: cae al servido directo de abajo

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
        try:
            with open(target, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    await resp.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            return resp
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
        try:
            with open(target, "rb") as f:
                while chunk := f.read(64 * 1024):
                    await resp.write(chunk)
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            return resp

    try:
        await resp.write_eof()
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        pass
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
    target = _safe_join(folder, rel)
    if not target or not target.exists() or not target.is_file():
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


_ANALYSIS_CACHE_MAX = 2000  # LRU cap — analyses are tiny (~100 bytes each)
_analysis_cache: "OrderedDict[str, dict]" = OrderedDict()  # path -> analysis result


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

    # Check in-memory cache (LRU touch on hit)
    if rel in _analysis_cache:
        _analysis_cache.move_to_end(rel)
        return web.json_response(_analysis_cache[rel])

    target = _safe_join(folder, rel)
    if not target or not target.exists() or not target.is_file():
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
        _analysis_cache.move_to_end(rel)
        while len(_analysis_cache) > _ANALYSIS_CACHE_MAX:
            _analysis_cache.popitem(last=False)
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
    """Check for update from public manifest hosted on Netlify, download if
    available, and restart. Uses djfreeapp.ar/agent/version.json instead of
    GitHub API to avoid 2FA / private repo issues."""
    import urllib.request

    update_msg = ""
    try:
        url = "https://djfreeapp.ar/agent/version.json"
        req = urllib.request.Request(url, headers={"User-Agent": "DJFreeAppAgent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = (data.get("version", "") or "").lstrip("v")
        current = VERSION
        # Manifest schema: { version, windows_url, macos_url }
        # Backwards-compat: also accept GitHub-style "assets" array.
        win_url = data.get("windows_url") or "https://djfreeapp.ar/GrooveSyncAgent.exe"
        mac_url = data.get("macos_url") or "https://djfreeapp.ar/GrooveSyncAgent-macOS.zip"

        if latest > current:
            log.info("Update available: v%s -> v%s", current, latest)

            if sys.platform == "darwin":
                import zipfile
                zip_url = mac_url
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
                exe_url = win_url
                if exe_url:
                    tmp_path = Path(os.environ.get("TEMP", "/tmp")) / "GrooveSyncAgent_update.exe"
                    log.info("Downloading update from %s", exe_url)
                    urllib.request.urlretrieve(exe_url, str(tmp_path))

                    # In onefile mode, sys.executable points to temp python.exe — use the real exe path
                    current_exe = Path(sys.executable) if not hasattr(sys, '_MEIPASS') else Path(sys.argv[0]).resolve()
                    if getattr(sys, 'frozen', False) or str(current_exe).endswith('.exe'):
                        bat = Path(os.environ.get("TEMP", "/tmp")) / "groovesync_update.bat"
                        # Rename-trick: en Windows podes RENOMBRAR un .exe en
                        # uso aunque no podes sobreescribirlo. Por eso el bat
                        # primero mueve el actual a .old (libera el nombre),
                        # despues copia el nuevo, y arranca. El viejo .exe
                        # sigue corriendo desde su .old y muere cuando ya no
                        # lo necesita nadie. Sin esto el copy fallaba por
                        # file lock y el update no entraba nunca.
                        old_exe = current_exe.with_suffix('.exe.old')
                        bat.write_text(f"""@echo off
timeout /t 2 /nobreak >nul
del /F /Q "{old_exe}" 2>nul
move /Y "{current_exe}" "{old_exe}"
copy /Y "{tmp_path}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
""", encoding="utf-8")
                        update_msg = f"Actualizando a v{latest}..."
                        log.info("Launching update+restart bat (rename-trick)...")
                        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
                        # Self-kill defensivo: aunque el rename-trick
                        # permite que el viejo siga vivo, conviene matar
                        # este proceso despues que la response salio para
                        # liberar el puerto 9900 antes que arranque el nuevo.
                        async def _selfkill():
                            await asyncio.sleep(3.0)
                            log.info("Self-exiting so new exe can bind port %d", PORT)
                            os._exit(0)
                        asyncio.create_task(_selfkill())
                    else:
                        update_msg = f"v{latest} disponible (solo .exe compilado)"
                else:
                    update_msg = "No se encontró .exe en el release"
        else:
            # Already up to date — just restart the process
            update_msg = f"Ya en v{current}, reiniciando..."
            log.info("No update needed, restarting current version...")

            exe = Path(sys.argv[0]).resolve() if hasattr(sys, '_MEIPASS') else Path(sys.executable)
            if getattr(sys, 'frozen', False) or str(exe).endswith('.exe'):
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
    log.info("-> %s %s (from %s)", request.method, request.path, request.headers.get("Origin", "direct"))
    try:
        response = await handler(request)
        log.info("<- %s %s -> %s", request.method, request.path, response.status)
        return response
    except Exception as e:
        log.error("<- %s %s -> ERROR: %s", request.method, request.path, e)
        raise


def _ui_dist_dir() -> Path | None:
    """Locate the bundled UI dist directory, if present."""
    base = Path(__file__).parent / "ui-dist"
    if base.exists() and (base / "index.html").exists():
        return base
    return None


async def _serve_index(request):
    ui = _ui_dist_dir()
    if not ui:
        return web.Response(status=404, text="UI not bundled. Open https://groovesyncdj.netlify.app instead.")
    return web.FileResponse(ui / "index.html", headers={"Cache-Control": "no-cache"})


async def _serve_spa_fallback(request):
    # Don't swallow unknown /api/* — let those 404 explicitly.
    if request.path.startswith("/api/"):
        return web.Response(status=404, text="Not found")
    return await _serve_index(request)


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
    app.router.add_post("/api/write-tags", handle_write_tags)
    app.router.add_post("/api/fix-metadata", handle_fix_metadata)
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
    app.router.add_post("/api/slsk-download", handle_slsk_download)
    app.router.add_post("/api/slsk-search", handle_slsk_search)
    app.router.add_post("/api/slsk-reconnect", handle_slsk_reconnect)
    app.router.add_post("/api/relogin", handle_slsk_reconnect)
    app.router.add_post("/api/tunnel-start", handle_tunnel_start)
    app.router.add_post("/api/tunnel-stop", handle_tunnel_stop)
    app.router.add_get("/api/tunnel-status", handle_tunnel_status)

    # Serve the UI from the agent itself (when bundled). Lets you go to
    # http://localhost:9900/ and skip Tailscale + cloud entirely for desktop use.
    ui = _ui_dist_dir()
    if ui:
        if (ui / "assets").exists():
            app.router.add_static("/assets", ui / "assets")
        for fname in ("manifest.json", "service-worker.js", "favicon.ico", "favicon.png", "logo.png", "icon-192.png", "icon-512.png", "icon-192-v2.png", "icon-512-v2.png"):
            fpath = ui / fname
            if fpath.exists():
                app.router.add_get(f"/{fname}", lambda req, p=fpath: web.FileResponse(p))
        app.router.add_get("/", _serve_index)
        # SPA fallback — must be last so explicit routes win
        app.router.add_get("/{tail:.*}", _serve_spa_fallback)
        log.info("Serving UI from %s", ui)
    return app


async def start_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info("HTTP server running on http://%s:%d", HOST, PORT)
    return runner


# ---------------------------------------------------------------------------
# System Tray
# ---------------------------------------------------------------------------


def _create_tray_icon() -> Image.Image:
    """Load the app logo for the tray icon."""
    size = 64
    meipass = Path(getattr(sys, '_MEIPASS', ''))
    for logo_name in ["logo.png", "logo_transparent.png", "icon.ico", "menubar_icon.png"]:
        for parent in [meipass, Path(__file__).parent, Path.cwd()]:
            if not str(parent).strip():
                continue
            logo_path = parent / logo_name
            if logo_path.exists():
                try:
                    img = Image.open(logo_path).convert("RGBA")
                    img = img.resize((size, size), Image.LANCZOS)
                    return img
                except Exception:
                    pass
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


def _reveal_path(path):
    """Open the OS file manager with `path` selected/highlighted."""
    if sys.platform == "win32":
        # explorer /select,FULLPATH abre la carpeta con el file marcado.
        # Pasamos como LISTA: si pasabamos string y el filename tenia & o
        # caracteres reservados de cmd, se truncaba el comando y abria nada.
        # Con lista Windows Popen escapa los argumentos correctamente.
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        # Most Linux file managers don't support reveal-with-selection
        # uniformly; open the parent folder instead.
        parent = str(Path(path).parent)
        subprocess.Popen(["xdg-open", parent])


def _do_check_update(notify_fn=None):
    """Check for updates. notify_fn(msg) is called to show messages."""
    import urllib.request
    if notify_fn is None:
        notify_fn = lambda msg: None
    try:
        url = "https://djfreeapp.ar/agent/version.json"
        req = urllib.request.Request(url, headers={"User-Agent": "DJFreeAppAgent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = (data.get("version", "") or "").lstrip("v")
        current = VERSION
        if _ver_tuple(latest) <= _ver_tuple(current):
            log.info("Already up to date: v%s", current)
            notify_fn(f"Ya tenés la última versión (v{current})")
            return

        log.info("New version available: v%s -> v%s", current, latest)
        win_url = data.get("windows_url") or "https://djfreeapp.ar/DjFreeAppAgent.exe"
        mac_url = data.get("macos_url") or "https://djfreeapp.ar/DjFreeAppAgent-macOS.zip"

        if sys.platform == "darwin":
            import zipfile
            zip_url = mac_url
            if not zip_url:
                log.error("No macOS zip found in release")
                notify_fn("No se encontró build de macOS en el release")
                return

            tmp_zip = Path("/tmp") / "DjFreeAppAgent-macOS.zip"
            tmp_extract = Path("/tmp") / "DjFreeAppAgent_update"
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
                new_app = tmp_extract / "DjFreeAppAgent.app"
                if new_app.exists() and current_app.name.endswith(".app"):
                    # Use a shell script to replace after exit
                    update_sh = Path("/tmp") / "djfreeapp_update.sh"
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
            exe_url = win_url
            if not exe_url:
                log.error("No exe url in manifest")
                return

            tmp_path = Path(os.environ.get("TEMP", "/tmp")) / "DjFreeAppAgent_update.exe"
            log.info("Downloading update from %s", exe_url)
            urllib.request.urlretrieve(exe_url, str(tmp_path))
            log.info("Downloaded update to %s", tmp_path)

            current_exe = Path(sys.executable)
            if getattr(sys, 'frozen', False):
                bat = Path(os.environ.get("TEMP", "/tmp")) / "djfreeapp_update.bat"
                bat.write_text(f"""@echo off
timeout /t 2 /nobreak >nul
copy /Y "{tmp_path}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
""", encoding="utf-8")
                log.info("Launching update script for .exe, restarting...")
                notify_fn(f"Actualizando a v{latest}...")
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
                os._exit(0)
            else:
                try:
                    raw_agent_url = "https://raw.githubusercontent.com/arenazl/slsk-agent/master/agent.py"
                    agent_file = Path(__file__).resolve()
                    log.info("Downloading raw agent.py update from %s to %s", raw_agent_url, agent_file)
                    notify_fn(f"Actualizando script agent.py a v{latest}...")
                    urllib.request.urlretrieve(raw_agent_url, str(agent_file))
                    log.info("Script agent.py updated successfully to v%s", latest)
                    notify_fn(f"Agente actualizado a v{latest}. Reiniciando...")
                    subprocess.Popen([sys.executable, str(agent_file)] + sys.argv[1:])
                    os._exit(0)
                except Exception as ex_script:
                    log.error("Failed updating script agent.py: %s", ex_script)
                    notify_fn(f"Error al actualizar agent.py: {ex_script}")
    except Exception as e:
        log.error("Update failed: %s", e)
        notify_fn(f"Error al actualizar: {e}")


def _start_auto_update_checker():
    """Background thread that automatically polls for agent updates every 5 minutes."""
    def _checker():
        time.sleep(5)
        while True:
            try:
                def _auto_notify(msg):
                    log.info("[auto-update] %s", msg)
                    _show_msg("DjFreeApp Agent", msg)
                _do_check_update(notify_fn=_auto_notify)
            except Exception as e:
                log.debug("[auto-update] Check failed: %s", e)
            time.sleep(300)
    threading.Thread(target=_checker, daemon=True).start()


# ---------------------------------------------------------------------------
# macOS tray (rumps)
# ---------------------------------------------------------------------------

if sys.platform == "darwin":
    import rumps

    class DjFreeAppMacApp(rumps.App):
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
            super().__init__("DjFreeApp", icon=icon_path)
            self.menu = [
                rumps.MenuItem("Abrir carpeta de descargas", callback=self._on_open_folder),
                rumps.MenuItem("Configurar carpeta...", callback=self._on_configure_folder),
                None,
                rumps.MenuItem("Renovar charts", callback=self._on_refresh_charts),
                rumps.MenuItem("Buscar actualizaciones", callback=self._on_check_update),
                rumps.MenuItem("Estado", callback=self._on_status),
            ]

        def _on_open_folder(self, sender):
            folder = get_download_folder()
            if folder and os.path.exists(folder):
                _open_path(folder)
            else:
                log.warning("Download folder does not exist: %s", folder)
                _on_configure_folder(None, None)

        def _on_configure_folder(self, sender):
            folder = _pick_folder()
            if folder:
                set_download_folder(folder)
                log.info("Folder configured via macOS tray: %s", folder)

        def _on_refresh_charts(self, sender):
            log.info("Manual chart refresh requested via macOS tray")
            rumps.notification("DjFreeApp Agent", "", "Renovando charts...")
            def do_scrape():
                loop = asyncio.new_event_loop()
                try:
                    count = loop.run_until_complete(scrape_beatport_charts())
                    log.info("Manual scrape done: %d charts", count)
                    rumps.notification("DjFreeApp Agent", "", f"Charts actualizados: {count} géneros")
                except Exception as e:
                    log.error("Manual scrape failed: %s", e)
                    rumps.notification("DjFreeApp Agent", "Error", f"Fallo al actualizar charts: {e}")
                finally:
                    loop.close()
            threading.Thread(target=do_scrape, daemon=True).start()

        def on_view_logs(self, _):
            _open_path(str(LOG_FILE))

        def on_update(self, _):
            log.info("Checking for updates...")
            def do():
                _do_check_update(
                    notify_fn=lambda msg: rumps.notification("DJ Free App Agent", "", msg)
                )
            threading.Thread(target=do, daemon=True).start()

        def on_quit(self, _):
            log.info("Agent shutting down via tray menu")
            rumps.quit_application()
            os._exit(0)


# ---------------------------------------------------------------------------
# Windows/Linux tray (pystray)
# ---------------------------------------------------------------------------

def _on_open_ui(icon, item):
    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}/")


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

def _show_msg(title, text, icon_type=0x40):
    if sys.platform == "win32":
        import ctypes
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(0, text, title, icon_type), daemon=True).start()
    else:
        log.info("[%s] %s", title, text)

def _on_status(icon, item):
    folder = get_download_folder() or "(no configurada)"
    msg = f"Carpeta: {folder}\nPuerto: {PORT}\nVersión: {VERSION}\nBackend: {SERVER_URL}"
    log.info("Status \u2014 Folder: %s, Port: %d, Version: %s, Backend: %s", folder, PORT, VERSION, SERVER_URL)
    _show_msg("Estado del Agente", msg)


def _on_refresh_charts(icon, item):
    log.info("Manual chart refresh requested")
    _show_msg("DJ Free App Agent", "Renovando charts...")
    def do_scrape():
        loop = asyncio.new_event_loop()
        try:
            count = loop.run_until_complete(scrape_beatport_charts())
            log.info("Manual scrape done: %d charts", count)
            _show_msg("DJ Free App Agent", f"Charts actualizados: {count} géneros")
        except Exception as e:
            log.error("Manual scrape failed: %s", e)
        finally:
            loop.close()
    threading.Thread(target=do_scrape, daemon=True).start()


def _on_slsk_reconnect(icon, item):
    log.info("[tray] Renovando sesión de Soulseek manualmente...")
    _show_msg("Soulseek", "Renovando sesión de Soulseek...")
    def do_reconnect():
        loop = asyncio.new_event_loop()
        try:
            cfg = load_config()
            u = cfg.get("username") or "arenazl"
            p = cfg.get("password") or "look"
            loop.run_until_complete(get_slsk_client(u, p, force_relogin=True))
            log.info("[tray] Sesión de Soulseek renovada con éxito para %s", u)
            _show_msg("Soulseek", f"Sesión de Soulseek renovada para {u}")
        except Exception as e:
            log.error("[tray] Error al renovar sesión: %s", e)
            _show_msg("Soulseek Error", f"Error al renovar sesión: {e}")
        finally:
            loop.close()
    threading.Thread(target=do_reconnect, daemon=True).start()


def _on_view_logs(icon, item):
    _open_path(str(LOG_FILE))


def _on_update(icon, item):
    log.info("Checking for updates...")
    _show_msg("DJ Free App Agent", "Buscando actualizaciones...")
    def do():
        def notify_fn(msg):
            _show_msg("DJ Free App Agent", msg)
        _do_check_update(notify_fn)
    threading.Thread(target=do, daemon=True).start()


def _on_exit(icon, item):
    log.info("Agent shutting down via tray menu")
    icon.stop()
    os._exit(0)


# ---------------------------------------------------------------------------
# Tray resilience — re-assert the icon when explorer.exe restarts
# ---------------------------------------------------------------------------
# pystray's own WM_TASKBARCREATED handler re-shows the icon on a *single*,
# clean explorer restart, but it's a one-shot broadcast with no retry. Under a
# burst of explorer crashes/restarts (observed 2026-06-07: 4 restarts in ~13
# min) the message is missed and the icon vanishes while the agent keeps
# running headless. This watchdog polls explorer's PID set and re-asserts the
# icon whenever explorer was fully replaced — independent of the broadcast.

def _explorer_pids() -> set:
    """Return the set of explorer.exe PIDs. Empty set on non-Windows or error."""
    if sys.platform != "win32":
        return set()
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    pids: set = set()
    # restype/argtypes are mandatory on x64: a HANDLE is 64-bit and ctypes
    # defaults to c_int (32-bit), which truncates the handle and corrupts
    # CloseHandle / the invalid-handle check.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    INVALID = ctypes.c_void_p(-1).value  # INVALID_HANDLE_VALUE, platform-correct
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID:
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if (entry.szExeFile or "").lower() == "explorer.exe":
                pids.add(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


def _reassert_tray_icon(icon):
    """Ask pystray to re-add the icon, ON ITS OWN message-loop thread.

    We must NOT mutate icon.visible from the watchdog thread: pystray's Win32
    backend reads/writes icon._visible from its message loop with no lock, so a
    cross-thread toggle races (duplicate NIM_ADD / lost re-assert). Instead we
    PostMessage the very same WM_TASKBARCREATED that Windows broadcasts on a real
    taskbar rebuild; pystray's _on_taskbarcreated handler then re-shows the icon
    in the correct thread. Idempotent and flicker-free (no hide/show toggle).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = getattr(icon, "_hwnd", None)
        if not hwnd:
            return  # message window not created yet (icon.run() hasn't started)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        # argtypes are mandatory: HWND is 64-bit on x64, default c_int truncates.
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        # "TaskbarCreated" is a registered message — same value process-wide.
        msg = user32.RegisterWindowMessageW("TaskbarCreated")
        if msg:
            user32.PostMessageW(wintypes.HWND(int(hwnd)), msg, 0, 0)
            log.info("Tray icon re-assert posted (WM_TASKBARCREATED)")
    except Exception:
        log.exception("Failed to re-assert tray icon")


def _tray_watchdog(icon):
    """Daemon loop: re-assert the tray icon when explorer's shell is rebuilt.

    Fires on (a) any newly-appeared explorer PID, or (b) explorer having fully
    vanished and come back (covers PID recycling within a poll window). The
    re-assert is idempotent and invisible, so being slightly liberal here is
    safe — a redundant post is a no-op, a missed one leaves the icon gone.
    """
    if sys.platform != "win32":
        return
    last = _explorer_pids()
    explorer_was_down = False
    while True:
        time.sleep(5)
        try:
            cur = _explorer_pids()
            if not cur:
                # explorer is gone right now (mid-restart) — remember and wait
                explorer_was_down = True
                continue
            appeared = cur - last
            if appeared or explorer_was_down:
                log.info("explorer change detected (pids %s -> %s, was_down=%s)",
                         last, cur, explorer_was_down)
                _reassert_tray_icon(icon)
            explorer_was_down = False
            last = cur
        except Exception as e:
            log.debug("tray watchdog error: %s", e)


def run_tray(ready_event: threading.Event):
    """Run the pystray icon (Windows/Linux only)."""
    icon = pystray.Icon(
        "groovesync",
        _create_tray_icon(),
        f"DJ Free App v{VERSION} - Online",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir UI", _on_open_ui, default=True),
            pystray.MenuItem("Abrir carpeta", _on_open_folder),
            pystray.MenuItem("Configurar carpeta", _on_configure_folder),
            pystray.MenuItem("Renovar Sesión Soulseek", _on_slsk_reconnect),
            pystray.MenuItem("Renovar Charts", _on_refresh_charts),
            pystray.MenuItem("Estado", _on_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ver logs", _on_view_logs),
            pystray.MenuItem("Actualizar", _on_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", _on_exit),
        ),
    )
    # Watchdog re-asserts the icon if explorer restarts (pystray's broadcast
    # handler alone is unreliable under rapid explorer crashes).
    threading.Thread(target=_tray_watchdog, args=(icon,), daemon=True).start()
    ready_event.set()
    icon.run()


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------


def first_run_setup():
    """Prompt user to pick a download folder on first run or if configured folder no longer exists."""
    current = get_download_folder()
    if current and Path(current).exists():
        return

    if current:
        log.warning("Configured folder %s no longer exists — re-prompting", current)
    else:
        log.info("First run detected — prompting for download folder")
    folder = _pick_folder()
    if folder:
        set_download_folder(folder)
        log.info("Folder set to: %s", folder)
    else:
        default = str(Path.home() / "Music" / "GrooveSync")
        Path(default).mkdir(parents=True, exist_ok=True)
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


def _maybe_start_tunnel(loop):
    """Auto-start del WS reverse tunnel si config.json tiene username.
    Sin esto el .exe queda con tunnel-running=false y la UI cloud no lo alcanza."""
    global _tunnel_task, _tunnel_user, _tunnel_started_at
    cfg = load_config()
    user = (cfg.get("username") or "").strip()
    if not user:
        log.info("[TUNNEL] no username in config — skipping auto-start")
        return
    _tunnel_user = user
    _tunnel_started_at = time.time()
    _tunnel_task = loop.create_task(_tunnel_loop(user))
    log.info("[TUNNEL] auto-starting for user %s", user)


def _run_server_in_thread():
    """Run the async HTTP server in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        runner = loop.run_until_complete(start_server())
        loop.create_task(beatport_scrape_loop())
        _maybe_start_tunnel(loop)
        log.info("Agent ready — listening on port %d", PORT)
        loop.run_forever()
    except Exception as e:
        log.error("Server error: %s", e)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------
# Without this, double-clicking the .exe while one is already running spawned a
# second process that died ~22 ms later on the port-9900 bind — silently ("lo
# corro y no pasa nada"). A named mutex lets us detect the running instance up
# front and tell the user where to look instead of failing quietly.

_singleton_handle = None  # keep the mutex handle alive for the process lifetime


def _acquire_single_instance() -> bool:
    """Return True if this is the only instance. Windows-only (named mutex);
    other platforms always return True (the port bind still guards there)."""
    global _singleton_handle
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    ERROR_ALREADY_EXISTS = 183
    # use_last_error + ctypes.get_last_error() is the reliable way to read the
    # error of the *last* foreign call; windll.kernel32.GetLastError() makes an
    # extra call that can clobber it. restype=HANDLE avoids 64-bit truncation.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    # Session-local name (no "Global\\") => one instance per logged-in user.
    handle = k32.CreateMutexW(None, False, "GrooveSyncAgent_singleton")
    err = ctypes.get_last_error()
    if not handle:
        # Couldn't create the mutex — fail open so we never block a real launch.
        return True
    if err == ERROR_ALREADY_EXISTS:
        return False
    _singleton_handle = handle
    return True


def _warn_already_running():
    """Tell the user an instance is live and where its tray icon hides."""
    log.info("Another instance is already running — exiting this one")
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "DJ Free App ya esta corriendo.\n\n"
                "Busca el icono 'G' en la bandeja del sistema "
                "(la flecha ^ junto al reloj).",
                "DJ Free App Agent",
                0x40,  # MB_ICONINFORMATION
            )
        except Exception:
            pass


def main():
    log.info("=== DJ Free App Agent v%s starting ===", VERSION)

    # Single-instance guard: bail out *before* the folder picker / port bind so
    # a duplicate launch reports clearly instead of dying silently.
    if not _acquire_single_instance():
        _warn_already_running()
        return

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
            _maybe_start_tunnel(loop)
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
