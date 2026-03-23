# SoulSeek Agent

Local agent that runs in the Windows system tray and manages audio file downloads and library organization. Communicates with the web frontend via a local HTTP server on port 9900.

## Setup

1. Install Python 3.10+
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Run the agent:
   ```
   python agent.py
   ```
4. On first run, you will be prompted to select your download folder.

## Build Executable

Run `build.bat` to create a standalone `.exe` file using PyInstaller. The executable will be in the `dist/` folder.

## Configuration

Config is stored at `~/.slsk-agent/config.json`. Logs are written to `~/.slsk-agent/agent.log`.

## API Endpoints (port 9900)

- `GET /api/status` — Agent status and configured folder
- `POST /api/save-file` — Save an uploaded file (multipart)
- `POST /api/move-file` — Move a file between genre folders
- `GET /api/library` — List all audio files with metadata
- `POST /api/config` — Update download folder
- `POST /api/rate` — Set rating for a file
- `POST /api/delete` — Delete a file
- `GET /api/open-folder` — Open folder in Windows Explorer
